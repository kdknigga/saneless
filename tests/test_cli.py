"""The saneless commands' output, exit codes and side effects, via CliRunner."""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import importlib.metadata
import io
import json
import logging
import logging.handlers
import os
import re
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, NoReturn

import click
import pytest
import tomlkit
import uvicorn
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

import saneless.checks as checks_module
import saneless.cli as cli_module
import saneless.config as config_module
import saneless.job as job_module
import saneless.vocabulary as vocabulary_module
from saneless.checks import (
    CheckKey,
    CheckResult,
    CheckState,
)
from saneless.cli import ClickFlipCoordinator, _failure_line, _truncate, cli
from saneless.config import (
    CONFIG_FILENAME,
    LEGACY_CONFIG_FILENAME,
    ConfigDiscovery,
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
    discover_config,
    load_settings,
)
from saneless.exceptions import (
    AllPagesBlankError,
    ConfigError,
    FeederEmptyError,
    NoScannerFoundError,
    PaperlessError,
    PaperlessTrustStoreError,
    PdfError,
    SanelessError,
    ScanCancelledError,
    ScanError,
    ScanInterrupted,
    StorageError,
)
from saneless.job import Job, JobResult, JobStore
from saneless.logging_config import configure_logging
from saneless.paperless import (
    PROBE_READ_SECONDS,
    ApiDelivery,
    TaskFiled,
    UploadResult,
)
from saneless.pipeline import PipelineEvent, PipelineRequest, ScanResult
from saneless.scanner import sane_backend
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScanBatch,
    ScannerBackend,
)
from saneless.scanner.saned_probe import PROBE_CONNECT_SECONDS
from saneless.vocabulary import (
    MULTI_PAGE_NEEDS_TERMINAL,
    UNCONFIRMED_FILING_LABEL,
    UNCONFIRMED_SEND_LABEL,
    WARNED_UPLOAD_LABEL,
    ErrorCategory,
    ExitCode,
    FlipOutcome,
    JobState,
    ScanOutcome,
    error_next_step,
    exit_code_for,
    job_label,
    local_time,
    multi_page_manual_duplex_refusal,
    state_label,
)
from saneless.web import app as app_module
from saneless.web import server as server_module
from saneless.web.app import create_app
from tests.conftest import (
    StubScannerBackend,
    build_settings,
    leave_killed_workspace,
    poll_until,
    scan_batch,
    services_of,
)
from tests.fake_sane import UNNAMED_OPTION_ENTRIES, FakeSaneDev, FakeSaneModule
from tests.prompt_support import (
    FakeClock,
    broken_read,
    never_readable,
    readable,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Generator

    from click.testing import Result

    from saneless.checks import CheckContext
    from saneless.config import LogLevel
    from saneless.scanner.base import PageSink, ScanSettings
    from saneless.web.refresher import CheckRefresher
    from saneless.workspace import RecoveredWorkspace


def _inked_page() -> Image.Image:
    """
    Draw a page with enough ink that the real empty-page filter keeps it.

    Returns:
        A clearly non-blank RGB image.

    """
    page = Image.new("RGB", (100, 100), "white")
    ImageDraw.Draw(page).rectangle([10, 10, 90, 90], fill="black")
    return page


# A warning sentence as the pipeline writes one for a skipped sheet: the row a
# warned upload leaves behind carries it next to state DONE.
_WARNED_SENTENCE = (
    "1 page(s) could not be read by the scanner and were skipped. "
    "They were not removed for being blank; rescan those sheets."
)


def _clean_scan_result() -> ScanResult:
    """
    Build the result of a clean one-page upload, as a faked pipeline returns.

    ``saneless scan`` chooses its outcome line and exit code from what the
    pipeline returns, so a fake that stands in for it has to return a result.

    Returns:
        A SUCCESS result with no warning.

    """
    return ScanResult(
        outcome=ScanOutcome.SUCCESS,
        pages_scanned=1,
        pages_removed=0,
        pages_uploaded=1,
        warning=None,
    )


def _make_settings(tmp_path: Path, **overrides: object) -> Settings:
    """
    Build the suite's test settings plus a ``photo`` profile.

    Args:
        tmp_path: The test's own temporary directory, for every path setting.
        **overrides: Whole sections to use instead of the defaults.

    Returns:
        A fresh Settings instance.

    """
    profiles = {
        "default": ProfileConfig(),
        "photo": ProfileConfig(resolution=600, mode="color"),
    }
    return build_settings(tmp_path, **({"profiles": profiles} | overrides))


def _patch_cli(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings | None = None,
    scanner_cls: type | None = None,
    paperless_cls: type | None = None,
) -> tuple[CliRunner, Settings]:
    """
    Patch cli module dependencies for testing.

    Without ``settings`` the defaults are built on the working directory, which
    the suite's autouse ``hermetic_env`` makes the test's own ``tmp_path``.

    Returns (runner, settings_used).
    """
    settings = settings or _make_settings(Path.cwd())

    def _fake_load_settings(config_path: str | None = None) -> Settings:
        """
        Return the test settings, recording ``--config`` as load_settings does.

        ``auto-profiles`` writes to ``settings.config_path``; a stub that
        dropped the path would send its writes to ``./saneless.toml`` in the
        suite's working directory.

        The search itself is recorded too, because the stale-file warning and
        ``auto-profiles``'s refusal read the recording rather than the path.  A
        recording a test attached to these settings stands, and the path is
        derived from it, which is the direction the real loader derives them
        in.
        """
        if config_path:
            explicit = Path(config_path)
            settings._config_path = explicit
            settings._config_discovery = ConfigDiscovery(
                explicit=explicit,
                searched=(),
                found=(explicit,),
                loaded=explicit,
                stale=(),
            )
            return settings
        injected = settings.config_discovery
        settings._config_path = None if injected is None else injected.loaded
        return settings

    def _no_sane_check() -> None:
        """Stand in for require_sane, so no CLI test imports the real python-sane."""

    monkeypatch.setattr("saneless.cli.load_settings", _fake_load_settings)
    monkeypatch.setattr(
        "saneless.cli.configure_logging",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("saneless.cli.require_sane", _no_sane_check)

    if scanner_cls is not None:
        monkeypatch.setattr("saneless.cli.SaneBackend", scanner_cls)
    else:

        class MockSaneBackend(StubScannerBackend):
            """
            Mock scanner backend for CLI tests.

            It subclasses the shared conftest stub, which subclasses the ABC, so the
            type checkers hold it to the backend contract.

            Only the two device-reporting methods are local, because these
            tests read the richer device list and capabilities back out of the
            CLI's own output. ``scan_pages`` is the base's: one inked page,
            spooled through the caller's sink.
            """

            def __init__(self, host: str = "") -> None:
                """Accept host parameter for API compatibility."""

            def get_devices(self) -> list[DeviceInfo]:
                """Return two test scanner devices."""
                return [
                    DeviceInfo("epson:001", "Epson", "ET-4850", "flatbed scanner"),
                    DeviceInfo(
                        "hp:002", "HP", "Envy 6055", "multi-function peripheral"
                    ),
                ]

            def get_capabilities(self, device_id: str) -> DeviceCapabilities:
                """Return fixed test capabilities."""
                return DeviceCapabilities(
                    sources=["Flatbed", "ADF"],
                    resolutions=[150, 300, 600],
                    modes=["color", "gray"],
                    option_names=("source",),
                )

        monkeypatch.setattr("saneless.cli.SaneBackend", MockSaneBackend)

    if paperless_cls is not None:
        monkeypatch.setattr("saneless.cli.PaperlessClient", paperless_cls)
    else:

        class MockPaperlessClient:
            """Mock paperless client for CLI tests."""

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                """Accept and ignore all constructor arguments."""

            def upload_document(
                self, *_args: object, **_kwargs: object
            ) -> UploadResult:
                """Return a delivered upload result carrying a fake task UUID."""
                return ApiDelivery(task_id="mock-task-uuid")

            def poll_task(self, *_args: object, **_kwargs: object) -> TaskFiled:
                """Return a successful task result."""
                return TaskFiled(task={"status": "SUCCESS"})

            def close(self) -> None:
                """No-op close."""

        monkeypatch.setattr("saneless.cli.PaperlessClient", MockPaperlessClient)

    return CliRunner(), settings


def _failure_lines(result: Result) -> list[str]:
    """
    Return a classified failure's stderr with the advice line removed.

    Every ``SanelessError`` the guard classifies to a code other than
    ``UNEXPECTED`` prints two things: the failure line and the category's
    ``Try: `` next step. The tests below each mean "one failure line and no
    traceback", so the advice line is *asserted* and stripped in one place
    rather than each assertion being loosened to a substring check.

    Args:
        result: The CliRunner result of a command that failed.

    Returns:
        The stderr lines before the advice line.

    """
    lines = result.stderr.splitlines()
    assert lines[-1].startswith("Try: "), result.stderr
    return lines[:-1]


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


class TestVersionOption:
    """
    ``saneless --version`` reports the installed distribution version.

    The number comes from ``importlib.metadata``, so ``pyproject.toml`` stays
    its single source, and the line is click's default
    ``%(prog)s, version %(version)s`` with no Python or platform block --
    ``saneless doctor`` already covers the diagnostics such a block would
    duplicate.
    """

    def test_version_option_prints_the_installed_version(self) -> None:
        """
        ``--version`` exits 0 printing ``saneless, version <installed>``.

        The expectation is read from ``importlib.metadata``, never from
        parsing ``pyproject.toml``. A pyproject-derived expectation fails in
        every venv whose installed metadata lags an edited ``pyproject.toml``
        -- which is every venv between the edit and the next ``uv sync`` --
        and it fails for a reason that has nothing to do with this option.
        ``prog_name="saneless"`` is equally mandatory: ``CliRunner`` otherwise
        reports the program as ``cli``, and the assertion then fails on the
        wrong half of the line.
        """
        installed = importlib.metadata.version("saneless")

        result = CliRunner().invoke(cli, ["--version"], prog_name="saneless")

        assert result.exit_code == 0, result.output
        assert result.output == f"saneless, version {installed}\n"

    def test_version_option_needs_no_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--version`` loads no settings and configures no logging."""
        calls: list[str] = []

        def failing_load(*_args: object, **_kwargs: object) -> Settings:
            """Fail as loading a nonexistent config would."""
            calls.append("load_settings")
            msg = "Configuration error in /nonexistent/saneless.toml:\n  missing"
            raise ConfigError(msg)

        def recording_logging(*_args: object, **_kwargs: object) -> None:
            """Record that logging configuration was attempted."""
            calls.append("configure_logging")

        monkeypatch.setattr("saneless.cli.load_settings", failing_load)
        monkeypatch.setattr("saneless.cli.configure_logging", recording_logging)

        result = CliRunner().invoke(
            cli,
            ["--config", "/nonexistent/saneless.toml", "--version"],
            prog_name="saneless",
        )

        assert result.exit_code == 0, result.output
        assert calls == [], f"--version went through {calls}"

    def test_verbose_short_flag_is_still_verbose(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``-v`` still means ``--verbose``, not ``--version``.

        The version option is deliberately declared without a short flag:
        ``-v`` is already ``--verbose`` on the same group, and click binds the
        short flag to whichever decorator declared it last, silently.
        """
        captured: dict[str, object] = {}

        def capture_logging(*_args: object, **kwargs: object) -> None:
            """Record whether verbose was passed."""
            captured["verbose"] = kwargs.get("verbose", False)

        runner, _ = _patch_cli(monkeypatch)
        monkeypatch.setattr("saneless.cli.configure_logging", capture_logging)

        result = runner.invoke(cli, ["-v", "devices"], prog_name="saneless")

        assert result.exit_code == 0, result.output
        assert captured.get("verbose") is True, "-v no longer reaches configure_logging"
        assert "saneless, version" not in result.output


class TestScanRecoversOrphanedWorkspaces:
    """
    ``saneless scan`` first recovers what a killed scan left in ``tmp_dir``.

    A CLI-only install has no server whose startup would do it, so each scan
    sweeps before it opens the scanner. The sweep is housekeeping: it never
    decides whether the scan the operator asked for runs.
    """

    def test_an_orphan_is_recovered_into_failed_before_the_scan(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The killed scan's pages become a PDF, then the new scan runs."""
        runner, settings = _patch_cli(monkeypatch)
        settings.output.tmp_dir.mkdir(mode=0o700)
        workspace = leave_killed_workspace(
            settings.output.tmp_dir, job_id="deadbeef-cli", title="Killed CLI scan"
        )
        order: list[str] = []
        original_sweep = cli_module.sweep_orphans
        original_backend = cli_module.SaneBackend

        def recording_sweep(
            tmp_dir: Path, failed_dir: Path, reserve_mb: int
        ) -> list[RecoveredWorkspace]:
            order.append("sweep")
            return original_sweep(tmp_dir, failed_dir, reserve_mb)

        def recording_backend(host: str = "") -> ScannerBackend:
            order.append("scanner")
            return original_backend(host=host)

        monkeypatch.setattr(cli_module, "sweep_orphans", recording_sweep)
        monkeypatch.setattr(cli_module, "SaneBackend", recording_backend)
        caplog.set_level(logging.WARNING, logger="saneless.workspace")

        result = runner.invoke(cli, ["scan", "--title", "Test"])

        assert result.exit_code == 0, result.output
        assert "Done: Test" in result.output
        assert order == ["sweep", "scanner"]
        assert not workspace.exists()
        (pdf,) = sorted(settings.output.failed_dir.glob("*.pdf"))
        assert pdf.name.endswith("-partial.pdf")
        assert any(
            "deadbeef-cli" in record.getMessage() and str(pdf) in record.getMessage()
            for record in caplog.records
            if record.name == "saneless.workspace"
        )

    def test_a_failing_sweep_never_fails_the_scan(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A sweep that raises is logged with its traceback; the scan exits 0."""
        runner, _ = _patch_cli(monkeypatch)

        def failing_sweep(
            tmp_dir: Path, failed_dir: Path, reserve_mb: int
        ) -> list[RecoveredWorkspace]:
            msg = f"could not read {tmp_dir} ({failed_dir}, {reserve_mb} MB)"
            raise OSError(msg)

        monkeypatch.setattr(cli_module, "sweep_orphans", failing_sweep)
        caplog.set_level(logging.WARNING, logger="saneless.cli")

        result = runner.invoke(cli, ["scan", "--title", "Test"])

        assert result.exit_code == 0, result.output
        assert "Done: Test" in result.output
        assert any(
            record.name == "saneless.cli"
            and record.levelno == logging.WARNING
            and record.exc_info is not None
            and "orphaned" in record.getMessage()
            for record in caplog.records
        )


class TestScanCommand:
    """Scan command tests."""

    def test_scan_without_title_falls_back_to_timestamp_title(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--title`` is optional; with no profile title it is ``Scan <time>``."""
        runner, _ = _patch_cli(monkeypatch)
        result = runner.invoke(cli, ["scan"])
        assert result.exit_code == 0, result.output
        assert re.search(r"Done: Scan \d{4}-\d{2}-\d{2} \d{2}:\d{2}", result.output)

    @staticmethod
    def _capture_title_run(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: list[str]
    ) -> tuple[Result, PipelineRequest | None]:
        """
        Run ``scan`` against a ``receipt`` profile titled "Receipt".

        ``run_pipeline`` is replaced by a recorder that captures the request and
        reports DONE through its callback, as the real pipeline does.

        Args:
            monkeypatch: The test's monkeypatch fixture.
            tmp_path: The test's own temporary directory.
            args: The CLI arguments.

        Returns:
            The CliRunner result and the captured request, if the pipeline ran.

        """
        settings = _make_settings(
            tmp_path,
            profiles={
                "default": ProfileConfig(),
                "receipt": ProfileConfig(title="Receipt"),
            },
        )
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        captured: list[PipelineRequest] = []

        def capturing_pipeline(
            _scanner: object,
            _paperless: object,
            _settings: object,
            request: PipelineRequest,
        ) -> ScanResult:
            captured.append(request)
            if request.status_callback is not None:
                request.status_callback(PipelineEvent.DONE)
            return _clean_scan_result()

        monkeypatch.setattr("saneless.cli.run_pipeline", capturing_pipeline)
        result = runner.invoke(cli, args)
        return result, (captured[0] if captured else None)

    @pytest.mark.parametrize("typed", [None, "", "   "])
    def test_scan_blank_title_uses_the_profile_title(
        self, monkeypatch: pytest.MonkeyPatch, typed: str | None, tmp_path: Path
    ) -> None:
        """An omitted or blank ``--title`` resolves to the profile's title."""
        args = ["scan", "--profile", "receipt"]
        if typed is not None:
            args += ["--title", typed]

        result, request = self._capture_title_run(monkeypatch, tmp_path, args)

        assert result.exit_code == 0, result.output
        assert request is not None
        assert request.title == "Receipt"
        assert "Done: Receipt" in result.output

    def test_scan_typed_title_beats_the_profile_title(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A non-blank ``--title`` is kept as typed."""
        result, request = self._capture_title_run(
            monkeypatch, tmp_path, ["scan", "--profile", "receipt", "--title", "Typed"]
        )

        assert result.exit_code == 0, result.output
        assert request is not None
        assert request.title == "Typed"
        assert "Done: Typed" in result.output

    def test_scan_files_the_document_with_the_profile_metadata(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The command line asks nobody, so the profile's defaults are the answer.

        It has no tag or correspondent option, which is exactly an untouched
        control: the profile's tags and correspondent apply, as they do for a
        web form nobody changed.
        """
        settings = _make_settings(
            tmp_path,
            profiles={
                "default": ProfileConfig(),
                "receipts": ProfileConfig.model_validate(
                    {"default_tags": [3, 7], "default_correspondent": 12}
                ),
            },
        )
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        captured: list[PipelineRequest] = []

        def capturing_pipeline(
            _scanner: object,
            _paperless: object,
            _settings: object,
            request: PipelineRequest,
        ) -> ScanResult:
            captured.append(request)
            return _clean_scan_result()

        monkeypatch.setattr("saneless.cli.run_pipeline", capturing_pipeline)
        result = runner.invoke(cli, ["scan", "--profile", "receipts"])

        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        assert captured[0].tags == [3, 7]
        assert captured[0].correspondent == 12

    def test_scan_unknown_profile_exits_2_before_title_resolution(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An unknown profile is refused with exit 2 and no pipeline run."""
        result, request = self._capture_title_run(
            monkeypatch, tmp_path, ["scan", "--profile", "nope"]
        )

        assert result.exit_code == 2
        assert "Unknown profile" in result.output
        assert request is None

    def test_scan_title_longer_than_paperless_keeps_is_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A 119-character ``--title`` exits 2 naming the option and 118.

        It is refused before the scanner exists, so no paper moves, and the
        title is never cut to fit.
        """
        constructed: list[str] = []

        class RecordingScanner(StubScannerBackend):
            """Record that the CLI built a scanner at all."""

            def __init__(self, host: str = "") -> None:
                """Note the construction."""
                constructed.append(host)

        runner, _ = _patch_cli(monkeypatch, scanner_cls=RecordingScanner)

        result = runner.invoke(cli, ["scan", "--title", "x" * 119])

        assert result.exit_code == 2, result.output
        assert "--title" in result.output
        assert "118 characters or fewer" in result.output
        assert constructed == []

    def test_scan_title_of_what_paperless_keeps_runs_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A 118-character ``--title`` reaches the pipeline exactly as typed."""
        title = "x" * 118
        result, request = self._capture_title_run(
            monkeypatch, tmp_path, ["scan", "--title", title]
        )

        assert result.exit_code == 0, result.output
        assert request is not None
        assert request.title == title

    def test_scan_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scan with --title succeeds and shows Done message."""
        runner, _ = _patch_cli(monkeypatch)
        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 0
        assert "Done: Test" in result.output
        assert "Removed as blank" not in result.output

    def test_scan_names_the_pages_removed_as_blank(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Pages 2 and 4 of four are blank: named on stdout after Done, and exit 0.

        Removed pages are not kept anywhere, so this line is how the operator
        learns which sheets to rescan if a real page was taken for a blank.
        It is information, not a warning: nothing reaches stderr and the
        command still exits 0.
        """

        class BlankBacksScanner(StubScannerBackend):
            """A feeder whose second and fourth sheets have nothing on them."""

            def __init__(self, host: str = "") -> None:
                """Accept host parameter for API compatibility."""

            def scan_pages(
                self, device_id: str, settings: ScanSettings, sink: PageSink
            ) -> ScanBatch:
                """Spool inked, blank, inked, blank."""
                blank = Image.new("RGB", (100, 100), "white")
                records = [
                    sink.add(page, dpi=settings.resolution)
                    for page in (_inked_page(), blank, _inked_page(), blank)
                ]
                return scan_batch(records, resolution=settings.resolution)

        runner, _ = _patch_cli(monkeypatch, scanner_cls=BlankBacksScanner)

        result = runner.invoke(cli, ["scan", "--title", "Test"])

        assert result.exit_code == 0, result.output
        lines = result.stdout.splitlines()
        done = lines.index("Done: Test")
        assert lines[done + 1] == "Removed as blank: pages 2, 4 of 4 scanned."
        assert "Removed as blank" not in result.stderr

    def test_scan_status_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scan shows its progress messages in pipeline order."""
        runner, _ = _patch_cli(monkeypatch)
        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 0, result.output
        scanning = result.output.index("Scanning...")
        assembling = result.output.index("Assembling PDF...")
        uploading = result.output.index("Uploading to paperless-ngx...")
        assert scanning < assembling < uploading

    def test_scan_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config loading raises ConfigError -> exit code 2, message as rendered."""
        runner = CliRunner()

        def bad_load(*_args: object, **_kwargs: object) -> None:
            msg = "Configuration error in /nope.toml:\n  [output] bad config"
            raise ConfigError(msg)

        monkeypatch.setattr("saneless.cli.load_settings", bad_load)
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *_a, **_kw: None)

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 2
        assert result.stderr.startswith("Configuration error in /nope.toml:")

    def test_scan_config_value_error_exits_5(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare ValueError while loading is not a saneless type -> exit 5."""
        runner = CliRunner()

        def bad_load(*_args: object, **_kwargs: object) -> None:
            msg = "bad config"
            raise ValueError(msg)

        monkeypatch.setattr("saneless.cli.load_settings", bad_load)
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *_a, **_kw: None)

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 5
        assert result.stderr.startswith("Unexpected error (ValueError): bad config")
        assert "Configuration error" not in result.output

    def test_scan_scan_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pipeline raises ScanError -> exit code 1."""

        class FailScanner(StubScannerBackend):
            """
            Scanner that always raises ScanError.

            It subclasses the shared conftest stub, so ``scan_pages`` is checked
            against the ABC's ``ScanBatch`` return type even though its body only
            raises. ``get_devices`` comes from that stub and answers ``[]``.
            """

            def __init__(self, host: str = "") -> None:
                """Accept host parameter for API compatibility."""

            def get_capabilities(self, device_id: str) -> DeviceCapabilities:
                """Unused here: the pipeline fails before capabilities load."""
                return DeviceCapabilities(sources=[], resolutions=[], modes=[])

            def scan_pages(
                self, device_id: str, settings: ScanSettings, sink: PageSink
            ) -> ScanBatch:
                """Raise a scan error, spooling nothing."""
                msg = "Paper jam"
                raise ScanError(msg)

        runner, _ = _patch_cli(monkeypatch, scanner_cls=FailScanner)

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 1
        assert "Paper jam" in result.output

    def test_scan_paperless_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Pipeline raises PaperlessError -> exit code 3.

        The failed upload preserves the assembled PDF, into this test's own
        ``data_dir``.
        """

        class FailPaperless:
            """Paperless client that always raises PaperlessError on upload."""

            def __init__(self, *_a: object, **_kw: object) -> None:
                """Accept and ignore all constructor arguments."""

            def upload_document(self, *_a: object, **_kw: object) -> UploadResult:
                """Raise a paperless error."""
                msg = "Server down"
                raise PaperlessError(msg)

            def close(self) -> None:
                """No-op close."""

        settings = _make_settings(tmp_path)
        runner, _ = _patch_cli(
            monkeypatch, settings=settings, paperless_cls=FailPaperless
        )

        result = runner.invoke(cli, ["scan", "--title", "Test"])
        assert result.exit_code == 3
        assert "Server down" in result.output
        # The scan is not lost when the upload is, and this pins
        # where it went: this test's own directory, not the shared one.
        preserved = sorted(settings.output.failed_dir.glob("*.pdf"))
        assert len(preserved) == 1
        assert str(preserved[0]) in result.output

    def test_scan_with_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scan --profile photo -> pipeline called with profile_name='photo'."""
        captured: dict[str, object] = {}

        def capturing_pipeline(*args: object, **_kwargs: object) -> ScanResult:
            """Capture the PipelineRequest from the 4th positional arg."""
            captured["request"] = args[3]
            return _clean_scan_result()

        runner, _ = _patch_cli(monkeypatch)
        monkeypatch.setattr("saneless.cli.run_pipeline", capturing_pipeline)

        result = runner.invoke(cli, ["scan", "--profile", "photo", "--title", "Test"])
        assert result.exit_code == 0
        request = captured["request"]
        assert hasattr(request, "profile_name")
        assert request.profile_name == "photo"

    def test_two_cli_scans_of_one_title_preserve_as_two_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The CLI mints a job id, so a same-second rescan cannot overwrite.

        ``build_pdf_filename`` takes uniqueness from the job id, not from the
        timestamp, and preservation writes to an explicit destination path that
        overwrites silently. Without a job id two scans of one title in the same
        second would land on one file.

        The two runs are driven back to back in one second, and the assertion is
        on the *files* rather than on the request's ``job_id``, so it tests the
        guarantee rather than the implementation.
        """

        class FailPaperless:
            """Paperless client that always refuses, so both scans preserve."""

            def __init__(self, *_a: object, **_kw: object) -> None:
                """Accept and ignore all constructor arguments."""

            def upload_document(self, *_a: object, **_kw: object) -> UploadResult:
                """Raise a paperless error."""
                msg = "Server down"
                raise PaperlessError(msg)

            def close(self) -> None:
                """No-op close."""

        settings = _make_settings(tmp_path)
        runner, _ = _patch_cli(
            monkeypatch, settings=settings, paperless_cls=FailPaperless
        )
        stamps: list[str] = []

        for _ in range(2):
            result = runner.invoke(cli, ["scan", "--title", "Same Title"])
            assert result.exit_code == 3
            stamps.append(datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S"))

        preserved = sorted(settings.output.failed_dir.glob("*.pdf"))
        assert len(preserved) == 2
        assert preserved[0].name != preserved[1].name
        # Only meaningful if the two really did land in the same second: if
        # they did not, the timestamp alone would have separated them and this
        # test would pass without exercising the job id at all.
        assert stamps[0] == stamps[1]


_DUPLEX_PROFILE = "duplex"

# A distinctive fragment of the CLI's flip prompt, asserted rather than the whole
# sentence so a rewording of the tail does not break the ordering checks.
_FLIP_PROMPT_FRAGMENT = "Flip the stack"

# The one line an operator's abort at the flip prompt prints.
_FLIP_CANCEL_LINE = "Manual duplex scan cancelled at the flip prompt"


def _duplex_settings(
    tmp_path: Path, operator_wait_timeout_seconds: int = 600
) -> Settings:
    """
    Build settings with a manual-duplex profile, writing only under ``tmp_path``.

    ``_make_settings`` replaces a whole section on override, so the three path
    fields are re-supplied here -- leaving them out would send the run into the
    developer's real state directory.

    Args:
        tmp_path: The pytest temporary directory for tmp, data and log files.
        operator_wait_timeout_seconds: The flip-wait bound, in seconds.

    Returns:
        Settings whose ``duplex`` profile is manual duplex on a feeder source.

    """
    return _make_settings(
        tmp_path,
        output=OutputConfig(
            tmp_dir=str(tmp_path),
            data_dir=str(tmp_path),
            log_file=str(tmp_path / "saneless.log"),
            operator_wait_timeout_seconds=operator_wait_timeout_seconds,
        ),
        profiles={
            "default": ProfileConfig(),
            _DUPLEX_PROFILE: ProfileConfig(source="ADF", duplex="manual"),
        },
    )


def _counting_scanner(calls: list[str]) -> type[ScannerBackend]:
    """
    Build a scanner backend class that records every ``scan_pages`` call.

    A class rather than an instance because ``cli.scan`` constructs the backend
    itself; the list is closed over so the test can read the count afterwards.

    Args:
        calls: Receives one device id per ``scan_pages`` call.

    Returns:
        A ``ScannerBackend`` subclass accepting ``host`` like ``SaneBackend``.

    """

    class CountingScanner(StubScannerBackend):
        """Scanner reporting a feeder, spooling one inked page per pass."""

        def __init__(self, host: str = "") -> None:
            """Accept host parameter for API compatibility."""

        def get_devices(self) -> list[DeviceInfo]:
            """Return one feeder-equipped device."""
            return [DeviceInfo("test:device:001", "Test", "Feeder", "scanner")]

        def get_capabilities(self, device_id: str) -> DeviceCapabilities:
            """Report a flatbed and a real feeder source."""
            return DeviceCapabilities(
                sources=["Flatbed", "ADF"],
                resolutions=[300],
                modes=["color"],
            )

        def scan_pages(
            self, device_id: str, settings: ScanSettings, sink: PageSink
        ) -> ScanBatch:
            """
            Record the call, then spool a single page with content.

            Args:
                device_id: Recorded, one entry per call, in call order.
                settings: Only ``resolution`` is used, as the page's dpi and the batch's.
                sink: The pipeline's own sink, which receives the page.

            Returns:
                A batch of the single record the sink returned.

            """
            calls.append(device_id)
            record = sink.add(_inked_page(), dpi=settings.resolution)
            return scan_batch([record], resolution=settings.resolution)

    return CountingScanner


def _recording_paperless(uploads: list[str]) -> type:
    """
    Build a paperless client class that records every upload.

    Args:
        uploads: Receives one entry per ``upload_document`` call.

    Returns:
        A class standing in for ``PaperlessClient``.

    """

    class RecordingPaperless:
        """Paperless client that remembers what it was asked to upload."""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            """Accept and ignore all constructor arguments."""

        def upload_document(self, *_args: object, **_kwargs: object) -> UploadResult:
            """Record the upload and report it delivered."""
            uploads.append("uploaded")
            return ApiDelivery(task_id="mock-task-uuid")

        def poll_task(self, *_args: object, **_kwargs: object) -> TaskFiled:
            """Return a successful task result."""
            return TaskFiled(task={"status": "SUCCESS"})

        def close(self) -> None:
            """No-op close."""

    return RecordingPaperless


class TestManualDuplexPrompt:
    """
    The CLI flip prompt, its non-terminal refusal, abort, and bounded wait.

    ``CliRunner`` is not a terminal, so the refusal test runs with
    ``_stdin_is_interactive`` *unpatched*; the prompt tests patch that one
    seam to ``True`` so they can reach the question at all. Neither test can
    be folded into the other: without the seam, one of them could not exist.

    The question waits for stdin to be readable before it reads a line, and
    ``CliRunner``'s stdin has no descriptor to wait on, so the prompt tests
    also replace that one wait (``tests.prompt_support``). The line is then
    read, parsed and answered for real.
    """

    def _interactive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pretend stdin is a terminal a human can answer on, readable at once."""
        monkeypatch.setattr("saneless.cli._stdin_is_interactive", lambda: True)
        readable(monkeypatch)

    def _typed(self, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
        """
        Give a coordinator called directly ``text`` on stdin, readable at once.

        Args:
            monkeypatch: Replaces ``sys.stdin`` and the prompt's wait.
            text: What the operator typed; empty for end of input.

        """
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))
        readable(monkeypatch)

    def test_prompt_between_passes_then_scans_backs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Answering yes scans both passes, and the prompt sits between them."""
        calls: list[str] = []
        uploads: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_duplex_settings(tmp_path),
            scanner_cls=_counting_scanner(calls),
            paperless_cls=_recording_paperless(uploads),
        )
        self._interactive(monkeypatch)

        result = runner.invoke(
            cli,
            ["scan", "--profile", _DUPLEX_PROFILE, "--title", "Both sides"],
            input="y\n",
        )

        assert result.exit_code == 0, result.output
        assert len(calls) == 2
        assert uploads == ["uploaded"]
        # The prompt is echoed into the output,
        # and it lands after the fronts and before the backs.
        fronts = result.output.index("Scanning...")
        prompt = result.output.index(_FLIP_PROMPT_FRAGMENT)
        backs = result.output.index("Scanning reverse sides...")
        assert fronts < prompt < backs

    def test_answering_no_cancels_without_scanning_backs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Answering no cancels the scan: one line, exit 130, nothing uploaded.

        A no is the operator choosing to stop, so it is a cancel rather than a
        scan error, and it exits with the shell's interrupt code so a script
        still sees a non-zero status. The prompt itself is echoed to stdout, so
        the cancel line is read from stderr.
        """
        calls: list[str] = []
        uploads: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_duplex_settings(tmp_path),
            scanner_cls=_counting_scanner(calls),
            paperless_cls=_recording_paperless(uploads),
        )
        self._interactive(monkeypatch)

        result = runner.invoke(
            cli,
            ["scan", "--profile", _DUPLEX_PROFILE, "--title", "Fronts only"],
            input="n\n",
        )

        assert result.exit_code == 130, result.output
        assert result.stderr.strip().splitlines()[-1] == _FLIP_CANCEL_LINE
        assert "Scan error" not in result.output
        assert len(calls) == 1
        assert uploads == []

    def test_eof_at_prompt_matches_answering_no(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        EOF (Ctrl-D) at the prompt cancels exactly as answering no does.

        Both are the operator's abort, so both exit 130 with the same cancel
        line, one scan pass and no upload -- the way a web Abort ends CANCELLED.
        """
        outcomes: list[tuple[int, str, int, int]] = []
        for answer in ("n\n", ""):
            calls: list[str] = []
            uploads: list[str] = []
            runner, _ = _patch_cli(
                monkeypatch,
                settings=_duplex_settings(tmp_path),
                scanner_cls=_counting_scanner(calls),
                paperless_cls=_recording_paperless(uploads),
            )
            self._interactive(monkeypatch)
            result = runner.invoke(
                cli,
                ["scan", "--profile", _DUPLEX_PROFILE, "--title", "Abort"],
                input=answer,
            )
            last_line = result.stderr.strip().splitlines()[-1]
            outcomes.append((result.exit_code, last_line, len(calls), len(uploads)))

        answered_no, end_of_input = outcomes
        assert end_of_input == answered_no
        assert end_of_input == (130, _FLIP_CANCEL_LINE, 1, 0)

    def test_ctrl_c_at_the_prompt_cancels_with_the_cancel_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Ctrl-C at the flip prompt exits 130 with the flip cancel line.

        The coordinator turns the ``KeyboardInterrupt`` into ``ABORTED``, so
        the guard sees ``ScanCancelledError`` and prints its message -- not the
        generic ``Cancelled (interrupted)`` of a Ctrl-C elsewhere in a command.
        """
        calls: list[str] = []
        uploads: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_duplex_settings(tmp_path),
            scanner_cls=_counting_scanner(calls),
            paperless_cls=_recording_paperless(uploads),
        )
        self._interactive(monkeypatch)
        # Raised out of the wait, as SIGINT is on the main thread.
        broken_read(monkeypatch, KeyboardInterrupt())

        result = runner.invoke(
            cli, ["scan", "--profile", _DUPLEX_PROFILE, "--title", "Ctrl-C"]
        )

        assert result.exit_code == 130, result.output
        assert result.stderr.strip().splitlines()[-1] == _FLIP_CANCEL_LINE
        assert "Cancelled (interrupted)" not in result.output
        assert len(calls) == 1
        assert uploads == []

    def test_a_broken_prompt_exits_1_with_the_prompt_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A prompt that breaks is a scan failure, exit 1 -- not a cancel.

        Nobody chose to stop, so the coordinator's ``abort_cause`` turns the
        ``ABORTED`` into a ``ScanError`` naming what broke.
        """
        calls: list[str] = []
        uploads: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_duplex_settings(tmp_path),
            scanner_cls=_counting_scanner(calls),
            paperless_cls=_recording_paperless(uploads),
        )
        self._interactive(monkeypatch)
        # A read error, as an I/O error at the terminal gives.
        broken_read(monkeypatch, OSError(errno.EIO, "Input/output error"))

        result = runner.invoke(
            cli, ["scan", "--profile", _DUPLEX_PROFILE, "--title", "Lost terminal"]
        )

        assert result.exit_code == 1, result.output
        # A prefix rather than the whole line: the fronts pass
        # A already fed are preserved, because nobody chose to stop, and the
        # error line goes on to name how many were kept and where.  The exit
        # code is unchanged, which is what preserving the exception type buys.
        failure_line = next(
            line
            for line in result.stderr.splitlines()
            if line.startswith(
                "Scan error: Flip prompt failed: [Errno 5] Input/output error"
            )
        )
        assert "were preserved at" in failure_line
        assert _FLIP_CANCEL_LINE not in result.output
        assert len(calls) == 1
        assert uploads == []

    def test_ctrl_c_during_the_wait_is_an_abort(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Ctrl-C while the question waits resolves ``ABORTED``, not a raw interrupt.

        SIGINT is handled on the main thread, which is the thread the question
        waits on, so it raises ``KeyboardInterrupt`` out of the wait.  A real
        signal cannot be sent here without taking the pytest session down with
        it if the handling regressed, so the wait is made to raise; the real
        signal is sent on a real terminal in ``test_cli_prompt_terminal``.
        """
        self._typed(monkeypatch, "")
        broken_read(monkeypatch, KeyboardInterrupt())
        coordinator = ClickFlipCoordinator()

        outcome = coordinator.wait_for_flip(600)

        assert outcome is FlipOutcome.ABORTED
        assert coordinator.abort_cause is None

    @pytest.mark.parametrize(
        "failure",
        [
            # A terminal that went away mid-prompt (a closed SSH session).
            OSError(5, "Input/output error"),
            # Bytes on stdin that are not valid in the terminal's encoding.
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ],
        ids=["lost-terminal", "undecodable-input"],
    )
    def test_a_broken_prompt_aborts_at_once_with_its_abort_cause_and_traceback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        failure: Exception,
    ) -> None:
        """
        An unexpected prompt failure ends the wait now, as ``ABORTED``.

        A broken read must not leave the scan waiting out the whole
        ``operator_wait_timeout_seconds`` and then report that nobody confirmed
        the flip, which would be false.  It is an abort, not a fourth outcome,
        and the traceback in the log carries the real cause.  The
        failure itself is kept as ``abort_cause``, so the pipeline reports a
        broken prompt as a failure rather than as the operator's cancel.
        """
        self._typed(monkeypatch, "")
        broken_read(monkeypatch, failure)
        caplog.set_level(logging.ERROR, logger="saneless.cli")
        coordinator = ClickFlipCoordinator()

        started = time.monotonic()
        outcome = coordinator.wait_for_flip(600)
        elapsed = time.monotonic() - started

        assert outcome is FlipOutcome.ABORTED
        assert elapsed < 5
        assert coordinator.abort_cause is failure
        records = [
            record
            for record in caplog.records
            if record.name == "saneless.cli"
            and record.levelno == logging.ERROR
            and "Flip prompt failed" in record.getMessage()
        ]
        assert len(records) == 1
        assert records[0].exc_info is not None

    def test_a_closed_stdin_at_the_question_is_a_broken_prompt(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        No stdin at all when the question is asked: a failure, never a crash.

        The scan refuses a closed stdin before any paper moves, so this is the
        coordinator's own guard: it answers as a broken read does.
        """
        monkeypatch.setattr(sys, "stdin", None)
        caplog.set_level(logging.ERROR, logger="saneless.cli")
        coordinator = ClickFlipCoordinator()

        outcome = coordinator.wait_for_flip(600)

        assert outcome is FlipOutcome.ABORTED
        assert isinstance(coordinator.abort_cause, OSError)
        assert any(
            "Flip prompt failed" in record.getMessage() for record in caplog.records
        )

    def test_answering_no_leaves_no_abort_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A no is the operator's own abort, so there is no cause to report."""
        self._typed(monkeypatch, "n\n")
        coordinator = ClickFlipCoordinator()

        outcome = coordinator.wait_for_flip(600)

        assert outcome is FlipOutcome.ABORTED
        assert coordinator.abort_cause is None

    @pytest.mark.parametrize("typed", ["\n", "y\n", "YES\n", " Y \n"])
    def test_yes_or_just_return_continues(
        self, monkeypatch: pytest.MonkeyPatch, typed: str
    ) -> None:
        """Yes, in any case, or Return on its own (the default) continues."""
        self._typed(monkeypatch, typed)

        outcome = ClickFlipCoordinator().wait_for_flip(600)

        assert outcome is FlipOutcome.CONTINUED

    def test_eof_at_the_prompt_leaves_no_abort_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ctrl-D (end of input at the question) is an operator abort."""
        self._typed(monkeypatch, "")
        coordinator = ClickFlipCoordinator()

        outcome = coordinator.wait_for_flip(600)

        assert outcome is FlipOutcome.ABORTED
        assert coordinator.abort_cause is None

    def test_ctrl_c_leaves_no_abort_cause_and_no_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Ctrl-C keeps its meaning, a cancel, and leaves nothing reading stdin.

        The question was read on the calling thread, so once ``wait_for_flip``
        has returned nothing of it is left behind to answer late.
        """
        self._typed(monkeypatch, "")
        broken_read(monkeypatch, KeyboardInterrupt())
        coordinator = ClickFlipCoordinator()

        outcome = coordinator.wait_for_flip(600)

        assert _FLIP_PROMPT_THREAD not in [
            thread.name for thread in threading.enumerate()
        ]
        assert outcome is FlipOutcome.ABORTED
        assert coordinator.abort_cause is None

    def test_the_coordinator_times_out_at_a_zero_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``wait_for_flip(0)`` with nobody answering resolves ``TIMED_OUT``.

        The coordinator's own contract, independent of the config bounds that
        keep zero out of ``operator_wait_timeout_seconds``: its ``timeout``
        argument is the zero-cost seam, so no wall clock is spent here.
        """
        self._typed(monkeypatch, "")
        never_readable(monkeypatch, FakeClock())
        coordinator = ClickFlipCoordinator()

        outcome = coordinator.wait_for_flip(0)

        assert outcome is FlipOutcome.TIMED_OUT

    def test_a_bad_answer_is_asked_again_with_the_time_left(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A refused answer does not restart the clock: the deadline still stands.

        The first answer is refused at 40 s into a 60 s wait; the question is
        asked again for the remaining 20 s, and nothing comes.
        """
        clock = FakeClock()
        waits: list[float] = []

        def wait(_stream: object, timeout: float) -> bool:
            """Answer once after 40 s, then stay silent for the time given."""
            waits.append(timeout)
            if len(waits) == 1:
                clock.advance(40)
                return True
            clock.advance(timeout)
            return False

        monkeypatch.setattr(sys, "stdin", io.StringIO("maybe\n"))
        monkeypatch.setattr("saneless.cli._monotonic", clock)
        monkeypatch.setattr("saneless.cli._wait_readable", wait)

        outcome = ClickFlipCoordinator().wait_for_flip(60)

        assert outcome is FlipOutcome.TIMED_OUT
        assert waits == [60, 20]

    def test_unanswered_prompt_times_out_before_pass_b(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        An answer that never arrives fails the job on the flip wait.

        The whole ``scan`` command runs end to end: the bounded wait expires
        and ``TIMED_OUT`` is claimed before pass B.  The wait runs on a fake
        clock, so the one second the config allows at least costs nothing.
        """
        calls: list[str] = []
        uploads: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_duplex_settings(tmp_path, operator_wait_timeout_seconds=1),
            scanner_cls=_counting_scanner(calls),
            paperless_cls=_recording_paperless(uploads),
        )
        self._interactive(monkeypatch)
        never_readable(monkeypatch, FakeClock())

        result = runner.invoke(
            cli, ["scan", "--profile", _DUPLEX_PROFILE, "--title", "Forgotten"]
        )

        # A forgotten prompt is a failure, not a cancel: exit 1, never 130.
        assert result.exit_code == 1, result.output
        assert "flip wait" in result.output
        assert len(calls) == 1
        assert uploads == []

    def test_non_interactive_stdin_refused_up_front(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Without a terminal, manual duplex exits 2 before any paper moves.

        Deliberately *not* patched: ``CliRunner`` is honestly not a TTY.
        """
        calls: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_duplex_settings(tmp_path),
            scanner_cls=_counting_scanner(calls),
        )

        result = runner.invoke(
            cli, ["scan", "--profile", _DUPLEX_PROFILE, "--title", "Cron"]
        )

        assert result.exit_code == 2
        assert "interactive terminal" in result.output
        assert calls == []

    def test_simplex_profile_neither_prompts_nor_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A non-duplex profile off a terminal scans once with no prompt."""
        calls: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_duplex_settings(tmp_path),
            scanner_cls=_counting_scanner(calls),
        )

        result = runner.invoke(cli, ["scan", "--title", "Simplex"])

        assert result.exit_code == 0, result.output
        assert _FLIP_PROMPT_FRAGMENT not in result.output
        assert "interactive terminal" not in result.output
        assert len(calls) == 1

    def test_no_flip_prompt_thread_is_started(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The flip question is asked on the calling thread: no prompt thread at all.

        A thread parked on stdin outlives the command that started it, and on
        a real terminal it holds the process open at exit until someone
        presses Enter.  So no thread is started for the question, and none is
        left behind once the command is over.
        """
        started: list[str] = []
        real_start = threading.Thread.start

        def recording_start(thread: threading.Thread) -> None:
            """Note the thread's name, then start it as usual."""
            started.append(thread.name)
            real_start(thread)

        calls: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_duplex_settings(tmp_path),
            scanner_cls=_counting_scanner(calls),
        )
        self._interactive(monkeypatch)
        readable(monkeypatch)
        monkeypatch.setattr(threading.Thread, "start", recording_start)

        result = runner.invoke(
            cli,
            ["scan", "--profile", _DUPLEX_PROFILE, "--title", "No thread"],
            input="n\n",
        )

        assert result.exit_code == ExitCode.CANCELLED, result.output
        assert result.stderr.strip().splitlines()[-1] == _FLIP_CANCEL_LINE
        assert _FLIP_PROMPT_THREAD not in started
        assert _FLIP_PROMPT_THREAD not in [
            thread.name for thread in threading.enumerate()
        ]
        assert len(calls) == 1

    def test_a_bad_flip_answer_is_asked_again(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        An answer that is neither yes nor no is refused, and the question asked again.

        The refusal is click's own wording, the one every click confirmation
        prompt prints.
        """
        calls: list[str] = []
        uploads: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_duplex_settings(tmp_path),
            scanner_cls=_counting_scanner(calls),
            paperless_cls=_recording_paperless(uploads),
        )
        self._interactive(monkeypatch)
        readable(monkeypatch)

        result = runner.invoke(
            cli,
            ["scan", "--profile", _DUPLEX_PROFILE, "--title", "Asked twice"],
            input="maybe\ny\n",
        )

        assert result.exit_code == ExitCode.SUCCESS, result.output
        assert "Error: invalid input" in result.output
        assert result.output.count(_FLIP_PROMPT_FRAGMENT) == 2
        assert len(calls) == 2
        assert uploads == ["uploaded"]

    @pytest.mark.parametrize(
        "failure",
        [
            OSError(errno.EIO, "Input/output error"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ],
        ids=["lost-terminal", "undecodable-input"],
    )
    def test_a_broken_flip_read_is_a_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
        failure: Exception,
    ) -> None:
        """
        A read that breaks at the flip question fails the scan: exit 1, logged.

        Nobody chose to stop, so it is neither a cancel (130) nor an
        unexpected error (5), and the log keeps the traceback.
        """
        calls: list[str] = []
        uploads: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_duplex_settings(tmp_path),
            scanner_cls=_counting_scanner(calls),
            paperless_cls=_recording_paperless(uploads),
        )
        self._interactive(monkeypatch)
        broken_read(monkeypatch, failure)
        caplog.set_level(logging.ERROR, logger="saneless.cli")

        result = runner.invoke(
            cli, ["scan", "--profile", _DUPLEX_PROFILE, "--title", "Broken read"]
        )

        assert result.exit_code == ExitCode.SCAN, result.output
        records = [
            record
            for record in caplog.records
            if record.name == "saneless.cli"
            and "Flip prompt failed; treating it as an abort" in record.getMessage()
        ]
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert len(calls) == 1
        assert uploads == []

    def test_a_closed_stdin_with_a_manual_duplex_profile_is_refused(
        self, tmp_path: Path
    ) -> None:
        """
        ``saneless scan`` with stdin closed (``<&-``) is refused, exit 2.

        With no stdin at all there is nobody to flip the stack, the same as
        from cron, so it gets the same refusal rather than an unexpected
        error.  A real process, because only a real process can start with
        no standard input.
        """
        completed = _scan_with_stdin_closed(tmp_path, ["--profile", _DUPLEX_PROFILE])

        assert completed.returncode == ExitCode.CONFIG, completed.stderr
        assert "which needs an interactive terminal" in completed.stderr

    def test_a_closed_stdin_with_multi_page_is_refused(self, tmp_path: Path) -> None:
        """
        ``saneless scan --multi-page`` with stdin closed (``<&-``) is refused, exit 2.

        Nobody can answer "another page?" without a stdin, so it gets the
        off-terminal refusal before the scanner opens, not an unexpected
        error.
        """
        completed = _scan_with_stdin_closed(tmp_path, ["--multi-page"])

        assert completed.returncode == ExitCode.CONFIG, completed.stderr
        assert MULTI_PAGE_NEEDS_TERMINAL in " ".join(completed.stderr.split())


def _scan_with_stdin_closed(
    tmp_path: Path, args: list[str]
) -> subprocess.CompletedProcess[str]:
    """
    Run the real ``saneless scan`` in a process started with no stdin (``<&-``).

    A shell starts the child because ``<&-`` leaves descriptor 0 closed, which
    no ``subprocess`` stdin setting does.

    Args:
        tmp_path: The test's scratch directory, for the config and every path
            the scan would write.
        args: The arguments after ``scan``, before ``--title``.

    Returns:
        The finished process, its output captured as text.

    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = tmp_path / "saneless.toml"
    config.write_text(
        _CLOSED_STDIN_CONFIG.format(
            tmp_dir=tmp_path / "scratch",
            data_dir=data_dir,
            log_file=tmp_path / "logs" / "saneless.log",
            profile=_DUPLEX_PROFILE,
        )
    )
    env = {
        **os.environ,
        "SANELESS_TEST_PYTHON": sys.executable,
        "SANELESS_TEST_SOURCE": _CLOSED_STDIN_CHILD,
        "SANELESS_TEST_CONFIG": str(config),
        "SANELESS_TEST_ARGS": json.dumps(args),
    }
    return subprocess.run(
        [
            "/bin/sh",
            "-c",
            'exec "$SANELESS_TEST_PYTHON" -c "$SANELESS_TEST_SOURCE" <&-',
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[1],
        timeout=_CLOSED_STDIN_CHILD_SECONDS,
    )


# The thread the flip question was once read on.  Nothing may start it.
_FLIP_PROMPT_THREAD = "saneless-flip-prompt"

_CLOSED_STDIN_CHILD_SECONDS = 60

_CLOSED_STDIN_CONFIG = """\
[scanner]
device = "test:device:001"

[paperless]
url = "http://paperless.invalid:8000"
token = "closed-stdin-token"

[output]
tmp_dir = "{tmp_dir}"
data_dir = "{data_dir}"
log_file = "{log_file}"

[profiles.default]

[profiles.{profile}]
source = "ADF"
duplex = "manual"
"""

# The console script's shim with the SANE library check replaced, the
# arguments travelling in the environment.  They are taken out of it before
# the CLI starts, because saneless reads every SANELESS_ variable as a setting.
_CLOSED_STDIN_CHILD = """
import json
import os
import sys

from saneless import cli as cli_module
from saneless import main

config = os.environ.pop("SANELESS_TEST_CONFIG")
args = json.loads(os.environ.pop("SANELESS_TEST_ARGS"))
os.environ.pop("SANELESS_TEST_PYTHON")
os.environ.pop("SANELESS_TEST_SOURCE")


def require_sane():
    return None


cli_module.require_sane = require_sane
sys.argv = ["saneless", "--config", config, "scan", *args, "--title", "t"]
sys.exit(main())
"""


class _RangeScanner(StubScannerBackend):
    """A scanner whose device constrains resolution with a range."""

    def __init__(self, host: str = "") -> None:
        """Accept host parameter for API compatibility."""

    def get_devices(self) -> list[DeviceInfo]:
        """Return one device, named as the SANE test backend names it."""
        return [DeviceInfo("test:0", "TestVendor", "TestModel", "scanner")]

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """Report resolution as a range and give no word list at all."""
        return DeviceCapabilities(
            sources=["Flatbed", "Automatic Document Feeder"],
            resolutions=[],
            modes=["Color", "Gray"],
            resolution_range=(1.0, 1200.0, 1.0),
        )


_HALF_BROKEN_REASON = "Could not open hp:002: Error during device I/O"


class _HalfBrokenScanner(StubScannerBackend):
    """Two devices, and the second one's capabilities cannot be read."""

    def __init__(self, host: str = "") -> None:
        """Accept host parameter for API compatibility."""

    def get_devices(self) -> list[DeviceInfo]:
        """Return a readable device and one whose probe fails."""
        return [
            DeviceInfo("epson:001", "Epson", "ET-4850", "flatbed scanner"),
            DeviceInfo("hp:002", "HP", "Envy 6055", "multi-function peripheral"),
        ]

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """Answer for the first device and fail the way SANE does for the second."""
        if device_id == "hp:002":
            # Whitespace the one-line rendering has to collapse.
            msg = "Could not open hp:002:\n  Error during device I/O"
            raise ScanError(msg)
        return DeviceCapabilities(
            sources=["Flatbed"], resolutions=[300], modes=["color"]
        )


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
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["name"] == "epson:001"

    def test_devices_json_without_capabilities_is_unchanged_byte_for_byte(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Scripts written against the plain device list see exactly what they saw.

        The capabilities key exists only when it was asked for, so the plain
        document is pinned as a literal: four keys, in this order, indented by
        two, and nothing on stdout before or after it.
        """
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["devices", "--json"])

        assert result.exit_code == 0, result.output
        assert result.stdout == (
            "[\n"
            "  {\n"
            '    "name": "epson:001",\n'
            '    "vendor": "Epson",\n'
            '    "model": "ET-4850",\n'
            '    "type": "flatbed scanner"\n'
            "  },\n"
            "  {\n"
            '    "name": "hp:002",\n'
            '    "vendor": "HP",\n'
            '    "model": "Envy 6055",\n'
            '    "type": "multi-function peripheral"\n'
            "  }\n"
            "]\n"
        )

    def test_devices_json_capabilities_is_one_document(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``--json --capabilities`` is a single JSON document a script can parse.

        Each device carries what the text mode prints for it: sources,
        resolutions, modes and the raw SANE option names.
        """
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["devices", "--json", "--capabilities"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert [d["name"] for d in data] == ["epson:001", "hp:002"]
        for device in data:
            assert device["capabilities"] == {
                "sources": ["Flatbed", "ADF"],
                "resolutions": [150, 300, 600],
                "modes": ["color", "gray"],
                "raw_options": ["source"],
            }
            assert "capabilities_error" not in device

    def test_devices_json_capabilities_reports_a_range_as_a_range(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A range-reporting device gets min, max and step, and no invented list."""
        runner, _ = _patch_cli(monkeypatch, scanner_cls=_RangeScanner)

        result = runner.invoke(cli, ["devices", "--json", "--capabilities"])

        assert result.exit_code == 0, result.output
        [device] = json.loads(result.stdout)
        assert device["capabilities"] == {
            "sources": ["Flatbed", "Automatic Document Feeder"],
            "resolution_range": {"min": 1.0, "max": 1200.0, "step": 1.0},
            "modes": ["Color", "Gray"],
        }

    def test_devices_json_capabilities_reports_a_failed_probe_per_device(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        One device whose capabilities cannot be read does not hide the others.

        That device gets ``null`` and the reason, the rest report in full,
        stdout is still one parseable document, and the exit code says a scan
        error happened.
        """
        runner, _ = _patch_cli(monkeypatch, scanner_cls=_HalfBrokenScanner)
        caplog.set_level(logging.WARNING, logger="saneless.cli")

        result = runner.invoke(cli, ["devices", "--json", "--capabilities"])

        assert result.exit_code == 1, result.output
        good, bad = json.loads(result.stdout)
        assert good["capabilities"]["sources"] == ["Flatbed"]
        assert "capabilities_error" not in good
        assert bad["capabilities"] is None
        assert bad["capabilities_error"] == _HALF_BROKEN_REASON
        assert result.stderr.splitlines() == [
            f"Capabilities for hp:002: {_HALF_BROKEN_REASON}"
        ]
        assert [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ] == [f"Could not read capabilities for 'hp:002': {_HALF_BROKEN_REASON!r}"]

    def test_devices_text_capabilities_reports_a_failed_probe_and_lists_the_rest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Text mode behaves the same: one stderr line, the others listed, exit 1."""
        runner, _ = _patch_cli(monkeypatch, scanner_cls=_HalfBrokenScanner)

        result = runner.invoke(cli, ["devices", "--capabilities"])

        assert result.exit_code == 1, result.output
        assert "Capabilities for epson:001:" in result.stdout
        assert "  Sources: Flatbed" in result.stdout
        assert "hp:002" in result.stdout  # still in the table
        assert "Capabilities for hp:002" not in result.stdout
        assert result.stderr.splitlines() == [
            "Discovering scanners...",
            f"Capabilities for hp:002: {_HALF_BROKEN_REASON}",
        ]

    def test_devices_capability_probe_bug_still_reaches_the_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a scanner error is reported per device; a saneless bug is exit 5."""

        class _BuggyScanner(_HalfBrokenScanner):
            """A scanner whose capability read fails with a non-saneless error."""

            def get_capabilities(self, device_id: str) -> DeviceCapabilities:
                """Fail the way a bug would, not the way SANE does."""
                msg = f"bug reading {device_id}"
                raise RuntimeError(msg)

        runner, _ = _patch_cli(monkeypatch, scanner_cls=_BuggyScanner)

        result = runner.invoke(cli, ["devices", "--json", "--capabilities"])

        assert result.exit_code == 5, result.output
        assert "Unexpected error (RuntimeError)" in result.stderr

    @pytest.mark.parametrize("args", [["devices"], ["devices", "--capabilities"]])
    def test_devices_status_line_goes_to_stderr(
        self, monkeypatch: pytest.MonkeyPatch, args: list[str]
    ) -> None:
        """Only data reaches stdout, so ``saneless devices | grep`` stays clean."""
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, args)

        assert result.exit_code == 0, result.output
        assert "Discovering scanners..." in result.stderr
        assert "Discovering scanners..." not in result.stdout

    @pytest.mark.parametrize(
        ("args", "stdout", "stderr"),
        [
            (["devices"], "", ["Discovering scanners...", "No scanners found."]),
            (["devices", "--json"], "[]\n", []),
        ],
        ids=["table", "json"],
    )
    def test_devices_with_no_scanners_keeps_stdout_for_data(
        self,
        monkeypatch: pytest.MonkeyPatch,
        args: list[str],
        stdout: str,
        stderr: list[str],
    ) -> None:
        """
        "No scanners found." is a status line, so it goes to stderr.

        An empty table is no data at all, so ``saneless devices | grep`` sees
        nothing; the JSON form still prints its empty array on stdout, because
        that is the data.
        """

        class _NoScanners(StubScannerBackend):
            """A backend that finds no devices."""

            def __init__(self, host: str = "") -> None:
                """Accept the host the CLI passes."""

            def get_devices(self) -> list[DeviceInfo]:
                """Report no devices."""
                return []

        runner, _ = _patch_cli(monkeypatch, scanner_cls=_NoScanners)

        result = runner.invoke(cli, args)

        assert result.exit_code == 0, result.output
        assert result.stdout == stdout
        assert result.stderr.splitlines() == stderr

    def test_the_device_table_fits_80_columns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        On an 80-column terminal no line of the table is wider than 80.

        That holds even for a device type as long as SANE's own
        "multi-function peripheral" after three fixed columns.
        """

        class _LongFields(StubScannerBackend):
            def __init__(self, host: str = "") -> None:
                """Accept the host the CLI passes."""

            def get_devices(self) -> list[DeviceInfo]:
                """Report one device whose every field is long."""
                return [
                    DeviceInfo(
                        "net:scanner-host.example.lan:hpaio:/net/OfficeJet_Pro?ip=1",
                        "Hewlett-Packard Development Company",
                        "OfficeJet Pro 9010 series all-in-one",
                        "multi-function peripheral",
                    )
                ]

        runner, _ = _patch_cli(monkeypatch, scanner_cls=_LongFields)

        result = runner.invoke(cli, ["devices"], env={"COLUMNS": "80"})

        assert result.exit_code == 0, result.output
        lines = result.stdout.splitlines()
        assert len(lines) == 3, result.stdout
        assert all(len(line) <= 80 for line in lines), [len(x) for x in lines]

    @pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
    def test_capabilities_omit_blank_option_names(
        self, monkeypatch: pytest.MonkeyPatch, *, as_json: bool
    ) -> None:
        """
        The raw option list names options, and nothing without a name.

        The real backend over a device that reports the option count ('') and
        a group heading (None) among its options: the text has no blank line
        and no "None" under "Raw options", and the JSON list holds strings
        only.
        """
        device = FakeSaneDev()
        named = device.get_options()
        count, group = UNNAMED_OPTION_ENTRIES

        def with_unnamed() -> list[tuple]:
            return [count, *named[:2], group, *named[2:]]

        # The device stores a name that is not one of its options on itself,
        # as python-sane does, so this shadows get_options for this device.
        monkeypatch.setattr(device, "get_options", with_unnamed)
        monkeypatch.setattr(sane_backend, "sane", FakeSaneModule(device=device))
        runner, _ = _patch_cli(monkeypatch, scanner_cls=sane_backend.SaneBackend)
        expected = [str(opt[1]) for opt in named]

        result = runner.invoke(
            cli, ["devices", "--capabilities", *(["--json"] if as_json else [])]
        )

        assert result.exit_code == 0, result.output
        if as_json:
            entries = json.loads(result.stdout)
            assert entries
            for entry in entries:
                assert entry["capabilities"]["raw_options"] == expected
        else:
            blocks = result.stdout.split("  Raw options:\n")[1:]
            assert blocks, result.stdout
            for block in blocks:
                listed = [
                    line for line in block.splitlines() if line.startswith("    ")
                ]
                assert listed == [f"    {name}" for name in expected]

    def test_devices_capabilities(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        Devices --capabilities -> raw option names shown.

        The "Raw options:" label and one indented name per line are the text
        the documentation shows, so they stay whatever shape the backend hands
        the names over in.
        """
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["devices", "--capabilities"])
        assert result.exit_code == 0
        assert "Flatbed" in result.output
        assert "ADF" in result.output
        assert "  Raw options:\n    source\n" in result.stdout

    def test_devices_capabilities_prints_a_reported_word_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A list-reporting device prints its exact values."""
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["devices", "--capabilities"])

        assert result.exit_code == 0
        assert "Resolutions: 150, 300, 600" in result.output
        # The device gave a list, so no range is invented to go with it.
        assert "Resolution range" not in result.output

    def test_devices_capabilities_prints_a_reported_range(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A range-reporting device's minimum, maximum and step all reach the operator.

        The SANE ``test`` backend constrains resolution with
        ``(1.0, 1200.0, 1.0)``; printed as a list, that range would be a label
        with nothing after it.
        """
        runner, _ = _patch_cli(monkeypatch, scanner_cls=_RangeScanner)

        result = runner.invoke(cli, ["devices", "--capabilities"])

        assert result.exit_code == 0
        assert "Resolution range: 1 to 1200 dpi in steps of 1" in result.output
        # No word list was reported, so none is printed and none is invented.
        assert "Resolutions:" not in result.output
        # The symptom itself: a label with its value missing. Such a line ends
        # at the separator with nothing following it.
        assert [
            line for line in result.output.splitlines() if line.endswith(": ")
        ] == []


class TestCliFlags:
    """CLI flag tests."""

    def test_verbose_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """-v puts saneless at DEBUG and mirrors its DEBUG records to stderr."""
        monkeypatch.setattr(
            "saneless.cli.load_settings", lambda *_a, **_kw: _tmp_settings(tmp_path)
        )

        class MockSaneBackend(StubScannerBackend):
            """Mock scanner that logs one DEBUG record and returns no devices."""

            def __init__(self, host: str = "") -> None:
                """Accept host parameter for API compatibility."""

            def get_devices(self) -> list[DeviceInfo]:
                """
                Log a saneless DEBUG record, then report no devices.

                Returns:
                    An empty list.

                """
                logging.getLogger("saneless.scanner").debug("verbose-probe-5c1d")
                return []

        monkeypatch.setattr("saneless.cli.SaneBackend", MockSaneBackend)

        with _restored_logging():
            result = CliRunner().invoke(cli, ["-v", "devices"])
            saneless_level = logging.getLogger("saneless").getEffectiveLevel()

        assert result.exit_code == 0, result.output
        assert saneless_level == logging.DEBUG
        assert "verbose-probe-5c1d" in result.stderr

    def test_config_flag(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """``--config`` hands its path to load_settings unchanged."""
        captured: dict[str, object] = {}

        def capture_load(config_path: str | None = None) -> Settings:
            """Record the config_path argument."""
            captured["config_path"] = config_path
            return _make_settings(tmp_path)

        runner = CliRunner()
        monkeypatch.setattr("saneless.cli.load_settings", capture_load)
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *_a, **_kw: None)

        class MockSaneBackend(StubScannerBackend):
            """Mock scanner that returns no devices."""

            def __init__(self, host: str = "") -> None:
                """Accept host parameter for API compatibility."""

            def get_devices(self) -> list[DeviceInfo]:
                """Return empty device list."""
                return []

        monkeypatch.setattr("saneless.cli.SaneBackend", MockSaneBackend)

        result = runner.invoke(cli, ["--config", "/path/to/saneless.toml", "devices"])
        assert result.exit_code == 0
        assert captured["config_path"] == "/path/to/saneless.toml"


class TestLegacyDuplexWarningReachesLogFile:
    """The legacy manual-duplex warning lands in the configured log file."""

    def test_warning_is_written_to_log_file(self, tmp_path: Path) -> None:
        """
        A real config load through ``cli()`` writes the warning to ``log_file``.

        Deliberately not ``_patch_cli``: the subject is the order of loading
        settings against configuring logging, so both run for real.  The
        warning is only useful to an operator if it reaches the log file the
        appliance keeps, and it can only reach it once that handler exists.
        """
        log_file = tmp_path / "logs" / "saneless.log"
        doc = tomlkit.document()
        output = tomlkit.table()
        output.add("tmp_dir", str(tmp_path / "tmp"))
        output.add("data_dir", str(tmp_path / "data"))
        output.add("log_file", str(log_file))
        doc.add("output", output)
        profiles_table = tomlkit.table(is_super_table=True)
        profiles_table.add("default", tomlkit.table())
        legacy = tomlkit.table()
        legacy.add("source", "Manual Duplex")
        profiles_table.add("legacy", legacy)
        doc.add("profiles", profiles_table)
        config_file = tmp_path / "saneless.toml"
        config_file.write_text(tomlkit.dumps(doc))

        with _restored_logging():
            result = CliRunner().invoke(
                cli, ["--config", str(config_file), "jobs", "--limit", "1"]
            )

        assert result.exit_code == 0, result.output
        content = log_file.read_text()
        assert "'legacy'" in content
        assert 'duplex = "manual"' in content


@contextlib.contextmanager
def _restored_logging() -> Generator[None]:
    """
    Undo what a real ``configure_logging`` call did to the logging tree.

    ``configure_logging`` adds handlers to the ROOT logger, sets its level, and
    sets the ``saneless`` logger's level; remove and restore all three so later
    tests neither write into this test's ``tmp_path`` nor inherit DEBUG.

    Yields:
        Nothing; the restore runs on exit.

    """
    root_logger = logging.getLogger()
    handlers_before = list(root_logger.handlers)
    level_before = root_logger.level
    try:
        yield
    finally:
        for handler in list(root_logger.handlers):
            if handler not in handlers_before:
                root_logger.removeHandler(handler)
                handler.close()
        root_logger.setLevel(level_before)
        logging.getLogger("saneless").setLevel(logging.NOTSET)


def _write_real_config(tmp_path: Path, paperless: dict[str, str] | None = None) -> Path:
    """
    Write a config whose every path lives under ``tmp_path``.

    The log file sits in ``tmp_path / "logs"``, a directory that exists only if
    something configured logging, which is what the ``--help`` tests assert
    never happened.

    Args:
        tmp_path: The test's temporary directory.
        paperless: Keys for a ``[paperless]`` table, possibly misspelt ones.

    Returns:
        The path of the written ``saneless.toml``.

    """
    doc = tomlkit.document()
    if paperless is not None:
        paperless_table = tomlkit.table()
        for key, value in paperless.items():
            paperless_table.add(key, value)
        doc.add("paperless", paperless_table)
    output = tomlkit.table()
    output.add("tmp_dir", str(tmp_path / "tmp"))
    output.add("data_dir", str(tmp_path / "data"))
    output.add("log_file", str(tmp_path / "logs" / "saneless.log"))
    doc.add("output", output)
    config_file = tmp_path / "saneless.toml"
    config_file.write_text(tomlkit.dumps(doc))
    return config_file


_ALL_COMMANDS = ["scan", "devices", "jobs", "serve", "auto-profiles"]


class TestLazySettingsLoading:
    """
    Settings load lazily, once, inside the commands that need them.

    ``saneless <subcommand> --help`` must work with a broken or missing
    configuration. Click runs the group callback *before* a subcommand
    parses its own ``--help``, and ``ctx.resilient_parsing`` is False there,
    so the group callback cannot load anything; each command loads on first
    need instead. A ConfigError from loading is printed by the group guard
    as rendered, exit 2; an unwritable log file is not a failure, it falls
    back to stderr.
    """

    @pytest.mark.parametrize("command", _ALL_COMMANDS)
    def test_subcommand_help_needs_no_config(
        self, monkeypatch: pytest.MonkeyPatch, command: str
    ) -> None:
        """``<command> --help`` exits 0 and neither loads nor configures logging."""
        calls: list[str] = []

        def failing_load(*_args: object, **_kwargs: object) -> Settings:
            calls.append("load_settings")
            msg = "Configuration error in /nope.toml:\n  [output] broken"
            raise ConfigError(msg)

        def recording_logging(*_args: object, **_kwargs: object) -> None:
            calls.append("configure_logging")

        monkeypatch.setattr("saneless.cli.load_settings", failing_load)
        monkeypatch.setattr("saneless.cli.configure_logging", recording_logging)

        result = CliRunner().invoke(cli, [command, "--help"])

        assert result.exit_code == 0, result.output
        assert f"Usage: cli {command}" in result.output
        assert calls == []

    def test_help_with_broken_real_config_creates_no_log_dir(
        self, tmp_path: Path
    ) -> None:
        """A real broken config does not stop ``serve --help`` or make a log dir."""
        config_file = _write_real_config(tmp_path, paperless={"tokne": "x"})

        with _restored_logging():
            result = CliRunner().invoke(
                cli, ["--config", str(config_file), "serve", "--help"]
            )

        assert result.exit_code == 0, result.output
        assert "--host" in result.output
        assert not (tmp_path / "logs").exists()

    def test_real_config_error_printed_once_with_its_own_header(
        self, tmp_path: Path
    ) -> None:
        """The loader's rendered error is echoed as-is, not prefixed again."""
        config_file = _write_real_config(tmp_path, paperless={"tokne": "x"})

        with _restored_logging():
            result = CliRunner().invoke(cli, ["--config", str(config_file), "jobs"])

        assert result.exit_code == 2
        assert result.output.startswith("Configuration error in")
        assert result.output.count("Configuration error") == 1
        assert "tokne" in result.output
        assert not (tmp_path / "logs").exists()

    def test_missing_config_exits_2_naming_the_path(self, tmp_path: Path) -> None:
        """``--config`` to a file that does not exist exits 2 and names it."""
        missing = tmp_path / "nope.toml"

        with _restored_logging():
            result = CliRunner().invoke(cli, ["--config", str(missing), "jobs"])

        assert result.exit_code == 2
        assert str(missing) in result.output

    def test_empty_config_path_exits_2_instead_of_discovering(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``--config ""`` is an explicit, unusable path, not "no path".

        ``saneless --config "$CFG" ...`` with ``CFG`` unset must not load -- or
        let ``auto-profiles`` write -- whichever file discovery would find.
        """
        _write_real_config(tmp_path)
        monkeypatch.chdir(tmp_path)

        with _restored_logging():
            result = CliRunner().invoke(cli, ["--config", "", "jobs"])

        assert result.exit_code == 2, result.output
        assert "empty" in result.output
        assert not (tmp_path / "logs").exists()

    def test_unwritable_log_file_falls_back_to_stderr_and_runs(
        self, tmp_path: Path
    ) -> None:
        """
        An unwritable ``log_file`` warns on stderr; the command still runs.

        The real ``configure_logging`` catches the OSError from creating the
        log directory or opening the file and logs to stderr instead, so this
        is neither an exit 2 nor a traceback. ``logs`` is a regular file here,
        so the log directory cannot be created even when running as root.
        """
        config_file = _write_real_config(tmp_path)
        (tmp_path / "logs").write_text("not a directory")

        with _restored_logging():
            result = CliRunner().invoke(cli, ["--config", str(config_file), "jobs"])

        assert result.exit_code == 0, result.output
        assert "logging to stderr only" in result.output
        assert str(tmp_path / "logs" / "saneless.log") in result.output
        assert "Traceback" not in result.output

    def test_toml_syntax_error_is_one_line_exit_2(self, tmp_path: Path) -> None:
        """
        A TOML syntax error exits 2 without a traceback, under the config header.

        The loader turns the ``TOMLDecodeError`` into a ConfigError naming the
        line and column, so it reaches the ConfigError handler rather than the
        generic one.
        """
        config_file = tmp_path / "saneless.toml"
        config_file.write_text("[output\n")

        with _restored_logging():
            result = CliRunner().invoke(cli, ["--config", str(config_file), "jobs"])

        assert result.exit_code == 2
        lines = result.output.splitlines()
        assert lines[0] == f"Configuration error in {config_file}:"
        assert lines[1].startswith("  line 1, column ")
        assert "Traceback" not in result.output
        assert isinstance(result.exception, SystemExit)

    def test_settings_loaded_and_logging_configured_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A second request for settings reuses the first load (memoised)."""
        settings = _make_settings(tmp_path)
        counts = {"load": 0, "logging": 0}

        def counting_load(*_args: object, **_kwargs: object) -> Settings:
            counts["load"] += 1
            return settings

        def counting_logging(*_args: object, **_kwargs: object) -> None:
            counts["logging"] += 1

        monkeypatch.setattr("saneless.cli.load_settings", counting_load)
        monkeypatch.setattr("saneless.cli.configure_logging", counting_logging)
        ctx = click.Context(cli, obj={"config_path": None, "verbose": False})

        first = cli_module._load_cli_settings(ctx)
        second = cli_module._load_cli_settings(ctx)

        assert first is settings
        assert second is settings
        assert counts == {"load": 1, "logging": 1}


class TestFirstRunFileModes:
    """
    A one-shot command run before the server creates private state.

    The default log file sits inside the default ``data_dir``, so whichever
    command runs first creates ``data_dir`` while it sets up logging. A bare
    metal install commonly runs ``saneless doctor`` or ``saneless jobs`` before
    ever starting the server, and later calls leave an existing directory's
    mode alone. The directory and the log, which holds document titles, must
    therefore come out owner-only from that first command.
    """

    @staticmethod
    def _mode(path: Path) -> int:
        """Return the permission bits of ``path``."""
        return path.stat().st_mode & 0o777

    def test_jobs_on_a_fresh_state_home_leaves_data_dir_and_log_private(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``saneless jobs`` first, on an empty state home, under umask 022."""
        state_home = tmp_path / "state"
        monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
        data_dir = state_home / "saneless"

        previous = os.umask(0o022)
        try:
            with _restored_logging():
                result = CliRunner().invoke(cli, ["jobs"])
        finally:
            os.umask(previous)

        assert result.exit_code == 0, result.output
        assert self._mode(data_dir) == 0o700
        assert self._mode(data_dir / "saneless.log") == 0o600
        # Listing the history only reads it: none yet is no database.
        assert not (data_dir / "saneless.db").exists()

    def test_a_log_nested_inside_data_dir_still_leaves_data_dir_private(
        self, tmp_path: Path
    ) -> None:
        """
        ``data_dir`` is 0700 when the log sits in a subdirectory of it.

        Logging creates the log's own directory 0700, but a plain parent
        created on the way would get the umask's 0755.
        """
        data_dir = tmp_path / "data"
        config_file = tmp_path / "saneless.toml"
        config_file.write_text(
            tomlkit.dumps(
                {
                    "output": {
                        "tmp_dir": str(tmp_path / "tmp"),
                        "data_dir": str(data_dir),
                        "log_file": str(data_dir / "logs" / "saneless.log"),
                    }
                }
            )
        )

        previous = os.umask(0o022)
        try:
            with _restored_logging():
                result = CliRunner().invoke(cli, ["--config", str(config_file), "jobs"])
        finally:
            os.umask(previous)

        assert result.exit_code == 0, result.output
        assert self._mode(data_dir) == 0o700
        assert self._mode(data_dir / "logs") == 0o700
        assert self._mode(data_dir / "logs" / "saneless.log") == 0o600

    def test_an_existing_data_dir_keeps_its_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A ``data_dir`` an earlier release left 0755 is not re-moded."""
        state_home = tmp_path / "state"
        monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
        data_dir = state_home / "saneless"
        data_dir.mkdir(parents=True)
        data_dir.chmod(0o755)

        previous = os.umask(0o022)
        try:
            with _restored_logging():
                result = CliRunner().invoke(cli, ["jobs"])
        finally:
            os.umask(previous)

        assert result.exit_code == 0, result.output
        assert self._mode(data_dir) == 0o755


class TestStartupConfigLog:
    """
    One INFO record says where the configuration came from.

    Names only, never values: a token supplied through the environment, or
    typed under a misspelt key, never reaches a log record or the terminal.
    """

    def test_config_sources_logged_once_without_values(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The record names the file and ``paperless.token``, not the token."""
        secret = "tok-SECRET-51aa"
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", secret)
        config_file = _write_real_config(tmp_path)

        with (
            _restored_logging(),
            caplog.at_level(logging.INFO, logger="saneless.config"),
        ):
            result = CliRunner().invoke(
                cli, ["--config", str(config_file), "jobs", "--limit", "1"]
            )

        assert result.exit_code == 0, result.output
        messages = [record.getMessage() for record in caplog.records]
        startup = [message for message in messages if "Configuration:" in message]
        assert len(startup) == 1
        assert str(config_file) in startup[0]
        assert "paperless.token" in startup[0]
        assert all(secret not in message for message in messages)
        assert secret not in result.output
        assert secret not in (tmp_path / "logs" / "saneless.log").read_text()

    def test_mistyped_key_error_never_echoes_its_value(self, tmp_path: Path) -> None:
        """A token under a misspelt key is not printed with the error."""
        secret = "tok-SECRET-51ab"
        config_file = _write_real_config(tmp_path, paperless={"tokne": secret})

        with _restored_logging():
            result = CliRunner().invoke(cli, ["--config", str(config_file), "jobs"])

        assert result.exit_code == 2
        assert secret not in result.output
        assert secret not in result.stderr


_SANE_COMMANDS = ["scan", "devices", "auto-profiles", "serve"]
_LIBSANE_MISSING = (
    "libsane.so.1: cannot open shared object file: No such file or directory"
)


def _block_sane_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import sane`` raise ModuleNotFoundError, as with python-sane absent."""
    monkeypatch.setattr(sane_backend, "sane", None)
    monkeypatch.setitem(sys.modules, "sane", None)


def _break_libsane(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the python-sane import fail the way a missing libsane.so does."""

    def unloadable() -> None:
        raise ImportError(_LIBSANE_MISSING)

    monkeypatch.setattr(sane_backend, "_ensure_sane", unloadable)


class TestRequireSane:
    """
    Every SANE command refuses at once when python-sane cannot be imported.

    python-sane is a mandatory dependency, so a missing package or an
    unloadable libsane is a setup problem: one line naming the import's reason
    and the SANE development package to install, exit 2, before the config is
    loaded or the scanner touched. ``jobs`` does not need a scanner and must
    keep working, and ``--help`` never reaches a command body, so neither
    imports python-sane.
    """

    @pytest.mark.parametrize("command", _SANE_COMMANDS)
    @pytest.mark.parametrize(
        ("break_sane", "reason"),
        [
            (_block_sane_import, "import of sane halted"),
            (_break_libsane, "libsane.so.1"),
        ],
        ids=["module_missing", "libsane_missing"],
    )
    def test_python_sane_unavailable_exits_2_before_anything_else(
        self,
        monkeypatch: pytest.MonkeyPatch,
        command: str,
        break_sane: Callable[[pytest.MonkeyPatch], None],
        reason: str,
    ) -> None:
        """One install-hint line and exit 2; no config load, no scanner."""
        runner, settings = _patch_cli(monkeypatch)
        calls: list[str] = []

        def recording_load(*_args: object, **_kwargs: object) -> Settings:
            calls.append("load_settings")
            return settings

        class RecordingScanner:
            """A scanner class that only records being constructed."""

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                """Record the construction."""
                calls.append("SaneBackend")

        monkeypatch.setattr("saneless.cli.load_settings", recording_load)
        monkeypatch.setattr("saneless.cli.SaneBackend", RecordingScanner)
        monkeypatch.setattr("saneless.cli.require_sane", sane_backend.require_sane)
        break_sane(monkeypatch)

        result = runner.invoke(cli, [command])

        assert result.exit_code == 2, result.output
        lines = _failure_lines(result)
        assert len(lines) == 1, result.stderr
        assert "python-sane cannot be imported" in lines[0]
        assert reason in lines[0]
        assert "libsane-dev" in lines[0]
        assert "sane-backends-devel" in lines[0]
        assert "Traceback" not in result.output
        assert calls == []

    def test_jobs_needs_no_python_sane_on_a_fresh_install(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        ``jobs`` runs without python-sane and shows an empty history.

        On a fresh install the data directory does not exist yet: the command
        prints the header with no rows, exit 0, and creates nothing, because
        listing the history only reads it.
        """
        data_dir = tmp_path / "new"
        settings = _make_settings(
            tmp_path,
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                data_dir=str(data_dir),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        monkeypatch.setattr("saneless.cli.require_sane", sane_backend.require_sane)
        _block_sane_import(monkeypatch)

        result = runner.invoke(cli, ["jobs"])

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert len(lines) == 2, result.output
        assert lines[0].startswith("Timestamp")
        assert set(lines[1]) == {"-"}
        assert not data_dir.exists()

    @pytest.mark.parametrize("command", _SANE_COMMANDS)
    def test_help_runs_without_python_sane(
        self, monkeypatch: pytest.MonkeyPatch, command: str
    ) -> None:
        """``<command> --help`` exits 0 without checking for python-sane."""
        runner, _ = _patch_cli(monkeypatch)
        calls: list[str] = []

        def recording_require_sane() -> None:
            calls.append("require_sane")

        monkeypatch.setattr("saneless.cli.require_sane", recording_require_sane)
        _block_sane_import(monkeypatch)

        result = runner.invoke(cli, [command, "--help"])

        assert result.exit_code == 0, result.output
        assert f"Usage: cli {command}" in result.output
        assert calls == []
        assert sys.modules.get("sane") is None


class TestJobsCommand:
    """Jobs command tests."""

    @staticmethod
    def _settings_for(tmp_path: Path) -> Settings:
        """
        Build Settings whose data_dir - and therefore db_path - is tmp_path.

        Every test in this class populates the database by hand and then lets
        the CLI open it. Both sides must resolve to the same file, so the
        OutputConfig is built in exactly one place.
        """
        return _make_settings(
            tmp_path,
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                data_dir=str(tmp_path),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )

    def _populate_store(self, db_path: Path, count: int = 2) -> None:
        """Populate a JobStore at db_path with test jobs."""
        store = JobStore(db_path=db_path)
        for i in range(count):
            store.create_job(
                profile="default" if i % 2 == 0 else "photo",
                title=f"Test Document {i + 1}",
            )
        store.close()

    def _populate_one(self, db_path: Path, state: JobState, title: str) -> None:
        """Populate a JobStore at db_path with a single job in `state`."""
        store = JobStore(db_path=db_path)
        job = store.create_job(profile="default", title=title)
        store.update_state(job.id, state)
        store.close()

    def _populate_warned(self, db_path: Path, title: str) -> None:
        """Populate a JobStore at db_path with one DONE job that has a warning."""
        store = JobStore(db_path=db_path)
        job = store.create_job(profile="default", title=title)
        store.finish_job(
            job.id,
            JobState.DONE,
            JobResult(
                outcome=ScanOutcome.SUCCESS,
                warning=_WARNED_SENTENCE,
                pages_scanned=4,
                pages_removed=0,
                pages_uploaded=3,
            ),
        )
        store.close()

    def test_jobs_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Jobs on an empty history prints the header and rule, no rows, exit 0."""
        settings = self._settings_for(tmp_path)
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        # Create empty DB
        store = JobStore(db_path=settings.output.db_path)
        store.close()

        result = runner.invoke(cli, ["jobs"])
        assert result.exit_code == 0
        lines = result.output.splitlines()
        assert len(lines) == 2
        assert lines[0].split() == ["Timestamp", "Profile", "Title", "Status"]
        assert set(lines[1]) == {"-"}

    def test_jobs_table_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Jobs with 2 jobs shows table with Timestamp, Profile, Title, Status columns."""
        settings = self._settings_for(tmp_path)
        self._populate_store(settings.output.db_path, count=2)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])
        assert result.exit_code == 0
        assert "Timestamp" in result.output
        assert "Profile" in result.output
        assert "Title" in result.output
        assert "Status" in result.output
        assert "Test Document 1" in result.output
        assert "Test Document 2" in result.output
        # Humanised, matching the web UI's register: the table is for people,
        # `--json` is the machine contract and keeps the raw enum value.
        assert "Pending" in result.output
        assert "PENDING" not in result.output

    def test_jobs_json_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Jobs --json with 2 jobs returns valid JSON array."""
        settings = self._settings_for(tmp_path)
        self._populate_store(settings.output.db_path, count=2)
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
            assert "outcome" in item
            assert "warning" in item

    def test_jobs_table_shows_fallback_distinctly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A FALLBACK job reads "Saved to folder", not Complete and not Failed."""
        settings = self._settings_for(tmp_path)
        self._populate_one(settings.output.db_path, JobState.FALLBACK, "Fallback Doc")
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])
        assert result.exit_code == 0
        assert "Saved to folder" in result.output
        assert "Complete" not in result.output
        assert "Failed" not in result.output
        assert "FALLBACK" not in result.output

    def test_jobs_table_label_survives_a_narrow_terminal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The widest status label still fits, unwrapped, in 80 columns.

        Humanised labels are longer than the raw enum values, and "Saved to
        folder" is longer than `FALLBACK`, so the status column's reserve is
        derived from `state_label` -- and from the warned-upload label, the
        widest of all -- rather than hardcoded.
        """
        monkeypatch.setenv("COLUMNS", "80")
        settings = self._settings_for(tmp_path)
        self._populate_one(settings.output.db_path, JobState.FALLBACK, "Fallback Doc")
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])
        assert result.exit_code == 0
        lines = [line for line in result.output.strip().split("\n") if line.strip()]
        assert len(lines) == 3
        row = lines[2]
        assert row.endswith("Saved to folder")
        widest = max(*(len(state_label(s)) for s in JobState), len(WARNED_UPLOAD_LABEL))
        assert all(len(line) <= 80 for line in lines)
        # Every other label would fit too, not just this one.
        assert len(row) - len("Saved to folder") + widest <= 80

    def test_jobs_json_keeps_the_raw_state_value(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        `--json` is a machine contract: the state stays the raw enum value.

        Humanising it would silently break every script that compares against
        `"DONE"` or `"FALLBACK"`.
        """
        settings = self._settings_for(tmp_path)
        self._populate_one(settings.output.db_path, JobState.FALLBACK, "Fallback Doc")
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["state"] == "FALLBACK"
        assert "Saved to folder" not in result.output
        # update_state records no outcome or warning, so both read None.
        assert data[0]["outcome"] is None
        assert data[0]["warning"] is None

    def test_jobs_table_labels_a_warned_upload(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A DONE job with a warning reads "Uploaded with a warning", not Complete."""
        monkeypatch.setenv("COLUMNS", "80")
        settings = self._settings_for(tmp_path)
        self._populate_warned(settings.output.db_path, "Warned Doc")
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])
        assert result.exit_code == 0
        lines = [line for line in result.output.strip().split("\n") if line.strip()]
        assert len(lines) == 3
        assert lines[2].endswith(WARNED_UPLOAD_LABEL)
        assert "Complete" not in result.output
        assert all(len(line) <= 80 for line in lines)

    def test_jobs_table_width_fits_the_warned_label(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The warned label fits beside a title that fills its column.

        The title column takes whatever the status reserve leaves, so a reserve
        sized only for `state_label` would let a long title push the warned
        label past column 80.
        """
        monkeypatch.setenv("COLUMNS", "80")
        settings = self._settings_for(tmp_path)
        self._populate_warned(settings.output.db_path, "W" * 120)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])
        assert result.exit_code == 0
        lines = [line for line in result.output.strip().split("\n") if line.strip()]
        row = lines[2]
        assert row.endswith(job_label(JobState.DONE, _WARNED_SENTENCE))
        assert all(len(line) <= 80 for line in lines)

    @pytest.mark.parametrize(
        ("category", "label"),
        [
            (ErrorCategory.UNCONFIRMED_SEND, UNCONFIRMED_SEND_LABEL),
            (ErrorCategory.UNCONFIRMED_FILING, UNCONFIRMED_FILING_LABEL),
        ],
    )
    def test_jobs_table_labels_an_amber_failure_by_its_category(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        category: ErrorCategory,
        label: str,
    ) -> None:
        """
        A failure that may be in paperless-ngx is not listed as "Failed".

        The label is the one the web history cell shows for the same row, and
        it fits beside a title that fills its column at 80 columns.
        """
        monkeypatch.setenv("COLUMNS", "80")
        settings = self._settings_for(tmp_path)
        store = JobStore(db_path=settings.output.db_path)
        job = store.create_job(profile="default", title="W" * 120)
        store.update_state(
            job.id,
            JobState.ERROR,
            error="the poll ran out",
            error_category=category,
        )
        store.close()
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])
        assert result.exit_code == 0
        lines = [line for line in result.output.strip().split("\n") if line.strip()]
        assert len(lines) == 3
        assert lines[2].endswith(label)
        assert state_label(JobState.ERROR) not in result.output
        assert all(len(line) <= 80 for line in lines)

    def test_jobs_json_keeps_done_and_the_warning_for_a_warned_upload(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        `--json` keeps the raw DONE state and the warning for a warned upload.

        The warned label is derived from the state and the warning, so the
        machine contract carries both unchanged and never the label.
        """
        settings = self._settings_for(tmp_path)
        self._populate_warned(settings.output.db_path, "Warned Doc")
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["state"] == "DONE"
        assert data[0]["outcome"] == "SUCCESS"
        assert data[0]["warning"] == _WARNED_SENTENCE
        assert WARNED_UPLOAD_LABEL not in result.output

    def test_jobs_limit(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Jobs --limit 1 with 2 jobs in DB shows only 1 job."""
        settings = self._settings_for(tmp_path)
        self._populate_store(settings.output.db_path, count=2)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--limit", "1"])
        assert result.exit_code == 0
        # Should only have 1 data row (plus header and separator)
        lines = [line for line in result.output.strip().split("\n") if line.strip()]
        # Header + separator + 1 data row = 3 lines
        assert len(lines) == 3

    def test_jobs_help(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Jobs --help shows --json and --limit options."""
        runner, _ = _patch_cli(monkeypatch, settings=self._settings_for(tmp_path))

        result = runner.invoke(cli, ["jobs", "--help"])
        assert result.exit_code == 0
        assert "--json" in result.output
        assert "--limit" in result.output

    def test_jobs_json_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Jobs --json with no jobs outputs empty JSON array."""
        settings = self._settings_for(tmp_path)
        # Create empty DB
        store = JobStore(db_path=settings.output.db_path)
        store.close()
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == []

    def test_jobs_json_error_carries_the_full_stored_text(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        ``--json`` adds the stored error verbatim, host path included.

        The web page shows a failure only as a path-free sentence pointing
        here, so this output is where the full text lives. The field is
        additive: every key a script already reads is still present.
        """
        settings = self._settings_for(tmp_path)
        stored = "kept at /x/failed/a.pdf"
        store = JobStore(db_path=settings.output.db_path)
        job = store.create_job(profile="default", title="Failed Doc")
        store.update_state(job.id, JobState.ERROR, error=stored)
        store.close()
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--json"])

        assert result.exit_code == 0, result.output
        (row,) = json.loads(result.output)
        assert row["error"] == stored
        assert {
            "id",
            "profile",
            "title",
            "state",
            "created_at",
            "outcome",
            "warning",
        } <= row.keys()
        assert row["state"] == "ERROR"

    def test_jobs_json_error_is_null_for_a_job_without_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A job that never failed carries ``"error": null``, not a missing key."""
        settings = self._settings_for(tmp_path)
        self._populate_store(settings.output.db_path, count=1)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--json"])

        assert result.exit_code == 0, result.output
        (row,) = json.loads(result.output)
        assert "error" in row
        assert row["error"] is None

    def test_jobs_json_appends_the_counts_and_removed_positions(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The three counts and the removed positions follow every existing key.

        Appended, so a script reading the earlier keys, in their order, is
        unaffected.  The positions are 1-based scanned-page numbers, and mean
        nothing without the scanned count beside them.
        """
        settings = self._settings_for(tmp_path)
        store = JobStore(db_path=settings.output.db_path)
        job = store.create_job(profile="default", title="Blank backs")
        store.finish_job(
            job.id,
            JobState.DONE,
            result=JobResult(
                outcome=ScanOutcome.SUCCESS,
                warning=None,
                pages_scanned=4,
                pages_removed=2,
                pages_uploaded=2,
                removed_positions=(2, 4),
            ),
        )
        store.close()
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--json"])

        assert result.exit_code == 0, result.output
        (row,) = json.loads(result.output)
        assert list(row) == [
            "id",
            "profile",
            "title",
            "state",
            "created_at",
            "outcome",
            "warning",
            "error",
            "pages_scanned",
            "pages_removed",
            "pages_uploaded",
            "pages_removed_positions",
        ]
        assert row["pages_scanned"] == 4
        assert row["pages_removed"] == 2
        assert row["pages_uploaded"] == 2
        assert row["pages_removed_positions"] == [2, 4]
        # Information, never a warning: a DONE with blank backs stays plain.
        assert row["warning"] is None

    def test_jobs_json_counts_and_positions_are_null_when_never_recorded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A job that counted nothing carries ``null`` for all four, not 0 or []."""
        settings = self._settings_for(tmp_path)
        self._populate_store(settings.output.db_path, count=1)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--json"])

        assert result.exit_code == 0, result.output
        (row,) = json.loads(result.output)
        for key in (
            "pages_scanned",
            "pages_removed",
            "pages_uploaded",
            "pages_removed_positions",
        ):
            assert key in row
            assert row[key] is None, key

    @staticmethod
    def _settings_in(tmp_path: Path, data_dir: Path) -> Settings:
        """Build Settings whose data_dir is ``data_dir``, everything else in tmp_path."""
        return _make_settings(
            tmp_path,
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                data_dir=str(data_dir),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )

    @pytest.mark.parametrize("limit", ["-1", "0"])
    def test_jobs_rejects_a_non_positive_limit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, limit: str
    ) -> None:
        """
        ``--limit`` below 1 is a usage error, exit 2.

        SQLite reads a negative LIMIT as no limit at all, so ``-1`` would print
        the whole history, and ``0`` would ask for nothing.
        """
        settings = self._settings_for(tmp_path)
        self._populate_store(settings.output.db_path, count=3)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--limit", limit])

        assert result.exit_code == ExitCode.CONFIG, result.output
        assert "Invalid value for '--limit'" in result.stderr
        assert "Test Document" not in result.stdout

    def test_jobs_refuses_an_older_database_and_leaves_it_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A history at an older schema is refused, and not upgraded behind a server.

        Listing the history is a read.  Upgrading the database is the job of
        the ``saneless serve`` that owns it, so ``jobs`` names that and exits
        2, and the file's bytes and its stamped version are as they were.
        """
        settings = self._settings_for(tmp_path)
        db_path = settings.output.db_path
        conn = sqlite3.connect(db_path)
        try:
            # The first two steps of the real ladder, as a version 2 release
            # left the database.
            for step in job_module._MIGRATIONS[:2]:
                step(conn, str(db_path))
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        finally:
            conn.close()
        before = hashlib.sha256(db_path.read_bytes()).hexdigest()
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])

        assert result.exit_code == ExitCode.CONFIG, result.output
        assert "schema version 2" in result.stderr
        assert "saneless serve" in result.stderr
        assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone() == (2,)
        finally:
            conn.close()

    @pytest.mark.parametrize("as_json", [False, True], ids=["table", "json"])
    @pytest.mark.parametrize("data_dir_exists", [True, False], ids=["dir", "no-dir"])
    def test_jobs_without_a_database_creates_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        as_json: bool,
        data_dir_exists: bool,
    ) -> None:
        """
        No history yet is an empty listing, and leaves no file behind.

        Neither the database nor its ``-wal`` and ``-shm`` files are created,
        and a ``data_dir`` that does not exist yet still does not: it is the
        server's and the scan's to create.
        """
        data_dir = tmp_path / "data"
        if data_dir_exists:
            data_dir.mkdir()
        settings = self._settings_in(tmp_path, data_dir)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", *(["--json"] if as_json else [])])

        assert result.exit_code == 0, result.output
        if as_json:
            assert json.loads(result.stdout) == []
        else:
            assert result.stdout.splitlines()[0].startswith("Timestamp")
            assert len(result.stdout.splitlines()) == 2
        assert data_dir.exists() is data_dir_exists
        db_path = settings.output.db_path
        for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            assert not path.exists(), path

    @pytest.mark.parametrize("as_json", [False, True], ids=["table", "json"])
    def test_jobs_with_a_database_it_cannot_reach_exits_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        as_json: bool,
    ) -> None:
        """
        A database path that cannot be looked up is a storage error, exit 2.

        Only a database that is not there is an empty history.  A path the
        lookup fails on for any other reason -- here a symlink that loops --
        is not, and an empty table, or ``[]`` on the JSON contract, would tell
        a script there are no jobs.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        settings = self._settings_in(tmp_path, data_dir)
        db_path = settings.output.db_path
        db_path.symlink_to(db_path.name)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", *(["--json"] if as_json else [])])

        assert result.exit_code == ExitCode.CONFIG, result.output
        assert result.stdout == ""
        assert str(db_path) in result.stderr

    def test_jobs_with_a_file_for_data_dir_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A data_dir that is a regular file is a configuration error, exit 2."""
        data_dir = tmp_path / "data"
        data_dir.write_text("not a directory\n")
        runner, _ = _patch_cli(
            monkeypatch, settings=self._settings_in(tmp_path, data_dir)
        )

        result = runner.invoke(cli, ["jobs"])

        assert result.exit_code == ExitCode.CONFIG, result.output
        assert "data_dir is not a directory" in result.stderr
        assert data_dir.read_text() == "not a directory\n"


_UVICORN_LOGGERS = ("uvicorn.error", "uvicorn.access", "uvicorn.asgi")

# The longest a serve test waits for a thread it started to reach a point.
_THREAD_JOIN_TIMEOUT = 5.0


@pytest.fixture
def uvicorn_loggers_restored() -> Generator[None]:
    """
    Put uvicorn's logger levels back after a test that built a real Config.

    ``uvicorn.Config`` sets the level of three uvicorn loggers when it is
    constructed, and those loggers are process-global.
    """
    levels = {name: logging.getLogger(name).level for name in _UVICORN_LOGGERS}
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


def _free_loopback_port() -> int:
    """
    Return a loopback port that was free a moment ago.

    Returns:
        An ephemeral port number on 127.0.0.1, released again.

    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _ipv6_loopback_available() -> bool:
    """
    Report whether this host can bind a socket on ``::1``.

    ``socket.has_ipv6`` only says Python was built with IPv6; a container can
    still have it switched off, so the test binds an ephemeral port to find out.

    Returns:
        True when ``::1`` accepts a bind.

    """
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


@dataclasses.dataclass
class _ServerRun:
    """What ``serve`` handed ``uvicorn.Server.run``, read while it was open."""

    sockets: list[socket.socket] | None
    config: uvicorn.Config
    family: int | None = None
    port: int | None = None
    v6only: int | None = None
    reuseaddr: int | None = None
    reuseport: int | None = None
    listening: int | None = None
    bound: list[tuple[int, str, int]] = dataclasses.field(default_factory=list)
    v6only_flags: list[int] = dataclasses.field(default_factory=list)


def _read_socket(run: _ServerRun, sock: socket.socket) -> None:
    """
    Record the facts about the handed-over socket that the tests assert on.

    Args:
        run: The record to fill in.
        sock: The first socket ``serve`` passed to ``Server.run``.

    """
    run.family = sock.family
    run.port = sock.getsockname()[1]
    run.reuseaddr = sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
    if hasattr(socket, "SO_REUSEPORT"):
        run.reuseport = sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT)
    if hasattr(socket, "SO_ACCEPTCONN"):
        run.listening = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
    if sock.family == socket.AF_INET6:
        run.v6only = sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY)


def _fake_server_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    started: bool = True,
    then: BaseException | None = None,
) -> list[_ServerRun]:
    """
    Replace ``uvicorn.Server.run`` with a recorder that serves nothing.

    The recorder reads the socket it was given, closes the app's eagerly
    opened JobStore so no test leaks one, sets ``started`` the way a real
    start-up would, and then raises ``then`` if one was given.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        started: What ``server.started`` reads once the fake run returns.
        then: An exception to raise after recording, such as the
            KeyboardInterrupt uvicorn re-raises after a graceful stop.

    Returns:
        The list each call's record is appended to.

    """
    runs: list[_ServerRun] = []

    def fake_run(
        self: uvicorn.Server, sockets: list[socket.socket] | None = None
    ) -> None:
        run = _ServerRun(sockets=sockets, config=self.config)
        if sockets:
            _read_socket(run, sockets[0])
            for sock in sockets:
                address, port = sock.getsockname()[:2]
                run.bound.append((sock.family, address, port))
                if sock.family == socket.AF_INET6:
                    run.v6only_flags.append(
                        sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY)
                    )
        runs.append(run)
        app = self.config.app
        if isinstance(app, FastAPI):
            store: object = services_of(app).job_store
            if isinstance(store, JobStore):
                store.close()
        self.started = started
        if then is not None:
            raise then

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    return runs


def _resolving_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> None:
    """
    Make the resolver ``serve`` uses answer any name with ``addresses``.

    The answer keeps the order given and the port asked for, the way
    ``getaddrinfo`` does, so a test decides which families a name has
    whatever this host's own resolver would say.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        addresses: The IPv4 or IPv6 literals to answer with, in order.

    """

    def fake_getaddrinfo(
        _host: str, port: int, **_kwargs: int
    ) -> list[tuple[int, int, int, str, tuple[str, int] | tuple[str, int, int, int]]]:
        infos: list[
            tuple[int, int, int, str, tuple[str, int] | tuple[str, int, int, int]]
        ] = []
        for address in addresses:
            if ":" in address:
                infos.append(
                    (
                        socket.AF_INET6,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        (address, port, 0, 0),
                    )
                )
            else:
                infos.append(
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        (address, port),
                    )
                )
        return infos

    monkeypatch.setattr("saneless.cli.socket.getaddrinfo", fake_getaddrinfo)


def _record_sockets(monkeypatch: pytest.MonkeyPatch) -> list[socket.socket]:
    """
    Record every socket ``serve`` creates, so a test can check each is closed.

    Args:
        monkeypatch: The test's monkeypatch fixture.

    Returns:
        The list each new socket is appended to.

    """
    made: list[socket.socket] = []

    class RecordingSocket(socket.socket):
        """A real socket that notes itself in ``made`` when it is created."""

        def __init__(self, *args: int) -> None:
            super().__init__(*args)
            made.append(self)

    monkeypatch.setattr("saneless.cli.socket.socket", RecordingSocket)
    return made


@contextlib.asynccontextmanager
async def _refusing_lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """
    Refuse to start, the way a lifespan with a failed pre-flight would.

    The ``yield`` below the raise is unreachable on purpose: it is what makes
    this function an async generator, which is what ``asynccontextmanager``
    needs to accept it at all.

    Args:
        _app: The app FastAPI hands to its lifespan; this one ignores it.

    Yields:
        Nothing -- the raise happens before the yield is ever reached.

    """
    msg = "the lifespan refuses to start"
    raise RuntimeError(msg)
    yield


def _invoke_on_a_worker_thread(runner: CliRunner, args: list[str]) -> Result:
    """
    Invoke the CLI on a worker thread, the way a fresh process would run it.

    ``uvicorn.Server.run`` calls ``asyncio.run``, which refuses to start on a
    thread that already has a running event loop. A real ``saneless serve``
    process never does. The suite's main thread does: ``tests/test_browser.py``
    holds Playwright's sync dispatcher loop open on it for the rest of the
    session, so any test reaching real uvicorn on the main thread after the
    browser module has run reports the CLI's unexpected-error code instead of
    the code under test. That is an ordering artefact, not CLI behaviour, and
    it makes the exit-code assertion pass alone and fail in a full run.

    Handing the invocation to a worker thread restores the production
    precondition without stubbing uvicorn or weakening what is asserted.

    The thread is a daemon joined with a timeout below pytest's own, so a
    worker that wedges inside uvicorn fails this test with the message below
    instead of hanging the run: a pool's ``shutdown(wait=True)`` would rejoin
    the stuck worker and a non-daemon thread would hold interpreter exit open.

    Args:
        runner: The CliRunner to invoke with.
        args: The command line to pass to ``cli``.

    Returns:
        The Result the runner produced on the worker thread.

    """
    outcome: list[Result] = []
    thread = threading.Thread(
        target=lambda: outcome.append(runner.invoke(cli, args)), daemon=True
    )
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "the CLI invocation did not finish"
    assert outcome, "the CLI invocation produced no result"
    return outcome[0]


@dataclasses.dataclass
class _HeldRefresherChecks:
    """
    The refresher's scanner and Paperless checks, each held on an Event.

    Neither looks at the stop: each waits for its release, as a request that
    cannot be cut short does, so only what the refresher does between checks
    decides whether a stop ends its run.
    """

    scanner_entered: threading.Event = dataclasses.field(
        default_factory=threading.Event
    )
    scanner_release: threading.Event = dataclasses.field(
        default_factory=threading.Event
    )
    paperless_entered: threading.Event = dataclasses.field(
        default_factory=threading.Event
    )
    paperless_release: threading.Event = dataclasses.field(
        default_factory=threading.Event
    )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Put both held checks in place of the real ones."""

        def held_scanner_check(
            _context: CheckContext, _gate: threading.Lock
        ) -> CheckResult:
            self.scanner_entered.set()
            self.scanner_release.wait(_THREAD_JOIN_TIMEOUT)
            return CheckResult(key=CheckKey.SCANNER, state=CheckState.OK, message="")

        def held_paperless_check(_context: CheckContext) -> CheckResult:
            self.paperless_entered.set()
            self.paperless_release.wait(_THREAD_JOIN_TIMEOUT)
            return CheckResult(key=CheckKey.PAPERLESS, state=CheckState.OK, message="")

        monkeypatch.setattr(checks_module, "_scanner_result", held_scanner_check)
        monkeypatch.setattr(checks_module, "_check_paperless", held_paperless_check)

    def release(self) -> None:
        """Let both checks finish, so no thread a test started is left waiting."""
        self.scanner_release.set()
        self.paperless_release.set()


@dataclasses.dataclass
class _RecordedCloses:
    """The closes the lifespan ran, and the real ones it may have skipped."""

    calls: list[str]
    skipped: list[Callable[[], None]]

    def release_leftovers(self) -> None:
        """Close the job store and Paperless client if the lifespan did not."""
        if not self.calls:
            for close in self.skipped:
                close()


def _record_closes(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI, scanner: StubScannerBackend
) -> _RecordedCloses:
    """
    Record the lifespan's three closes, in order, still running the real two.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        app: The app whose job store and Paperless client are watched.
        scanner: The scanner backend the app was built with.

    Returns:
        The record the closes are appended to.

    """
    store: JobStore = services_of(app).job_store
    paperless = services_of(app).paperless
    real_store_close = store.close
    real_paperless_close = paperless.close
    closes = _RecordedCloses(calls=[], skipped=[real_paperless_close, real_store_close])

    def recording_store_close() -> None:
        closes.calls.append("job_store.close")
        real_store_close()

    def recording_paperless_close() -> None:
        closes.calls.append("paperless.close")
        real_paperless_close()

    def recording_scanner_close() -> None:
        closes.calls.append("scanner.close")

    monkeypatch.setattr(store, "close", recording_store_close)
    monkeypatch.setattr(paperless, "close", recording_paperless_close)
    monkeypatch.setattr(scanner, "close", recording_scanner_close)
    return closes


@pytest.mark.usefixtures("uvicorn_loggers_restored")
class TestServeCommand:
    """
    Serve command tests.

    ``serve`` binds its own socket and hands it to uvicorn, so these tests bind
    for real, but only on the loopback address and an OS-chosen port, and
    replace ``uvicorn.Server.run`` so nothing is served.
    """

    @staticmethod
    def _loopback_settings(tmp_path: Path, port: int = 0) -> Settings:
        """Build settings that serve on 127.0.0.1 and, by default, port 0."""
        settings = _make_settings(tmp_path)
        settings.output.web_host = "127.0.0.1"
        settings.output.web_port = port
        return settings

    @staticmethod
    def _stub_create_app(monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace create_app with a stub, so no test leaves a JobStore open."""

        def fake_create_app(*_args: object, **_kwargs: object) -> object:
            return object()

        monkeypatch.setattr("saneless.web.app.create_app", fake_create_app)

    @staticmethod
    def _refusing_create_app(monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace create_app with a real app whose lifespan refuses to start."""

        def failing_create_app(*_args: object, **_kwargs: object) -> FastAPI:
            return FastAPI(lifespan=_refusing_lifespan)

        monkeypatch.setattr("saneless.web.app.create_app", failing_create_app)

    def test_serve_binds_the_configured_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        With no flags, the configured ``0.0.0.0:8080`` is what gets resolved.

        The resolver is wrapped so the real bind lands on an ephemeral loopback
        port: a unit test must not take 8080 on every interface.
        """
        real_getaddrinfo = socket.getaddrinfo
        asked: list[tuple[str | bytes | None, str | int | None]] = []

        def recording_getaddrinfo(
            host: str | bytes | None, port: str | int | None, **kwargs: int
        ) -> list:
            asked.append((host, port))
            return real_getaddrinfo("127.0.0.1", 0, **kwargs)

        monkeypatch.setattr("saneless.cli.socket.getaddrinfo", recording_getaddrinfo)
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 0, result.output
        assert asked == [("0.0.0.0", 8080)]
        [run] = runs
        assert run.config.log_config is None
        assert run.config.access_log is True

    def test_serve_bounds_the_graceful_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A stop waits 8 s for requests to finish, per uvicorn's config.

        An int, because uvicorn types the setting ``int | None``.  The drain
        is long enough for one request-path call to paperless-ngx, whose
        connect and read budgets come to 7 s, to end inside it, so the
        lifespan does not close the Paperless client under that request.  An
        idle server has no request to wait for, so its stop still fits
        inside Docker's default 10 s grace period.
        """
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve", "--host", "127.0.0.1", "--port", "0"])

        assert result.exit_code == 0, result.output
        [run] = runs
        bound = run.config.timeout_graceful_shutdown
        assert isinstance(bound, int)
        assert bound > PROBE_CONNECT_SECONDS + PROBE_READ_SECONDS
        assert bound == 8

    def test_a_stop_ends_the_refreshers_run_before_its_paperless_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A stop tells the refresher at once, so the drain does not let it go on.

        The stop lands during the refresher's scanner check, which finishes in
        the request drain; the Paperless check after it never ends here. Told to
        stop only after the drain, the refresher would be inside that request,
        miss its join, and the lifespan would close nothing. Told at once, it
        ends its run after the scanner check and all three closes run.

        uvicorn is replaced by a run that does what it does around a SIGTERM:
        start the lifespan, hand the signal to ``handle_exit``, wait out the
        drain, then shut the lifespan down. The join is cut to 1 s so a broken
        stop fails fast.
        """
        monkeypatch.setattr(app_module, "STOP_JOIN_SECONDS", 1.0)
        held = _HeldRefresherChecks()
        held.install(monkeypatch)
        scanner = StubScannerBackend()
        app = create_app(self._loopback_settings(tmp_path), scanner)
        refresher: CheckRefresher = services_of(app).refresher
        closes = _record_closes(monkeypatch, app, scanner)

        def stopped_run(
            self: uvicorn.Server, sockets: list[socket.socket] | None = None
        ) -> None:
            del sockets
            with TestClient(app):
                self.started = True
                refresher.note_watcher()
                assert held.scanner_entered.wait(_THREAD_JOIN_TIMEOUT)
                self.handle_exit(signal.SIGTERM, None)
                held.scanner_release.set()
                # The drain: requests still being answered keep the lifespan
                # waiting until the refresher has either gone on to the
                # Paperless check or ended its run.
                assert poll_until(
                    lambda: (
                        held.paperless_entered.is_set() or not refresher.probe_in_flight
                    ),
                    _THREAD_JOIN_TIMEOUT,
                )

        monkeypatch.setattr(uvicorn.Server, "run", stopped_run)
        sock = socket.create_server(("127.0.0.1", 0))
        try:
            server_module.run_server(app, [sock], "WARNING")
            assert not held.paperless_entered.is_set()
            assert closes.calls == [
                "paperless.close",
                "job_store.close",
                "scanner.close",
            ]
        finally:
            sock.close()
            held.release()
            assert refresher.stop(timeout=_THREAD_JOIN_TIMEOUT) is True
            closes.release_leftovers()

    def test_a_sigterm_before_uvicorn_takes_it_also_tells_the_refresher(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``serve``'s own SIGTERM handler stops the refresher as well as the server.

        It is in place around uvicorn's run, so it is what a SIGTERM reaches
        before uvicorn has installed its own handler.  The run below is a
        stand-in that installs none, so the signal it raises lands there.
        """
        assert threading.current_thread() is threading.main_thread()
        app = create_app(self._loopback_settings(tmp_path), StubScannerBackend())
        refresher: CheckRefresher = services_of(app).refresher
        asked: list[str] = []
        should_exit: list[bool] = []

        def recording_note_stop() -> None:
            asked.append("note_stop")

        def signalled_run(
            self: uvicorn.Server, sockets: list[socket.socket] | None = None
        ) -> None:
            del sockets
            signal.raise_signal(signal.SIGTERM)
            should_exit.append(self.should_exit)
            self.started = True

        monkeypatch.setattr(refresher, "note_stop", recording_note_stop)
        monkeypatch.setattr(uvicorn.Server, "run", signalled_run)
        sock = socket.create_server(("127.0.0.1", 0))
        try:
            server_module.run_server(app, [sock], "WARNING")
            assert should_exit == [True]
            assert asked == ["note_stop"]
        finally:
            sock.close()
            services_of(app).paperless.close()
            services_of(app).job_store.close()

    def test_an_inherited_ignored_sigterm_still_stops_serve_and_is_put_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``serve`` stops on SIGTERM even when it was started with SIGTERM ignored.

        uvicorn replaces an ignored SIGTERM with its own handler while it
        runs, so ``serve``'s handler replaces it too, and a SIGTERM before
        uvicorn's handler is in place stops the server as one during the run
        would.  The ignored disposition is put back once the run returns.
        """
        assert threading.current_thread() is threading.main_thread()
        app = create_app(self._loopback_settings(tmp_path), StubScannerBackend())
        refresher: CheckRefresher = services_of(app).refresher
        should_exit: list[bool] = []

        def signalled_run(
            self: uvicorn.Server, sockets: list[socket.socket] | None = None
        ) -> None:
            del sockets
            signal.raise_signal(signal.SIGTERM)
            should_exit.append(self.should_exit)
            self.started = True

        monkeypatch.setattr(refresher, "note_stop", lambda: None)
        monkeypatch.setattr(uvicorn.Server, "run", signalled_run)
        previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        sock = socket.create_server(("127.0.0.1", 0))
        try:
            server_module.run_server(app, [sock], "WARNING")
            assert should_exit == [True]
            assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
        finally:
            signal.signal(signal.SIGTERM, previous)
            sock.close()
            services_of(app).paperless.close()
            services_of(app).job_store.close()

    @pytest.mark.parametrize(
        "stopping",
        [
            pytest.param(True, id="during-the-shutdown"),
            pytest.param(False, id="after-a-failed-start-up"),
        ],
    )
    def test_a_stop_signal_inside_the_refreshers_stop_does_not_hang(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, stopping: bool
    ) -> None:
        """
        A signal that lands while the main thread stops the refresher is safe.

        The lifespan stops the refresher on the main thread, at shutdown and
        after a failed start-up, and uvicorn's signal handler runs on that same
        thread. ``threading.Event.set`` holds a non-reentrant lock, so a handler
        setting the same Event from inside that call would wait for ever on the
        frame it interrupted. After a failed start-up ``should_exit`` is unset,
        so the handler cannot tell the two cases apart by it.

        The patched ``set`` stands in for that lock: a second ``set`` beginning
        inside the first is recorded instead of blocking, and the handler is
        called from inside the first, as a signal arriving there would run it.
        """
        app = create_app(self._loopback_settings(tmp_path), StubScannerBackend())
        refresher: CheckRefresher = services_of(app).refresher
        # serve's own config: no log_config, so uvicorn rewires no loggers
        # for the rest of the session.
        server = server_module.StoppingServer(uvicorn.Config(app, log_config=None), app)
        server.should_exit = stopping
        stop_event = refresher._stopping
        real_set = threading.Event.set
        inside = threading.Lock()
        reentered: list[str] = []
        signalled: list[signal.Signals] = []

        def interrupted_set(event: threading.Event) -> None:
            if event is not stop_event:
                real_set(event)
                return
            if not inside.acquire(blocking=False):
                reentered.append("set")
                return
            try:
                if not signalled:
                    signalled.append(signal.SIGTERM)
                    server.handle_exit(signal.SIGTERM, None)
                real_set(event)
            finally:
                inside.release()

        monkeypatch.setattr(threading.Event, "set", interrupted_set)
        try:
            refresher.request_stop()
        finally:
            monkeypatch.undo()
            services_of(app).paperless.close()
            services_of(app).job_store.close()
        assert signalled == [signal.SIGTERM]
        assert reentered == []
        assert server.should_exit is True
        assert stop_event.is_set() is True

    def test_serve_port_0_binds_an_os_chosen_port_and_reports_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``--port 0`` binds a real port, and the line on stderr names that port.

        The address goes to stderr, so stdout stays clean.
        """
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve", "--host", "127.0.0.1", "--port", "0"])

        assert result.exit_code == 0, result.output
        [run] = runs
        assert run.family == socket.AF_INET
        assert run.port
        assert result.stderr.splitlines() == [f"Serving on http://127.0.0.1:{run.port}"]
        assert "Serving on" not in result.stdout

    def test_serve_explicit_port_0_is_not_replaced_by_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An explicit ``--port 0`` is a request, not a missing value."""
        free_port = _free_loopback_port()
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(
            monkeypatch, settings=self._loopback_settings(tmp_path, port=free_port)
        )

        result = runner.invoke(cli, ["serve", "--port", "0"])

        assert result.exit_code == 0, result.output
        [run] = runs
        assert run.port
        assert run.port != free_port

    def test_serve_configured_port_is_used_without_a_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With no ``--port``, the configured port is the one bound."""
        free_port = _free_loopback_port()
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(
            monkeypatch, settings=self._loopback_settings(tmp_path, port=free_port)
        )

        result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 0, result.output
        assert [run.port for run in runs] == [free_port]

    @pytest.mark.skipif(
        not _ipv6_loopback_available(), reason="this host cannot bind ::1"
    )
    def test_serve_binds_an_ipv6_address_and_brackets_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``--host ::1`` binds IPv6, restricted to IPv6, and prints a usable URL.

        IPV6_V6ONLY keeps ``--host ::`` from quietly listening on IPv4 too.
        """
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve", "--host", "::1", "--port", "0"])

        assert result.exit_code == 0, result.output
        [run] = runs
        assert run.family == socket.AF_INET6
        assert run.v6only == 1
        assert result.stderr.splitlines() == [f"Serving on http://[::1]:{run.port}"]

    @pytest.mark.skipif(
        not _ipv6_loopback_available(), reason="this host cannot bind ::1"
    )
    def test_serve_binds_every_address_a_name_resolves_to(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``--host localhost`` listens on ``::1`` and ``127.0.0.1``, one port.

        A resolver lists ``::1`` first on a typical host.  Binding only that
        would refuse a reverse proxy pointed at ``127.0.0.1``; binding every
        address matches uvicorn binding the name itself.  With port 0 the OS
        picks the port once and every address shares it.
        """
        _resolving_to(monkeypatch, "::1", "127.0.0.1")
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve", "--host", "localhost", "--port", "0"])

        assert result.exit_code == 0, result.output
        [run] = runs
        [(_, _, port), _] = run.bound
        assert run.bound == [
            (socket.AF_INET6, "::1", port),
            (socket.AF_INET, "127.0.0.1", port),
        ]
        assert run.v6only_flags == [1]
        assert result.stderr.splitlines() == [
            f"Serving on http://[::1]:{port}",
            f"Serving on http://127.0.0.1:{port}",
        ]
        assert run.sockets is not None
        assert all(sock.fileno() == -1 for sock in run.sockets)

    def test_serve_binds_a_repeated_address_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An address the resolver lists twice is bound once, not refused."""
        _resolving_to(monkeypatch, "127.0.0.1", "127.0.0.1")
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve", "--host", "localhost", "--port", "0"])

        assert result.exit_code == 0, result.output
        [run] = runs
        [(family, address, port)] = run.bound
        assert (family, address) == (socket.AF_INET, "127.0.0.1")
        assert result.stderr.splitlines() == [f"Serving on http://127.0.0.1:{port}"]

    def test_serve_closes_the_bound_sockets_when_a_later_address_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        One address that cannot be bound fails ``serve``: exit 2, nothing leaks.

        The first address is bound before the second fails, so its socket
        must be closed on the way out.  192.0.2.1 is a documentation address
        no host holds, so binding it fails with EADDRNOTAVAIL.
        """
        _resolving_to(monkeypatch, "127.0.0.1", "192.0.2.1")
        made = _record_sockets(monkeypatch)
        self._stub_create_app(monkeypatch)
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve", "--host", "twohomed", "--port", "0"])

        assert result.exit_code == 2, result.output
        [line] = _failure_lines(result)
        assert line.startswith("Cannot bind to twohomed:0 (192.0.2.1): ")
        assert runs == []
        assert len(made) == 2
        assert all(sock.fileno() == -1 for sock in made)

    def test_serve_closes_the_sockets_when_start_up_raises_after_binding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A failure between the bind and ``Server.run`` still closes the sockets.

        Building uvicorn's config is the last step before the hand-over; a
        bug there is exit 5, and the socket it never reached is closed.
        """

        def failing_config(*_args: object, **_kwargs: object) -> NoReturn:
            msg = "config exploded"
            raise RuntimeError(msg)

        made = _record_sockets(monkeypatch)
        self._stub_create_app(monkeypatch)
        monkeypatch.setattr("saneless.web.server.uvicorn.Config", failing_config)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve", "--host", "127.0.0.1", "--port", "0"])

        assert result.exit_code == 5, result.output
        assert len(made) == 1
        assert made[0].fileno() == -1

    def test_serve_port_conflict_fails_before_sane_is_initialised(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A port that is taken is found before SANE is touched.

        Binding first means a setup mistake costs no SANE start-up, and leaves
        no initialised backend behind that no lifespan will ever close.
        """
        scanner_cls, built = _closing_scanner()
        runs = _fake_server_run(monkeypatch)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            taken = holder.getsockname()[1]
            runner, _ = _patch_cli(
                monkeypatch,
                settings=self._loopback_settings(tmp_path, port=taken),
                scanner_cls=scanner_cls,
            )

            result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 2, result.output
        assert built == []
        assert runs == []

    def test_serve_closes_the_sockets_when_sane_fails_to_initialise(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """SANE failing after the bind still closes the bound socket: exit 2."""

        class FailingSaneBackend:
            """A backend whose construction fails the way ``sane.init()`` does."""

            def __init__(self, host: str = "") -> None:
                msg = "Could not initialise SANE: Error during device I/O"
                raise ScanError(msg)

        made = _record_sockets(monkeypatch)
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(
            monkeypatch,
            settings=self._loopback_settings(tmp_path),
            scanner_cls=FailingSaneBackend,
        )

        result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 2, result.output
        assert runs == []
        assert len(made) == 1
        assert made[0].fileno() == -1

    def test_serve_hands_over_a_listening_socket_without_reuseport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The socket is already listening, reuses addresses, and never shares a port.

        Listening before the hand-over means a connection made during start-up
        queues rather than being refused. SO_REUSEADDR matches what uvicorn's
        own bind set; SO_REUSEPORT would let a second process listen on the
        same port, so it is never set.
        """
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve", "--host", "127.0.0.1", "--port", "0"])

        assert result.exit_code == 0, result.output
        [run] = runs
        assert run.sockets is not None
        assert len(run.sockets) == 1
        assert run.reuseaddr
        assert run.reuseport in {None, 0}
        assert run.listening in {None, 1}

    def test_serve_closes_the_socket_once_the_server_returns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The listening socket does not outlive the command."""
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve", "--host", "127.0.0.1", "--port", "0"])

        assert result.exit_code == 0, result.output
        [run] = runs
        assert run.sockets is not None
        assert run.sockets[0].fileno() == -1

    def test_serve_log_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Serve passes log_level from settings to uvicorn."""
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve", "--host", "127.0.0.1", "--port", "0"])

        assert result.exit_code == 0, result.output
        assert [run.config.log_level for run in runs] == ["info"]

    def test_serve_log_level_ignores_verbose(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        With ``-v`` uvicorn still gets the configured level, not ``debug``.

        ``-v`` is saneless's own detail; turning uvicorn up with it would drag
        its and httpx2's debug output into the log.
        """
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(
            cli, ["-v", "serve", "--host", "127.0.0.1", "--port", "0"]
        )

        assert result.exit_code == 0, result.output
        assert [run.config.log_level for run in runs] == ["info"]

    def test_serve_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Serve --help shows --host and --port options."""
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--host" in result.output
        assert "--port" in result.output

    def test_serve_receives_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Serve hands uvicorn the FastAPI app it built."""
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve", "--host", "127.0.0.1", "--port", "0"])

        assert result.exit_code == 0, result.output
        [run] = runs
        assert isinstance(run.config.app, FastAPI)

    def test_serve_bind_failure_exits_2_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A port that is already taken is a setup problem: one line, exit 2.

        The port is held by a listening socket of the test's own, which no
        SO_REUSEADDR lets a second socket bind.
        """
        self._stub_create_app(monkeypatch)
        runs = _fake_server_run(monkeypatch)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            taken = holder.getsockname()[1]
            runner, _ = _patch_cli(
                monkeypatch, settings=self._loopback_settings(tmp_path, port=taken)
            )

            result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 2, result.output
        assert _failure_lines(result) == [
            f"Cannot bind to 127.0.0.1:{taken}: [Errno {errno.EADDRINUSE}] "
            f"{os.strerror(errno.EADDRINUSE)}"
        ]
        assert runs == []

    @pytest.mark.parametrize("port", ["-1", "65536", "70000"])
    def test_serve_out_of_range_port_is_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, port: str
    ) -> None:
        """
        ``--port`` outside 0-65535 is refused before anything is bound.

        The resolver would otherwise wrap it into a different real port.
        """
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["serve", "--host", "127.0.0.1", "--port", port])

        assert result.exit_code == 2, result.output
        assert "--port" in result.stderr
        assert "0<=x<=65535" in result.stderr
        assert runs == []

    def test_serve_unresolvable_host_exits_2_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A host the resolver cannot turn into an address is exit 2, one line."""

        def failing_getaddrinfo(*_args: object, **_kwargs: object) -> NoReturn:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        monkeypatch.setattr("saneless.cli.socket.getaddrinfo", failing_getaddrinfo)
        self._stub_create_app(monkeypatch)
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(
            cli, ["serve", "--host", "scanner.invalid", "--port", "0"]
        )

        assert result.exit_code == 2, result.output
        assert _failure_lines(result) == [
            f"Cannot bind to scanner.invalid:0: [Errno {socket.EAI_NONAME}] "
            "Name or service not known"
        ]
        assert "Traceback" not in result.output
        assert runs == []

    def test_serve_startup_failure_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A server that never started is a start-up failure: exit 2, one line.

        uvicorn records the cause in its own log lines; saneless's exit table
        has one code for "cannot start", whatever uvicorn's own status was.
        """
        self._stub_create_app(monkeypatch)
        runs = _fake_server_run(monkeypatch, started=False)
        runner, _ = _patch_cli(monkeypatch, settings=self._loopback_settings(tmp_path))

        result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 2, result.output
        [run] = runs
        # The address was bound, and announced, before uvicorn tried to start.
        assert _failure_lines(result) == [
            f"Serving on http://127.0.0.1:{run.port}",
            f"The web server could not start on http://127.0.0.1:{run.port}; "
            "the cause is in the preceding log lines",
        ]
        assert "Traceback" not in result.output

    def test_serve_exits_two_when_the_lifespan_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Uvicorn's own start-up exit code never becomes this CLI's.

        Nothing about uvicorn is stubbed: ``serve`` hands a real loopback socket
        to the real ``uvicorn.Server.run(sockets=...)``. Newer uvicorn exits the
        process itself when start-up fails, with a code that is this project's
        Paperless code, so a caller would be told the wrong cause.

        The lifespan refuses through a patched ``saneless.web.app.create_app``: an
        unwritable configured directory would be refused by the settings
        pre-flight, exit 2, before ``run_server`` is reached, and prove nothing.
        It reaches real uvicorn, so it runs on ``_invoke_on_a_worker_thread``.
        """
        self._refusing_create_app(monkeypatch)
        runner, _ = _patch_cli(monkeypatch, settings=self._loopback_settings(tmp_path))

        result = _invoke_on_a_worker_thread(runner, ["serve"])

        assert result.exit_code == 2, result.output
        lines = _failure_lines(result)
        # The port is the OS's, so it is read back from the announcement and
        # then required to be the same one the failure line names.
        announced = re.fullmatch(r"Serving on http://127\.0\.0\.1:(\d+)", lines[0])
        assert announced is not None, result.output
        port = announced[1]
        assert lines == [
            f"Serving on http://127.0.0.1:{port}",
            f"The web server could not start on http://127.0.0.1:{port}; "
            "the cause is in the preceding log lines",
        ]
        assert "Traceback" not in result.output

    def test_serve_normal_stop_exits_0(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A server that started and then returned is a clean shutdown, exit 0."""
        self._stub_create_app(monkeypatch)
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch, settings=self._loopback_settings(tmp_path))

        result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 0, result.output
        [run] = runs
        assert result.stderr.splitlines() == [f"Serving on http://127.0.0.1:{run.port}"]

    def test_serve_ctrl_c_once_running_exits_0_not_cancelled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Ctrl-C on a running ``serve`` is a normal stop, exit 0, not a cancel.

        uvicorn stops gracefully and then re-raises the captured SIGINT, which
        arrives as the KeyboardInterrupt the fake raises after starting.
        """
        self._stub_create_app(monkeypatch)
        runs = _fake_server_run(monkeypatch, then=KeyboardInterrupt())
        runner, _ = _patch_cli(monkeypatch, settings=self._loopback_settings(tmp_path))

        result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 0, result.output
        assert len(runs) == 1
        assert "Cancelled" not in result.output

    def test_serve_sane_init_failure_exits_2_not_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        SANE failing to initialise stops ``serve`` starting: exit 2, not 1.

        ``serve`` scans nothing itself, so its setup failures share the
        "can't start, fix your setup" code.
        """
        runs = _fake_server_run(monkeypatch)

        class FailingSaneBackend:
            """A backend whose construction fails the way ``sane.init()`` does."""

            def __init__(self, host: str = "") -> None:
                msg = "Could not initialise SANE: Error during device I/O"
                raise ScanError(msg)

        runner, _ = _patch_cli(
            monkeypatch,
            settings=self._loopback_settings(tmp_path),
            scanner_cls=FailingSaneBackend,
        )

        result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 2, result.output
        assert _failure_lines(result) == [
            "The web server could not start: Could not initialise SANE: "
            "Error during device I/O"
        ]
        assert runs == []

    def test_serve_ctrl_c_before_the_server_starts_exits_130(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Ctrl-C during start-up, before uvicorn owns the signal, is a cancel.

        Only uvicorn's own graceful stop exits 0; the reference documents both.
        """
        runs = _fake_server_run(monkeypatch)

        def interrupted_create_app(*_args: object, **_kwargs: object) -> object:
            raise KeyboardInterrupt

        monkeypatch.setattr("saneless.web.app.create_app", interrupted_create_app)
        runner, _ = _patch_cli(monkeypatch, settings=self._loopback_settings(tmp_path))

        result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 130, result.output
        assert result.stderr.splitlines() == ["Cancelled (interrupted)"]
        assert runs == []

    def test_serve_malformed_paperless_url_exits_3(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A Paperless URL the client cannot parse is a Paperless error, exit 3."""
        runs = _fake_server_run(monkeypatch)

        def failing_create_app(*_args: object, **_kwargs: object) -> object:
            msg = "Paperless URL http://host:abc is not valid: Invalid port: 'abc'"
            raise PaperlessError(msg)

        monkeypatch.setattr("saneless.web.app.create_app", failing_create_app)
        runner, _ = _patch_cli(monkeypatch, settings=self._loopback_settings(tmp_path))

        result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 3, result.output
        lines = _failure_lines(result)
        assert len(lines) == 1, result.stderr
        assert lines[0].startswith(
            "Paperless error: Paperless URL http://host:abc is not valid"
        )
        assert runs == []


class TestAutoProfiles:
    """auto-profiles generates, refreshes and reports scanner profiles."""

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

        class _AutoScanner(StubScannerBackend):
            """Mock scanner for auto-profiles tests."""

            def __init__(self, host: str = "") -> None:
                """Accept host parameter for API compatibility."""

            def get_devices(self) -> list[DeviceInfo]:
                """Return configured device list."""
                return _devices

            def get_capabilities(self, device_id: str) -> DeviceCapabilities:
                """Return configured capabilities."""
                return _caps

        return _AutoScanner

    @staticmethod
    def _group_line(output: str, label: str) -> str:
        """Return the one output line that starts with ``label``."""
        lines = [line for line in output.splitlines() if line.startswith(label)]
        assert len(lines) == 1, output
        return lines[0]

    @staticmethod
    def _flatbed_config(config_file: Path, *, flagged: bool) -> str:
        """Write a config holding a stale ``flatbed`` profile; return its text."""
        doc = tomlkit.document()
        profiles_table = tomlkit.table(is_super_table=True)
        existing = tomlkit.table()
        existing.add("source", "Old Source")
        existing.add("resolution", 150)
        existing.add("mode", "Gray")
        existing.add("default_tags", [4])
        if flagged:
            existing.add("auto_generated", flagged)
        profiles_table["flatbed"] = existing
        doc.add("profiles", profiles_table)
        text = tomlkit.dumps(doc)
        config_file.write_text(text)
        return text

    def test_auto_profiles_generates_profiles_grouped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """auto-profiles prints the Added group, detail lines and the real path."""
        config_file = tmp_path / "saneless.toml"
        scanner_cls = self._make_auto_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)

        result = runner.invoke(cli, ["--config", str(config_file), "auto-profiles"])
        assert result.exit_code == 0
        assert f"Profiles in {config_file.resolve()}:" in result.output
        added = self._group_line(result.output, "Added: ")
        assert "'flatbed'" in added
        assert "'adf'" in added
        # Match the whole printed "  <name>: source=..." line, not a bare
        # substring: "flatbed" also occurs inside longer profile names, so a
        # looser assertion could match the wrong line.
        assert "  flatbed: source=Flatbed" in result.output
        assert "  adf: source=ADF" in result.output

    def test_auto_profiles_writes_to_loaded_config_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """auto-profiles writes to the file settings were loaded from."""
        monkeypatch.chdir(tmp_path)
        config_file = tmp_path / "elsewhere" / CONFIG_FILENAME
        config_file.parent.mkdir()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=self._make_auto_scanner())

        result = runner.invoke(cli, ["--config", str(config_file), "auto-profiles"])

        assert result.exit_code == 0
        assert config_file.exists()
        assert not (tmp_path / "saneless.toml").exists()

    def test_auto_profiles_without_loaded_file_never_writes_the_cwd(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """
        With no config file loaded, ./saneless.toml is never the target.

        It is the first file the next start looks at, so a file created there
        would outrank every documented location, and the output names the
        file that was written by its absolute path.
        """
        files = patched_search_paths
        settings = _make_settings(tmp_path)
        settings._config_discovery = _searched()
        runner, _ = _patch_cli(
            monkeypatch, settings=settings, scanner_cls=self._make_auto_scanner()
        )

        result = runner.invoke(cli, ["auto-profiles"])

        assert result.exit_code == 0, result.output
        assert not files.cwd.exists()
        assert f"Profiles in {files.xdg}:" in result.output

    def test_auto_profiles_no_scanners(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        auto-profiles with no scanners is a scanner condition: exit 1, as on scan.

        Finding no scanner means the scanner is off, unplugged or out of
        reach, not that the configuration is wrong, so both commands that can
        find none exit 1: an exit code means the same thing in every command.
        The line carries the ``Scan error:`` prefix every scanner failure has,
        and the advice is the scanner's.
        """
        scanner_cls = self._make_auto_scanner(devices=[])
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)

        result = runner.invoke(cli, ["auto-profiles"])
        assert result.exit_code == 1
        lines = _failure_lines(result)
        assert len(lines) == 1
        assert lines[0].startswith("Scan error: No scanner found: ")
        advice = result.stderr.splitlines()[-1]
        assert advice == f"Try: {error_next_step(ErrorCategory.SCANNER)}"

    def test_auto_profiles_force_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """auto-profiles --force refreshes a flagged profile's generated keys."""
        config_file = tmp_path / "saneless.toml"
        self._flatbed_config(config_file, flagged=True)

        scanner_cls = self._make_auto_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)

        result = runner.invoke(
            cli, ["--config", str(config_file), "auto-profiles", "--force"]
        )
        assert result.exit_code == 0
        assert self._group_line(result.output, "Refreshed: ") == "Refreshed: 'flatbed'"
        assert "  flatbed: source=Flatbed" in result.output
        flatbed = tomllib.loads(config_file.read_text())["profiles"]["flatbed"]
        assert flatbed["source"] == "Flatbed"
        # A key the tool does not own survives the refresh.
        assert flatbed["default_tags"] == [4]

    def test_auto_profiles_force_skips_an_unflagged_profile_grouped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--force reports a hand-written profile and leaves it unchanged."""
        config_file = tmp_path / "saneless.toml"
        self._flatbed_config(config_file, flagged=False)
        before = tomllib.loads(config_file.read_text())["profiles"]["flatbed"]

        runner, _ = _patch_cli(monkeypatch, scanner_cls=self._make_auto_scanner())

        result = runner.invoke(
            cli, ["--config", str(config_file), "auto-profiles", "--force"]
        )
        assert result.exit_code == 0
        line = self._group_line(result.output, "Skipped (not auto-generated): ")
        assert line.startswith("Skipped (not auto-generated): 'flatbed'")
        assert "not created by auto-profiles (no auto_generated = true)" in line
        assert "rename or delete it to regenerate" in line
        after = tomllib.loads(config_file.read_text())["profiles"]["flatbed"]
        assert after == before
        assert "  flatbed: source=Flatbed" not in result.output

    def test_auto_profiles_ebusy_exits_2_without_traceback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A single-file bind mount prints the fix and exits 2."""
        config_file = tmp_path / "saneless.toml"
        self._flatbed_config(config_file, flagged=True)
        runner, _ = _patch_cli(monkeypatch, scanner_cls=self._make_auto_scanner())

        def busy(_self: Path, _target: object) -> Path:
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))

        monkeypatch.setattr(Path, "replace", busy)

        result = runner.invoke(cli, ["--config", str(config_file), "auto-profiles"])

        assert result.exit_code == 2
        assert "Mount its directory instead" in result.output
        assert isinstance(result.exception, SystemExit)
        assert "Traceback" not in result.output

    def test_auto_profiles_unwritable_file_exits_2_like_ebusy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A PermissionError from the write is a clean exit 2 naming the path."""
        config_file = tmp_path / "saneless.toml"
        runner, _ = _patch_cli(monkeypatch, scanner_cls=self._make_auto_scanner())

        def denied(path: Path, _text: str) -> Path:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))

        monkeypatch.setattr("saneless.auto_profiles.replace_file_atomically", denied)

        result = runner.invoke(cli, ["--config", str(config_file), "auto-profiles"])

        assert result.exit_code == 2
        assert str(config_file) in result.output
        assert isinstance(result.exception, SystemExit)

    def test_auto_profiles_force_in_a_config_directory_mount(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--force refreshes a config held in a mounted directory."""
        config_file = tmp_path / "config" / CONFIG_FILENAME
        config_file.parent.mkdir()
        self._flatbed_config(config_file, flagged=True)
        runner, _ = _patch_cli(monkeypatch, scanner_cls=self._make_auto_scanner())

        result = runner.invoke(
            cli, ["--config", str(config_file), "auto-profiles", "--force"]
        )

        assert result.exit_code == 0, result.output
        assert "Refreshed: " in result.output
        flatbed = tomllib.loads(config_file.read_text())["profiles"]["flatbed"]
        assert flatbed["default_tags"] == [4]
        assert [path.name for path in config_file.parent.iterdir()] == [CONFIG_FILENAME]

    def test_auto_profiles_no_force_skips_existing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """auto-profiles without --force reports flagged profiles as existing."""
        config_file = tmp_path / "saneless.toml"
        # Pre-populate config with ALL profiles that would be generated
        doc = tomlkit.document()
        profiles_table = tomlkit.table(is_super_table=True)
        flag = True
        for name in ("default", "flatbed", "adf"):
            entry = tomlkit.table()
            entry.add("source", "Existing")
            entry.add("resolution", 150)
            entry.add("mode", "Gray")
            entry.add("auto_generated", flag)
            profiles_table[name] = entry
        doc.add("profiles", profiles_table)
        config_file.write_text(tomlkit.dumps(doc))

        scanner_cls = self._make_auto_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)

        result = runner.invoke(cli, ["--config", str(config_file), "auto-profiles"])
        assert result.exit_code == 0
        line = self._group_line(
            result.output, "Skipped (already exists; use --force to refresh): "
        )
        for name in ("default", "flatbed", "adf"):
            assert repr(name) in line
        assert "Added: " not in result.output

    def test_auto_profiles_force_unchanged_reports_no_changes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        --force over a file that already matches the generation changes nothing.

        The command says so, names nothing as refreshed, and leaves the file
        where it was rather than replacing it with identical text.
        """
        config_file = tmp_path / "saneless.toml"
        runner, _ = _patch_cli(monkeypatch, scanner_cls=self._make_auto_scanner())
        first = runner.invoke(cli, ["--config", str(config_file), "auto-profiles"])
        assert first.exit_code == 0, first.output
        inode = config_file.stat().st_ino

        result = runner.invoke(
            cli, ["--config", str(config_file), "auto-profiles", "--force"]
        )

        assert result.exit_code == 0, result.output
        assert f"No changes to {config_file.resolve()}." in result.output
        assert "Refreshed" not in result.output
        assert config_file.stat().st_ino == inode

    def test_auto_profiles_no_source_device_keeps_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A scanner with no source option adds default and prunes nothing.

        Such a device generates only ``default``, so a flagged ``flatbed`` left
        by an earlier run cannot be judged orphaned: the scanner said nothing
        about where pages come from. The operator's table, with its
        ``default_tags``, survives, and the file the command leaves behind loads.
        """
        config_file = tmp_path / "saneless.toml"
        self._flatbed_config(config_file, flagged=True)
        no_source = DeviceCapabilities(
            sources=[], resolutions=[150, 300], modes=["Gray", "Color"]
        )
        runner, _ = _patch_cli(
            monkeypatch, scanner_cls=self._make_auto_scanner(caps=no_source)
        )

        result = runner.invoke(cli, ["--config", str(config_file), "auto-profiles"])

        assert result.exit_code == 0, result.output
        assert self._group_line(result.output, "Added: ") == "Added: 'default'"
        assert "Removed" not in result.output
        settings = load_settings(str(config_file))
        assert "default" in settings.profiles
        assert settings.profiles["flatbed"].default_tags == [4]

    def test_auto_profiles_no_source_default_claims_no_source(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The detail line says what the file says: no source.

        The written table has no ``source`` key, so the scanner uses its own.
        Printing the model's ``Flatbed`` fallback would claim a platen the
        device never reported.
        """
        config_file = tmp_path / "saneless.toml"
        no_source = DeviceCapabilities(
            sources=[], resolutions=[150, 300], modes=["Gray", "Color"]
        )
        runner, _ = _patch_cli(
            monkeypatch, scanner_cls=self._make_auto_scanner(caps=no_source)
        )

        result = runner.invoke(cli, ["--config", str(config_file), "auto-profiles"])

        assert result.exit_code == 0, result.output
        written = tomllib.loads(config_file.read_text())["profiles"]["default"]
        assert "source" not in written
        details = [
            line for line in result.output.splitlines() if line.startswith("  default:")
        ]
        assert details == [
            "  default: source=(none; the scanner's own), resolution=300, mode=Color"
        ]

    @staticmethod
    def _two_devices() -> list[DeviceInfo]:
        """Two visible scanners, listed in the order discovery reports them."""
        return [
            DeviceInfo(name="dev:a", vendor="A", model="One", device_type="scanner"),
            DeviceInfo(name="dev:b", vendor="B", model="Two", device_type="scanner"),
        ]

    def test_auto_profiles_pins_the_discovered_device_when_none_is_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With ``scanner.device`` empty, the id discovery chose is written."""
        config_file = tmp_path / "saneless.toml"
        settings = _make_settings(tmp_path, scanner=ScannerConfig(device=""))
        scanner_cls = self._make_auto_scanner(devices=self._two_devices())
        runner, _ = _patch_cli(monkeypatch, settings=settings, scanner_cls=scanner_cls)

        result = runner.invoke(cli, ["--config", str(config_file), "auto-profiles"])

        assert result.exit_code == 0, result.output
        data = tomllib.loads(config_file.read_text())
        assert data["scanner"]["device"] == "dev:a"
        pinned = self._group_line(result.output, "Pinned ")
        assert pinned == "Pinned [scanner] device: 'dev:a'"

    def test_auto_profiles_does_not_pin_over_a_configured_device(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A configured device, from a file or the environment, is not re-pinned."""
        config_file = tmp_path / "saneless.toml"
        settings = _make_settings(tmp_path, scanner=ScannerConfig(device="dev:b"))
        scanner_cls = self._make_auto_scanner(devices=self._two_devices())
        runner, _ = _patch_cli(monkeypatch, settings=settings, scanner_cls=scanner_cls)

        result = runner.invoke(cli, ["--config", str(config_file), "auto-profiles"])

        assert result.exit_code == 0, result.output
        assert "scanner" not in tomllib.loads(config_file.read_text())
        assert "Pinned " not in result.output


# What the superseded-name file holds in these tests.  Byte-for-byte
# comparable, so "auto-profiles left it alone" is an assertion and not a hope.
_STALE_TEXT = "# left behind\n[paperless]\nurl = 'http://nas:8000'\n"


def _stale_only_discovery(tmp_path: Path) -> ConfigDiscovery:
    """
    Record a search that found nothing but a file under the superseded name.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        The recording: no ``saneless.toml``, and one stale
        ``<tmp_path>/etc/config.toml``.

    """
    directory = tmp_path / "etc"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / LEGACY_CONFIG_FILENAME).write_text(_STALE_TEXT)
    return discover_config((directory / CONFIG_FILENAME,))


def _leftover_discovery(tmp_path: Path) -> ConfigDiscovery:
    """
    Record a search that loaded a file and found an old one beside it.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        The recording, in the ``LOADED_WITH_LEFTOVER`` shape.

    """
    directory = tmp_path / "etc"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / CONFIG_FILENAME).write_text("# in use\n")
    (directory / LEGACY_CONFIG_FILENAME).write_text(_STALE_TEXT)
    return discover_config((directory / CONFIG_FILENAME,))


class _SearchFiles(NamedTuple):
    """The three config search candidates of one test, each spelled absolute."""

    cwd: Path
    xdg: Path
    etc: Path


@pytest.fixture
def patched_search_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _SearchFiles:
    """
    Redirect the three config search candidates into ``tmp_path``.

    The real third candidate is ``/etc/saneless``, which no test may create or
    write, so the search list itself is replaced.  The working directory is
    moved into the first candidate's directory and that candidate stays
    relative, exactly as the real one is.  Only the directories the first two
    candidates need to exist are created: the XDG base, but not its
    ``saneless`` directory, and nothing under ``etc`` -- whether those exist is
    what several tests are about.

    A test attaches ``_searched()`` to its settings, which is what the real
    loader records.

    Returns:
        The three candidate files, absolute, in search order.

    """
    files = _SearchFiles(
        cwd=tmp_path / "cwd" / CONFIG_FILENAME,
        xdg=tmp_path / "xdg" / "saneless" / CONFIG_FILENAME,
        etc=tmp_path / "etc" / "saneless" / CONFIG_FILENAME,
    )
    files.cwd.parent.mkdir(parents=True)
    files.xdg.parent.parent.mkdir(parents=True)
    monkeypatch.chdir(files.cwd.parent)
    monkeypatch.setattr(
        "saneless.config.config_search_paths",
        lambda: (Path(CONFIG_FILENAME), files.xdg, files.etc),
    )
    return files


def _searched() -> ConfigDiscovery:
    """
    Record the (patched) search as the real loader would, right now.

    Returns:
        The recording of a search over ``config_search_paths()``.

    """
    return discover_config(config_module.config_search_paths())


class TestAutoProfilesRefusesAStaleOnlyConfig:
    """
    auto-profiles refuses to write while a superseded-name file is the only one.

    With nothing loaded, ``auto-profiles`` creates a file in a searched
    directory, and the next start loads it. A ``saneless.toml`` written while
    an unread ``config.toml`` holds the only copy of the Paperless URL and
    token would bury them: the search would stop at the new file and the red
    row saying to rename the old one would drop to amber, with the appliance
    still running on defaults.
    """

    @staticmethod
    def _counting_scanner(calls: list[str]) -> type:
        """
        Build a scanner class that records every device enumeration.

        Args:
            calls: The list each ``get_devices`` call appends to.

        Returns:
            A backend class for ``_patch_cli``.

        """

        class _CountingScanner(StubScannerBackend):
            """A backend that records being asked for devices."""

            def __init__(self, host: str = "") -> None:
                """Accept the host argument ``SaneBackend`` takes."""

            def get_devices(self) -> list[DeviceInfo]:
                """
                Record the call and report one device.

                Returns:
                    A single flatbed.

                """
                calls.append("get_devices")
                return [DeviceInfo("test:device", "Test", "Scanner", "scanner")]

            def get_capabilities(self, device_id: str) -> DeviceCapabilities:
                """
                Report capabilities the generator can work from.

                Args:
                    device_id: Ignored.

                Returns:
                    Two sources, three resolutions, two modes.

                """
                return DeviceCapabilities(
                    sources=["Flatbed", "ADF"],
                    resolutions=[150, 300, 600],
                    modes=["Color", "Gray"],
                )

        return _CountingScanner

    def _refuse(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, calls: list[str]
    ) -> Result:
        """
        Run ``auto-profiles`` over a stale-only search.

        Args:
            monkeypatch: pytest's patcher.
            tmp_path: pytest's per-test directory.
            calls: Where the scanner records its device enumerations.

        Returns:
            The runner's result.

        """
        monkeypatch.chdir(tmp_path)
        settings = _make_settings(tmp_path)
        settings._config_discovery = _stale_only_discovery(tmp_path)
        runner, _ = _patch_cli(
            monkeypatch,
            settings=settings,
            scanner_cls=self._counting_scanner(calls),
        )
        return runner.invoke(cli, ["auto-profiles"])

    def test_it_exits_two_and_says_to_rename_the_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The refusal exits 2 through the group guard and says to rename the file."""
        result = self._refuse(monkeypatch, tmp_path, [])
        stale = tmp_path / "etc" / LEGACY_CONFIG_FILENAME

        assert result.exit_code == 2
        lines = _failure_lines(result)
        assert len(lines) == 1
        assert f"saneless now reads {CONFIG_FILENAME}" in lines[0]
        assert f"Rename {stale.absolute()} to {CONFIG_FILENAME}" in lines[0]

    def test_the_sentence_is_printed_exactly_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The refusal replaces the stderr warning; it does not follow it.

        Two copies of one sentence is how a reader learns the output repeats
        itself and stops reading it.
        """
        result = self._refuse(monkeypatch, tmp_path, [])

        assert result.output.count(f"saneless now reads {CONFIG_FILENAME}") == 1
        assert "Warning: " not in result.output

    def test_it_creates_no_shadowing_config_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The refusal creates no ``saneless.toml`` that would shadow the stale file."""
        self._refuse(monkeypatch, tmp_path, [])

        assert not (tmp_path / CONFIG_FILENAME).exists()
        assert not (tmp_path / "etc" / CONFIG_FILENAME).exists()

    def test_it_leaves_the_stale_files_bytes_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The stale file is stat-ed, never touched: its bytes stay as they were."""
        self._refuse(monkeypatch, tmp_path, [])

        stale = tmp_path / "etc" / LEGACY_CONFIG_FILENAME
        assert stale.read_text() == _STALE_TEXT

    def test_it_refuses_before_asking_the_scanner_for_devices(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The refusal comes before any device enumeration.

        Placing it after the device call would mean an operator with the file
        in the wrong place *and* no scanner attached is told about the scanner
        -- the one of the two they cannot fix from the keyboard.
        """
        calls: list[str] = []
        result = self._refuse(monkeypatch, tmp_path, calls)

        assert result.exit_code == 2
        assert calls == []


class TestAutoProfilesWriteTargetFollowsTheSearch:
    """
    The write goes to the file the search loaded, under its new name.

    These are the paths where no ``--config`` was given and the recording is
    the only thing that says where the configuration lives.
    """

    def test_it_writes_to_the_file_the_search_loaded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A loaded XDG file is written in place, not shadowed by a new cwd file."""
        monkeypatch.chdir(tmp_path)
        loaded = tmp_path / "xdg" / CONFIG_FILENAME
        loaded.parent.mkdir()
        loaded.write_text("# loaded by the search\n")
        settings = _make_settings(tmp_path)
        settings._config_discovery = discover_config((loaded,))
        runner, _ = _patch_cli(
            monkeypatch,
            settings=settings,
            scanner_cls=TestAutoProfiles._make_auto_scanner(),
        )

        result = runner.invoke(cli, ["auto-profiles"])

        assert result.exit_code == 0, result.output
        assert "flatbed" in tomllib.loads(loaded.read_text())["profiles"]
        assert not (tmp_path / CONFIG_FILENAME).exists()

    def test_a_search_that_found_nothing_writes_no_cwd_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """With nothing loaded and nothing stale, the cwd is still not the target."""
        files = patched_search_paths
        settings = _make_settings(tmp_path)
        settings._config_discovery = _searched()
        runner, _ = _patch_cli(
            monkeypatch,
            settings=settings,
            scanner_cls=TestAutoProfiles._make_auto_scanner(),
        )

        result = runner.invoke(cli, ["auto-profiles"])

        assert result.exit_code == 0, result.output
        assert "flatbed" in tomllib.loads(files.xdg.read_text())["profiles"]
        assert not files.cwd.exists()


class TestAutoProfilesTarget:
    """
    With no config file loaded, the new file goes where no later search shadows.

    The search reads ``./saneless.toml`` first, and in the container the
    working directory is the data directory, so a file created there outranks
    the ``/etc/saneless`` file the documentation tells the operator to mount --
    the command that was meant to help would build the trap itself.  The target
    is therefore the last documented location saneless may write: the system
    directory when it already exists and is writable, else the per-user XDG
    one.  saneless never creates the system directory.

    Every test redirects the search into ``tmp_path``; no test may touch the
    real ``/etc/saneless``.
    """

    @staticmethod
    def _run(
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        args: tuple[str, ...] = ("auto-profiles",),
    ) -> Result:
        """
        Run ``auto-profiles`` over the patched search with nothing loaded.

        Args:
            monkeypatch: pytest's patcher.
            tmp_path: pytest's per-test directory.
            args: The command line.

        Returns:
            The runner's result.

        """
        settings = _make_settings(tmp_path)
        settings._config_discovery = _searched()
        runner, _ = _patch_cli(
            monkeypatch,
            settings=settings,
            scanner_cls=TestAutoProfiles._make_auto_scanner(),
        )
        return runner.invoke(cli, list(args))

    def test_the_target_is_an_existing_writable_etc_directory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """
        An existing, writable system directory is the target.

        The container's mounted ``./config`` is that directory.
        """
        files = patched_search_paths
        files.etc.parent.mkdir(parents=True)

        result = self._run(monkeypatch, tmp_path)

        assert result.exit_code == 0, result.output
        assert files.etc.exists()
        assert files.etc.stat().st_mode & 0o777 == 0o600
        assert not files.cwd.exists()
        assert not files.xdg.exists()
        assert str(files.etc) in result.stdout

    def test_the_target_is_xdg_without_an_etc_directory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """
        No system directory: the per-user one, created private, and nothing else.

        The ``saneless`` directory under the XDG base is created 0700, because
        the file it holds may later carry the Paperless token.
        """
        files = patched_search_paths
        assert not files.xdg.parent.exists()

        result = self._run(monkeypatch, tmp_path)

        assert result.exit_code == 0, result.output
        assert files.xdg.exists()
        assert files.xdg.parent.stat().st_mode & 0o777 == 0o700
        assert files.xdg.stat().st_mode & 0o777 == 0o600
        assert not files.etc.parent.exists()
        assert not files.cwd.exists()
        assert str(files.xdg) in result.stdout

    def test_the_target_is_xdg_when_etc_is_not_writable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """
        An admin-owned system directory is left alone for a non-root user.

        ``os.access`` is patched for that one directory, so the test does not
        depend on whether it runs as root.
        """
        files = patched_search_paths
        files.etc.parent.mkdir(parents=True)
        real_access = os.access

        def _access(path: str | Path, mode: int) -> bool:
            """
            Refuse write access to the system directory only.

            Returns:
                False for writing the system directory, else the real answer.

            """
            if Path(path) == files.etc.parent and mode == os.W_OK:
                return False
            return real_access(path, mode)

        monkeypatch.setattr(os, "access", _access)

        result = self._run(monkeypatch, tmp_path)

        assert result.exit_code == 0, result.output
        assert files.xdg.exists()
        assert not files.etc.exists()
        assert not files.cwd.exists()

    def test_a_target_directory_that_cannot_be_created_exits_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """
        The container's HOME may not exist: a configuration error, not a crash.

        The message names the file that could not be written, so the operator
        can create the directory or mount ``/etc/saneless``.
        """
        files = patched_search_paths
        refused = files.xdg.parent
        real_mkdir = Path.mkdir

        def _mkdir(
            path: Path,
            mode: int = 0o777,
            *,
            parents: bool = False,
            exist_ok: bool = False,
        ) -> None:
            """
            Refuse to create the XDG ``saneless`` directory only.

            Raises:
                PermissionError: For that directory.

            """
            if path == refused:
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)
            real_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

        monkeypatch.setattr(Path, "mkdir", _mkdir)

        result = self._run(monkeypatch, tmp_path)

        assert result.exit_code == 2
        assert f"Cannot write {files.xdg}" in result.stderr
        assert "Traceback" not in result.output
        assert not files.cwd.exists()

    @staticmethod
    def _refuse_etc_and_xdg(
        monkeypatch: pytest.MonkeyPatch, files: _SearchFiles
    ) -> None:
        """
        Make the system directory unwritable and the XDG directory uncreatable.

        ``os.access`` is patched for the system directory only, so the test
        does not depend on whether it runs as root; the XDG ``saneless``
        directory's creation is refused the way a missing HOME refuses it.

        Args:
            monkeypatch: pytest's patcher.
            files: The patched search candidates.

        """
        files.etc.parent.mkdir(parents=True)
        real_access = os.access
        real_mkdir = Path.mkdir

        def _access(path: str | Path, mode: int) -> bool:
            """
            Refuse write access to the system directory only.

            Returns:
                False for writing the system directory, else the real answer.

            """
            if Path(path) == files.etc.parent and mode == os.W_OK:
                return False
            return real_access(path, mode)

        def _mkdir(
            path: Path,
            mode: int = 0o777,
            *,
            parents: bool = False,
            exist_ok: bool = False,
        ) -> None:
            """
            Refuse to create the XDG ``saneless`` directory only.

            Raises:
                PermissionError: For that directory.

            """
            if path == files.xdg.parent:
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)
            real_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

        monkeypatch.setattr(os, "access", _access)
        monkeypatch.setattr(Path, "mkdir", _mkdir)

    def test_a_failed_fallback_names_the_unwritable_etc_directory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """
        The failure says why the system directory was passed over.

        Otherwise the operator is told only about a per-user file they never
        meant to use, and goes looking in the wrong place.
        """
        files = patched_search_paths
        self._refuse_etc_and_xdg(monkeypatch, files)

        result = self._run(monkeypatch, tmp_path)

        assert result.exit_code == 2
        (line,) = [
            line for line in result.stderr.splitlines() if "Cannot write" in line
        ]
        assert line.startswith(f"Cannot write {files.xdg}: ")
        assert line.endswith(
            f"({files.etc.parent} exists but is not writable by this user)"
        )

    def test_a_failed_fallback_names_a_read_only_etc_mount(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """A ``:ro`` mount is named as one, since its fix is not an ownership change."""
        files = patched_search_paths
        self._refuse_etc_and_xdg(monkeypatch, files)
        monkeypatch.setattr(
            cli_module, "is_read_only_mount", lambda path: path == files.etc.parent
        )

        result = self._run(monkeypatch, tmp_path)

        assert result.exit_code == 2
        assert f"({files.etc.parent} exists but is mounted read-only)" in result.stderr

    def test_a_failed_write_with_no_etc_directory_adds_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """With no system directory, the write error names no skipped directory."""
        files = patched_search_paths
        self._refuse_etc_and_xdg(monkeypatch, files)
        files.etc.parent.rmdir()

        result = self._run(monkeypatch, tmp_path)

        assert result.exit_code == 2
        assert f"Cannot write {files.xdg}: " in result.stderr
        assert "exists but" not in result.stderr

    def test_a_dangling_symlink_at_the_target_exits_2_and_creates_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """
        The search counts a link to nothing as no file; the write must not follow it.

        Whoever can write the system directory could plant the link, and a
        root run would otherwise create the file it names.
        """
        files = patched_search_paths
        files.etc.parent.mkdir(parents=True)
        planted = tmp_path / "elsewhere" / "planted.conf"
        planted.parent.mkdir()
        files.etc.symlink_to(planted)

        result = self._run(monkeypatch, tmp_path)

        assert result.exit_code == 2
        assert f"Cannot create {files.etc}: it is a symlink" in result.stderr
        assert "Traceback" not in result.output
        assert not planted.exists()
        assert list(planted.parent.iterdir()) == []

    def test_an_explicit_config_is_the_target_whatever_else_exists(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """``--config`` names the file, and a writable system directory is ignored."""
        files = patched_search_paths
        files.etc.parent.mkdir(parents=True)
        given = tmp_path / "given" / CONFIG_FILENAME
        given.parent.mkdir()

        result = self._run(
            monkeypatch, tmp_path, ("--config", str(given), "auto-profiles")
        )

        assert result.exit_code == 0, result.output
        assert given.exists()
        assert not files.etc.exists()
        assert not files.xdg.exists()
        assert not files.cwd.exists()

    def test_the_target_without_a_recording_uses_the_documented_search(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """
        Settings built without a load carry no recording; the search list stands in.

        The CLI's own name for the search list is patched, as the module-level
        fixture patches the loader's.
        """
        files = patched_search_paths
        files.etc.parent.mkdir(parents=True)
        monkeypatch.setattr(
            cli_module,
            "config_search_paths",
            lambda: (Path(CONFIG_FILENAME), files.xdg, files.etc),
        )
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_make_settings(tmp_path),
            scanner_cls=TestAutoProfiles._make_auto_scanner(),
        )

        result = runner.invoke(cli, ["auto-profiles"])

        assert result.exit_code == 0, result.output
        assert files.etc.exists()
        assert not files.cwd.exists()


def _fake_file_owner(monkeypatch: pytest.MonkeyPatch, path: Path, uid: int) -> None:
    """
    Make ``os.stat`` report ``path`` as owned by ``uid`` once it exists.

    A path that does not exist yet still raises, so a writer asking whether
    the file is there is answered for real.  Nothing is chowned: a non-root
    test cannot give a file to root.

    Args:
        monkeypatch: pytest's patcher.
        path: The file whose owner is faked.
        uid: The owner to report.

    """
    real_stat = os.stat
    faked = os.fspath(path)

    def fake_stat(
        target: int | str | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        """
        Return the real status, with the file's owner replaced.

        Returns:
            The real status of ``target``, owned by ``uid`` when it is the file.

        """
        result = real_stat(target, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if isinstance(target, int) or os.fspath(target) != faked:
            return result
        fields = list(result)
        fields[stat.ST_UID] = uid
        return os.stat_result(fields)

    monkeypatch.setattr(os, "stat", fake_stat)


class TestAutoProfilesRootOwnedNewConfig:
    """
    A config root creates and cannot give away is named, with the fix.

    A new file takes its directory's owner, which on a bare-metal host is
    usually root for ``/etc/saneless``.  The file then stays root's, mode 0600,
    and a service running as an ordinary user finds it and cannot read it, so
    every later command exits 2.  The command therefore says so on stderr, with
    the absolute path and the ``chown`` that fixes it.
    """

    @staticmethod
    def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Result:
        """
        Run ``auto-profiles`` over the patched search with nothing loaded.

        Args:
            monkeypatch: pytest's patcher.
            tmp_path: pytest's per-test directory.

        Returns:
            The runner's result.

        """
        return TestAutoProfilesTarget._run(monkeypatch, tmp_path)

    def test_a_new_root_owned_file_is_named_with_the_chown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """The note names the absolute file and tells the operator to chown it."""
        files = patched_search_paths
        files.etc.parent.mkdir(parents=True)
        _fake_file_owner(monkeypatch, files.etc, 0)

        result = self._run(monkeypatch, tmp_path)

        assert result.exit_code == 0, result.output
        assert files.etc.exists()
        notes = [line for line in result.stderr.splitlines() if "chown" in line]
        assert len(notes) == 1, result.stderr
        assert f"chown <user>: {files.etc}" in notes[0]
        assert f"{files.etc} is owned by root" in notes[0]
        assert "chown" not in result.stdout

    def test_a_new_root_owned_per_user_file_is_not_told_to_chown_in_place(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """
        A root-owned per-user file gets the moves that work, not a chown.

        Root's own per-user file is not in another user's search at all.
        With no system directory, root's target is the file under its own
        home.  A ``chown`` there fixes nothing: another user never searches
        that home, and its saneless would quietly run on defaults.  The note
        says so and gives the moves that do work.
        """
        files = patched_search_paths
        _fake_file_owner(monkeypatch, files.xdg, 0)

        result = self._run(monkeypatch, tmp_path)

        assert result.exit_code == 0, result.output
        assert files.xdg.exists()
        notes = [line for line in result.stderr.splitlines() if "chown" in line]
        assert len(notes) == 1, result.stderr
        note = notes[0]
        assert f"{files.xdg} is owned by root" in note
        assert f"chown <user>: {files.xdg}" not in note
        assert "any other user will not read it" in note
        assert f"mv {files.xdg} {files.etc.parent}/" in note
        assert f"chown <user>: {files.etc}" in note
        assert "`saneless auto-profiles` as that user" in note

    def test_a_new_per_user_directory_takes_its_parents_owner(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """
        Root with an ordinary user's HOME leaves no root-only directory there.

        ``sudo -E`` keeps HOME, so the per-user target is in the invoking
        user's home.  The ``saneless`` directory it creates takes that home's
        owner, the rule the new file already follows, so the file follows it
        too and the user can reach both.
        """
        files = patched_search_paths
        base = files.xdg.parent.parent.stat()
        monkeypatch.setattr(os, "geteuid", lambda: base.st_uid + 1)
        given: list[tuple[int, int, int]] = []

        def fchown(fd: int, uid: int, gid: int) -> None:
            given.append((os.fstat(fd).st_ino, uid, gid))

        monkeypatch.setattr(os, "fchown", fchown)

        result = self._run(monkeypatch, tmp_path)

        assert result.exit_code == 0, result.output
        directory = files.xdg.parent.stat()
        assert (directory.st_ino, base.st_uid, base.st_gid) in given

    def test_a_new_file_owned_by_another_user_says_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """A file that went to the directory's non-root owner needs no fix."""
        files = patched_search_paths
        files.etc.parent.mkdir(parents=True)
        _fake_file_owner(monkeypatch, files.etc, 4242)

        result = self._run(monkeypatch, tmp_path)

        assert result.exit_code == 0, result.output
        assert files.etc.exists()
        assert "chown" not in result.output

    def test_an_existing_root_owned_file_says_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A rewrite keeps the owner the file already had; that is not news.

        The note is about a file this command created, so a loaded file owned
        by root -- whoever put it there chose that -- is rewritten silently.
        """
        loaded = tmp_path / "etc" / CONFIG_FILENAME
        loaded.parent.mkdir()
        loaded.write_text("# loaded by the search\n")
        settings = _make_settings(tmp_path)
        settings._config_discovery = discover_config((loaded,))
        _fake_file_owner(monkeypatch, loaded, 0)
        runner, _ = _patch_cli(
            monkeypatch,
            settings=settings,
            scanner_cls=TestAutoProfiles._make_auto_scanner(),
        )

        result = runner.invoke(cli, ["auto-profiles"])

        assert result.exit_code == 0, result.output
        assert "flatbed" in tomllib.loads(loaded.read_text())["profiles"]
        assert "chown" not in result.output


class TestStaleConfigWarningReachesTheTerminal:
    """
    A one-shot command says on stderr what its log file says.

    The startup log names the ignored file, but a one-shot command's log goes
    to ``log_file`` and is read afterwards, if at all, so the operator
    watching the command run would otherwise see nothing.

    ``serve`` is exempt because its records already stream to stderr: a service
    printing the sentence twice per start is noise in the one stream an
    operator does read.
    """

    @staticmethod
    def _warnings(result: Result) -> list[str]:
        """
        Return the terminal warning lines of a command's stderr.

        Args:
            result: The runner's result.

        Returns:
            Every stderr line starting with the warning prefix.

        """
        return [
            line for line in result.stderr.splitlines() if line.startswith("Warning: ")
        ]

    def _devices(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        discovery: ConfigDiscovery | None,
    ) -> Result:
        """
        Run a one-shot command over a recorded search.

        Args:
            monkeypatch: pytest's patcher.
            tmp_path: pytest's per-test directory.
            discovery: The recording to attach, or None for settings with none.

        Returns:
            The runner's result.

        """
        settings = _make_settings(tmp_path)
        if discovery is not None:
            settings._config_discovery = discovery
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        return runner.invoke(cli, ["devices"])

    def test_a_stale_only_search_warns_once_with_the_rows_words(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One line, carrying the Configuration row's message and its next step."""
        result = self._devices(monkeypatch, tmp_path, _stale_only_discovery(tmp_path))
        stale = tmp_path / "etc" / LEGACY_CONFIG_FILENAME

        assert result.exit_code == 0
        warnings = self._warnings(result)
        assert len(warnings) == 1
        assert f"saneless now reads {CONFIG_FILENAME}" in warnings[0]
        assert f"Rename {stale.absolute()} to {CONFIG_FILENAME}" in warnings[0]

    def test_a_leftover_beside_a_loaded_file_warns_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The amber state is warned about as well as the red one.

        It is the state an upgraded Docker deployment lands in, and the old
        file there can hold the only copy of the URL and token -- so the
        terminal says to move before it says to delete.
        """
        result = self._devices(monkeypatch, tmp_path, _leftover_discovery(tmp_path))
        stale = tmp_path / "etc" / LEGACY_CONFIG_FILENAME

        warnings = self._warnings(result)
        assert len(warnings) == 1
        assert f"an old {LEGACY_CONFIG_FILENAME} is being ignored" in warnings[0]
        assert f"Move anything you still need from {stale.absolute()}" in warnings[0]
        assert warnings[0].index("Move") < warnings[0].index("then delete")

    def test_shadowed_config_is_warned_on_stderr(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """
        Two config files: the terminal names the one in use and the one not read.

        A second file is the Docker trap in its settled form -- the file the
        operator edits is not the one saneless reads -- and a one-shot command's
        log is not in front of the person who just ran it.
        """
        files = patched_search_paths
        files.cwd.write_text("# in use\n")
        files.etc.parent.mkdir(parents=True)
        files.etc.write_text("# not read\n")

        result = self._devices(monkeypatch, tmp_path, _searched())

        assert result.exit_code == 0, result.output
        warnings = self._warnings(result)
        assert len(warnings) == 1, result.stderr
        assert warnings[0].startswith("Warning: Using ")
        assert str(files.cwd) in warnings[0]
        assert str(files.etc) in warnings[0]

    def test_shadowed_and_leftover_are_both_warned(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        patched_search_paths: _SearchFiles,
    ) -> None:
        """
        A shadowing file must not hide an old-name file that may hold the token.

        The Configuration row can show only one of the two situations, and the
        shadowed one wins there; the terminal has room for both lines.
        """
        files = patched_search_paths
        files.cwd.write_text("# in use\n")
        files.etc.parent.mkdir(parents=True)
        files.etc.write_text("# not read\n")
        stale = files.cwd.with_name(LEGACY_CONFIG_FILENAME)
        stale.write_text(_STALE_TEXT)

        result = self._devices(monkeypatch, tmp_path, _searched())

        warnings = self._warnings(result)
        assert len(warnings) == 2, result.stderr
        assert warnings[0].startswith("Warning: Using ")
        assert str(files.etc) in warnings[0]
        assert f"an old {LEGACY_CONFIG_FILENAME} is being ignored" in warnings[1]
        assert f"Move anything you still need from {stale}" in warnings[1]

    def test_a_loaded_file_with_nothing_beside_it_prints_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A healthy appliance gets a quiet terminal."""
        loaded = tmp_path / "etc" / CONFIG_FILENAME
        loaded.parent.mkdir()
        loaded.write_text("# in use\n")
        result = self._devices(monkeypatch, tmp_path, discover_config((loaded,)))

        assert self._warnings(result) == []

    def test_no_config_file_at_all_prints_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        An environment-only deployment is not being warned at every command.

        The amber Configuration row and the log's searched list already cover
        it; a stderr line on every invocation would be a warning about a
        supported setup.
        """
        result = self._devices(
            monkeypatch,
            tmp_path,
            discover_config((tmp_path / "etc" / CONFIG_FILENAME,)),
        )

        assert self._warnings(result) == []

    @pytest.mark.parametrize(("shape", "expected"), [("service", 0), ("one-shot", 1)])
    def test_only_the_one_shot_shape_prints_the_line(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        shape: str,
        expected: int,
    ) -> None:
        """
        Only the one-shot shape echoes the warning; ``serve``'s log streams it.

        Both shapes of ``_load_cli_settings`` run in one test, so the exemption
        cannot be satisfied by printing nowhere at all.

        Args:
            monkeypatch: pytest's patcher.
            tmp_path: pytest's per-test directory.
            capsys: Captures the echo, which goes to the real stderr here.
            shape: "service" for ``serve``'s shape, "one-shot" for the rest.
            expected: How many warning lines that shape should print.

        """
        settings = _make_settings(tmp_path)
        settings._config_discovery = _stale_only_discovery(tmp_path)
        _patch_cli(monkeypatch, settings=settings)
        context = click.Context(cli, obj={})

        loaded = cli_module._load_cli_settings(context, stream_logs=shape == "service")

        assert loaded is settings
        printed = [
            line
            for line in capsys.readouterr().err.splitlines()
            if line.startswith("Warning: ")
        ]
        assert len(printed) == expected


class TestTruncation:
    """``_truncate`` and the CLI tables cut over-wide text with an ellipsis."""

    def test_truncate_short_string(self) -> None:
        """Short string within width is returned unchanged."""
        assert _truncate("hello", 10) == "hello"

    def test_truncate_exact_width(self) -> None:
        """String exactly matching width is returned unchanged."""
        assert _truncate("hello", 5) == "hello"

    def test_truncate_long_string(self) -> None:
        """String exceeding width is truncated with ellipsis character."""
        result = _truncate("a very long device name", 10)
        assert result == "a very lo\u2026"
        assert len(result) == 10

    def test_devices_truncation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Devices table truncates long device names with ellipsis."""
        long_name = "x" * 50

        class LongNameScanner(StubScannerBackend):
            """Scanner returning a device with a very long name."""

            def __init__(self, host: str = "") -> None:
                """Accept host parameter for API compatibility."""

            def get_devices(self) -> list[DeviceInfo]:
                """Return a device with a 50-character name."""
                return [DeviceInfo(long_name, "Vendor", "Model", "scanner")]

            def get_capabilities(self, device_id: str) -> DeviceCapabilities:
                """Return minimal capabilities."""
                return DeviceCapabilities(
                    sources=["Flatbed"],
                    resolutions=[300],
                    modes=["color"],
                )

        # Force a narrow terminal so truncation kicks in
        monkeypatch.setattr(
            "shutil.get_terminal_size",
            lambda _f=(80, 24): type("TermSize", (), {"columns": 80, "lines": 24})(),
        )
        runner, _ = _patch_cli(monkeypatch, scanner_cls=LongNameScanner)
        result = runner.invoke(cli, ["devices"])
        assert result.exit_code == 0
        assert "\u2026" in result.output
        assert long_name not in result.output

    def test_jobs_truncation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Jobs table truncates long titles with ellipsis."""
        long_title = "T" * 50
        settings = _make_settings(
            tmp_path,
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                data_dir=str(tmp_path),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )
        store = JobStore(db_path=settings.output.db_path)
        store.create_job(profile="default", title=long_title)
        store.close()

        # Force a narrow terminal so truncation kicks in
        monkeypatch.setattr(
            "shutil.get_terminal_size",
            lambda _f=(80, 24): type("TermSize", (), {"columns": 80, "lines": 24})(),
        )
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        result = runner.invoke(cli, ["jobs"])
        assert result.exit_code == 0
        assert "\u2026" in result.output


def _raising_scanner(exc: BaseException) -> type[ScannerBackend]:
    """
    Build a scanner backend class whose every device call raises ``exc``.

    Every method raises, so the same class serves ``scan`` (``scan_pages``),
    ``devices`` and ``auto-profiles`` (``get_devices``) whichever the pipeline
    reaches first.

    Args:
        exc: The exception every call raises.

    Returns:
        A ``ScannerBackend`` subclass accepting ``host`` like ``SaneBackend``.

    """

    class RaisingScanner(ScannerBackend):
        """Scanner whose every call raises the configured exception."""

        def __init__(self, host: str = "") -> None:
            """Accept host parameter for API compatibility."""

        def get_devices(self) -> list[DeviceInfo]:
            """Raise the configured exception."""
            raise exc

        def get_capabilities(self, device_id: str) -> DeviceCapabilities:
            """Raise the configured exception."""
            raise exc

        def scan_pages(
            self, device_id: str, settings: ScanSettings, sink: PageSink
        ) -> ScanBatch:
            """Raise the configured exception, spooling nothing."""
            raise exc

    return RaisingScanner


def _raising_paperless(exc: Exception) -> type:
    """
    Build a Paperless client class whose upload raises ``exc``.

    Args:
        exc: The exception ``upload_document`` raises.

    Returns:
        A class constructed like ``PaperlessClient``.

    """

    class RaisingPaperless:
        """Paperless client whose upload raises the configured exception."""

        def __init__(self, *_a: object, **_kw: object) -> None:
            """Accept and ignore all constructor arguments."""

        def upload_document(self, *_a: object, **_kw: object) -> UploadResult:
            """Raise the configured exception."""
            raise exc

        def close(self) -> None:
            """No-op close."""

    return RaisingPaperless


def _tmp_settings(tmp_path: Path) -> Settings:
    """
    Build settings whose tmp, data and log paths all live under ``tmp_path``.

    Args:
        tmp_path: The test's temporary directory.

    Returns:
        Settings with a ``default`` profile and tmp_path-rooted output paths.

    """
    return _make_settings(
        tmp_path,
        output=OutputConfig(
            tmp_dir=str(tmp_path),
            data_dir=str(tmp_path),
            log_file=str(tmp_path / "saneless.log"),
        ),
    )


@contextlib.contextmanager
def _restored_root_logging() -> Generator[None]:
    """
    Undo what a real ``configure_logging`` call does to the root logger.

    Only the handlers added inside the block are removed, so pytest's own
    capture handlers survive; a stderr handler left behind would write to the
    runner's closed stream in every later test.

    Yields:
        Nothing; the root logger is restored on exit.

    """
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    try:
        yield
    finally:
        for handler in root.handlers[:]:
            if handler not in handlers:
                handler.close()
                root.removeHandler(handler)
        root.setLevel(level)
        logging.getLogger("saneless").setLevel(logging.NOTSET)


def _cli_error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """
    Return the ERROR records ``saneless.cli`` emitted.

    Args:
        caplog: The test's log capture fixture.

    Returns:
        Every captured record from ``saneless.cli`` at ERROR or above.

    """
    return [
        record
        for record in caplog.records
        if record.name == "saneless.cli" and record.levelno >= logging.ERROR
    ]


class TestExitCodes:
    """
    One guard maps every CLI failure to one message and its exit code.

    A bad config (2), a broken scanner (1), an unreachable Paperless (3) and
    an unassemblable PDF (4) each print one error report; a job database
    saneless cannot use is a setup problem (2); a cancel or Ctrl-C is 130;
    anything that is not a saneless type is 5 with its traceback in the log.
    click's own ``--help``, usage errors and ``ClickException`` keep click's
    behaviour.
    """

    def test_bad_config_exits_2_with_header_and_one_problem_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The loader's header plus its one problem line, exit 2."""
        runner, _ = _patch_cli(monkeypatch)

        def bad_load(*_args: object, **_kwargs: object) -> Settings:
            msg = (
                "Configuration error in /etc/saneless/saneless.toml:\n"
                "  line 12, column 5: Invalid value"
            )
            raise ConfigError(msg)

        monkeypatch.setattr("saneless.cli.load_settings", bad_load)

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 2
        assert _failure_lines(result) == [
            "Configuration error in /etc/saneless/saneless.toml:",
            "  line 12, column 5: Invalid value",
        ]
        assert "Traceback" not in result.output

    def test_broken_scanner_exits_1_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A ScanError from the scanner is one ``Scan error:`` line, exit 1."""
        exc = ScanError(
            "Could not open scanner epson2:libusb:001:004: Invalid argument"
        )
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_tmp_settings(tmp_path),
            scanner_cls=_raising_scanner(exc),
        )

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 1
        assert _failure_lines(result) == [
            "Scan error: Could not open scanner epson2:libusb:001:004: Invalid argument"
        ]
        assert "Traceback" not in result.output

    def test_unreachable_paperless_exits_3_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A PaperlessError from the upload is one ``Paperless error:`` line, exit 3."""
        exc = PaperlessError(
            "Could not reach Paperless at http://paperless:8000: "
            "[Errno 111] Connection refused"
        )
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_tmp_settings(tmp_path),
            paperless_cls=_raising_paperless(exc),
        )

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 3
        lines = _failure_lines(result)
        assert len(lines) == 1
        assert lines[0].startswith(
            "Paperless error: Could not reach Paperless at http://paperless:8000"
        )
        assert "Traceback" not in result.output

    def test_unassemblable_pdf_exits_4_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A PdfError from assembly is one ``PDF error:`` line, exit 4.

        The line goes on to say where the spooled pages were kept: no PDF
        could be built, so the page files themselves are moved into a
        job-keyed directory under ``failed/``.  It stays one line and exit 4,
        which is what preserving the exception type buys.
        """
        runner, _ = _patch_cli(monkeypatch, settings=_tmp_settings(tmp_path))

        def failing_assemble(*_args: object, **_kwargs: object) -> Path:
            msg = (
                "Could not assemble 1 page(s) into /tmp/x.pdf: No space left on device"
            )
            raise PdfError(msg)

        monkeypatch.setattr("saneless.pipeline.assemble_pdf", failing_assemble)

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 4
        lines = _failure_lines(result)
        assert len(lines) == 1
        assert lines[0].startswith(
            "PDF error: Could not assemble 1 page(s) into /tmp/x.pdf: "
            "No space left on device."
        )
        assert "1 spooled page file(s) were preserved at" in lines[0]
        assert "Traceback" not in result.output

    def test_mid_scan_no_scanner_error_exits_1_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A NoScannerFoundError from the scanner is one ``Scan error:`` line, exit 1."""
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_tmp_settings(tmp_path),
            scanner_cls=_raising_scanner(NoScannerFoundError("No scanner found")),
        )

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 1
        assert _failure_lines(result) == ["Scan error: No scanner found"]

    def test_scan_with_no_scanner_found_exits_1_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        ``scan`` with no device set and none discovered is a scanner error, exit 1.

        The advice is the scanner's, to check it is switched on and connected,
        not the configuration's: nothing in the file is wrong.
        """

        class _NoDeviceScanner(StubScannerBackend):
            """A SANE that answers discovery with an empty list."""

            def __init__(self, host: str = "") -> None:
                """Accept host parameter for API compatibility."""

            def get_devices(self) -> list[DeviceInfo]:
                """Find no scanner."""
                return []

        settings = _tmp_settings(tmp_path)
        settings.scanner.device = ""
        runner, _ = _patch_cli(
            monkeypatch, settings=settings, scanner_cls=_NoDeviceScanner
        )

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 1
        lines = _failure_lines(result)
        assert len(lines) == 1
        assert lines[0].startswith("Scan error: No scanner found: ")
        advice = result.stderr.splitlines()[-1]
        assert advice == f"Try: {error_next_step(ErrorCategory.SCANNER)}"
        assert "Traceback" not in result.output

    def test_scan_cancelled_exits_130_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A ScanCancelledError is a cancel, not a failure: exit 130."""
        exc = ScanCancelledError("Manual duplex scan cancelled at the flip prompt")
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_tmp_settings(tmp_path),
            scanner_cls=_raising_scanner(exc),
        )

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 130
        assert result.stderr.splitlines() == [
            "Manual duplex scan cancelled at the flip prompt"
        ]

    @pytest.mark.parametrize("command", ["scan", "devices", "auto-profiles"])
    def test_keyboard_interrupt_exits_130_without_aborted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
    ) -> None:
        """
        Ctrl-C in a command is ``Cancelled (interrupted)`` and exit 130.

        Raised only inside ``runner.invoke``: a KeyboardInterrupt
        escaping a test would stop the whole pytest session.
        """
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_tmp_settings(tmp_path),
            scanner_cls=_raising_scanner(KeyboardInterrupt()),
        )

        result = runner.invoke(cli, [command])

        assert result.exit_code == 130
        lines = result.stderr.splitlines()
        # devices announces itself on stderr before it touches SANE; that
        # status line is not part of the cancel report.
        if command == "devices":
            assert lines[0] == "Discovering scanners..."
            lines = lines[1:]
        assert lines == ["Cancelled (interrupted)"]
        assert "Aborted!" not in result.output

    def test_all_pages_blank_exits_8_with_threshold_advice(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Every page judged blank is its own failure: exit 8, never a scan error.

        The advice line points at the detection threshold, because the scanner
        did its job; a script can tell this apart from exit 1.
        """
        exc = AllPagesBlankError("All pages were blank")
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_tmp_settings(tmp_path),
            scanner_cls=_raising_scanner(exc),
        )

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 8
        lines = _failure_lines(result)
        assert len(lines) == 1
        assert lines[0].startswith("Empty-page detection: All pages were blank")
        assert "empty_page_coverage_threshold" in result.stderr.splitlines()[-1]
        assert "Scan error" not in result.stderr

    def test_a_classified_failure_line_includes_the_exception_notes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        What ``add_note`` attached reaches stderr, on the one failure line.

        ``str(exc)`` drops notes, and a preserved scan says where its pages
        went in one; printing the bare message would hide the file.
        """
        exc = ScanError("jam")
        exc.add_note("The 3 page(s) were preserved at /d/x.pdf")
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_tmp_settings(tmp_path),
            scanner_cls=_raising_scanner(exc),
        )

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 1
        assert _failure_lines(result) == [
            "Scan error: jam. The 3 page(s) were preserved at /d/x.pdf"
        ]

    def test_an_unexpected_error_line_includes_the_exception_notes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A foreign exception's notes reach its ``Unexpected error`` line too."""
        exc = RuntimeError("boom")
        exc.add_note("The 3 page(s) were preserved at /d/x.pdf")
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_tmp_settings(tmp_path),
            scanner_cls=_raising_scanner(exc),
        )
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *_a, **_kw: False)

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 5
        lines = result.stderr.splitlines()
        assert len(lines) == 1
        assert lines[0].startswith(
            "Unexpected error (RuntimeError): boom. "
            "The 3 page(s) were preserved at /d/x.pdf"
        )

    @pytest.mark.parametrize(
        ("signum", "code"),
        [
            pytest.param(signal.SIGHUP, 129, id="sighup"),
            pytest.param(signal.SIGTERM, 143, id="sigterm"),
        ],
    )
    def test_a_signal_interruption_exits_128_plus_the_signal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        signum: signal.Signals,
        code: int,
    ) -> None:
        """
        SIGHUP or SIGTERM is an interruption, not a cancel: 129 or 143.

        One ``Interrupted:`` line, never the cancel's 130, because the pages
        already scanned are kept rather than discarded.
        """
        exc = ScanInterrupted(f"Interrupted by {signum.name}", signum=signum)
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_tmp_settings(tmp_path),
            scanner_cls=_raising_scanner(exc),
        )

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == code
        lines = result.stderr.splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("Interrupted: ")
        assert "Traceback" not in result.output

    def test_keyboard_interrupt_still_exits_130_beside_the_signal_codes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Ctrl-C is a deliberate cancel that keeps nothing: exit 130."""
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_tmp_settings(tmp_path),
            scanner_cls=_raising_scanner(KeyboardInterrupt()),
        )

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 130
        assert result.stderr.splitlines() == ["Cancelled (interrupted)"]

    def test_unexpected_error_exits_5_naming_the_log_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """After logging attached a file, the line points at it and it is logged."""
        exc = RuntimeError("kaboom")
        settings = _tmp_settings(tmp_path)
        runner, _ = _patch_cli(
            monkeypatch, settings=settings, scanner_cls=_raising_scanner(exc)
        )
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *_a, **_kw: True)
        caplog.set_level(logging.ERROR, logger="saneless.cli")

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 5
        assert result.stderr.splitlines() == [
            "Unexpected error (RuntimeError): kaboom. "
            f"Full details in {settings.output.log_file}"
        ]
        records = _cli_error_records(caplog)
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert records[0].exc_info[1] is exc

    def test_unexpected_error_exits_5_without_log_file_when_not_attached(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        When logging fell back to stderr, the line claims no log file.

        The fallback renders no traceback, so the line says how to get one
        instead.
        """
        exc = RuntimeError("kaboom")
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_tmp_settings(tmp_path),
            scanner_cls=_raising_scanner(exc),
        )
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *_a, **_kw: False)
        caplog.set_level(logging.ERROR, logger="saneless.cli")

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 5
        assert result.stderr.splitlines() == [
            "Unexpected error (RuntimeError): kaboom. "
            "Run again with -v to see the traceback"
        ]
        assert "Full details in" not in result.output
        records = _cli_error_records(caplog)
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert records[0].exc_info[1] is exc

    def test_multi_line_unexpected_message_prints_one_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An exception whose text spans lines still prints one line."""
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_tmp_settings(tmp_path),
            scanner_cls=_raising_scanner(RuntimeError("kaboom\nsecond line")),
        )
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *_a, **_kw: True)

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 5
        assert len(result.stderr.splitlines()) == 1
        assert result.stderr.startswith(
            "Unexpected error (RuntimeError): kaboom second line. Full details in "
        )

    def test_unexpected_error_before_logging_exits_5_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Before logging is configured nothing is logged and one line is printed."""
        runner, _ = _patch_cli(monkeypatch)

        def exploding_load(*_args: object, **_kwargs: object) -> Settings:
            msg = "kaboom"
            raise RuntimeError(msg)

        monkeypatch.setattr("saneless.cli.load_settings", exploding_load)
        caplog.set_level(logging.DEBUG, logger="saneless.cli")

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 5
        assert result.stderr.splitlines() == [
            "Unexpected error (RuntimeError): kaboom. "
            "Run again with -v to see the traceback"
        ]
        assert _cli_error_records(caplog) == []

    def test_unexpected_error_before_logging_with_verbose_exits_5_with_traceback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With -v the traceback goes to stderr, followed by the bare line."""
        runner, _ = _patch_cli(monkeypatch)

        def exploding_load(*_args: object, **_kwargs: object) -> Settings:
            msg = "kaboom"
            raise RuntimeError(msg)

        monkeypatch.setattr("saneless.cli.load_settings", exploding_load)

        result = runner.invoke(cli, ["-v", "scan"])

        assert result.exit_code == 5
        assert "Traceback" in result.stderr
        assert (
            result.stderr.splitlines()[-1] == "Unexpected error (RuntimeError): kaboom"
        )

    @pytest.mark.parametrize(
        ("exc", "code"),
        [
            pytest.param(RuntimeError("kaboom"), 5, id="unexpected"),
            pytest.param(
                ScanError("Could not open scanner test:device:001: Invalid argument"),
                1,
                id="classified",
            ),
        ],
    )
    def test_stderr_log_fallback_prints_no_traceback_without_verbose(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        exc: Exception,
        code: int,
    ) -> None:
        """
        An unwritable log file logs to stderr, but never a traceback.

        The real ``configure_logging`` runs, so the stderr fallback handler is
        genuinely attached: a stub that attaches nothing cannot catch a
        traceback printed through it.
        """
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")
        settings = _make_settings(
            tmp_path,
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                data_dir=str(tmp_path),
                log_file=str(blocker / "logs" / "saneless.log"),
            ),
        )
        runner, _ = _patch_cli(
            monkeypatch, settings=settings, scanner_cls=_raising_scanner(exc)
        )
        monkeypatch.setattr("saneless.cli.configure_logging", configure_logging)

        with _restored_root_logging():
            result = runner.invoke(cli, ["scan"])

        assert result.exit_code == code
        assert "Traceback" not in result.stderr
        if code == 5:
            assert result.stderr.splitlines()[-1] == (
                "Unexpected error (RuntimeError): kaboom. "
                "Run again with -v to see the traceback"
            )

    def test_stderr_log_fallback_with_verbose_prints_the_traceback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With -v the traceback is mirrored to stderr."""
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")
        settings = _make_settings(
            tmp_path,
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                data_dir=str(tmp_path),
                log_file=str(blocker / "logs" / "saneless.log"),
            ),
        )
        runner, _ = _patch_cli(
            monkeypatch,
            settings=settings,
            scanner_cls=_raising_scanner(RuntimeError("kaboom")),
        )
        monkeypatch.setattr("saneless.cli.configure_logging", configure_logging)

        with _restored_root_logging():
            result = runner.invoke(cli, ["-v", "scan"])

        assert result.exit_code == 5
        assert "Traceback" in result.stderr
        assert (
            result.stderr.splitlines()[-1] == "Unexpected error (RuntimeError): kaboom"
        )

    def test_job_database_not_sqlite_exits_2_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A jobs.db that is not SQLite is a setup problem: exit 2."""
        settings = _tmp_settings(tmp_path)
        settings.output.db_path.write_bytes(b"this is not a database\n" * 64)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])

        assert result.exit_code == 2
        lines = _failure_lines(result)
        assert len(lines) == 1
        assert lines[0].startswith("Job database error: ")
        assert str(settings.output.db_path) in lines[0]
        assert "Unexpected error" not in result.output
        assert "Traceback" not in result.output

    def test_job_database_unsupported_schema_exits_2_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A jobs table from before the versioned schema is exit 2, naming the database.

        ``jobs`` only reads, so it refuses the old table rather than migrating
        it, and points at ``saneless serve``, which upgrades it.
        """
        settings = _tmp_settings(tmp_path)
        conn = sqlite3.connect(settings.output.db_path)
        try:
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY)")
            conn.commit()
        finally:
            conn.close()
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])

        assert result.exit_code == 2
        lines = _failure_lines(result)
        assert len(lines) == 1
        assert str(settings.output.db_path) in lines[0]
        assert "older than this saneless reads" in lines[0]
        assert "Unexpected error" not in result.output
        assert "Traceback" not in result.output

    def test_job_database_storage_error_is_logged_and_exits_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A StorageError is one line and exit 2, logged without a bug-report hint."""
        exc = StorageError(f"Could not open the job database at {tmp_path}: boom")

        def failing_read(*_args: object, **_kwargs: object) -> list[Job]:
            raise exc

        runner, _ = _patch_cli(monkeypatch, settings=_tmp_settings(tmp_path))
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *_a, **_kw: True)
        monkeypatch.setattr("saneless.cli.read_recent_jobs", failing_read)
        caplog.set_level(logging.ERROR, logger="saneless.cli")

        result = runner.invoke(cli, ["jobs"])

        assert result.exit_code == 2
        assert _failure_lines(result) == [f"Job database error: {exc}"]
        assert "Full details in" not in result.output
        records = _cli_error_records(caplog)
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert records[0].exc_info[1] is exc

    def test_help_exits_0_without_running_the_command(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``scan --help`` is click's Exit, re-raised by the guard: exit 0."""
        runner, _ = _patch_cli(monkeypatch)
        calls: list[str] = []

        def recording_load(*_args: object, **_kwargs: object) -> Settings:
            calls.append("load_settings")
            return _make_settings(tmp_path)

        monkeypatch.setattr("saneless.cli.load_settings", recording_load)

        result = runner.invoke(cli, ["scan", "--help"])

        assert result.exit_code == 0
        assert "Usage: cli scan" in result.stdout
        assert calls == []

    def test_unknown_option_is_clicks_usage_error_exit_2(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown option stays click's usage error, not an unexpected error."""
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["scan", "--bogus"])

        assert result.exit_code == 2
        assert "No such option" in result.stderr
        assert "Unexpected error" not in result.output

    def test_click_exception_keeps_its_own_exit_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ClickException raised in a command is shown by click, exit 1."""
        runner, _ = _patch_cli(monkeypatch)

        def bind_failure(*_args: object, **_kwargs: object) -> None:
            msg = "bind"
            raise click.ClickException(msg)

        monkeypatch.setattr("saneless.cli.run_pipeline", bind_failure)

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 1
        assert result.stderr.splitlines() == ["Error: bind"]


class _ClosingScanner(StubScannerBackend):
    """
    A CLI scanner stub that counts the closes its entry point gives it.

    It reports one device and a usable capability set, so ``devices`` and
    ``auto-profiles`` have real work to finish before the close is due.
    """

    def __init__(self, host: str = "") -> None:
        """Accept the host like SaneBackend, and start with no close."""
        self.host = host
        self.close_calls = 0

    def get_devices(self) -> list[DeviceInfo]:
        """Report the one device these tests need."""
        return [DeviceInfo("test:device", "Test", "Scanner", "scanner")]

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """Report enough for auto-profiles to generate something."""
        return DeviceCapabilities(
            sources=["Flatbed", "ADF"],
            resolutions=[150, 300, 600],
            modes=["Color", "Gray"],
        )

    def close(self) -> None:
        """Count the close the entry point owes this backend."""
        self.close_calls += 1


def _closing_scanner() -> tuple[type[_ClosingScanner], list[_ClosingScanner]]:
    """
    Build a one-test backend class that records every construction.

    The list is what the assertions read: "closed exactly once" is a claim
    about the backend the command actually built, and a class attribute shared
    across tests could not say which construction it belonged to.

    Returns:
        The class to patch into the CLI, and the list its instances land in.

    """
    built: list[_ClosingScanner] = []

    class _Recorded(_ClosingScanner):
        """The class the command under test constructs."""

        def __init__(self, host: str = "") -> None:
            """Record this construction, then behave like the base."""
            super().__init__(host)
            built.append(self)

    return _Recorded, built


class TestEntryPointsCloseTheBackend:
    """
    Each one-shot command shuts SANE down when it ends.

    ``sane_init`` is process-global, so somebody has to undo it, and the one
    place that knows the process is ending is the entry point that started it.
    The close is owed on the error paths as much as on the success ones: a
    command that exits 1 has finished with the scanner exactly as much as one
    that exits 0.

    ``serve`` is the exception and is asserted as one: once its server has
    started, the backend outlives the command body and the lifespan closes it.
    Before that, nobody else will, so a start that fails closes it here.
    """

    def test_scan_closes_the_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A completed scan leaves no initialised SANE behind."""
        scanner_cls, built = _closing_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 0, result.output
        assert [scanner.close_calls for scanner in built] == [1]

    def test_scan_closes_the_backend_when_the_pipeline_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing pipeline still closes the backend once, and the scan exits 1."""
        scanner_cls, built = _closing_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)

        def failing_pipeline(*_args: object, **_kwargs: object) -> None:
            msg = "Scanner error on test:device: jammed"
            raise ScanError(msg)

        monkeypatch.setattr("saneless.cli.run_pipeline", failing_pipeline)

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 1, result.output
        assert [scanner.close_calls for scanner in built] == [1]

    def test_devices_closes_the_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Listing devices is the shortest command, and still owes the close."""
        scanner_cls, built = _closing_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)

        result = runner.invoke(cli, ["devices"])

        assert result.exit_code == 0, result.output
        assert [scanner.close_calls for scanner in built] == [1]

    def test_devices_closes_the_backend_when_enumeration_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A SANE failure mid-command does not cost the process its close."""
        scanner_cls, built = _closing_scanner()

        def busy_bus(_self: _ClosingScanner) -> list[DeviceInfo]:
            """Fail as ``sane.get_devices()`` does when the bus is busy."""
            msg = "Could not list scanners: Device busy"
            raise ScanError(msg)

        monkeypatch.setattr(scanner_cls, "get_devices", busy_bus)
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)

        result = runner.invoke(cli, ["devices"])

        assert result.exit_code == 1, result.output
        assert [scanner.close_calls for scanner in built] == [1]

    def test_auto_profiles_closes_the_backend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A successful profile generation closes the backend it queried."""
        scanner_cls, built = _closing_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)
        config_file = tmp_path / "saneless.toml"

        result = runner.invoke(cli, ["--config", str(config_file), "auto-profiles"])

        assert result.exit_code == 0, result.output
        assert [scanner.close_calls for scanner in built] == [1]

    def test_auto_profiles_closes_the_backend_on_the_early_config_exit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The refusal to write the config file is not a way to skip the close.

        An unwritable config file ends ``auto-profiles`` with a configuration
        error raised after the scanner was opened, exit 2, and an early exit
        is the path most likely to be forgotten.
        """
        scanner_cls, built = _closing_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)

        def unwritable(*_args: object, **_kwargs: object) -> None:
            raise OSError(errno.EACCES, "Permission denied")

        monkeypatch.setattr("saneless.cli.write_profiles_to_config", unwritable)
        config_file = tmp_path / "saneless.toml"

        result = runner.invoke(cli, ["--config", str(config_file), "auto-profiles"])

        assert result.exit_code == 2, result.output
        assert [scanner.close_calls for scanner in built] == [1]

    @pytest.mark.usefixtures("uvicorn_loggers_restored")
    def test_serve_leaves_the_backend_for_the_lifespan_to_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``serve`` hands its backend to the app, which outlives the command body.

        Closing it here would shut SANE down before the first request, so this
        one command deliberately does not, and the lifespan does it instead.
        """
        scanner_cls, built = _closing_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)
        monkeypatch.setattr("saneless.web.app.create_app", lambda *_args: object())
        _fake_server_run(monkeypatch)

        result = runner.invoke(cli, ["serve", "--host", "127.0.0.1", "--port", "0"])

        assert result.exit_code == 0, result.output
        assert [scanner.close_calls for scanner in built] == [0]

    def test_serve_closes_the_backend_when_the_app_cannot_be_built(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        An app that is never built never takes the backend, so ``serve`` closes it.

        An unreadable trust store is the real case: the Paperless client is
        built after SANE was initialised, and the command exits 3.
        """
        scanner_cls, built = _closing_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)

        def unreadable_trust_store(*_args: object) -> NoReturn:
            msg = "The TLS trust store named by SSL_CERT_FILE could not be read"
            raise PaperlessTrustStoreError(msg)

        monkeypatch.setattr("saneless.web.app.create_app", unreadable_trust_store)

        result = runner.invoke(cli, ["serve", "--host", "127.0.0.1", "--port", "0"])

        assert result.exit_code == 3, result.output
        assert [scanner.close_calls for scanner in built] == [1]

    @pytest.mark.usefixtures("uvicorn_loggers_restored")
    def test_serve_closes_the_backend_when_the_server_never_starts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A server that never started never ran the lifespan that would close SANE.

        The command exits 2, and the backend is closed on the way out.
        """
        scanner_cls, built = _closing_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)
        _fake_server_run(monkeypatch, started=False)

        result = runner.invoke(cli, ["serve", "--host", "127.0.0.1", "--port", "0"])

        assert result.exit_code == 2, result.output
        assert [scanner.close_calls for scanner in built] == [1]

    @pytest.mark.usefixtures("uvicorn_loggers_restored")
    def test_serve_leaves_the_backend_to_a_lifespan_that_took_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Once the lifespan owns the backend, a failed run does not close it here.

        The lifespan keeps SANE open on purpose when a thread will not stop,
        and that thread may be inside a SANE call that a shutdown from here
        would pull out from under it.
        """
        scanner_cls, built = _closing_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)
        real_create_app = app_module.create_app

        def taken_over(settings: Settings, scanner: ScannerBackend) -> FastAPI:
            app = real_create_app(settings, scanner)
            services_of(app).lifecycle.started = True
            return app

        monkeypatch.setattr("saneless.web.app.create_app", taken_over)
        _fake_server_run(monkeypatch, started=False)

        result = runner.invoke(cli, ["serve", "--host", "127.0.0.1", "--port", "0"])

        assert result.exit_code == 2, result.output
        assert [scanner.close_calls for scanner in built] == [0]

    def test_serve_leaves_sane_uninitialised_after_a_failed_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A real backend's failed start leaves the process with SANE shut down.

        The app factory checks that SANE really was initialised when it ran,
        so the final assertion is about a shutdown and not about a backend
        that never started SANE at all.
        """
        fake_sane = FakeSaneModule()
        monkeypatch.setattr(sane_backend, "sane", fake_sane)
        runner, _ = _patch_cli(monkeypatch, scanner_cls=sane_backend.SaneBackend)
        initialised_when_built: list[bool] = []

        def unreadable_trust_store(*_args: object) -> NoReturn:
            initialised_when_built.append(sane_backend._INIT.done)
            msg = "The TLS trust store named by SSL_CERT_FILE could not be read"
            raise PaperlessTrustStoreError(msg)

        monkeypatch.setattr("saneless.web.app.create_app", unreadable_trust_store)

        result = runner.invoke(cli, ["serve", "--host", "127.0.0.1", "--port", "0"])

        assert result.exit_code == 3, result.output
        assert initialised_when_built == [True]
        assert sane_backend._INIT.done is False
        assert fake_sane.exit_call_count == 1


