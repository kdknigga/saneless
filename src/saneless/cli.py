"""
The saneless command-line interface: one click group and its six commands.

``scan`` runs the scan-to-upload pipeline, ``devices`` lists the scanners
SANE can see, ``jobs`` prints the job history, ``serve`` starts the web
server, ``auto-profiles`` writes scan profiles from a scanner's capabilities,
and ``doctor`` runs the readiness checks. Every command runs inside one guard
that turns a failure into a stderr line saying what failed, a ``Try:`` line
with the next step, and a documented exit code.  A configuration error's line
is the loader's header naming the file and one line per problem.  ``serve``
logs these expected failures without a traceback; an unexpected error (exit 5)
keeps its traceback in the log.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import select
import shutil
import signal
import socket
import sys
import termios
import threading
import time
import traceback
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Final, assert_never
from uuid import uuid4

import click

from .atomic_write import is_read_only_mount, make_config_directory
from .auto_profiles import (
    device_type_of,
    generate_profiles,
    write_profiles_to_config,
)
from .checks import (
    CheckContext,
    CheckKey,
    CheckResult,
    CheckState,
    PaperlessRefusal,
    ScannerRefusal,
    check_name,
    configuration_check,
    leftover_config_check,
    run_checks,
    worst_state,
)
from .config import (
    CONFIG_FILENAME,
    LEGACY_CONFIG_FILENAME,
    Settings,
    absolute_or_as_spelled,
    config_file_state,
    config_search_paths,
    is_placeholder_token,
    load_settings,
    log_config_sources,
    profile_storage_for_loaded,
    resolve_job_title,
    validate_settings_dirs,
    warn_on_legacy_duplex_sources,
)
from .exceptions import (
    ConfigError,
    NoScannerFoundError,
    PaperlessError,
    PaperlessTrustStoreError,
    SanelessError,
    ScanCancelledError,
    ScanError,
    ScanInterrupted,
    StorageError,
    describe,
    failure_text,
    note_text,
)
from .flip import FlipCoordinator, PassCoordinator
from .job import CLI_JOBS_DEFAULT_LIMIT, read_recent_jobs
from .logging_config import configure_logging
from .paperless import PaperlessClient
from .pipeline import (
    PipelineEvent,
    RequestHooks,
    Settled,
    build_pipeline_request,
    run_pipeline,
)
from .private_dirs import make_private_dir
from .scan_metadata import resolve_scan_metadata
from .scanner.sane_backend import SaneBackend, require_sane
from .text_safety import neutralise_controls
from .vocabulary import (
    CONFIG_WRITE_NEXT_STEP,
    FALLBACK_NOT_UPLOADED_LINE,
    MULTI_PAGE_MANUAL_DUPLEX_NEXT_STEP,
    MULTI_PAGE_NEEDS_TERMINAL,
    MULTI_PAGE_OPTION_HELP,
    NOTHING_TO_FINISH,
    SCAN_FROM_A_TERMINAL_NEXT_STEP,
    SERVE_ADDRESS_NEXT_STEP,
    SERVE_BIND_NEXT_STEP,
    SERVE_PORT_IN_USE_NEXT_STEP,
    SERVE_PORT_NOT_ALLOWED_NEXT_STEP,
    SERVE_SANE_START_NEXT_STEP,
    TITLE_MAX_LENGTH,
    UNCONFIRMED_FILING_LABEL,
    UNCONFIRMED_SEND_LABEL,
    UNKNOWN_PROFILE_NEXT_STEP,
    UNSET_CREDENTIAL_CLAUSE,
    WARNED_UPLOAD_LABEL,
    CheckSurface,
    ConfigFileState,
    ErrorCategory,
    ExitCode,
    FlipOutcome,
    JobState,
    PassAnswer,
    PassWait,
    RequestRejection,
    ScanOutcome,
    abort_question,
    classify_error,
    cli_choice_hint,
    cli_pass_choices,
    cli_pass_question,
    error_next_step,
    exit_code_for,
    exit_code_for_outcome,
    exit_code_for_signal,
    job_label,
    job_state_for,
    local_time,
    manual_duplex_needs_terminal_refusal,
    multi_page_manual_duplex_refusal,
    outcome_line,
    progress_label,
    rejection_message,
    removed_pages_note,
    render_check_step,
    root_owned_config_note,
    root_per_user_config_note,
    state_label,
)
from .workspace import sweep_orphans

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import FrameType
    from typing import TextIO

    from .auto_profiles import ProfileWriteResult
    from .config import ProfileConfig
    from .scanner.base import DeviceCapabilities, DeviceInfo, ScannerBackend
    from .vocabulary import PassPrompt

__all__ = ["ClickFlipCoordinator", "ClickPassCoordinator", "cli"]

logger = logging.getLogger(__name__)

# How many not-yet-accepted connections the web server's socket queues. This
# is the backlog uvicorn itself listens with.
_LISTEN_BACKLOG: Final = 2048

# Width of the Status column in `saneless jobs`, derived from every label a row
# can show, so a new JobState member cannot overflow the column unnoticed. The
# warned-upload and the two unconfirmed-upload labels come from the warning or
# the category rather than the state, so they join the max explicitly.
_STATUS_COL_WIDTH = max(
    *(len(state_label(state)) for state in JobState),
    len(WARNED_UPLOAD_LABEL),
    len(UNCONFIRMED_SEND_LABEL),
    len(UNCONFIRMED_FILING_LABEL),
)

# Width of the Profile column in `saneless jobs`, and the floor the Title
# column never shrinks below. At 80 columns the Timestamp, Profile and Status
# columns and the three separators leave the Title exactly its floor.
_PROFILE_COL_WIDTH: Final = 14
_TITLE_COL_FLOOR: Final = 15

# Widths of the fixed columns in `saneless devices`, and the floor the Name
# column never shrinks below.  The Name column takes what the terminal has left,
# 30 at 80 columns: room for a typical network device name.  The Type column is
# cut too, because SANE's 25-character "multi-function peripheral" would push a
# row past 80; ``--json`` gives every value whole.
_DEVICE_VENDOR_COL_WIDTH: Final = 15
_DEVICE_MODEL_COL_WIDTH: Final = 20
_DEVICE_TYPE_COL_WIDTH: Final = 12
_DEVICE_NAME_COL_FLOOR: Final = 20

# The widest zone token ``%Z`` produces at a realistic offset: five characters,
# the ``+0545`` shape the tz database falls back to where there is no
# abbreviation. This host cannot demonstrate it on its own.
_WIDEST_ZONE_TOKEN = len("+0545")

# Width of the Timestamp column in `saneless jobs`, derived from a rendered
# sample. Only the zone token varies, so the column reserves the widest
# realistic one; a host whose token is wider still wins the max().
_TIME_COL_SAMPLE = local_time(datetime(2026, 9, 16, 19, 3, tzinfo=UTC))
_TIME_COL_WIDTH = max(
    len(_TIME_COL_SAMPLE),
    len(_TIME_COL_SAMPLE.rsplit(" ", 1)[0]) + 1 + _WIDEST_ZONE_TOKEN,
)

# What the operator is asked between the two manual-duplex passes.  A yes/no
# question rather than "press Enter" on purpose: click's pause(), the only API that
# matches "press Enter", is a documented no-op off a terminal and would start
# pass B on the unflipped stack without a word.
_FLIP_PROMPT = (
    "Flip the stack over and load it back into the feeder. Scan the back sides?"
)

# Why `scan` refuses when nobody configured paperless-ngx: the tail of the CLI's
# `<what saneless was doing>: <problem>` error line, which names the problem and
# never the value, the URL or the config path.
_UNSET_CREDENTIAL_PROBLEM = UNSET_CREDENTIAL_CLAUSE
# The same refusal for an empty paperless.url, which names the setting.
_UNSET_ADDRESS_PROBLEM = "the paperless-ngx address in paperless.url has not been set"


# A function of its own so the prompt test patches this one name, while the
# non-TTY refusal test runs unpatched against CliRunner, which is not a terminal.
def _stdin_is_interactive() -> bool:
    """
    Whether stdin is a terminal a human can answer a prompt on.

    A process started with stdin closed (``<&-``) has no ``sys.stdin`` at all,
    which is no more a terminal than a pipe is.
    """
    stdin = sys.stdin
    return stdin is not None and not stdin.closed and stdin.isatty()


# A function of its own so a test patches this one name to move time on.
def _monotonic() -> float:
    """Read the clock a prompt's deadline is measured on."""
    return time.monotonic()


# The one wait a terminal prompt makes, a function of its own so a test patches
# this one name: CliRunner's stdin has no descriptor to wait on, and the read
# after the wait then runs for real. Clamped at zero because select refuses a
# negative timeout.
def _wait_readable(stream: TextIO, timeout: float) -> bool:
    """
    Wait up to ``timeout`` seconds for ``stream`` to be readable.

    Runs on the main thread, where Ctrl-C raises ``KeyboardInterrupt`` out of
    the wait, and SIGTERM or SIGHUP raises the handler's ``ScanInterrupted``.
    """
    ready, _, _ = select.select([stream.fileno()], [], [], max(0.0, timeout))
    return bool(ready)


# The signals a one-shot command turns into an interruption: a supervisor
# stopping it, or its terminal going away. Nobody at the keyboard chose to
# stop, so the pages already scanned are kept, not thrown away. SIGINT is not
# here: Ctrl-C is the operator's own cancel, which Python already raises as
# KeyboardInterrupt, and it keeps nothing.
_INTERRUPT_SIGNALS: Final = (signal.SIGTERM, signal.SIGHUP)

# A dropped SSH session delivers SIGHUP and end of input to a terminal prompt
# at nearly the same moment, in no promised order. End of input on its own is a
# cancel, which keeps nothing, so the prompt waits this long for the signal
# before it treats end of input as one: the signal wins, and the hangup keeps
# the pages scanned -- the fronts at the flip prompt, the accepted pages at a
# multi-page one.
_HANGUP_GRACE_SECONDS: Final = 0.25


