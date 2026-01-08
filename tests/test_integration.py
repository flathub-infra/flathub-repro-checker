import json
import os
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "appid, allowed_statuses",
    [
        (
            "com.valvesoftware.Steam",
            {"42"},
        ),
        (
            "io.github.kriptolix.Poliedros",
            {"0"},
        ),
    ],
)
def test_full_repro_check_flow(appid: str, allowed_statuses: set[str]) -> None:
    workspace = Path.cwd()

    workdir = workspace / "reproworkdir"
    tmpdir = workdir / "tmp"

    seccomp_path = workspace / "flatpak.seccomp.json"

    workdir.mkdir(parents=True, exist_ok=True)
    tmpdir.mkdir(parents=True, exist_ok=True)

    os.chmod(workdir, 0o777)
    os.chmod(tmpdir, 0o777)

    if not seccomp_path.exists():
        urllib.request.urlretrieve(
            "https://raw.githubusercontent.com/flathub-infra/vorarbeiter/refs/heads/main/flatpak.seccomp.json",
            seccomp_path,
        )

    image = os.environ.get(
        "IMAGE",
        "ghcr.io/flathub-infra/flatpak-builder-lint:unprivileged",
    )

    env = {
        **os.environ,
        "TMPDIR": "/reproworkdir/tmp",
    }

    cmd = [
        "docker",
        "run",
        "--rm",
        "--entrypoint=",
        "--security-opt",
        f"seccomp={seccomp_path}",
        "--security-opt",
        "apparmor=unconfined",
        "--cap-drop",
        "all",
        "-v",
        "/proc:/host/proc",
        "-v",
        f"{workspace}:/src",
        "-v",
        f"{workdir}:/reproworkdir",
        "-e",
        "TMPDIR",
        "-e",
        "GITHUB_SERVER_URL",
        "-e",
        "GITHUB_REPOSITORY",
        "-e",
        "GITHUB_RUN_ID",
        "-w",
        "/src",
        image,
        "python",
        "-m",
        "flathub_repro_checker",
        "--json",
        "--appid",
        appid,
    ]

    result = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr

    data: dict[str, Any] = json.loads(result.stdout)

    assert data["appid"] == appid
    assert data["status_code"] in allowed_statuses
    assert isinstance(data["timestamp"], str)
    assert isinstance(data["message"], str)
    assert isinstance(data["log_url"], str)
    assert isinstance(data["result_url"], str)

    if data["status_code"] == "0":
        assert data["message"].lower() == "success"
        assert data["result_url"] == ""
    elif data["status_code"] == "42":
        assert "repro" in data["message"].lower()
