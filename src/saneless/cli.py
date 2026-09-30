"""
The saneless command-line interface: one click group and its six commands.

``scan`` runs the scan-to-upload pipeline, ``devices`` lists the scanners
SANE can see, ``jobs`` prints the job history, ``serve`` starts the web
server, ``auto-profiles`` writes scan profiles from a scanner's capabilities,
and ``doctor`` runs the readiness checks. Every command runs inside one guard
that turns a failure into a single stderr line and a documented exit code.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import signal
import socket
import sys
import termios
import threading
import traceback
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Final, assert_never
from uuid import uuid4

import click
import uvicorn

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
    check_name,
    configuration_check,
    leftover_config_check,
    run_checks,
    worst_state,
)
from .config import (
    CONFIG_FILENAME,
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
    SanelessError,
    ScanCancelledError,
    ScanError,
    ScanInterrupted,
    StorageError,
    describe,
    failure_text,
    note_text,
)
from .job import CLI_JOBS_DEFAULT_LIMIT, JobStore
from .logging_config import configure_logging
from .paperless import PaperlessClient
from .pipeline import (
    AnswerSlot,
    FlipAnswerSlot,
    FlipCoordinator,
    PassCoordinator,
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
    FALLBACK_NOT_UPLOADED_LINE,
    MULTI_PAGE_NEEDS_TERMINAL,
    MULTI_PAGE_OPTION_HELP,
    NOTHING_TO_FINISH,
    TITLE_MAX_LENGTH,
    UNCONFIRMED_FILING_LABEL,
    UNCONFIRMED_SEND_LABEL,
    WARNED_UPLOAD_LABEL,
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
    multi_page_manual_duplex_refusal,
    outcome_line,
    progress_label,
    rejection_message,
    removed_pages_note,
    root_owned_config_note,
    root_per_user_config_note,
    state_label,
)
from .web.app import create_app
from .workspace import sweep_orphans

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import FrameType
    from typing import TextIO

    from fastapi import FastAPI

    from .auto_profiles import ProfileWriteResult
    from .config import ProfileConfig
    from .scanner.base import DeviceCapabilities, DeviceInfo, ScannerBackend
    from .vocabulary import PassPrompt

__all__ = ["ClickFlipCoordinator", "ClickPassCoordinator", "cli"]

logger = logging.getLogger(__name__)

# How many not-yet-accepted connections the web server's socket queues. This
# is the backlog uvicorn itself listens with.
_LISTEN_BACKLOG: Final = 2048

# Width of the Status column in `saneless jobs`, derived rather than written
# down: the humanised labels are longer than the raw enum values they replaced,
# and a new JobState member must not be able to overflow an 80-column
# terminal without anyone noticing. A DONE row that carries a warning is
# labelled from the state and the warning together rather than by a state of
# its own, so its label joins the max explicitly, and so do the labels of the
# two failures that may already be in paperless-ngx, which come from the
# category rather than the state.
_STATUS_COL_WIDTH = max(
    *(len(state_label(state)) for state in JobState),
    len(WARNED_UPLOAD_LABEL),
    len(UNCONFIRMED_SEND_LABEL),
    len(UNCONFIRMED_FILING_LABEL),
)

# Width of the Profile column in `saneless jobs`, and the floor the Title
# column never shrinks below. At 80 columns the Timestamp, Profile and Status
# columns and the three separators leave the Title exactly its floor; the
# Profile column gave up its fifteenth character so the widest status label,
# "Waiting: blank pages found", still fits without eating into the title.
_PROFILE_COL_WIDTH: Final = 14
_TITLE_COL_FLOOR: Final = 15

# The widest zone token ``%Z`` produces at a realistic offset: five characters,
# the ``+0545`` shape the tz database falls back to where there is no
# abbreviation. The only literal in the width below, and the one this host
# cannot demonstrate on its own.
_WIDEST_ZONE_TOKEN = len("+0545")

# Width of the Timestamp column in `saneless jobs`, derived from a rendered
# sample rather than written down, exactly as _STATUS_COL_WIDTH is. The date
# and time half is fixed-width; only the zone token this host happens to report
# varies, so the column reserves the widest realistic one instead of trusting
# the three letters most of the world sees. A host whose token is wider still
# wins the max(), so a zone abbreviation cannot silently truncate the column.
# Comes to 22, which is what ts_w was before local time arrived -- the reserve
# the seconds used to occupy is exactly the reserve the zone now needs.
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

# Why `scan` refuses when nobody configured paperless-ngx. A developer
# constant: it names the problem and never the value, the URL or the config
# path. Lower-cased and without a full stop because it is the tail of the CLI's
# `<what saneless was doing>: <problem>` error line, unlike checks.py's
# sentence for the same fact, which stands alone in a table row.
#
# The name carries no password-ish word on purpose: ruff's S105 reads the
# *name* of the target, not the value, so `_TOKEN_...` here would be flagged as
# a hardcoded credential. This is vocabulary.py's `_REJECTED_WIRE_VALUE` idiom
# rather than a suppression.
_UNSET_CREDENTIAL_PROBLEM = "the paperless-ngx API token has not been set"
# The same refusal for an empty paperless.url, which names the setting.
_UNSET_ADDRESS_PROBLEM = "the paperless-ngx address in paperless.url has not been set"


# Whether a human can answer a prompt here, behind a function of its own rather
# than written inline: CliRunner is genuinely not a terminal, so the non-TTY
# refusal test runs unpatched and the prompt test patches this one name.
# Written as a bare sys.stdin.isatty() in scan, one of the two could not exist.
def _stdin_is_interactive() -> bool:
    """Whether stdin is a terminal a human can answer a prompt on."""
    return sys.stdin.isatty()


# The signals a one-shot command turns into an interruption: a supervisor
# stopping it, or its terminal going away. Nobody at the keyboard chose to
# stop, so the pages already scanned are kept, not thrown away. SIGINT is not
# here: Ctrl-C is the operator's own cancel, which Python already raises as
# KeyboardInterrupt, and it keeps nothing.
_INTERRUPT_SIGNALS: Final = (signal.SIGTERM, signal.SIGHUP)

# A dropped SSH session delivers SIGHUP to the main thread and end of input to
# a terminal prompt's thread at nearly the same moment, in no promised order.
# End of input on its own is a cancel, which keeps nothing, so the prompt thread
# waits this long for the signal before it treats end of input as one: the
# signal wins, and the hangup keeps the pages scanned -- the fronts at the flip
# prompt, the accepted pages at a multi-page one.
_HANGUP_GRACE_SECONDS: Final = 0.25


class _Interruption:
    """
    Whether a SIGTERM or SIGHUP has reached the running command, and which.

    Set by the signal handler on the main thread and read by a terminal
    prompt's thread, which is why the signal itself is an ``Event`` and not a bare
    flag.  On the main thread that Event's lock is taken only inside the
    handler, and in ``clear``, which runs only while the handler is not
    installed.

    ``settled`` is the scan's ``PipelineRequest.settled``: the run sets it once
    its outcome is fixed, and the guard sets it as it starts reporting a
    failure.  From then on a signal is recorded and deferred rather than
    raised, because raising it could only undo what is already done -- abandon
    a failure's pages half way into ``failed/``, or turn a delivered document
    into an interruption.  The command finishes and exits with its own
    outcome's code.  It is a lock-free ``Settled``, not an ``Event``: it is
    set on the main thread, where the handler that reads it can interrupt the
    very call that sets it.
    """

    def __init__(self) -> None:
        """Start with no signal received and nothing settled."""
        self._received = threading.Event()
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
        self._received.set()

    def wait(self, timeout: float) -> bool:
        """
        Wait up to ``timeout`` seconds for a signal to arrive.

        Args:
            timeout: The longest to wait, in seconds.

        Returns:
            Whether a signal has arrived.

        """
        return self._received.wait(timeout)

    def clear(self) -> None:
        """Forget any signal, once the command that received it is over."""
        self.signum = None
        self.deferred = None
        self._received.clear()
        self.settled.clear()


_INTERRUPTION = _Interruption()


def _interrupt_handler(signum: int, _frame: FrameType | None) -> None:
    """
    Turn SIGTERM or SIGHUP into ``ScanInterrupted`` on the main thread.

    Both signals are ignored from here on, first of all, so a second one -- an
    impatient supervisor, or a hangup following a stop -- cannot abandon the
    preservation this one starts. The command's context puts the original
    handlers back when it closes.

    Once the command's outcome is settled (``_Interruption.settled``) the
    signal is only recorded: the command is already delivering its result or
    keeping a failure's pages, and it finishes that and exits as it would
    have.

    Args:
        signum: The signal that arrived.
        _frame: The interrupted frame; unused.

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

    A terminal that closes sends SIGHUP to the main thread and end of input to
    the prompt's thread at nearly the same moment, in no promised order.  The
    signal is an interruption, which keeps the pages scanned; end of input on
    its own is the operator's cancel, which keeps nothing.  So the prompt
    thread waits ``_HANGUP_GRACE_SECONDS`` for a signal first.  If one came,
    it claims nothing and leaves the calling thread to the ``ScanInterrupted``
    the signal raised there; otherwise it settles the cancel.  Every terminal
    prompt shares this, so a hangup means the same thing at each of them.

    Args:
        settle_abort: Claims the prompt's cancel answer.
        what: The prompt, as its log line names it.

    """
    if _INTERRUPTION.wait(_HANGUP_GRACE_SECONDS):
        logger.info(
            "%s reached end of input after a signal (%s); "
            "leaving the answer to the interruption",
            what,
            _INTERRUPTION.signum,
        )
        return
    settle_abort()


class ClickFlipCoordinator(FlipCoordinator):
    """
    The CLI flip coordinator: a terminal prompt with a bounded wait.

    ``click.confirm`` has no timeout of its own, so it runs on a daemon thread
    while the calling thread waits for at most ``timeout`` seconds.  Whichever
    of the operator's answer or the clock claims the single answer first is the
    answer -- claimed through ``FlipAnswerSlot``, the same slot the web worker's
    coordinator uses, so an answer that lands as the wait expires is honoured
    rather than overwritten.

    A yes is ``CONTINUED``; a no is ``ABORTED``; and so are EOF at the prompt
    (``click.Abort`` on the prompt thread) and Ctrl-C (``KeyboardInterrupt`` on
    the calling thread, where Python delivers SIGINT).  Those three are an
    operator's abort -- a cancel -- so giving up at the terminal and clicking
    Abort in the web UI end the job the same way.  The exception is end of
    input caused by a hangup: a terminal that closes also sends SIGHUP, which
    is an interruption, not a cancel, and keeps the fronts.  The two arrive at
    nearly the same moment on different threads, so on end of input the prompt
    thread waits ``_HANGUP_GRACE_SECONDS`` for a signal and, if one came,
    claims nothing and leaves the calling thread to the ``ScanInterrupted``
    the signal raised there.  A prompt that fails with a
    read error instead -- an I/O error from the terminal, undecodable input --
    also answers ``ABORTED`` at once, logged with its traceback, but it records
    the exception as ``abort_cause``: nobody chose to stop, so the scan is
    reported as failed (exit 1), not cancelled.

    Accepted cost, deliberate and not a leak: after a timeout the prompt thread
    is abandoned.  It keeps its read on stdin until the process exits, and its
    prompt may be left sitting on the terminal.  That is bounded -- the job has
    already failed and the CLI is on its way out -- and a daemon thread parked
    on stdin was measured not to delay interpreter shutdown.  It must stay a
    daemon thread: a non-daemon one would hold the interpreter open at exit
    waiting for an answer nobody is going to give.
    """

    def __init__(self) -> None:
        """Start unanswered, with no abort cause."""
        self._slot = FlipAnswerSlot()
        # Held across a broken prompt's claim and its cause, so the calling
        # thread, woken by that claim, cannot read the cause before it is set.
        self._cause_lock = threading.Lock()
        self._abort_cause: Exception | None = None

    @property
    def abort_cause(self) -> Exception | None:
        """
        The exception a broken prompt raised, if that failure claimed the answer.

        Returns:
            The prompt's exception, or ``None`` when the answer came from the
            operator (yes, no, EOF, Ctrl-C) or from the clock.

        """
        with self._cause_lock:
            return self._abort_cause

    def wait_for_flip(self, timeout: float) -> FlipOutcome:
        """
        Prompt the operator, and claim ``TIMED_OUT`` if ``timeout`` elapses first.

        Args:
            timeout: The longest to wait for an answer, in seconds.

        Returns:
            The one answer this coordinator resolved to.

        """
        prompt = threading.Thread(
            target=self._prompt, name="saneless-flip-prompt", daemon=True
        )
        prompt.start()
        try:
            self._slot.wait(timeout)
        except KeyboardInterrupt:
            # Ctrl-C lands here, not in click.confirm: Python handles SIGINT on
            # the main thread, which is this one, parked in the wait.  Offered
            # as ABORTED so it ends the job the way a web Abort does.
            return self._slot.settle(FlipOutcome.ABORTED)
        # One path for both endings.  If the prompt answered, settle finds that
        # answer already claimed and hands it back; if the wait expired,
        # TIMED_OUT is offered and wins unless the answer beat it after all.
        return self._slot.settle(FlipOutcome.TIMED_OUT)

    def _prompt(self) -> None:
        """Ask the operator on the prompt thread and claim their answer."""
        try:
            flipped = click.confirm(_FLIP_PROMPT, default=True)
        except click.Abort:
            # End of input.  If the terminal hung up, SIGHUP is on its way to
            # (or already at) the calling thread as ScanInterrupted, and
            # settling ABORTED here first would turn that interruption into a
            # cancel that keeps nothing.  So the signal gets a moment to win.
            _end_of_input(
                partial(self._slot.settle, FlipOutcome.ABORTED), "Flip prompt"
            )
            return
        except Exception as exc:
            # Anything else used to kill this thread silently, leaving the
            # calling thread waiting out the whole timeout and then reporting
            # that nobody confirmed the flip, which was false.  The prompt
            # broke, so the scan stops now: ABORTED rather than a fourth
            # outcome, with the exception kept as abort_cause so the pipeline
            # reports a failure, not a cancel.  The cause is set only if this
            # claim won: a Ctrl-C or a timeout that answered first keeps its own
            # meaning.  Logged before the claim, so the record exists once the
            # wait wakes.
            logger.exception("Flip prompt failed; treating it as an abort")
            with self._cause_lock:
                claimed = self._slot.offer(FlipOutcome.ABORTED)
                if claimed:
                    self._abort_cause = exc
            return
        self._slot.settle(FlipOutcome.CONTINUED if flipped else FlipOutcome.ABORTED)


def _flush_typed_ahead() -> None:
    """
    Throw away keys typed while a pass ran, so none answers the next question.

    A second ``n`` pressed during a pass sits in the terminal's input queue and
    would answer the next prompt the moment it appeared, starting a pass on a
    platen nobody has changed.  Only a terminal has such a queue; anything else
    -- a test's fake stdin, a stream with no descriptor, a terminal that went
    away -- is left alone, and the prompt reads it as it is.
    """
    try:
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except OSError, ValueError, termios.error:
        return


class _AbortConfirmation:
    """
    Whether the operator is confirming an abort, which the clock must wait out.

    The operator who typed ``a`` is at the terminal, answering "abort?".  If
    the wait ran out under them and the clock claimed its answer, the document
    would be finished and uploaded while they were confirming they wanted it
    thrown away.  So the prompt thread opens this before it asks, and the
    calling thread, its wait over, waits for it to close before it claims the
    timeout.  A Yes is left open until the abort has been claimed, so the
    calling thread can never find the confirmation over and the answer still
    unclaimed.
    """

    def __init__(self) -> None:
        """Start closed: no abort is being confirmed."""
        self._changed = threading.Condition()
        self._open = False

    def begin(self) -> None:
        """Mark an abort as being confirmed."""
        with self._changed:
            self._open = True

    def end(self) -> None:
        """Mark the confirmation over, and wake a waiter.  Idempotent."""
        with self._changed:
            self._open = False
            self._changed.notify_all()

    def wait_out(self, timeout: float) -> None:
        """
        Wait until no abort is being confirmed, for at most ``timeout`` seconds.

        Args:
            timeout: The longest to wait, in seconds.

        """
        with self._changed:
            self._changed.wait_for(lambda: not self._open, timeout)


def _read_pass_answer(
    prompt: PassPrompt, confirmation: _AbortConfirmation
) -> PassAnswer:
    """
    Ask one multi-page question at the terminal until it has an answer.

    Only the first character of a line counts, in either case, and only a
    letter the prompt lists is accepted.  Anything else is refused with the
    letters on offer, and ``f`` while nothing is kept says why there is
    nothing to finish; either way the question is asked again.  An abort is
    confirmed first, No by default, and a No asks the question again.

    Args:
        prompt: The open question.
        confirmation: Opened while an abort is being confirmed.  Closed again
            on a No; left open on a Yes, for the caller to close once the
            abort is claimed.

    Returns:
        The operator's answer, one of those ``prompt`` offers.

    """
    letters = {choice.letter: choice.answer for choice in cli_pass_choices(prompt)}

    def parse(text: str) -> PassAnswer:
        """
        Turn a typed line into the answer its first letter picks.

        Args:
            text: What the operator typed.

        Returns:
            The answer.

        Raises:
            click.BadParameter: The letter picks nothing this question offers;
                click prints the reason and asks again.

        """
        letter = text.strip().lower()[:1]
        answer = letters.get(letter)
        if answer is not None:
            return answer
        if letter == "f" and prompt.wait is not PassWait.BLANK_DECISION:
            raise click.BadParameter(NOTHING_TO_FINISH)
        raise click.BadParameter(cli_choice_hint(prompt))

    # A failed pass's error text comes from outside saneless -- a SANE status
    # string, a wrapped OS error -- so its control characters are shown as
    # escapes here, at the terminal, as on every other failure line.
    shown = prompt
    if prompt.error is not None:
        shown = replace(prompt, error=neutralise_controls(prompt.error))
    question = cli_pass_question(shown)
    _flush_typed_ahead()
    while True:
        answer = click.prompt(question, value_proc=parse, show_default=False)
        if answer is not PassAnswer.ABORT:
            return answer
        confirmation.begin()
        if click.confirm(abort_question(prompt.pages_kept), default=False):
            return answer
        confirmation.end()


class ClickPassCoordinator(PassCoordinator):
    """
    The CLI multi-page coordinator: a one-letter prompt with a bounded wait.

    Built as ``ClickFlipCoordinator`` is, for the same reasons.  ``click.prompt``
    has no timeout of its own, so it runs on a daemon thread while the calling
    thread waits for at most the prompt's ``timeout_seconds``, and the
    operator's answer and the clock race for one ``AnswerSlot``.  Every
    question gets a fresh slot, so an answer to one question can never be
    read as the answer to the next.

    The question lists only the letters it accepts, and keys typed during the
    pass are thrown away before it is shown (``_read_pass_answer``).  Ctrl-C
    is a cancel, with no confirmation however many pages are kept: SIGINT
    raises in the calling thread's wait, which answers ``ABORT``, and a page
    is only ever kept by choosing to finish.  End of input is a cancel too,
    unless a hangup caused it (``_end_of_input``).  A prompt that fails with a
    read error answers ``ABORT`` at once, logged with its traceback, and
    records the exception as ``abort_cause``: nobody chose to stop, so the
    scan is reported as failed, not cancelled, and its pages are kept.

    An abort being confirmed when the wait runs out holds the clock
    (``_AbortConfirmation``), for at most one more ``timeout_seconds``: a Yes
    then aborts, as the operator chose, rather than being overtaken by a
    finish that uploads the pages.  A No, or no answer within that bound --
    the confirmation's default -- is not an abort, and the expired wait
    resolves ``TIMED_OUT``.

    Accepted cost, as at the flip prompt, deliberate and not a leak: after a
    timeout the prompt thread is abandoned.  It keeps its read on stdin until
    the process exits, and its question may be left sitting on the terminal.
    A timeout always ends the document, so at most one thread is ever
    abandoned, and a daemon thread parked on stdin does not delay interpreter
    shutdown.  It must stay a daemon thread: a non-daemon one would hold the
    interpreter open at exit waiting for an answer nobody is going to give.
    """

    def __init__(self) -> None:
        """Start with no abort cause."""
        # Held across a broken prompt's claim and its cause, so the calling
        # thread, woken by that claim, cannot read the cause before it is set.
        self._cause_lock = threading.Lock()
        self._abort_cause: Exception | None = None

    @property
    def abort_cause(self) -> Exception | None:
        """
        The exception a broken prompt raised, if that failure claimed the answer.

        Returns:
            The prompt's exception, or ``None`` when the answer came from the
            operator (a letter, end of input, Ctrl-C) or from the clock.

        """
        with self._cause_lock:
            return self._abort_cause

    def ask(self, prompt: PassPrompt) -> PassAnswer:
        """
        Put ``prompt`` to the operator; claim ``TIMED_OUT`` if its wait expires.

        Args:
            prompt: The question, and the only answers it accepts.

        Returns:
            The one answer this question resolved to.

        """
        slot = AnswerSlot[PassAnswer]()
        confirmation = _AbortConfirmation()
        reader = threading.Thread(
            target=self._prompt,
            args=(prompt, slot, confirmation),
            name="saneless-multi-page-prompt",
            daemon=True,
        )
        reader.start()
        try:
            slot.wait(prompt.timeout_seconds)
            # The operator may be confirming an abort as the wait runs out;
            # the clock must not finish the document under them.  Returns at
            # once when nothing is being confirmed.
            confirmation.wait_out(prompt.timeout_seconds)
        except KeyboardInterrupt:
            # Ctrl-C lands here, not in click.prompt: Python handles SIGINT on
            # the main thread, which is this one, parked in the wait.  It is
            # the operator's cancel, unconfirmed, whatever has been kept.
            return slot.settle(PassAnswer.ABORT)
        # One path for both endings, as at the flip prompt: an answer already
        # claimed is handed back, and an expired wait claims TIMED_OUT.
        return slot.settle(PassAnswer.TIMED_OUT)

    def _prompt(
        self,
        prompt: PassPrompt,
        slot: AnswerSlot[PassAnswer],
        confirmation: _AbortConfirmation,
    ) -> None:
        """
        Ask the operator on the prompt thread, claim their answer, then let go.

        The abort confirmation is closed only once the answer is claimed,
        whatever the ending, so the calling thread never finds it over with
        nothing claimed.

        Args:
            prompt: The open question.
            slot: This question's slot.
            confirmation: Open while the operator confirms an abort.

        """
        try:
            self._claim(prompt, slot, confirmation)
        finally:
            confirmation.end()

    def _claim(
        self,
        prompt: PassPrompt,
        slot: AnswerSlot[PassAnswer],
        confirmation: _AbortConfirmation,
    ) -> None:
        """
        Ask the operator and claim their answer, or the prompt's failure.

        Args:
            prompt: The open question.
            slot: This question's slot.
            confirmation: Passed to ``_read_pass_answer``.

        """
        try:
            answer = _read_pass_answer(prompt, confirmation)
        except click.Abort:
            _end_of_input(partial(slot.settle, PassAnswer.ABORT), "Multi-page prompt")
            return
        except Exception as exc:
            # As at the flip prompt: a prompt that broke ends the wait now as
            # ABORT, with the exception kept as abort_cause so the run reports
            # a failure that keeps its pages rather than a cancel.  The cause
            # is set only if this claim won, and the failure is logged before
            # the claim, so the record exists once the wait wakes.
            logger.exception("Multi-page prompt failed; treating it as an abort")
            with self._cause_lock:
                if slot.offer(PassAnswer.ABORT):
                    self._abort_cause = exc
            return
        slot.settle(answer)


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
code. Held under a name so the guard's clause is
one short, parenthesis-free ``except``: the multi-type spelling ruff formats to
(PEP 758) does not parse on the older interpreter the pre-commit AST hooks run.
"""


