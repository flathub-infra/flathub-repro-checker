import json
import logging
import os
import shutil

from .config import Config
from .manifest import Manifest
from .subp_utils import (
    run_command,
    run_flatpak,
)
from .utils import (
    configure_git_file_protocol,
    fp_builder_filename_to_uri,
    process_git_bare_repos,
)


def find_git_src_commit(
    manifest_file: str,
    git_url: str,
) -> str | None:
    if not os.path.isfile(manifest_file):
        logging.error("Manifest file does not exist: %s", manifest_file)
        return None

    try:
        with open(manifest_file, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as err:
        logging.error("Failed to open manifest: %s", err)
        return None

    for module in data.get("modules", []):
        for source in module.get("sources", []):
            if source.get("type") == "git" and source.get("url") == git_url:
                if "commit" in source:
                    return str(source["commit"])
                logging.error("Git source found but no commit: %s", git_url)
                return None

    logging.warning("Git url not found in manifest: %s", git_url)
    return None


def replace_git_sources(
    manifest_file: str,
    replace_dict: dict[str, str],
) -> bool:
    if not os.path.isfile(manifest_file):
        logging.error("Manifest file does not exist: %s", manifest_file)
        return False

    for local_path in replace_dict.values():
        if not os.path.isdir(local_path):
            logging.error("Target git checkout does not exist: %s", local_path)
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

    file_url_map = {url: f"file://{os.path.abspath(path)}" for url, path in replace_dict.items()}

    for module in data.get("modules", []):
        for source in module.get("sources", []):
            if source.get("type") == "git" and source.get("url") in file_url_map:
                source["url"] = file_url_map[source["url"]]

    try:
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except OSError as err:
        logging.error("Failed to write manifest: %s", err)
        return False

    return True


class FlatpakSession:
    def __init__(self, flatpak_id: str):
        self.flatpak_id = flatpak_id
        self.manifest = Manifest(flatpak_id)

    def get_flatpak_arch(self) -> str | None:
        ret = run_flatpak(
            ["--default-arch"],
            capture_output=True,
            message="Failed to get Flatpak arch",
        )
        return ret.stdout.strip() if ret else None

    def setup_flathub(self) -> bool:
        remotes = {"flathub": "https://dl.flathub.org/repo/flathub.flatpakrepo"}
        return all(
            run_flatpak(
                ["remote-add", "--user", "--if-not-exists", name, url],
                message=f"Failed to add remote '{name}'",
            )
            for name, url in remotes.items()
        )

    def is_ref_in_remote(
        self,
        ref: str,
    ) -> bool:
        return (
            run_flatpak(
                ["remote-info", "flathub", ref],
                capture_output=False,
                message=f"Failed to run remote-info for '{ref}'",
            )
            is not None
        )

    def install_flatpak(self, ref: str, repo: str | None = None) -> bool:
        args = [
            "install",
            "--user",
            "--assumeyes",
            "--noninteractive",
            "--reinstall",
        ]

        if repo:
            args.append(os.path.abspath(repo))
        else:
            args.append("flathub")

        args.append(ref)

        return (
            run_flatpak(
                args,
                message=f"Failed to install or reinstall '{ref}' from '{repo or 'flathub'}'",
            )
            is not None
        )

    def flatpak_mask(self, ref: str, remove: bool = False) -> bool:
        args = ["mask", "--user"]
        if remove:
            args.append("--remove")
        args.append(ref)

        return (
            run_flatpak(
                args,
                message=f"Failed to {'unmask' if remove else 'mask'} '{ref}'",
            )
            is not None
        )

    def get_build_deps_refs(self) -> list[str]:
        runtime_ref = self.manifest.get_runtime_ref()
        sdk_ref = self.manifest.get_sdk_ref()

        if not (runtime_ref or sdk_ref):
            return []

        return list(
            {
                *runtime_ref,
                *sdk_ref,
                *self.manifest.get_build_extension_refs(),
                *self.manifest.get_baseapp_ref(),
            }
        )

    def install_build_deps_refs(self) -> bool:
        build_deps_refs = self.get_build_deps_refs()
        if not build_deps_refs:
            return False
        return all(self.install_flatpak(ref) for ref in build_deps_refs)

    def update_refs_to_pinned_commit(self) -> bool:
        success = True
        pinned_refs = self.manifest.get_pinned_refs()

        if not pinned_refs:
            logging.error(
                "No pinned refs found in manifest for '%s'",
                self.flatpak_id,
            )
            return False

        for ref, commit in pinned_refs.items():
            result = run_flatpak(
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

    def handle_build_deps(self) -> bool:
        if not self.install_build_deps_refs():
            return False
        if not self.update_refs_to_pinned_commit():
            return False
        return all(self.flatpak_mask(ref) for ref in self.manifest.get_pinned_refs())

    def create_flatpak_builder_state_dir(self) -> str | None:
        path = os.path.join(
            Config.flatpak_builder_state_dir(),
            f"flatpak_builder_state-{self.flatpak_id}",
        )
        os.makedirs(path, exist_ok=True)
        return path if os.path.isdir(path) else None

    def build_flatpak(self, manifest_path: str) -> bool:
        manifest_dir = os.path.dirname(manifest_path)
        manifest_file = os.path.basename(manifest_path)
        state_dir = self.create_flatpak_builder_state_dir()

        if not state_dir:
            return False

        sources_id = [ref.split("/")[1] for ref in self.manifest.get_sources_ref()]

        sources_dir = None
        if sources_id:
            sources_dir = os.path.join(
                Config.flatpak_root_dir(),
                Config.RUNTIME_REF_KIND,
                sources_id[0],
                Config.SUPPORTED_REF_ARCH,
                Config.SUPPORTED_REF_BRANCH,
                "active",
                "files",
            )

        sources_manifest_dir = os.path.join(sources_dir, "manifest") if sources_dir else None
        sources_downloads_dir = os.path.join(sources_dir, "downloads") if sources_dir else None
        sources_git_dir = os.path.join(sources_dir, "git") if sources_dir else None

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
                checkout_commit = find_git_src_commit(
                    manifest_path,
                    uri,
                )

                if checkout_commit and os.path.isdir(src):
                    shutil.copytree(src, dest, dirs_exist_ok=True)
                    checkout_path = process_git_bare_repos(
                        dest,
                        manifest_dir,
                        checkout_commit,
                    )
                    if checkout_path:
                        replace_dict[uri] = checkout_path

            if replace_dict:
                replace_git_sources(
                    manifest_path,
                    replace_dict,
                )

        if sources_manifest_dir and os.path.isdir(sources_manifest_dir):
            for item in os.listdir(sources_manifest_dir):
                src = os.path.join(sources_manifest_dir, item)
                dest = os.path.join(manifest_dir, item)

                if os.path.isfile(src) and item.endswith(
                    (
                        f"{self.flatpak_id}.json",
                        f"{self.flatpak_id}.yml",
                        f"{self.flatpak_id}.yaml",
                    )
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

        for path in self.manifest.collect_src_paths():
            target = os.path.join(manifest_dir, path)
            if os.path.exists(target):
                continue

            basename = os.path.basename(path)
            for root, _, files in os.walk(manifest_dir):
                if basename in files:
                    shutil.copy2(
                        os.path.join(root, basename),
                        target,
                    )
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
            f"--arch={Config.SUPPORTED_REF_ARCH}",
            "builddir",
            manifest_file,
        ]

        env = os.environ.copy()
        if "FLATPAK_USER_DIR" not in env:
            env["FLATPAK_USER_DIR"] = Config.flatpak_root_dir()

        if Config.is_inside_container():
            env["FLATPAK_SYSTEM_HELPER_ON_SESSION"] = "foo"

        configure_git_file_protocol(unset=False)

        result = run_command(
            args,
            cwd=manifest_dir,
            message=f"Failed to run flatpak-builder on '{manifest_file}'",
            env=env,
            capture_output=True,
        )

        configure_git_file_protocol(unset=True)

        return result is not None

    def get_built_app_branch(self, manifest_path: str) -> str | None:
        repo_path = os.path.join(
            os.path.dirname(manifest_path),
            "repo",
        )

        result = run_command(
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