class _Interruption:
    """
    Whether a SIGTERM or SIGHUP has reached the running command, and which.

    Set by the signal handler on the main thread, and read after end of input
    at a terminal prompt.  It takes no lock: the handler raises out of
    whatever the main thread was doing, so a lock held there could stay held
    and the next record or clear would block forever.

    ``settled`` is the scan's ``PipelineRequest.settled``, set once the run's
    outcome is fixed or the guard starts reporting a failure.  From then on a
    signal is recorded and deferred rather than raised, because raising could
    only undo finished work, such as a failure's pages half moved into
    ``failed/``.  It is a lock-free ``Settled``, not an ``Event``, for the
    same reason the flag takes no lock.
    """

    def __init__(self) -> None:
        """Start with no signal received and nothing settled."""
        self._received = False
        self.signum: int | None = None
        self.deferred: int | None = None
        self.settled = Settled()

    def record(self, signum: int) -> None:
        """
        Note that ``signum`` arrived.

        Args:
            signum: The signal's number.

        """
        self.signum = signum
        self._received = True

    def received(self) -> bool:
        """
        Say whether a signal has arrived since the command started.

        Returns:
            Whether ``record`` has been called since the last ``clear``.

        """
        return self._received

    def clear(self) -> None:
        """Forget any signal, once the command that received it is over."""
        self.signum = None
        self.deferred = None
        self._received = False
        self.settled.clear()


_INTERRUPTION = _Interruption()


def _interrupt_handler(signum: int, _frame: FrameType | None) -> None:
    """
    Turn SIGTERM or SIGHUP into ``ScanInterrupted`` on the main thread.

    Both signals are ignored from here on, so a second one cannot abandon the
    preservation this one starts.  Once the outcome is settled the signal is
    only recorded, and the command exits as it would have.

    Raises:
        ScanInterrupted: Carrying ``signum``, unless the outcome is settled.

    """
    for each in _INTERRUPT_SIGNALS:
        signal.signal(each, signal.SIG_IGN)
    _INTERRUPTION.record(signum)
    if _INTERRUPTION.settled.is_set():
        _INTERRUPTION.deferred = signum
        return
    msg = f"Interrupted by {signal.Signals(signum).name}"
    raise ScanInterrupted(msg, signum=signum)


def _keep_handlers() -> None:
    """Restore nothing: no handler was installed."""


def _install_interrupt_handlers() -> Callable[[], None]:
    """
    Install the SIGTERM and SIGHUP handlers, returning what puts them back.

    Only the main thread can install a signal handler, so anywhere else this
    installs nothing. A signal whose current handler was installed from
    outside Python (``getsignal`` returns ``None``) is left alone, because
    that handler could not be put back afterwards. So is a signal the command
    was started with ignored: ``nohup saneless scan`` asked for the hangup to
    be ignored, and it stays ignored.

    Returns:
        A function that restores the handlers found here and forgets any
        signal received, for the command's context to call as it closes.

    """
    if threading.current_thread() is not threading.main_thread():
        return _keep_handlers
    # A fresh start, whatever an earlier in-process command left behind.
    _INTERRUPTION.clear()
    previous = {
        signum: handler
        for signum in _INTERRUPT_SIGNALS
        if (handler := signal.getsignal(signum)) is not None
        and handler is not signal.SIG_IGN
    }
    for signum in previous:
        signal.signal(signum, _interrupt_handler)

    def restore() -> None:
        """Put the original handlers back, then forget any signal received."""
        # In this order: once the handlers are back, no late signal can reach
        # _interrupt_handler and set the flag after it was cleared.
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        deferred = _INTERRUPTION.deferred
        if deferred is not None:
            logger.info(
                "%s arrived once the command's outcome was settled, so the "
                "command finished first",
                signal.Signals(deferred).name,
            )
        _INTERRUPTION.clear()

    return restore


def _end_of_input(settle_abort: Callable[[], object], what: str) -> None:
    """
    Treat end of input at a prompt as a cancel, unless a hangup caused it.

    The prompt pauses ``_HANGUP_GRACE_SECONDS`` for a signal first, and
    calls ``settle_abort`` only if none came.  The pause is a plain sleep,
    never a wait on a lock, so the handler's ``ScanInterrupted`` raises out
    of it to the caller.  A signal recorded without raising lets the sleep
    end and leaves the answer to that interruption.
    """
    time.sleep(_HANGUP_GRACE_SECONDS)
    if _INTERRUPTION.received():
        logger.info(
            "%s reached end of input after a signal (%s); "
            "leaving the answer to the interruption",
            what,
            _INTERRUPTION.signum,
        )
        return
    settle_abort()


class _PromptTimedOut(Exception):
    """A terminal prompt's deadline passed with no line typed."""


def _end_the_question_line() -> None:
    """
    End the line a question was left on, when no answer ended it.

    An answer ends the line itself: the terminal echoes the operator's
    Return.  A timeout, end of input or a signal does not, and the line that
    reports it would land after the question on the same row.  A terminal
    that has gone away cannot take the newline either, and that must not
    replace the ending being reported.
    """
    with contextlib.suppress(OSError):
        click.echo()


def _read_line(question: str, deadline: float) -> str | None:
    """
    Ask ``question`` and read one line of answer, on the calling thread.

    A line is read only once stdin is readable.  A terminal in line mode hands
    over at most one line per read, so nothing typed is left buffered where
    the next wait could not see it.  Returns ``None`` at end of input.

    Raises:
        OSError: stdin is closed, or the terminal failed.
        _PromptTimedOut: ``deadline`` passed first.

    """
    stdin = sys.stdin
    if stdin is None or stdin.closed:
        raise OSError(errno.EBADF, "stdin is closed")
    click.echo(question, nl=False)
    try:
        while not _wait_readable(stdin, deadline - _monotonic()):
            if _monotonic() >= deadline:
                raise _PromptTimedOut
        line = stdin.readline()
    except _PromptTimedOut, KeyboardInterrupt, ScanInterrupted:
        _end_the_question_line()
        raise
    if not line:
        _end_the_question_line()
        return None
    return line


# The flip question and the refusal of an answer that is neither yes nor no, in
# click.confirm(_FLIP_PROMPT, default=True)'s own wording.
_FLIP_QUESTION: Final = f"{_FLIP_PROMPT} [Y/n]: "
_INVALID_YES_NO: Final = "Error: invalid input"

# The answers click.confirm accepted, after stripping and lower-casing. An
# empty line takes the default, which is yes.
_FLIP_YES: Final = frozenset({"", "y", "yes"})
_FLIP_NO: Final = frozenset({"n", "no"})


class ClickFlipCoordinator(FlipCoordinator):
    """
    The CLI flip coordinator: a terminal question with a bounded wait.

    The question is asked and read on the calling thread -- the main thread,
    where Python delivers signals -- so every way of leaving it ends the wait
    at once and nothing is left reading stdin.  The deadline answers
    ``TIMED_OUT``.

    A yes, or just Return, is ``CONTINUED``; a no is ``ABORTED``; anything
    else is refused and the question asked again for the time left.  End of
    input and Ctrl-C are ``ABORTED`` too, the same cancel as the web UI's
    Abort, unless a hangup caused the end of input (``_end_of_input``): a
    SIGHUP or SIGTERM propagates out of this call as ``ScanInterrupted``.

    A read that fails -- an I/O error, undecodable input, stdin closed --
    also answers ``ABORTED``, but records the exception as ``abort_cause``:
    nobody chose to stop, so the scan is reported as failed, not cancelled.
    """

    def __init__(self) -> None:
        """Start with no abort cause."""
        self._abort_cause: Exception | None = None

    @property
    def abort_cause(self) -> Exception | None:
        """
        The exception a broken read raised, if one ended the question.

        Returns:
            The read's exception, or ``None`` when the answer came from the
            operator (yes, no, end of input, Ctrl-C) or from the clock.

        """
        return self._abort_cause

    def wait_for_flip(self, timeout: float) -> FlipOutcome:
        """
        Ask the operator, answering ``TIMED_OUT`` if ``timeout`` elapses first.

        Args:
            timeout: The longest to wait for an answer, in seconds.

        Returns:
            The one answer the question resolved to.

        """
        deadline = _monotonic() + timeout
        try:
            return self._ask(deadline)
        except KeyboardInterrupt:
            # Ctrl-C, raised out of the wait or the end-of-input pause: the
            # operator's cancel, so it ends the job the way a web Abort does.
            return FlipOutcome.ABORTED
        except _PromptTimedOut:
            return FlipOutcome.TIMED_OUT
        except (OSError, ValueError) as exc:
            # ValueError covers UnicodeDecodeError.  abort_cause makes the
            # pipeline report a failure, not a cancel.
            logger.exception("Flip prompt failed; treating it as an abort")
            self._abort_cause = exc
            return FlipOutcome.ABORTED

    def _ask(self, deadline: float) -> FlipOutcome:
        """
        Ask until an answer is accepted, input ends, or ``deadline`` passes.

        Args:
            deadline: The ``_monotonic`` reading the answer is due by.

        Returns:
            The operator's answer, or the cancel end of input stands for.

        """
        while True:
            line = _read_line(_FLIP_QUESTION, deadline)
            if line is None:
                return self._end_of_input()
            answer = line.strip().lower()
            if answer in _FLIP_YES:
                return FlipOutcome.CONTINUED
            if answer in _FLIP_NO:
                return FlipOutcome.ABORTED
            click.echo(_INVALID_YES_NO)

    @staticmethod
    def _end_of_input() -> FlipOutcome:
        """
        Answer end of input: a cancel, unless a signal claims the question.

        A raising signal propagates out of ``_end_of_input``'s pause.  One
        recorded without raising leaves the question unanswered, which ends it
        as an unanswered wait does: ``TIMED_OUT``, which keeps the fronts,
        rather than a cancel that would throw them away.

        Returns:
            ``ABORTED`` for the cancel, else ``TIMED_OUT``.

        """
        settled: list[FlipOutcome] = []
        _end_of_input(partial(settled.append, FlipOutcome.ABORTED), "Flip prompt")
        return settled[0] if settled else FlipOutcome.TIMED_OUT


def _flush_typed_ahead() -> None:
    """
    Throw away keys typed while a pass ran, so none answers the next question.

    A second ``n`` pressed during a pass sits in the terminal's input queue and
    would answer the next prompt the moment it appeared, starting a pass on a
    platen nobody has changed.  Only a terminal has such a queue; anything else
    -- a test's fake stdin, a stream with no descriptor, a terminal that went
    away, no stdin at all -- is left alone, and the prompt reads it as it is.
    """
    stdin = sys.stdin
    if stdin is None:
        return
    try:
        if stdin.isatty():
            termios.tcflush(stdin.fileno(), termios.TCIFLUSH)
    except OSError, ValueError, termios.error:
        return