# The five categories a SanelessError can carry through the guard to an exit
# code that is not ExitCode.UNEXPECTED, each paired with an exception
# classify_error maps to it.  UNKNOWN and REJECTED are deliberately absent:
# both map to UNEXPECTED, which takes the _report_unexpected branch, and that
# branch prints no advice because its category is a guess.
_ADVISED_CATEGORIES: list[tuple[ErrorCategory, type[SanelessError]]] = [
    (ErrorCategory.FEEDER, FeederEmptyError),
    (ErrorCategory.SCANNER, ScanError),
    (ErrorCategory.CONFIG, ConfigError),
    (ErrorCategory.UPLOAD, PaperlessError),
    (ErrorCategory.ASSEMBLY, PdfError),
]


def _scan_raising(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> Result:
    """
    Run ``scan`` against a pipeline that raises ``exc``, and return the result.

    The failure is raised from inside the command rather than from the settings
    loader so it travels the whole ``_GuardedGroup.invoke`` path the advice line
    is added to.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        exc: The exception the stub pipeline raises.

    Returns:
        The CliRunner result.

    """
    runner, _ = _patch_cli(monkeypatch)

    def failing_pipeline(*_args: object, **_kwargs: object) -> None:
        raise exc

    monkeypatch.setattr("saneless.cli.run_pipeline", failing_pipeline)
    return runner.invoke(cli, ["scan", "--title", "Tax return"])


class TestFailureAdvice:
    """
    A classified failure ends with a second line, ``Try: <next step>``.

    The failure line is printed byte for byte and the advice follows it on
    stderr, so a script parsing line 1 is unaffected.
    """

    def test_feeder_failure_prints_the_failure_line_then_the_advice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A feeder failure prints its failure line, then the ``Try: `` line."""
        exc = FeederEmptyError("the feeder is empty")
        result = _scan_raising(monkeypatch, exc)

        lines = result.stderr.strip().split("\n")
        assert lines[0] == "Scan error: the feeder is empty"
        assert lines[1] == (
            "Try: Load the pages squarely in the feeder, clear any jam, then "
            "start the scan again."
        )

    @pytest.mark.parametrize(("category", "exc_type"), _ADVISED_CATEGORIES)
    def test_the_advice_line_is_error_next_step_verbatim(
        self,
        monkeypatch: pytest.MonkeyPatch,
        category: ErrorCategory,
        exc_type: type[SanelessError],
    ) -> None:
        """Every advised category renders the shared vocabulary string."""
        result = _scan_raising(monkeypatch, exc_type("boom"))

        lines = result.stderr.strip().split("\n")
        assert lines[1] == f"Try: {error_next_step(category)}"

    @pytest.mark.parametrize(("category", "exc_type"), _ADVISED_CATEGORIES)
    def test_line_one_is_byte_identical_now_that_advice_follows_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        category: ErrorCategory,
        exc_type: type[SanelessError],
    ) -> None:
        """Line 1 is exactly ``_failure_line``'s output; the advice only adds line 2."""
        exc = exc_type("boom")
        result = _scan_raising(monkeypatch, exc)

        lines = result.stderr.strip().split("\n")
        assert lines[0] == _failure_line(exc, category)
        assert len(lines) == 2

    @pytest.mark.parametrize(("category", "exc_type"), _ADVISED_CATEGORIES)
    def test_exit_codes_are_unchanged_by_the_advice_line(
        self,
        monkeypatch: pytest.MonkeyPatch,
        category: ErrorCategory,
        exc_type: type[SanelessError],
    ) -> None:
        """Each category keeps its own exit code with the advice line printed."""
        result = _scan_raising(monkeypatch, exc_type("boom"))

        assert result.exit_code == int(exit_code_for(category))

    def test_the_advice_goes_to_stderr_not_stdout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A script redirecting stdout is unaffected by the advice line."""
        result = _scan_raising(monkeypatch, FeederEmptyError("the feeder is empty"))

        assert "Try: " in result.stderr
        assert "Try: " not in result.stdout

    def test_the_prefix_carries_no_colour_and_no_glyph(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The prefix is exactly ``Try: `` -- one space, nothing else."""
        result = _scan_raising(monkeypatch, FeederEmptyError("the feeder is empty"))

        advice = result.stderr.strip().split("\n")[1]
        assert advice.startswith("Try: ")
        assert "\x1b" not in advice

    def test_an_unexpected_saneless_error_gets_no_advice_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare SanelessError takes ``_report_unexpected``, which prints no advice."""
        result = _scan_raising(monkeypatch, SanelessError("boom"))

        assert result.exit_code == 5
        assert "Try: " not in result.stderr

    def test_a_non_saneless_exception_gets_no_advice_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The final ``except Exception`` arm prints no advice line."""
        result = _scan_raising(monkeypatch, RuntimeError("boom"))

        assert result.exit_code == 5
        assert "Try: " not in result.stderr

    @pytest.mark.parametrize(("category", "exc_type"), _ADVISED_CATEGORIES)
    def test_a_raise_site_next_step_replaces_the_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        category: ErrorCategory,
        exc_type: type[SanelessError],
    ) -> None:
        """
        A raise site that knows the fix is advised by it, not by its category.

        Line 1 is unchanged; only line 2 moves from the category's fallback to
        the error's own next step.
        """
        exc = exc_type("boom", next_step="Do y.")
        result = _scan_raising(monkeypatch, exc)

        lines = result.stderr.strip().split("\n")
        assert lines == [_failure_line(exc, category), "Try: Do y."]
        assert result.exit_code == int(exit_code_for(category))

    def test_a_next_step_has_its_control_characters_shown_as_escapes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A next step is neutralised like the failure line, so it stays one line."""
        exc = ConfigError("boom", next_step="Do \x1b[31m y.\nThen z.")
        result = _scan_raising(monkeypatch, exc)

        lines = result.stderr.strip().split("\n")
        assert lines == ["boom", "Try: Do \\x1b[31m y.\\nThen z."]

    def test_a_storage_error_gets_advice(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A job database saneless cannot use ends with a ``Try:`` line too.

        ``StorageError`` has its own arm, ahead of the classified one, so it
        has to print the advice itself: its first line is unchanged and the
        second is the configuration fallback.
        """
        runner, _ = _patch_cli(monkeypatch, settings=_make_settings(tmp_path))

        def unusable_history(*_args: object, **_kwargs: object) -> NoReturn:
            msg = "db is unreadable"
            raise StorageError(msg)

        monkeypatch.setattr("saneless.cli.read_recent_jobs", unusable_history)

        result = runner.invoke(cli, ["jobs"])

        assert result.exit_code == 2, result.output
        assert result.stderr.splitlines() == [
            "Job database error: db is unreadable",
            f"Try: {error_next_step(ErrorCategory.CONFIG)}",
        ]

    def test_a_storage_error_with_its_own_next_step_prints_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``StorageError`` that knows its fix is advised by it."""
        exc = StorageError("db is unreadable", next_step="Do y.")
        result = _scan_raising(monkeypatch, exc)

        assert result.exit_code == 2
        assert result.stderr.splitlines() == [
            "Job database error: db is unreadable",
            "Try: Do y.",
        ]

    def test_a_cancelled_scan_gets_no_advice_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cancel is not a failure, so there is nothing to advise."""
        result = _scan_raising(monkeypatch, ScanCancelledError("Cancelled"))

        assert result.exit_code == 130
        assert "Try: " not in result.stderr

    def test_a_keyboard_interrupt_gets_no_advice_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ctrl-C exits 130 with one line and no advice."""
        result = _scan_raising(monkeypatch, KeyboardInterrupt())

        assert result.exit_code == 130
        assert "Try: " not in result.stderr


# Arranges one raise site's failure and returns the runner and the argv.
type _RaiseSiteSetup = Callable[
    [pytest.MonkeyPatch, Path, contextlib.ExitStack], tuple[CliRunner, list[str]]
]


@dataclasses.dataclass(frozen=True)
class _RaiseSite:
    """
    One known raise site, and the two lines its failure ends with.

    Attributes:
        setup: Arranges the failure and returns the runner and the argv.
        exit_code: The documented exit code.
        line_one: How the failure line starts.
        next_step: The ``Try:`` line's text: the site's own fix.

    """

    setup: _RaiseSiteSetup
    exit_code: int
    line_one: str
    next_step: str


def _site_without_python_sane(
    monkeypatch: pytest.MonkeyPatch, _tmp_path: Path, _stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """``devices`` with python-sane missing, through the real ``require_sane``."""
    runner, _ = _patch_cli(monkeypatch)
    monkeypatch.setattr("saneless.cli.require_sane", sane_backend.require_sane)
    _block_sane_import(monkeypatch)
    return runner, ["devices"]


def _site_port_in_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """``serve`` on a port a real listening socket already holds."""
    runner, _ = _patch_cli(monkeypatch, settings=_make_settings(tmp_path))
    holder = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    return runner, ["serve", "--host", "127.0.0.1", "--port", str(port)]


def _site_sane_will_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """``serve`` whose SANE backend raises as ``sane.init()`` failing does."""

    class _UnstartableSane(StubScannerBackend):
        """A backend whose construction fails the way a broken libsane does."""

        def __init__(self, host: str = "") -> None:
            """Fail as SaneBackend does when SANE cannot be initialised."""
            msg = "SANE could not be initialised: Invalid argument"
            raise ScanError(msg)

    runner, _ = _patch_cli(
        monkeypatch, settings=_make_settings(tmp_path), scanner_cls=_UnstartableSane
    )
    return runner, ["serve", "--host", "127.0.0.1", "--port", "0"]


def _site_server_never_started(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """``serve`` whose uvicorn start-up fails, so the server never started."""
    runner, _ = _patch_cli(monkeypatch, settings=_make_settings(tmp_path))
    TestServeCommand._stub_create_app(monkeypatch)
    _fake_server_run(monkeypatch, started=False)
    return runner, ["serve", "--host", "127.0.0.1", "--port", "0"]


def _site_unreadable_trust_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """``serve`` with SSL_CERT_FILE naming no file, through the real app factory."""
    runner, _ = _patch_cli(monkeypatch, settings=_make_settings(tmp_path))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "absent-ca.crt"))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    return runner, ["serve", "--host", "127.0.0.1", "--port", "0"]


def _site_empty_config_path(
    _monkeypatch: pytest.MonkeyPatch, _tmp_path: Path, _stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """``--config ""``, as an unset ``$CFG`` gives, through the real loader."""
    return CliRunner(), ["--config", "", "jobs"]


def _site_missing_config_path(
    _monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """``--config`` naming a file that is not there, through the real loader."""
    return CliRunner(), ["--config", str(tmp_path / "absent.toml"), "jobs"]


def _site_shared_tmp_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """``jobs`` with a group-writable ``output.tmp_dir``."""
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o770)
    settings = _make_settings(
        tmp_path,
        output=OutputConfig(
            tmp_dir=str(shared),
            data_dir=str(tmp_path),
            log_file=str(tmp_path / "saneless.log"),
        ),
    )
    runner, _ = _patch_cli(monkeypatch, settings=settings)
    return runner, ["jobs"]


def _site_flip_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """Arrange a manual-duplex scan whose flip prompt nobody answers in time."""
    runner, _ = _patch_cli(
        monkeypatch,
        settings=_duplex_settings(tmp_path, operator_wait_timeout_seconds=1),
        scanner_cls=_counting_scanner([]),
        paperless_cls=_recording_paperless([]),
    )
    monkeypatch.setattr("saneless.cli._stdin_is_interactive", lambda: True)
    never_readable(monkeypatch, FakeClock())
    return runner, ["scan", "--profile", _DUPLEX_PROFILE, "--title", "Forgotten"]


def _site_broken_flip_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """Arrange a manual-duplex scan whose terminal fails at the flip prompt."""
    runner, _ = _patch_cli(
        monkeypatch,
        settings=_duplex_settings(tmp_path),
        scanner_cls=_counting_scanner([]),
        paperless_cls=_recording_paperless([]),
    )
    monkeypatch.setattr("saneless.cli._stdin_is_interactive", lambda: True)
    broken_read(monkeypatch, OSError(errno.EIO, "Input/output error"))
    return runner, ["scan", "--profile", _DUPLEX_PROFILE, "--title", "Lost"]


def _site_unknown_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """``scan --profile`` naming no profile in the configuration."""
    runner, _ = _patch_cli(monkeypatch, settings=_make_settings(tmp_path))
    return runner, ["scan", "--profile", "nonesuch"]


def _site_multi_page_manual_duplex(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """``--multi-page`` with a manual-duplex profile, at a terminal."""
    runner, _ = _patch_cli(monkeypatch, settings=_duplex_settings(tmp_path))
    monkeypatch.setattr("saneless.cli._stdin_is_interactive", lambda: True)
    return runner, ["scan", "--multi-page", "--profile", _DUPLEX_PROFILE]


def _site_multi_page_off_a_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """``--multi-page`` with no terminal to ask on; CliRunner is honestly none."""
    runner, _ = _patch_cli(monkeypatch, settings=_make_settings(tmp_path))
    return runner, ["scan", "--multi-page"]


def _site_manual_duplex_off_a_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """Arrange a manual-duplex scan with no terminal to prompt the flip on."""
    runner, _ = _patch_cli(monkeypatch, settings=_duplex_settings(tmp_path))
    return runner, ["scan", "--profile", _DUPLEX_PROFILE]


def _site_unwritable_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """``auto-profiles`` whose write of the config file is refused."""
    runner, _ = _patch_cli(monkeypatch, settings=_make_settings(tmp_path))

    def unwritable(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr("saneless.cli.write_profiles_to_config", unwritable)
    return runner, ["--config", str(tmp_path / "saneless.toml"), "auto-profiles"]


def _site_stale_only_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _stack: contextlib.ExitStack
) -> tuple[CliRunner, list[str]]:
    """``auto-profiles`` when the only config file has the superseded name."""
    settings = _make_settings(tmp_path)
    settings._config_discovery = _stale_only_discovery(tmp_path)
    runner, _ = _patch_cli(monkeypatch, settings=settings)
    return runner, ["auto-profiles"]


_FROM_A_TERMINAL = (
    "Run the scan again from an interactive terminal, or scan from the web UI."
)

_RAISE_SITES: dict[str, _RaiseSite] = {
    "python-sane missing": _RaiseSite(
        _site_without_python_sane,
        2,
        "python-sane cannot be imported",
        "Install the SANE development package and reinstall saneless, as the "
        "error says, then run the command again.",
    ),
    "port in use": _RaiseSite(
        _site_port_in_use,
        2,
        "Cannot bind to 127.0.0.1:",
        "Stop the program that is using that port, or set output.web_port (or "
        "pass --port) to a free port, then start saneless serve again.",
    ),
    "SANE will not start": _RaiseSite(
        _site_sane_will_not_start,
        2,
        "The web server could not start: SANE could not be initialised",
        "Check the SANE setup on this machine and scanner.host (saneless "
        "doctor shows what is wrong), then start saneless serve again.",
    ),
    "server never started": _RaiseSite(
        _site_server_never_started,
        2,
        "The web server could not start on http://127.0.0.1:",
        "Fix the problem the log lines above name, then start saneless serve again.",
    ),
    "unreadable trust store": _RaiseSite(
        _site_unreadable_trust_store,
        3,
        "Paperless error: Could not build the TLS trust store",
        "Check that SSL_CERT_FILE names a readable CA bundle file and "
        "SSL_CERT_DIR a readable directory, or unset them, then try again.",
    ),
    "empty --config": _RaiseSite(
        _site_empty_config_path,
        2,
        "Config file path is empty (was --config given an unset variable?)",
        "Give --config the path of a saneless config file, or leave --config "
        "out to use the usual search.",
    ),
    "missing --config": _RaiseSite(
        _site_missing_config_path,
        2,
        "Config file not found or not a regular file: ",
        "Give --config the path of an existing saneless config file, or leave "
        "--config out to use the usual search.",
    ),
    "shared tmp_dir": _RaiseSite(
        _site_shared_tmp_dir,
        2,
        "output.tmp_dir ",
        "Make output.tmp_dir a directory you own that nobody else can write "
        "to, or set output.tmp_dir to another directory, then try again.",
    ),
    "flip timeout": _RaiseSite(
        _site_flip_timeout,
        1,
        "Scan error: Manual duplex flip wait timed out",
        "Start the scan again and answer the flip prompt within "
        "output.operator_wait_timeout_seconds, or raise that setting.",
    ),
    "broken flip prompt": _RaiseSite(
        _site_broken_flip_prompt,
        1,
        "Scan error: Flip prompt failed",
        "Run the scan again from a working terminal.",
    ),
    "unknown profile": _RaiseSite(
        _site_unknown_profile,
        2,
        "Unknown profile: nonesuch",
        "Pass --profile the name of a profile in the saneless config file, or "
        "add a profile with that name to it, then run the scan again.",
    ),
    "multi-page with manual duplex": _RaiseSite(
        _site_multi_page_manual_duplex,
        2,
        multi_page_manual_duplex_refusal(_DUPLEX_PROFILE),
        "Run the scan again without --multi-page, or with a profile that is "
        "not manual duplex.",
    ),
    "multi-page off a terminal": _RaiseSite(
        _site_multi_page_off_a_terminal,
        2,
        MULTI_PAGE_NEEDS_TERMINAL,
        _FROM_A_TERMINAL,
    ),
    "manual duplex off a terminal": _RaiseSite(
        _site_manual_duplex_off_a_terminal,
        2,
        f"Profile '{_DUPLEX_PROFILE}' is manual duplex, which needs an "
        "interactive terminal: saneless must prompt you to flip the stack "
        "between the two passes. Run it from a terminal, or scan from the web UI.",
        _FROM_A_TERMINAL,
    ),
    "unwritable config": _RaiseSite(
        _site_unwritable_config,
        2,
        "Cannot write ",
        "Make that file and its folder writable by this user, or pass "
        "--config naming an existing config file this user can write, then "
        "run saneless auto-profiles again.",
    ),
    "stale-only config": _RaiseSite(
        _site_stale_only_config,
        2,
        "No config file loaded: saneless now reads saneless.toml",
        f"Rename the {LEGACY_CONFIG_FILENAME} named above to {CONFIG_FILENAME}, "
        "then run saneless auto-profiles again.",
    ),
}

# The five refusals that exit 2 through the group guard, each with the whole
# first line it prints.
_REFUSALS: dict[str, str] = {
    "unknown profile": "Unknown profile: nonesuch",
    "multi-page with manual duplex": multi_page_manual_duplex_refusal(_DUPLEX_PROFILE),
    "multi-page off a terminal": MULTI_PAGE_NEEDS_TERMINAL,
    "manual duplex off a terminal": _RAISE_SITES[
        "manual duplex off a terminal"
    ].line_one,
    "unwritable config": "",
}


class TestRaiseSiteAdvice:
    """
    Each known raise site's failure ends with its own fix, not its category's.

    Every case runs the whole command, so the next step is shown to travel
    from where the error is raised, through the group guard, to stderr.
    """

    @pytest.mark.parametrize(
        "site",
        [
            pytest.param(
                site,
                # The trust store is built by the real HTTP transport only.
                marks=[pytest.mark.real_paperless_transport]
                if site == "unreadable trust store"
                else [],
            )
            for site in _RAISE_SITES
        ],
    )
    def test_each_raise_site_prints_its_own_next_step(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, site: str
    ) -> None:
        """Line 1 is the failure line; line 2 is the site's own fix."""
        case = _RAISE_SITES[site]
        with contextlib.ExitStack() as stack:
            runner, args = case.setup(monkeypatch, tmp_path, stack)
            result = runner.invoke(cli, args)

        assert result.exit_code == case.exit_code, result.output
        lines = result.stderr.splitlines()
        assert lines[-1] == f"Try: {case.next_step}", result.stderr
        assert lines[-2].startswith(case.line_one), result.stderr
        fallbacks = {f"Try: {error_next_step(category)}" for category in ErrorCategory}
        assert lines[-1] not in fallbacks
        assert "Traceback" not in result.output

    @pytest.mark.parametrize("site", list(_REFUSALS))
    def test_refusals_are_logged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        site: str,
    ) -> None:
        """
        A refusal is logged at ERROR, as every other failure is, and exits 2.

        Its first line is the refusal's own line, byte for byte.
        """
        case = _RAISE_SITES[site]
        caplog.set_level(logging.ERROR, logger="saneless.cli")
        with contextlib.ExitStack() as stack:
            runner, args = case.setup(monkeypatch, tmp_path, stack)
            result = runner.invoke(cli, args)

        assert result.exit_code == 2, result.output
        line_one = _failure_lines(result)[-1]
        expected = _REFUSALS[site] or (
            f"Cannot write {tmp_path / 'saneless.toml'}: Permission denied"
        )
        assert line_one == expected
        records = [
            record
            for record in _cli_error_records(caplog)
            if record.getMessage().startswith(
                ("saneless scan failed", "saneless auto-profiles failed")
            )
        ]
        assert len(records) == 1, caplog.text
        assert expected in records[0].getMessage()


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (errno.EADDRINUSE, "Stop the program that is using that port"),
        (errno.EACCES, "not allowed to listen on that port"),
        (errno.EADDRNOTAVAIL, "Set output.web_host (or pass --host)"),
        (errno.EAFNOSUPPORT, "Check output.web_host and output.web_port"),
    ],
)
def test_a_bind_failure_next_step_fits_its_cause(error: int, fragment: str) -> None:
    """
    The bind advice follows the errno, so it cannot send anyone the wrong way.

    A port in use, a port this user may not take and an address this machine
    does not have are three different fixes; anything else gets advice that
    names both settings without guessing which is wrong.
    """
    step = cli_module._bind_next_step(OSError(error, os.strerror(error)))

    assert fragment in step
    assert step.endswith("then start saneless serve again.")


