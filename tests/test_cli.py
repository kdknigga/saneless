"""Tests for CLI commands via click.testing.CliRunner."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import tomlkit
from click.testing import CliRunner
from PIL import Image, ImageDraw

from saneless.cli import cli
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.exceptions import PaperlessError, ScanError
from saneless.job import JobStore
from saneless.scanner.base import DeviceCapabilities, DeviceInfo

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest


_TEST_TMP = str(Path(tempfile.gettempdir()) / "saneless-test")
_TEST_LOG = str(Path(tempfile.gettempdir()) / "saneless-test" / "saneless.log")


def _make_settings(**overrides: object) -> Settings:
    """Create a Settings instance with test defaults."""
    auth = "test-token"
    defaults: dict[str, Any] = {
        "scanner": ScannerConfig(device="test:device:001"),
        "paperless": PaperlessConfig(
            url="http://localhost:8000",
            token=auth,
        ),
        "output": OutputConfig(
            tmp_dir=_TEST_TMP,
            log_file=_TEST_LOG,
        ),
        "profiles": {
            "default": ProfileConfig(),
            "photo": ProfileConfig(resolution=600, mode="color"),
        },
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _patch_cli(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings | None = None,
    scanner_cls: type | None = None,
    paperless_cls: type | None = None,
) -> tuple[CliRunner, Settings]:
    """
    Patch cli module dependencies for testing.

    Returns (runner, settings_used).
    """
    settings = settings or _make_settings()

    monkeypatch.setattr(
        "saneless.cli.load_settings",
        lambda *_args, **_kwargs: settings,
    )
    monkeypatch.setattr(
        "saneless.cli.configure_logging",
        lambda *_args, **_kwargs: None,
    )

    if scanner_cls is not None:
        monkeypatch.setattr("saneless.cli.SaneBackend", scanner_cls)
    else:

        class MockSaneBackend:
            """Mock scanner backend for CLI tests."""

            def get_devices(self) -> list[DeviceInfo]:
                """Return two test scanner devices."""
                return [
                    DeviceInfo("epson:001", "Epson", "ET-4850", "flatbed scanner"),
                    DeviceInfo(
                        "hp:002", "HP", "Envy 6055", "multi-function peripheral"
                    ),
                ]

            def get_capabilities(self, _device_id: str) -> DeviceCapabilities:
                """Return fixed test capabilities."""
                return DeviceCapabilities(
                    sources=["Flatbed", "ADF"],
                    resolutions=[150, 300, 600],
                    modes=["color", "gray"],
                    raw_options=[
                        (
                            0,
                            "source",
                            "Source",
                            "desc",
                            3,
                            0,
                            1,
                            0,
                            ["Flatbed", "ADF"],
                        ),
                    ],
                )

            def scan_pages(
                self, _device_id: str, _settings: object
            ) -> Iterator[Image.Image]:
                """Return a single test image with content."""
                img = Image.new("RGB", (100, 100), "white")
                draw = ImageDraw.Draw(img)
                draw.rectangle([10, 10, 90, 90], fill="black")
                return iter([img])

        monkeypatch.setattr("saneless.cli.SaneBackend", MockSaneBackend)

    if paperless_cls is not None:
        monkeypatch.setattr("saneless.cli.PaperlessClient", paperless_cls)
    else:

        class MockPaperlessClient:
            """Mock paperless client for CLI tests."""

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                """Accept and ignore all constructor arguments."""

            def upload_document(self, *_args: object, **_kwargs: object) -> str:
                """Return a fake task UUID."""
                return "mock-task-uuid"

            def poll_task(self, *_args: object, **_kwargs: object) -> dict[str, str]:
                """Return a successful task result."""
                return {"status": "SUCCESS"}

            def close(self) -> None:
                """No-op close."""

        monkeypatch.setattr("saneless.cli.PaperlessClient", MockPaperlessClient)

    return CliRunner(), settings


class TestCliHelp:
    """CLI help text tests."""

    def test_cli_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Main CLI --help shows usage information."""
        runner, _ = _patch_cli(monkeypatch)
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "saneless" in result.output.lower() or "scan" in result.output.lower()

    def test_scan_command_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scan subcommand --help shows --profile and --title options."""
        runner, _ = _patch_cli(monkeypatch)
        result = runner.invoke(cli, ["scan", "--help"])
        assert result.exit_code == 0
        assert "--profile" in result.output
        assert "--title" in result.output

    def test_devices_command_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Devices subcommand --help shows --json and --capabilities options."""
        runner, _ = _patch_cli(monkeypatch)
        result = runner.invoke(cli, ["devices", "--help"])
        assert result.exit_code == 0
        assert "--json" in result.output
        assert "--capabilities" in result.output