# The abort confirmation as click.confirm(question, default=False) put it. An
# empty line takes the default, which is no; anything else is refused with
# _INVALID_YES_NO and the confirmation asked again.
_ABORT_SUFFIX: Final = " [y/N]: "
_ABORT_YES: Final = frozenset({"y", "yes"})
_ABORT_NO: Final = frozenset({"", "n", "no"})


def _pass_question(prompt: PassPrompt) -> str:
    """
    Return the multi-page question as the terminal shows it, suffix included.

    A failed pass's error text comes from outside saneless -- a SANE status
    string, a wrapped OS error -- so its control characters are shown as
    escapes, as on every other failure line.
    """
    shown = prompt
    if prompt.error is not None:
        shown = replace(prompt, error=neutralise_controls(prompt.error))
    return f"{cli_pass_question(shown)}: "


def _parse_pass_answer(prompt: PassPrompt, line: str) -> PassAnswer | None:
    """
    Turn one typed line into the answer its first letter picks, if any.

    Only the first character counts, in either case, and only a letter the
    prompt lists is accepted.  Anything else is refused with the letters on
    offer, and ``f`` while nothing is kept says why there is nothing to
    finish.  An empty line is passed over without a word.  ``None`` means
    the question must be asked again.
    """
    text = line.rstrip("\r\n")
    if not text:
        return None
    letter = text.strip().lower()[:1]
    letters = {choice.letter: choice.answer for choice in cli_pass_choices(prompt)}
    answer = letters.get(letter)
    if answer is not None:
        return answer
    if letter == "f" and prompt.wait is not PassWait.BLANK_DECISION:
        click.echo(f"Error: {NOTHING_TO_FINISH}")
    else:
        click.echo(f"Error: {cli_choice_hint(prompt)}")
    return None


class ClickPassCoordinator(PassCoordinator):
    """
    The CLI multi-page coordinator: a one-letter question with a bounded wait.

    Each question is asked and read on the calling thread, as the flip
    question is, and its deadline is ``timeout_seconds`` after it was asked,
    which answers ``TIMED_OUT``.  A refused answer does not restart the
    clock.  Keys typed during the pass are thrown away before the question
    shows (``_flush_typed_ahead``).

    Ctrl-C is an unconfirmed cancel, ``ABORT``: a page is only ever kept by
    choosing to finish.  End of input is a cancel too, unless a hangup
    caused it (``_end_of_input``); a SIGHUP or SIGTERM propagates out of
    this call as ``ScanInterrupted``.

    An ``a`` is confirmed first, No by default, and the confirmation holds
    the clock for at most one more ``timeout_seconds``, so an operator
    answering "abort?" as the deadline passes does not have the document
    uploaded under them.  A No before the question's deadline asks again for
    the time left; a No after it, or no answer, answers ``TIMED_OUT``.

    A read that fails answers ``ABORT`` and records ``abort_cause``, so the
    scan is reported as failed, not cancelled, and its pages are kept.
    """

    def __init__(self) -> None:
        """Start with no abort cause."""
        self._abort_cause: Exception | None = None

    @property
    def abort_cause(self) -> Exception | None:
        """
        The exception a broken read raised, if one ended the question.

        Returns:
            The read's exception, or ``None`` when the answer came from the
            operator (a letter, end of input, Ctrl-C) or from the clock.

        """
        return self._abort_cause

    def ask(self, prompt: PassPrompt) -> PassAnswer:
        """
        Put ``prompt`` to the operator; answer ``TIMED_OUT`` if its wait expires.

        Args:
            prompt: The question, and the only answers it accepts.

        Returns:
            The one answer this question resolved to.

        """
        _flush_typed_ahead()
        deadline = _monotonic() + prompt.timeout_seconds
        try:
            return self._ask(prompt, deadline)
        except KeyboardInterrupt:
            # Ctrl-C, raised out of the wait or the end-of-input pause: the
            # operator's cancel, unconfirmed, whatever has been kept.
            return PassAnswer.ABORT
        except _PromptTimedOut:
            return PassAnswer.TIMED_OUT
        except (OSError, ValueError) as exc:
            # ValueError covers UnicodeDecodeError.  abort_cause makes the run
            # report a failure that keeps its pages, not a cancel.
            logger.exception("Multi-page prompt failed; treating it as an abort")
            self._abort_cause = exc
            return PassAnswer.ABORT

    def _ask(self, prompt: PassPrompt, deadline: float) -> PassAnswer:
        """
        Ask until an answer is accepted, input ends, or ``deadline`` passes.

        Args:
            prompt: The open question.
            deadline: The ``_monotonic`` reading the answer is due by.

        Returns:
            The operator's answer, the cancel end of input stands for, or
            ``TIMED_OUT`` when an abort was declined after ``deadline``.

        """
        question = _pass_question(prompt)
        while True:
            line = _read_line(question, deadline)
            if line is None:
                return self._end_of_input()
            answer = _parse_pass_answer(prompt, line)
            if answer is None:
                continue
            if answer is not PassAnswer.ABORT:
                return answer
            confirmed = self._confirm_abort(prompt, deadline)
            if confirmed is not None:
                return confirmed
            if _monotonic() >= deadline:
                return PassAnswer.TIMED_OUT

    def _confirm_abort(self, prompt: PassPrompt, deadline: float) -> PassAnswer | None:
        """
        Ask whether to abort, holding the clock for at most one more timeout.

        Returns ``ABORT`` for a Yes, ``None`` for a No, or what end of input
        stands for.

        Raises:
            _PromptTimedOut: Nobody answered by the confirmation's deadline,
                ``deadline`` plus ``prompt.timeout_seconds``.

        """
        question = f"{abort_question(prompt.pages_kept)}{_ABORT_SUFFIX}"
        confirm_deadline = deadline + prompt.timeout_seconds
        while True:
            line = _read_line(question, confirm_deadline)
            if line is None:
                return self._end_of_input()
            answer = line.strip().lower()
            if answer in _ABORT_YES:
                return PassAnswer.ABORT
            if answer in _ABORT_NO:
                return None
            click.echo(_INVALID_YES_NO)

    @staticmethod
    def _end_of_input() -> PassAnswer:
        """
        Answer end of input: a cancel, unless a signal claims the question.

        A raising signal propagates out of ``_end_of_input``'s pause.  One
        recorded without raising leaves the question unanswered, which ends it
        as an unanswered wait does: ``TIMED_OUT``.

        Returns:
            ``ABORT`` for the cancel, else ``TIMED_OUT``.

        """
        settled: list[PassAnswer] = []
        _end_of_input(partial(settled.append, PassAnswer.ABORT), "Multi-page prompt")
        return settled[0] if settled else PassAnswer.TIMED_OUT


def _truncate(value: str, width: int) -> str:
    """Truncate string to width, appending ellipsis if exceeding limit."""
    if len(value) <= width:
        return value
    return value[: width - 1] + "\u2026"


_CLICK_CONTROL_FLOW: tuple[type[Exception], ...] = (
    click.exceptions.Exit,
    click.exceptions.Abort,
    click.ClickException,
)
"""click's own control-flow exceptions, which the group guard re-raises untouched.

``--help`` raises ``Exit``, EOF at a prompt raises ``Abort``, and a usage error
is a ``ClickException``; click's ``main`` turns each into its own output and exit
code. Held under a name because the parenthesis-free multi-type ``except`` ruff
formats to (PEP 758) does not parse on the older interpreter the pre-commit AST
hooks run.
"""


_VERBOSE_HINT = "Run again with -v to see the traceback"
"""The hint an unexpected error's line ends with when no traceback was kept."""


def _logging_ready(ctx: click.Context) -> bool:
    """
    Whether ``_load_cli_settings`` has configured logging for this process.

    Before that, an ERROR record has no handler but ``logging.lastResort``,
    which would print it -- traceback and all -- straight to stderr, so the
    guard must not log at all.
    """
    obj = ctx.obj
    return isinstance(obj, dict) and bool(obj.get("logging_configured"))


def _unexpected_line(exc: BaseException) -> str:
    """
    Render the one line an exception that is not a saneless type is reported as.

    The message is ``failure_text``'s, so any note on the exception is part of
    it; no hint is appended.
    """
    return f"Unexpected error ({type(exc).__name__}): {failure_text(exc)}"


def _failure_line(exc: SanelessError, category: ErrorCategory) -> str:
    """
    Render the one line a classified saneless failure is reported as.

    The prefixes are documented (``docs/how-to/set-up-adf-duplex.md`` quotes
    them), so they must not change.  A configuration error keeps the loader's
    text, which already has its header and escapes its own names, and appends
    only the notes.  Every other line has its control characters escaped,
    because it can carry text from outside saneless such as a device name.
    Messages go through ``failure_text``, so an ``add_note`` note is on the
    line too.
    """
    match category:
        case ErrorCategory.FEEDER | ErrorCategory.SCANNER:
            line = f"Scan error: {failure_text(exc)}"
        case ErrorCategory.CONFIG:
            notes = note_text(exc)
            return f"{exc} {neutralise_controls(notes)}" if notes else str(exc)
        case (
            ErrorCategory.UPLOAD
            | ErrorCategory.UNCONFIRMED_SEND
            | ErrorCategory.UNCONFIRMED_FILING
            | ErrorCategory.PAPERLESS_VERSION
        ):
            line = f"Paperless error: {failure_text(exc)}"
        case ErrorCategory.ASSEMBLY:
            line = f"PDF error: {failure_text(exc)}"
        case ErrorCategory.ALL_BLANK:
            line = f"Empty-page detection: {failure_text(exc)}"
        case ErrorCategory.DISK_SPACE:
            line = f"Disk space: {failure_text(exc)}"
        case ErrorCategory.UNKNOWN | ErrorCategory.REJECTED:
            line = _unexpected_line(exc)
        case _:
            assert_never(category)
    return neutralise_controls(line)


def _advice_line(exc: SanelessError, category: ErrorCategory) -> str:
    """
    Render the ``Try:`` line that follows a classified failure's line.

    A raise site's own ``next_step`` wins over the category's fallback, which
    has to be true for every error in the category.  Every category has a
    fallback, so no classified failure ends without a next step.  Control
    characters are escaped, because a next step can carry a setting key or a
    file name and the line must stay one line.
    """
    return neutralise_controls(f"Try: {exc.next_step or error_next_step(category)}")


