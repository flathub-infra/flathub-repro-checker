import datetime
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Generator
from typing import Any
from unittest.mock import Mock, patch

import pytest

import flathub_repro_checker.__main__ as repro


@pytest.fixture(autouse=True)
def clear_caches() -> Generator[None, None, None]:
    caches: tuple[Any, ...] = (
        repro.parse_manifest,
        repro.get_runtime_ref,
        repro.get_sdk_ref,
        repro.get_baseapp_ref,
        repro.get_sources_ref,
        repro.get_pinned_refs,
        repro.get_flatpak_arch,
    )
    for fn in caches:
        fn.cache_clear()
    yield
    for fn in caches:
        fn.cache_clear()


@pytest.fixture
def temp_dir() -> Generator[str, None, None]:
    with tempfile.TemporaryDirectory(prefix="flathub-repro-test-") as d:
        yield d


@pytest.fixture
def manifest() -> dict[str, Any]:
    return {
        "id": "com.example.App",
        "runtime": "org.freedesktop.Platform",
        "runtime-version": "25.08",
        "sdk": "org.freedesktop.Sdk",
        "runtime-commit": "runtimecommit123",
        "sdk-commit": "sdkcommit456",
        "base": "org.example.BaseApp",
        "base-version": "1.0",
        "base-commit": "basecommit789",
        "sdk-extensions": [
            "org.freedesktop.Sdk.Extension.rust-stable",
            "org.freedesktop.Sdk.Extension.llvm",
        ],
        "add-build-extensions": {
            "org.freedesktop.Sdk.Extension.node18": {
                "version": "25.08",
                "directory": "foo/bar",
            }
        },
        "modules": [
            {
                "sources": [
                    {
                        "type": "git",
                        "url": "https://example.com/example/app.git",
                        "commit": "xyz789",
                    },
                    {"path": "local.txt"},
                ]
            }
        ],
    }


