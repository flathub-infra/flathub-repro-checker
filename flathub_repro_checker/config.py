import os
from enum import IntEnum
from typing import NamedTuple


class ExitCode(IntEnum):
    SUCCESS = 0
    FAILURE = 1
    UNHANDLED = 2
    UNREPRODUCIBLE = 42


class ReproResult(NamedTuple):
    url: str | None
    code: ExitCode


ALLOWED_RUNTIMES = (
    "org.freedesktop.Platform",
    "org.freedesktop.Sdk",
    "org.gnome.Platform",
    "org.gnome.Sdk",
    "org.kde.Platform",
    "org.kde.Sdk",
)

UNSUPPORTED_FLATPAK_IDS = (
    "org.mozilla.firefox",
    "org.mozilla.Thunderbird",
    "net.pcsx2.PCSX2",
    "org.duckstation.DuckStation",
    "net.wz2100.wz2100",
    "com.obsproject.Studio",
)

SUPPORTED_REF_ARCH = "x86_64"
SUPPORTED_REF_BRANCH = "stable"
RUNTIME_REF_KIND = "runtime"
APP_REF_KIND = "app"

REPRO_DATADIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "flathub_repro_checker",
)
FLATPAK_ROOT_DIR = os.path.join(REPRO_DATADIR, "flatpak_root")
FLATPAK_BUILDER_STATE_DIR = os.path.join(REPRO_DATADIR, "flatpak_builder_state")


def is_inside_container() -> bool:
    return any(os.path.exists(p) for p in ("/.dockerenv", "/run/.containerenv"))


def is_root() -> bool:
    return os.geteuid() == 0
