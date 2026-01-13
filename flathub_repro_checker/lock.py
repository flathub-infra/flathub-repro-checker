import contextlib
import errno
import fcntl
import logging
import os
import types
from typing import TextIO

from .config import ExitCode


class Lock:
    def __init__(self, path: str) -> None:
        self.lock_path: str = path
        self.lock_file: TextIO | None = None
        self.locked: bool = False

    def acquire(self) -> None:
        if self.locked:
            logging.warning("Lock already acquired: %s", self.lock_path)
            return

        self.lock_file = open(self.lock_path, "w")  # noqa: SIM115
        try:
            fcntl.flock(
                self.lock_file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            self.locked = True
            logging.info("Lock acquired: %s", self.lock_path)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                logging.error("Another instance is already running. Exiting")
                raise SystemExit(int(ExitCode.FAILURE)) from e
            raise

    def release(self) -> None:
        if self.locked and self.lock_file:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()
            with contextlib.suppress(FileNotFoundError):
                os.remove(self.lock_path)
            self.locked = False
            logging.info(
                "Lock released and lockfile deleted: %s",
                self.lock_path,
            )

    def __enter__(self) -> "Lock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.release()
