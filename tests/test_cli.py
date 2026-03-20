"""Tests for CLI commands via click.testing.CliRunner."""

import json

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


def _make_settings(**overrides):
    """Create a Settings instance with test defaults."""
    defaults = {
        "scanner": ScannerConfig(device="test:device:001"),
        "paperless": PaperlessConfig(
            url="http://localhost:8000",
            token="test-token",
        ),
        "output": OutputConfig(
            tmp_dir="/tmp/saneless-test",
            log_file="/tmp/saneless-test/saneless.log",
        ),
        "profiles": {
            "default": ProfileConfig(),
            "photo": ProfileConfig(resolution=600, mode="color"),
        },
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _patch_cli(monkeypatch, settings=None, scanner_cls=None, paperless_cls=None):
    """Patch cli module dependencies for testing.

    Returns (runner, settings_used).
    """
    settings = settings or _make_settings()

    monkeypatch.setattr(
        "saneless.cli.load_settings",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr(
        "saneless.cli.configure_logging",
        lambda *args, **kwargs: None,
    )

    if scanner_cls is not None:
        monkeypatch.setattr("saneless.cli.SaneBackend", scanner_cls)
    else:
        # Default mock scanner
        class MockSaneBackend:
            def get_devices(self):
                return [
                    DeviceInfo("epson:001", "Epson", "ET-4850", "flatbed scanner"),
                    DeviceInfo("hp:002", "HP", "Envy 6055", "multi-function peripheral"),
                ]

            def get_capabilities(self, device_id):
                return DeviceCapabilities(
                    sources=["Flatbed", "ADF"],
                    resolutions=[150, 300, 600],
                    modes=["color", "gray"],
                    raw_options=[
                        (0, "source", "Source", "desc", 3, 0, 1, 0, ["Flatbed", "ADF"]),
                    ],
                )

            def scan_pages(self, device_id, settings):
                return iter([Image.new("RGB", (100, 100), "white")])

        monkeypatch.setattr("saneless.cli.SaneBackend", MockSaneBackend)

    if paperless_cls is not None:
        monkeypatch.setattr("saneless.cli.PaperlessClient", paperless_cls)
    else:
        class MockPaperlessClient:
            def __init__(self, *args, **kwargs):
                pass

            def upload_document(self, *args, **kwargs):
                return "mock-task-uuid"

            def poll_task(self, *args, **kwargs):
                return {"status": "SUCCESS"}

            def close(self):
                pass

        monkeypatch.setattr("saneless.cli.PaperlessClient", MockPaperlessClient)

    return CliRunner(mix_stderr=False), settings


class TestCliHelp:
    """CLI help text tests."""

    def test_cli_help(self, monkeypatch):
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "saneless" in result.output.lower() or "scan" in result.output.lower()

    def test_scan_command_help(self, monkeypatch):
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan", "--help"])
        assert result.exit_code == 0
        assert "--profile" in result.output
        assert "--title" in result.output

    def test_devices_command_help(self, monkeypatch):
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["devices", "--help"])
        assert result.exit_code == 0
        assert "--json" in result.output
        assert "--capabilities" in result.output


class TestScanCommand:
    """Scan command tests."""

    def test_scan_requires_title(self, monkeypatch):
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan"])
        assert result.exit_code != 0

    def test_scan_happy_path(self, monkeypatch):
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 0
        assert "Done: Test" in result.output

    def test_scan_status_output(self, monkeypatch):
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert "Scanning..." in result.output
        assert "Assembling PDF..." in result.output
        assert "Uploading to paperless-ngx..." in result.output

    def test_scan_config_error(self, monkeypatch):
        """Config loading fails -> exit code 2."""
        runner = CliRunner(mix_stderr=False)

        def bad_load(*args, **kwargs):
            msg = "bad config"
            raise ValueError(msg)

        monkeypatch.setattr("saneless.cli.load_settings", bad_load)
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *a, **kw: None)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 2
        assert "Configuration error" in result.stderr

    def test_scan_scan_error(self, monkeypatch):
        """Pipeline raises ScanError -> exit code 1."""

        class FailScanner:
            def scan_pages(self, *args, **kwargs):
                raise ScanError("Paper jam")

        runner, _ = _patch_cli(monkeypatch, scanner_cls=FailScanner)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 1
        assert "Paper jam" in result.stderr

    def test_scan_paperless_error(self, monkeypatch):
        """Pipeline raises PaperlessError -> exit code 3."""

        class FailPaperless:
            def __init__(self, *a, **kw):
                pass

            def upload_document(self, *a, **kw):
                raise PaperlessError("Server down")

            def close(self):
                pass

        runner, _ = _patch_cli(monkeypatch, paperless_cls=FailPaperless)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 3
        assert "Server down" in result.stderr

    def test_scan_with_profile(self, monkeypatch):
        """scan --profile photo -> pipeline called with profile_name='photo'."""
        captured = {}

        original_run_pipeline = None

        def capturing_pipeline(*args, **kwargs):
            captured["profile_name"] = kwargs.get("profile_name") or args[3]
            return {"status": "SUCCESS"}

        runner, _ = _patch_cli(monkeypatch)
        monkeypatch.setattr("saneless.cli.run_pipeline", capturing_pipeline)
        from saneless.cli import cli

        result = runner.invoke(cli, ["scan", "--profile", "photo", "--title", "Test"])
        assert result.exit_code == 0
        assert captured["profile_name"] == "photo"


class TestDevicesCommand:
    """Devices command tests."""

    def test_devices_table_output(self, monkeypatch):
        """devices -> table with device names, vendors, models."""
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["devices"])
        assert result.exit_code == 0
        assert "epson:001" in result.output
        assert "Epson" in result.output
        assert "ET-4850" in result.output
        assert "hp:002" in result.output

    def test_devices_json_output(self, monkeypatch):
        """devices --json -> valid JSON with device list."""
        runner, _ = _patch_cli(monkeypatch)
        from saneless.cli import cli

        result = runner.invoke(cli, ["devices", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["name"] == "epson:001"

    def test_devices_capabilities(self, monkeypatch):
        """devices --capabilities -> raw option names shown."""
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

        def capture_logging(*args, **kwargs):
            captured["verbose"] = kwargs.get("verbose", False)

        runner = CliRunner(mix_stderr=False)
        monkeypatch.setattr("saneless.cli.load_settings", lambda *a, **kw: _make_settings())
        monkeypatch.setattr("saneless.cli.configure_logging", capture_logging)

        class MockSaneBackend:
            def get_devices(self):
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
            captured["config_path"] = config_path
            return _make_settings()

        runner = CliRunner(mix_stderr=False)
        monkeypatch.setattr("saneless.cli.load_settings", capture_load)
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *a, **kw: None)

        class MockSaneBackend:
            def get_devices(self):
                return []

        monkeypatch.setattr("saneless.cli.SaneBackend", MockSaneBackend)
        from saneless.cli import cli

        result = runner.invoke(cli, ["--config", "/path/to/config.toml", "devices"])
        assert result.exit_code == 0
        assert captured["config_path"] == "/path/to/config.toml"
