import logging
import os
import re
import subprocess
from subprocess import CompletedProcess

from .config import FLATPAK_ROOT_DIR, is_inside_container


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
    cur_env = os.environ.copy()
    if env:
        cur_env.update(env)

    if "FLATPAK_USER_DIR" not in cur_env:
        cur_env["FLATPAK_USER_DIR"] = FLATPAK_ROOT_DIR

    if is_inside_container():
        cur_env["FLATPAK_SYSTEM_HELPER_ON_SESSION"] = "foo"

    return _run_command(
        ["flatpak", *args],
        check=check,
        capture_output=capture_output,
        cwd=cwd,
        message=message,
        warn=warn,
        env=cur_env,
    )