def _echo_err(text: str, *, nl: bool = True) -> None:
    """
    Write one line of a command's report to stderr, surviving a dead terminal.

    A terminal that has gone away -- the dropped SSH session behind a SIGHUP
    -- answers every write with EIO, and an ``OSError`` escaping here would
    replace the exit code the command chose.  So a failed write is only
    logged, and ``drain_dead_streams`` discards what it left behind.
    """
    try:
        click.echo(text, err=True, nl=nl)
    except OSError:
        logger.info("Could not write to stderr: %r", text, exc_info=True)


def _echo_out(text: str) -> None:
    """
    Write one line of a scan's report to stdout, surviving a dead terminal.

    The stdout twin of ``_echo_err``.  A hangup after delivery is deferred, so
    the scan prints its outcome to a terminal that is already gone, and the
    exit code chosen from the result must stand rather than become exit 5.
    """
    try:
        click.echo(text)
    except OSError:
        logger.info("Could not write to stdout: %r", text, exc_info=True)


def _drain_dead_stream(stream: TextIO | None) -> None:
    """Discard what ``sys.stdout`` or ``sys.stderr`` can no longer write."""
    if stream is None:
        return
    try:
        stream.flush()
    except ValueError:
        return  # Closed already: shutdown will not flush it either.
    except OSError:
        pass
    else:
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, stream.fileno())
        finally:
            os.close(devnull)
    except OSError, ValueError:
        logger.info("Could not detach a dead output stream", exc_info=True)
        return
    with contextlib.suppress(OSError):
        stream.flush()  # The bytes the dead terminal refused now go nowhere.


def drain_dead_streams() -> None:
    """
    Point a dead stdout or stderr at ``/dev/null`` before the interpreter exits.

    A write that fails on a terminal that has gone away leaves its bytes in
    the stream's buffer.  The interpreter's last flush at shutdown then fails
    too, and CPython replaces the command's exit code with 120.  So each
    stream that still cannot be flushed has its descriptor pointed at
    ``/dev/null``; a stream that flushes normally is left untouched.

    Called once, as the console entry point returns, so it covers every write
    the command made.
    """
    _drain_dead_stream(sys.stdout)
    _drain_dead_stream(sys.stderr)


def _log_failure(ctx: click.Context, exc: Exception) -> None:
    """
    Log a classified failure, but only once logging is configured.

    The message is quoted with ``%r``, because it can carry text from outside
    saneless.  The traceback goes with the record only when the log is a
    file: ``serve``'s stderr stream renders every traceback, which would
    stack saneless's frames above a setup problem the failure and ``Try:``
    lines already explain.  An unexpected error is logged by
    ``_report_unexpected`` instead.
    """
    if not _logging_ready(ctx):
        return
    streamed = isinstance(ctx.obj, dict) and bool(ctx.obj.get("log_stream"))
    logger.error(
        "saneless %s failed: %r",
        ctx.invoked_subcommand,
        failure_text(exc),
        exc_info=None if streamed else exc,
    )


def _report_unexpected(ctx: click.Context, exc: Exception) -> None:
    """
    Report an exception that is not a saneless type as one stderr line.

    Once logging is configured the traceback goes to the log, and the line
    names the log file only when the file handler really attached; a stderr
    fallback renders no traceback, so without ``-v`` the line says how to get
    one.  Before logging is configured, ``-v`` prints the traceback to stderr
    instead.  ``serve``'s stream already shows the traceback, so it gets no
    hint.
    """
    obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    line = _unexpected_line(exc)
    verbose = bool(obj.get("verbose"))
    if _logging_ready(ctx):
        logger.error(
            "Unexpected error in saneless %s", ctx.invoked_subcommand, exc_info=exc
        )
        log_file = obj.get("log_file")
        if log_file:
            line = f"{line}. Full details in {log_file}"
        elif not verbose and not obj.get("log_stream"):
            line = f"{line}. {_VERBOSE_HINT}"
    elif verbose:
        _echo_err("".join(traceback.format_exception(exc)), nl=False)
    else:
        line = f"{line}. {_VERBOSE_HINT}"
    _echo_err(line)


class _GuardedGroup(click.Group):
    """
    The CLI group with one last-resort handler around every command.

    Every failure a command raises becomes a stderr message and its
    ``ExitCode``: the failure line, then a ``Try:`` line when the failure is
    classified.  A one-shot command shows no traceback unless ``-v`` asked for
    one; ``serve``'s stream shows one only for an unexpected error. The
    ``except`` clauses are ordered, and the order is the design:

    1. click's ``Exit``, ``Abort`` and ``ClickException`` are re-raised first.
       ``Exit`` and ``Abort`` subclass ``RuntimeError``, so a later
       ``except Exception`` would turn ``--help`` into exit 5.
    2. ``BrokenPipeError`` -- the reader of stdout went away, as under
       ``saneless jobs | head`` -- is no failure: nothing is printed and the
       command exits ``ExitCode.BROKEN_PIPE``.  It must come before the last
       clause, which would call it a saneless bug.
    3. ``ScanInterrupted`` -- a SIGHUP or SIGTERM -- is an interruption, not
       a cancel: the pages already scanned were kept, and the exit code is
       128 plus the signal number.  It is a ``BaseException``, so no later
       clause would catch it.  A signal after the outcome is settled is
       deferred instead, and the command exits with its own outcome's code.
    4. ``KeyboardInterrupt`` and ``ScanCancelledError`` are a cancel:
       ``ExitCode.CANCELLED``.
    5. ``StorageError`` -- a job database saneless cannot use -- is a setup
       problem, ``ExitCode.CONFIG``, mapped by type before the
       ``SanelessError`` clause because ``classify_error`` keeps it
       ``UNKNOWN``.  See docs/explanation/decisions/0008-no-storage-error-category.md.
    6. Any other ``SanelessError`` is classified once, by the same
       ``classify_error`` the web worker uses, so the CLI's exit code and the
       job's category cannot disagree.
    7. Anything else is not a saneless type: ``Unexpected error (<Type>)``,
       ``ExitCode.UNEXPECTED``, the traceback in the log.
    """

    def invoke(self, ctx: click.Context) -> object:
        """
        Invoke the subcommand, translating whatever it raises.

        Args:
            ctx: The group's context.

        Returns:
            Whatever the subcommand returned.

        """
        try:
            try:
                return super().invoke(ctx)
            except BaseException:
                # From here the command only reports how it ended, so a signal
                # is deferred rather than raised into the middle of an arm
                # below, where it would escape the guard as a traceback.
                _INTERRUPTION.settled.set()
                raise
        except _CLICK_CONTROL_FLOW:
            raise
        except BrokenPipeError:
            # What the reader refused is discarded, so the interpreter's last
            # flush raises nothing either.
            logger.info("The reader of saneless's output went away; stopping")
            _drain_dead_stream(sys.stdout)
            ctx.exit(ExitCode.BROKEN_PIPE)
        except ScanInterrupted as exc:
            logger.info("Command interrupted by a signal: %r", failure_text(exc))
            _echo_err(neutralise_controls(f"Interrupted: {failure_text(exc)}"))
            ctx.exit(exit_code_for_signal(exc.signum))
        except KeyboardInterrupt:
            logger.info("Command interrupted")
            _echo_err("Cancelled (interrupted)")
            ctx.exit(ExitCode.CANCELLED)
        except ScanCancelledError as exc:
            logger.info("Scan cancelled: %s", exc)
            _echo_err(failure_text(exc))
            ctx.exit(ExitCode.CANCELLED)
        except StorageError as exc:
            _log_failure(ctx, exc)
            _echo_err(f"Job database error: {failure_text(exc)}")
            _echo_err(_advice_line(exc, ErrorCategory.CONFIG))
            ctx.exit(ExitCode.CONFIG)
        except SanelessError as exc:
            category = classify_error(exc)
            code = exit_code_for(category)
            if code is ExitCode.UNEXPECTED:
                _report_unexpected(ctx, exc)
            else:
                _log_failure(ctx, exc)
                # Line 1 keeps its documented `<what saneless was doing>:
                # <problem>` shape, which scripts parse.  The UNEXPECTED branch
                # above gets no advice: its category is only a guess.
                _echo_err(_failure_line(exc, category))
                _echo_err(_advice_line(exc, category))
            ctx.exit(code)
        except Exception as exc:
            _report_unexpected(ctx, exc)
            ctx.exit(ExitCode.UNEXPECTED)


@click.group(cls=_GuardedGroup)
# package_name reads the version from importlib.metadata, so pyproject.toml
# stays its single source. No short flag: `-v` is --verbose on this group, and
# click would silently bind it to whichever option declared it last.
@click.version_option(package_name="saneless")
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Path to config file.",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Log saneless's own debug detail: stderr, and the log file outside serve.",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, *, verbose: bool) -> None:
    """Saneless -- SANE scanner to paperless-ngx bridge."""
    # Click runs this callback before a subcommand parses its own --help, so
    # nothing may be loaded here: a broken config would break
    # `saneless serve --help`.
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["verbose"] = verbose
    # SIGTERM and SIGHUP become ScanInterrupted.  This context closes after the
    # guard has chosen the exit code, which is when the original handlers go
    # back, so none leaks into an in-process caller.  serve is left to uvicorn,
    # which installs its own handlers for a graceful shutdown.
    if ctx.invoked_subcommand != "serve":
        ctx.call_on_close(_install_interrupt_handlers())


# The two situations in which a file under the superseded name is sitting in a
# searched directory doing nothing.  Named once, so the stderr warning below
# says the same thing the Configuration row does about the same situations.
_SUPERSEDED_NAME_STATES: Final = (
    ConfigFileState.STALE_ONLY,
    ConfigFileState.LOADED_WITH_LEFTOVER,
)


