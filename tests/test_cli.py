"""Tests for CLI commands via click.testing.CliRunner."""

import json
import tempfile
from pathlib import Path

from click.testing import CliRunner
from PIL import Image

from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.exceptions import PaperlessError, ScanError
from saneless.scanner.base import DeviceCapabilities, DeviceInfo

_TEST_TMP = str(Path(tempfile.gettempdir()) / "saneless-test")
_TEST_LOG = str(Path(tempfile.gettempdir()) / "saneless-test" / "saneless.log")


def _make_settings(**overrides):
    """Create a Settings instance with test defaults."""
    auth = "test-token"
    defaults = {
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


def _patch_cli(monkeypatch, settings=None, scanner_cls=None, paperless_cls=None):
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

            def get_devices(self):
                """Return two test scanner devices."""
                return [
                    DeviceInfo("epson:001", "Epson", "ET-4850", "flatbed scanner"),
                    DeviceInfo(
                        "hp:002", "HP", "Envy 6055", "multi-function peripheral"
                    ),
                ]

            def get_capabilities(self, _device_id):
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

            def scan_pages(self, _device_id, _settings):
                """Return a single white test image."""
                return iter([Image.new("RGB", (100, 100), "white")])

        monkeypatch.setattr("saneless.cli.SaneBackend", MockSaneBackend)

    if paperless_cls is not None:
        monkeypatch.setattr("saneless.cli.PaperlessClient", paperless_cls)
    else:

        class MockPaperlessClient:
            """Mock paperless client for CLI tests."""

            def __init__(self, *_args, **_kwargs):
                """Accept and ignore all constructor arguments."""

            def upload_document(self, *_args, **_kwargs):
                """Return a fake task UUID."""
                return "mock-task-uuid"

            def poll_task(self, *_args, **_kwargs):
                """Return a successful task result."""
                return {"status": "SUCCESS"}

            def close(self):
                """No-op close."""

        monkeypatch.setattr("saneless.cli.PaperlessClient", MockPaperlessClient)

    return CliRunner(), settings


class TestCliHelp:
    """CLI help text tests."""

    def test_cli_help(self, monkeypatch):
        """Main CLI --help shows usage information."""
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "saneless" in result.output.lower() or "scan" in result.output.lower()

    def test_scan_command_help(self, monkeypatch):
        """Scan subcommand --help shows --profile and --title options."""
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan", "--help"])
        assert result.exit_code == 0
        assert "--profile" in result.output
        assert "--title" in result.output

    def test_devices_command_help(self, monkeypatch):
        """Devices subcommand --help shows --json and --capabilities options."""
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["devices", "--help"])
        assert result.exit_code == 0
        assert "--json" in result.output
        assert "--capabilities" in result.output