_VERBOSE_HINT = "Run again with -v to see the traceback"
"""The hint an unexpected error's line ends with when no traceback was kept."""


def _logging_ready(ctx: click.Context) -> bool:
    """
    Whether ``_load_cli_settings`` has configured logging for this process.

    Before that, an ERROR record has no handler but ``logging.lastResort``,
    which would print it -- traceback and all -- straight to stderr, so the
    guard must not log at all.

    Args:
        ctx: The group's context; its ``obj`` is shared with the subcommand's.

    Returns:
        True once logging is configured.

    """
    obj = ctx.obj
    return isinstance(obj, dict) and bool(obj.get("logging_configured"))


def _unexpected_line(exc: BaseException) -> str:
    """
    Render the one line an exception that is not a saneless type is reported as.

    Args:
        exc: The exception to report.

    Returns:
        ``Unexpected error (<Type>): <message>``, with no trailing hint.  The
        message is ``failure_text``'s, so any note on the exception is part of
        it.

    """
    return f"Unexpected error ({type(exc).__name__}): {failure_text(exc)}"


def _failure_line(exc: SanelessError, category: ErrorCategory) -> str:
    """
    Render the one line a classified saneless failure is reported as.

    The prefixes are documented (``docs/how-to/set-up-adf-duplex.md`` quotes
    them), so they are kept as they were before the guard existed. A
    configuration error is printed as-is: the loader's renderer already wrote
    its own ``Configuration error in <file>:`` header, and escapes the names
    in its own lines.  Every other line has its control characters shown as
    escapes, because its text can carry something from outside saneless,
    such as a device name that LAN discovery reported.

    Every message is rendered through ``failure_text`` rather than ``str``, so
    a note attached with ``add_note`` -- where a failed scan's pages were
    kept, for one -- is on the line too.  The configuration arm keeps the
    loader's text as written and appends only the notes.

    Args:
        exc: The failure.
        category: What ``classify_error`` made of it.

    Returns:
        The line to print on stderr.

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


def _echo_err(text: str, *, nl: bool = True) -> None:
    """
    Write one line of a command's report to stderr, surviving a dead terminal.

    The guard, and a scan's closing lines, report how a command ended, and the
    exit code is the report a script reads.  A terminal that has gone away --
    the dropped SSH session behind a SIGHUP -- answers every write with EIO,
    and an ``OSError`` raised here would escape as a traceback, or reach the
    guard as an unexpected error, instead of the code the command chose.  So a
    failed write is logged here, and ``drain_dead_streams`` discards what it
    left behind before the interpreter exits, so the exit code still stands.

    Args:
        text: The line to write.
        nl: Whether to end it with a newline.

    """
    try:
        click.echo(text, err=True, nl=nl)
    except OSError:
        logger.info("Could not write to stderr: %r", text, exc_info=True)


def _echo_out(text: str) -> None:
    """
    Write one line of a scan's report to stdout, surviving a dead terminal.

    The stdout twin of ``_echo_err``.  A hangup after delivery is deferred, so
    the scan goes on to print its outcome to a terminal that is already gone;
    the document is in paperless-ngx by then, and the exit code chosen from
    the result must stand rather than become exit 5.

    Args:
        text: The line to write.

    """
    try:
        click.echo(text)
    except OSError:
        logger.info("Could not write to stdout: %r", text, exc_info=True)


def _drain_dead_stream(stream: TextIO | None) -> None:
    """
    Discard what a standard stream can no longer write, if it cannot.

    Args:
        stream: ``sys.stdout`` or ``sys.stderr``; ``None`` when there is none.

    """
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

    A write that fails on a terminal that has gone away -- a hangup, or a
    closed pipe -- leaves its bytes in the stream's buffer, and catching the
    ``OSError`` does not remove them.  The interpreter flushes both streams
    once more as it shuts down, that flush fails the same way, and CPython
    then replaces the exit code the command chose with 120: a delivered scan
    would stop exiting 0 and a hangup would stop exiting 129.  So each stream
    that still cannot be flushed has its descriptor pointed at ``/dev/null``,
    where the unwritten bytes, and anything written after them, go without
    error.  A stream that flushes normally is left untouched.

    It is called once, as the console entry point returns, so it covers every
    write the command made: the ``_echo_out`` and ``_echo_err`` lines, and the
    progress lines whose failure the pipeline only logs.
    """
    _drain_dead_stream(sys.stdout)
    _drain_dead_stream(sys.stderr)