class TestScanCommand:
    """Scan command tests."""

    def test_scan_requires_title(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scan without --title exits with non-zero code."""
        runner, _ = _patch_cli(monkeypatch)
        result = runner.invoke(cli, ["scan"])
        assert result.exit_code != 0

    def test_scan_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scan with --title succeeds and shows Done message."""
        runner, _ = _patch_cli(monkeypatch)
        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 0
        assert "Done: Test" in result.output

    def test_scan_status_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scan shows progress messages during pipeline execution."""
        runner, _ = _patch_cli(monkeypatch)
        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert "Scanning..." in result.output
        assert "Assembling PDF..." in result.output
        assert "Uploading to paperless-ngx..." in result.output

    def test_scan_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config loading fails -> exit code 2."""
        runner = CliRunner()

        def bad_load(*_args: object, **_kwargs: object) -> None:
            msg = "bad config"
            raise ValueError(msg)

        monkeypatch.setattr("saneless.cli.load_settings", bad_load)
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *_a, **_kw: None)

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 2
        assert "Configuration error" in result.output

    def test_scan_scan_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pipeline raises ScanError -> exit code 1."""

        class FailScanner:
            """Scanner that always raises ScanError."""

            def scan_pages(self, *_args: object, **_kwargs: object) -> None:
                """Raise a scan error."""
                msg = "Paper jam"
                raise ScanError(msg)

        runner, _ = _patch_cli(monkeypatch, scanner_cls=FailScanner)

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 1
        assert "Paper jam" in result.output

    def test_scan_paperless_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pipeline raises PaperlessError -> exit code 3."""

        class FailPaperless:
            """Paperless client that always raises PaperlessError on upload."""

            def __init__(self, *_a: object, **_kw: object) -> None:
                """Accept and ignore all constructor arguments."""

            def upload_document(self, *_a: object, **_kw: object) -> None:
                """Raise a paperless error."""
                msg = "Server down"
                raise PaperlessError(msg)

            def close(self) -> None:
                """No-op close."""

        runner, _ = _patch_cli(monkeypatch, paperless_cls=FailPaperless)

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 3
        assert "Server down" in result.output

    def test_scan_with_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scan --profile photo -> pipeline called with profile_name='photo'."""
        captured: dict[str, object] = {}

        def capturing_pipeline(*args: object, **_kwargs: object) -> dict[str, str]:
            """Capture the PipelineRequest from the 4th positional arg."""
            captured["request"] = args[3]
            return {"status": "SUCCESS"}

        runner, _ = _patch_cli(monkeypatch)
        monkeypatch.setattr("saneless.cli.run_pipeline", capturing_pipeline)

        result = runner.invoke(cli, ["scan", "--profile", "photo", "--title", "Test"])
        assert result.exit_code == 0
        request = captured["request"]
        assert hasattr(request, "profile_name")
        assert request.profile_name == "photo"


class TestDevicesCommand:
    """Devices command tests."""

    def test_devices_table_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Devices -> table with device names, vendors, models."""
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["devices"])
        assert result.exit_code == 0
        assert "epson:001" in result.output
        assert "Epson" in result.output
        assert "ET-4850" in result.output
        assert "hp:002" in result.output

    def test_devices_json_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Devices --json -> valid JSON with device list."""
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["devices", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["name"] == "epson:001"

    def test_devices_capabilities(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Devices --capabilities -> raw option names shown."""
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["devices", "--capabilities"])
        assert result.exit_code == 0
        assert "Flatbed" in result.output
        assert "ADF" in result.output


class TestCliFlags:
    """CLI flag tests."""

    def test_verbose_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The -v flag calls configure_logging with verbose=True."""
        captured: dict[str, object] = {}

        def capture_logging(*_args: object, **kwargs: object) -> None:
            """Record whether verbose was passed."""
            captured["verbose"] = kwargs.get("verbose", False)

        runner = CliRunner()
        monkeypatch.setattr(
            "saneless.cli.load_settings", lambda *_a, **_kw: _make_settings()
        )
        monkeypatch.setattr("saneless.cli.configure_logging", capture_logging)

        class MockSaneBackend:
            """Mock scanner that returns no devices."""

            def get_devices(self) -> list[DeviceInfo]:
                """Return empty device list."""
                return []

        monkeypatch.setattr("saneless.cli.SaneBackend", MockSaneBackend)

        result = runner.invoke(cli, ["-v", "devices"])
        assert result.exit_code == 0
        assert captured.get("verbose") is True

    def test_config_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--config /path/to/config -> load_settings called with that path."""
        captured: dict[str, object] = {}

        def capture_load(config_path: str | None = None) -> Settings:
            """Record the config_path argument."""
            captured["config_path"] = config_path
            return _make_settings()

        runner = CliRunner()
        monkeypatch.setattr("saneless.cli.load_settings", capture_load)
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *_a, **_kw: None)

        class MockSaneBackend:
            """Mock scanner that returns no devices."""

            def get_devices(self) -> list[DeviceInfo]:
                """Return empty device list."""
                return []

        monkeypatch.setattr("saneless.cli.SaneBackend", MockSaneBackend)

        result = runner.invoke(cli, ["--config", "/path/to/config.toml", "devices"])
        assert result.exit_code == 0
        assert captured["config_path"] == "/path/to/config.toml"


