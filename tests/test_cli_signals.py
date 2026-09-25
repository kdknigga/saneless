"""
SIGTERM and SIGHUP end a one-shot command cleanly, keeping the pages scanned.

A one-shot command given SIGTERM or SIGHUP was not stopped by the operator, so
it is an interruption, not a cancel: the pages already scanned are kept in
``failed/`` and the command exits 128 plus the signal number (143 or 129) with
one ``Interrupted:`` line naming what was kept.  Ctrl-C stays the deliberate
cancel that keeps nothing (exit 130).

Every signal here is real, sent with ``os.kill`` to this very process, and each
is sent from inside the run at the moment it matters: from the in-memory
paperless-ngx while it receives the upload, and from the flip prompt's own
thread, where a dropped SSH session delivers its hangup.  That is the only way
to prove the handler is live at that moment, rather than merely installed at
some point.

The safety rule: a signal is sent only when ``signal.getsignal`` shows a
Python handler installed for it -- a callable other than
``signal.default_int_handler``.  With the default disposition in place,
SIGTERM or SIGHUP would kill pytest itself; so the sender refuses instead,
records the refusal and raises ``AssertionError("no <SIG> handler installed")``.
Each test checks for a refusal first, so a run without handlers fails with
that line rather than taking the test runner down.

An autouse fixture puts both dispositions back after every test whatever the
command did, so a handler that leaks cannot reach any later test; the test
that checks the command restores them itself asserts before that fixture runs.
"""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, override

import click
import pytest
from click.testing import CliRunner
from fastapi import FastAPI

