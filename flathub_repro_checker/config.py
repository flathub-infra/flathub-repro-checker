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


class Config:
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

    @staticmethod
    def get_supported_repro_checker_ref(flatpak_id: str) -> str:
        return (
            f"{Config.APP_REF_KIND}/"
            f"{flatpak_id}/"
            f"{Config.SUPPORTED_REF_ARCH}/"
            f"{Config.SUPPORTED_REF_BRANCH}"
        )

    @staticmethod
    def xdg_data_home() -> str:
        return os.environ.get(
            "XDG_DATA_HOME",
            os.path.expanduser("~/.local/share"),
        )

    @staticmethod
    def repro_datadir() -> str:
        return os.path.join(
            Config.xdg_data_home(),
            "flathub_repro_checker",
        )

    @staticmethod
    def flatpak_root_dir() -> str:
        return os.path.join(
            Config.repro_datadir(),
            "flatpak_root",
        )

    @staticmethod
    def flatpak_builder_state_dir() -> str:
        return os.path.join(
            Config.repro_datadir(),
            "flatpak_builder_state",
        )

    @staticmethod
    def is_inside_container() -> bool:
        return any(os.path.exists(p) for p in ("/.dockerenv", "/run/.containerenv"))

    @staticmethod
    def is_root() -> bool:
        return os.geteuid() == 0
