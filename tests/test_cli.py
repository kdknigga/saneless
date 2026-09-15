"""Tests for CLI commands via click.testing.CliRunner."""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import click
import pytest
import tomlkit
from click.testing import CliRunner
from PIL import Image, ImageDraw

import saneless.cli as cli_module
from saneless.cli import ClickFlipCoordinator, _truncate, cli
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.exceptions import (
    ConfigError,
    PaperlessError,
    PdfError,
    ScanCancelledError,
    ScanError,
    StorageError,
)
from saneless.job import JobStore
from saneless.paperless import UploadResult
from saneless.pipeline import PipelineEvent, PipelineRequest
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScanBatch,
    ScannerBackend,
    ScanSettings,
)
from saneless.vocabulary import FlipOutcome, JobState, state_label

if TYPE_CHECKING:
    from collections.abc import Generator

    from click.testing import Result

_TEST_TMP = str(Path(tempfile.gettempdir()) / "saneless-test")
# Keeps the suite out of the developer's real ~/.local/state/saneless.
_TEST_DATA = str(Path(tempfile.gettempdir()) / "saneless-test" / "data")
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
            data_dir=_TEST_DATA,
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

    def _fake_load_settings(config_path: str | None = None) -> Settings:
        """
        Return the test settings, recording ``--config`` as load_settings does.

        ``auto-profiles`` writes to ``settings.config_path`` (D-16); a stub that
        dropped the path would send its writes to ``./saneless.toml`` in the
        suite's working directory.
        """
        settings._config_path = Path(config_path) if config_path else None
        return settings

    monkeypatch.setattr("saneless.cli.load_settings", _fake_load_settings)
    monkeypatch.setattr(
        "saneless.cli.configure_logging",
        lambda *_args, **_kwargs: None,
    )

    if scanner_cls is not None:
        monkeypatch.setattr("saneless.cli.SaneBackend", scanner_cls)
    else:

        class MockSaneBackend(ScannerBackend):
            """
            Mock scanner backend for CLI tests.

            Subclasses the ABC so the type checkers can see the contract at
            all. This was one of the two CLI stubs that would *not* have
            failed when ScanBatch replaced the generator in this phase --
            every stub that does subclass was caught by the checkers.
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

            def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
                """Return a batch holding a single test image with content."""
                img = Image.new("RGB", (100, 100), "white")
                draw = ImageDraw.Draw(img)
                draw.rectangle([10, 10, 90, 90], fill="black")
                return ScanBatch(pages=[img], actual_resolution=300, pages_rejected=0)

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
                return UploadResult(delivered_to_api=True, task_uuid="mock-task-uuid")

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
        monkeypatch: pytest.MonkeyPatch, args: list[str]
    ) -> tuple[Result, PipelineRequest | None]:
        """
        Run ``scan`` against a ``receipt`` profile titled "Receipt".

        ``run_pipeline`` is replaced by a recorder that captures the request and
        reports DONE through its callback, as the real pipeline does.

        Args:
            monkeypatch: The test's monkeypatch fixture.
            args: The CLI arguments.

        Returns:
            The CliRunner result and the captured request, if the pipeline ran.

        """
        settings = _make_settings(
            profiles={
                "default": ProfileConfig(),
                "receipt": ProfileConfig(title="Receipt"),
            }
        )
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        captured: list[PipelineRequest] = []

        def capturing_pipeline(
            _scanner: object,
            _paperless: object,
            _settings: object,
            request: PipelineRequest,
        ) -> None:
            captured.append(request)
            if request.status_callback is not None:
                request.status_callback(PipelineEvent.DONE)

        monkeypatch.setattr("saneless.cli.run_pipeline", capturing_pipeline)
        result = runner.invoke(cli, args)
        return result, (captured[0] if captured else None)

    @pytest.mark.parametrize("typed", [None, "", "   "])
    def test_scan_blank_title_uses_the_profile_title(
        self, monkeypatch: pytest.MonkeyPatch, typed: str | None
    ) -> None:
        """An omitted or blank ``--title`` resolves to the profile's title (D-16)."""
        args = ["scan", "--profile", "receipt"]
        if typed is not None:
            args += ["--title", typed]

        result, request = self._capture_title_run(monkeypatch, args)

        assert result.exit_code == 0, result.output
        assert request is not None
        assert request.title == "Receipt"
        assert "Done: Receipt" in result.output

    def test_scan_typed_title_beats_the_profile_title(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-blank ``--title`` is kept as typed."""
        result, request = self._capture_title_run(
            monkeypatch, ["scan", "--profile", "receipt", "--title", "Typed"]
        )

        assert result.exit_code == 0, result.output
        assert request is not None
        assert request.title == "Typed"
        assert "Done: Typed" in result.output

    def test_scan_unknown_profile_exits_2_before_title_resolution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown profile is still refused with exit 2 and no pipeline run."""
        result, request = self._capture_title_run(
            monkeypatch, ["scan", "--profile", "nope"]
        )

        assert result.exit_code == 2
        assert "Unknown profile" in result.output
        assert request is None

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
        """A bare ValueError while loading is not a saneless type -> exit 5 (D-06)."""
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

        class FailScanner(ScannerBackend):
            """
            Scanner that always raises ScanError.

            The return type used to be annotated ``-> None`` against an ABC
            whose scan_pages returns ScanBatch. That annotation was "true"
            only because the body always raises, and the checkers had nothing
            to compare it against because the class did not subclass the ABC
            it was standing in for.
            """

            def __init__(self, host: str = "") -> None:
                """Accept host parameter for API compatibility."""

            def get_devices(self) -> list[DeviceInfo]:
                """Unused here: the pipeline fails before device discovery."""
                return []

            def get_capabilities(self, device_id: str) -> DeviceCapabilities:
                """Unused here: the pipeline fails before capabilities load."""
                return DeviceCapabilities(sources=[], resolutions=[], modes=[])

            def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
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

            def upload_document(self, *_a: object, **_kw: object) -> UploadResult:
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


_DUPLEX_PROFILE = "duplex"

# A distinctive fragment of the CLI's flip prompt, asserted rather than the whole
# sentence so a rewording of the tail does not break the ordering checks.
_FLIP_PROMPT_FRAGMENT = "Flip the stack"


def _duplex_settings(tmp_path: Path, flip_timeout_seconds: int = 600) -> Settings:
    """
    Build settings with a manual-duplex profile, writing only under ``tmp_path``.

    ``_make_settings`` replaces a whole section on override, so the three path
    fields are re-supplied here -- leaving them out would send the run into the
    developer's real state directory.

    Args:
        tmp_path: The pytest temporary directory for tmp, data and log files.
        flip_timeout_seconds: The flip-wait bound, in seconds.

    Returns:
        Settings whose ``duplex`` profile is manual duplex on a feeder source.

    """
    return _make_settings(
        output=OutputConfig(
            tmp_dir=str(tmp_path),
            data_dir=str(tmp_path),
            log_file=str(tmp_path / "saneless.log"),
            flip_timeout_seconds=flip_timeout_seconds,
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

    class CountingScanner(ScannerBackend):
        """Scanner reporting a feeder, returning one inked page per pass."""

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

        def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
            """Record the call and return a single page with content."""
            calls.append(device_id)
            img = Image.new("RGB", (100, 100), "white")
            draw = ImageDraw.Draw(img)
            draw.rectangle([10, 10, 90, 90], fill="black")
            return ScanBatch(pages=[img], actual_resolution=300, pages_rejected=0)

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
            return UploadResult(delivered_to_api=True, task_uuid="mock-task-uuid")

        def poll_task(self, *_args: object, **_kwargs: object) -> dict[str, str]:
            """Return a successful task result."""
            return {"status": "SUCCESS"}

        def close(self) -> None:
            """No-op close."""

    return RecordingPaperless


class TestManualDuplexPrompt:
    """
    The CLI flip prompt, its non-terminal refusal, abort, and bounded wait.

    Read the two halves of the TTY policy together, because they look
    contradictory and are not.  ``CliRunner`` genuinely is not a terminal, so
    the refusal test runs with ``_stdin_is_interactive`` *unpatched* -- it is
    telling the truth about its environment.  The prompt tests patch that one
    seam to ``True`` so they can reach ``click.confirm`` at all.  Neither test
    lies, and neither can be "simplified" into the other: without the seam, one
    of the two could not be written.
    """

    def _interactive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pretend stdin is a terminal a human can answer on."""
        monkeypatch.setattr("saneless.cli._stdin_is_interactive", lambda: True)

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
        # C-02's own prescribed check: the prompt is echoed into the output,
        # and it lands after the fronts and before the backs.
        fronts = result.output.index("Scanning...")
        prompt = result.output.index(_FLIP_PROMPT_FRAGMENT)
        backs = result.output.index("Scanning reverse sides...")
        assert fronts < prompt < backs

    def test_answering_no_aborts_without_scanning_backs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Answering no fails the job at the flip prompt and uploads nothing."""
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

        assert result.exit_code != 0
        assert "flip prompt" in result.output
        assert len(calls) == 1
        assert uploads == []

    def test_eof_at_prompt_matches_answering_no(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """EOF (as Ctrl-C) at the prompt ends the job exactly as a web Abort does."""
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
            # Sliced from the message rather than taken by line: at EOF the
            # prompt gets no newline, so the error shares the prompt's line.
            error_line = result.output[result.output.index("Scan error") :]
            error_line = error_line.splitlines()[0]
            outcomes.append((result.exit_code, error_line, len(calls), len(uploads)))

        answered_no, interrupted = outcomes
        assert interrupted == answered_no
        assert "flip prompt" in interrupted[1]

    def test_ctrl_c_during_the_wait_is_an_abort(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Ctrl-C while the prompt is up resolves ``ABORTED``, not a raw interrupt.

        SIGINT is handled on the main thread, so a real Ctrl-C does not reach
        ``click.confirm`` on the prompt thread at all -- it raises
        ``KeyboardInterrupt`` out of the calling thread's bounded wait (measured
        with a real SIGINT against a never-answering stdin).  A real signal
        cannot be sent here without taking the pytest session down with it if
        the handling regressed, so the answer slot's wait is made to raise.
        """

        def interrupted_wait(timeout: float) -> None:
            """Raise as SIGINT would on the main thread."""
            raise KeyboardInterrupt

        release = threading.Event()

        def never_answered(*_args: object, **_kwargs: object) -> bool:
            """Block until the test lets go, then decline."""
            release.wait()
            return False

        monkeypatch.setattr("saneless.cli.click.confirm", never_answered)
        coordinator = ClickFlipCoordinator()
        monkeypatch.setattr(coordinator._slot, "wait", interrupted_wait)

        try:
            outcome = coordinator.wait_for_flip(600)
        finally:
            release.set()

        assert outcome is FlipOutcome.ABORTED

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
    def test_a_broken_prompt_aborts_at_once_with_a_logged_traceback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        failure: Exception,
    ) -> None:
        """
        WR-08: an unexpected prompt failure ends the wait now, as ``ABORTED``.

        A prompt thread that died on anything but ``click.Abort`` used to leave
        the calling thread waiting out the whole ``flip_timeout_seconds`` and
        then report that nobody confirmed the flip, which was false.  It is an
        abort, not a fourth outcome (D-09), and the traceback in the log carries
        the real cause.  The suite's ``filterwarnings = ["error"]`` also turns a
        prompt thread that died unhandled into a failure here.
        """

        def broken_confirm(*_args: object, **_kwargs: object) -> bool:
            """Fail the way a broken terminal does."""
            raise failure

        monkeypatch.setattr("saneless.cli.click.confirm", broken_confirm)
        caplog.set_level(logging.ERROR, logger="saneless.cli")
        coordinator = ClickFlipCoordinator()

        started = time.monotonic()
        outcome = coordinator.wait_for_flip(600)
        elapsed = time.monotonic() - started

        assert outcome is FlipOutcome.ABORTED
        assert elapsed < 5
        records = [
            record
            for record in caplog.records
            if record.name == "saneless.cli"
            and record.levelno == logging.ERROR
            and "Flip prompt failed" in record.getMessage()
        ]
        assert len(records) == 1
        assert records[0].exc_info is not None

    def test_the_coordinator_times_out_at_a_zero_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``wait_for_flip(0)`` with nobody answering resolves ``TIMED_OUT``.

        The coordinator's own contract, independent of the config bounds that
        keep zero out of ``flip_timeout_seconds`` (WR-01): its ``timeout``
        argument is the zero-cost seam, so no wall clock is spent here.
        """
        release = threading.Event()

        def never_answered(*_args: object, **_kwargs: object) -> bool:
            """Block until the test lets go, then decline."""
            release.wait()
            return False

        monkeypatch.setattr("saneless.cli.click.confirm", never_answered)
        coordinator = ClickFlipCoordinator()

        try:
            outcome = coordinator.wait_for_flip(0)
        finally:
            release.set()

        assert outcome is FlipOutcome.TIMED_OUT

    def test_unanswered_prompt_times_out_before_pass_b(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        An answer that never arrives fails the job on the flip wait (D-19).

        The timeout is one second, the smallest value config accepts: zero is
        no longer a legal ``flip_timeout_seconds`` (WR-01), and the field is
        typed ``int``, so a fractional float is rejected too.  One second of
        wall clock buys the whole ``scan`` command end to end -- the bounded
        wait expires and ``TIMED_OUT`` is claimed before pass B.  The
        zero-cost version of the coordinator's own contract is
        ``test_the_coordinator_times_out_at_a_zero_timeout``.
        """
        # The only test in this class that stubs click.confirm instead of
        # driving the real one, and it has to.  CliRunner's empty input stream
        # hits EOF immediately, click.confirm raises Abort, and the coordinator
        # would claim ABORTED in a race against TIMED_OUT -- a flaky test that
        # passes on one machine.  This stub makes "the answer never comes"
        # deterministic, and the finally releases it so the daemon thread ends
        # with the test instead of lingering in the pytest process.
        release = threading.Event()

        def never_answered(*_args: object, **_kwargs: object) -> bool:
            """Block until the test lets go, then decline."""
            release.wait()
            return False

        calls: list[str] = []
        uploads: list[str] = []
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_duplex_settings(tmp_path, flip_timeout_seconds=1),
            scanner_cls=_counting_scanner(calls),
            paperless_cls=_recording_paperless(uploads),
        )
        self._interactive(monkeypatch)
        monkeypatch.setattr("saneless.cli.click.confirm", never_answered)

        try:
            result = runner.invoke(
                cli, ["scan", "--profile", _DUPLEX_PROFILE, "--title", "Forgotten"]
            )
        finally:
            release.set()

        assert result.exit_code != 0
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

    def test_devices_capabilities_prints_a_reported_word_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A list-reporting device prints its exact values, as it always did."""
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

        This is the N-01 symptom the user actually sees. The SANE ``test``
        backend constrains resolution with ``(1.0, 1200.0, 1.0)``, which used to
        arrive as an empty list and print a label with nothing after it.
        """

        class _RangeScanner:
            """A scanner whose device constrains resolution with a range."""

            def __init__(self, host: str = "") -> None:
                """Accept host parameter for API compatibility."""

            def get_devices(self) -> list[DeviceInfo]:
                """Return one device, named as the SANE test backend names it."""
                return [DeviceInfo("test:0", "TestVendor", "TestModel", "scanner")]

            def get_capabilities(self, _device_id: str) -> DeviceCapabilities:
                """Report resolution as a range and give no word list at all."""
                return DeviceCapabilities(
                    sources=["Flatbed", "Automatic Document Feeder"],
                    resolutions=[],
                    modes=["Color", "Gray"],
                    resolution_range=(1.0, 1200.0, 1.0),
                )

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

            def __init__(self, host: str = "") -> None:
                """Accept host parameter for API compatibility."""

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

            def __init__(self, host: str = "") -> None:
                """Accept host parameter for API compatibility."""

            def get_devices(self) -> list[DeviceInfo]:
                """Return empty device list."""
                return []

        monkeypatch.setattr("saneless.cli.SaneBackend", MockSaneBackend)

        result = runner.invoke(cli, ["--config", "/path/to/config.toml", "devices"])
        assert result.exit_code == 0
        assert captured["config_path"] == "/path/to/config.toml"


class TestLegacyDuplexWarningReachesLogFile:
    """The legacy manual-duplex warning lands in the configured log file (WR-05)."""

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

    CFG-10 / N-25: ``saneless <subcommand> --help`` must work with a broken or
    missing configuration. Click runs the group callback *before* a subcommand
    parses its own ``--help``, and ``ctx.resilient_parsing`` is False there
    (verified against Click 8.3), so the group callback cannot load anything;
    each command loads on first need instead. A ConfigError from loading is
    printed by the group guard as rendered, exit 2; an unwritable log file is
    not a failure, it falls back to stderr.
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
        """The loader's rendered error is echoed as-is, not prefixed again (D-10)."""
        config_file = _write_real_config(tmp_path, paperless={"tokne": "x"})

        with _restored_logging():
            result = CliRunner().invoke(cli, ["--config", str(config_file), "jobs"])

        assert result.exit_code == 2
        assert result.output.startswith("Configuration error in")
        assert result.output.count("Configuration error") == 1
        assert "tokne" in result.output
        assert not (tmp_path / "logs").exists()

    def test_missing_config_exits_2_naming_the_path(self, tmp_path: Path) -> None:
        """``--config`` to a file that does not exist exits 2 and names it (CFG-02)."""
        missing = tmp_path / "nope.toml"

        with _restored_logging():
            result = CliRunner().invoke(cli, ["--config", str(missing), "jobs"])

        assert result.exit_code == 2
        assert str(missing) in result.output

    def test_empty_config_path_exits_2_instead_of_discovering(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``--config ""`` is an explicit, unusable path, not "no path" (WR-05).

        ``saneless --config "$CFG" ...`` with ``CFG`` unset used to load -- and
        ``auto-profiles`` to write -- whichever file discovery found.
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
        An unwritable ``log_file`` warns on stderr; the command still runs (WR-07).

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
        A TOML syntax error exits 2 without a traceback, under the D-10 header.

        The loader turns the ``TOMLDecodeError`` into a ConfigError naming the
        line and column (D-12), so it reaches the ConfigError handler rather
        than the generic one.
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
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second request for settings reuses the first load (memoised)."""
        settings = _make_settings()
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


class TestStartupConfigLog:
    """
    One INFO record says where the configuration came from (CFG-11).

    Names only, never values (D-14, CFG-05): a token supplied through the
    environment, or typed under a misspelt key, never reaches a log record or
    the terminal.
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
        """A token under a misspelt key is not printed with the error (D-14)."""
        secret = "tok-SECRET-51ab"
        config_file = _write_real_config(tmp_path, paperless={"tokne": secret})

        with _restored_logging():
            result = CliRunner().invoke(cli, ["--config", str(config_file), "jobs"])

        assert result.exit_code == 2
        assert secret not in result.output
        assert secret not in result.stderr


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
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                data_dir=str(tmp_path),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )

    def _populate_store(self, db_path: str, count: int = 2) -> None:
        """Populate a JobStore at db_path with test jobs."""
        store = JobStore(db_path=db_path)
        for i in range(count):
            store.create_job(
                profile="default" if i % 2 == 0 else "photo",
                title=f"Test Document {i + 1}",
            )
        store.close()

    def _populate_one(self, db_path: str, state: JobState, title: str) -> None:
        """Populate a JobStore at db_path with a single job in `state`."""
        store = JobStore(db_path=db_path)
        job = store.create_job(profile="default", title=title)
        store.update_state(job.id, state)
        store.close()

    def test_jobs_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Jobs with no jobs in DB shows empty output (exit 0)."""
        settings = self._settings_for(tmp_path)
        runner, _ = _patch_cli(monkeypatch, settings=settings)
        # Create empty DB
        store = JobStore(db_path=str(settings.output.db_path))
        store.close()

        result = runner.invoke(cli, ["jobs"])
        assert result.exit_code == 0

    def test_jobs_table_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Jobs with 2 jobs shows table with Timestamp, Profile, Title, Status columns."""
        settings = self._settings_for(tmp_path)
        self._populate_store(str(settings.output.db_path), count=2)
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
        self._populate_store(str(settings.output.db_path), count=2)
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
        self._populate_one(
            str(settings.output.db_path), JobState.FALLBACK, "Fallback Doc"
        )
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

        The status column's reserve was sized for the raw enum values, the
        longest of which was `AWAITING_FLIP`. Humanised labels are longer, and
        "Saved to folder" is longer than `FALLBACK`, so the reserve is derived
        from `state_label` rather than hardcoded.
        """
        monkeypatch.setenv("COLUMNS", "80")
        settings = self._settings_for(tmp_path)
        self._populate_one(
            str(settings.output.db_path), JobState.FALLBACK, "Fallback Doc"
        )
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])
        assert result.exit_code == 0
        lines = [line for line in result.output.strip().split("\n") if line.strip()]
        assert len(lines) == 3
        row = lines[2]
        assert row.endswith("Saved to folder")
        widest = max(len(state_label(s)) for s in JobState)
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
        self._populate_one(
            str(settings.output.db_path), JobState.FALLBACK, "Fallback Doc"
        )
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["state"] == "FALLBACK"
        assert "Saved to folder" not in result.output
        # No writer for either column until plan 23-07, so both read None today.
        assert data[0]["outcome"] is None
        assert data[0]["warning"] is None

    def test_jobs_limit(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Jobs --limit 1 with 2 jobs in DB shows only 1 job."""
        settings = self._settings_for(tmp_path)
        self._populate_store(str(settings.output.db_path), count=2)
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
        settings = self._settings_for(tmp_path)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])
        assert result.exit_code == 0

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
        store = JobStore(db_path=str(settings.output.db_path))
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

    def test_serve_log_level_ignores_verbose(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        With ``-v`` uvicorn still gets the configured level, not ``debug``.

        ``-v`` is saneless's own detail; turning uvicorn up with it would drag
        its and httpx's debug output into the log (orchestrator resolution 5).
        """
        self._mock_socket(monkeypatch)
        captured = self._capture_uvicorn(monkeypatch)
        runner, _ = _patch_cli(monkeypatch)

        result = runner.invoke(cli, ["-v", "serve"])
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

            def __init__(self, host: str = "") -> None:
                """Accept host parameter for API compatibility."""

            def get_devices(self) -> list[DeviceInfo]:
                """Return configured device list."""
                return _devices

            def get_capabilities(self, _device_id: str) -> DeviceCapabilities:
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
        # substring: "flatbed" alone is a substring of the pre-D-14 name too,
        # so a looser assertion would pass before and after the rename.
        assert "  flatbed: source=Flatbed" in result.output
        assert "  adf: source=ADF" in result.output

    def test_auto_profiles_writes_to_loaded_config_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """auto-profiles writes to the file settings were loaded from (D-16)."""
        monkeypatch.chdir(tmp_path)
        config_file = tmp_path / "elsewhere" / "config.toml"
        config_file.parent.mkdir()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=self._make_auto_scanner())

        result = runner.invoke(cli, ["--config", str(config_file), "auto-profiles"])

        assert result.exit_code == 0
        assert config_file.exists()
        assert not (tmp_path / "saneless.toml").exists()

    def test_auto_profiles_without_loaded_file_writes_cwd_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With no config file loaded, auto-profiles keeps ./saneless.toml."""
        monkeypatch.chdir(tmp_path)
        runner, _ = _patch_cli(monkeypatch, scanner_cls=self._make_auto_scanner())

        result = runner.invoke(cli, ["auto-profiles"])

        assert result.exit_code == 0
        assert (tmp_path / "saneless.toml").exists()
        # Orchestrator resolution 2: the relative default is named absolutely.
        resolved = (tmp_path / "saneless.toml").resolve()
        assert f"Profiles in {resolved}:" in result.output

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
        # D-02: a key the tool does not own survives the refresh.
        assert flatbed["default_tags"] == [4]

    def test_auto_profiles_force_skips_an_unflagged_profile_grouped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """D-01: --force reports a hand-written profile and leaves it unchanged."""
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
        """D-08: a single-file bind mount prints the fix and exits 2."""
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
        """SC3: --force refreshes a config held in a mounted directory."""
        config_file = tmp_path / "config" / "config.toml"
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
        assert [path.name for path in config_file.parent.iterdir()] == ["config.toml"]

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


class TestTruncation:
    """Tests for the _truncate helper and CLI table truncation behavior."""

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

        class LongNameScanner:
            """Scanner returning a device with a very long name."""

            def __init__(self, host: str = "") -> None:
                """Accept host parameter for API compatibility."""

            def get_devices(self) -> list[DeviceInfo]:
                """Return a device with a 50-character name."""
                return [DeviceInfo(long_name, "Vendor", "Model", "scanner")]

            def get_capabilities(self, _device_id: str) -> DeviceCapabilities:
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
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                data_dir=str(tmp_path),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )
        store = JobStore(db_path=str(settings.output.db_path))
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

        def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
            """Raise the configured exception."""
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
        output=OutputConfig(
            tmp_dir=str(tmp_path),
            data_dir=str(tmp_path),
            log_file=str(tmp_path / "saneless.log"),
        ),
    )


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
    One guard maps every CLI failure to one message and its D-07 exit code.

    EXC-02 / success criterion 2: a bad config (2), a broken scanner (1), an
    unreachable Paperless (3) and an unassemblable PDF (4) each print one error
    report; a job database saneless cannot use is a setup problem (2); a cancel
    or Ctrl-C is 130; anything that is not a saneless type is 5 with its
    traceback in the log (D-06). click's own ``--help``, usage errors and
    ``ClickException`` keep click's behaviour (Pitfall 2).
    """

    def test_bad_config_exits_2_with_header_and_one_problem_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The loader's header plus its one problem line, exit 2 (Pitfall 8)."""
        runner, _ = _patch_cli(monkeypatch)

        def bad_load(*_args: object, **_kwargs: object) -> Settings:
            msg = (
                "Configuration error in /etc/saneless/config.toml:\n"
                "  line 12, column 5: Invalid value"
            )
            raise ConfigError(msg)

        monkeypatch.setattr("saneless.cli.load_settings", bad_load)

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 2
        assert result.stderr.splitlines() == [
            "Configuration error in /etc/saneless/config.toml:",
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
        assert result.stderr.splitlines() == [
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
        lines = result.stderr.splitlines()
        assert len(lines) == 1
        assert lines[0].startswith(
            "Paperless error: Could not reach Paperless at http://paperless:8000"
        )
        assert "Traceback" not in result.output

    def test_unassemblable_pdf_exits_4_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A PdfError from assembly is one ``PDF error:`` line, exit 4."""
        runner, _ = _patch_cli(monkeypatch, settings=_tmp_settings(tmp_path))

        def failing_assemble(*_args: object, **_kwargs: object) -> Path:
            msg = (
                "Could not assemble 1 page(s) into /tmp/x.pdf: No space left on device"
            )
            raise PdfError(msg)

        monkeypatch.setattr("saneless.pipeline.assemble_pdf", failing_assemble)

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 4
        assert result.stderr.splitlines() == [
            "PDF error: Could not assemble 1 page(s) into /tmp/x.pdf: "
            "No space left on device"
        ]
        assert "Traceback" not in result.output

    def test_mid_scan_config_error_exits_2_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A ConfigError raised by the scanner mid-command is exit 2, as rendered."""
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_tmp_settings(tmp_path),
            scanner_cls=_raising_scanner(ConfigError("No scanner found")),
        )

        result = runner.invoke(cli, ["scan"])

        assert result.exit_code == 2
        assert result.stderr.splitlines() == ["No scanner found"]

    def test_scan_cancelled_exits_130_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A ScanCancelledError is a cancel, not a failure: exit 130 (D-01, D-03)."""
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
        Ctrl-C in a command is ``Cancelled (interrupted)`` and exit 130 (D-03).

        Raised only inside ``runner.invoke`` (Pitfall 5): a KeyboardInterrupt
        escaping a test would stop the whole pytest session.
        """
        runner, _ = _patch_cli(
            monkeypatch,
            settings=_tmp_settings(tmp_path),
            scanner_cls=_raising_scanner(KeyboardInterrupt()),
        )

        result = runner.invoke(cli, [command])

        assert result.exit_code == 130
        assert result.stderr.splitlines() == ["Cancelled (interrupted)"]
        assert "Aborted!" not in result.output

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
        """When logging fell back to stderr, the line claims no log file (T-28-35)."""
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
        assert result.stderr.splitlines() == ["Unexpected error (RuntimeError): kaboom"]
        assert "Full details in" not in result.output
        records = _cli_error_records(caplog)
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert records[0].exc_info[1] is exc

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

    def test_job_database_not_sqlite_exits_2_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A jobs.db that is not SQLite is a setup problem: exit 2 (D-07 amendment)."""
        settings = _tmp_settings(tmp_path)
        settings.output.db_path.write_bytes(b"this is not a database\n" * 64)
        runner, _ = _patch_cli(monkeypatch, settings=settings)

        result = runner.invoke(cli, ["jobs"])

        assert result.exit_code == 2
        lines = result.stderr.splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("Job database error: ")
        assert str(settings.output.db_path) in lines[0]
        assert "Unexpected error" not in result.output
        assert "Traceback" not in result.output

    def test_job_database_unsupported_schema_exits_2_with_one_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A jobs table saneless cannot migrate is exit 2, naming the database."""
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
        lines = result.stderr.splitlines()
        assert len(lines) == 1
        assert str(settings.output.db_path) in lines[0]
        assert "unsupported schema" in lines[0]
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

        def failing_store(*_args: object, **_kwargs: object) -> JobStore:
            raise exc

        runner, _ = _patch_cli(monkeypatch, settings=_tmp_settings(tmp_path))
        monkeypatch.setattr("saneless.cli.configure_logging", lambda *_a, **_kw: True)
        monkeypatch.setattr("saneless.cli.JobStore", failing_store)
        caplog.set_level(logging.ERROR, logger="saneless.cli")

        result = runner.invoke(cli, ["jobs"])

        assert result.exit_code == 2
        assert result.stderr.splitlines() == [f"Job database error: {exc}"]
        assert "Full details in" not in result.output
        records = _cli_error_records(caplog)
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert records[0].exc_info[1] is exc

    def test_help_exits_0_without_running_the_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``scan --help`` is click's Exit, re-raised by the guard: exit 0."""
        runner, _ = _patch_cli(monkeypatch)
        calls: list[str] = []

        def recording_load(*_args: object, **_kwargs: object) -> Settings:
            calls.append("load_settings")
            return _make_settings()

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