def _log_failure(ctx: click.Context, exc: Exception) -> None:
    """
    Log a failure with its traceback, but only once logging is configured.

    The message names the failure too: when the log fell back to stderr the
    traceback is not rendered there, and the record must still say what went
    wrong.  It is quoted with ``%r``, because it can carry text from outside
    saneless and repr shows a control character in it as its escape.

    Args:
        ctx: The group's context.
        exc: The failure to log.

    """
    if _logging_ready(ctx):
        logger.error(
            "saneless %s failed: %r",
            ctx.invoked_subcommand,
            failure_text(exc),
            exc_info=exc,
        )


def _report_unexpected(ctx: click.Context, exc: Exception) -> None:
    """
    Report an exception that is not a saneless type as one stderr line.

    Once logging is configured the traceback goes to the log, and the line
    points at the log file only when the file handler really attached
    (``configure_logging``'s return value): a stderr fallback must not be called
    a log file. That fallback never renders a traceback, so without ``-v`` the
    line then says how to get one. Before logging is configured nothing is
    logged; ``-v`` prints the traceback to stderr instead, and without it the
    line says how to get one.

    ``serve`` has neither a log file nor a traceback-free sink: its stream
    renders the traceback the ``logger.error`` above just emitted, with or
    without ``-v``. The hint is suppressed there rather than telling an operator
    to restart a running service to see something already printed directly above
    the line.

    Args:
        ctx: The group's context.
        exc: The exception to report.

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

    Every failure a command raises becomes one stderr message and its
    ``ExitCode``, and no user ever sees a traceback unless they asked for one
    with ``-v``. The ``except`` clauses are ordered, and the order is the
    design:

    1. click's ``Exit``, ``Abort`` and ``ClickException`` are re-raised first.
       ``Exit`` and ``Abort`` subclass ``RuntimeError``, so a later
       ``except Exception`` would turn ``--help`` into exit 5.
    2. ``ScanInterrupted`` -- a SIGHUP or SIGTERM to the command -- is an
       interruption, not a cancel: nobody chose to stop, so the pages already
       scanned were kept. One ``Interrupted:`` line and 128 plus the signal
       number, 129 or 143. It is a ``BaseException``, so no later clause
       would catch it. A signal that arrives once the outcome is settled --
       delivered, or failed and being kept, or being reported here -- is
       deferred instead, and the command exits with its own outcome's code.
    3. ``KeyboardInterrupt`` and ``ScanCancelledError`` are a cancel, not a
       failure: one line and exit 130.
    4. ``StorageError`` -- a job database saneless cannot use -- is a setup
       problem and exits 2. It is mapped here by type and sits before the
       ``SanelessError`` clause because ``ErrorCategory`` is persisted on job
       records, and ``classify_error`` deliberately keeps ``StorageError``
       ``UNKNOWN`` rather than growing a category for it. Its message already
       names the database path and the reason.
    5. Any other ``SanelessError`` is classified once, by the same
       ``classify_error`` the web worker uses, so the CLI's exit code and the
       job's category cannot disagree.
    6. Anything else is not a saneless type: ``Unexpected error (<Type>)``,
       exit 5, the traceback in the log.
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
            ctx.exit(ExitCode.CONFIG)
        except SanelessError as exc:
            category = classify_error(exc)
            code = exit_code_for(category)
            if code is ExitCode.UNEXPECTED:
                _report_unexpected(ctx, exc)
            else:
                _log_failure(ctx, exc)
                # Two lines, and the first one is not ours to change. Line 1
                # keeps its documented `<what saneless was doing>: <problem>`
                # shape, printed unchanged, so a script parsing it and the
                # doc-truth message-shape tests that pin it are both unaffected.
                # Line 2 is the same error_next_step string the web error page
                # renders -- which is why the copy is surface-neutral and never
                # says "press Scan" or "run the command". The UNEXPECTED branch
                # above gets no advice: its category is a guess about an
                # exception saneless did not raise.
                _echo_err(_failure_line(exc, category))
                _echo_err(f"Try: {error_next_step(category)}")
            ctx.exit(code)
        except Exception as exc:
            _report_unexpected(ctx, exc)
            ctx.exit(ExitCode.UNEXPECTED)


@click.group(cls=_GuardedGroup)
# package_name makes click read the version from importlib.metadata, so
# pyproject.toml stays its single source. No custom message is passed: the
# default "%(prog)s, version %(version)s" is the whole contract, because
# `saneless doctor` already reports the Python and platform detail a longer
# block would duplicate. No short flag either -- `-v` below is already --verbose
# on this group, and click would bind it to whichever option declared it last,
# silently.
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
    # Click runs this callback before a subcommand parses its own --help, and
    # ctx.resilient_parsing is False there, so nothing may be loaded here: a
    # broken config would otherwise break `saneless serve --help`.
    # Each command loads through _load_cli_settings instead.
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["verbose"] = verbose
    # SIGTERM and SIGHUP become ScanInterrupted, which keeps the pages already
    # scanned and exits 143 or 129 through the guard. Installing a handler
    # loads nothing, so `--help` stays free of side effects; this group's
    # context closes after the guard has chosen the exit code, which is when
    # the original handlers go back, so none leaks into a caller that invoked
    # the CLI in-process. serve is left to uvicorn, which installs its own
    # handlers for a graceful shutdown.
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

    Two situations.  A file under the old name is being ignored; or a second
    ``saneless.toml`` is sitting in a later searched directory, so the file an
    operator edits may not be the one saneless reads.  The startup log already
    says both, and on a service that is enough, because the records stream to
    the same terminal the operator is watching.  A one-shot command writes its
    log to ``log_file`` and is read afterwards if at all, so the person who
    just ran ``saneless scan`` and got the defaults, or the other file's
    settings, sees nothing at all -- which is the failure this phase exists to
    remove, in miniature.

    The words are not written here.  ``configuration_check`` holds the one
    copy of them, and the terminal spelling is the same sentence with the file
    named by its absolute path, which a terminal may carry and the LAN-visible
    status strip may not.  The row can show only one situation, and a shadowed
    file outranks an old-name leftover there; the terminal has room for both,
    and the leftover may hold the only copy of the Paperless URL and token, so
    it gets its own line from ``leftover_config_check``.

    Args:
        settings: The settings in hand, carrying the search that built them.

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
    ctx: click.Context, *, stream_logs: bool = False, warn_stale: bool = True
) -> Settings:
    """
    Load and validate settings and configure logging, once per process.

    Called by every command on first need rather than by the group callback, so
    ``--help`` never touches the configuration. Nothing is caught here: every
    failure reaches the group guard. Loading and directory validation raise
    ``ConfigError`` for every problem with the file -- including a TOML syntax
    error -- which the guard prints as rendered and exits 2; anything else is an
    unexpected error, exit 5. An unwritable ``log_file`` is not a failure:
    ``configure_logging`` warns on stderr, logs there instead, and the command
    runs. ``load_settings`` and ``configure_logging`` are called by their
    module-global names, which is where the tests patch them.

    Once logging is configured, ``ctx.obj`` records it (``logging_configured``)
    and records ``log_file`` only if the file handler really attached, so the
    guard logs failures and names the log file truthfully. In the streaming
    mode ``serve`` asks for, no file handler is attached at all, so
    ``ctx.obj["log_file"]`` is always None there and nothing ever offers
    "Full details in <log_file>" for a service that writes none.

    Args:
        ctx: The command's context; its ``obj`` carries ``config_path`` and
            ``verbose`` from the group, and caches the loaded settings.
        stream_logs: If True, configure the 12-factor service shape -- records
            stream to stderr and no log file is written, so ``docker logs`` or
            journald sees them and owns retention. Only ``serve`` passes it;
            every one-shot command keeps the rotating file handler unchanged.
        warn_stale: If False, do not print the superseded-name warning here.
            Only ``auto-profiles`` passes it: that command refuses outright in
            one of the two states, and the refusal and the warning are the
            same sentence, so it decides for itself which one is printed.

    Returns:
        The loaded settings, the same object on every call.

    """
    cached = ctx.obj.get("settings")
    if isinstance(cached, Settings):
        return cached

    settings = load_settings(ctx.obj.get("config_path"))
    validate_settings_dirs(settings)
    if not stream_logs:
        _make_log_home_private(settings)
    # A service is handed no log file at all: configure_logging then attaches
    # one stderr handler and returns False, so the line below records no
    # log_file with no special case of its own. The rotation settings still go
    # along and are simply unused -- they govern one-shot mode, which is what
    # the configuration reference says and why no warning fires here.
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
    # Which file and which environment keys, names only, once.
    log_config_sources(settings)
    # A service's log is already on stderr, so the line above has reached the
    # terminal and repeating it would be the same sentence twice per start.  A
    # one-shot command's log is a file nobody is watching, so for it this is
    # the only thing said in front of the person who ran it.
    if warn_stale and not stream_logs:
        _warn_stale_config(settings)

    ctx.obj["settings"] = settings
    return settings


def _make_log_home_private(settings: Settings) -> None:
    """
    Create ``data_dir`` owner-only before logging can create it with the umask.

    The default log file lives in ``data_dir``, so the first one-shot command
    on a new install -- often ``saneless doctor`` -- creates that directory
    while setting up logging, before anything that would create it privately.
    Every later call finds it existing and leaves its mode alone, so it has to
    come out 0700 here. ``configure_logging`` makes only the log file's own
    directory private; this covers a log file nested deeper inside
    ``data_dir``. A log file elsewhere leaves ``data_dir`` to the commands
    that use it.

    A failure is left to ``configure_logging``, which meets the same error
    creating the log directory and falls back to stderr with a warning
    naming the log file.

    Args:
        settings: The loaded settings.

    """
    output = settings.output
    if not output.log_file.is_relative_to(output.data_dir):
        return
    with contextlib.suppress(OSError):
        make_private_dir(output.data_dir)


def _recover_orphaned_workspaces(settings: Settings) -> None:
    """
    Keep the pages a killed scan left in ``tmp_dir``, before this scan starts.

    A CLI-only install has no server whose startup would recover a scan that
    was SIGKILLed, OOM-killed or cut off by a power failure, so each
    ``saneless scan`` sweeps first. Only workspaces whose lock is free are
    taken, so a scan still running -- another ``saneless scan``, or a server
    sharing this ``tmp_dir`` -- is never touched. A CLI scan has no job row:
    the sweep's WARNING, naming where the pages went, is its whole report.

    The sweep is housekeeping and never decides whether the scan runs: any
    failure is logged with its traceback and the scan goes ahead.

    Args:
        settings: The loaded settings; ``tmp_dir``, ``failed_dir`` and
            ``min_free_space_mb`` are read.

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

    The rule is the one the web form shares: typed, else the profile's title,
    else "Scan <time>"; blank after stripping counts as not typed. A longer
    title than paperless-ngx keeps whole is refused before the scanner exists,
    never shortened. Only a typed title can be that long: a profile's title is
    held to the same cap when the config loads.

    Args:
        title: The ``--title`` value, possibly empty.
        profile: The chosen profile, for its title.
        now: The time a "Scan <time>" title names.

    Returns:
        The resolved title, exactly as typed or configured.

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
    # python-sane is mandatory: a command that needs it refuses before loading
    # config or touching the device, exit 2 through the guard. --help never
    # reaches this body, so it needs no python-sane.
    require_sane()
    settings = _load_cli_settings(ctx)
    # Before the scanner is opened, and before anything that can refuse the
    # scan: the pages a killed scan left behind are recovered whether or not
    # this one goes ahead.
    _recover_orphaned_workspaces(settings)

    if profile not in settings.profiles:
        click.echo(f"Unknown profile: {profile}", err=True)
        ctx.exit(ExitCode.CONFIG)

    now = datetime.now(tz=UTC)
    resolved_title = _scan_title(title, settings.profiles[profile], now=now)

    # Refused here, before the scanner is opened, because a scan that
    # cannot upload is wasted paper. The refusal is unconditional -- a
    # configured paperless.consume_dir fallback does not soften it, or `scan`,
    # `doctor` and the web UI would disagree about whether the appliance can
    # work. `devices`, `auto-profiles` and `jobs` are deliberately untouched:
    # none of them talks to paperless-ngx.
    #
    # This is the second place cli.py unwraps the token; the PaperlessClient
    # construction below is the other. The value goes to the predicate and
    # nowhere else -- it is never logged, echoed or interpolated into the
    # message, which is a developer constant (ASVS V7). The line is built in the
    # `<what saneless was doing>: <problem>` shape because ErrorCategory.CONFIG
    # prints the exception as-is (_failure_line), so the "what saneless was
    # doing" half has to be part of the message.
    #
    # An empty paperless.url is refused here for the same reason: the upload
    # would fail for certain as a configuration error, and no consume-folder
    # copy is made for it, so the stack would be fed for a PDF in failed/.
    unset = None
    if is_placeholder_token(settings.paperless.token.get_secret_value()):
        unset = _UNSET_CREDENTIAL_PROBLEM
    elif not settings.paperless.url:
        unset = _UNSET_ADDRESS_PROBLEM
    if unset is not None:
        msg = f"Scanning '{resolved_title}' with profile '{profile}': {unset}"
        raise ConfigError(msg)

    manual_duplex = settings.profiles[profile].duplex == "manual"
    # Both multi-page refusals come before the backend exists, so no paper
    # moves.  Manual duplex first: a terminal would not help there, so the
    # operator is told the reason that would.  Off a terminal nobody can say
    # whether there is another page, so cron or a pipe never drives the loop.
    if multi_page and manual_duplex:
        click.echo(multi_page_manual_duplex_refusal(profile), err=True)
        ctx.exit(ExitCode.CONFIG)
    if multi_page and not _stdin_is_interactive():
        click.echo(MULTI_PAGE_NEEDS_TERMINAL, err=True)
        ctx.exit(ExitCode.CONFIG)
    # Refused here, before the backend exists, so no paper moves: from cron or a
    # pipe there is nobody to flip the stack, and pass A would be wasted.
    if manual_duplex and not _stdin_is_interactive():
        click.echo(
            f"Profile '{profile}' is manual duplex, which needs an interactive "
            "terminal: saneless must prompt you to flip the stack between the "
            "two passes. Run it from a terminal, or scan from the web UI.",
            err=True,
        )
        ctx.exit(ExitCode.CONFIG)

    scanner = SaneBackend(host=settings.scanner.host)
    # click runs close callbacks when this command's Context leaves its `with`
    # block: on success, on ctx.exit(), and while an exception propagates --
    # all of them before the guarded group's error handlers choose an exit
    # code. So SANE is already down by the time the error line is printed, and
    # no early exit path can skip it.
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
            # A uuid4, exactly as the worker supplies for a web job.  Without
            # one, every CLI run composed {timestamp}-{title-slug}.pdf and
            # rested on the timestamp alone -- while build_pdf_filename's whole
            # collision argument is "uniqueness comes from the job id".  That is
            # not cosmetic: preservation never replaces a file in failed/, so
            # two same-second scans of the same title would share a name there,
            # leaving the later PDF under a numbered name that no longer says
            # which scan it was, and refusing the later page files outright.
            # Four kinds of artefact land there, and every mid-scan fault can
            # reach it.
            job_id=str(uuid4()),
            # The command line has no tag or correspondent option, so neither
            # control is answered and the profile's defaults apply -- the same
            # rule, through the same function, as a web form nobody touched.
            metadata=resolve_scan_metadata(
                settings.profiles[profile],
                tags=None,
                correspondent=None,
                correspondent_given=False,
            ),
            hooks=RequestHooks(
                status_callback=status_callback,
                # run_pipeline is synchronous, so the flip wait holds this
                # thread; only the click.confirm read itself moves to the
                # prompt thread.
                flip_coordinator=ClickFlipCoordinator() if manual_duplex else None,
                # The same holds for every multi-page question: the run waits
                # on this thread, and only each click.prompt read moves off it.
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
        # Failures reach the group guard, which prints one line and exits with
        # its ExitCode; the client is closed either way.
        paperless.close()

    # The outcome line, the stderr lines and the exit code are the CLI's whole
    # report of a scan: nothing is written to the job store. A document that was
    # delivered but degraded -- saved to the consume folder, or uploaded with a
    # warning -- exits 6 or 7, so a script can tell it from a clean success and
    # from a paperless failure (3), which it might retry by scanning again.
    #
    # Every closing line goes through a helper that survives a dead terminal:
    # a hangup after delivery is deferred, and the lines it leaves nowhere to
    # write must not replace that exit code with a crash.
    _echo_out(
        outcome_line(job_state_for(result.outcome), result.warning, resolved_title)
    )
    # Removed pages are not kept anywhere, so naming them is how the operator
    # learns which sheets to rescan if a real page was taken for a blank.  It
    # goes to stdout beside the outcome and leaves the exit code alone: it is
    # information about a success, not a warning.
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

    Extracted from ``devices`` so that rendering the resolution constraint in
    whichever shape the device gave it does not push that command past ruff's
    PLR0912 branch limit. The limit is respected rather than raised, and
    nothing is suppressed.

    Every line is printed only when there is something to put after its label.
    A label followed by nothing is the symptom the operator actually saw on a
    range-reporting device: it reads as "this scanner offers none",
    when the truth was that saneless had not read what the scanner offered.

    Args:
        caps: The capabilities to render.

    """
    if caps.sources:
        sources = ", ".join(neutralise_controls(s) for s in caps.sources)
        click.echo(f"  Sources: {sources}")
    if caps.resolutions:
        click.echo(f"  Resolutions: {', '.join(str(r) for r in caps.resolutions)}")
    elif caps.resolution_range is not None:
        # The device gave a span rather than an enumeration, so it is shown as
        # a span. Expanding it into a list of plausible values would print
        # saneless's own invention rather than the device's answer.
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
    ``_echo_capabilities`` shows and nothing it does not: a key appears only
    when the device reported something for it, and the resolution support
    keeps whichever shape the device gave, a list or a min/max/step range
    with the floats as reported.

    Args:
        caps: The capabilities to convert.

    Returns:
        The capabilities, keyed in the order the text mode prints them.

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

    Only a scanner error is caught: one device that will not answer must not
    hide the others, so its reason is logged and printed as one stderr line
    and the caller carries on. Anything else is a saneless bug and still
    reaches the group guard.

    Args:
        scanner: The open backend.
        name: The SANE device name to probe.

    Returns:
        The capabilities, or the one-line reason the probe failed.

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

    Args:
        device_list: The devices SANE reported.

    """
    cols = shutil.get_terminal_size((80, 24)).columns
    name_w = max(20, cols - 45)
    vendor_w = 15
    model_w = 20
    header = f"{'Name':<{name_w}} {'Vendor':<{vendor_w}} {'Model':<{model_w}} {'Type'}"
    click.echo(header)
    click.echo("-" * min(len(header), cols))
    for d in device_list:
        click.echo(
            f"{_truncate(neutralise_controls(d.name), name_w):<{name_w}} "
            f"{_truncate(neutralise_controls(d.vendor), vendor_w):<{vendor_w}} "
            f"{_truncate(neutralise_controls(d.model), model_w):<{model_w}} "
            f"{neutralise_controls(d.device_type)}"
        )


def _devices_as_json(
    scanner: ScannerBackend, device_list: list[DeviceInfo], *, capabilities: bool
) -> int:
    """
    Print the device list as one JSON document on stdout.

    Without ``capabilities`` the document is the four keys it has always had,
    in the same order, so scripts written against it see no change. With it,
    each device also gets ``capabilities``, which is ``null`` alongside a
    ``capabilities_error`` reason when that device's probe failed.

    Args:
        scanner: The open backend.
        device_list: The devices SANE reported.
        capabilities: Whether to probe and include each device's capabilities.

    Returns:
        The number of devices whose capabilities could not be read.

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

    Args:
        scanner: The open backend.
        device_list: The devices SANE reported.
        capabilities: Whether to probe and print each device's capabilities.

    Returns:
        The number of devices whose capabilities could not be read.

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
    # python-sane is mandatory: a command that needs it refuses before loading
    # config or touching the device, exit 2 through the guard. --help never
    # reaches this body, so it needs no python-sane.
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
        # The empty JSON array is data, so it goes to stdout; the table mode's
        # sentence is a status line like "Discovering scanners...", so it goes
        # to stderr and an empty table leaves stdout empty.
        if as_json:
            click.echo("[]")
        else:
            click.echo("No scanners found.", err=True)
        return

    render = _devices_as_json if as_json else _devices_as_text
    failed = render(scanner, device_list, capabilities=capabilities)
    # A device whose capabilities could not be read is reported where it
    # stands and the others still print; the exit code then says a scanner
    # read failed, once everything has been written.
    if failed:
        ctx.exit(ExitCode.SCAN)


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option(
    "--limit", default=CLI_JOBS_DEFAULT_LIMIT, type=int, help="Maximum jobs to show."
)
@click.pass_context
def jobs(ctx: click.Context, *, as_json: bool, limit: int) -> None:
    """List recent scan job history."""
    settings = _load_cli_settings(ctx)
    # sqlite3.connect does not create parent directories, so data_dir must
    # exist before JobStore opens the database. Deliberately not hidden inside
    # the db_path property: a property with a filesystem side effect surprises.
    # Created owner-only: it holds the job history and any preserved scans.
    make_private_dir(settings.output.data_dir)
    store = JobStore(db_path=settings.output.db_path)
    try:
        recent = store.list_recent(limit=limit)
        if as_json:
            click.echo(
                json.dumps(
                    [
                        {
                            "id": j.id,
                            "profile": j.profile,
                            "title": j.title,
                            # Raw enum value, deliberately not humanised: this
                            # is the machine contract and scripts compare
                            # against "DONE" / "FALLBACK".
                            "state": j.state.value,
                            # UTC ISO-8601, deliberately not localised: this is
                            # a machine contract documented in
                            # docs/how-to/cli-scripting.md, and local time is
                            # for *user-facing* surfaces. The human table below
                            # goes local; this does not.
                            "created_at": j.created_at.isoformat(),
                            "outcome": j.outcome.value if j.outcome else None,
                            "warning": j.warning,
                            # The full stored text, host paths and the
                            # paperless URL included: the web page shows only
                            # a path-free sentence that points here. Added
                            # after the other keys, so existing scripts are
                            # unaffected.
                            "error": j.error,
                            # Added after the other keys, so existing scripts
                            # are unaffected. NULL when never recorded, never
                            # a zero. The positions are 1-based scanned page
                            # numbers in document order, and are information
                            # rather than a warning: a DONE that removed blank
                            # backs keeps "warning": null.
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
                # A stored title or profile can hold a control character, and
                # this table goes to a terminal. It is escaped before
                # truncating, so the width is measured on what prints.
                profile = _truncate(neutralise_controls(j.profile), profile_w)
                title = _truncate(neutralise_controls(j.title), title_w)
                click.echo(
                    # The one shared formatter the web history table reads, so
                    # the two surfaces cannot drift. Seconds are gone and
                    # the zone is named.
                    f"{local_time(j.created_at):<{ts_w}} "
                    f"{profile:<{profile_w}} "
                    f"{title:<{title_w}} "
                    f"{job_label(j.state, j.warning, j.error_category)}"
                )
    finally:
        store.close()


def _bind_listening_sockets(host: str, port: int) -> list[socket.socket]:
    """
    Bind and listen on every address ``serve`` was given, before uvicorn starts.

    The sockets are bound here rather than by uvicorn so that the port
    saneless reports is the port it holds: with port 0 the OS chooses one,
    and there is no gap between checking a port and binding it for another
    process to take it in. The host is resolved, and every address it
    resolves to is bound, each once, the way uvicorn bound a name itself:
    ``localhost`` usually resolves to ``::1`` first and ``127.0.0.1`` second,
    and a reverse proxy pointed at either must reach saneless. An address
    literal resolves to itself alone. With port 0 the port the OS chose for
    the first address is reused for the rest, so one port serves them all.

    If any address cannot be bound, the sockets already bound are closed and
    ``serve`` fails, rather than starting on only part of what was asked.

    Args:
        host: The address or name to bind.
        port: The port to bind; 0 asks the OS for a free one.

    Returns:
        The bound, listening sockets, in the resolver's order.

    Raises:
        ConfigError: The host did not resolve or an address could not be
            bound. That is a setup problem, so it exits 2 like any other
            failure to start.

    """
    try:
        infos = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )
    except OSError as exc:
        msg = f"Cannot bind to {host}:{port}: {describe(exc)}"
        raise ConfigError(msg) from exc
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

    The options match what uvicorn's own bind set. SO_REUSEADDR lets a
    restart bind a port whose last connections are still closing. On IPv6,
    IPV6_V6ONLY keeps ``::`` from also listening on IPv4 behind the
    operator's back. Port sharing is never switched on, because it would let
    a second process listen on the same port. ``listen()`` is called before
    the socket is handed over, so a connection made while the app starts up
    waits in the queue instead of being refused.

    Args:
        host: The address or name ``serve`` was given, for the message.
        port: The port ``serve`` was given, for the message.
        kind: The family, socket type and protocol the resolver gave.
        sockaddr: The resolved address to bind.

    Returns:
        The bound, listening socket.

    Raises:
        ConfigError: The address could not be bound. The message names the
            resolved address too when the host was a name.

    """
    family, socktype, proto = kind
    address = str(sockaddr[0])
    where = f"{host}:{port}" if address == host else f"{host}:{port} ({address})"
    try:
        sock = socket.socket(family, socktype, proto)
    except OSError as exc:
        msg = f"Cannot bind to {where}: {describe(exc)}"
        raise ConfigError(msg) from exc
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind(sockaddr)
        sock.listen(_LISTEN_BACKLOG)
    except OSError as exc:
        sock.close()
        msg = f"Cannot bind to {where}: {describe(exc)}"
        raise ConfigError(msg) from exc
    return sock


def _socket_url(sock: socket.socket) -> str:
    """
    Return the URL a bound socket serves, with an IPv6 address bracketed.

    Args:
        sock: A bound socket.

    Returns:
        ``http://<address>:<port>``, bracketed so it pastes into a browser.

    """
    address, port = sock.getsockname()[:2]
    shown = f"[{address}]" if sock.family == socket.AF_INET6 else address
    return f"http://{shown}:{port}"


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
    # python-sane is mandatory: a command that needs it refuses before loading
    # config or touching the device, exit 2 through the guard. --help never
    # reaches this body, so it needs no python-sane.
    require_sane()
    # The one command that streams its logs: a service writes no file, so its
    # records reach `docker logs` and journald instead. Every other command
    # keeps the rotating file handler.
    settings = _load_cli_settings(ctx, stream_logs=True)
    actual_host = host or settings.output.web_host
    # An explicit 0 is a request for an OS-chosen port, not a missing value,
    # so only an absent flag falls back to the configured port.
    actual_port = port if port is not None else settings.output.web_port

    # The address is bound before SANE is initialised or the app is built, so
    # a port that is taken fails at once, costing no SANE start-up and leaving
    # no initialised backend behind that no lifespan would ever close.
    sockets = _bind_listening_sockets(actual_host, actual_port)
    # Every path out of here closes every socket, whatever raises between the
    # bind and the end of Server.run.
    try:
        # serve scans nothing itself, so SANE failing to initialise is a
        # failure to start -- "can't start, fix your setup", exit 2 like a
        # port that cannot be bound -- not exit 1, which means a scan failed.
        try:
            scanner = SaneBackend(host=settings.scanner.host)
        except ScanError as exc:
            msg = f"The web server could not start: {exc}"
            raise ConfigError(msg) from exc
        # No close callback here, unlike the three one-shot commands: this
        # backend outlives the command body.  The app is handed it and the
        # lifespan closes it once the worker confirms it stopped, which is the
        # only point at which no thread can still be inside SANE.
        app = create_app(settings, scanner)
        _run_server(app, sockets, settings.output.log_level)
    finally:
        for sock in sockets:
            sock.close()


def _run_server(app: FastAPI, sockets: list[socket.socket], log_level: str) -> None:
    """
    Announce the bound addresses and run uvicorn on the given sockets.

    Args:
        app: The ASGI app to serve.
        sockets: The bound, listening sockets; the caller closes them.
        log_level: The configured log level, which uvicorn follows.

    Raises:
        ConfigError: The server never started; uvicorn has already logged why.

    """
    urls = [_socket_url(sock) for sock in sockets]
    # Printed, not logged: serve's log stream is stderr as well, so doing both
    # put the same line there twice, and a log record alone would disappear
    # at log_level WARNING. uvicorn announces no address of its own when it is
    # handed sockets, so this is the only place the address is shown.
    for url in urls:
        click.echo(f"Serving on {url}", err=True)

    # uvicorn follows the configured log_level, not -v: -v is saneless's own
    # detail and must not turn on uvicorn's or httpx2's debug output. The
    # validated log level lower-cases to a name uvicorn accepts.
    #
    # The config needs nothing extra for the streaming mode, and adding
    # anything would break it: a None log config means uvicorn applies no
    # dictConfig and attaches no handlers of its own, so uvicorn.error,
    # uvicorn.access and uvicorn.asgi propagate to saneless's root handlers.
    # Leaving the access log on therefore puts per-request lines, and
    # uvicorn's own startup lines, on the same stream for free.
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_config=None,
            log_level=log_level.lower(),
            access_log=True,
        )
    )
    # On Ctrl-C uvicorn shuts down gracefully and then re-raises the signal it
    # caught, which arrives here as KeyboardInterrupt. A running server being
    # stopped is a normal stop, exit 0, so it is swallowed here; a Ctrl-C
    # before this point -- while settings load or the app is built -- is not
    # uvicorn's to handle and reaches the group guard, exit 130.
    try:
        with contextlib.suppress(KeyboardInterrupt):
            server.run(sockets=sockets)
    except SystemExit:
        # uvicorn exits the process itself when start-up fails, with a code of
        # its own choosing that collides with this CLI's table. A server that
        # never started is this project's "could not start", handled below; a
        # started server exiting is uvicorn's own decision and is left alone.
        if server.started:
            raise
    # Server.run returns quietly when start-up fails, such as the app's
    # lifespan raising; uvicorn has already logged why. Every command shares
    # one exit table, so that is a failure to start: one line, exit 2.
    if not server.started:
        msg = (
            f"The web server could not start on {', '.join(urls)}; "
            "the cause is in the preceding log lines"
        )
        raise ConfigError(msg)