class TestUtilities:
    def test_fp_builder_filename_to_uri(self) -> None:
        assert (
            repro.fp_builder_filename_to_uri("https_example.com_example_app.git")
            == "https://example.com/example/app.git"
        )
        assert repro.fp_builder_filename_to_uri("plain") == "plain"
        assert repro.fp_builder_filename_to_uri("no_underscore") == "no://underscore"

    def test_find_git_src_commit(self, temp_dir: str, manifest: dict[str, Any]) -> None:
        manifest_path = os.path.join(temp_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        assert (
            repro.find_git_src_commit(
                manifest_path,
                "https://example.com/example/app.git",
            )
            == "xyz789"
        )
        assert (
            repro.find_git_src_commit(
                manifest_path,
                "https://example.com/other/repo.git",
            )
            is None
        )

    @patch("flathub_repro_checker.__main__._run_command")
    def test_get_built_app_branch(self, mock_run: Mock, temp_dir: str) -> None:
        manifest_path = os.path.join(temp_dir, "com.example.App.json")
        os.makedirs(os.path.join(temp_dir, "repo"), exist_ok=True)

        mock_run.return_value = Mock(stdout="app/com.example.App/x86_64/repro\n")

        assert repro.get_built_app_branch(manifest_path) == "repro"


class TestLock:
    def _make(self, temp_dir: str) -> tuple[repro.Lock, str]:
        path = os.path.join(temp_dir, "flathub_repro_checker.lock")
        return repro.Lock(path), path

    def test_acquire_release(self, temp_dir: str) -> None:
        lock, path = self._make(temp_dir)
        lock.acquire()
        assert lock.locked
        assert os.path.exists(path)
        lock.release()
        assert not lock.locked
        assert not os.path.exists(path)

    def test_double_acquire(self, temp_dir: str, caplog: pytest.LogCaptureFixture) -> None:
        lock, _ = self._make(temp_dir)
        lock.acquire()
        lock.acquire()
        assert "Lock already acquired" in caplog.text

    def test_context_manager(self, temp_dir: str) -> None:
        path = os.path.join(temp_dir, "flathub_repro_checker.lock")
        with repro.Lock(path) as lock:
            assert lock.locked
            assert os.path.exists(path)
        assert not lock.locked
        assert not os.path.exists(path)


class TestJSONOutput:
    def _run(
        self, capsys: pytest.CaptureFixture[str], *args: Any
    ) -> tuple[int, dict[str, str], str]:
        with pytest.raises(SystemExit) as exc:
            repro.print_json_output(*args)

        captured = capsys.readouterr()
        code_raw = exc.value.code
        assert isinstance(code_raw, int)

        obj: dict[str, str] = json.loads(captured.out)
        return code_raw, obj, captured.err

    def _assert_schema(self, obj: dict[str, str]) -> None:
        assert set(obj) == {
            "timestamp",
            "appid",
            "status_code",
            "log_url",
            "result_url",
            "message",
        }
        for value in obj.values():
            assert isinstance(value, str)

    def test_status_codes(self, capsys: pytest.CaptureFixture[str]) -> None:
        for code in (0, 1, 42):
            _, out, _ = self._run(capsys, "com.example.App", code, "msg")
            self._assert_schema(out)
            assert out["status_code"] == str(code)

    def test_invalid_status(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            repro.print_json_output("com.example.App", 9, "Bad")
        assert "Unknown status code" in capsys.readouterr().err

    def test_timestamp_iso(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, out, _ = self._run(capsys, "com.example.App", 0, "OK")
        datetime.datetime.fromisoformat(out["timestamp"])

    @patch.dict(
        os.environ,
        {
            "GITHUB_REPOSITORY": "example-org/example-repo",
            "GITHUB_RUN_ID": "12345",
            "GITHUB_SERVER_URL": "https://github.com",
        },
        clear=True,
    )
    def test_github_log_url(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, out, _ = self._run(capsys, "com.example.App", 0, "OK")
        assert out["log_url"] == "https://github.com/example-org/example-repo/actions/runs/12345"


class TestManifestParse:
    def _write_manifest(self, base_dir: str, manifest: dict[str, Any]) -> None:
        app_dir = os.path.join(base_dir, "com.example.App")
        os.makedirs(app_dir, exist_ok=True)
        path = os.path.join(app_dir, "com.example.App.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)

    @patch.object(repro, "REPRO_DATADIR", "")
    @patch("flathub_repro_checker.__main__.is_ref_in_remote")
    @patch("flathub_repro_checker.__main__._run_flatpak")
    def test_manifest_parse(
        self,
        mock_run_flatpak: Mock,
        mock_is_ref_in_remote: Mock,
        temp_dir: str,
        manifest: dict[str, Any],
    ) -> None:
        repro.REPRO_DATADIR = temp_dir
        mock_is_ref_in_remote.return_value = True

        mock_run_flatpak.return_value = Mock(
            stdout="""
[Extension org.freedesktop.Platform.GL]
versions = 25.08;25.08-extra;1.4
version = 1.4
""",
            returncode=0,
        )

        self._write_manifest(temp_dir, manifest)

        assert repro.get_runtime_ref("com.example.App") == ["org.freedesktop.Platform//25.08"]
        assert repro.get_sdk_ref("com.example.App") == ["org.freedesktop.Sdk//25.08"]
        assert repro.get_baseapp_ref("com.example.App") == ["org.example.BaseApp//1.0"]

        assert repro.get_base_runtime_version("org.freedesktop.Platform", "25.08") == "25.08"

        assert set(repro.get_build_extension_refs("com.example.App")) == {
            "org.freedesktop.Sdk.Extension.rust-stable//25.08",
            "org.freedesktop.Sdk.Extension.llvm//25.08",
            "org.freedesktop.Sdk.Extension.node18//25.08",
        }

        assert repro.get_sources_ref("com.example.App") == [
            "runtime/com.example.App.Sources/x86_64/stable"
        ]

        assert set(repro.get_build_deps_refs("com.example.App")) == {
            "org.freedesktop.Platform//25.08",
            "org.freedesktop.Sdk//25.08",
            "org.example.BaseApp//1.0",
            "org.freedesktop.Sdk.Extension.rust-stable//25.08",
            "org.freedesktop.Sdk.Extension.llvm//25.08",
            "org.freedesktop.Sdk.Extension.node18//25.08",
        }

        assert repro.get_pinned_refs("com.example.App") == {
            "org.freedesktop.Platform//25.08": "runtimecommit123",
            "org.freedesktop.Sdk//25.08": "sdkcommit456",
            "org.example.BaseApp//1.0": "basecommit789",
        }


class TestDiffoscope:
    @pytest.mark.parametrize(
        "run_result, expected_code",
        [
            (None, 1),
            (Mock(returncode=0), 0),
        ],
    )
    @patch("flathub_repro_checker.__main__._run_command")
    def test_run_diffoscope_basic_outcomes(
        self,
        mock_run: Mock,
        run_result: Mock | None,
        expected_code: int,
        temp_dir: str,
    ) -> None:
        mock_run.return_value = run_result

        out_dir = os.path.join(temp_dir, "diffoscope-output")

        url, code = repro.run_diffoscope(
            "/var/lib/flatpak/app/A",
            "/var/lib/flatpak/app/B",
            out_dir,
        )

        assert url is None
        assert code == expected_code


class TestMain:
    def _run_main(self, argv: list[str]) -> int:
        old_argv = sys.argv[:]
        sys.argv = ["flathub-repro-checker", *argv]
        try:
            return repro.main()
        finally:
            sys.argv = old_argv

    def _sandbox(self, monkeypatch: pytest.MonkeyPatch, temp_dir: str) -> None:
        monkeypatch.setattr(repro, "REPRO_DATADIR", temp_dir)
        monkeypatch.setattr(repro, "FLATPAK_BUILDER_STATE_DIR", os.path.join(temp_dir, "builder"))
        os.makedirs(repro.FLATPAK_BUILDER_STATE_DIR, exist_ok=True)

    @patch("flathub_repro_checker.__main__.is_root", return_value=True)
    @patch("flathub_repro_checker.__main__.print_json_output")
    def test_root_rejected(
        self,
        mock_print_json: Mock,
        _: Mock,
        monkeypatch: pytest.MonkeyPatch,
        temp_dir: str,
    ) -> None:
        self._sandbox(monkeypatch, temp_dir)

        code = self._run_main(["--json", "--appid", "com.example.App"])

        assert code == 1
        assert mock_print_json.call_count >= 1

        first_call = mock_print_json.call_args_list[0].args
        assert first_call[1] == 1
        assert "running the checker as root is unsupported" in first_call[2].lower()

    @patch("flathub_repro_checker.__main__.is_root", return_value=False)
    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["--appid", next(iter(repro.UNSUPPORTED_FLATPAK_IDS))],
        ],
    )
    def test_invalid_appids(
        self,
        _: Mock,
        argv: list[str],
    ) -> None:
        code = self._run_main(argv)
        assert code == 1

    @patch("flathub_repro_checker.__main__.is_root", return_value=False)
    @patch("flathub_repro_checker.__main__.run_repro_check")
    def test_success(
        self,
        mock_run: Mock,
        _: Mock,
        monkeypatch: pytest.MonkeyPatch,
        temp_dir: str,
    ) -> None:
        self._sandbox(monkeypatch, temp_dir)
        mock_run.return_value = (None, 0)

        code = self._run_main(
            [
                "--appid",
                "com.example.App",
                "--output-dir",
                os.path.join(temp_dir, "out"),
            ]
        )

        assert code == 0
        mock_run.assert_called_once()

    @patch("flathub_repro_checker.__main__.is_root", return_value=False)
    @patch("flathub_repro_checker.__main__.run_repro_check")
    @patch("flathub_repro_checker.__main__.print_json_output")
    def test_unreproducible(
        self,
        mock_print_json: Mock,
        mock_run: Mock,
        _: Mock,
        monkeypatch: pytest.MonkeyPatch,
        temp_dir: str,
    ) -> None:
        self._sandbox(monkeypatch, temp_dir)
        mock_run.return_value = ("https://example.com/diff.zip", 42)

        code = self._run_main(
            [
                "--json",
                "--appid",
                "com.example.App",
                "--output-dir",
                os.path.join(temp_dir, "out"),
            ]
        )

        assert code == 42
        mock_print_json.assert_called_once()
        args = mock_print_json.call_args.args
        assert args[1] == 42
        assert args[3] == "https://example.com/diff.zip"

    @patch("flathub_repro_checker.__main__.is_root", return_value=False)
    @pytest.mark.parametrize("dir_exists", [True, False])
    def test_cleanup(
        self,
        _: Mock,
        dir_exists: bool,
        monkeypatch: pytest.MonkeyPatch,
        temp_dir: str,
    ) -> None:
        self._sandbox(monkeypatch, temp_dir)

        if not dir_exists:
            shutil.rmtree(temp_dir)

        code = self._run_main(["--cleanup"])

        assert code == 0
        assert not os.path.exists(temp_dir)
