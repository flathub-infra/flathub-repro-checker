import datetime
import json
import logging
import os
import shutil
import sys
import tempfile
import zipfile
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import quote

from .config import ExitCode
from .process import _run_command

try:
    import boto3

    BOTO3_AVAIL = True
except ImportError:
    BOTO3_AVAIL = False

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client  # noqa: F401


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


def fp_builder_filename_to_uri(name: str) -> str:
    if "_" not in name:
        return name
    proto, rest = name.split("_", 1)
    return proto + "://" + rest.replace("_", "/")


def print_json_output(
    appid: str, status_code: ExitCode, msg: str, result_url: str | None = None
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


def json_exit(
    appid: str,
    code: ExitCode,
    message: str,
    url: str | None,
) -> NoReturn:
    print_json_output(appid, code, message, url)
    raise SystemExit(0)


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


def setup_logging(json_mode: bool = False) -> None:
    if json_mode:
        logging.disable(logging.CRITICAL)
    else:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


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
