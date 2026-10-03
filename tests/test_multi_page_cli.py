"""
``saneless scan --multi-page``: the refusals, the prompt, and how a wait ends.

Whole runs go through ``CliRunner`` with the real pipeline, a scanner feeding
identifiable pages and an in-memory paperless-ngx, so page order is read back
from the upload; ``ClickPassCoordinator.ask`` is called directly for the parsing,
the confirmation and the endings a run cannot reach on demand.  ``CliRunner`` is
not a terminal, so the non-terminal refusal runs with ``_stdin_is_interactive``
unpatched, and every test that needs the prompt patches that one seam to
``True``.  Every test starts with the question's wait reporting stdin readable
at once (``tests.prompt_support``), since ``CliRunner``'s stdin has no descriptor
to wait on; a test about the clock, a broken read or Ctrl-C replaces it again.
"""

from __future__ import annotations

import errno
import io
import logging
import signal
import sys
import termios
import threading
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from saneless.cli import (
    ClickFlipCoordinator,
    ClickPassCoordinator,
    _flush_typed_ahead,
    _Interruption,
    cli,
)
from saneless.vocabulary import (
    MULTI_PAGE_NEEDS_TERMINAL,
    NOTHING_TO_FINISH,
    ExitCode,
    FlipOutcome,
    PassAnswer,
    PassPrompt,
    PassWait,
    abort_question,
    blank_timeout_finish_warning,
    cli_choice_hint,
    multi_page_manual_duplex_refusal,
    timeout_finish_warning,
)
from tests.golden_support import (
    DistinctPageScanner,
    RecordingPaperless,
    cli_client_builder,
    embedded_streams,
    png_idat,
)
from tests.multi_page_support import (
    DUPLEX_PROFILE,
    ScriptedPassCoordinator,
    multi_page_settings,
)
from tests.prompt_support import (
    FakeClock,
    broken_read,
    never_readable,
    readable,
    typed_after,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from click.testing import Result

    from saneless.config import Settings
    from saneless.pipeline import PassCoordinator

_TITLE = "Stapled letter"

# The operator-wait bound multi_page_settings gives every prompt, in seconds.
_TIMEOUT = 600

# The four answers the between-pass prompt offers once a page is kept.
_EVERY_NEXT_PASS_ANSWER = frozenset(
    {PassAnswer.NEXT, PassAnswer.RESCAN, PassAnswer.FINISH, PassAnswer.ABORT}
)

# The manual-duplex refusal off a terminal, which never mentions --multi-page.
_MANUAL_DUPLEX_NEEDS_TERMINAL = (
    f"Profile '{DUPLEX_PROFILE}' is manual duplex, which needs an interactive "
    "terminal: saneless must prompt you to flip the stack between the "
    "two passes. Run it from a terminal, or scan from the web UI."
)


@dataclass(frozen=True)
class _Run:
    """
    What one ``saneless scan`` left behind.

    Attributes:
        result: The command's exit code and output.
        settings: The settings the command loaded.
        scanner: The scanner, with a copy of every page it spooled.
        recorder: The in-memory paperless-ngx.
        opened: One entry per ``SaneBackend(...)`` the command built.

    """

    result: Result
    settings: Settings
    scanner: DistinctPageScanner
    recorder: RecordingPaperless
    opened: list[str]

    @property
    def failed(self) -> list[Path]:
        """
        Return the files in ``failed/`` after the command returned.

        Returns:
            Every file there, sorted.

        """
        failed_dir = self.settings.output.failed_dir
        return sorted(failed_dir.iterdir()) if failed_dir.is_dir() else []

    def uploaded_pages(self) -> list[bytes]:
        """
        Return the single upload's pages, one pixel stream each, in order.

        Returns:
            ``embedded_streams`` of the one uploaded document.

        """
        (_upload,) = self.recorder.uploads()
        return embedded_streams(self.recorder.document(0))

    def spooled(self, indices: Sequence[int]) -> list[bytes]:
        """
        Return what a document of exactly these spooled pages embeds.

        Args:
            indices: The page indices, in document order.

        Returns:
            One ``png_idat`` per page.

        """
        return [png_idat(self.scanner.spooled[index]) for index in indices]


def _scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: Sequence[str],
    *,
    passes: Sequence[Sequence[int]] = ((0,),),
    text: str | None = None,
) -> _Run:
    """
    Run the real ``saneless scan`` against a fake scanner and paperless-ngx.

    Args:
        tmp_path: pytest's per-test directory.
        monkeypatch: Replaces what ``saneless.cli`` reaches outside itself.
        args: The arguments after ``scan``.
        passes: The page indices each scanner pass feeds.
        text: What the operator types, if anything.

    Returns:
        What the run left behind.

    """
    scanner = DistinctPageScanner(passes=passes)
    return _scan_with(tmp_path, monkeypatch, args, scanner=scanner, text=text)


