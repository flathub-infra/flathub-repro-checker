import logging
import os
import tempfile
import zipfile
from typing import TYPE_CHECKING
from urllib.parse import quote

from .subp_utils import run_git

try:
    import boto3

    BOTO3_AVAIL = True
except ImportError:
    BOTO3_AVAIL = False

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client  # noqa: F401


def ensure_boto3() -> bool:
    return BOTO3_AVAIL


def configure_git_file_protocol(unset: bool) -> bool:
    if not unset:
        result = run_git(
            ["config", "--global", "protocol.file.allow", "always"],
            message="Failed to set git file protocol config",
        )
    else:
        result = run_git(
            ["config", "--global", "--unset", "protocol.file.allow"],
            message="Failed to unset git file protocol config",
            warn=True,
        )

    if result:
        action = "unset" if unset else "set"
        logging.info("Successfully %s git file protocol config", action)
        return True
    return False


def process_git_bare_repos(
    bare_repo_path: str,
    checkout_dir: str,
    commit: str,
) -> str | None:
    if not (os.path.isdir(bare_repo_path) and commit):
        return None

    bare_repo_dir = os.path.dirname(bare_repo_path)
    checkout_folder_name = os.path.basename(bare_repo_path) + "_checkout"
    checkout_repo_path = os.path.join(checkout_dir, checkout_folder_name)

    if (
        run_git(
            ["clone", bare_repo_path, checkout_repo_path],
            repo_path=bare_repo_dir,
        )
        is None
    ):
        return None

    if run_git(
        ["checkout", "-f", commit],
        repo_path=checkout_repo_path,
    ):
        return checkout_repo_path

    return None


def zip_directory(dir_path: str) -> str | None:
    if not os.path.isdir(dir_path):
        return None

    zip_path = os.path.join(
        tempfile.gettempdir(),
        f"{os.path.basename(dir_path)}.zip",
    )

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


def upload_to_s3(path: str) -> str:
    url = ""

    if not ensure_boto3():
        logging.error("Uploading results requires 'boto3', but it is not installed")
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
        s3.upload_file(
            path,
            bucket_name,
            object_key,
            ExtraArgs={"ACL": "public-read"},
        )
        if aws_region != "us-east-1":
            url = f"https://{bucket_name}.s3.{aws_region}.amazonaws.com/{quote(object_key)}"
        else:
            url = f"https://{bucket_name}.s3.amazonaws.com/{quote(object_key)}"
    except Exception as err:
        logging.error("Failed to upload file: %s", str(err))

    return url
