import argparse
import logging
import os
import shutil

from . import __version__
from .config import (
    APP_REF_KIND,
    FLATPAK_BUILDER_STATE_DIR,
    REPRO_DATADIR,
    SUPPORTED_REF_ARCH,
    SUPPORTED_REF_BRANCH,
    UNSUPPORTED_FLATPAK_IDS,
    ExitCode,
    is_root,
)
from .flatpak import (
    is_ref_in_remote,
    setup_flathub,
)
from .lock import Lock
from .repro import run_repro_check
from .utils import (
    json_exit,
    report_and_exit,
    setup_logging,
    validate_env,
)


def parse_args() -> tuple[bool, argparse.Namespace]:
    early = argparse.ArgumentParser(add_help=False)
    early.add_argument("--json", action="store_true")
    early_args, _ = early.parse_known_args()

    parser = argparse.ArgumentParser(
        description="Flathub reproducibility checker",
        epilog="""
    This tool only works on "app" Flatpak refs on Flathub and any other
    ref will return an exit code of 2.

    This uses a custom Flatpak root directory. Set the FLATPAK_USER_DIR
    environment variable to override that.

    STATUS CODES:
      0   Success
      1   Failure
      2   Unhandled
      42  Unreproducible

    JSON OUTPUT FORMAT:

    Always exits with 0 unless fatal errors. All values are
    strings. "appid", "message", "log_url", "result_url" can
    be empty strings.

      {
        "timestamp": "2025-07-22T04:00:17.099066+00:00"
        "appid": "com.example.baz"
        "status_code": "42"
        "log_url": "https://example.com"
        "result_url": "https://example.com"
        "message": "Unreproducible"
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

    return early_args.json, parser.parse_args()


def main() -> int:
    json_mode, args = parse_args()
    setup_logging(json_mode)

    if is_root():
        return report_and_exit(
            json_mode,
            "",
            ExitCode.FAILURE,
            "Running the checker as root is unsupported",
        )

    if args.cleanup:
        if os.path.isdir(REPRO_DATADIR):
            shutil.rmtree(REPRO_DATADIR)
            return report_and_exit(
                json_mode,
                "",
                ExitCode.SUCCESS,
                f"Cleaning up: {REPRO_DATADIR}",
                level="info",
            )
        return report_and_exit(
            json_mode,
            "",
            ExitCode.SUCCESS,
            "Nothing to clean",
            level="info",
        )

    if not args.appid:
        return report_and_exit(
            json_mode,
            "",
            ExitCode.FAILURE,
            "--appid is required",
        )

    flatpak_id = args.appid

    if not validate_env():
        return report_and_exit(
            json_mode,
            "",
            ExitCode.FAILURE,
            "Failed to validate the environment",
        )

    unhandled_msg = f"Running the checker against '{flatpak_id}' is unsupported right now"

    if flatpak_id in UNSUPPORTED_FLATPAK_IDS:
        return report_and_exit(
            json_mode,
            flatpak_id,
            ExitCode.UNHANDLED,
            unhandled_msg,
        )

    if not setup_flathub():
        return report_and_exit(
            json_mode,
            flatpak_id,
            ExitCode.FAILURE,
            "Failed to set up Flathub remote",
        )

    if not is_ref_in_remote(
        APP_REF_KIND,
        flatpak_id,
        SUPPORTED_REF_ARCH,
        SUPPORTED_REF_BRANCH,
    ):
        return report_and_exit(
            json_mode,
            flatpak_id,
            ExitCode.UNHANDLED,
            unhandled_msg,
        )

    ref_build_source = None
    if args.ref_build_path:
        ref_build_path = os.path.abspath(args.ref_build_path)
        if not os.path.isdir(ref_build_path):
            return report_and_exit(
                json_mode,
                flatpak_id,
                ExitCode.FAILURE,
                f"The path does not exist: {ref_build_path}",
            )
        ref_build_source = ref_build_path

    os.makedirs(REPRO_DATADIR, exist_ok=True)
    os.makedirs(FLATPAK_BUILDER_STATE_DIR, exist_ok=True)

    if not json_mode:
        logging.info("Created data directory: %s", REPRO_DATADIR)
        logging.info(
            "Created flatpak-builder root state directory: %s",
            FLATPAK_BUILDER_STATE_DIR,
        )

    output_dir = (
        os.path.abspath(args.output_dir)
        if args.output_dir
        else os.path.abspath(f"./diffoscope_result-{flatpak_id}")
    )

    lockfile_path = os.path.join(REPRO_DATADIR, "flathub_repro_checker.lock")
    with Lock(lockfile_path):
        result = run_repro_check(
            flatpak_id,
            output_dir,
            ref_build_source,
            args.upload_result,
        )

    msg = {
        ExitCode.SUCCESS: "Success",
        ExitCode.UNREPRODUCIBLE: "Unreproducible",
    }.get(result.code, "Failure")

    if json_mode:
        json_exit(flatpak_id, result.code, msg, result.url)

    return int(result.code)


if __name__ == "__main__":
    raise SystemExit(main())
