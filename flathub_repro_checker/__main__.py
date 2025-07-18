import argparse
import contextlib
import errno
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import types
from subprocess import CompletedProcess
from typing import Any, TextIO

from . import __version__

REPRO_DATADIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "flathub_repro_checker"
)
FLATPAK_ROOT_DIR = os.path.join(REPRO_DATADIR, "flatpak_root")
FLATPAK_BUILDER_STATE_DIR = os.path.join(REPRO_DATADIR, "flatpak_builder_state")
os.makedirs(REPRO_DATADIR, exist_ok=True)
os.makedirs(FLATPAK_BUILDER_STATE_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


class Lock:
    def __init__(self, path: str) -> None:
        self.lock_path: str = path
        self.lock_file: TextIO | None = None
        self.locked: bool = False

    def acquire(self) -> None:
        if self.locked:
            logging.warning("Lock already acquired: %s", self.lock_path)
            return

        self.lock_file = open(self.lock_path, "w")  # noqa: SIM115
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.locked = True
            logging.info("Lock acquired: %s", self.lock_path)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                logging.error("Another instance is already running. Exiting")
                raise SystemExit(1) from e
            raise

    def release(self) -> None:
        if self.locked and self.lock_file:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()
            with contextlib.suppress(FileNotFoundError):
                os.remove(self.lock_path)
            self.locked = False
            logging.info("Lock released and lockfile deleted: %s", self.lock_path)

    def __enter__(self) -> "Lock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.release()


def _run_command(
    command: list[str],
    check: bool = True,
    capture_output: bool = False,
    cwd: str | None = None,
    message: str | None = None,
    warn: bool = False,
    env: dict[str, str] | None = None,
) -> CompletedProcess[str] | None:
    try:
        cmd_str = " ".join(command)
        msg = f"Running: {cmd_str}"
        if cwd:
            msg += f" in directory {os.path.abspath(cwd)}"
        logging.info("%s", msg)

        return subprocess.run(
            command,
            check=check,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            text=True,
            cwd=cwd,
            env=env,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip() if e.stderr else ""
        stdout = e.stdout.strip() if e.stdout else ""
        stdout_lines = "\n".join(stdout.splitlines()[-30:]) if stdout else ""
        if stdout_lines:
            logging.error("Last lines of stdout:\n%s", stdout_lines)
        log_func = logging.warning if warn else logging.error
        if message:
            log_func("%s: %s", message, stderr)
        else:
            logging.error("Command failed: %s\nError: %s", " ".join(command), stderr)
        return None


def _run_flatpak(
    args: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    cwd: str | None = None,
    message: str | None = None,
    warn: bool = False,
    env: dict[str, str] | None = None,
) -> CompletedProcess[str] | None:
    env = os.environ.copy()
    env["FLATPAK_USER_DIR"] = FLATPAK_ROOT_DIR

    return _run_command(
        ["flatpak", *args],
        check=check,
        capture_output=capture_output,
        cwd=cwd,
        message=message,
        warn=warn,
        env=env,
    )


def get_flatpak_arch() -> str | None:
    ret = _run_flatpak(
        ["--default-arch"], capture_output=True, message="Failed to get Flatpak arch"
    )
    return ret.stdout.strip() if ret else None


def setup_flathub() -> bool:
    remotes = {"flathub": "https://dl.flathub.org/repo/flathub.flatpakrepo"}
    return all(
        _run_flatpak(
            ["remote-add", "--user", "--if-not-exists", name, url],
            message=f"Failed to add remote '{name}'",
        )
        for name, url in remotes.items()
    )


def is_ref_in_remote(ref_id: str, ref_branch: str = "stable") -> bool:
    ref = f"{ref_id}//{ref_branch}"
    return (
        _run_flatpak(
            ["remote-info", "flathub", ref],
            capture_output=False,
            message=f"Failed to run remote-info for '{ref}'",
        )
        is not None
    )


def install_flatpak(ref: str) -> bool:
    return (
        _run_flatpak(
            ["install", "--assumeyes", "--noninteractive", "--user", "--or-update", "flathub", ref],
            message=f"Failed to install or update {ref}",
        )
        is not None
    )


def flatpak_mask(ref: str, remove: bool = False) -> bool:
    args = ["mask", "--user"]
    if remove:
        args.append("--remove")
    args.append(ref)
    return (
        _run_flatpak(args, message=f"Failed to {'unmask' if remove else 'mask'} '{ref}'")
        is not None
    )


def get_manifest_output_path(flatpak_id: str) -> str:
    manifest_dir = os.path.abspath(os.path.join(REPRO_DATADIR, flatpak_id))
    os.makedirs(manifest_dir, exist_ok=True)
    return os.path.abspath(os.path.join(manifest_dir, f"{flatpak_id}.json"))


def get_saved_manifest_path(flatpak_id: str) -> str | None:
    path = get_manifest_output_path(flatpak_id)
    return path if os.path.isfile(path) else None


def save_manifest(flatpak_id: str) -> bool:
    output_path = get_manifest_output_path(flatpak_id)
    if os.path.exists(output_path):
        os.remove(output_path)
    ref = f"{flatpak_id}//stable"
    result = _run_flatpak(
        ["run", "--command=/usr/bin/cat", ref, "/app/manifest.json"],
        capture_output=True,
        message=f"Failed to extract manifest from {ref}",
    )
    if result is None:
        return False
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(result.stdout)
    return True


def parse_manifest(flatpak_id: str) -> dict[str, Any]:
    manifest: dict[str, Any] = {}
    path = get_saved_manifest_path(flatpak_id)
    if path:
        with open(path, encoding="utf-8") as f:
            manifest = json.load(f)
    return manifest


def get_runtime_ref(flatpak_id: str) -> list[str]:
    manifest = parse_manifest(flatpak_id)
    return [f"{manifest['runtime']}//{manifest['runtime-version']}"]


def get_sdk_ref(flatpak_id: str) -> list[str]:
    manifest = parse_manifest(flatpak_id)
    return [f"{manifest['sdk']}//{manifest['runtime-version']}"]


def get_baseapp_ref(flatpak_id: str) -> list[str]:
    manifest = parse_manifest(flatpak_id)
    base = manifest.get("base")
    base_version = manifest.get("base-version")
    return [f"{base}//{base_version}"] if base and base_version else []


def get_base_runtime_version(ref_id: str, ref_branch: str) -> str | None:
    base_runtime_version = None
    ref = f"{ref_id}//{ref_branch}"
    result = _run_flatpak(
        ["remote-info", "-m", "flathub", ref],
        capture_output=True,
    )
    if result is None:
        logging.error("Failed to run remote-info on '%s'", ref)

    if result is not None:
        version_pattern = re.compile(r"^2\d\.08$")
        in_target_section = False
        versions: list[str] = []

        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_target_section = stripped == "[Extension org.freedesktop.Platform.GL]"
                continue
            if not in_target_section or "=" not in stripped:
                continue

            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()

            if key == "versions":
                versions.extend(v.strip() for v in value.split(";"))
            elif key == "version":
                versions.append(value)

        for version in versions:
            if version_pattern.fullmatch(version):
                base_runtime_version = version

    if not base_runtime_version:
        logging.error(
            "Failed to determine the version of the base runtime for '%s'."
            "This may result in missing build dependencies during the build process",
            ref,
        )

    return base_runtime_version


def get_build_extension_refs(flatpak_id: str) -> list[str]:
    manifest = parse_manifest(flatpak_id)
    sdk_exts = manifest.get("sdk-extensions", [])
    add_build_exts = manifest.get("add-build-extensions", {})
    refs: list[str] = []
    if sdk_exts:
        runtime_ref = get_runtime_ref(flatpak_id)[0]
        runtime_id, runtime_branch = runtime_ref.split("//", 1)
        base_branch = get_base_runtime_version(runtime_id, runtime_branch)
        if base_branch:
            for s in sdk_exts:
                refs.append(f"{s}//{base_branch}")
    for ext_id, ext_info in add_build_exts.items():
        refs.append(f"{ext_id}//{ext_info.get('version', 'stable')}")
    return refs


def get_sources_ref(flatpak_id: str) -> list[str]:
    sources_ref: list[str] = []
    sources_ref_str = f"runtime/{flatpak_id}.Sources/x86_64/stable"
    if is_ref_in_remote(sources_ref_str):
        sources_ref = [sources_ref_str]
    else:
        logging.warning("Failed to find sources extension for %s", flatpak_id)
    return sources_ref


def get_build_deps_refs(flatpak_id: str) -> list[str]:
    return list(
        {
            *get_runtime_ref(flatpak_id),
            *get_sdk_ref(flatpak_id),
            *get_build_extension_refs(flatpak_id),
            *get_baseapp_ref(flatpak_id),
            *get_sources_ref(flatpak_id),
        }
    )


def get_pinned_refs(flatpak_id: str) -> dict[str, str]:
    manifest = parse_manifest(flatpak_id)
    refs = {
        get_runtime_ref(flatpak_id)[0]: manifest["runtime-commit"],
        get_sdk_ref(flatpak_id)[0]: manifest["sdk-commit"],
    }
    baseapp = get_baseapp_ref(flatpak_id)
    if baseapp:
        refs[baseapp[0]] = manifest["base-commit"]
    return refs


def install_build_deps_refs(flatpak_id: str) -> bool:
    return all(install_flatpak(ref) for ref in get_build_deps_refs(flatpak_id))


def update_refs_to_pinned_commit(flatpak_id: str) -> bool:
    success = True
    for ref, commit in get_pinned_refs(flatpak_id).items():
        result = _run_flatpak(
            [
                "update",
                "--assumeyes",
                "--noninteractive",
                "--no-related",
                "--no-deps",
                f"--commit={commit}",
                ref,
            ],
            message=f"Failed to pin {ref} to commit {commit}",
        )
        if result is None:
            success = False
    return success


def handle_build_deps(flatpak_id: str) -> bool:
    if not install_build_deps_refs(flatpak_id):
        return False
    if not update_refs_to_pinned_commit(flatpak_id):
        return False
    return all(flatpak_mask(ref) for ref in get_pinned_refs(flatpak_id))


def create_flatpak_builder_state_dir(flatpak_id: str) -> str | None:
    path = os.path.join(FLATPAK_BUILDER_STATE_DIR, f"flatpak_builder_state-{flatpak_id}")
    os.makedirs(path, exist_ok=True)
    return path if os.path.isdir(path) else None


def build_flatpak(manifest_path: str) -> bool:
    manifest_dir = os.path.dirname(manifest_path)
    manifest_file = os.path.basename(manifest_path)
    flatpak_id = os.path.splitext(manifest_file)[0]
    arch = get_flatpak_arch()
    state_dir = create_flatpak_builder_state_dir(flatpak_id)
    if not arch:
        return False

    # not combined as mypy complains
    if not state_dir:
        return False

    sources_ext = f"{flatpak_id}.Sources"
    sources_dir = os.path.join(
        FLATPAK_ROOT_DIR, "runtime", sources_ext, "x86_64", "stable", "active", "files"
    )
    sources_manifest_dir = os.path.join(sources_dir, "manifest")
    sources_downloads_dir = os.path.join(sources_dir, "downloads")

    state_dir_downloads = os.path.join(state_dir, "downloads")
    os.makedirs(state_dir_downloads, exist_ok=True)

    if os.path.isdir(sources_manifest_dir):
        for item in os.listdir(sources_manifest_dir):
            src = os.path.join(sources_manifest_dir, item)
            dest = os.path.join(manifest_dir, item)

            if os.path.isfile(src) and item.endswith(
                (f"{flatpak_id}.json", f"{flatpak_id}.yml", f"{flatpak_id}.yaml")
            ):
                continue

            if os.path.isdir(src):
                if os.path.exists(dest):
                    shutil.rmtree(dest)
                shutil.copytree(src, dest)
            else:
                shutil.copy2(src, dest)

    if os.path.isdir(sources_downloads_dir):
        for item in os.listdir(sources_downloads_dir):
            src = os.path.join(sources_downloads_dir, item)
            dest = os.path.join(state_dir_downloads, item)
            if os.path.isdir(src):
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)

    args = [
        "flatpak-builder",
        "--force-clean",
        "--sandbox",
        "--delete-build-dirs",
        "--override-source-date-epoch",
        "1321009871",
        "--user",
        "--ccache",
        "--mirror-screenshots-url=https://dl.flathub.org/media",
        "--repo=repo",
        "--install",
        "--default-branch=repro",
        "--disable-rofiles-fuse",
        f"--state-dir={state_dir}",
        "--assumeyes",
        f"--arch={arch}",
        "builddir",
        manifest_file,
    ]

    env = os.environ.copy()
    env["FLATPAK_USER_DIR"] = FLATPAK_ROOT_DIR

    result = _run_command(
        args,
        cwd=manifest_dir,
        message=f"Failed to run flatpak-builder on {manifest_file}",
        env=env,
        capture_output=True,
    )

    return result is not None


def get_built_app_branch(manifest_path: str) -> str | None:
    repo_path = os.path.join(os.path.dirname(manifest_path), "repo")
    result = _run_command(
        ["ostree", f"--repo={repo_path}", "refs"],
        capture_output=True,
        message=f"Failed to list refs in {repo_path}",
    )

    if result is None:
        return None

    for line in result.stdout.strip().splitlines():
        line_s = line.strip()
        if line_s.startswith("app/"):
            parts = line_s.split("/")
            if len(parts) >= 4:
                return parts[-1]
    return None


def run_diffoscope(folder_a: str, folder_b: str, output_dir: str) -> bool:
    if os.path.isdir(output_dir):
        shutil.rmtree(output_dir, ignore_errors=True)
    cmd = [
        "diffoscope",
        f"--html-dir={output_dir}",
        "--exclude-directory-metadata=recursive",
        folder_a,
        folder_b,
    ]
    result = _run_command(
        cmd, check=False, capture_output=True, message="Diffoscope failed", warn=True
    )
    if result is None:
        return False
    if result.returncode == 0:
        logging.info("Result is reproducible")
    elif result.returncode == 1:
        logging.error("Result is not reproducible")
        return False
    else:
        logging.error("Diffoscope failed with code %d", result.returncode)
        return False
    return True


def validate_env() -> bool:
    required_tools = (
        "flatpak",
        "flatpak-builder",
        "ostree",
        "diffoscope",
    )
    missing = [tool for tool in required_tools if shutil.which(tool) is None]

    if missing:
        for tool in missing:
            logging.error("%s is required but was not found in PATH", tool)
        return False

    return True


def run_repro_check(flatpak_id: str, output_dir: str, args: argparse.Namespace) -> bool:
    backup_install_manifest = None
    backup_rebuilt_manifest = None
    install_app_info_dir = None
    rebuilt_app_info_dir = None
    install_manifest = None
    rebuilt_manifest = None
    backup_dir = None
    backup_install_app_info_dir = None
    backup_rebuilt_app_info_dir = None

    try:
        if not validate_env():
            return False

        if not setup_flathub():
            return False
        if not install_flatpak(f"{flatpak_id}//stable"):
            return False
        if not save_manifest(flatpak_id):
            return False
        if not handle_build_deps(flatpak_id):
            return False

        manifest_path = get_saved_manifest_path(flatpak_id)
        if manifest_path is None:
            logging.error("Manifest path not found")
            return False

        if not build_flatpak(manifest_path):
            return False

        arch = get_flatpak_arch()
        if not arch:
            return False

        built_branch = get_built_app_branch(manifest_path)
        if not built_branch:
            return False

        install_dir = os.path.join(
            FLATPAK_ROOT_DIR, "app", flatpak_id, arch, "stable", "active", "files"
        )
        rebuilt_dir = os.path.join(
            FLATPAK_ROOT_DIR, "app", flatpak_id, arch, built_branch, "active", "files"
        )

        install_manifest = os.path.join(install_dir, "manifest.json")
        rebuilt_manifest = os.path.join(rebuilt_dir, "manifest.json")
        install_app_info_dir = os.path.join(install_dir, "share", "app-info")
        rebuilt_app_info_dir = os.path.join(rebuilt_dir, "share", "app-info")

        backup_dir = os.path.join(REPRO_DATADIR, "backups")
        os.makedirs(backup_dir, exist_ok=True)

        backup_install_manifest = os.path.join(backup_dir, "install_manifest.json")
        backup_rebuilt_manifest = os.path.join(backup_dir, "rebuilt_manifest.json")
        backup_install_app_info_dir = os.path.join(backup_dir, "install_app_info")
        backup_rebuilt_app_info_dir = os.path.join(backup_dir, "rebuilt_app_info")

        if os.path.isfile(install_manifest):
            shutil.move(install_manifest, backup_install_manifest)
        else:
            logging.error("Failed to find manifest from install directory %s", install_dir)
            return False

        if os.path.isfile(rebuilt_manifest):
            shutil.move(rebuilt_manifest, backup_rebuilt_manifest)
        else:
            logging.error("Failed to find manifest from rebuilt directory %s", rebuilt_dir)
            return False

        if os.path.isdir(install_app_info_dir):
            os.makedirs(backup_install_app_info_dir, exist_ok=True)
            shutil.move(install_app_info_dir, os.path.join(backup_install_app_info_dir, "app-info"))
        if os.path.isdir(rebuilt_app_info_dir):
            os.makedirs(backup_rebuilt_app_info_dir, exist_ok=True)
            shutil.move(rebuilt_app_info_dir, os.path.join(backup_rebuilt_app_info_dir, "app-info"))

        return run_diffoscope(install_dir, rebuilt_dir, output_dir)

    finally:
        for ref in get_pinned_refs(flatpak_id):
            flatpak_mask(ref, remove=True)

        app_info_subdir = "app-info"

        if backup_install_manifest and install_manifest and os.path.exists(backup_install_manifest):
            shutil.move(backup_install_manifest, install_manifest)
        if backup_rebuilt_manifest and rebuilt_manifest and os.path.exists(backup_rebuilt_manifest):
            shutil.move(backup_rebuilt_manifest, rebuilt_manifest)

        if (
            backup_install_app_info_dir
            and install_app_info_dir
            and os.path.exists(os.path.join(backup_install_app_info_dir, app_info_subdir))
        ):
            shutil.move(
                os.path.join(backup_install_app_info_dir, app_info_subdir),
                install_app_info_dir,
            )

        if (
            backup_rebuilt_app_info_dir
            and rebuilt_app_info_dir
            and os.path.exists(os.path.join(backup_rebuilt_app_info_dir, app_info_subdir))
        ):
            shutil.move(
                os.path.join(backup_rebuilt_app_info_dir, app_info_subdir),
                rebuilt_app_info_dir,
            )

        if backup_dir and os.path.isdir(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)

        if args.cleanup and os.path.isdir(REPRO_DATADIR):
            shutil.rmtree(REPRO_DATADIR, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Flathub reproducibility checker")
    parser.add_argument("--flatpak-id", required=True, help="Flatpak ID on Flathub stable repo")
    parser.add_argument(
        "--output-dir",
        help="Output dir for diffoscope report (default: ./diffoscope_result-$FLATPAK_ID)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Cleanup all state",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="",
    )
    args = parser.parse_args()

    UNSUPPORTED_FLATPAK_IDS = (
        "org.mozilla.firefox",
        "org.mozilla.Thunderbird",
        "net.pcsx2.PCSX2",
        "org.duckstation.DuckStation",
        "net.wz2100.wz2100",
        "com.obsproject.Studio",
    )

    lockfile_path = os.path.join(REPRO_DATADIR, "flathub_repro_checker.lock")
    with Lock(lockfile_path):
        flatpak_id = args.flatpak_id

        if flatpak_id in UNSUPPORTED_FLATPAK_IDS:
            logging.error("Running the checker against %s is unsupported right now", flatpak_id)
            return 1

        if args.output_dir:
            output_dir = os.path.abspath(args.output_dir)
        else:
            output_dir = os.path.abspath(f"./diffoscope_result-{flatpak_id}")
        os.makedirs(output_dir, exist_ok=True)

        if not run_repro_check(flatpak_id, output_dir, args):
            return 1

        return 0


if __name__ == "__main__":
    raise SystemExit(main())