from saneless import preservation
from saneless import workspace as workspace_module
from saneless.cli import _INTERRUPTION, cli
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.exceptions import ScanError
from saneless.vocabulary import ExitCode
from tests.golden_support import (
    DOCUMENTS_PATH,
    DistinctPageScanner,
    RecordingPaperless,
    cli_client_builder,
    embedded_streams,
    png_idat,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from pathlib import Path
    from types import FrameType

    import httpx2
    from click.testing import Result

    from saneless.scanner.base import PageSink, ScanBatch, ScanSettings

_TITLE = "Quarterly Report"
_SIMPLEX = "signal-simplex"
_DUPLEX = "signal-duplex"

# The two signals a one-shot command turns into an interruption.
_INTERRUPTING = (signal.SIGTERM, signal.SIGHUP)

# Short next to pytest-timeout's ceiling, so a flip wait the signal failed to
# end still finishes inside the test instead of hanging it.
_FLIP_TIMEOUT_SECONDS = 10

type _Disposition = Callable[[int, FrameType | None], object] | int | None


@pytest.fixture(autouse=True)
def _restore_signal_dispositions() -> Generator[None]:
    """Put SIGTERM's and SIGHUP's dispositions back after every test."""
    saved = {signum: signal.getsignal(signum) for signum in _INTERRUPTING}
    yield
    for signum, disposition in saved.items():
        if disposition is not None:
            signal.signal(signum, disposition)


@dataclass
class _Signaller:
    """
    Send a signal to this process, but only when a handler will catch it.

    Attributes:
        refusals: One line per signal withheld because no handler was there.
        sent: The signals actually sent, in order.

    """

    refusals: list[str] = field(default_factory=list)
    sent: list[signal.Signals] = field(default_factory=list)

    def send(self, signum: signal.Signals) -> None:
        """
        Send ``signum`` to this process under the safety rule.

        Args:
            signum: The signal to send.

        Raises:
            AssertionError: No Python handler is installed for ``signum``, so
                sending it would kill the test runner.

        """
        handler = signal.getsignal(signum)
        if not callable(handler) or handler is signal.default_int_handler:
            refusal = f"no {signum.name} handler installed"
            self.refusals.append(refusal)
            raise AssertionError(refusal)
        self.sent.append(signum)
        os.kill(os.getpid(), signum)


class _SignallingPaperless(RecordingPaperless):
    """The in-memory paperless-ngx, sending a signal as an upload arrives."""

    def __init__(self, signaller: _Signaller, signum: signal.Signals) -> None:
        """
        Send ``signum`` through ``signaller`` on the first upload.

        Args:
            signaller: Sends the signal under the safety rule.
            signum: The signal the upload triggers.

        """
        super().__init__()
        self._signaller = signaller
        self._signum = signum

    @override
    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """
        Signal before an upload is recorded or answered, else answer as usual.

        Args:
            request: The request the client sent.

        Returns:
            The recorder's answer, for anything that is not an upload.

        """
        if request.method == "POST" and request.url.path == DOCUMENTS_PATH:
            self._signaller.send(self._signum)
        return super().__call__(request)


class _ObservingScanner(DistinctPageScanner):
    """
    A scanner that notes the signal dispositions it scans under.

    Optionally the pass fails after it has spooled its pages, with a
    ``ScanError`` or with Ctrl-C.
    """

    def __init__(
        self,
        *,
        passes: Sequence[Sequence[int]],
        failure: BaseException | None = None,
    ) -> None:
        """
        Prepare the passes, and the failure to raise after the first one.

        Args:
            passes: The page indices each pass feeds.
            failure: Raised once the first pass has spooled its pages.

        """
        super().__init__(passes=passes)
        self.failure = failure
        self.dispositions: dict[signal.Signals, _Disposition] = {}

    @override
    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Note the dispositions, feed the pass, then raise the failure if any.

        Returns:
            The pass's batch.

        Raises:
            BaseException: The prepared failure, after the pages spooled.

        """
        for signum in _INTERRUPTING:
            self.dispositions[signum] = signal.getsignal(signum)
        batch = super().scan_pages(device_id, settings, sink)
        if self.failure is not None:
            raise self.failure
        return batch


@dataclass(frozen=True)
class _CliRun:
    """
    What one ``saneless scan`` left behind.

    Attributes:
        result: The command's exit code, stdout and stderr.
        failed: The files in ``failed/`` after the command returned.
        recorder: The in-memory paperless-ngx.
        scanner: The scanner, with a copy of every page it spooled.

    """

    result: Result
    failed: list[Path]
    recorder: RecordingPaperless
    scanner: DistinctPageScanner

    @property
    def interrupted_lines(self) -> list[str]:
        """
        Return the stderr lines announcing an interruption.

        Returns:
            Every stderr line starting ``Interrupted:``.

        """
        return [
            line
            for line in self.result.stderr.splitlines()
            if line.startswith("Interrupted:")
        ]


def _settings(tmp_path: Path) -> Settings:
    """
    Build settings whose every path is under ``tmp_path``.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        Settings with a simplex and a manual-duplex profile.

    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(
            url="http://paperless.invalid:8000", token="signal-token"
        ),
        output=OutputConfig(
            tmp_dir=str(tmp_path / "scratch"),
            data_dir=str(data_dir),
            log_file=str(tmp_path / "logs" / "saneless.log"),
            flip_timeout_seconds=_FLIP_TIMEOUT_SECONDS,
        ),
        profiles={
            "default": ProfileConfig(),
            _SIMPLEX: ProfileConfig.model_validate({"source": "Flatbed"}),
            _DUPLEX: ProfileConfig.model_validate(
                {"source": "ADF", "duplex": "manual"}
            ),
        },
    )


def _patch_environment(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    *,
    scanner: DistinctPageScanner | None = None,
) -> None:
    """
    Replace only what a test cannot have: config, logging, python-sane, SANE.

    Args:
        monkeypatch: Replaces the names in ``saneless.cli``.
        settings: The settings every command loads.
        scanner: The scanner ``SaneBackend(...)`` hands back, if any.

    """

    def load_settings(config_path: str | None = None) -> Settings:
        """Return the prepared settings whatever ``--config`` says."""
        return settings

    def configure_logging(*_args: object, **_kwargs: object) -> bool:
        """Attach no handler; report that no log file was attached."""
        return False

    def require_sane() -> None:
        """Skip the python-sane import check; no hardware is involved."""

    def sane_backend(host: str = "") -> DistinctPageScanner:
        """Hand the command the prepared scanner."""
        assert scanner is not None, "this command was not expected to open SANE"
        return scanner

    monkeypatch.setattr("saneless.cli.load_settings", load_settings)
    monkeypatch.setattr("saneless.cli.configure_logging", configure_logging)
    monkeypatch.setattr("saneless.cli.require_sane", require_sane)
    monkeypatch.setattr("saneless.cli.SaneBackend", sane_backend)


