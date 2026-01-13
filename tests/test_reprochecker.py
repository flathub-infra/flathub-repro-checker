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

import flathub_repro_checker.__main__ as main
from flathub_repro_checker import flatpak, repro, utils
from flathub_repro_checker.config import Config, ExitCode, ReproResult
from flathub_repro_checker.lock import Lock
from flathub_repro_checker.manifest import Manifest


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


class TestConfig:
    @pytest.mark.parametrize(
        "code,value",
        [
            (ExitCode.SUCCESS, 0),
            (ExitCode.FAILURE, 1),
            (ExitCode.UNHANDLED, 2),
            (ExitCode.UNREPRODUCIBLE, 42),
        ],
    )
    def test_exit_code(self, code: ExitCode, value: int) -> None:
        assert code == value

    def test_exit_code_invalid(self) -> None:
        with pytest.raises(ValueError):
            ExitCode(99)

    def test_repro_result_structure(self) -> None:
        result = ReproResult("https://example.com", ExitCode.SUCCESS)
        assert result.url == "https://example.com"
        assert result.code == ExitCode.SUCCESS

    @pytest.mark.parametrize(
        "attr",
        [
            "ALLOWED_RUNTIMES",
            "UNSUPPORTED_FLATPAK_IDS",
        ],
    )
    def test_immutable_config_constants(self, attr: str) -> None:
        constant = getattr(Config, attr)
        assert isinstance(constant, tuple | frozenset)