def _refusing_paperless() -> type:
    """
    Build a PaperlessClient stand-in whose construction fails the test.

    ``scan`` builds the client after the scanner, so reaching it at all would
    mean the token refusal ran too late.

    Returns:
        A class that raises if it is ever constructed.

    """

    class _NeverBuilt:
        """A paperless client that must not be reached."""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            """Fail loudly: the token refusal comes before this point."""
            msg = "PaperlessClient was built despite a placeholder token"
            raise AssertionError(msg)

    return _NeverBuilt


def _open_counting_scanner(opens: list[str]) -> type[ScannerBackend]:
    """
    Build a scanner class that records every construction and device query.

    "Before the scanner is opened" is the whole point of the token refusal,
    so it is asserted from a counter rather than assumed from the exit code.

    Args:
        opens: The list every call appends its name to.

    Returns:
        A ``ScannerBackend`` subclass sharing that list.

    """

    class _CountingScanner(StubScannerBackend):
        """A stub that records being opened."""

        def __init__(self, host: str = "") -> None:
            """Record the construction -- this is what "opened" means here."""
            opens.append("SaneBackend")

        def get_devices(self) -> list[DeviceInfo]:
            """Record the query and report one device."""
            opens.append("get_devices")
            return [DeviceInfo("test:device:001", "Test", "Model", "flatbed scanner")]

    return _CountingScanner