def _echo_write_result(
    result: ProfileWriteResult, profiles: dict[str, ProfileConfig]
) -> None:
    """
    Print what ``auto-profiles`` did to the config file, grouped by action.

    The group lines come from ``ProfileWriteResult.groups``, the same
    vocabulary the worker's startup log uses. Added and Refreshed
    groups are followed by one detail line per profile they wrote.

    Args:
        result: What the write did.
        profiles: The generated profiles, for the detail lines.

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

    A new file takes its directory's owner, so root writing into a root-owned
    ``/etc/saneless`` -- the usual bare-metal layout -- leaves the file root's,
    mode 0600.  saneless running as an ordinary user would then find it on the
    next start and fail to read it, so the operator is told which file to give
    to that user.  With no writable system directory, root's file is instead
    the per-user one under its own home, which no other user searches; a
    ``chown`` there would fix nothing, so that note gives the moves that do.
    A file owned by anyone else needs nothing, and a status that cannot be
    read is not worth failing a write that succeeded.

    Args:
        path: The file just created, absolute.
        system: The system config file, absolute.

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

    The last documented location saneless may write, because the search reads
    in order and stops at the first file: a new file in ``./`` -- which in the
    container is the working directory ``/var/lib/saneless`` -- would outrank
    ``/etc/saneless`` on the next start, and the command meant to help would
    have built the shadowing trap itself.  So the system file is the target
    when its directory already exists and this process may write it; that is
    the directory the container's ``./config`` mount provides.  It must already
    exist because saneless never creates a system directory, even as root.
    Otherwise the per-user XDG file is the target, which nothing but the
    working-directory file outranks.

    The candidates are the ones the load searched, so this reads the same list
    in the same order and cannot drift from it; settings built without a load
    fall back to the search list itself.  Nothing is created here.

    Args:
        settings: The settings in hand, carrying the search that built them.

    Returns:
        The file to create, absolute.

    """
    system, user = _new_config_candidates(settings)
    if system.parent.is_dir() and os.access(system.parent, os.W_OK):
        return system.absolute()
    return user.absolute()