def _warn_stale_config(settings: Settings) -> None:
    """
    Say on stderr which config file is not being read, if one is not.

    Either a file under the old name is being ignored, or a second
    ``saneless.toml`` in a later searched directory is shadowed.  A one-shot
    command's log is a file read afterwards if at all, so without this line
    the operator sees nothing.  ``configuration_check`` holds the one copy of
    the words; the terminal may name absolute paths, which the LAN-visible
    strip may not.  The row shows only one situation, so a leftover, which
    may hold the only copy of the Paperless credentials, gets its own line.
    """
    state = config_file_state(settings)
    if state is ConfigFileState.LOADED_WITH_SHADOWED:
        rows = [
            configuration_check(settings, absolute_paths=True),
            leftover_config_check(settings, absolute_paths=True),
        ]
    elif state in _SUPERSEDED_NAME_STATES:
        rows = [configuration_check(settings, absolute_paths=True)]
    else:
        return
    for row in rows:
        if row is not None:
            click.echo(f"Warning: {row.message} {row.next_step}", err=True)


def _load_cli_settings(
    ctx: click.Context,
    *,
    stream_logs: bool = False,
    warn_stale: bool = True,
    validate_dirs: bool = True,
) -> Settings:
    """
    Load and validate settings and configure logging, once per process.

    Called by every command on first need rather than by the group callback,
    so ``--help`` never touches the configuration.  Nothing is caught here;
    every failure reaches the group guard.  ``ctx.obj`` records ``log_file``
    only if the file handler really attached, so the guard names the log file
    truthfully.  The same settings object is returned on every call.
    ``load_settings`` and ``configure_logging`` are called by their
    module-global names, which is where the tests patch them.

    ``stream_logs`` (``serve`` only) sends records to stderr and writes no log
    file.  ``warn_stale=False`` (``auto-profiles`` only) leaves the
    superseded-name warning to the command, whose refusal is the same
    sentence.  ``validate_dirs=False`` (``doctor`` only) skips the folder
    refusal, because doctor reports those folders row by row.
    """
    cached = ctx.obj.get("settings")
    if isinstance(cached, Settings):
        return cached

    settings = load_settings(ctx.obj.get("config_path"))
    if validate_dirs:
        validate_settings_dirs(settings)
    if not stream_logs:
        _make_log_home_private(settings)
    # A service is handed no log file, so configure_logging attaches one stderr
    # handler and returns False; the rotation settings then go unused.
    attached = configure_logging(
        None if stream_logs else settings.output.log_file,
        settings.output.log_level,
        max_bytes=settings.output.log_max_bytes,
        backup_count=settings.output.log_backup_count,
        verbose=bool(ctx.obj.get("verbose")),
    )
    ctx.obj["logging_configured"] = True
    ctx.obj["log_file"] = settings.output.log_file if attached else None
    # Read by _report_unexpected: a stream that already renders tracebacks
    # must not end an exit-5 line with "run again with -v".
    ctx.obj["log_stream"] = stream_logs

    # Emitted only now, once the log file handler exists to receive it.
    warn_on_legacy_duplex_sources(settings)
    log_config_sources(settings)
    # A service's log is already on stderr, where the line above has said it.
    if warn_stale and not stream_logs:
        _warn_stale_config(settings)

    ctx.obj["settings"] = settings
    return settings


def _make_log_home_private(settings: Settings) -> None:
    """
    Create ``data_dir`` owner-only before logging can create it with the umask.

    The first one-shot command on a new install creates ``data_dir`` while
    setting up logging, and every later call leaves its mode alone, so it has
    to come out 0700 here.  ``configure_logging`` makes only the log file's
    own directory private.  A failure is left to ``configure_logging``, which
    meets the same error and falls back to stderr with a warning.
    """
    output = settings.output
    if not output.log_file.is_relative_to(output.data_dir):
        return
    with contextlib.suppress(OSError):
        make_private_dir(output.data_dir)


def _recover_orphaned_workspaces(settings: Settings) -> None:
    """
    Keep the pages a killed scan left in ``tmp_dir``, before this scan starts.

    A CLI-only install has no server start-up to recover a killed scan, so
    each ``saneless scan`` sweeps first.  Only workspaces whose lock is free
    are taken, so a scan still running is never touched.  The sweep never
    decides whether the scan runs: a failure is logged and the scan goes
    ahead.
    """
    output = settings.output
    try:
        sweep_orphans(output.tmp_dir, output.failed_dir, output.min_free_space_mb)
    except Exception:
        logger.warning(
            "Could not recover the orphaned scan workspaces in %s; scanning anyway",
            output.tmp_dir,
            exc_info=True,
        )


def _scan_title(title: str, profile: ProfileConfig, *, now: datetime) -> str:
    """
    Resolve the title ``scan`` uploads under, refusing one paperless-ngx would cut.

    The rule is the one the web form shares.  A title longer than
    paperless-ngx keeps whole is refused before the scanner exists, never
    shortened.

    Raises:
        click.BadParameter: If the title is longer than ``TITLE_MAX_LENGTH``,
            which click reports as a usage error naming ``--title`` (exit 2).

    """
    resolved = resolve_job_title(title, profile, now=now)
    if len(resolved) > TITLE_MAX_LENGTH:
        raise click.BadParameter(
            rejection_message(RequestRejection.TITLE_TOO_LONG), param_hint="--title"
        )
    return resolved


@cli.command()
@click.option(
    "--profile",
    default="default",
    help="Scan profile name.",
)
@click.option(
    "--title",
    default="",
    help="Document title (default: the profile's title, else 'Scan <time>').",
)
@click.option("--multi-page", "multi_page", is_flag=True, help=MULTI_PAGE_OPTION_HELP)
@click.pass_context
def scan(ctx: click.Context, profile: str, title: str, *, multi_page: bool) -> None:
    """Scan a document and upload to paperless-ngx."""
    # Refuses before loading config or touching the device.
    require_sane()
    settings = _load_cli_settings(ctx)
    # Before anything that can refuse the scan, so a killed scan's pages are
    # recovered whether or not this one goes ahead.
    _recover_orphaned_workspaces(settings)

    # Every refusal below is raised, not printed, so the guard logs it and
    # adds its Try: line.
    if profile not in settings.profiles:
        msg = f"Unknown profile: {profile}"
        raise ConfigError(msg, next_step=UNKNOWN_PROFILE_NEXT_STEP)

    now = datetime.now(tz=UTC)
    resolved_title = _scan_title(title, settings.profiles[profile], now=now)

    # Refused before the scanner is opened, because a scan that cannot upload
    # is wasted paper. A consume_dir fallback does not soften it, or `scan`,
    # `doctor` and the web UI would disagree about whether the appliance works.
    #
    # The token goes to the predicate and nowhere else: it is never logged,
    # echoed or interpolated into the message (ASVS 4.0.3 V7.1). The message
    # carries its own "what saneless was doing" half, because a CONFIG error
    # is printed as-is.
    unset = None
    if is_placeholder_token(settings.paperless.token.get_secret_value()):
        unset = _UNSET_CREDENTIAL_PROBLEM
    elif not settings.paperless.url:
        unset = _UNSET_ADDRESS_PROBLEM
    if unset is not None:
        msg = f"Scanning '{resolved_title}' with profile '{profile}': {unset}"
        raise ConfigError(msg)

    manual_duplex = settings.profiles[profile].duplex == "manual"
    # Every refusal below comes before the backend exists, so no paper moves.
    # Manual duplex first: a terminal would not help there.
    if multi_page and manual_duplex:
        raise ConfigError(
            multi_page_manual_duplex_refusal(profile),
            next_step=MULTI_PAGE_MANUAL_DUPLEX_NEXT_STEP,
        )
    if multi_page and not _stdin_is_interactive():
        raise ConfigError(
            MULTI_PAGE_NEEDS_TERMINAL, next_step=SCAN_FROM_A_TERMINAL_NEXT_STEP
        )
    if manual_duplex and not _stdin_is_interactive():
        raise ConfigError(
            manual_duplex_needs_terminal_refusal(profile),
            next_step=SCAN_FROM_A_TERMINAL_NEXT_STEP,
        )

    scanner = SaneBackend(host=settings.scanner.host)
    # click runs close callbacks on every way out of this command, before the
    # guard chooses an exit code, so SANE is down before the error line prints.
    ctx.call_on_close(scanner.close)
    paperless = PaperlessClient(
        settings.paperless.url,
        settings.paperless.token.get_secret_value(),
        settings.paperless.consume_dir,
    )

    def status_callback(event: PipelineEvent) -> None:
        # DONE is announced for an upload and a consume-folder fallback alike,
        # warning or not, so it cannot choose the closing line: the ScanResult
        # does, once run_pipeline returns.
        if event is not PipelineEvent.DONE:
            click.echo(progress_label(event.job_state))

    try:
        request = build_pipeline_request(
            profile_name=profile,
            title=resolved_title,
            # A uuid4, as for a web job: file names in failed/ are unique only
            # through the job id, and preservation never replaces a file there.
            job_id=str(uuid4()),
            # No tag or correspondent option, so the profile's defaults apply,
            # as on a web form nobody touched.
            metadata=resolve_scan_metadata(
                settings.profiles[profile],
                tags=None,
                correspondent=None,
                correspondent_given=False,
            ),
            hooks=RequestHooks(
                status_callback=status_callback,
                # run_pipeline is synchronous, so every question is asked and
                # read on this thread, where a signal ends its wait.
                flip_coordinator=ClickFlipCoordinator() if manual_duplex else None,
                multi_page=multi_page,
                pass_coordinator=ClickPassCoordinator() if multi_page else None,
                # The run sets it once its outcome is fixed, and from then on
                # _interrupt_handler defers a signal instead of raising it.
                settled=_INTERRUPTION.settled,
            ),
        )
        result = run_pipeline(
            scanner,
            paperless,
            settings,
            request,
        )
    finally:
        paperless.close()

    # These lines and the exit code are the CLI's whole report of a scan;
    # nothing goes to the job store. Every line goes through a helper that
    # survives a dead terminal, because a hangup after delivery is deferred
    # and must not replace the exit code with a crash.
    _echo_out(
        outcome_line(job_state_for(result.outcome), result.warning, resolved_title)
    )
    # Removed pages are not kept anywhere, so the note names them for a rescan.
    # It is information about a success, not a warning, so it goes to stdout.
    removed_note = removed_pages_note(result.removed_positions, result.pages_scanned)
    if removed_note is not None:
        _echo_out(removed_note)
    if result.outcome is ScanOutcome.FALLBACK:
        _echo_err(FALLBACK_NOT_UPLOADED_LINE)
    if result.warning:
        _echo_err(f"Warning: {result.warning}")
    code = exit_code_for_outcome(result.outcome, result.warning)
    if code is not ExitCode.SUCCESS:
        ctx.exit(code)