def _token_settings(
    tmp_path: Path,
    value: str,
    consume_dir: str = "",
    url: str = "http://paperless.invalid",
) -> Settings:
    """
    Build settings carrying ``value`` as the paperless-ngx token.

    Args:
        tmp_path: The test's own temporary directory.
        value: The configured token, placeholder or not.
        consume_dir: A fallback consume directory, when the test needs one.
        url: The configured paperless-ngx address; empty means unset.

    Returns:
        Settings otherwise identical to the suite's defaults.

    """
    return _make_settings(
        tmp_path,
        paperless=PaperlessConfig(url=url, token=value, consume_dir=consume_dir),
    )


class TestScanTokenRefusal:
    """
    ``saneless scan`` refuses a placeholder token before paper moves.

    The check runs before the scanner is opened, it is unconditional, and the
    three commands that never talk to paperless-ngx are untouched.
    """

    def test_a_blank_token_is_a_placeholder_and_exits_2_before_the_scanner(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An unset token refuses with exit 2 and zero scanner opens."""
        opens: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_token_settings(tmp_path, ""),
            scanner_cls=_open_counting_scanner(opens),
            paperless_cls=_refusing_paperless(),
        )

        result = runner.invoke(cli, ["scan", "--title", "Tax return"])

        assert result.exit_code == 2, result.output
        assert opens == []

    @pytest.mark.parametrize("value", ["changeme", "your-api-token-here", "   "])
    def test_a_placeholder_token_exits_2_before_the_scanner(
        self, monkeypatch: pytest.MonkeyPatch, value: str, tmp_path: Path
    ) -> None:
        """Every member of the literal set refuses the same way."""
        opens: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_token_settings(tmp_path, value),
            scanner_cls=_open_counting_scanner(opens),
            paperless_cls=_refusing_paperless(),
        )

        result = runner.invoke(cli, ["scan", "--title", "Tax return"])

        assert result.exit_code == 2, result.output
        assert opens == []

    def test_a_real_token_is_not_a_placeholder_and_scans(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A real-looking token leaves ``scan`` exactly as it was."""
        runner, _ = _patch_cli(
            monkeypatch, settings=_token_settings(tmp_path, "a-real-token")
        )

        result = runner.invoke(cli, ["scan", "--title", "Tax return"])

        assert result.exit_code == 0, result.output
        assert "Done: Tax return" in result.output

    def test_a_consume_dir_fallback_does_not_soften_the_placeholder_refusal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A configured consume-dir fallback does not soften the token refusal."""
        opens: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_token_settings(tmp_path, "changeme", consume_dir=str(tmp_path)),
            scanner_cls=_open_counting_scanner(opens),
            paperless_cls=_refusing_paperless(),
        )

        result = runner.invoke(cli, ["scan", "--title", "Tax return"])

        assert result.exit_code == 2, result.output
        assert opens == []

    def test_the_placeholder_refusal_names_the_title_and_profile_then_advises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """As a ConfigError it names the title and profile, then gives the advice."""
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_token_settings(tmp_path, "changeme"),
            scanner_cls=_open_counting_scanner([]),
            paperless_cls=_refusing_paperless(),
        )

        result = runner.invoke(cli, ["scan", "--title", "Tax return"])

        lines = result.stderr.splitlines()
        assert lines[0] == (
            "Scanning 'Tax return' with profile 'default': "
            "the paperless-ngx API token has not been set"
        )
        assert lines[1] == f"Try: {error_next_step(ErrorCategory.CONFIG)}"

    def test_the_placeholder_refusal_never_echoes_the_value(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The token is passed to the predicate, never rendered."""
        secret = "your-token-here"
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_token_settings(tmp_path, secret),
            scanner_cls=_open_counting_scanner([]),
            paperless_cls=_refusing_paperless(),
        )

        result = runner.invoke(cli, ["scan", "--title", "Tax return"])

        assert result.exit_code == 2
        assert secret not in result.output
        assert secret not in result.stderr

    def test_an_unset_url_exits_2_before_the_scanner(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        An empty ``paperless.url`` refuses like a placeholder token.

        The upload would fail for certain, so feeding the stack first would
        only waste the paper and leave the PDF in ``failed/``.
        """
        opens: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_token_settings(tmp_path, "a-real-token", url=""),
            scanner_cls=_open_counting_scanner(opens),
            paperless_cls=_refusing_paperless(),
        )

        result = runner.invoke(cli, ["scan", "--title", "Tax return"])

        assert result.exit_code == 2, result.output
        assert opens == []
        lines = result.stderr.splitlines()
        assert lines[0] == (
            "Scanning 'Tax return' with profile 'default': "
            "the paperless-ngx address in paperless.url has not been set"
        )
        assert lines[1] == f"Try: {error_next_step(ErrorCategory.CONFIG)}"

    def test_a_consume_dir_fallback_does_not_soften_the_unset_url_refusal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No copy is made for an unset URL, so a scan must not start either."""
        opens: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_token_settings(
                tmp_path, "a-real-token", consume_dir=str(tmp_path), url=""
            ),
            scanner_cls=_open_counting_scanner(opens),
            paperless_cls=_refusing_paperless(),
        )

        result = runner.invoke(cli, ["scan", "--title", "Tax return"])

        assert result.exit_code == 2, result.output
        assert opens == []

    def test_devices_is_unaffected_by_a_placeholder_token(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``devices`` never talks to paperless-ngx, so it still exits 0."""
        runner, _ = _patch_cli(
            monkeypatch, settings=_token_settings(tmp_path, "changeme")
        )

        result = runner.invoke(cli, ["devices"])

        assert result.exit_code == 0, result.output
        assert "Epson" in result.output

    def test_jobs_is_unaffected_by_a_placeholder_token(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``jobs`` reads the local database only, so it still exits 0."""
        settings = _token_settings(tmp_path, "changeme")
        settings.output = OutputConfig(
            tmp_dir=str(tmp_path),
            data_dir=str(tmp_path),
            log_file=str(tmp_path / "saneless.log"),
        )
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])

        assert result.exit_code == 0, result.output

    @pytest.mark.usefixtures("patched_search_paths")
    def test_auto_profiles_is_unaffected_by_a_placeholder_token(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``auto-profiles`` only reads the scanner, so it still exits 0."""
        settings = _token_settings(tmp_path, "changeme")
        settings._config_discovery = _searched()
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["auto-profiles"])

        assert result.exit_code == 0, result.output


@pytest.fixture
def cli_local_zone(monkeypatch: pytest.MonkeyPatch) -> Generator[Callable[[str], None]]:
    """
    Give one test control of the process's local zone, then restore it.

    The same technique as test_vocabulary's ``local_zone``: ``local_time``
    renders whatever zone the C library reports, so pinning ``TZ`` and calling
    ``time.tzset()`` is the only way to assert an exact string on a host in an
    unknown zone. The trailing ``tzset`` is what makes the C library notice the
    variable monkeypatch removed.

    Yields:
        A function that switches the process's zone for the rest of the test.

    """

    def _use(zone: str) -> None:
        monkeypatch.setenv("TZ", zone)
        time.tzset()

    yield _use
    time.tzset()


def _jobs_header_title_width(header: str) -> int:
    """
    Measure the Title column's width from a rendered ``jobs`` header.

    Measured rather than recomputed from the CLI's own constants, so a test
    that says "24 at 80 columns" is reading the output an operator sees.
    ``Title`` is padded to its width and ``Status`` follows one space later.

    Args:
        header: The first line ``saneless jobs`` printed.

    Returns:
        The Title column's width in characters.

    """
    return header.index("Status") - header.index("Title") - 1


class TestJobsTableWidth:
    """The human ``jobs`` table renders local time and still fits."""

    @staticmethod
    def _settings_for(tmp_path: Path) -> Settings:
        """Build Settings whose data_dir -- and so db_path -- is tmp_path."""
        return _make_settings(
            tmp_path,
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                data_dir=str(tmp_path),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )

    def _one_job(self, db_path: Path) -> Job:
        """Create one job and return it as the store recorded it."""
        store = JobStore(db_path=db_path)
        store.create_job(profile="default", title="Invoice")
        recorded = store.list_recent(limit=1)[0]
        store.close()
        return recorded

    def test_the_cli_and_the_web_share_one_formatter_object(self) -> None:
        """cli.py imports the formatter; it does not re-derive the format."""
        assert cli_module.local_time is vocabulary_module.local_time

    def test_the_table_renders_local_time_with_the_zone_named(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        cli_local_zone: Callable[[str], None],
    ) -> None:
        """Each row's timestamp is local_time's output, seconds dropped."""
        cli_local_zone("America/Chicago")
        settings = self._settings_for(tmp_path)
        job = self._one_job(settings.output.db_path)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])

        assert result.exit_code == 0, result.output
        row = result.output.strip().split("\n")[2]
        assert row.startswith(local_time(job.created_at))
        assert job.created_at.strftime("%Y-%m-%d %H:%M:%S") not in row

    def test_the_timestamp_column_width_is_derived_not_written_down(self) -> None:
        """A six-character zone token cannot silently truncate the column."""
        assert len(local_time(datetime.now(tz=UTC))) <= cli_module._TIME_COL_WIDTH
        assert cli_module._TIME_COL_WIDTH == 22

    def test_the_title_column_is_15_at_80_columns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        At 80 columns the title keeps its floor of 15.

        The Status column is wide enough for "Waiting: blank pages found",
        and the Profile column gives up one character so the title does not
        drop below its floor.  The zone token fits in the timestamp column's
        own slack.
        """
        monkeypatch.setenv("COLUMNS", "80")
        settings = self._settings_for(tmp_path)
        self._one_job(settings.output.db_path)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])

        assert result.exit_code == 0, result.output
        lines = result.output.strip().split("\n")
        assert _jobs_header_title_width(lines[0]) == 15
        assert all(len(line) <= 80 for line in lines)

    def test_a_narrow_terminal_floors_the_title_column_at_15(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """40 columns still renders a table rather than raising."""
        monkeypatch.setenv("COLUMNS", "40")
        settings = self._settings_for(tmp_path)
        self._one_job(settings.output.db_path)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])

        assert result.exit_code == 0, result.output
        lines = result.output.strip().split("\n")
        assert _jobs_header_title_width(lines[0]) == 15