def _run_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: str,
    scanner: DistinctPageScanner,
    recorder: RecordingPaperless | None = None,
) -> _CliRun:
    """
    Run the real ``saneless scan`` against a fake scanner and paperless-ngx.

    Args:
        tmp_path: pytest's per-test directory.
        monkeypatch: Replaces the environment ``saneless.cli`` reaches for.
        profile: The profile to scan with.
        scanner: The scanner the command opens.
        recorder: The in-memory paperless-ngx; a plain one if omitted.

    Returns:
        What the run left behind.

    """
    settings = _settings(tmp_path)
    recorder = recorder if recorder is not None else RecordingPaperless()
    _patch_environment(monkeypatch, settings, scanner=scanner)
    monkeypatch.setattr("saneless.cli.PaperlessClient", cli_client_builder(recorder))
    # The flip prompt needs a terminal, which CliRunner is not.
    monkeypatch.setattr("saneless.cli._stdin_is_interactive", lambda: True)
    result = CliRunner().invoke(cli, ["scan", "--profile", profile, "--title", _TITLE])
    failed_dir = settings.output.failed_dir
    failed = sorted(failed_dir.iterdir()) if failed_dir.is_dir() else []
    return _CliRun(result=result, failed=failed, recorder=recorder, scanner=scanner)


def _kept_pages(kept: Path) -> list[bytes]:
    """
    Read a PDF kept in ``failed/`` back, one raw image stream per page.

    Args:
        kept: The file in ``failed/``.

    Returns:
        ``embedded_streams`` of the kept PDF.

    """
    assert kept.suffix == ".pdf", kept
    return embedded_streams(kept)


def _spooled_idat(scanner: DistinctPageScanner, indices: Sequence[int]) -> list[bytes]:
    """
    Return the compressed pixel data of spooled pages, in the given order.

    Args:
        scanner: The scanner that spooled them.
        indices: Which pages, in order.

    Returns:
        One ``png_idat`` per page.

    """
    return [png_idat(scanner.spooled[index]) for index in indices]


