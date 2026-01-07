import argparse
import contextlib
import datetime
import errno
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import zipfile
from functools import lru_cache
from subprocess import CompletedProcess
from typing import TYPE_CHECKING, Any, TextIO
from urllib.parse import quote

from . import __version__

try:
    import boto3

    BOTO3_AVAIL = True
except ImportError:
    BOTO3_AVAIL = False

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client  # noqa: F401


ALLOWED_RUNTIMES = (
    "org.freedesktop.Platform",
    "org.freedesktop.Sdk",
    "org.gnome.Platform",
    "org.gnome.Sdk",
    "org.kde.Platform",
    "org.kde.Sdk",
)


def setup_logging(json_mode: bool = False) -> None:
    if json_mode:
        logging.disable(logging.CRITICAL)
    else:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


REPRO_DATADIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "flathub_repro_checker",
)
FLATPAK_ROOT_DIR = os.path.join(REPRO_DATADIR, "flatpak_root")
FLATPAK_BUILDER_STATE_DIR = os.path.join(REPRO_DATADIR, "flatpak_builder_state")


def print_json_output(
    appid: str, status_code: int, msg: str, result_url: str | None = None
) -> None:
    timestamp = str(datetime.datetime.now(datetime.timezone.utc).isoformat())

    gh_server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    gh_repo = os.environ.get("GITHUB_REPOSITORY")
    gh_run_id = os.environ.get("GITHUB_RUN_ID")

    gl_pipeline_url = os.environ.get("CI_PIPELINE_URL")

    if gh_repo and gh_run_id:
        log_url = f"{gh_server_url}/{gh_repo}/actions/runs/{gh_run_id}"
    elif gl_pipeline_url:
        log_url = str(gl_pipeline_url)
    else:
        log_url = ""

    if status_code not in (0, 1, 42):
        print(f"Unknown status code: {status_code}", file=sys.stderr)  # noqa: T201
        sys.exit(1)

    if result_url is None:
        result_url = ""

    ret: dict[str, str] = {
        "timestamp": timestamp,
        "appid": appid,
        "status_code": str(status_code),
        "log_url": log_url,
        "result_url": result_url,
        "message": msg,
    }

    print(json.dumps(ret, indent=4))  # noqa: T201
    sys.exit(0)


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


def is_inside_container() -> bool:
    return any(os.path.exists(p) for p in ("/.dockerenv", "/run/.containerenv"))


def is_root() -> bool:
    return os.geteuid() == 0


def upload_to_s3(path: str) -> str:
    url = ""

    if not BOTO3_AVAIL:
        logging.error("Failed to import boto3")
        return url

    if not os.path.isfile(path):
        logging.error("The file to upload does not exist: %s", path)
        return url

    bucket_name = os.environ.get("AWS_S3_BUCKET_NAME")
    if not bucket_name:
        logging.error("No AWS S3 bucket name is set. Use AWS_S3_BUCKET_NAME environment variable")
        return url

    aws_region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=aws_region)
    object_key = os.path.basename(path)
    try:
        s3.upload_file(path, bucket_name, object_key, ExtraArgs={"ACL": "public-read"})
        if aws_region != "us-east-1":
            url = f"https://{bucket_name}.s3.{aws_region}.amazonaws.com/{quote(object_key)}"
        else:
            url = f"https://{bucket_name}.s3.amazonaws.com/{quote(object_key)}"
    except Exception as err:
        logging.error("Failed to upload file: %s", str(err))
    return url


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
            msg += f" in directory: {os.path.abspath(cwd)}"
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
        stdout_lines = stdout.splitlines()[-100:] if stdout else []
        if stdout_lines:
            keywords = re.compile(
                r"^(error|fail|failed|failure|abort|aborted|fatal)", re.IGNORECASE
            )
            important = [line.strip() for line in stdout_lines if keywords.match(line.strip())]
            if important:
                for line in important:
                    logging.error("%s", line)
        log_func = logging.warning if warn else logging.error
        if message:
            if stderr:
                log_func("%s: %s", message, stderr)
            else:
                log_func("%s", message)
        elif stderr:
            logging.error("Command failed: %s\nError: %s", " ".join(command), stderr)
        else:
            logging.error("Command failed: %s", " ".join(command))
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
    if "FLATPAK_USER_DIR" not in env:
        env["FLATPAK_USER_DIR"] = FLATPAK_ROOT_DIR

    if is_inside_container():
        env["FLATPAK_SYSTEM_HELPER_ON_SESSION"] = "foo"

    return _run_command(
        ["flatpak", *args],
        check=check,
        capture_output=capture_output,
        cwd=cwd,
        message=message,
        warn=warn,
        env=env,
    )