class TestJobsJsonContract:
    """``jobs --json`` stays UTC ISO-8601: it is a machine contract."""

    @staticmethod
    def _settings_for(tmp_path: Path) -> Settings:
        """Build Settings whose data_dir -- and so db_path -- is tmp_path."""
        return _make_settings(
            tmp_path,
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                data_dir=str(tmp_path),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )

    def test_created_at_is_utc_iso_8601_whatever_the_servers_zone(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        cli_local_zone: Callable[[str], None],
    ) -> None:
        """The offset is +00:00 even on a server the table renders as CDT."""
        cli_local_zone("America/Chicago")
        settings = self._settings_for(tmp_path)
        store = JobStore(db_path=settings.output.db_path)
        store.create_job(profile="default", title="Invoice")
        recorded = store.list_recent(limit=1)[0]
        store.close()
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--json"])

        assert result.exit_code == 0, result.output
        created_at = json.loads(result.output)[0]["created_at"]
        assert created_at.endswith("+00:00")
        assert datetime.fromisoformat(created_at) == recorded.created_at

    def test_json_never_carries_the_local_rendering(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        cli_local_zone: Callable[[str], None],
    ) -> None:
        """Localising a machine contract would break every script silently."""
        cli_local_zone("America/Chicago")
        settings = self._settings_for(tmp_path)
        job = JobStore(db_path=settings.output.db_path)
        job.create_job(profile="default", title="Invoice")
        recorded = job.list_recent(limit=1)[0]
        job.close()
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--json"])

        assert result.exit_code == 0, result.output
        listed = json.loads(result.output)
        assert [row["id"] for row in listed] == [recorded.id]
        assert local_time(recorded.created_at) not in result.output