def test_sigterm_during_upload_keeps_the_pdf_and_exits_143(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM mid-upload: exit 143, the assembled PDF in failed/, nothing uploaded."""
    signaller = _Signaller()
    recorder = _SignallingPaperless(signaller, signal.SIGTERM)
    scanner = DistinctPageScanner(passes=((0, 1, 2),))

    run = _run_cli(
        tmp_path, monkeypatch, profile=_SIMPLEX, scanner=scanner, recorder=recorder
    )

    assert signaller.refusals == []
    assert signaller.sent == [signal.SIGTERM]
    assert run.result.exit_code == ExitCode.TERMINATED, run.result.output
    (kept,) = run.failed
    # The finished document itself, named as it would have been uploaded: not
    # a "(partial)" rebuilt from the spooled pages.
    assert kept.stem.endswith("-quarterly-report"), kept.name
    assert run.interrupted_lines == [run.interrupted_lines[0]]
    assert str(kept) in run.interrupted_lines[0]
    assert _kept_pages(kept) == _spooled_idat(scanner, (0, 1, 2))
    # The signal landed before paperless-ngx answered: no task was issued, so
    # nothing was ever polled and no document exists over there.
    assert recorder.issued == []
    assert recorder.polls() == []


def test_sighup_at_the_flip_prompt_keeps_the_fronts_and_exits_129(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hangup at the flip prompt: exit 129, the fronts kept, pass B never run."""
    signaller = _Signaller()

    def hang_up(*_args: object, **_kwargs: object) -> bool:
        """
        Be the terminal closing: SIGHUP, then end of input on the prompt.

        Raises:
            click.Abort: The end of input a closed terminal gives the prompt.

        """
        signaller.send(signal.SIGHUP)
        raise click.Abort

    monkeypatch.setattr("saneless.cli.click.confirm", hang_up)
    scanner = DistinctPageScanner(passes=((0, 2, 4), (5, 3, 1)))

    run = _run_cli(tmp_path, monkeypatch, profile=_DUPLEX, scanner=scanner)

    assert signaller.refusals == []
    assert signaller.sent == [signal.SIGHUP]
    assert run.result.exit_code == ExitCode.HANGUP, run.result.output
    (kept,) = run.failed
    # build_pdf_filename sanitises "(fronts)" to "-fronts".
    assert kept.stem.endswith("-quarterly-report-fronts"), kept.name
    assert run.interrupted_lines == [run.interrupted_lines[0]]
    assert str(kept) in run.interrupted_lines[0]
    assert _kept_pages(kept) == _spooled_idat(scanner, (0, 2, 4))
    assert scanner.calls == 1
    assert run.recorder.uploads() == []


def test_a_hangup_whose_end_of_input_lands_first_still_keeps_the_fronts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    End of input first, SIGHUP a moment later: still an interruption, not a cancel.

    The other order of the same race.  The prompt thread sees end of input
    before the main thread sees SIGHUP; the signal is sent from inside the
    prompt thread's wait for it, so it lands after end of input by
    construction, with no timed pause.  Settling ABORTED without that wait
    would end the run as a cancel that keeps nothing.
    """
    signaller = _Signaller()
    real_wait = _INTERRUPTION.wait

    def end_of_input(*_args: object, **_kwargs: object) -> bool:
        """
        Be the closed terminal's end of input, arriving ahead of its signal.

        Raises:
            click.Abort: Always.

        """
        raise click.Abort

    def signal_arrives(timeout: float) -> bool:
        """Deliver the hangup's SIGHUP now, then wait for it as the code does."""
        signaller.send(signal.SIGHUP)
        return real_wait(timeout)

    monkeypatch.setattr("saneless.cli.click.confirm", end_of_input)
    monkeypatch.setattr(_INTERRUPTION, "wait", signal_arrives)
    scanner = DistinctPageScanner(passes=((0, 2, 4), (5, 3, 1)))

    run = _run_cli(tmp_path, monkeypatch, profile=_DUPLEX, scanner=scanner)

    assert signaller.refusals == []
    assert signaller.sent == [signal.SIGHUP]
    assert run.result.exit_code == ExitCode.HANGUP, run.result.output
    (kept,) = run.failed
    assert kept.stem.endswith("-quarterly-report-fronts"), kept.name
    assert _kept_pages(kept) == _spooled_idat(scanner, (0, 2, 4))
    assert scanner.calls == 1


def test_ctrl_c_is_still_a_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C after two pages is the operator's cancel: exit 130, nothing kept."""
    scanner = _ObservingScanner(passes=((0, 1),), failure=KeyboardInterrupt())

    run = _run_cli(tmp_path, monkeypatch, profile=_SIMPLEX, scanner=scanner)

    assert run.result.exit_code == ExitCode.CANCELLED, run.result.output
    assert len(scanner.spooled) == 2
    assert run.failed == []
    assert run.interrupted_lines == []
    assert run.recorder.uploads() == []


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (None, ExitCode.SUCCESS),
        (ScanError("The scanner jammed"), ExitCode.SCAN),
    ],
    ids=["success", "failed_scan"],
)
def test_handlers_are_restored_after_the_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException | None,
    expected: ExitCode,
) -> None:
    """Handlers are live during a scan and gone again after it, however it ends."""
    before = {signum: signal.getsignal(signum) for signum in _INTERRUPTING}
    scanner = _ObservingScanner(passes=((0, 1),), failure=failure)

    run = _run_cli(tmp_path, monkeypatch, profile=_SIMPLEX, scanner=scanner)

    assert run.result.exit_code == expected, run.result.output
    for signum in _INTERRUPTING:
        during = scanner.dispositions[signum]
        assert during != before[signum], f"no {signum.name} handler installed"
        assert callable(during), f"no {signum.name} handler installed"
        assert signal.getsignal(signum) == before[signum], signum.name