def _new_config_candidates(settings: Settings) -> tuple[Path, Path]:
    """
    Return the system and per-user config files ``auto-profiles`` may create.

    Args:
        settings: The settings in hand, carrying the search that built them.

    Returns:
        The last searched candidate (the system file) and the second (the
        per-user XDG file), from the recorded search or else the search list.

    """
    discovery = settings.config_discovery
    searched = discovery.searched if discovery is not None else ()
    candidates = searched if len(searched) > 1 else config_search_paths()
    return candidates[-1], candidates[1]


def _passed_over_system_dir(settings: Settings) -> str:
    """
    Say why the system config directory was not the target, if it exists.

    ``os.access`` refuses a read-only mount and a directory owned by someone
    else alike, and either sends ``auto-profiles`` to the per-user file. When
    that write then fails, its message would name only a file the operator
    never meant to use; this names the directory that was passed over and
    which of the two it is, because their fixes differ -- remount it
    read-write, or change its owner.

    Args:
        settings: The settings in hand, carrying the search that built them.

    Returns:
        `` (<dir> exists but ...)`` to append to the failure, or an empty
        string when the directory does not exist or is writable.

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
    # python-sane is mandatory: a command that needs it refuses before loading
    # config or touching the device, exit 2 through the guard. --help never
    # reaches this body, so it needs no python-sane.
    require_sane()
    # The warning is suppressed so that the refusal below can be the only
    # thing said: both are the same sentence, and printing it twice teaches a
    # reader that this output repeats itself.
    settings = _load_cli_settings(ctx, warn_stale=False)
    if config_file_state(settings) is ConfigFileState.STALE_ONLY:
        # This command is the only thing in saneless that creates a config
        # file, and with nothing loaded its target is a saneless.toml in a
        # searched directory -- which the next start loads.  Writing it now
        # would not lose the old file's URL and token but would bury them: the
        # search would stop at the new file, the red row telling the operator
        # to rename the old one would drop to amber, and the appliance would
        # keep running on defaults.  Refusing costs a rename; writing costs
        # the evidence.  Exit 2 through the group guard, as a configuration
        # error.
        row = configuration_check(settings, absolute_paths=True)
        msg = f"{row.message} {row.next_step}"
        raise ConfigError(msg)
    # Not stale-only, so nothing is refused; an old file sitting beside the
    # loaded one still gets said once, here rather than at load.
    _warn_stale_config(settings)

    scanner = SaneBackend(host=settings.scanner.host)
    # This command ends through ctx.exit() as well as by returning and by
    # raising; a close callback covers all three.
    ctx.call_on_close(scanner.close)
    device_list = scanner.get_devices()
    if not device_list:
        # A scanner condition, exit 1 through the guard, exactly as `scan`
        # reports the same finding: an exit code means the same thing in every
        # command.
        msg = (
            "No scanner found: auto-detection found no devices. "
            "Check what SANE can see with `saneless devices`"
        )
        raise NoScannerFoundError(msg)

    # Use configured device or first discovered device.  A discovered one is
    # also pinned below, so later scans do not follow whichever scanner SANE
    # lists first; a configured one, from the file or the environment, is
    # already a choice and is not written again.
    pin = None if settings.scanner.device else device_list[0].name
    device_id = settings.scanner.device or device_list[0].name
    caps = scanner.get_capabilities(device_id)
    profiles = generate_profiles(caps, device_type_of(device_list, device_id))

    # The file that was loaded (including an explicit --config). With no loaded
    # file the target is the last documented location that is writable, never
    # the working directory, whose file would outrank every other on the next
    # start; _new_config_target has the reasoning.  Chosen before the write so
    # a failure below can always name it, and the output names it absolutely
    # so the operator sees where it went.
    config_path = settings.config_path or _new_config_target(settings)
    # Asked before the write, which is what creates it; a rewrite keeps the
    # owner the file already had, so only a created file can need the note.
    created = not config_path.exists()
    # A ConfigError here (a single-file bind mount, a non-UTF-8 file, or
    # merged text that would not parse) names the file and the fix; the group
    # guard prints it as-is and exits 2, with no traceback.  An OSError --
    # including a per-user directory that cannot be created, as in a container
    # whose HOME does not exist -- ends the same way, naming the target.
    try:
        # Only a new file's directory is created: the system target's
        # directory exists by construction, so this is only ever the XDG
        # ``saneless`` directory, made private because the file may later
        # hold the Paperless token, and owned like its parent so root with a
        # user's HOME leaves nothing in it that the user cannot reach.  The
        # file itself is 0600 and takes its directory's owner, which the
        # atomic writer sees to.
        if settings.config_path is None and not config_path.parent.is_dir():
            make_config_directory(config_path.parent)
        result = write_profiles_to_config(
            config_path, profiles, force=force, device=pin
        )
    except OSError as exc:
        passed_over = (
            _passed_over_system_dir(settings) if settings.config_path is None else ""
        )
        click.echo(
            f"Cannot write {config_path}: {exc.strerror or exc}{passed_over}",
            err=True,
        )
        ctx.exit(ExitCode.CONFIG)
    _echo_write_result(result, profiles)
    if created:
        _note_root_owned_config(
            config_path, _new_config_candidates(settings)[0].absolute()
        )


def _state_marker(state: CheckState) -> str:
    """
    Return the bracketed token ``doctor`` prints in front of one check row.

    These are CLI affordances rather than vocabulary, which is why they are not
    ``check_state_label``: that function answers "what does a screen reader
    announce", and the answer there is ``"Failed"``, a word. Here the job is a
    scannable left margin in a fixed-width terminal, so all three tokens are
    the same width and a reader's eye finds the red rows without reading them.

    A total ``match``, for the reason ``checks.py``'s four lookups are: a
    fourth ``CheckState`` stops this function type-checking until somebody
    decides what it looks like.

    Args:
        state: The state to mark.

    Returns:
        ``"[ OK ]"``, ``"[WARN]"`` or ``"[FAIL]"``.

    Raises:
        AssertionError: If the value is not a CheckState member.

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