def _raise_runtime_error(message: str) -> None:
    """
    Raise a RuntimeError carrying ``message``, so a test can log a real traceback.

    Args:
        message: The exception's message.

    Raises:
        RuntimeError: Always.

    """
    raise RuntimeError(message)


@pytest.mark.usefixtures("uvicorn_loggers_restored")
class TestServeLogging:
    """
    ``serve`` streams to stderr and writes no log file.

    Every test here patches in the **real** ``configure_logging``: a stub that
    attaches nothing can neither prove a handler was attached nor prove one
    was not.
    """

    @staticmethod
    def _serve_settings(tmp_path: Path) -> Settings:
        """
        Build settings whose ``log_file`` parent directory does not exist yet.

        A file handler would have to create ``logs/`` to attach, so the
        directory's absence afterwards is proof that none did.

        Args:
            tmp_path: The test's temporary directory.

        Returns:
            Settings serving on 127.0.0.1, on a port the OS chooses, with a
            tmp_path-rooted log file.

        """
        return _make_settings(
            tmp_path,
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                data_dir=str(tmp_path),
                log_file=str(tmp_path / "logs" / "saneless.log"),
                web_host="127.0.0.1",
                web_port=0,
            ),
        )

    @staticmethod
    def _real_logging(monkeypatch: pytest.MonkeyPatch) -> None:
        """Undo ``_patch_cli``'s stub, so the real configure_logging runs."""
        monkeypatch.setattr("saneless.cli.configure_logging", configure_logging)

    def test_serve_attaches_no_file_handler_and_creates_no_log_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A service writes no file: the platform owns retention."""
        settings = self._serve_settings(tmp_path)
        TestServeCommand._stub_create_app(monkeypatch)
        _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        self._real_logging(monkeypatch)

        with _restored_root_logging():
            result = runner.invoke(cli, ["serve"])
            root_handlers = logging.getLogger().handlers[:]

        assert result.exit_code == 0, result.output
        # pytest keeps its own /dev/null FileHandler on the root logger, so the
        # assertion names the handler configure_logging would have attached.
        assert not [
            h
            for h in root_handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert not (tmp_path / "logs").exists()

    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
    def test_serve_announces_its_address_once_at_any_log_level(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, level: LogLevel
    ) -> None:
        """
        The Serving on line reaches stderr exactly once, whatever the level.

        A service's log stream is stderr too, so printing the line and also
        logging it would put the same fact on the same stream twice; a record
        alone would vanish at ``log_level = "WARNING"``.
        """
        settings = self._serve_settings(tmp_path)
        settings.output.log_level = level
        TestServeCommand._stub_create_app(monkeypatch)
        runs = _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        self._real_logging(monkeypatch)

        with _restored_root_logging():
            result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 0, result.output
        [run] = runs
        assert result.stderr.count("Serving on") == 1
        assert f"Serving on http://127.0.0.1:{run.port}\n" in result.stderr

    def test_serve_attaches_one_stderr_stream_handler(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Exactly one stream handler carries the service's records.

        The handlers are counted by difference against a snapshot, because
        pytest keeps capture handlers of its own on the root logger. That the
        one handler writes to stderr rather than stdout is pinned by
        ``test_serve_stream_renders_a_traceback``.
        """
        settings = self._serve_settings(tmp_path)
        TestServeCommand._stub_create_app(monkeypatch)
        _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        self._real_logging(monkeypatch)
        before = {id(h) for h in logging.getLogger().handlers}

        with _restored_root_logging():
            result = runner.invoke(cli, ["serve"])
            added = [h for h in logging.getLogger().handlers if id(h) not in before]

        assert result.exit_code == 0, result.output
        assert len(added) == 1
        assert isinstance(added[0], logging.StreamHandler)
        assert not isinstance(added[0], logging.FileHandler)

    def test_serve_sets_log_file_to_none_in_the_context(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Nothing can print "Full details in <log_file>" for a service.

        ``configure_logging`` reports that no file handler attached, so the
        context records no log file and the guard's hint stays silent.
        """
        settings = self._serve_settings(tmp_path)
        TestServeCommand._stub_create_app(monkeypatch)
        _fake_server_run(monkeypatch)
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        self._real_logging(monkeypatch)
        obj: dict[str, object] = {}

        with _restored_root_logging():
            result = runner.invoke(cli, ["serve"], obj=obj)

        assert result.exit_code == 0, result.output
        assert obj["logging_configured"] is True
        assert obj["log_file"] is None

    @staticmethod
    def _uvicorn_logs_a_traceback(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
        """Patch the server's run to log an exception through saneless's logger."""

        def logging_run(
            self: uvicorn.Server, sockets: list[socket.socket] | None = None
        ) -> None:
            self.started = True
            try:
                _raise_runtime_error(marker)
            except RuntimeError:
                logging.getLogger("saneless.test").exception("the worker died")
            for sock in sockets or []:
                sock.close()

        monkeypatch.setattr(uvicorn.Server, "run", logging_run)

    @pytest.mark.parametrize("args", [["serve"], ["-v", "serve"]])
    def test_serve_stream_renders_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: list[str]
    ) -> None:
        """
        The serve stream renders tracebacks with and without ``-v``.

        A one-shot command keeps tracebacks off stderr because stderr is the
        user's terminal. For a service the stream *is* the log, journald is
        nobody's terminal, and no file carries the traceback instead, so a
        traceback-free stream would discard every one of them.
        """
        marker = "kaboom-serve-8a3c"
        settings = self._serve_settings(tmp_path)
        TestServeCommand._stub_create_app(monkeypatch)
        self._uvicorn_logs_a_traceback(monkeypatch, marker)
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        self._real_logging(monkeypatch)

        with _restored_root_logging():
            result = runner.invoke(cli, args)

        assert result.exit_code == 0, result.output
        assert "the worker died" in result.stderr
        assert result.stderr.count("Traceback") == 1
        assert marker in result.stderr
        # Everything serve prints, the address line included, is on stderr.
        assert result.stdout == ""

    def test_serve_never_offers_the_verbose_hint_on_an_unexpected_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        An exit-5 line under ``serve`` does not tell the operator to restart.

        The stream already rendered the traceback, so "Run
        again with -v to see the traceback" would send someone to restart a
        running service to see what is printed directly above the line.
        """
        settings = self._serve_settings(tmp_path)
        TestServeCommand._stub_create_app(monkeypatch)

        def exploding_run(_self: uvicorn.Server, **_kwargs: object) -> None:
            _raise_runtime_error("kaboom-serve-exit5")

        monkeypatch.setattr(uvicorn.Server, "run", exploding_run)
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        self._real_logging(monkeypatch)

        with _restored_root_logging():
            result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 5, result.output
        assert "Unexpected error (RuntimeError): kaboom-serve-exit5" in result.stderr
        assert cli_module._VERBOSE_HINT not in result.stderr
        assert "Full details in" not in result.stderr
        assert "Traceback" in result.stderr

    def test_serve_logs_an_expected_failure_without_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A port that is taken is a setup problem, reported with no traceback.

        The real ``configure_logging`` runs, so the stream handler that would
        render a traceback is really attached: a stub that attaches nothing
        would pass this test whatever the guard logged.  The port is held by a
        real listening socket, so the bind failure is the real one.  The
        record still names the failure, and the failure line is printed once.
        """
        settings = self._serve_settings(tmp_path)
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        self._real_logging(monkeypatch)

        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder,
            _restored_root_logging(),
        ):
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            port = holder.getsockname()[1]
            result = runner.invoke(
                cli, ["serve", "--host", "127.0.0.1", "--port", str(port)]
            )

        assert result.exit_code == 2, result.output
        assert "Traceback" not in result.stderr
        lines = result.stderr.splitlines()
        failure = [
            line
            for line in lines
            if line.startswith(f"Cannot bind to 127.0.0.1:{port}")
        ]
        assert len(failure) == 1, result.stderr
        assert lines[-1].startswith("Try: ")
        assert lines[-2] == failure[0]
        assert "saneless serve failed" in result.stderr

    def test_one_shot_command_still_attaches_the_file_handler(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A one-shot command gets the rotating file handler, unlike ``serve``.

        ``jobs`` records ``log_file``, so "Full details in <log_file>" works.
        A failure here means the service mode leaked into CLI mode.
        """
        log_file = tmp_path / "logs" / "saneless.log"
        settings = _make_settings(
            tmp_path,
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                data_dir=str(tmp_path),
                log_file=str(log_file),
            ),
        )
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        self._real_logging(monkeypatch)
        obj: dict[str, object] = {}

        with _restored_root_logging():
            result = runner.invoke(cli, ["jobs"], obj=obj)
            rotating = [
                h
                for h in logging.getLogger().handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            attached_to = [h.baseFilename for h in rotating]

        assert result.exit_code == 0, result.output
        assert attached_to == [str(log_file)]
        assert obj["log_file"] == log_file
        assert log_file.parent.is_dir()


_HOSTILE_DEVICE_NAME = "dev\x1b"
_HOSTILE_BROKEN_NAME = "broken\x1b[2J"


class _HostileScanner(StubScannerBackend):
    """
    A LAN device whose every reported string carries terminal controls.

    The second device's probe fails, so the stderr line naming it is exercised
    too.
    """

    def __init__(self, host: str = "") -> None:
        """Accept host parameter for API compatibility."""

    def get_devices(self) -> list[DeviceInfo]:
        """Return a device whose strings hold ESC and CSI, and one that fails."""
        return [
            DeviceInfo(_HOSTILE_DEVICE_NAME, "Vend\x1b", "\x1b[2J", "scanner\x9b"),
            DeviceInfo(_HOSTILE_BROKEN_NAME, "V", "M", "scanner"),
        ]

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """Report control characters in every label, or fail for the second."""
        if device_id == _HOSTILE_BROKEN_NAME:
            msg = "Could not open the device"
            raise ScanError(msg)
        return DeviceCapabilities(
            sources=["Flat\x1b[2Jbed"],
            resolutions=[300],
            modes=["co\x9blor"],
            option_names=("src\x1bopt",),
        )


def _control_free(text: str) -> bool:
    """Return whether ``text`` holds no ESC, CSI or other terminal control."""
    return not any(ch in text for ch in ("\x1b", "\x9b", "\x85", "\x7f"))


class TestControlCharactersAtCliSinks:
    """Human-readable CLI output never carries a raw control character."""

    def test_devices_table_neutralises_control_characters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A device name, vendor and model with ESC print as visible escapes."""
        runner, _ = _patch_cli(monkeypatch, scanner_cls=_HostileScanner)

        result = runner.invoke(cli, ["devices"])

        assert result.exit_code == 0, result.output
        assert _control_free(result.stdout), repr(result.stdout)
        assert _control_free(result.stderr), repr(result.stderr)
        assert "dev\\x1b" in result.stdout
        assert "\\x1b[2J" in result.stdout

    def test_devices_capabilities_neutralise_control_characters(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Capability labels, both "Capabilities for" lines and the log hold none."""
        runner, _ = _patch_cli(monkeypatch, scanner_cls=_HostileScanner)

        with caplog.at_level(logging.WARNING, logger="saneless.cli"):
            result = runner.invoke(cli, ["devices", "--capabilities"])

        assert result.exit_code == 1, result.output
        assert _control_free(result.stdout), repr(result.stdout)
        assert _control_free(result.stderr), repr(result.stderr)
        assert "Capabilities for dev\\x1b:" in result.stdout
        assert "Flat\\x1b[2Jbed" in result.stdout
        assert "co\\x9blor" in result.stdout
        assert "src\\x1bopt" in result.stdout
        assert "Capabilities for broken\\x1b[2J:" in result.stderr
        messages = [record.getMessage() for record in caplog.records]
        assert messages
        assert all(_control_free(message) for message in messages), messages

    def test_devices_json_keeps_the_raw_name_for_the_machine_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--json`` round-trips the raw name; ``json.dumps`` escapes it."""
        runner, _ = _patch_cli(monkeypatch, scanner_cls=_HostileScanner)

        result = runner.invoke(cli, ["devices", "--json"])

        assert result.exit_code == 0, result.output
        assert _control_free(result.stdout), repr(result.stdout)
        data = json.loads(result.stdout)
        assert data[0]["name"] == _HOSTILE_DEVICE_NAME

    @staticmethod
    def _stored_hostile_job(tmp_path: Path) -> Settings:
        """
        Store one job whose title and profile carry terminal controls.

        Returns:
            Settings whose database holds that job.

        """
        settings = _make_settings(
            tmp_path,
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                data_dir=str(tmp_path),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )
        store = JobStore(db_path=settings.output.db_path)
        store.create_job(profile="p\x1b", title="t\x1b[31m")
        store.close()
        return settings

    def test_jobs_table_neutralises_control_characters(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A stored title or profile holding ESC prints as a visible escape."""
        settings = self._stored_hostile_job(tmp_path)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])

        assert result.exit_code == 0, result.output
        assert _control_free(result.stdout), repr(result.stdout)
        assert "t\\x1b[31m" in result.stdout
        assert "p\\x1b" in result.stdout

    def test_jobs_json_keeps_the_raw_title_despite_control_characters(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``jobs --json`` round-trips the stored title unchanged."""
        settings = self._stored_hostile_job(tmp_path)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--json"])

        assert result.exit_code == 0, result.output
        assert _control_free(result.stdout), repr(result.stdout)
        data = json.loads(result.stdout)
        assert data[0]["title"] == "t\x1b[31m"
        assert data[0]["profile"] == "p\x1b"


# A scanner failure naming a device as LAN discovery could report it.
_HOSTILE_SCAN_FAILURE = "Could not open scanner net:evil\x1b]0;owned\x07:0: busy"


class _HostileProbeScanner(StubScannerBackend):
    """A device whose capability probe fails naming it with a terminal escape."""

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """Fail the way a device named with an escape would."""
        raise ScanError(_HOSTILE_SCAN_FAILURE)


class TestControlCharactersInFailureSinks:
    """A failure's text reaches the terminal and the log with its controls escaped."""

    @pytest.mark.parametrize(
        ("category", "exc_type"),
        [pair for pair in _ADVISED_CATEGORIES if pair[0] is not ErrorCategory.CONFIG],
    )
    def test_failure_line_escapes_controls(
        self, category: ErrorCategory, exc_type: type[SanelessError]
    ) -> None:
        """The one stderr line for a failure carries no live control character."""
        line = _failure_line(exc_type(_HOSTILE_SCAN_FAILURE), category)

        assert _control_free(line), repr(line)
        assert "net:evil\\x1b]0;owned\\x07:0" in line

    def test_failure_line_keeps_a_configuration_error_as_written(self) -> None:
        """The loader's multi-line configuration report is printed unchanged."""
        report = "Configuration error in saneless.toml:\n  web.port: bad"

        assert _failure_line(ConfigError(report), ErrorCategory.CONFIG) == report

    def test_failure_log_line_escapes_controls(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The ERROR record for a failure quotes its text with controls escaped."""
        ctx = click.Context(cli, obj={"logging_configured": True})
        ctx.invoked_subcommand = "scan"
        caplog.set_level(logging.ERROR, logger="saneless.cli")

        cli_module._log_failure(ctx, ScanError(_HOSTILE_SCAN_FAILURE))

        [record] = [r for r in caplog.records if r.name == "saneless.cli"]
        assert _control_free(record.getMessage()), record.getMessage()

    def test_capability_probe_log_line_escapes_controls(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The WARNING for a failed capability probe quotes the reason escaped."""
        caplog.set_level(logging.WARNING, logger="saneless.cli")

        reason = cli_module._probe_capabilities(_HostileProbeScanner(), "net:0")

        assert reason == _HOSTILE_SCAN_FAILURE
        [record] = [r for r in caplog.records if r.name == "saneless.cli"]
        assert _control_free(record.getMessage()), record.getMessage()


# Enough history that either rendering overflows the pipe's buffer: the
# command is still writing when its reader goes away, as under
# `saneless jobs | head`, rather than finished before the reader closed.
_CLOSED_PIPE_JOBS = 3000

_CLOSED_PIPE_CHILD_SECONDS = 60

# The shell's code for a process its reader went away from: 128 + SIGPIPE.
_CLOSED_PIPE_EXIT = 128 + signal.SIGPIPE

_CLOSED_PIPE_CONFIG = """\
[paperless]
url = "http://paperless.invalid:8000"
token = "closed-pipe-token"

[output]
tmp_dir = "{tmp_dir}"
data_dir = "{data_dir}"
log_file = "{log_file}"

[profiles.default]
"""

# The console script's shim, the arguments travelling in the environment and
# taken out of it before the CLI starts, because saneless reads every
# SANELESS_ variable as a setting.
_CLOSED_PIPE_CHILD = """
import json
import os
import sys

from saneless import main

config = os.environ.pop("SANELESS_TEST_CONFIG")
args = json.loads(os.environ.pop("SANELESS_TEST_ARGS"))
sys.argv = ["saneless", "--config", config, "jobs", *args]
sys.exit(main())
"""


class TestJobsIntoAClosedPipe:
    """``saneless jobs | head``: the reader goes away and saneless exits quietly."""

    @pytest.mark.parametrize(
        ("args", "taken"),
        [([], "lines"), (["--json"], "bytes")],
        ids=["table", "json"],
    )
    def test_jobs_into_a_closed_pipe_exits_141(
        self, tmp_path: Path, args: list[str], taken: str
    ) -> None:
        """
        A reader that stops early ends ``jobs`` with 141 and nothing on stderr.

        The reader taking a few lines and closing its end is not a failure of
        saneless's and not a bug: it is the shell's broken pipe, 128 plus
        SIGPIPE.  Nothing is printed about it, and the log keeps no traceback,
        because there is nothing for anyone to fix.  A real process and a real
        pipe, because the interpreter's own last flush of stdout is part of
        what has to stay quiet.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        log_file = tmp_path / "logs" / "saneless.log"
        store = JobStore(db_path=data_dir / "saneless.db")
        try:
            for index in range(_CLOSED_PIPE_JOBS):
                store.create_job(profile="default", title=f"Closed pipe {index}")
        finally:
            store.close()
        config = tmp_path / "saneless.toml"
        config.write_text(
            _CLOSED_PIPE_CONFIG.format(
                tmp_dir=tmp_path / "scratch", data_dir=data_dir, log_file=log_file
            )
        )
        env = {
            **os.environ,
            "SANELESS_TEST_CONFIG": str(config),
            "SANELESS_TEST_ARGS": json.dumps(
                [*args, "--limit", str(_CLOSED_PIPE_JOBS + 2000)]
            ),
        }
        with subprocess.Popen(
            [sys.executable, "-c", _CLOSED_PIPE_CHILD],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).resolve().parents[1],
        ) as child:
            assert child.stdout is not None
            if taken == "lines":
                read = [child.stdout.readline() for _ in range(3)]
                assert all(read), read
            else:
                assert len(child.stdout.read(40)) == 40
            child.stdout.close()
            _, stderr = child.communicate(timeout=_CLOSED_PIPE_CHILD_SECONDS)

        assert child.returncode == _CLOSED_PIPE_EXIT, stderr
        assert stderr == b"", stderr
        log = log_file.read_text() if log_file.exists() else ""
        assert "Traceback" not in log, log
        assert "Unexpected error" not in log, log