class TestUtilities:
    def test_fp_builder_filename_to_uri(self) -> None:
        assert (
            utils.fp_builder_filename_to_uri("https_example.com_example_app.git")
            == "https://example.com/example/app.git"
        )
        assert utils.fp_builder_filename_to_uri("plain") == "plain"
        assert utils.fp_builder_filename_to_uri("no_underscore") == "no://underscore"
        assert utils.fp_builder_filename_to_uri("") == ""

    def test_find_git_src_commit(self, temp_dir: str, manifest: dict[str, Any]) -> None:
        manifest_path = os.path.join(temp_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        assert (
            flatpak.find_git_src_commit(
                manifest_path,
                "https://example.com/example/app.git",
            )
            == "xyz789"
        )
        assert (
            flatpak.find_git_src_commit(
                manifest_path,
                "https://example.com/other/repo.git",
            )
            is None
        )

    def test_find_git_src_commit_no_commit(self, temp_dir: str) -> None:
        manifest = {
            "modules": [
                {
                    "sources": [
                        {
                            "type": "git",
                            "url": "https://example.com/app.git",
                        }
                    ]
                }
            ]
        }
        manifest_path = os.path.join(temp_dir, "manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)
        assert flatpak.find_git_src_commit(manifest_path, "https://example.com/app.git") is None

    def test_replace_git_sources(self, temp_dir: str, manifest: dict[str, Any]) -> None:
        manifest_path = os.path.join(temp_dir, "manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        checkout_dir = os.path.join(temp_dir, "checkout")
        os.makedirs(checkout_dir)

        replace_dict = {"https://example.com/example/app.git": checkout_dir}

        assert flatpak.replace_git_sources(manifest_path, replace_dict)

        with open(manifest_path) as f:
            updated = json.load(f)

        url = updated["modules"][0]["sources"][0]["url"]
        assert url.startswith("file://")
        assert checkout_dir in url

        backup_file = f"{manifest_path}.backup"
        assert os.path.exists(backup_file)

    @pytest.mark.parametrize(
        "stdout,expected",
        [
            ("app/com.example.App/x86_64/repro\n", "repro"),
            ("runtime/something/x86_64/stable\n", None),
        ],
    )
    @patch("flathub_repro_checker.flatpak.run_command")
    def test_get_built_app_branch(
        self,
        mock_run: Mock,
        stdout: str,
        expected: str | None,
        temp_dir: str,
    ) -> None:
        manifest_path = os.path.join(temp_dir, "com.example.App.json")
        os.makedirs(os.path.join(temp_dir, "repo"), exist_ok=True)

        mock_run.return_value = Mock(stdout=stdout)

        session = flatpak.FlatpakSession("com.example.App")
        assert session.get_built_app_branch(manifest_path) == expected

    def test_zip_directory(self, temp_dir: str) -> None:
        test_dir = os.path.join(temp_dir, "testdir")
        os.makedirs(test_dir)

        with open(os.path.join(test_dir, "file1.txt"), "w") as f:
            f.write("content1")
        with open(os.path.join(test_dir, "file2.txt"), "w") as f:
            f.write("content2")

        zip_path = utils.zip_directory(test_dir)
        assert zip_path is not None
        assert os.path.exists(zip_path)
        assert zip_path.endswith(".zip")

    @patch("flathub_repro_checker.utils.BOTO3_AVAIL", True)
    @patch("flathub_repro_checker.utils.boto3")
    @patch.dict(os.environ, {"AWS_S3_BUCKET_NAME": "test-bucket"})
    def test_upload_to_s3(self, mock_boto3: Mock, temp_dir: str) -> None:
        test_file = os.path.join(temp_dir, "test.txt")
        with open(test_file, "w") as f:
            f.write("test")

        mock_client = Mock()
        mock_boto3.client.return_value = mock_client

        url = utils.upload_to_s3(test_file)
        assert url.startswith("https://")
        assert "test-bucket" in url
        mock_client.upload_file.assert_called_once()


class TestLock:
    def _make(self, temp_dir: str) -> tuple[Lock, str]:
        path = os.path.join(temp_dir, "flathub_repro_checker.lock")
        return Lock(path), path

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
        with Lock(path) as lock:
            assert lock.locked
            assert os.path.exists(path)
        assert not lock.locked
        assert not os.path.exists(path)

    def test_acquire_concurrent_fails(self, temp_dir: str) -> None:
        lock1, path = self._make(temp_dir)
        lock2 = Lock(path)

        lock1.acquire()
        with pytest.raises(SystemExit):
            lock2.acquire()

    def test_release_without_acquire(self, temp_dir: str) -> None:
        lock, _ = self._make(temp_dir)
        lock.release()
        assert not lock.locked


class TestJSONOutput:
    def _run(
        self, capsys: pytest.CaptureFixture[str], *args: Any
    ) -> tuple[int, dict[str, str], str]:
        with pytest.raises(SystemExit) as exc:
            main.print_json_output(*args)

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
        for code in (ExitCode.SUCCESS, ExitCode.FAILURE, ExitCode.UNREPRODUCIBLE):
            _, out, _ = self._run(capsys, "com.example.App", code, "msg")
            assert out["status_code"] == str(int(code))

    def test_invalid_status(self) -> None:
        with pytest.raises(ValueError):
            ExitCode(9)

    def test_timestamp_iso(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, out, _ = self._run(capsys, "com.example.App", ExitCode.SUCCESS, "OK")
        datetime.datetime.fromisoformat(out["timestamp"])

    def test_json_output_with_url(self, capsys: pytest.CaptureFixture[str]) -> None:
        _, out, _ = self._run(
            capsys,
            "com.example.App",
            ExitCode.UNREPRODUCIBLE,
            "msg",
            "https://example.com/result.zip",
        )
        assert out["result_url"] == "https://example.com/result.zip"

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
        _, out, _ = self._run(capsys, "com.example.App", ExitCode.SUCCESS, "OK")
        assert out["log_url"] == "https://github.com/example-org/example-repo/actions/runs/12345"


class TestManifestParse:
    def _write_manifest(self, base_dir: str, manifest: dict[str, Any]) -> str:
        app_dir = os.path.join(base_dir, "com.example.App")
        os.makedirs(app_dir, exist_ok=True)
        path = os.path.join(app_dir, "com.example.App.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        return path

    @patch("flathub_repro_checker.manifest.run_flatpak")
    def test_manifest_parse(
        self,
        mock_run_flatpak: Mock,
        temp_dir: str,
        manifest: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(Config, "repro_datadir", staticmethod(lambda: temp_dir))

        mock_run_flatpak.return_value = Mock(
            stdout="""
[Extension org.freedesktop.Platform.GL]
versions = 25.08;25.08-extra;1.4
version = 1.4
""",
            returncode=0,
        )

        self._write_manifest(temp_dir, manifest)

        m = Manifest("com.example.App")

        assert m.get_runtime_ref() == ["org.freedesktop.Platform//25.08"]
        assert m.get_sdk_ref() == ["org.freedesktop.Sdk//25.08"]
        assert m.get_baseapp_ref() == ["org.example.BaseApp//1.0"]
        assert m.construct_sources_ref() == "runtime/com.example.App.Sources/x86_64/stable"
        assert m.get_base_runtime_version("org.freedesktop.Platform", "25.08") == "25.08"

        assert m.get_build_extension_refs() == [
            "org.freedesktop.Sdk.Extension.rust-stable//25.08",
            "org.freedesktop.Sdk.Extension.llvm//25.08",
            "org.freedesktop.Sdk.Extension.node18//25.08",
        ]

        assert m.get_pinned_refs() == {
            "org.freedesktop.Platform//25.08": "runtimecommit123",
            "org.freedesktop.Sdk//25.08": "sdkcommit456",
            "org.example.BaseApp//1.0": "basecommit789",
        }

    def test_manifest_missing_runtime(self, temp_dir: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Config, "repro_datadir", staticmethod(lambda: temp_dir))

        bad_manifest = {"id": "com.example.App"}

        self._write_manifest(temp_dir, bad_manifest)

        m = Manifest("com.example.App")
        assert m.get_runtime_ref() == []
        assert m.get_sdk_ref() == []

    def test_manifest_missing_base(self, temp_dir: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Config, "repro_datadir", staticmethod(lambda: temp_dir))

        manifest_no_base = {
            "id": "com.example.App",
            "runtime": "org.freedesktop.Platform",
            "runtime-version": "25.08",
        }

        self._write_manifest(temp_dir, manifest_no_base)

        m = Manifest("com.example.App")
        assert m.get_baseapp_ref() == []

    def test_manifest_unknown_runtime(
        self, temp_dir: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(Config, "repro_datadir", staticmethod(lambda: temp_dir))

        manifest_unknown = {
            "id": "com.example.App",
            "runtime": "org.unknown.Runtime",
            "runtime-version": "1.0",
            "sdk": "org.unknown.Sdk",
        }

        self._write_manifest(temp_dir, manifest_unknown)

        m = Manifest("com.example.App")
        m.get_runtime_ref()
        assert "Unknown runtime" in caplog.text

    def test_manifest_collect_src_paths(
        self, temp_dir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Config, "repro_datadir", staticmethod(lambda: temp_dir))

        manifest_with_paths = {
            "id": "com.example.App",
            "modules": [
                {"sources": [{"path": "file1.txt"}, {"path": "./file2.txt"}]},
                {"sources": [{"paths": ["file3.txt", "subdir/file4.txt"]}]},
            ],
        }

        self._write_manifest(temp_dir, manifest_with_paths)

        m = Manifest("com.example.App")
        paths = m.collect_src_paths()
        assert "file1.txt" in paths
        assert "file2.txt" in paths
        assert "file3.txt" in paths

    def test_manifest_id_mismatch(
        self, temp_dir: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(Config, "repro_datadir", staticmethod(lambda: temp_dir))

        wrong_manifest = {"id": "com.example.Wrong"}
        self._write_manifest(temp_dir, wrong_manifest)

        m = Manifest("com.example.App")
        _ = m.data
        assert "does not match" in caplog.text


class TestDiffoscope:
    @pytest.mark.parametrize(
        "run_result, expected_code",
        [
            (None, ExitCode.FAILURE),
            (Mock(returncode=0), ExitCode.SUCCESS),
        ],
    )
    @patch("flathub_repro_checker.repro.run_command")
    def test_run_diffoscope_outcomes(
        self,
        mock_run: Mock,
        run_result: Mock | None,
        expected_code: ExitCode,
        temp_dir: str,
    ) -> None:
        mock_run.return_value = run_result

        checker = repro.ReproChecker(
            flatpak_id="com.example.App",
            output_dir=os.path.join(temp_dir, "out"),
            build_src=None,
            upload_results=False,
        )

        result = checker.run_diffoscope("/a", "/b")

        assert isinstance(result, ReproResult)
        assert result.url is None
        assert result.code is expected_code


class TestMain:
    @pytest.fixture
    def mock_default_setup(self) -> Generator[None, None, None]:
        with (
            patch("flathub_repro_checker.__main__.Config.is_root", return_value=False),
            patch("flathub_repro_checker.__main__.validate_env", return_value=True),
            patch(
                "flathub_repro_checker.__main__.FlatpakSession.setup_flathub",
                return_value=True,
            ),
            patch(
                "flathub_repro_checker.__main__.FlatpakSession.is_ref_in_remote",
                return_value=True,
            ),
        ):
            yield

    @pytest.fixture
    def sandbox(self, monkeypatch: pytest.MonkeyPatch, temp_dir: str) -> None:
        monkeypatch.setattr(Config, "repro_datadir", staticmethod(lambda: temp_dir))
        monkeypatch.setattr(
            Config,
            "flatpak_builder_state_dir",
            staticmethod(lambda: os.path.join(temp_dir, "builder")),
        )
        os.makedirs(Config.flatpak_builder_state_dir(), exist_ok=True)

    def _invoke_main(self, argv: list[str]) -> int:
        old_argv = sys.argv[:]
        sys.argv = ["flathub-repro-checker", *argv]
        try:
            return main.main()
        finally:
            sys.argv = old_argv

    def _run_main(self, argv: list[str]) -> int:
        return self._invoke_main(argv)

    def _run_main_json(
        self,
        capsys: pytest.CaptureFixture[str],
        argv: list[str],
    ) -> tuple[int, dict[str, str]]:
        with pytest.raises(SystemExit) as exc:
            self._invoke_main(argv)

        exit_code = exc.value.code
        assert isinstance(exit_code, int)

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        return exit_code, data

    def _assert_json_schema(self, obj: dict[str, str]) -> None:
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

    @pytest.mark.usefixtures("sandbox")
    @patch("flathub_repro_checker.__main__.Config.is_root", return_value=True)
    def test_root_rejected_json(
        self,
        _is_root: Mock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code, data = self._run_main_json(
            capsys,
            ["--json", "--appid", "com.example.App"],
        )

        assert exit_code == 0
        self._assert_json_schema(data)
        assert data["appid"] == ""
        assert data["status_code"] == str(int(ExitCode.FAILURE))
        assert "root is unsupported" in data["message"].lower()

    @pytest.mark.usefixtures("mock_default_setup", "sandbox")
    @patch("flathub_repro_checker.__main__.ReproChecker.run")
    def test_unreproducible_json(
        self,
        mock_run: Mock,
        temp_dir: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_run.return_value = ReproResult(
            "https://example.com/diff.zip",
            ExitCode.UNREPRODUCIBLE,
        )

        exit_code, data = self._run_main_json(
            capsys,
            [
                "--json",
                "--appid",
                "com.example.App",
                "--output-dir",
                os.path.join(temp_dir, "out"),
            ],
        )

        assert exit_code == 0
        self._assert_json_schema(data)
        assert data["appid"] == "com.example.App"
        assert data["status_code"] == str(int(ExitCode.UNREPRODUCIBLE))
        assert "reproducible" in data["message"].lower()
        assert data["result_url"] == "https://example.com/diff.zip"

    @patch("flathub_repro_checker.__main__.Config.is_root", return_value=False)
    @patch("flathub_repro_checker.__main__.validate_env", return_value=True)
    @pytest.mark.parametrize(
        "argv,expected_code",
        [
            ([], ExitCode.FAILURE),
            (["--appid", "org.mozilla.firefox"], ExitCode.UNHANDLED),
        ],
    )
    def test_early_exit_conditions(
        self,
        _validate: Mock,
        _is_root: Mock,
        argv: list[str],
        expected_code: ExitCode,
    ) -> None:
        code = self._run_main(argv)
        assert code == expected_code

    @pytest.mark.usefixtures("mock_default_setup", "sandbox")
    @patch("flathub_repro_checker.__main__.ReproChecker.run")
    def test_success_non_json(
        self,
        mock_run: Mock,
        temp_dir: str,
    ) -> None:
        mock_run.return_value = ReproResult(None, ExitCode.SUCCESS)

        code = self._run_main(
            [
                "--appid",
                "com.example.App",
                "--output-dir",
                os.path.join(temp_dir, "out"),
            ]
        )

        assert code == ExitCode.SUCCESS

    @pytest.mark.usefixtures("sandbox")
    @patch("flathub_repro_checker.__main__.Config.is_root", return_value=False)
    @pytest.mark.parametrize("dir_exists", [True, False])
    def test_cleanup(
        self,
        _is_root: Mock,
        dir_exists: bool,
        temp_dir: str,
    ) -> None:
        if not dir_exists:
            shutil.rmtree(temp_dir)

        code = self._run_main(["--cleanup"])

        assert code == ExitCode.SUCCESS
        assert not os.path.exists(temp_dir)

    @patch("flathub_repro_checker.__main__.Config.is_root", return_value=False)
    @pytest.mark.parametrize(
        "validate_env,setup_flathub,is_ref_in_remote,expected_code",
        [
            (False, True, True, ExitCode.FAILURE),
            (True, False, True, ExitCode.FAILURE),
            (True, True, False, ExitCode.UNHANDLED),
        ],
    )
    def test_setup_failures(
        self,
        _is_root: Mock,
        validate_env: bool,
        setup_flathub: bool,
        is_ref_in_remote: bool,
        expected_code: ExitCode,
    ) -> None:
        with (
            patch(
                "flathub_repro_checker.__main__.validate_env",
                return_value=validate_env,
            ),
            patch(
                "flathub_repro_checker.__main__.FlatpakSession.setup_flathub",
                return_value=setup_flathub,
            ),
            patch(
                "flathub_repro_checker.__main__.FlatpakSession.is_ref_in_remote",
                return_value=is_ref_in_remote,
            ),
        ):
            code = self._run_main(["--appid", "com.example.App"])
            assert code == expected_code

    @pytest.mark.usefixtures("mock_default_setup", "sandbox")
    @patch("flathub_repro_checker.__main__.ReproChecker.run")
    @pytest.mark.parametrize(
        "ref_build_exists,expected_code",
        [
            (True, ExitCode.SUCCESS),
            (False, ExitCode.FAILURE),
        ],
    )
    def test_ref_build_path(
        self,
        mock_run: Mock,
        temp_dir: str,
        ref_build_exists: bool,
        expected_code: ExitCode,
    ) -> None:
        ref_build_path = os.path.join(temp_dir, "ref_build")

        if ref_build_exists:
            os.makedirs(ref_build_path)
            mock_run.return_value = ReproResult(None, ExitCode.SUCCESS)

        code = self._run_main(["--appid", "com.example.App", "--ref-build-path", ref_build_path])

        assert code == expected_code
