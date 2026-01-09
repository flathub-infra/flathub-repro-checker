import logging
import os
import shutil

from .config import (
    APP_REF_KIND,
    FLATPAK_ROOT_DIR,
    REPRO_DATADIR,
    SUPPORTED_REF_BRANCH,
    ExitCode,
    ReproResult,
)
from .flatpak import (
    build_flatpak,
    flatpak_mask,
    get_built_app_branch,
    get_flatpak_arch,
    get_pinned_refs,
    get_saved_manifest_path,
    get_sources_ref,
    handle_build_deps,
    install_flatpak,
    save_manifest,
)
from .process import _run_command
from .utils import upload_to_s3, zip_directory


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


def run_diffoscope(
    folder_a: str, folder_b: str, output_dir: str, upload_results: bool = False
) -> ReproResult:
    ret = ReproResult(None, ExitCode.FAILURE)

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
        return ReproResult(None, ExitCode.SUCCESS)
    if result.returncode == 1:
        logging.error("Result is not reproducible")
        if upload_results and os.path.exists(output_dir):
            zip_path = zip_directory(output_dir)
            if zip_path:
                url = upload_to_s3(zip_path)
                if url:
                    logging.info("Results uploaded to: %s", url)
                    return ReproResult(url, ExitCode.UNREPRODUCIBLE)
                logging.error("Failed to upload results")
            else:
                logging.error("Failed to create zip file")
        return ReproResult(None, ExitCode.UNREPRODUCIBLE)
    logging.error("Diffoscope failed with code %d", result.returncode)
    return ret


def run_repro_check(
    flatpak_id: str, output_dir: str, build_src: str | None, upload_results: bool = False
) -> ReproResult:
    backup_info = None
    backup_dir = None
    handled_build_deps = False
    appref = f"{APP_REF_KIND}/{flatpak_id}//{SUPPORTED_REF_BRANCH}"

    ret = ReproResult(None, ExitCode.FAILURE)

    try:
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
            FLATPAK_ROOT_DIR,
            APP_REF_KIND,
            flatpak_id,
            arch,
            SUPPORTED_REF_BRANCH,
            "active",
            "files",
        )
        rebuilt_dir = os.path.join(
            FLATPAK_ROOT_DIR, APP_REF_KIND, flatpak_id, arch, built_branch, "active", "files"
        )

        result = backup_and_remove_nondeterminism(install_dir, rebuilt_dir)
        if result is None:
            return ret
        backup_info, backup_dir = result

        return run_diffoscope(install_dir, rebuilt_dir, output_dir, upload_results)

    finally:
        restore_backups(flatpak_id, handled_build_deps, backup_info, backup_dir)