def _echo_capabilities(caps: DeviceCapabilities) -> None:
    """
    Print one device's capabilities, naming only what that device reported.

    A line is printed only when there is something after its label: a bare
    label reads as "this scanner offers none" when saneless had only not read
    what it offered.
    """
    if caps.sources:
        sources = ", ".join(neutralise_controls(s) for s in caps.sources)
        click.echo(f"  Sources: {sources}")
    if caps.resolutions:
        click.echo(f"  Resolutions: {', '.join(str(r) for r in caps.resolutions)}")
    elif caps.resolution_range is not None:
        # Shown as the span the device gave: expanding it into a list would
        # print saneless's invention rather than the device's answer.
        low, high, step = caps.resolution_range
        click.echo(f"  Resolution range: {low:g} to {high:g} dpi in steps of {step:g}")
    if caps.modes:
        modes = ", ".join(neutralise_controls(m) for m in caps.modes)
        click.echo(f"  Modes: {modes}")
    if caps.option_names:
        click.echo("  Raw options:")
        for name in caps.option_names:
            click.echo(f"    {neutralise_controls(name)}")


def _capabilities_dict(caps: DeviceCapabilities) -> dict[str, object]:
    """
    Return one device's capabilities as the JSON object ``devices`` prints.

    This is the machine contract scripts read, so it carries exactly what
    ``_echo_capabilities`` shows: a key appears only when the device reported
    something for it, and the resolution support keeps the device's shape.
    """
    result: dict[str, object] = {}
    if caps.sources:
        result["sources"] = list(caps.sources)
    if caps.resolutions:
        result["resolutions"] = list(caps.resolutions)
    elif caps.resolution_range is not None:
        low, high, step = caps.resolution_range
        result["resolution_range"] = {"min": low, "max": high, "step": step}
    if caps.modes:
        result["modes"] = list(caps.modes)
    # The key keeps its documented name: scripts read "raw_options", whatever
    # the value object calls the field.
    if caps.option_names:
        result["raw_options"] = list(caps.option_names)
    return result


def _probe_capabilities(scanner: ScannerBackend, name: str) -> DeviceCapabilities | str:
    """
    Read one device's capabilities, or report why they could not be read.

    Only a scanner error is caught, so one device that will not answer does
    not hide the others: its reason is printed on stderr and returned.
    Anything else reaches the group guard.
    """
    try:
        return scanner.get_capabilities(name)
    except ScanError as exc:
        reason = describe(exc)
        logger.warning("Could not read capabilities for %r: %r", name, reason)
        click.echo(
            f"Capabilities for {neutralise_controls(name)}: "
            f"{neutralise_controls(reason)}",
            err=True,
        )
        return reason


def _echo_device_table(device_list: list[DeviceInfo]) -> None:
    """
    Print the device list as a table sized to the terminal.

    Each value is escaped before it is cut, so the width is measured on what
    prints, and every row fits a terminal of 70 columns or more.
    """
    cols = shutil.get_terminal_size((80, 24)).columns
    vendor_w = _DEVICE_VENDOR_COL_WIDTH
    model_w = _DEVICE_MODEL_COL_WIDTH
    type_w = _DEVICE_TYPE_COL_WIDTH
    # Three single spaces separate the four columns.
    name_w = max(_DEVICE_NAME_COL_FLOOR, cols - (vendor_w + model_w + type_w + 3))
    header = f"{'Name':<{name_w}} {'Vendor':<{vendor_w}} {'Model':<{model_w}} {'Type'}"
    click.echo(header)
    click.echo("-" * min(len(header), cols))
    for d in device_list:
        click.echo(
            f"{_truncate(neutralise_controls(d.name), name_w):<{name_w}} "
            f"{_truncate(neutralise_controls(d.vendor), vendor_w):<{vendor_w}} "
            f"{_truncate(neutralise_controls(d.model), model_w):<{model_w}} "
            f"{_truncate(neutralise_controls(d.device_type), type_w)}"
        )


def _devices_as_json(
    scanner: ScannerBackend, device_list: list[DeviceInfo], *, capabilities: bool
) -> int:
    """
    Print the device list as one JSON document on stdout.

    With ``capabilities`` each device also gets ``capabilities``, which is
    ``null`` beside a ``capabilities_error`` reason when its probe failed.
    Returns how many probes failed.
    """
    data: list[dict[str, object]] = [
        {
            "name": d.name,
            "vendor": d.vendor,
            "model": d.model,
            "type": d.device_type,
        }
        for d in device_list
    ]
    failed = 0
    if capabilities:
        for entry, d in zip(data, device_list, strict=True):
            probed = _probe_capabilities(scanner, d.name)
            if isinstance(probed, str):
                entry["capabilities"] = None
                entry["capabilities_error"] = probed
                failed += 1
            else:
                entry["capabilities"] = _capabilities_dict(probed)
    click.echo(json.dumps(data, indent=2))
    return failed


def _devices_as_text(
    scanner: ScannerBackend, device_list: list[DeviceInfo], *, capabilities: bool
) -> int:
    """
    Print the device table, then each device's capabilities when asked.

    Returns how many probes failed.
    """
    _echo_device_table(device_list)
    failed = 0
    if capabilities:
        click.echo()
        for d in device_list:
            probed = _probe_capabilities(scanner, d.name)
            if isinstance(probed, str):
                failed += 1
                continue
            click.echo(f"Capabilities for {neutralise_controls(d.name)}:")
            _echo_capabilities(probed)
    return failed


@cli.command()
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Output as JSON.",
)
@click.option(
    "--capabilities",
    is_flag=True,
    help="Show sources, modes, resolution support and raw SANE option names.",
)
@click.pass_context
def devices(ctx: click.Context, *, as_json: bool, capabilities: bool) -> None:
    """List available scanning devices."""
    require_sane()
    _settings = _load_cli_settings(ctx)

    # Status goes to stderr, so stdout carries only data in either mode and a
    # pipe into jq or grep sees nothing else.
    if not as_json:
        click.echo("Discovering scanners...", err=True)
    scanner = SaneBackend(host=_settings.scanner.host)
    # Registered before the first SANE call, so an enumeration that fails still
    # leaves the process with SANE shut down.
    ctx.call_on_close(scanner.close)
    device_list = scanner.get_devices()

    if not device_list:
        # The empty JSON array is data, for stdout; the sentence is status.
        if as_json:
            click.echo("[]")
        else:
            click.echo("No scanners found.", err=True)
        return

    render = _devices_as_json if as_json else _devices_as_text
    failed = render(scanner, device_list, capabilities=capabilities)
    # The exit code says a capability read failed, once everything is written.
    if failed:
        ctx.exit(ExitCode.SCAN)


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option(
    "--limit",
    default=CLI_JOBS_DEFAULT_LIMIT,
    type=click.IntRange(min=1),
    help="Maximum jobs to show (at least 1).",
)
@click.pass_context
def jobs(ctx: click.Context, *, as_json: bool, limit: int) -> None:
    """List recent scan job history."""
    settings = _load_cli_settings(ctx)
    # A read and nothing more, because the history may belong to a running
    # server: no database, folder, schema upgrade or (for anyone but its owner)
    # -wal or -shm file is created. The log file set up above is outside that
    # promise, and under sudo it may be created or rotated owned by root.
    recent = read_recent_jobs(settings.output.db_path, limit)
    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "id": j.id,
                        "profile": j.profile,
                        "title": j.title,
                        # The JSON is the machine contract documented in
                        # docs/how-to/cli-scripting.md: raw enum values and
                        # UTC ISO-8601, never humanised or localised.
                        "state": j.state.value,
                        "created_at": j.created_at.isoformat(),
                        "outcome": j.outcome.value if j.outcome else None,
                        "warning": j.warning,
                        # The full stored text, host paths included: the web
                        # page shows only a path-free sentence pointing here.
                        "error": j.error,
                        # null when never recorded, never a zero. Positions
                        # are 1-based scanned page numbers in document order.
                        "pages_scanned": j.pages_scanned,
                        "pages_removed": j.pages_removed,
                        "pages_uploaded": j.pages_uploaded,
                        "pages_removed_positions": (
                            None
                            if j.removed_positions is None
                            else list(j.removed_positions)
                        ),
                    }
                    for j in recent
                ],
                indent=2,
            )
        )
    else:
        cols = shutil.get_terminal_size((80, 24)).columns
        ts_w = _TIME_COL_WIDTH
        profile_w = _PROFILE_COL_WIDTH
        # Three single spaces separate the four columns.
        title_w = max(
            _TITLE_COL_FLOOR, cols - (ts_w + profile_w + _STATUS_COL_WIDTH + 3)
        )
        header = (
            f"{'Timestamp':<{ts_w}} {'Profile':<{profile_w}} "
            f"{'Title':<{title_w}} {'Status'}"
        )
        click.echo(header)
        click.echo("-" * min(len(header), cols))
        for j in recent:
            # Escaped before truncating, so the width is measured on what
            # prints.
            profile = _truncate(neutralise_controls(j.profile), profile_w)
            title = _truncate(neutralise_controls(j.title), title_w)
            click.echo(
                # The formatter the web history table reads too.
                f"{local_time(j.created_at):<{ts_w}} "
                f"{profile:<{profile_w}} "
                f"{title:<{title_w}} "
                f"{job_label(j.state, j.warning, j.error_category)}"
            )


def _bind_next_step(exc: OSError) -> str:
    """
    Choose the advice for an address ``serve`` could not bind, from its errno.

    A port another program holds, a port this user may not take and an
    address this machine does not have are three different fixes.  Anything
    else names both settings without guessing which is wrong.
    """
    match exc.errno:
        case errno.EADDRINUSE:
            return SERVE_PORT_IN_USE_NEXT_STEP
        case errno.EACCES:
            return SERVE_PORT_NOT_ALLOWED_NEXT_STEP
        case errno.EADDRNOTAVAIL:
            return SERVE_ADDRESS_NEXT_STEP
        case _:
            return SERVE_BIND_NEXT_STEP