def test_handlers_are_restored_after_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``saneless scan --help`` leaves both dispositions as it found them."""
    before = {signum: signal.getsignal(signum) for signum in _INTERRUPTING}
    scanner = _ObservingScanner(passes=((0,),))
    _patch_environment(monkeypatch, _settings(tmp_path), scanner=scanner)

    result = CliRunner().invoke(cli, ["scan", "--profile", _SIMPLEX, "--help"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert scanner.calls == 0
    for signum in _INTERRUPTING:
        assert signal.getsignal(signum) == before[signum], signum.name


def test_serve_installs_no_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``serve`` keeps uvicorn's own signal handling: saneless installs nothing."""
    before = {signum: signal.getsignal(signum) for signum in _INTERRUPTING}
    seen: dict[signal.Signals, _Disposition] = {}
    _patch_environment(monkeypatch, _settings(tmp_path), scanner=DistinctPageScanner())

    def create_app(*_args: object, **_kwargs: object) -> FastAPI:
        """Build a bare app instead; the server below never serves it."""
        return FastAPI()

    def run_server(*_args: object, **_kwargs: object) -> None:
        """Note what uvicorn would have started under, and return at once."""
        for signum in _INTERRUPTING:
            seen[signum] = signal.getsignal(signum)

    monkeypatch.setattr("saneless.cli.create_app", create_app)
    monkeypatch.setattr("saneless.cli._run_server", run_server)

    result = CliRunner().invoke(cli, ["serve", "--host", "127.0.0.1", "--port", "0"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert seen == before
    for signum in _INTERRUPTING:
        assert signal.getsignal(signum) == before[signum], signum.name


def test_second_signal_during_preservation_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the first SIGTERM a second is ignored, so preservation finishes."""
    signaller = _Signaller()
    recorder = _SignallingPaperless(signaller, signal.SIGTERM)
    scanner = DistinctPageScanner(passes=((0, 1, 2),))
    seen: list[_Disposition] = []
    real_move_private = preservation.move_private

    def move_private(source: Path, destination: Path) -> None:
        """Note SIGTERM's disposition; if ignored, send a second one; then move."""
        disposition = signal.getsignal(signal.SIGTERM)
        seen.append(disposition)
        if disposition is signal.SIG_IGN:
            # Ignored, so this cannot reach pytest: the second SIGTERM of an
            # impatient supervisor, arriving mid-preservation.
            os.kill(os.getpid(), signal.SIGTERM)
        real_move_private(source, destination)

    monkeypatch.setattr("saneless.preservation.move_private", move_private)

    run = _run_cli(
        tmp_path, monkeypatch, profile=_SIMPLEX, scanner=scanner, recorder=recorder
    )

    assert signaller.refusals == []
    assert seen == [signal.SIG_IGN]
    assert run.result.exit_code == ExitCode.TERMINATED, run.result.output
    (kept,) = run.failed
    assert _kept_pages(kept) == _spooled_idat(scanner, (0, 1, 2))


def test_first_signal_while_a_failure_is_kept_lets_the_keeping_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    SIGTERM while a scanner fault's pages are being kept: they are all kept.

    The run had already failed on its own, and its outcome was settled, so
    the signal is deferred: the preservation finishes, the workspace goes, and
    the command reports the fault it really had rather than an interruption.
    """
    signaller = _Signaller()
    scanner = _ObservingScanner(
        passes=((0, 1, 2),), failure=ScanError("The scanner jammed")
    )
    real_move_private = preservation.move_private

    def move_private(source: Path, destination: Path) -> None:
        """Send the first SIGTERM as the first page lands, then move it."""
        if not signaller.sent:
            signaller.send(signal.SIGTERM)
        real_move_private(source, destination)

    monkeypatch.setattr("saneless.preservation.move_private", move_private)

    run = _run_cli(tmp_path, monkeypatch, profile=_SIMPLEX, scanner=scanner)

    assert signaller.refusals == []
    assert signaller.sent == [signal.SIGTERM]
    assert run.result.exit_code == ExitCode.SCAN, run.result.output
    (kept,) = run.failed
    assert _kept_pages(kept) == _spooled_idat(scanner, (0, 1, 2))
    assert str(kept) in run.result.stderr
    assert run.interrupted_lines == []
    assert list((tmp_path / "scratch").glob("job-*")) == []


def test_a_signal_after_delivery_leaves_the_success_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM while a delivered scan's workspace is removed: still exit 0."""
    signaller = _Signaller()
    scanner = DistinctPageScanner(passes=((0, 1),))
    real_remove = workspace_module._remove_quietly

    def remove_quietly(path: Path, what: str) -> None:
        """Send SIGTERM as the workspace starts to go, then remove it."""
        signaller.send(signal.SIGTERM)
        real_remove(path, what)

    monkeypatch.setattr(workspace_module, "_remove_quietly", remove_quietly)

    run = _run_cli(tmp_path, monkeypatch, profile=_SIMPLEX, scanner=scanner)

    assert signaller.refusals == []
    assert signaller.sent == [signal.SIGTERM]
    assert run.result.exit_code == ExitCode.SUCCESS, run.result.output
    assert len(run.recorder.uploads()) == 1
    assert run.failed == []
    assert run.interrupted_lines == []
    assert list((tmp_path / "scratch").glob("job-*")) == []