# The token a row nobody probed prints instead of `[ OK ]`.
#
# No `doctor` invocation produces this marker today: the command builds its
# CheckContext with `skip_scanner` at its default and passes no scanner gate,
# so neither `_scanner_skipped` nor `_scanner_busy` is reachable from the CLI.
# It exists anyway, and deliberately. The point of one check registry is that
# it feeds both surfaces, so a surface that would mis-render a row the registry
# can build is a divergence already present and merely unreached -- and
# `CheckResult.skipped` was once left exactly that way, half-wired, rendered by
# neither surface while two docstrings said it was rendered by both.
# The first caller that passes `skip_scanner=True` should get a correct table,
# not a bug report.
#
# Six characters, like the three state tokens, so `_MARKER_WIDTH` below does
# not silently widen the name column for one row.
_SKIPPED_MARKER: Final = "[SKIP]"


def _row_marker(result: CheckResult) -> str:
    """
    Return the token ``doctor`` prints in front of one finished row.

    ``_state_marker`` answers "what does this state look like"; this answers
    "what does this row look like", and the two differ whenever ``skipped`` is
    set.  The flag is a fact about the probe and the state is a verdict about
    the appliance: a skipped row carries ``CheckState.OK`` so that a scripted
    health gate does not go red for a probe that was deliberately not taken,
    which means marking it from the state alone prints the one token a reader
    scans for as "fine" in front of a sentence saying nothing was checked.
    ``checks.check_row_class`` and its two siblings make the same substitution
    for the web strip, from the same flag.

    Args:
        result: The finished row about to be printed.

    Returns:
        ``_SKIPPED_MARKER`` when the probe was skipped, otherwise
        ``_state_marker(result.state)``.

    """
    if result.skipped:
        return _SKIPPED_MARKER
    return _state_marker(result.state)