class TestJobsCommand:
    """Jobs command tests."""

    def _populate_store(self, db_path: str, count: int = 2) -> None:
        """Populate a JobStore at db_path with test jobs."""
        store = JobStore(db_path=db_path)
        for i in range(count):
            store.create_job(
                profile="default" if i % 2 == 0 else "photo",
                title=f"Test Document {i + 1}",
            )
        store.close()

    def test_jobs_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Jobs with no jobs in DB shows empty output (exit 0)."""
        db_path = str(tmp_path / "saneless.db")
        settings = _make_settings(
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        # Create empty DB
        store = JobStore(db_path=db_path)
        store.close()

        result = runner.invoke(cli, ["jobs"])
        assert result.exit_code == 0

    def test_jobs_table_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Jobs with 2 jobs shows table with Timestamp, Profile, Title, Status columns."""
        db_path = str(tmp_path / "saneless.db")
        settings = _make_settings(
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )
        self._populate_store(db_path, count=2)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])
        assert result.exit_code == 0
        assert "Timestamp" in result.output
        assert "Profile" in result.output
        assert "Title" in result.output
        assert "Status" in result.output
        assert "Test Document 1" in result.output
        assert "Test Document 2" in result.output
        assert "PENDING" in result.output

    def test_jobs_json_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Jobs --json with 2 jobs returns valid JSON array."""
        db_path = str(tmp_path / "saneless.db")
        settings = _make_settings(
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )
        self._populate_store(db_path, count=2)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 2
        for item in data:
            assert "id" in item
            assert "profile" in item
            assert "title" in item
            assert "state" in item
            assert "created_at" in item

    def test_jobs_limit(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Jobs --limit 1 with 2 jobs in DB shows only 1 job."""
        db_path = str(tmp_path / "saneless.db")
        settings = _make_settings(
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )
        self._populate_store(db_path, count=2)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--limit", "1"])
        assert result.exit_code == 0
        # Should only have 1 data row (plus header and separator)
        lines = [line for line in result.output.strip().split("\n") if line.strip()]
        # Header + separator + 1 data row = 3 lines
        assert len(lines) == 3

    def test_jobs_exit_code_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Jobs always exits with code 0."""
        settings = _make_settings(
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])
        assert result.exit_code == 0

    def test_jobs_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Jobs --help shows --json and --limit options."""
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["jobs", "--help"])
        assert result.exit_code == 0
        assert "--json" in result.output
        assert "--limit" in result.output

    def test_jobs_json_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Jobs --json with no jobs outputs empty JSON array."""
        db_path = str(tmp_path / "saneless.db")
        settings = _make_settings(
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )
        # Create empty DB
        store = JobStore(db_path=db_path)
        store.close()
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == []


class TestServeCommand:
    """Serve command tests."""

    @staticmethod
    def _mock_socket(monkeypatch: pytest.MonkeyPatch) -> None:
        """Bypass the port-availability check in serve()."""
        mock_sock = MagicMock()
        monkeypatch.setattr("saneless.cli.socket.socket", lambda *_a, **_kw: mock_sock)

    @staticmethod
    def _capture_uvicorn(
        monkeypatch: pytest.MonkeyPatch,
    ) -> dict[str, object]:
        """Patch uvicorn.run to capture args and close the app's job_store."""
        captured: dict[str, object] = {}

        def mock_uvicorn_run(app: object, **kwargs: object) -> None:
            """Capture uvicorn.run arguments and close the app's job_store."""
            captured["app"] = app
            captured.update(kwargs)
            # Close the eagerly-created JobStore to prevent ResourceWarning
            from fastapi import FastAPI  # noqa: PLC0415

            if isinstance(app, FastAPI):
                store: object = app.state.job_store
                if isinstance(store, JobStore):
                    store.close()

        monkeypatch.setattr("saneless.cli.uvicorn.run", mock_uvicorn_run)
        return captured

    def test_serve_calls_uvicorn_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Serve with no flags calls uvicorn.run with config defaults."""
        self._mock_socket(monkeypatch)
        captured = self._capture_uvicorn(monkeypatch)
        runner, _settings = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve"])
        assert result.exit_code == 0
        assert captured["host"] == "0.0.0.0"
        assert captured["port"] == 8080
        assert captured["log_config"] is None
        assert captured["access_log"] is True

    def test_serve_custom_host_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Serve --host/--port overrides config defaults."""
        captured = self._capture_uvicorn(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve", "--host", "127.0.0.1", "--port", "9090"])
        assert result.exit_code == 0
        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 9090

    def test_serve_log_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Serve passes log_level from settings to uvicorn."""
        self._mock_socket(monkeypatch)
        captured = self._capture_uvicorn(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve"])
        assert result.exit_code == 0
        assert captured["log_level"] == "info"

    def test_serve_prints_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Serve prints listening address to stdout."""
        self._mock_socket(monkeypatch)
        self._capture_uvicorn(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve"])
        assert result.exit_code == 0
        assert "Serving on http://0.0.0.0:8080" in result.output

    def test_serve_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Serve --help shows --host and --port options."""
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--host" in result.output
        assert "--port" in result.output

    def test_serve_receives_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Serve passes a FastAPI app (not None) to uvicorn.run."""
        self._mock_socket(monkeypatch)
        captured = self._capture_uvicorn(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve"])
        assert result.exit_code == 0
        assert captured["app"] is not None


class TestAutoProfiles:
    """auto-profiles command tests."""

    @staticmethod
    def _make_auto_scanner(
        *,
        devices: list[DeviceInfo] | None = None,
        caps: DeviceCapabilities | None = None,
    ) -> type:
        """Build a mock scanner class for auto-profiles tests."""
        _devices = (
            devices
            if devices is not None
            else [
                DeviceInfo(
                    name="test:device",
                    vendor="Test",
                    model="Scanner",
                    device_type="scanner",
                ),
            ]
        )
        _caps = (
            caps
            if caps is not None
            else DeviceCapabilities(
                sources=["Flatbed", "ADF"],
                resolutions=[150, 300, 600],
                modes=["Color", "Gray"],
            )
        )

        class _AutoScanner:
            """Mock scanner for auto-profiles tests."""

            def get_devices(self) -> list[DeviceInfo]:
                """Return configured device list."""
                return _devices

            def get_capabilities(self, _device_id: str) -> DeviceCapabilities:
                """Return configured capabilities."""
                return _caps

        return _AutoScanner

    def test_auto_profiles_generates_profiles(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """auto-profiles generates profiles and prints summary."""
        config_file = tmp_path / "saneless.toml"
        scanner_cls = self._make_auto_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)

        result = runner.invoke(cli, ["--config", str(config_file), "auto-profiles"])
        assert result.exit_code == 0
        assert "Generated" in result.output
        assert "flatbed-scan" in result.output
        assert "adf-simplex" in result.output

    def test_auto_profiles_no_scanners(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """auto-profiles with no scanners exits with code 1."""
        scanner_cls = self._make_auto_scanner(devices=[])
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)

        result = runner.invoke(cli, ["auto-profiles"])
        assert result.exit_code == 1
        assert "No scanners found" in result.output

    def test_auto_profiles_force_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """auto-profiles --force overwrites existing profiles."""
        config_file = tmp_path / "saneless.toml"
        # Pre-populate config with an existing flatbed-scan profile
        doc = tomlkit.document()
        profiles_table = tomlkit.table(is_super_table=True)
        existing = tomlkit.table()
        existing.add("source", "Old Source")
        existing.add("resolution", 150)
        existing.add("mode", "Gray")
        profiles_table["flatbed-scan"] = existing
        doc.add("profiles", profiles_table)
        config_file.write_text(tomlkit.dumps(doc))

        scanner_cls = self._make_auto_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)

        result = runner.invoke(
            cli, ["--config", str(config_file), "auto-profiles", "--force"]
        )
        assert result.exit_code == 0
        assert "Generated" in result.output
        assert "flatbed-scan" in result.output

    def test_auto_profiles_no_force_skips_existing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """auto-profiles without --force does not overwrite existing profiles."""
        config_file = tmp_path / "saneless.toml"
        # Pre-populate config with ALL profiles that would be generated
        doc = tomlkit.document()
        profiles_table = tomlkit.table(is_super_table=True)
        for name in ("default", "flatbed-scan", "adf-simplex"):
            entry = tomlkit.table()
            entry.add("source", "Existing")
            entry.add("resolution", 150)
            entry.add("mode", "Gray")
            profiles_table[name] = entry
        doc.add("profiles", profiles_table)
        config_file.write_text(tomlkit.dumps(doc))

        scanner_cls = self._make_auto_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)

        result = runner.invoke(cli, ["--config", str(config_file), "auto-profiles"])
        assert result.exit_code == 0
        assert "No new profiles written" in result.output
