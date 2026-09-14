"""Tests for CLI commands via click.testing.CliRunner."""

from __future__ import annotations

import json
import logging
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import tomlkit
from click.testing import CliRunner
from PIL import Image, ImageDraw

from saneless.cli import ClickFlipCoordinator, _truncate, cli
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.exceptions import PaperlessError, ScanError
from saneless.job import JobStore
from saneless.paperless import UploadResult
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScanBatch,
    ScannerBackend,
    ScanSettings,
)
from saneless.vocabulary import FlipOutcome, JobState, state_label

if TYPE_CHECKING:
    import pytest


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
        the handling regressed, so the wait itself is made to raise.
        """

        class InterruptedEvent:
            """An event whose wait is cut short by Ctrl-C."""

            def wait(self, timeout: float | None = None) -> bool:
                """Raise as SIGINT would on the main thread."""
                raise KeyboardInterrupt

            def set(self) -> None:
                """Accept the prompt thread's late signal."""

        release = threading.Event()

        def never_answered(*_args: object, **_kwargs: object) -> bool:
            """Block until the test lets go, then decline."""
            release.wait()
            return False

        monkeypatch.setattr("saneless.cli.click.confirm", never_answered)
        coordinator = ClickFlipCoordinator()
        monkeypatch.setattr(coordinator, "_event", InterruptedEvent())

        try:
            outcome = coordinator.wait_for_flip(600)
        finally:
            release.set()

        assert outcome is FlipOutcome.ABORTED

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

        root_logger = logging.getLogger()
        handlers_before = list(root_logger.handlers)
        level_before = root_logger.level
        try:
            result = CliRunner().invoke(
                cli, ["--config", str(config_file), "jobs", "--limit", "1"]
            )
        finally:
            # configure_logging adds to the ROOT logger; remove what this
            # invocation added so later tests do not write into tmp_path.
            for handler in list(root_logger.handlers):
                if handler not in handlers_before:
                    root_logger.removeHandler(handler)
                    handler.close()
            root_logger.setLevel(level_before)

        assert result.exit_code == 0, result.output
        content = log_file.read_text()
        assert "'legacy'" in content
        assert 'duplex = "manual"' in content


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
        # Match the whole printed "  <name>: source=..." line, not a bare
        # substring: "flatbed" alone is a substring of the pre-D-14 name too,
        # so a looser assertion would pass before and after the rename.
        assert "  flatbed: source=Flatbed" in result.output
        assert "  adf: source=ADF" in result.output

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
        # Pre-populate config with an existing flatbed profile
        doc = tomlkit.document()
        profiles_table = tomlkit.table(is_super_table=True)
        existing = tomlkit.table()
        existing.add("source", "Old Source")
        existing.add("resolution", 150)
        existing.add("mode", "Gray")
        profiles_table["flatbed"] = existing
        doc.add("profiles", profiles_table)
        config_file.write_text(tomlkit.dumps(doc))

        scanner_cls = self._make_auto_scanner()
        runner, _ = _patch_cli(monkeypatch, scanner_cls=scanner_cls)

        result = runner.invoke(
            cli, ["--config", str(config_file), "auto-profiles", "--force"]
        )
        assert result.exit_code == 0
        assert "Generated" in result.output
        assert "  flatbed: source=Flatbed" in result.output

    def test_auto_profiles_no_force_skips_existing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """auto-profiles without --force does not overwrite existing profiles."""
        config_file = tmp_path / "saneless.toml"
        # Pre-populate config with ALL profiles that would be generated
        doc = tomlkit.document()
        profiles_table = tomlkit.table(is_super_table=True)
        for name in ("default", "flatbed", "adf"):
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