# The three column widths `saneless doctor` renders with, all derived rather
# than written down, exactly as _STATUS_COL_WIDTH is and for the same reason: a
# sixth CheckKey with a longer name, or a fourth CheckState with a wider token,
# must not be able to overflow an 80-column terminal without anyone noticing.
# The indent puts a next step underneath the message it belongs to, so a row
# and its remedy read as one item rather than two.
#
# The marker width counts `_SKIPPED_MARKER` alongside the state tokens rather
# than relying on the four strings happening to be the same length, so the
# derivation stays correct if any one of them is ever respelled.
_MARKER_WIDTH = max(
    len(marker)
    for marker in (*(_state_marker(state) for state in CheckState), _SKIPPED_MARKER)
)
_NAME_COL_WIDTH = max(len(check_name(key)) for key in CheckKey)
_NEXT_STEP_INDENT = " " * (_MARKER_WIDTH + 1 + _NAME_COL_WIDTH + 1)

# The config resolution table's caption and its six verdicts, one per line.
# "used", "not used" and "same file" are about the search -- the last is a
# candidate that names a file already listed, such as ./saneless.toml when run
# from inside the XDG directory; "ignored" and "leftover" are about a file under
# the superseded name, and they differ because the two situations differ -- in
# one nothing was loaded and the file is the reason, in the other something was
# loaded and the file is merely still there.
_RESOLUTION_CAPTION: Final = "Config files searched, in order:"
_RESOLUTION_LABELS: Final = (
    "used",
    "not used",
    "same file",
    "not found",
    "ignored",
    "leftover",
)
# Derived, like the row widths above and for the same reason: respelling a
# verdict must not be able to break the column silently.
_RESOLUTION_LABEL_WIDTH: Final = max(len(label) for label in _RESOLUTION_LABELS)
# Printed instead of an empty table. A caption with nothing under it reads as
# output that failed halfway, rather than as "this process ran no search" --
# which is what settings built directly, without a load, actually did.
_RESOLUTION_NONE: Final = "  none recorded"


