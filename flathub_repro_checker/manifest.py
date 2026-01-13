import json
import logging
import os
import re
from functools import cached_property
from typing import Any

from .config import Config
from .subp_utils import run_flatpak


class Manifest:
    def __init__(self, flatpak_id: str):
        self.flatpak_id = flatpak_id

    def construct_manifest_save_path(self) -> str:
        manifest_save_dir = Config.manifest_save_dir(self.flatpak_id)
        os.makedirs(manifest_save_dir, exist_ok=True)
        return os.path.abspath(
            os.path.join(
                manifest_save_dir,
                f"{self.flatpak_id}.json",
            )
        )

    def get_saved_manifest_path(self) -> str | None:
        path = self.construct_manifest_save_path()
        return path if os.path.isfile(path) else None

    def save(self) -> bool:
        output_path = self.construct_manifest_save_path()
        if os.path.exists(output_path):
            os.remove(output_path)

        ref = Config.get_supported_repro_checker_ref(self.flatpak_id)
        result = run_flatpak(
            ["run", "--command=/usr/bin/cat", ref, "/app/manifest.json"],
            capture_output=True,
            message=f"Failed to extract manifest from '{ref}'",
        )
        if result is None:
            return False

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(result.stdout)

        return True

    @cached_property
    def data(self) -> dict[str, Any]:
        path = self.get_saved_manifest_path()
        if path:
            with open(path, encoding="utf-8") as f:
                manifest: dict[str, Any] = json.load(f)
                manifest_id = manifest.get("id") or manifest.get("app-id")
                if manifest_id == self.flatpak_id:
                    return manifest
                logging.error(
                    "The 'id' in manifest '%s' does not match the expected id '%s'",
                    manifest_id,
                    self.flatpak_id,
                )
        return {}

    def collect_src_paths(self) -> list[str]:
        def walk_modules(modules: list[dict[str, Any]]) -> list[str]:
            paths: list[str] = []
            for module in modules:
                for source in module.get("sources", []):
                    if "path" in source and "/" not in source["path"].lstrip("./"):
                        paths.append(os.path.basename(source["path"]))
                    if "paths" in source:
                        paths.extend(
                            os.path.basename(p)
                            for p in source["paths"]
                            if "/" not in p.lstrip("./")
                        )
                paths.extend(walk_modules(module.get("modules", [])))
            return paths

        return walk_modules(self.data.get("modules", []))

    def get_runtime_ref(self) -> list[str]:
        if "runtime" in self.data and "runtime-version" in self.data:
            runtime = self.data["runtime"]
            if runtime in Config.ALLOWED_RUNTIMES:
                return [f"{runtime}//{self.data['runtime-version']}"]
            logging.warning("Unknown runtime '%s'", runtime)
        logging.error(
            "Missing 'runtime' or 'runtime-version' in manifest for '%s'",
            self.flatpak_id,
        )
        return []

    def get_sdk_ref(self) -> list[str]:
        if "sdk" in self.data and "runtime-version" in self.data:
            sdk = self.data["sdk"]
            if sdk in Config.ALLOWED_RUNTIMES:
                return [f"{sdk}//{self.data['runtime-version']}"]
            logging.warning("Unknown sdk '%s'", sdk)
        logging.error(
            "Missing 'sdk' or 'runtime-version' in manifest for '%s'",
            self.flatpak_id,
        )
        return []

    def get_baseapp_ref(self) -> list[str]:
        base = self.data.get("base")
        base_version = self.data.get("base-version")
        if base and base_version:
            return [f"{base}//{base_version}"]
        return []

    def construct_sources_ref(self) -> str:
        parts = self.flatpak_id.split(".")
        if parts:
            parts[-1] = parts[-1].replace("-", "_")
        sources_id = ".".join(parts) + ".Sources"

        sources_ref_parts = (
            Config.RUNTIME_REF_KIND,
            sources_id,
            Config.SUPPORTED_REF_ARCH,
            Config.SUPPORTED_REF_BRANCH,
        )
        return "/".join(sources_ref_parts)

    def get_sources_ref(self) -> list[str]:
        sources_ref_str = self.construct_sources_ref()

        result = run_flatpak(
            ["remote-info", "flathub", sources_ref_str],
            capture_output=False,
        )
        if result is not None:
            return [sources_ref_str]

        logging.warning(
            "Failed to find sources extension for '%s'",
            self.flatpak_id,
        )
        return []

    def get_base_runtime_version(
        self,
        ref_id: str,
        ref_branch: str,
    ) -> str | None:
        base_runtime_version = None
        ref = f"{ref_id}//{ref_branch}"

        result = run_flatpak(
            ["remote-info", "-m", "flathub", ref],
            capture_output=True,
        )
        if result is None:
            logging.error("Failed to run remote-info on '%s'", ref)

        if result is not None:
            version_pattern = re.compile(r"^2\d\.08$")
            in_target_section = False
            versions: list[str] = []

            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    in_target_section = stripped == "[Extension org.freedesktop.Platform.GL]"
                    continue
                if not in_target_section or "=" not in stripped:
                    continue

                key, _, value = stripped.partition("=")
                key = key.strip()
                value = value.strip()

                if key == "versions":
                    versions.extend(v.strip() for v in value.split(";"))
                elif key == "version":
                    versions.append(value)

            for version in versions:
                if version_pattern.fullmatch(version):
                    base_runtime_version = version

        if not base_runtime_version:
            logging.error(
                "Failed to determine the version of the base runtime for '%s'."
                "This may result in missing build dependencies during the build process",
                ref,
            )

        return base_runtime_version

    def get_build_extension_refs(self) -> list[str]:
        sdk_exts = self.data.get("sdk-extensions", [])
        add_build_exts = self.data.get("add-build-extensions", {})
        refs: list[str] = []

        if sdk_exts:
            runtime_refs = self.get_runtime_ref()
            if runtime_refs and "//" in runtime_refs[0]:
                runtime_id, runtime_branch = runtime_refs[0].split("//", 1)
                base_branch = self.get_base_runtime_version(
                    runtime_id,
                    runtime_branch,
                )
                if base_branch:
                    for s in sdk_exts:
                        refs.append(f"{s}//{base_branch}")
                else:
                    logging.warning(
                        "No base branch found for runtime '%s'",
                        runtime_refs[0],
                    )

        if isinstance(add_build_exts, dict):
            for ext_id, ext_info in add_build_exts.items():
                version = (
                    ext_info.get(
                        "version",
                        Config.SUPPORTED_REF_BRANCH,
                    )
                    if isinstance(ext_info, dict)
                    else Config.SUPPORTED_REF_BRANCH
                )
                refs.append(f"{ext_id}//{version}")

        return refs

    def get_pinned_refs(self) -> dict[str, str]:
        refs: dict[str, str] = {}

        runtime_ref = self.get_runtime_ref()
        sdk_ref = self.get_sdk_ref()

        if runtime_ref and sdk_ref:
            refs[runtime_ref[0]] = self.data["runtime-commit"]
            refs[sdk_ref[0]] = self.data["sdk-commit"]

        baseapp = self.get_baseapp_ref()
        if baseapp:
            refs[baseapp[0]] = self.data["base-commit"]

        return refs