class TestScanCommand:
    """Scan command tests."""

    def test_scan_requires_title(self, monkeypatch):
        """Scan without --title exits with non-zero code."""
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan"])
        assert result.exit_code != 0

    def test_scan_happy_path(self, monkeypatch):
        """Scan with --title succeeds and shows Done message."""
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 0
        assert "Done: Test" in result.output

    def test_scan_status_output(self, monkeypatch):
        """Scan shows progress messages during pipeline execution."""
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert "Scanning..." in result.output
        assert "Assembling PDF..." in result.output
        assert "Uploading to paperless-ngx..." in result.output

    def test_scan_config_error(self, monkeypatch):
        """Config loading fails -> exit code 2."""
        runner = CliRunner()

        def bad_load(*_args, **_kwargs):
            msg = "bad config"
            raise ValueError(msg)

        monkeypatch.setattr("saneless.cli.load_settings", bad_load)
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *_a, **_kw: None)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 2
        assert "Configuration error" in result.output

    def test_scan_scan_error(self, monkeypatch):
        """Pipeline raises ScanError -> exit code 1."""

        class FailScanner:
            """Scanner that always raises ScanError."""

            def scan_pages(self, *_args, **_kwargs):
                """Raise a scan error."""
                msg = "Paper jam"
                raise ScanError(msg)

        runner, _ = _patch_cli(monkeypatch, scanner_cls=FailScanner)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 1
        assert "Paper jam" in result.output

    def test_scan_paperless_error(self, monkeypatch):
        """Pipeline raises PaperlessError -> exit code 3."""

        class FailPaperless:
            """Paperless client that always raises PaperlessError on upload."""

            def __init__(self, *_a, **_kw):
                """Accept and ignore all constructor arguments."""

            def upload_document(self, *_a, **_kw):
                """Raise a paperless error."""
                msg = "Server down"
                raise PaperlessError(msg)

            def close(self):
                """No-op close."""

        runner, _ = _patch_cli(monkeypatch, paperless_cls=FailPaperless)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 3
        assert "Server down" in result.output

    def test_scan_with_profile(self, monkeypatch):
        """Scan --profile photo -> pipeline called with profile_name='photo'."""
        captured = {}

        def capturing_pipeline(*args, **_kwargs):
            """Capture the PipelineRequest from the 4th positional arg."""
            captured["request"] = args[3]
            return {"status": "SUCCESS"}

        runner, _ = _patch_cli(monkeypatch)
        monkeypatch.setattr("saneless.cli.run_pipeline", capturing_pipeline)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan", "--profile", "photo", "--title", "Test"])
        assert result.exit_code == 0
        assert captured["request"].profile_name == "photo"


class TestDevicesCommand:
    """Devices command tests."""

    def test_devices_table_output(self, monkeypatch):
        """Devices -> table with device names, vendors, models."""
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["devices"])
        assert result.exit_code == 0
        assert "epson:001" in result.output
        assert "Epson" in result.output
        assert "ET-4850" in result.output
        assert "hp:002" in result.output

    def test_devices_json_output(self, monkeypatch):
        """Devices --json -> valid JSON with device list."""
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["devices", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["name"] == "epson:001"

    def test_devices_capabilities(self, monkeypatch):
        """Devices --capabilities -> raw option names shown."""
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["devices", "--capabilities"])
        assert result.exit_code == 0
        assert "Flatbed" in result.output
        assert "ADF" in result.output


class TestCliFlags:
    """CLI flag tests."""

    def test_verbose_flag(self, monkeypatch):
        """The -v flag calls configure_logging with verbose=True."""
        captured = {}

        def capture_logging(*_args, **kwargs):
            """Record whether verbose was passed."""
            captured["verbose"] = kwargs.get("verbose", False)

        runner = CliRunner()
        monkeypatch.setattr(
            "saneless.cli.load_settings", lambda *_a, **_kw: _make_settings()
        )
        monkeypatch.setattr("saneless.cli.configure_logging", capture_logging)

        class MockSaneBackend:
            """Mock scanner that returns no devices."""

            def get_devices(self):
                """Return empty device list."""
                return []

        monkeypatch.setattr("saneless.cli.SaneBackend", MockSaneBackend)
        from saneless.cli import cli

        result = runner.invoke(cli, ["-v", "devices"])
        assert result.exit_code == 0
        assert captured.get("verbose") is True

    def test_config_flag(self, monkeypatch):
        """--config /path/to/config -> load_settings called with that path."""
        captured = {}

        def capture_load(config_path=None):
            """Record the config_path argument."""
            captured["config_path"] = config_path
            return _make_settings()

        runner = CliRunner()
        monkeypatch.setattr("saneless.cli.load_settings", capture_load)
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *_a, **_kw: None)

        class MockSaneBackend:
            """Mock scanner that returns no devices."""

            def get_devices(self):
                """Return empty device list."""
                return []

        monkeypatch.setattr("saneless.cli.SaneBackend", MockSaneBackend)
        from saneless.cli import cli

        result = runner.invoke(cli, ["--config", "/path/to/config.toml", "devices"])
        assert result.exit_code == 0
        assert captured["config_path"] == "/path/to/config.toml"
