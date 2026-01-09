import json
import logging
import os
import re
import shutil
from functools import lru_cache
from typing import Any

from .config import (
    ALLOWED_RUNTIMES,
    FLATPAK_BUILDER_STATE_DIR,
    FLATPAK_ROOT_DIR,
    REPRO_DATADIR,
    RUNTIME_REF_KIND,
    SUPPORTED_REF_ARCH,
    SUPPORTED_REF_BRANCH,
    is_inside_container,
)
from .process import (
    _run_command,
    _run_flatpak,
)
from .utils import (
    configure_git_file_protocol,
    find_git_src_commit,
    fp_builder_filename_to_uri,
    process_git_bare_repos,
    replace_git_sources,
)


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
    ref = f"{flatpak_id}//{SUPPORTED_REF_BRANCH}"
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
            version = (
                ext_info.get("version", SUPPORTED_REF_BRANCH)
                if isinstance(ext_info, dict)
                else SUPPORTED_REF_BRANCH
            )
            refs.append(f"{ext_id}//{version}")

    return refs


@lru_cache(maxsize=1)
def get_sources_ref(flatpak_id: str) -> list[str]:
    sources_ref: list[str] = []

    parts = flatpak_id.split(".")
    if parts:
        parts[-1] = parts[-1].replace("-", "_")
    sources_id = ".".join(parts) + ".Sources"
    sources_ref_parts = (RUNTIME_REF_KIND, sources_id, SUPPORTED_REF_ARCH, SUPPORTED_REF_BRANCH)
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
            FLATPAK_ROOT_DIR,
            RUNTIME_REF_KIND,
            sources_id[0],
            SUPPORTED_REF_ARCH,
            SUPPORTED_REF_BRANCH,
            "active",
            "files",
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