def _scan_with(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: Sequence[str],
    *,
    scanner: DistinctPageScanner,
    text: str | None = None,
) -> _Run:
    """
    Run the real ``saneless scan`` with a prepared scanner, as ``_scan`` does.

    Empty-page detection is on exactly when ``scanner`` feeds blank paper, so
    a run about the loop is never also a run about blank pages.

    Args:
        tmp_path: pytest's per-test directory.
        monkeypatch: Replaces what ``saneless.cli`` reaches outside itself.
        args: The arguments after ``scan``.
        scanner: The scanner the command opens.
        text: What the operator types, if anything.

    Returns:
        What the run left behind.

    """
    settings = multi_page_settings(tmp_path, detection=bool(scanner.blank))
    recorder = RecordingPaperless()
    opened: list[str] = []

    def load_settings(config_path: str | None = None) -> Settings:
        """Return the prepared settings whatever ``--config`` says."""
        return settings

    def configure_logging(*_args: object, **_kwargs: object) -> bool:
        """Attach no handler; report that no log file was attached."""
        return False

    def require_sane() -> None:
        """Skip the python-sane import check; no hardware is involved."""

    def sane_backend(host: str = "") -> DistinctPageScanner:
        """Note that the scanner was opened, and hand over the prepared one."""
        opened.append(host)
        return scanner

    monkeypatch.setattr("saneless.cli.load_settings", load_settings)
    monkeypatch.setattr("saneless.cli.configure_logging", configure_logging)
    monkeypatch.setattr("saneless.cli.require_sane", require_sane)
    monkeypatch.setattr("saneless.cli.SaneBackend", sane_backend)
    monkeypatch.setattr("saneless.cli.PaperlessClient", cli_client_builder(recorder))
    result = CliRunner().invoke(cli, ["scan", "--title", _TITLE, *args], input=text)
    return _Run(
        result=result,
        settings=settings,
        scanner=scanner,
        recorder=recorder,
        opened=opened,
    )


def _next_pass_prompt(pages_kept: int = 1, *, timeout: float = _TIMEOUT) -> PassPrompt:
    """
    Build the between-pass question as the pipeline asks it.

    Args:
        pages_kept: How many pages the document holds; Finish is offered only
            above zero.
        timeout: How long the operator has.

    Returns:
        The prompt.

    """
    offered = _EVERY_NEXT_PASS_ANSWER
    if pages_kept < 1:
        offered -= {PassAnswer.FINISH}
    return PassPrompt(
        number=1,
        wait=PassWait.NEXT_PASS,
        pages_kept=pages_kept,
        offered=offered,
        timeout_seconds=timeout,
        last_pass_pages=1,
        last_pass_kept=1,
    )


def _blank_prompt() -> PassPrompt:
    """
    Build the blank-page question for a six-page pass whose pages 2 and 4 look blank.

    Returns:
        The prompt.

    """
    return PassPrompt(
        number=3,
        wait=PassWait.BLANK_DECISION,
        pages_kept=2,
        offered=frozenset(
            {PassAnswer.SKIP_BLANKS, PassAnswer.KEEP_BLANKS, PassAnswer.RESCAN}
        ),
        timeout_seconds=_TIMEOUT,
        pass_pages=6,
        blank_positions=(2, 4),
    )


def _retry_prompt() -> PassPrompt:
    """
    Build the question after a failed pass, with two pages kept.

    Returns:
        The prompt.

    """
    return PassPrompt(
        number=4,
        wait=PassWait.RETRY,
        pages_kept=2,
        offered=frozenset({PassAnswer.NEXT, PassAnswer.FINISH, PassAnswer.ABORT}),
        timeout_seconds=_TIMEOUT,
        error="The scanner jammed",
    )


def _ask(prompt: PassPrompt, text: str) -> tuple[PassAnswer, str, ClickPassCoordinator]:
    """
    Put ``prompt`` to a fresh terminal coordinator, the operator typing ``text``.

    Args:
        prompt: The question.
        text: Everything the operator types.

    Returns:
        The answer, everything the terminal showed, and the coordinator.

    """
    coordinator = ClickPassCoordinator()
    with CliRunner().isolation(input=text) as (_stdout, _stderr, output):
        answer = coordinator.ask(prompt)
    return answer, output.getvalue().decode(), coordinator


@pytest.fixture(autouse=True)
def _lines_are_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every question's wait report stdin readable at once."""
    readable(monkeypatch)