def _bind_listening_sockets(host: str, port: int) -> list[socket.socket]:
    """
    Bind and listen on every address ``serve`` was given, before uvicorn starts.

    Bound here rather than by uvicorn so that the port saneless reports is the
    port it holds, with no gap between choosing a port and binding it.  Every
    address the host resolves to is bound once -- ``localhost`` is usually
    both ``::1`` and ``127.0.0.1``, and a proxy pointed at either must reach
    saneless -- and with port 0 the OS's first choice is reused for the rest.
    If any address cannot be bound, the sockets already bound are closed and
    ``serve`` fails rather than start on part of what was asked.

    Raises:
        ConfigError: The host did not resolve or an address could not be
            bound.

    """
    try:
        infos = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )
    except OSError as exc:
        # The name did not resolve: the host is what to change.
        msg = f"Cannot bind to {host}:{port}: {describe(exc)}"
        raise ConfigError(msg, next_step=SERVE_ADDRESS_NEXT_STEP) from exc
    sockets: list[socket.socket] = []
    seen: set[tuple[int, str]] = set()
    try:
        for family, socktype, proto, _, sockaddr in infos:
            address = str(sockaddr[0])
            if (family, address) in seen:
                continue
            seen.add((family, address))
            # The first bound socket fixes the port when the OS chose it.
            bind_address = (
                (address, sockets[0].getsockname()[1], *sockaddr[2:])
                if sockets
                else sockaddr
            )
            sockets.append(
                _bind_one(host, port, (family, socktype, proto), bind_address)
            )
    except BaseException:
        for sock in sockets:
            sock.close()
        raise
    return sockets


def _bind_one(
    host: str,
    port: int,
    kind: tuple[int, int, int],
    sockaddr: tuple[object, ...],
) -> socket.socket:
    """
    Bind and listen on one resolved address.

    The options match uvicorn's own bind.  SO_REUSEADDR lets a restart bind
    a port whose last connections are still closing; IPV6_V6ONLY keeps
    ``::`` from also listening on IPv4; port sharing stays off, so no second
    process can listen on the port.  ``listen()`` is called before the socket
    is handed over, so a connection made during start-up waits in the queue.
    ``host`` and ``port`` are only for the message.

    Raises:
        ConfigError: The address could not be bound.

    """
    family, socktype, proto = kind
    address = str(sockaddr[0])
    where = f"{host}:{port}" if address == host else f"{host}:{port} ({address})"
    try:
        sock = socket.socket(family, socktype, proto)
    except OSError as exc:
        msg = f"Cannot bind to {where}: {describe(exc)}"
        raise ConfigError(msg, next_step=_bind_next_step(exc)) from exc
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind(sockaddr)
        sock.listen(_LISTEN_BACKLOG)
    except OSError as exc:
        sock.close()
        msg = f"Cannot bind to {where}: {describe(exc)}"
        raise ConfigError(msg, next_step=_bind_next_step(exc)) from exc
    return sock


@cli.command()
@click.option("--host", default=None, help="Bind address.")
@click.option(
    "--port",
    default=None,
    type=click.IntRange(0, 65_535),
    help="Bind port; 0 lets the OS choose one.",
)
@click.pass_context
def serve(ctx: click.Context, host: str | None, port: int | None) -> None:
    """Start the web server."""
    # Imported here, so that no other command pays for the web stack.
    from .web.app import create_app
    from .web.server import run_server
    from .web.services import Services

    require_sane()
    # A service writes no log file; its records reach `docker logs` and
    # journald through stderr.
    settings = _load_cli_settings(ctx, stream_logs=True)
    actual_host = host or settings.output.web_host
    # An explicit 0 asks for an OS-chosen port, so only an absent flag falls
    # back to the configured port.
    actual_port = port if port is not None else settings.output.web_port

    # Bound before SANE is initialised, so a taken port fails at once and
    # leaves no initialised backend that no lifespan would ever close.
    sockets = _bind_listening_sockets(actual_host, actual_port)
    try:
        # serve scans nothing itself, so SANE failing to initialise is a
        # failure to start, ExitCode.CONFIG, not a failed scan.
        try:
            scanner = SaneBackend(host=settings.scanner.host)
        except ScanError as exc:
            msg = f"The web server could not start: {exc}"
            raise ConfigError(msg, next_step=SERVE_SANE_START_NEXT_STEP) from exc
        # Once the lifespan has started it owns the backend: it closes it after
        # the worker stops, or leaves it open on purpose when a thread is
        # stuck inside SANE. Until then serve owns it, and this stack closes it
        # if the app cannot be built or the server never starts.
        with contextlib.ExitStack() as unowned:
            unowned.callback(scanner.close)
            # create_app closes the job store and Paperless client itself if
            # it raises, and a lifespan whose start-up raises closes them too.
            app = create_app(settings, scanner)
            try:
                run_server(app, sockets, settings.output.log_level)
            except BaseException:
                # Read so that it cannot raise: an error here would replace
                # the one that ended the run.
                found = getattr(getattr(app, "state", None), "services", None)
                if isinstance(found, Services) and found.lifecycle.started:
                    unowned.pop_all()
                raise
            # A normal return means the lifespan ran and owned the backend.
            unowned.pop_all()
    finally:
        for sock in sockets:
            sock.close()


def _echo_write_result(
    result: ProfileWriteResult, profiles: dict[str, ProfileConfig]
) -> None:
    """
    Print what ``auto-profiles`` did to the config file, grouped by action.

    The group lines come from ``ProfileWriteResult.groups``, the same
    vocabulary the worker's startup log uses.
    """
    path = result.path.resolve()
    groups = result.groups()
    if not groups:
        click.echo(f"No changes to {path}.")
        return
    click.echo(f"Profiles in {path}:")
    for line, written in groups:
        click.echo(line)
        for name in written:
            p = profiles[name]
            # A profile generated for a device with no source option leaves
            # ``source`` unset, and the file names none; the model's Flatbed
            # fallback would claim a platen the device never reported.
            source = (
                p.source
                if "source" in p.model_fields_set
                else "(none; the scanner's own)"
            )
            click.echo(
                f"  {name}: source={source}, resolution={p.resolution}, mode={p.mode}"
            )


def _note_root_owned_config(path: Path, system: Path) -> None:
    """
    Say on stderr when a config file this command created belongs to root.

    A new file takes its directory's owner and mode 0600, so saneless running
    as an ordinary user could not read a root-owned system file.  Root's
    per-user file is under its own home, which no other user searches, so
    that note gives other moves than a ``chown``.  A status that cannot be
    read is not worth failing a write that succeeded.
    """
    try:
        owner = path.stat().st_uid
    except OSError:
        return
    if owner != 0:
        return
    if path == system:
        click.echo(root_owned_config_note(path), err=True)
    else:
        click.echo(root_per_user_config_note(path, system), err=True)


def _new_config_target(settings: Settings) -> Path:
    """
    Choose where ``auto-profiles`` creates a config file when none was loaded.

    Never ``./``: the search stops at the first file, so a new file there
    would shadow ``/etc/saneless`` on the next start.  The system file is the
    target when its directory already exists and is writable, since saneless
    never creates a system directory; otherwise the per-user XDG file is.
    The candidates are the ones the load searched, so they cannot drift.
    """
    system, user = _new_config_candidates(settings)
    if system.parent.is_dir() and os.access(system.parent, os.W_OK):
        return system.absolute()
    return user.absolute()


def _new_config_candidates(settings: Settings) -> tuple[Path, Path]:
    """
    Return the system and per-user config files ``auto-profiles`` may create.

    They are the last and the second searched candidates, from the recorded
    search or else the search list.
    """
    discovery = settings.config_discovery
    searched = discovery.searched if discovery is not None else ()
    candidates = searched if len(searched) > 1 else config_search_paths()
    return candidates[-1], candidates[1]


def _passed_over_system_dir(settings: Settings) -> str:
    """
    Say why the system config directory was not the target, if it exists.

    ``os.access`` refuses a read-only mount and a directory owned by someone
    else alike, and their fixes differ, so when the per-user write then
    fails its message names the passed-over directory and which case it is.
    Returns an aside to append, or "" when the directory is absent or
    writable.
    """
    directory = _new_config_candidates(settings)[0].parent.absolute()
    if not directory.is_dir() or os.access(directory, os.W_OK):
        return ""
    if is_read_only_mount(directory):
        return f" ({directory} exists but is mounted read-only)"
    return f" ({directory} exists but is not writable by this user)"


@cli.command(name="auto-profiles")
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Refresh the generated keys of auto-generated profiles (hand-written "
        "profiles are never touched)."
    ),
)
@click.pass_context
def auto_profiles(ctx: click.Context, *, force: bool) -> None:
    """Generate scan profiles from scanner capabilities."""
    require_sane()
    # The refusal below is the warning's own sentence, so it is said once.
    settings = _load_cli_settings(ctx, warn_stale=False)
    if config_file_state(settings) is ConfigFileState.STALE_ONLY:
        # A new saneless.toml would stop the next search before the old-name
        # file, burying its URL and token and turning the red rename row amber
        # while the appliance ran on defaults. Refusing costs only a rename.
        row = configuration_check(settings, absolute_paths=True)
        msg = f"{row.message} {row.next_step}"
        # The row's next step says to restart saneless; here the command is
        # run again instead.
        raise ConfigError(
            msg,
            next_step=(
                f"Rename the {LEGACY_CONFIG_FILENAME} named above to "
                f"{CONFIG_FILENAME}, then run saneless auto-profiles again."
            ),
        )
    _warn_stale_config(settings)

    scanner = SaneBackend(host=settings.scanner.host)
    ctx.call_on_close(scanner.close)
    device_list = scanner.get_devices()
    if not device_list:
        # The same scanner failure `scan` reports for the same finding.
        msg = (
            "No scanner found: auto-detection found no devices. "
            "Check what SANE can see with `saneless devices`"
        )
        raise NoScannerFoundError(msg)

    # A discovered device is pinned, so later scans do not follow whichever
    # scanner SANE lists first; a configured one is not written again.
    pin = None if settings.scanner.device else device_list[0].name
    device_id = settings.scanner.device or device_list[0].name
    caps = scanner.get_capabilities(device_id)
    profiles = generate_profiles(caps, device_type_of(device_list, device_id))

    # Chosen before the write, so a failure below can always name it.
    config_path = settings.config_path or _new_config_target(settings)
    # A rewrite keeps the file's owner, so only a created file can need the
    # root-owned note.
    created = not config_path.exists()
    # An OSError, such as a per-user directory a container without HOME
    # cannot create, becomes a ConfigError naming the target.
    try:
        # Only ever the XDG ``saneless`` directory, since the system target's
        # exists by construction; made private because the file may later hold
        # the Paperless token.
        if settings.config_path is None and not config_path.parent.is_dir():
            make_config_directory(config_path.parent)
        result = write_profiles_to_config(
            config_path, profiles, force=force, device=pin
        )
    except OSError as exc:
        passed_over = (
            _passed_over_system_dir(settings) if settings.config_path is None else ""
        )
        msg = f"Cannot write {config_path}: {exc.strerror or exc}{passed_over}"
        raise ConfigError(msg, next_step=CONFIG_WRITE_NEXT_STEP) from exc
    _echo_write_result(result, profiles)
    if created:
        _note_root_owned_config(
            config_path, _new_config_candidates(settings)[0].absolute()
        )


