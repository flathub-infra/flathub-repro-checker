import argparse
import datetime
import json
import logging
import os
import shutil
import sys
from typing import NoReturn

from . import __version__
from .config import Config, ExitCode, ReproResult
from .flatpak import FlatpakSession
from .lock import Lock
from .repro import ReproChecker
from .utils import ensure_boto3


def setup_logging(json_mode: bool = False) -> None:
    if json_mode:
        logging.disable(logging.CRITICAL)
    else:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


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


def print_json_output(
    appid: str,
    status_code: ExitCode,
    msg: str,
    result_url: str | None = None,
) -> NoReturn:
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

    ret: dict[str, str] = {
        "timestamp": timestamp,
        "appid": appid,
        "status_code": str(int(status_code)),
        "log_url": log_url,
        "result_url": result_url or "",
        "message": msg,
    }

    print(json.dumps(ret, indent=4))  # noqa: T201
    sys.exit(0)


def json_exit(
    appid: str,
    code: ExitCode,
    message: str,
    url: str | None,
) -> NoReturn:
    print_json_output(appid, code, message, url)


def report_and_exit(
    json_mode: bool,
    appid: str,
    code: ExitCode,
    message: str,
    *,
    level: str = "error",
    url: str = "",
) -> int:
    if json_mode:
        json_exit(appid, code, message, url)

    getattr(logging, level)(message)
    return int(code)


def parse_args() -> tuple[bool, argparse.Namespace]:
    early = argparse.ArgumentParser(add_help=False)
    early.add_argument("--json", action="store_true")
    early_args, _ = early.parse_known_args()

    parser = argparse.ArgumentParser(
        description="Flathub reproducibility checker",
        epilog="""
    This tool only supports x86_64 app Flatpak refs with the stable
    branch from Flathub and any other ref will return an exit code of 2.

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

    if Config.is_root():
        return report_and_exit(
            json_mode,
            "",
            ExitCode.FAILURE,
            "Running the checker as root is unsupported",
        )

    if args.cleanup:
        repro_dir = Config.repro_datadir()
        if os.path.isdir(repro_dir):
            shutil.rmtree(repro_dir)
            return report_and_exit(
                json_mode,
                "",
                ExitCode.SUCCESS,
                f"Cleaning up: {repro_dir}",
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

    if args.upload_result and not ensure_boto3():
        return report_and_exit(
            json_mode,
            flatpak_id,
            ExitCode.FAILURE,
            "Uploading results requires 'boto3', but it is not installed",
        )

    if not validate_env():
        return report_and_exit(
            json_mode,
            "",
            ExitCode.FAILURE,
            "Failed to validate the environment",
        )

    unhandled_msg = f"Running the checker against '{flatpak_id}' is unsupported"

    if flatpak_id in Config.UNSUPPORTED_FLATPAK_IDS:
        return report_and_exit(
            json_mode,
            flatpak_id,
            ExitCode.UNHANDLED,
            unhandled_msg,
        )

    session = FlatpakSession(flatpak_id)

    if not session.setup_flathub():
        return report_and_exit(
            json_mode,
            flatpak_id,
            ExitCode.FAILURE,
            "Failed to set up Flathub remote",
        )

    if not session.is_ref_in_remote(Config.get_supported_repro_checker_ref(flatpak_id)):
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

    os.makedirs(Config.repro_datadir(), exist_ok=True)
    os.makedirs(Config.flatpak_builder_state_dir(), exist_ok=True)

    if not json_mode:
        logging.info(
            "Created data directory: %s",
            Config.repro_datadir(),
        )
        logging.info(
            "Created flatpak-builder root state directory: %s",
            Config.flatpak_builder_state_dir(),
        )

    output_dir = (
        os.path.abspath(args.output_dir)
        if args.output_dir
        else Config.diffoscope_output_dir(flatpak_id)
    )

    result: ReproResult | None = None

    with Lock(Config.lockfile_path()):
        checker = ReproChecker(
            flatpak_id=flatpak_id,
            output_dir=output_dir,
            build_src=ref_build_source,
            upload_results=args.upload_result,
        )
        result = checker.run()

    if result is None:
        return report_and_exit(
            json_mode,
            flatpak_id,
            ExitCode.FAILURE,
            "The checker did not produce a result",
        )

    msg = result.code.message()

    if json_mode:
        json_exit(flatpak_id, result.code, msg, result.url)

    return int(result.code)


if __name__ == "__main__":
    raise SystemExit(main())