def _resolution_line(label: str, path: Path, note: str = "") -> str:
    """
    Render one entry of the config resolution table.

    Args:
        label: One of ``_RESOLUTION_LABELS``.
        path: The file this entry is about.
        note: A parenthesised aside, or "" for none.

    Returns:
        The line, with the path at the same column whatever the label.

    """
    return f"  {label:<{_RESOLUTION_LABEL_WIDTH}}  {absolute_or_as_spelled(path)}{note}"


def _config_resolution_lines(settings: Settings) -> list[str]:
    """
    Say where every configuration file the search looked at ended up.

    Absolute paths, which the status strip's rows may not carry: that page is
    reachable by anyone on the LAN, and this is a terminal on the machine,
    where the resolved path is the half an operator can act on.

    The recording is read and nothing is stat-ed again.  A file created,
    renamed or deleted since startup would make a fresh look describe a
    program that is not running -- and the whole point of the table is to
    explain the settings the process is holding.

    Each candidate is ``used``, ``not used`` (an earlier file won), ``same
    file`` (a file already listed, reached through another spelling) or ``not
    found``; each superseded-name file is ``ignored`` or ``leftover``.

    Args:
        settings: The settings in hand, carrying the search that built them.

    Returns:
        One line per entry, or empty when no search was recorded.

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
        # One file reached through a second candidate is listed once more,
        # as itself: never "not used", which would claim a second file went
        # unread, and never "not found", which would claim the path is empty.
        # Two candidates can even be spelled alike (run from inside the XDG
        # directory, ./saneless.toml is the XDG file), so a spelling already
        # listed counts as well as a recorded alias.
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
    # A superseded-name file beside a candidate. Which verdict it gets is the
    # same distinction the Configuration row draws: with nothing loaded it is
    # the reason there is no configuration, and with something loaded it is
    # only still there.
    if discovery.loaded is None:
        label, note = "ignored", f" (old name; rename it to {CONFIG_FILENAME})"
    else:
        label, note = "leftover", " (old name; ignored)"
    lines.extend(_resolution_line(label, stale, note) for stale in discovery.stale)
    return lines


def _echo_config_resolution(settings: Settings) -> None:
    """
    Print the config resolution table under the check rows.

    Args:
        settings: The settings in hand.

    """
    click.echo("")
    click.echo(_RESOLUTION_CAPTION)
    for line in _config_resolution_lines(settings) or [_RESOLUTION_NONE]:
        click.echo(line)


def _doctor_scanner(settings: Settings) -> ScannerBackend | None:
    """
    Build a scanner backend for one ``doctor`` run, or report that there is none.

    ``None`` is how ``CheckContext`` represents "no scanner support on this
    machine", and producing it here rather than letting the failure out is what
    lets ``doctor`` report a machine without scanner support instead of
    refusing to run on it.

    All three failure shapes collapse to ``None``. ``ImportError`` is the bare
    missing module; ``ConfigError`` is what ``require_sane`` -- which
    ``SaneBackend.__init__`` calls for itself -- raises once it has translated
    that ``ImportError``; and ``ScanError`` is ``sane.init()`` refusing. The
    last is the least obvious of the three and is deliberate: a libsane that
    will not initialise is SANE support this machine does not actually have,
    the row's "install scanner support, then restart" is the right advice for
    it, and letting it out instead would exit 1 -- a code ``doctor``'s
    documented table does not list, for a command that scans nothing.

    Args:
        settings: The loaded configuration, for the sane-net host.

    Returns:
        A backend, or None when none could be built.

    """
    try:
        return SaneBackend(host=settings.scanner.host)
    except (ImportError, ConfigError, ScanError) as exc:
        # The type name only. The ConfigError's own message names the install
        # hint and the ImportError names a shared object path, and neither
        # belongs on a report a household member is meant to act on.
        logger.info("Scanner support unavailable: %s", type(exc).__name__)
        return None


def _doctor_paperless(settings: Settings) -> PaperlessClient | None:
    """
    Build a Paperless client for one ``doctor`` run, or report that there is none.

    ``PaperlessClient.__init__`` refuses exactly one thing -- a URL httpx2 will
    not parse -- and it refuses it with ``PaperlessError``, which the group
    guard would turn into exit 3. That would cost the operator the other four
    rows to report a fact the Paperless row already has a sentence for, and it
    would put a code in ``doctor``'s output that its documented table does not
    list. ``None`` reaches ``checks.py`` as the "not found at that URL" row.

    Args:
        settings: The loaded configuration.

    Returns:
        A client, or None when none could be built.

    """
    try:
        return PaperlessClient(
            settings.paperless.url,
            settings.paperless.token.get_secret_value(),
            settings.paperless.consume_dir,
        )
    except PaperlessError as exc:
        # Only the class name. A URL carrying credentials is refused when the
        # config loads, so the message holds no secret; it is left out because
        # the Paperless row already explains the failure to the operator, and
        # this line only has to record which exception it was.
        logger.info("Paperless client unavailable: %s", type(exc).__name__)
        return None


# This command deliberately does NOT call require_sane(), which is the first
# statement of `scan`, `devices`, `serve` and `auto-profiles`. Those four cannot
# do their job without a scanner, so refusing early is honest. `doctor`'s job is
# to say what is wrong, and a machine with no python-sane is precisely the
# machine whose owner needs that said: it still has a token, profiles, a
# fallback folder and a data directory to be told about. The import failure is
# caught in _doctor_scanner and rendered as one FAIL row among six instead of a
# refusal to run at all.
#
# The output is two sections: the check rows, then a table of where every
# configuration file the search looked at ended up. The rows say which
# situation the appliance is in, in the words the status strip uses; the table
# says which files, by absolute path, which the rows may not carry -- the strip
# is a page anyone on the LAN can load and this is a terminal on the machine.
# It is printed on every run and not only when something is wrong, because a
# resolution that appears only on failure cannot be compared against a working
# machine's, and comparing the two is how a configuration problem gets found.
#
# There is no --json, and this is a decision rather than an omission. Nothing in
# the docs, the tests, the Dockerfile or the compose file would consume it, and
# a container HEALTHCHECK that calls `doctor` -- the one caller that would have
# wanted a machine shape -- is deliberately not offered. A JSON mode would be a
# wire contract with no reader, and a wire contract is only free until the first
# person parses it. The human-readable rows, the resolution table and the exit
# code are the whole contract.
@cli.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Check that saneless is ready to scan."""
    settings = _load_cli_settings(ctx)
    scanner = _doctor_scanner(settings)
    if scanner is not None:
        # Registered before the first SANE call, so a check that fails still
        # leaves the process with SANE shut down.
        ctx.call_on_close(scanner.close)
    paperless = _doctor_paperless(settings)
    try:
        results = run_checks(
            CheckContext(
                settings=settings,
                scanner=scanner,
                paperless=paperless,
                # A one-shot command attempts no persist, so this is the
                # derivation it is entitled to; the function's docstring has
                # the reasoning, including why it cannot report the third
                # outcome. The status strip calls the same function, which is
                # what keeps the two surfaces on one Profiles row.
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
        if result.next_step:
            click.echo(f"{_NEXT_STEP_INDENT}{result.next_step}")

    _echo_config_resolution(settings)

    # Any failing check exits 2, and no new ExitCode member expresses it. Three
    # reasons, in order: tests/test_deployment_config.py:393,403 assert the
    # documented global tables equal every member, so a sixth code is a
    # documentation change in three files and a revision of the exit-code table;
    # 2 already means "can't start, fix your setup", which is what every red
    # check is saying; and `doctor` reports a list, so one process has one exit
    # code to give and splitting a red Paperless row out to 3 would mean
    # choosing which red row the shell gets to hear about. A WARN is
    # deliberately not a failure -- an appliance that scans and files is not
    # broken because it could be tidier, and a gate that goes red for tidiness
    # gets ignored.
    if worst_state(results) is CheckState.FAIL:
        ctx.exit(ExitCode.CONFIG)
