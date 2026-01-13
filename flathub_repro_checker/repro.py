import logging
import os
import shutil

from .config import Config, ExitCode, ReproResult
from .flatpak import FlatpakSession
from .subp_utils import run_command
from .utils import upload_to_s3, zip_directory


class ReproChecker:
    def __init__(
        self,
        flatpak_id: str,
        output_dir: str,
        build_src: str | None,
        upload_results: bool = False,
    ):
        self.flatpak_id = flatpak_id
        self.output_dir = output_dir
        self.build_src = build_src
        self.upload_results = upload_results
        self.session = FlatpakSession(flatpak_id)

    def backup_and_remove_nondeterminism(
        self,
        install_dir: str,
        rebuilt_dir: str,
    ) -> tuple[dict[str, str], str] | None:
        install_manifest = os.path.join(install_dir, "manifest.json")
        rebuilt_manifest = os.path.join(rebuilt_dir, "manifest.json")
        install_app_info_dir = os.path.join(install_dir, "share", "app-info")
        rebuilt_app_info_dir = os.path.join(rebuilt_dir, "share", "app-info")

        backup_dir = os.path.join(Config.repro_datadir(), "backups")
        os.makedirs(backup_dir, exist_ok=True)

        backup_install_manifest = os.path.join(backup_dir, "install_manifest.json")
        backup_rebuilt_manifest = os.path.join(backup_dir, "rebuilt_manifest.json")
        backup_install_app_info_dir = os.path.join(backup_dir, "install_app_info")
        backup_rebuilt_app_info_dir = os.path.join(backup_dir, "rebuilt_app_info")

        if not os.path.isfile(install_manifest):
            logging.error(
                "Failed to find manifest from install directory %s",
                install_dir,
            )
            return None

        if not os.path.isfile(rebuilt_manifest):
            logging.error(
                "Failed to find manifest from rebuilt directory %s",
                rebuilt_dir,
            )
            return None

        shutil.move(install_manifest, backup_install_manifest)
        shutil.move(rebuilt_manifest, backup_rebuilt_manifest)

        if os.path.isdir(install_app_info_dir):
            os.makedirs(backup_install_app_info_dir, exist_ok=True)
            shutil.move(
                install_app_info_dir,
                os.path.join(backup_install_app_info_dir, "app-info"),
            )

        if os.path.isdir(rebuilt_app_info_dir):
            os.makedirs(backup_rebuilt_app_info_dir, exist_ok=True)
            shutil.move(
                rebuilt_app_info_dir,
                os.path.join(backup_rebuilt_app_info_dir, "app-info"),
            )

        return (
            {
                "install_manifest": install_manifest,
                "rebuilt_manifest": rebuilt_manifest,
                "backup_install_manifest": backup_install_manifest,
                "backup_rebuilt_manifest": backup_rebuilt_manifest,
                "install_app_info_dir": install_app_info_dir,
                "rebuilt_app_info_dir": rebuilt_app_info_dir,
                "backup_install_app_info_dir": backup_install_app_info_dir,
                "backup_rebuilt_app_info_dir": backup_rebuilt_app_info_dir,
            },
            backup_dir,
        )

    def restore_backups(
        self,
        handled_build_deps: bool,
        backup_info: dict[str, str] | None,
        backup_dir: str | None,
    ) -> None:
        if handled_build_deps:
            for ref in self.session.manifest.get_pinned_refs():
                self.session.flatpak_mask(ref, remove=True)

        if backup_info:
            app_info_subdir = "app-info"

            if os.path.exists(backup_info["backup_install_manifest"]):
                shutil.move(
                    backup_info["backup_install_manifest"],
                    backup_info["install_manifest"],
                )

            if os.path.exists(backup_info["backup_rebuilt_manifest"]):
                shutil.move(
                    backup_info["backup_rebuilt_manifest"],
                    backup_info["rebuilt_manifest"],
                )

            if os.path.exists(
                os.path.join(
                    backup_info["backup_install_app_info_dir"],
                    app_info_subdir,
                )
            ):
                shutil.move(
                    os.path.join(
                        backup_info["backup_install_app_info_dir"],
                        app_info_subdir,
                    ),
                    backup_info["install_app_info_dir"],
                )

            if os.path.exists(
                os.path.join(
                    backup_info["backup_rebuilt_app_info_dir"],
                    app_info_subdir,
                )
            ):
                shutil.move(
                    os.path.join(
                        backup_info["backup_rebuilt_app_info_dir"],
                        app_info_subdir,
                    ),
                    backup_info["rebuilt_app_info_dir"],
                )

        if backup_dir and os.path.isdir(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)

    def run_diffoscope(
        self,
        folder_a: str,
        folder_b: str,
    ) -> ReproResult:
        ret = ReproResult(None, ExitCode.FAILURE)

        if os.path.isdir(self.output_dir):
            shutil.rmtree(self.output_dir, ignore_errors=True)

        cmd = [
            "diffoscope",
            f"--html-dir={self.output_dir}",
            "--exclude-directory-metadata=recursive",
            folder_a,
            folder_b,
        ]

        result = run_command(
            cmd,
            check=False,
            capture_output=True,
            message="Diffoscope failed",
            warn=True,
        )

        if result is None:
            return ret

        if result.returncode == 0:
            logging.info("Result is reproducible")
            if os.path.isdir(self.output_dir):
                shutil.rmtree(self.output_dir, ignore_errors=True)
            return ReproResult(None, ExitCode.SUCCESS)

        if result.returncode == 1:
            logging.error("Result is not reproducible")
            if self.upload_results and os.path.exists(self.output_dir):
                zip_path = zip_directory(self.output_dir)
                if zip_path:
                    url = upload_to_s3(zip_path)
                    if url:
                        logging.info("Results uploaded to: %s", url)
                        return ReproResult(
                            url,
                            ExitCode.UNREPRODUCIBLE,
                        )
                    logging.error("Failed to upload results")
                else:
                    logging.error("Failed to create zip file")
            return ReproResult(None, ExitCode.UNREPRODUCIBLE)

        logging.error(
            "Diffoscope failed with code %d",
            result.returncode,
        )
        return ret

    def run(self) -> ReproResult:
        backup_info = None
        backup_dir = None
        handled_build_deps = False

        appref = Config.get_supported_repro_checker_ref(self.flatpak_id)

        ret = ReproResult(None, ExitCode.FAILURE)

        try:
            if os.path.exists(Config.manifest_save_dir(self.flatpak_id)):
                shutil.rmtree(Config.manifest_save_dir(self.flatpak_id))

            if self.build_src and not self.session.install_flatpak(
                appref,
                self.build_src,
            ):
                return ret

            if not (self.build_src or self.session.install_flatpak(appref)):
                return ret

            src_ref = self.session.manifest.get_sources_ref()
            if not src_ref:
                return ret

            if self.build_src and not self.session.install_flatpak(
                src_ref[0],
                self.build_src,
            ):
                return ret

            if not (self.build_src or self.session.install_flatpak(src_ref[0])):
                return ret

            if not self.session.manifest.save():
                return ret

            manifest_path = self.session.manifest.get_saved_manifest_path()
            if manifest_path is None:
                logging.error("Flatpak manifest not found")
                return ret

            if not self.session.handle_build_deps():
                return ret
            handled_build_deps = True

            if not self.session.build_flatpak(manifest_path):
                return ret

            built_branch = self.session.get_built_app_branch(manifest_path)
            if not built_branch:
                return ret

            install_dir = os.path.join(
                Config.flatpak_root_dir(),
                Config.APP_REF_KIND,
                self.flatpak_id,
                Config.SUPPORTED_REF_ARCH,
                Config.SUPPORTED_REF_BRANCH,
                "active",
                "files",
            )

            rebuilt_dir = os.path.join(
                Config.flatpak_root_dir(),
                Config.APP_REF_KIND,
                self.flatpak_id,
                Config.SUPPORTED_REF_ARCH,
                built_branch,
                "active",
                "files",
            )

            result = self.backup_and_remove_nondeterminism(
                install_dir,
                rebuilt_dir,
            )
            if result is None:
                return ret

            backup_info, backup_dir = result

            return self.run_diffoscope(
                install_dir,
                rebuilt_dir,
            )

        finally:
            self.restore_backups(
                handled_build_deps,
                backup_info,
                backup_dir,
            )