def _state_marker(state: CheckState) -> str:
    """
    Return the bracketed token ``doctor`` prints in front of one check row.

    Not ``check_state_label``, which is a word for a screen reader: these are
    equal-width tokens, so the eye finds the red rows in the left margin.  A
    total ``match``, so a new ``CheckState`` stops this type-checking until
    somebody decides what it looks like.
    """
    match state:
        case CheckState.OK:
            marker = "[ OK ]"
        case CheckState.WARN:
            marker = "[WARN]"
        case CheckState.FAIL:
            marker = "[FAIL]"
        case _:
            assert_never(state)
    return marker


# The token a row nobody probed prints instead of `[ OK ]`. `doctor` never
# skips the scanner today, but one check registry feeds both surfaces, so the
# CLI renders every row the registry can build. Six characters, like the state
# tokens, so it does not widen the name column.
_SKIPPED_MARKER: Final = "[SKIP]"


def _row_marker(result: CheckResult) -> str:
    """
    Return the token ``doctor`` prints in front of one finished row.

    A skipped row carries ``CheckState.OK`` so a health gate does not go red
    for a probe deliberately not taken, so marking it from the state alone
    would print "fine" before a sentence saying nothing was checked.
    ``checks.check_row_class`` and its siblings make the same substitution.
    """
    if result.skipped:
        return _SKIPPED_MARKER
    return _state_marker(result.state)


# The column widths `saneless doctor` renders with, derived so a longer CheckKey
# name or a wider marker cannot overflow the columns unnoticed. The indent puts
# a next step under the message it belongs to.
_MARKER_WIDTH = max(
    len(marker)
    for marker in (*(_state_marker(state) for state in CheckState), _SKIPPED_MARKER)
)
_NAME_COL_WIDTH = max(len(check_name(key)) for key in CheckKey)
_NEXT_STEP_INDENT = " " * (_MARKER_WIDTH + 1 + _NAME_COL_WIDTH + 1)

# The config resolution table's caption and its verdicts. "ignored" and
# "leftover" are both an old-name file: with nothing loaded it is the reason,
# with something loaded it is merely still there.
_RESOLUTION_CAPTION: Final = "Config files searched, in order:"
_RESOLUTION_LABELS: Final = (
    "used",
    "not used",
    "same file",
    "not found",
    "ignored",
    "leftover",
)
_RESOLUTION_LABEL_WIDTH: Final = max(len(label) for label in _RESOLUTION_LABELS)
# Printed instead of an empty table, which would read as output that failed
# halfway rather than as "this process ran no search".
_RESOLUTION_NONE: Final = "  none recorded"


def _resolution_line(label: str, path: Path, note: str = "") -> str:
    """Render one entry of the config resolution table, paths in one column."""
    return f"  {label:<{_RESOLUTION_LABEL_WIDTH}}  {absolute_or_as_spelled(path)}{note}"


def _config_resolution_lines(settings: Settings) -> list[str]:
    """
    Say where every configuration file the search looked at ended up.

    Absolute paths, which the LAN-visible status strip may not carry.  The
    recorded search is read and nothing is stat-ed again, so the table
    explains the settings this process holds even if a file changed since.
    Empty when no search was recorded.
    """
    discovery = settings.config_discovery
    if discovery is None:
        if settings.config_path is None:
            return []
        return [_resolution_line("used", settings.config_path)]
    if discovery.explicit is not None:
        return [
            _resolution_line(
                "used", discovery.explicit, " (given with --config; no search)"
            )
        ]
    lines = []
    listed: set[Path] = set()
    for candidate in discovery.searched:
        # A file reached through a second candidate is "same file": never "not
        # used" or "not found", which would claim a second file. Two candidates
        # can even be spelled alike (./saneless.toml run from the XDG directory).
        if candidate in listed or (
            candidate in discovery.duplicates and candidate not in discovery.found
        ):
            lines.append(_resolution_line("same file", candidate, " (already listed)"))
        elif candidate == discovery.loaded:
            lines.append(_resolution_line("used", candidate))
        elif candidate in discovery.found:
            lines.append(
                _resolution_line("not used", candidate, " (an earlier file won)")
            )
        elif not candidate.is_absolute():
            # Recorded as spelled only when the directory it is relative to
            # had been removed, which is why nothing could be there.
            lines.append(
                _resolution_line(
                    "not found",
                    candidate,
                    " (the working directory no longer exists)",
                )
            )
        else:
            lines.append(_resolution_line("not found", candidate))
        listed.add(candidate)
    # The same distinction the Configuration row draws.
    if discovery.loaded is None:
        label, note = "ignored", f" (old name; rename it to {CONFIG_FILENAME})"
    else:
        label, note = "leftover", " (old name; ignored)"
    lines.extend(_resolution_line(label, stale, note) for stale in discovery.stale)
    return lines


def _echo_config_resolution(settings: Settings) -> None:
    """Print the config resolution table under the check rows."""
    click.echo("")
    click.echo(_RESOLUTION_CAPTION)
    for line in _config_resolution_lines(settings) or [_RESOLUTION_NONE]:
        click.echo(line)


def _doctor_scanner(
    settings: Settings,
) -> tuple[ScannerBackend | None, ScannerRefusal | None]:
    """
    Build a scanner backend for one ``doctor`` run, or say why there is none.

    Catching the failure lets ``doctor`` report a machine without scanner
    support instead of refusing to run on it.  ``ImportError`` and the
    ``ConfigError`` ``require_sane`` translates it into both mean python-sane
    is not installed.  ``ScanError`` is ``sane.init()`` refusing, which
    reinstalling does not fix, so it is its own row and its reason is logged
    at WARNING for the row to point at.
    """
    try:
        return SaneBackend(host=settings.scanner.host), None
    except (ImportError, ConfigError) as exc:
        # The type name only: neither message adds to "not installed".
        logger.info("Scanner support unavailable: %s", type(exc).__name__)
        return None, ScannerRefusal.NOT_INSTALLED
    except ScanError as exc:
        logger.warning("Scanner support could not be started: %s", describe(exc))
        return None, ScannerRefusal.START_FAILED


def _doctor_paperless(
    settings: Settings,
) -> tuple[PaperlessClient | None, PaperlessRefusal | None]:
    """
    Build a Paperless client for one ``doctor`` run, or say why there is none.

    The constructor's refusals -- a bad URL or token, an unreadable TLS trust
    store -- become the Paperless row rather than reach the guard, which
    would cost the other rows.  The trust store is caught first, because it
    is a ``PaperlessError`` too but its remedy is ``SSL_CERT_FILE`` or
    ``SSL_CERT_DIR``, not a setting.  The logged message carries no secret:
    the constructor strips credentials from the URL and never quotes the
    token.
    """
    try:
        client = PaperlessClient(
            settings.paperless.url,
            settings.paperless.token.get_secret_value(),
            settings.paperless.consume_dir,
        )
    except PaperlessTrustStoreError as exc:
        logger.warning("Paperless client unavailable: %s", describe(exc))
        return None, PaperlessRefusal.TRUST_STORE
    except PaperlessError as exc:
        logger.warning("Paperless client unavailable: %s", describe(exc))
        return None, PaperlessRefusal.CONFIGURATION
    return client, None


# doctor deliberately does not call require_sane(): a machine with no
# python-sane is the one whose owner most needs the other rows, so
# _doctor_scanner renders the missing module as one FAIL row. The resolution
# table is printed on every run, so a failing machine's can be compared with a
# working one's. There is no --json: the rows, the table and the exit code are
# the whole contract, and a JSON mode would be a wire contract with no reader.
@cli.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Check that saneless is ready to scan."""
    # No directory gate: an unusable folder is a red row, not a refusal.
    settings = _load_cli_settings(ctx, validate_dirs=False)
    scanner, scanner_refusal = _doctor_scanner(settings)
    if scanner is not None:
        # Registered before the first SANE call, so a failing check still
        # leaves SANE shut down.
        ctx.call_on_close(scanner.close)
    paperless, paperless_refusal = _doctor_paperless(settings)
    try:
        results = run_checks(
            CheckContext(
                settings=settings,
                scanner=scanner,
                paperless=paperless,
                paperless_refusal=paperless_refusal,
                scanner_refusal=scanner_refusal,
                # The status strip calls the same function, which keeps the
                # two surfaces on one Profiles row.
                profile_storage=profile_storage_for_loaded(settings),
            )
        )
    finally:
        if paperless is not None:
            paperless.close()

    for result in results:
        click.echo(
            f"{_row_marker(result):<{_MARKER_WIDTH}} "
            f"{check_name(result.key):<{_NAME_COL_WIDTH}} {result.message}"
        )
        if result.terminal_detail:
            # Terminal-only and upstream-derived, such as where paperless-ngx
            # redirected to, so its control characters are escaped.
            detail = neutralise_controls(result.terminal_detail)
            click.echo(f"{_NEXT_STEP_INDENT}{detail}")
        if result.next_step:
            # Spelled for a terminal, which has no Check again button.
            step = render_check_step(result.next_step, CheckSurface.DOCTOR)
            click.echo(f"{_NEXT_STEP_INDENT}{step}")

    _echo_config_resolution(settings)

    # Any failing check is ExitCode.CONFIG, "fix your setup", which is what
    # every red row says; one process has one code, so no red row is singled
    # out. A WARN is not a failure: a gate that goes red for tidiness gets
    # ignored.
    if worst_state(results) is CheckState.FAIL:
        ctx.exit(ExitCode.CONFIG)