def _interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend stdin is a terminal a human can answer on."""
    monkeypatch.setattr("saneless.cli._stdin_is_interactive", lambda: True)


class TestMultiPageRefusals:
    """Requests the terminal cannot serve are refused before the scanner opens."""

    def test_without_a_terminal_it_is_refused_before_the_scanner_opens(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Off a terminal nobody can answer the prompt, so nothing is scanned.

        Deliberately not patched: ``CliRunner`` is honestly not a terminal.
        """
        run = _scan(tmp_path, monkeypatch, ["--multi-page"])

        assert run.result.exit_code == ExitCode.CONFIG, run.result.output
        assert MULTI_PAGE_NEEDS_TERMINAL in run.result.stderr
        assert run.opened == []
        assert run.scanner.calls == 0

    def test_a_manual_duplex_profile_is_refused_before_the_scanner_opens(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multi-page and manual duplex together are refused, naming the profile."""
        _interactive(monkeypatch)
        run = _scan(
            tmp_path, monkeypatch, ["--multi-page", "--profile", DUPLEX_PROFILE]
        )

        assert run.result.exit_code == ExitCode.CONFIG, run.result.output
        assert multi_page_manual_duplex_refusal(DUPLEX_PROFILE) in run.result.stderr
        assert run.opened == []

    def test_the_manual_duplex_refusal_comes_before_the_terminal_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Off a terminal, manual duplex still gets the multi-page refusal.

        A terminal would not help, so the reason given is the one that would.
        """
        run = _scan(
            tmp_path,
            monkeypatch,
            ["--multi-page", "--profile", DUPLEX_PROFILE],
        )

        assert run.result.exit_code == ExitCode.CONFIG, run.result.output
        assert multi_page_manual_duplex_refusal(DUPLEX_PROFILE) in run.result.stderr
        assert MULTI_PAGE_NEEDS_TERMINAL not in run.result.stderr
        assert _MANUAL_DUPLEX_NEEDS_TERMINAL not in run.result.stderr
        assert run.opened == []

    def test_manual_duplex_without_the_flag_is_refused_as_before(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --multi-page, manual duplex off a terminal gets its own refusal."""
        run = _scan(tmp_path, monkeypatch, ["--profile", DUPLEX_PROFILE])

        assert run.result.exit_code == ExitCode.CONFIG, run.result.output
        assert _MANUAL_DUPLEX_NEEDS_TERMINAL in run.result.stderr
        assert "--multi-page" not in run.result.stderr
        assert run.opened == []

    def test_the_flag_is_documented_in_the_help(self) -> None:
        """``scan --help`` lists --multi-page and says it needs a terminal."""
        result = CliRunner().invoke(cli, ["scan", "--help"])

        assert result.exit_code == 0, result.output
        assert "--multi-page" in result.output
        assert "interactive terminal" in result.output


class TestMultiPageScan:
    """A whole ``saneless scan --multi-page`` run, answered at the terminal."""

    def test_next_three_times_then_finish_uploads_four_pages_in_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Four passes, three nexts and a finish: one four-page document, exit 0."""
        _interactive(monkeypatch)
        run = _scan(
            tmp_path,
            monkeypatch,
            ["--multi-page"],
            passes=((0,), (1,), (2,), (3,)),
            text="n\nn\nn\nf\n",
        )

        assert run.result.exit_code == ExitCode.SUCCESS, run.result.output
        output = run.result.output
        assert "1 page kept so far. [n]ext, [r]e-scan last, [f]inish, [a]bort" in output
        assert "4 pages kept so far." in output
        assert run.scanner.calls == 4
        assert run.uploaded_pages() == run.spooled((0, 1, 2, 3))
        assert run.failed == []

    def test_an_unknown_letter_lists_the_choices_and_asks_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``x`` is refused with the offered letters; the run then goes on."""
        _interactive(monkeypatch)
        run = _scan(
            tmp_path,
            monkeypatch,
            ["--multi-page"],
            passes=((0,), (1,)),
            text="x\nn\nf\n",
        )

        assert run.result.exit_code == ExitCode.SUCCESS, run.result.output
        assert (
            "Error: Choose one of: [n]ext, [r]e-scan last, [f]inish, [a]bort"
            in run.result.output
        )
        assert run.uploaded_pages() == run.spooled((0, 1))

    def test_abort_declined_asks_again_and_the_scan_goes_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``a`` then ``n`` to the confirmation: nothing is lost, Finish still works."""
        _interactive(monkeypatch)
        run = _scan(
            tmp_path, monkeypatch, ["--multi-page"], passes=((0,),), text="a\nn\nf\n"
        )

        assert run.result.exit_code == ExitCode.SUCCESS, run.result.output
        assert abort_question(1) in run.result.output
        assert "[y/N]" in run.result.output
        assert run.uploaded_pages() == run.spooled((0,))

    def test_abort_confirmed_cancels_and_keeps_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``a`` then ``y``: exit 130, nothing uploaded, nothing kept."""
        _interactive(monkeypatch)
        run = _scan(
            tmp_path, monkeypatch, ["--multi-page"], passes=((0,),), text="a\ny\n"
        )

        assert run.result.exit_code == ExitCode.CANCELLED, run.result.output
        assert run.recorder.uploads() == []
        assert run.failed == []

    def test_a_timeout_uploads_the_kept_pages_with_a_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nobody answered prompt 2: both pages upload, and the exit code warns."""
        created: list[PassCoordinator] = []

        def scripted() -> PassCoordinator:
            """Stand in for the terminal: one next, then nobody answers."""
            coordinator = ScriptedPassCoordinator(
                [PassAnswer.NEXT, PassAnswer.TIMED_OUT]
            )
            created.append(coordinator)
            return coordinator

        monkeypatch.setattr("saneless.cli.ClickPassCoordinator", scripted)

        _interactive(monkeypatch)
        run = _scan(tmp_path, monkeypatch, ["--multi-page"], passes=((0,), (1,)))

        assert run.result.exit_code == ExitCode.UPLOADED_WITH_WARNING, run.result.output
        assert len(created) == 1
        assert timeout_finish_warning(2, _TIMEOUT) in run.result.output
        assert run.uploaded_pages() == run.spooled((0, 1))

    def test_without_the_flag_a_run_asks_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain scan on a terminal scans once and never prompts."""
        _interactive(monkeypatch)
        run = _scan(tmp_path, monkeypatch, [], passes=((0,),))

        assert run.result.exit_code == ExitCode.SUCCESS, run.result.output
        assert "kept so far" not in run.result.output
        assert run.scanner.calls == 1


def _script(
    monkeypatch: pytest.MonkeyPatch, answers: Sequence[PassAnswer]
) -> list[ScriptedPassCoordinator]:
    """
    Answer the run's questions from ``answers`` instead of the terminal.

    Args:
        monkeypatch: Replaces ``saneless.cli.ClickPassCoordinator``.
        answers: The scripted answers, in order.

    Returns:
        The coordinators the command built, filled in as it builds them.

    """
    created: list[ScriptedPassCoordinator] = []

    def scripted() -> ScriptedPassCoordinator:
        """Stand in for the terminal with the scripted answers."""
        coordinator = ScriptedPassCoordinator(answers)
        created.append(coordinator)
        return coordinator

    monkeypatch.setattr("saneless.cli.ClickPassCoordinator", scripted)
    return created


class TestMultiPageBlankTimeouts:
    """
    Nobody answering a question about blank pages.

    With a page kept, a timeout skips the blank pages and uploads the rest
    with a warning.  With none kept there is nothing to upload, so the run
    fails the way an all-blank scan does: exit 8, the skipped pages kept in
    ``failed/``.
    """

    @pytest.mark.parametrize(
        "answers",
        [
            (PassAnswer.TIMED_OUT,),
            (PassAnswer.SKIP_BLANKS, PassAnswer.TIMED_OUT),
        ],
        ids=["blank-question-times-out", "skipped-then-next-question-times-out"],
    )
    def test_a_timeout_with_nothing_kept_exits_8_and_keeps_the_page(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        answers: tuple[PassAnswer, ...],
    ) -> None:
        """The only page looks blank and is never kept: all-blank, nothing sent."""
        created = _script(monkeypatch, answers)
        _interactive(monkeypatch)
        scanner = DistinctPageScanner(passes=((0,),), blank={0})

        run = _scan_with(tmp_path, monkeypatch, ["--multi-page"], scanner=scanner)

        assert run.result.exit_code == ExitCode.ALL_BLANK, run.result.output
        (coordinator,) = created
        assert coordinator.prompts[0].wait is PassWait.BLANK_DECISION
        assert [prompt.pages_kept for prompt in coordinator.prompts] == [0] * len(
            answers
        )
        (kept,) = run.failed
        assert embedded_streams(kept) == run.spooled((0,))
        assert run.recorder.uploads() == []
        assert "empty_page_coverage_threshold" in run.result.stderr

    def test_a_blank_question_timeout_with_a_page_kept_exits_7(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A kept page, then a blank pass nobody answers: the page uploads, warned."""
        created = _script(monkeypatch, [PassAnswer.NEXT, PassAnswer.TIMED_OUT])
        _interactive(monkeypatch)
        scanner = DistinctPageScanner(passes=((0,), (1,)), blank={1})

        run = _scan_with(tmp_path, monkeypatch, ["--multi-page"], scanner=scanner)

        assert run.result.exit_code == ExitCode.UPLOADED_WITH_WARNING, run.result.output
        (coordinator,) = created
        assert [prompt.wait for prompt in coordinator.prompts] == [
            PassWait.NEXT_PASS,
            PassWait.BLANK_DECISION,
        ]
        assert blank_timeout_finish_warning(1, _TIMEOUT) in run.result.output
        assert run.uploaded_pages() == run.spooled((0,))
        assert run.failed == []


class TestClickPassCoordinatorAnswers:
    """What the operator types, and the answer it becomes."""

    def test_finish_with_nothing_kept_is_refused_and_asked_again(self) -> None:
        """``f`` with no page kept says why not; the next letter is honoured."""
        answer, shown, _ = _ask(_next_pass_prompt(pages_kept=0), "f\nn\n")

        assert answer is PassAnswer.NEXT
        assert f"Error: {NOTHING_TO_FINISH}" in shown

    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            ("s\n", PassAnswer.SKIP_BLANKS),
            ("k\n", PassAnswer.KEEP_BLANKS),
            ("r\n", PassAnswer.RESCAN),
            ("S\n", PassAnswer.SKIP_BLANKS),
            ("keep\n", PassAnswer.KEEP_BLANKS),
        ],
        ids=["skip", "keep", "rescan", "upper-case", "first-letter-counts"],
    )
    def test_the_blank_page_question_takes_its_own_letters(
        self, typed: str, expected: PassAnswer
    ) -> None:
        """Skip, keep and re-scan each answer the blank-page question."""
        answer, _, _ = _ask(_blank_prompt(), typed)

        assert answer is expected

    def test_a_letter_the_blank_question_does_not_offer_is_refused(self) -> None:
        """``n`` means nothing there: the blank question's own letters are listed."""
        prompt = _blank_prompt()

        answer, shown, _ = _ask(prompt, "n\ns\n")

        assert answer is PassAnswer.SKIP_BLANKS
        assert f"Error: {cli_choice_hint(prompt)}" in shown
        assert NOTHING_TO_FINISH not in shown

    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            ("n\n", PassAnswer.NEXT),
            ("N\n", PassAnswer.NEXT),
            ("f\n", PassAnswer.FINISH),
        ],
        ids=["scan-again", "upper-case", "finish"],
    )
    def test_the_failed_pass_question_takes_its_own_letters(
        self, typed: str, expected: PassAnswer
    ) -> None:
        """After a failed pass, ``n`` scans again and ``f`` finishes."""
        answer, shown, _ = _ask(_retry_prompt(), typed)

        assert answer is expected
        assert "The scanner jammed" in shown

    def test_the_failed_pass_error_has_its_control_characters_escaped(
        self,
    ) -> None:
        """
        A scanner's error text reaches the terminal escaped, as every failure line does.

        The text comes from outside saneless -- a SANE status string, a wrapped
        OS error -- so an escape sequence in it must be shown, not obeyed.
        """
        prompt = replace(_retry_prompt(), error="jammed\x1b[2J\x1b]0;owned\x07")

        answer, shown, _ = _ask(prompt, "f\n")

        assert answer is PassAnswer.FINISH
        assert "\x1b" not in shown
        assert "\x07" not in shown
        assert "jammed\\x1b[2J\\x1b]0;owned\\x07" in shown

    def test_nothing_typed_but_spaces_asks_again(self) -> None:
        """A line of spaces is no letter at all: the choices are listed."""
        prompt = _next_pass_prompt()

        answer, shown, _ = _ask(prompt, "   \nf\n")

        assert answer is PassAnswer.FINISH
        assert f"Error: {cli_choice_hint(prompt)}" in shown

    def test_abort_declined_then_confirmed_aborts_with_no_cause(self) -> None:
        """Every ``a`` is confirmed; a no asks again, a yes is the operator's abort."""
        answer, shown, coordinator = _ask(
            _next_pass_prompt(pages_kept=3), "a\nn\na\ny\n"
        )

        assert answer is PassAnswer.ABORT
        assert coordinator.abort_cause is None
        assert shown.count(abort_question(3)) == 2