def configure_git_file_protocol(unset: bool) -> bool:
    if not unset:
        result = _run_command(
            ["git", "config", "--global", "protocol.file.allow", "always"],
            message="Failed to set git file protocol config",
        )
    else:
        result = _run_command(
            ["git", "config", "--global", "--unset", "protocol.file.allow"],
            check=False,
            message="Failed to unset git file protocol config",
        )

    if result:
        action = "unset" if unset else "set"
        logging.info("Successfully %s git file protocol config", action)
        return True
    return False


def _invalidate_manifest_cache() -> None:
    for fn in (
        parse_manifest,
        get_runtime_ref,
        get_sdk_ref,
        get_baseapp_ref,
        get_sources_ref,
        get_pinned_refs,
    ):
        fn.cache_clear()


@lru_cache(maxsize=1)
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


def is_ref_in_remote(ref_type: str, ref_id: str, ref_arch: str, ref_branch: str) -> bool:
    ref = f"{ref_type}/{ref_id}/{ref_arch}/{ref_branch}"
    return (
        _run_flatpak(
            ["remote-info", "flathub", ref],
            capture_output=False,
            message=f"Failed to run remote-info for '{ref}'",
        )
        is not None
    )


def install_flatpak(ref: str, repo: str | None = None) -> bool:
    args = [
        "install",
        "--user",
        "--assumeyes",
        "--noninteractive",
        "--reinstall",
    ]

    if repo:
        repo_path = os.path.abspath(repo)
        args.append(repo_path)
    else:
        args.append("flathub")

    args.append(ref)
    return (
        _run_flatpak(
            args,
            message=f"Failed to install or reinstall '{ref}' from '{repo or 'flathub'}'",
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
        _invalidate_manifest_cache()
    ref = f"{flatpak_id}//stable"
    result = _run_flatpak(
        ["run", "--command=/usr/bin/cat", ref, "/app/manifest.json"],
        capture_output=True,
        message=f"Failed to extract manifest from '{ref}'",
    )
    if result is None:
        return False
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(result.stdout)
    _invalidate_manifest_cache()
    return True


@lru_cache(maxsize=1)
def parse_manifest(flatpak_id: str) -> dict[str, Any]:
    path = get_saved_manifest_path(flatpak_id)
    if path:
        with open(path, encoding="utf-8") as f:
            manifest: dict[str, Any] = json.load(f)
            manifest_id = manifest.get("id") or manifest.get("app-id")
            if manifest_id == flatpak_id:
                return manifest
            logging.error(
                "The 'id' in manifest '%s' does not match the expected id '%s'",
                manifest_id,
                flatpak_id,
            )
    return {}


def collect_src_paths(flatpak_id: str) -> list[str]:
    def walk_modules(modules: list[dict[str, Any]]) -> list[str]:
        paths: list[str] = []
        for module in modules:
            for source in module.get("sources", []):
                if "path" in source and "/" not in source["path"].lstrip("./"):
                    paths.append(os.path.basename(source["path"]))
                if "paths" in source:
                    paths.extend(
                        os.path.basename(p) for p in source["paths"] if "/" not in p.lstrip("./")
                    )
            paths.extend(walk_modules(module.get("modules", [])))
        return paths

    manifest = parse_manifest(flatpak_id)
    return walk_modules(manifest.get("modules", []))


@lru_cache(maxsize=1)
def get_runtime_ref(flatpak_id: str) -> list[str]:
    manifest = parse_manifest(flatpak_id)
    if "runtime" in manifest and "runtime-version" in manifest:
        runtime = manifest["runtime"]
        if runtime in ALLOWED_RUNTIMES:
            return [f"{runtime}//{manifest['runtime-version']}"]
        logging.warning("Unknown runtime '%s'", runtime)
    logging.error("Missing 'runtime' or 'runtime-version' in manifest for '%s'", flatpak_id)
    return []


@lru_cache(maxsize=1)
def get_sdk_ref(flatpak_id: str) -> list[str]:
    manifest = parse_manifest(flatpak_id)
    if "sdk" in manifest and "runtime-version" in manifest:
        sdk = manifest["sdk"]
        if sdk in ALLOWED_RUNTIMES:
            return [f"{sdk}//{manifest['runtime-version']}"]
        logging.warning("Unknown sdk '%s'", sdk)
    logging.error("Missing 'sdk' or 'runtime-version' in manifest for '%s'", flatpak_id)
    return []


@lru_cache(maxsize=1)
def get_baseapp_ref(flatpak_id: str) -> list[str]:
    manifest = parse_manifest(flatpak_id)
    base = manifest.get("base")
    base_version = manifest.get("base-version")
    if base and base_version:
        return [f"{base}//{base_version}"]
    return []


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
        runtime_refs = get_runtime_ref(flatpak_id)
        if runtime_refs and "//" in runtime_refs[0]:
            runtime_id, runtime_branch = runtime_refs[0].split("//", 1)
            base_branch = get_base_runtime_version(runtime_id, runtime_branch)
            if base_branch:
                for s in sdk_exts:
                    refs.append(f"{s}//{base_branch}")
            else:
                logging.warning("No base branch found for runtime '%s'", runtime_refs[0])

    if isinstance(add_build_exts, dict):
        for ext_id, ext_info in add_build_exts.items():
            version = ext_info.get("version", "stable") if isinstance(ext_info, dict) else "stable"
            refs.append(f"{ext_id}//{version}")

    return refs


@lru_cache(maxsize=1)
def get_sources_ref(flatpak_id: str) -> list[str]:
    sources_ref: list[str] = []

    parts = flatpak_id.split(".")
    if parts:
        parts[-1] = parts[-1].replace("-", "_")
    sources_id = ".".join(parts) + ".Sources"
    sources_ref_parts = ("runtime", sources_id, "x86_64", "stable")
    sources_ref_str = "/".join(sources_ref_parts)

    if is_ref_in_remote(*sources_ref_parts):
        sources_ref = [sources_ref_str]
    else:
        logging.warning("Failed to find sources extension for '%s'", flatpak_id)

    return sources_ref


def get_build_deps_refs(flatpak_id: str) -> list[str]:
    runtime_ref = get_runtime_ref(flatpak_id)
    sdk_ref = get_sdk_ref(flatpak_id)

    if not (runtime_ref or sdk_ref):
        return []

    return list(
        {
            *runtime_ref,
            *sdk_ref,
            *get_build_extension_refs(flatpak_id),
            *get_baseapp_ref(flatpak_id),
        }
    )


@lru_cache(maxsize=1)
def get_pinned_refs(flatpak_id: str) -> dict[str, str]:
    manifest = parse_manifest(flatpak_id)
    refs: dict[str, str] = {}
    runtime_ref = get_runtime_ref(flatpak_id)
    sdk_ref = get_sdk_ref(flatpak_id)
    if runtime_ref and sdk_ref:
        refs[runtime_ref[0]] = manifest["runtime-commit"]
        refs[sdk_ref[0]] = manifest["sdk-commit"]
    baseapp = get_baseapp_ref(flatpak_id)
    if baseapp:
        base_commit = manifest["base-commit"]
        refs[baseapp[0]] = base_commit

    return refs


def install_build_deps_refs(flatpak_id: str) -> bool:
    build_deps_refs = get_build_deps_refs(flatpak_id)
    if not build_deps_refs:
        return False
    return all(install_flatpak(ref) for ref in build_deps_refs)


def update_refs_to_pinned_commit(flatpak_id: str) -> bool:
    success = True
    pinned_refs = get_pinned_refs(flatpak_id)
    if not pinned_refs:
        logging.error("No pinned refs found in manifest for '%s'", flatpak_id)
        return False

    for ref, commit in pinned_refs.items():
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
            message=f"Failed to pin '{ref}' to commit '{commit}'",
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


def fp_builder_filename_to_uri(name: str) -> str:
    if "_" not in name:
        return name
    proto, rest = name.split("_", 1)
    return proto + "://" + rest.replace("_", "/")


def process_git_bare_repos(bare_repo_path: str, checkout_dir: str, commit: str) -> str | None:
    if not (os.path.isdir(bare_repo_path) and commit):
        return None

    bare_repo_dir = os.path.dirname(bare_repo_path)
    checkout_folder_name = os.path.basename(bare_repo_path) + "_checkout"
    checkout_repo_path = os.path.join(checkout_dir, checkout_folder_name)

    if (
        _run_command(["git", "clone", bare_repo_path, checkout_repo_path], cwd=bare_repo_dir)
        is None
    ):
        return None

    if _run_command(["git", "checkout", "-f", commit], cwd=checkout_repo_path):
        return checkout_repo_path
    return None


def find_git_src_commit(manifest_file: str, git_url: str) -> str | None:
    if not os.path.isfile(manifest_file):
        logging.error("Manifest file does not exist: %s", manifest_file)
        return None

    try:
        with open(manifest_file, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as err:
        logging.error("Failed to open manifest: %s", err)
        return None

    if "modules" in data:
        for module in data["modules"]:
            if "sources" in module:
                for source in module["sources"]:
                    if source.get("type") == "git" and source.get("url") == git_url:
                        if "commit" in source:
                            return str(source["commit"])
                        logging.error("Git source found but no commit: %s", git_url)
                        return None

    logging.warning("Git url not found in manifest: %s", git_url)
    return None


def replace_git_sources(manifest_file: str, replace_dict: dict[str, str]) -> bool:
    if not os.path.isfile(manifest_file):
        logging.error("Manifest file does not exist: %s", manifest_file)
        return False

    for _, local_path in replace_dict.items():
        if not os.path.isdir(local_path):
            logging.error("Target git checkout does not exist: %s", local_path)
            return False
        if local_path.startswith("file://"):
            logging.error("Target path must not be a file uri: %s", local_path)
            return False

    backup_file = f"{manifest_file}.backup"
    try:
        shutil.copy2(manifest_file, backup_file)
        logging.info("Created backup: %s", backup_file)
    except OSError as err:
        logging.error("Failed to create backup of manifest file: %s", err)
        return False

    try:
        with open(manifest_file, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as err:
        logging.error("Failed to open manifest: %s", err)
        return False

    file_url_map: dict[str, str] = {}
    for url, local_path in replace_dict.items():
        file_url_map[url] = f"file://{os.path.abspath(local_path)}"

    if "modules" in data:
        for module in data["modules"]:
            if "sources" in module:
                for source in module["sources"]:
                    if source.get("type") == "git" and "url" in source:
                        old_url = source["url"]
                        if old_url in file_url_map:
                            source["url"] = file_url_map[old_url]

    try:
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except OSError as err:
        logging.error("Failed to write manifest: %s", err)
        return False

    return True


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

    sources_dir = None
    sources_manifest_dir = None
    sources_downloads_dir = None
    sources_git_dir = None
    sources_id = [ref.split("/")[1] for ref in get_sources_ref(flatpak_id)]
    if sources_id:
        sources_dir = os.path.join(
            FLATPAK_ROOT_DIR, "runtime", sources_id[0], "x86_64", "stable", "active", "files"
        )
        sources_manifest_dir = os.path.join(sources_dir, "manifest")
        sources_downloads_dir = os.path.join(sources_dir, "downloads")
        sources_git_dir = os.path.join(sources_dir, "git")

    state_dir_downloads = os.path.join(state_dir, "downloads")
    os.makedirs(state_dir_downloads, exist_ok=True)

    state_dir_git = os.path.join(state_dir, "git")
    os.makedirs(state_dir_git, exist_ok=True)

    replace_dict: dict[str, str] = {}
    if sources_git_dir and os.path.isdir(sources_git_dir):
        for item in os.listdir(sources_git_dir):
            src = os.path.join(sources_git_dir, item)
            dest = os.path.join(state_dir_git, item)
            uri = fp_builder_filename_to_uri(os.path.basename(dest))
            checkout_commit = find_git_src_commit(manifest_path, uri)
            if checkout_commit and os.path.isdir(src):
                shutil.copytree(src, dest, dirs_exist_ok=True)
                checkout_path = process_git_bare_repos(dest, manifest_dir, checkout_commit)
                if checkout_path:
                    replace_dict[uri] = checkout_path

        if replace_dict:
            replace_git_sources(manifest_path, replace_dict)

    if sources_manifest_dir and os.path.isdir(sources_manifest_dir):
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

    if sources_downloads_dir and os.path.isdir(sources_downloads_dir):
        for item in os.listdir(sources_downloads_dir):
            src = os.path.join(sources_downloads_dir, item)
            dest = os.path.join(state_dir_downloads, item)
            if os.path.isdir(src):
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)

    src_paths = collect_src_paths(flatpak_id)

    for path in src_paths:
        target = os.path.join(manifest_dir, path)
        if os.path.exists(target):
            continue

        basename = os.path.basename(path)

        for root, _, files in os.walk(manifest_dir):
            if basename in files:
                source = os.path.join(root, basename)
                shutil.copy2(source, target)
                break

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
    if "FLATPAK_USER_DIR" not in env:
        env["FLATPAK_USER_DIR"] = FLATPAK_ROOT_DIR

    if is_inside_container():
        env["FLATPAK_SYSTEM_HELPER_ON_SESSION"] = "foo"

    configure_git_file_protocol(unset=False)

    result = _run_command(
        args,
        cwd=manifest_dir,
        message=f"Failed to run flatpak-builder on '{manifest_file}'",
        env=env,
        capture_output=True,
    )

    configure_git_file_protocol(unset=True)

    return result is not None


def get_built_app_branch(manifest_path: str) -> str | None:
    repo_path = os.path.join(os.path.dirname(manifest_path), "repo")
    result = _run_command(
        ["ostree", f"--repo={repo_path}", "refs"],
        capture_output=True,
        message=f"Failed to list refs in '{repo_path}'",
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


def backup_and_remove_nondeterminism(
    install_dir: str, rebuilt_dir: str
) -> tuple[dict[str, str], str] | None:
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

    if not os.path.isfile(install_manifest):
        logging.error("Failed to find manifest from install directory %s", install_dir)
        return None
    if not os.path.isfile(rebuilt_manifest):
        logging.error("Failed to find manifest from rebuilt directory %s", rebuilt_dir)
        return None

    shutil.move(install_manifest, backup_install_manifest)
    shutil.move(rebuilt_manifest, backup_rebuilt_manifest)

    if os.path.isdir(install_app_info_dir):
        os.makedirs(backup_install_app_info_dir, exist_ok=True)
        shutil.move(install_app_info_dir, os.path.join(backup_install_app_info_dir, "app-info"))

    if os.path.isdir(rebuilt_app_info_dir):
        os.makedirs(backup_rebuilt_app_info_dir, exist_ok=True)
        shutil.move(rebuilt_app_info_dir, os.path.join(backup_rebuilt_app_info_dir, "app-info"))

    return {
        "install_manifest": install_manifest,
        "rebuilt_manifest": rebuilt_manifest,
        "backup_install_manifest": backup_install_manifest,
        "backup_rebuilt_manifest": backup_rebuilt_manifest,
        "install_app_info_dir": install_app_info_dir,
        "rebuilt_app_info_dir": rebuilt_app_info_dir,
        "backup_install_app_info_dir": backup_install_app_info_dir,
        "backup_rebuilt_app_info_dir": backup_rebuilt_app_info_dir,
    }, backup_dir


def restore_backups(
    flatpak_id: str,
    handled_build_deps: bool,
    backup_info: dict[str, str] | None,
    backup_dir: str | None,
) -> None:
    if handled_build_deps:
        for ref in get_pinned_refs(flatpak_id):
            flatpak_mask(ref, remove=True)

    if backup_info:
        app_info_subdir = "app-info"

        if os.path.exists(backup_info["backup_install_manifest"]):
            shutil.move(backup_info["backup_install_manifest"], backup_info["install_manifest"])
        if os.path.exists(backup_info["backup_rebuilt_manifest"]):
            shutil.move(backup_info["backup_rebuilt_manifest"], backup_info["rebuilt_manifest"])

        if os.path.exists(
            os.path.join(backup_info["backup_install_app_info_dir"], app_info_subdir)
        ):
            shutil.move(
                os.path.join(backup_info["backup_install_app_info_dir"], app_info_subdir),
                backup_info["install_app_info_dir"],
            )
        if os.path.exists(
            os.path.join(backup_info["backup_rebuilt_app_info_dir"], app_info_subdir)
        ):
            shutil.move(
                os.path.join(backup_info["backup_rebuilt_app_info_dir"], app_info_subdir),
                backup_info["rebuilt_app_info_dir"],
            )

    if backup_dir and os.path.isdir(backup_dir):
        shutil.rmtree(backup_dir, ignore_errors=True)


def zip_directory(dir_path: str) -> str | None:
    if not os.path.isdir(dir_path):
        return None
    zip_path = os.path.join(tempfile.gettempdir(), f"{os.path.basename(dir_path)}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, _dirs, files in os.walk(dir_path):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, dir_path)
                zipf.write(file_path, arcname)
    logging.info("Created zip file: %s", zip_path)
    return zip_path


def run_diffoscope(
    folder_a: str, folder_b: str, output_dir: str, upload_results: bool = False
) -> tuple[str | None, int]:
    ret = (None, 1)
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
        return ret
    if result.returncode == 0:
        logging.info("Result is reproducible")
        if os.path.isdir(output_dir):
            shutil.rmtree(output_dir, ignore_errors=True)
        return (None, 0)
    if result.returncode == 1:
        logging.error("Result is not reproducible")
        if upload_results and os.path.exists(output_dir):
            zip_path = zip_directory(output_dir)
            if zip_path:
                url = upload_to_s3(zip_path)
                if url:
                    logging.info("Results uploaded to: %s", url)
                    return (url, 42)
                logging.error("Failed to upload results")
            else:
                logging.error("Failed to create zip file")
        return (None, 42)
    logging.error("Diffoscope failed with code %d", result.returncode)
    return ret


def run_repro_check(
    flatpak_id: str, output_dir: str, build_src: str | None, upload_results: bool = False
) -> tuple[str | None, int]:
    backup_info = None
    backup_dir = None
    handled_build_deps = False
    appref = f"app/{flatpak_id}//stable"

    ret = (None, 1)

    try:
        if not validate_env():
            return ret
        if not setup_flathub():
            return ret
        if build_src and not install_flatpak(appref, build_src):
            return ret
        if not (build_src or install_flatpak(appref)):
            return ret
        src_ref = get_sources_ref(flatpak_id)
        if not src_ref:
            return ret
        if build_src and not install_flatpak(src_ref[0], build_src):
            return ret
        if not (build_src or install_flatpak(src_ref[0])):
            return ret
        if not save_manifest(flatpak_id):
            return ret
        manifest_path = get_saved_manifest_path(flatpak_id)
        if manifest_path is None:
            logging.error("Flatpak manifest not found")
            return ret

        if not handle_build_deps(flatpak_id):
            return ret
        handled_build_deps = True

        if not build_flatpak(manifest_path):
            return ret

        arch = get_flatpak_arch()
        if not arch:
            return ret

        built_branch = get_built_app_branch(manifest_path)
        if not built_branch:
            return ret

        install_dir = os.path.join(
            FLATPAK_ROOT_DIR, "app", flatpak_id, arch, "stable", "active", "files"
        )
        rebuilt_dir = os.path.join(
            FLATPAK_ROOT_DIR, "app", flatpak_id, arch, built_branch, "active", "files"
        )

        result = backup_and_remove_nondeterminism(install_dir, rebuilt_dir)
        if result is None:
            return ret
        backup_info, backup_dir = result

        return run_diffoscope(install_dir, rebuilt_dir, output_dir, upload_results)

    finally:
        restore_backups(flatpak_id, handled_build_deps, backup_info, backup_dir)


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
            logging.error("'%s' is required but was not found in PATH", tool)
        return False

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Flathub reproducibility checker", add_help=False)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON output. Always exits with exits with 0 unless fatal errors",
    )
    early_args, _ = parser.parse_known_args()
    json_mode = early_args.json

    setup_logging(json_mode)

    if is_root():
        msg = "Running the checker as root is unsupported"
        if json_mode:
            print_json_output("", 1, msg)
        else:
            logging.error("Running the checker as root is unsupported")
            return 1

    parser = argparse.ArgumentParser(
        description="Flathub reproducibility checker",
        epilog="""
    This tool only works on "app" Flatpak refs for now and any other ref
    will return an exit code of 1.

    This uses a custom Flatpak root directory. Set the FLATPAK_USER_DIR
    environment variable to override that.

    STATUS CODES:
      0   Success
      42  Unreproducible
      1   Failure

    JSON OUTPUT FORMAT:

    Always exits with 0 unless fatal errors. All values are
    strings. "appid", "message", "log_url", "result_url" can
    be empty strings.

      {
        "timestamp": "2025-07-22T04:00:17.099066+00:00"  // ISO Format
        "appid": "com.example.baz",                      // App ID
        "status_code": "42",                             // Status Code
        "log_url": "https://example.com",                // Log URL
        "result_url": "https://example.com",             // Link to uploaded diffoscope result
        "message": "Unreproducible"                      // Message
      }
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
        usage=argparse.SUPPRESS,
    )
    parser.add_argument("-h", "--help", action="help", help="Show this help message and exit")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show the version and exit",
    )
    parser.add_argument("--appid", metavar="", help="App ID on Flathub")
    parser.add_argument(
        "--json",
        action="store_true",
        help="JSON output. Always exits with 0 unless fatal errors",
    )
    parser.add_argument(
        "--ref-build-path",
        metavar="",
        help="Install the reference build from this OSTree repo path instead of Flathub",
    )
    parser.add_argument(
        "--output-dir",
        metavar="",
        help="Output dir for diffoscope report (default: ./diffoscope_result-$FLATPAK_ID)",
    )
    parser.add_argument(
        "--upload-result",
        action="store_true",
        help=(
            "Upload results to AWS S3. "
            "Requires boto3. "
            "Use AWS_S3_BUCKET_NAME to specify bucket name"
        ),
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Cleanup all state",
    )
    args = parser.parse_args()

    if args.cleanup:
        msg = f"Cleaning up: {REPRO_DATADIR}"
        if os.path.isdir(REPRO_DATADIR):
            shutil.rmtree(REPRO_DATADIR)
            if json_mode:
                print_json_output("", 0, msg)
            else:
                logging.info("Cleaning up: %s", REPRO_DATADIR)
        else:
            msg = "Nothing to clean"
            if json_mode:
                print_json_output("", 0, msg)
            else:
                logging.info("Nothing to clean")
        return 0

    if not args.appid:
        msg = "--appid is required"
        if json_mode:
            print_json_output("", 1, msg)
        else:
            logging.error("--appid is required")
        return 1

    UNSUPPORTED_FLATPAK_IDS = (
        "org.mozilla.firefox",
        "org.mozilla.Thunderbird",
        "net.pcsx2.PCSX2",
        "org.duckstation.DuckStation",
        "net.wz2100.wz2100",
        "com.obsproject.Studio",
    )

    flatpak_id = args.appid

    if flatpak_id in UNSUPPORTED_FLATPAK_IDS:
        msg = f"Running the checker against '{flatpak_id}' is unsupported right now"
        if json_mode:
            print_json_output(flatpak_id, 1, msg)
        else:
            logging.error(msg)
        return 1

    ref_build_source = None
    if args.ref_build_path:
        ref_build_path = os.path.abspath(args.ref_build_path)
        msg = f"The path does not exist: {ref_build_path}"
        if os.path.isdir(ref_build_path):
            ref_build_source = ref_build_path
        if not ref_build_source:
            if json_mode:
                print_json_output(flatpak_id, 1, msg)
            else:
                logging.error(msg)
            return 1

    os.makedirs(REPRO_DATADIR, exist_ok=True)
    if not json_mode:
        logging.info("Created data directory: %s", REPRO_DATADIR)
    os.makedirs(FLATPAK_BUILDER_STATE_DIR, exist_ok=True)
    if not json_mode:
        logging.info("Created flatpak-builder root state directory: %s", FLATPAK_BUILDER_STATE_DIR)

    if args.output_dir:
        output_dir = os.path.abspath(args.output_dir)
    else:
        output_dir = os.path.abspath(f"./diffoscope_result-{flatpak_id}")

    lockfile_path = os.path.join(REPRO_DATADIR, "flathub_repro_checker.lock")
    with Lock(lockfile_path):
        upload_url, result = run_repro_check(
            flatpak_id, output_dir, ref_build_source, args.upload_result
        )
        if json_mode:
            if result == 0:
                msg = "Success"
            elif result == 42:
                msg = "Unreproducible"
            else:
                msg = "Failure"
            print_json_output(flatpak_id, result, msg, upload_url)
        return result


if __name__ == "__main__":
    raise SystemExit(main())