class TestClickPassCoordinatorEndings:
    """The ways a wait ends that are not a letter: the clock, Ctrl-C, EOF, a fault."""

    def test_nobody_answering_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A zero timeout with nothing typed resolves ``TIMED_OUT``."""
        never_readable(monkeypatch, FakeClock())

        answer, _, coordinator = _ask(_next_pass_prompt(timeout=0), "")

        assert answer is PassAnswer.TIMED_OUT
        assert coordinator.abort_cause is None

    def test_ctrl_c_aborts_at_once_without_asking_to_confirm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Ctrl-C is a cancel with no confirmation, however many pages are kept.

        SIGINT raises ``KeyboardInterrupt`` out of the wait on the main
        thread, so the wait is made to raise it; the real signal is sent on a
        real terminal in ``test_cli_prompt_terminal``.
        """
        broken_read(monkeypatch, KeyboardInterrupt())

        answer, shown, coordinator = _ask(_next_pass_prompt(pages_kept=40), "a\ny\n")

        assert answer is PassAnswer.ABORT
        assert coordinator.abort_cause is None
        assert abort_question(40) not in shown

    def test_end_of_input_with_no_signal_is_an_abort(self) -> None:
        """Ctrl-D at the prompt is the operator's cancel, once the grace has passed."""
        answer, _, coordinator = _ask(_next_pass_prompt(), "")

        assert answer is PassAnswer.ABORT
        assert coordinator.abort_cause is None

    def test_end_of_input_at_the_confirmation_is_an_abort(self) -> None:
        """Ctrl-D while "abort?" is asked is a cancel too, not a No."""
        answer, shown, coordinator = _ask(_next_pass_prompt(pages_kept=2), "a\n")

        assert answer is PassAnswer.ABORT
        assert coordinator.abort_cause is None
        assert abort_question(2) in shown

    def test_end_of_input_after_a_signal_leaves_the_answer_alone(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        A hangup's end of input claims no cancel: the signal decides the run.

        A signal recorded without raising leaves the question unanswered, so
        it ends as an unanswered wait does, never as a cancel that would throw
        the kept pages away.
        """
        interruption = _Interruption()
        interruption.record(signal.SIGHUP.value)
        monkeypatch.setattr("saneless.cli._INTERRUPTION", interruption)
        caplog.set_level(logging.INFO, logger="saneless.cli")

        answer, _, _ = _ask(_next_pass_prompt(), "")

        assert answer is PassAnswer.TIMED_OUT
        assert (
            "Multi-page prompt reached end of input after a signal "
            f"({signal.SIGHUP.value}); leaving the answer to the interruption"
        ) in [record.getMessage() for record in caplog.records]

    def test_a_broken_prompt_aborts_with_its_cause(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A prompt that fails is not the operator's choice: ABORT, cause kept."""
        failure = OSError(errno.EIO, "Input/output error")
        broken_read(monkeypatch, failure)
        caplog.set_level(logging.ERROR, logger="saneless.cli")

        answer, _, coordinator = _ask(_next_pass_prompt(), "n\n")

        assert answer is PassAnswer.ABORT
        assert coordinator.abort_cause is failure
        assert any(record.exc_info is not None for record in caplog.records)

    def test_a_closed_stdin_at_the_question_is_a_broken_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        No stdin at all when the question is asked: a failure, never a crash.

        The scan refuses a closed stdin before any paper moves, so this is the
        coordinator's own guard: it answers as a broken read does.
        """
        monkeypatch.setattr(sys, "stdin", None)
        coordinator = ClickPassCoordinator()

        answer = coordinator.ask(_next_pass_prompt())

        assert answer is PassAnswer.ABORT
        assert isinstance(coordinator.abort_cause, OSError)

    def test_the_flip_prompt_logs_its_end_of_input_as_before(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        The manual-duplex prompt logs a hangup's end of input in its own words.

        A signal recorded without raising leaves the question unanswered, so
        it ends as an unanswered wait does, keeping the fronts.
        """
        interruption = _Interruption()
        interruption.record(signal.SIGHUP.value)
        monkeypatch.setattr("saneless.cli._INTERRUPTION", interruption)
        # A closed terminal's end of input, readable at once.
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        readable(monkeypatch)
        caplog.set_level(logging.INFO, logger="saneless.cli")

        outcome = ClickFlipCoordinator().wait_for_flip(0)

        assert outcome is FlipOutcome.TIMED_OUT
        assert (
            "Flip prompt reached end of input after a signal "
            f"({signal.SIGHUP.value}); leaving the answer to the interruption"
        ) in [record.getMessage() for record in caplog.records]


class _FakeTerminal:
    """A stdin that says it is a terminal, on a descriptor the test names."""

    def __init__(self, *, fileno_fails: bool = False) -> None:
        """
        Prepare the stand-in.

        Args:
            fileno_fails: Raise from ``fileno`` as a stream with no descriptor does.

        """
        self._fileno_fails = fileno_fails

    def isatty(self) -> bool:
        """
        Report a terminal.

        Returns:
            Always ``True``.

        """
        return True

    def fileno(self) -> int:
        """
        Return descriptor 0.

        Returns:
            Always 0.

        Raises:
            io.UnsupportedOperation: When built with ``fileno_fails``.

        """
        if self._fileno_fails:
            msg = "fileno"
            raise io.UnsupportedOperation(msg)
        return 0


class TestTypeAheadFlush:
    """Keys typed during a pass are thrown away before the next question."""

    def test_nothing_is_flushed_off_a_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under CliRunner's fake stdin the flush does nothing, and does not raise."""
        flushed: list[tuple[int, int]] = []
        monkeypatch.setattr(
            "saneless.cli.termios.tcflush",
            lambda fd, queue: flushed.append((fd, queue)),
        )

        with CliRunner().isolation(input="n\n"):
            _flush_typed_ahead()

        assert flushed == []

    def test_a_terminal_has_its_pending_input_flushed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On a terminal the input queue, and only it, is flushed."""
        flushed: list[tuple[int, int]] = []
        monkeypatch.setattr("saneless.cli.sys.stdin", _FakeTerminal())
        monkeypatch.setattr(
            "saneless.cli.termios.tcflush",
            lambda fd, queue: flushed.append((fd, queue)),
        )

        _flush_typed_ahead()

        assert flushed == [(0, termios.TCIFLUSH)]

    def test_a_terminal_that_cannot_be_flushed_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing descriptor or a refused flush never breaks the prompt."""

        def refuse(fd: int, queue: int) -> None:
            """Fail as a terminal that went away does."""
            msg = "gone"
            raise termios.error(msg)

        monkeypatch.setattr("saneless.cli.termios.tcflush", refuse)
        monkeypatch.setattr("saneless.cli.sys.stdin", _FakeTerminal())
        _flush_typed_ahead()
        monkeypatch.setattr("saneless.cli.sys.stdin", _FakeTerminal(fileno_fails=True))
        _flush_typed_ahead()

    def test_every_question_is_flushed_once_before_it_is_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One flush per ask, and it comes before the prompt reads anything."""
        log: list[str] = []

        def flush() -> None:
            """Record the flush."""
            log.append("flush")

        def wait(_stream: object, _timeout: float) -> bool:
            """
            Record the wait, and report the next line ready.

            Returns:
                Always ``True``.

            """
            log.append("wait")
            return True

        monkeypatch.setattr("saneless.cli._flush_typed_ahead", flush)
        monkeypatch.setattr("saneless.cli._wait_readable", wait)
        coordinator = ClickPassCoordinator()

        with CliRunner().isolation(input="n\nn\n"):
            first = coordinator.ask(_next_pass_prompt())
            second = coordinator.ask(_next_pass_prompt(pages_kept=2))

        assert (first, second) == (PassAnswer.NEXT, PassAnswer.NEXT)
        assert log == ["flush", "wait", "flush", "wait"]


# The name a thread reading the multi-page question would carry.  Nothing may
# start it.
_MULTI_PAGE_PROMPT_THREAD = "saneless-multi-page-prompt"

# The operator wait the confirmation tests run with, in seconds.
_SHORT = 60

# Where FakeClock starts.
_CLOCK_START = FakeClock().now


def _ask_typed(
    monkeypatch: pytest.MonkeyPatch,
    prompt: PassPrompt,
    text: str,
    delays: Sequence[float] | None = None,
) -> tuple[PassAnswer, str, list[float], FakeClock]:
    """
    Put ``prompt`` to a fresh coordinator, each line typed after its pause.

    Args:
        monkeypatch: Replaces the prompt's wait and clock.
        prompt: The question.
        text: Everything the operator types.
        delays: The pause before each line, in seconds; no pause at all for
            any line when omitted.

    Returns:
        The answer, everything the terminal showed, the timeout each wait
        was given, and the clock.

    """
    clock = FakeClock()
    if delays is None:
        delays = [0.0] * text.count("\n")
    waits = typed_after(monkeypatch, clock, delays)
    answer, shown, _ = _ask(prompt, text)
    return answer, shown, waits, clock


class TestConfirmationHoldsTheClock:
    """
    An abort being confirmed holds the clock, for at most one more timeout.

    The operator who typed ``a`` is at the terminal, answering "abort?".  If
    the wait ran out under them the document would be finished and uploaded
    while they were confirming they wanted it thrown away.  So the
    confirmation is due by the question's deadline plus one more timeout.
    """

    def test_a_yes_after_the_deadline_still_aborts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``a`` at 50 s, ``y`` at 80 s of a 60 s wait: the operator's abort stands."""
        answer, _, waits, _ = _ask_typed(
            monkeypatch,
            _next_pass_prompt(pages_kept=3, timeout=_SHORT),
            "a\ny\n",
            [50, 30],
        )

        assert answer is PassAnswer.ABORT
        assert waits == [_SHORT, 70]

    def test_a_no_after_the_deadline_times_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``a`` at 50 s, ``n`` at 80 s: not an abort, and the wait is over."""
        answer, _, waits, _ = _ask_typed(
            monkeypatch,
            _next_pass_prompt(pages_kept=3, timeout=_SHORT),
            "a\nn\n",
            [50, 30],
        )

        assert answer is PassAnswer.TIMED_OUT
        assert waits == [_SHORT, 70]

    def test_a_no_before_the_deadline_asks_again_with_the_time_left(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``a`` at 10 s, ``n`` at 15 s: the question again, for the 45 s left."""
        answer, shown, waits, _ = _ask_typed(
            monkeypatch,
            _next_pass_prompt(pages_kept=3, timeout=_SHORT),
            "a\nn\nf\n",
            [10, 5, 5],
        )

        assert answer is PassAnswer.FINISH
        assert waits == [_SHORT, 110, 45]
        assert shown.count("3 pages kept so far.") == 2

    def test_an_unanswered_confirmation_times_out_one_timeout_later(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``a`` at 50 s, then nothing: ``TIMED_OUT`` at 120 s, and not before."""
        answer, _, waits, clock = _ask_typed(
            monkeypatch,
            _next_pass_prompt(pages_kept=3, timeout=_SHORT),
            "a\n",
            [50],
        )

        assert answer is PassAnswer.TIMED_OUT
        assert waits == [_SHORT, 70]
        assert clock.now == _CLOCK_START + 2 * _SHORT

    def test_a_yes_after_the_deadline_cancels_the_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole run: a held confirmation's yes is a cancel, exit 130."""
        _interactive(monkeypatch)
        waits = typed_after(monkeypatch, FakeClock(), [_TIMEOUT - 10, 30])

        run = _scan(
            tmp_path, monkeypatch, ["--multi-page"], passes=((0,),), text="a\ny\n"
        )

        assert run.result.exit_code == ExitCode.CANCELLED, run.result.output
        assert waits == [_TIMEOUT, _TIMEOUT + 10]
        assert run.recorder.uploads() == []
        assert run.failed == []


class TestTheQuestionOnTheMainThread:
    """The multi-page question is asked and read on the calling thread."""

    def test_typed_ahead_is_flushed_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        One flush per question, before its first read, and none on a re-ask.

        A refused letter and a declined abort are both answered from what is
        typed after them, so flushing again would throw a real answer away.
        """
        log: list[str] = []

        def flush() -> None:
            """Record the flush."""
            log.append("flush")

        def wait(_stream: object, _timeout: float) -> bool:
            """
            Record the wait, and report the next line ready.

            Returns:
                Always ``True``.

            """
            log.append("wait")
            return True

        monkeypatch.setattr("saneless.cli._flush_typed_ahead", flush)
        monkeypatch.setattr("saneless.cli._wait_readable", wait)

        answer, _, _ = _ask(_next_pass_prompt(), "x\na\nn\nn\n")

        assert answer is PassAnswer.NEXT
        assert log == ["flush", "wait", "wait", "wait", "wait"]

    def test_a_bad_letter_is_asked_again_with_the_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``x`` is refused with the offered letters, and the question asked again."""
        prompt = _next_pass_prompt()

        answer, shown, waits, _ = _ask_typed(monkeypatch, prompt, "x\nn\n")

        assert answer is PassAnswer.NEXT
        assert f"Error: {cli_choice_hint(prompt)}" in shown
        assert shown.count("1 page kept so far.") == 2
        assert len(waits) == 2

    def test_finish_with_nothing_kept_explains(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``f`` with nothing kept says why there is nothing to finish."""
        answer, shown, waits, _ = _ask_typed(
            monkeypatch, _next_pass_prompt(pages_kept=0), "f\nn\n"
        )

        assert answer is PassAnswer.NEXT
        assert f"Error: {NOTHING_TO_FINISH}" in shown
        assert len(waits) == 2

    def test_an_empty_line_asks_again_quietly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Return on its own asks the question again, with no error line."""
        answer, shown, waits, _ = _ask_typed(monkeypatch, _next_pass_prompt(), "\nn\n")

        assert answer is PassAnswer.NEXT
        assert "Error" not in shown
        assert shown.count("1 page kept so far.") == 2
        assert len(waits) == 2

    @pytest.mark.parametrize(
        "failure",
        [
            OSError(errno.EIO, "Input/output error"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ],
        ids=["lost-terminal", "undecodable-input"],
    )
    def test_a_broken_read_is_a_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        failure: Exception,
    ) -> None:
        """
        A read that breaks fails the scan: exit 1, logged, the page kept.

        Nobody chose to stop, so it is neither a cancel (130) nor an
        unexpected error (5), and the log keeps the traceback.
        """
        _interactive(monkeypatch)
        broken_read(monkeypatch, failure)
        caplog.set_level(logging.ERROR, logger="saneless.cli")

        run = _scan(tmp_path, monkeypatch, ["--multi-page"], passes=((0,),))

        assert run.result.exit_code == ExitCode.SCAN, run.result.output
        assert "Multi-page prompt failed: " in run.result.stderr
        records = [
            record
            for record in caplog.records
            if record.name == "saneless.cli"
            and "Multi-page prompt failed; treating it as an abort"
            in record.getMessage()
        ]
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert len(run.failed) == 1
        assert run.recorder.uploads() == []

    def test_no_multi_page_prompt_thread_is_started(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        No thread is started for a question, and none is left behind.

        A thread parked on stdin outlives the command that started it, and on
        a real terminal it holds the process open at exit until someone
        presses Enter.
        """
        started: list[str] = []
        real_start = threading.Thread.start

        def recording_start(thread: threading.Thread) -> None:
            """Note the thread's name, then start it as usual."""
            started.append(thread.name)
            real_start(thread)

        _interactive(monkeypatch)
        monkeypatch.setattr(threading.Thread, "start", recording_start)

        run = _scan(
            tmp_path, monkeypatch, ["--multi-page"], passes=((0,), (1,)), text="n\nf\n"
        )

        assert run.result.exit_code == ExitCode.SUCCESS, run.result.output
        assert _MULTI_PAGE_PROMPT_THREAD not in started
        assert _MULTI_PAGE_PROMPT_THREAD not in [
            thread.name for thread in threading.enumerate()
        ]
        assert run.uploaded_pages() == run.spooled((0, 1))
