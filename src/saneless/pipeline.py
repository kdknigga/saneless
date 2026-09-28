"""
Pipeline orchestration: scan -> assemble PDF -> upload to paperless-ngx.

Coordinates the full scan workflow inside a job workspace that is removed
when the run ends. One guard, ``_PipelineRun.execute``, spans the whole run
and keeps the most finished artefact of a failed one in ``failed/``.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import shutil
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final, assert_never

from saneless import preservation
from saneless.exceptions import (
    AllPagesBlankError,
    ConfigError,
    ScanCancelledError,
    ScanError,
    ScanInterrupted,
    SpoolError,
    describe,
)
from saneless.pages import BlankFilterResult, filter_blank_pages
from saneless.pdf import assemble_pdf, build_pdf_filename
from saneless.private_dirs import ensure_private_dir
from saneless.scanner.base import MAX_PAGES_PER_PASS, ScanBatch, ScanSettings
from saneless.spool import SpooledPageSink
from saneless.vocabulary import (
    ErrorCategory,
    FlipOutcome,
    JobState,
    PassAnswer,
    PassPrompt,
    PassWait,
    ScanOutcome,
    blank_timeout_finish_warning,
    cap_finish_warning,
    classify_error,
    timeout_finish_warning,
)
from saneless.workspace import SPOOL_DIR_NAME, JobWorkspace, has_pages_left

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from pathlib import Path

    from saneless.config import ProfileConfig, Settings
    from saneless.paperless import PaperlessClient, UploadResult
    from saneless.scanner.base import PageRecord, ScannerBackend

__all__ = [
    "MAX_DOCUMENT_PAGES",
    "SCAN_LABEL_BACK",
    "SCAN_LABEL_FRONT",
    "AnswerSlot",
    "FlipAnswerSlot",
    "FlipCoordinator",
    "PassCoordinator",
    "PipelineEvent",
    "PipelineRequest",
    "ScanResult",
    "Settled",
    "run_pipeline",
]


# The value of the one event whose name ends in "PASS", named rather than
# written inline: ruff's S105 reads a string literal assigned to a name ending
# in "pass" as a hardcoded password.  A scan pass is not a password, and the
# convention that every member's value is its name stays unbroken this way.
_NEXT_WAIT_EVENT_VALUE: Final = "AWAITING_NEXT_PASS"


class PipelineEvent(StrEnum):
    """Events emitted by the scan pipeline to report progress."""

    SCANNING = "SCANNING"
    AWAITING_FLIP = "AWAITING_FLIP"
    SCANNING_REVERSE = "SCANNING_REVERSE"
    AWAITING_NEXT_PASS = _NEXT_WAIT_EVENT_VALUE
    AWAITING_BLANK_DECISION = "AWAITING_BLANK_DECISION"
    AWAITING_RETRY = "AWAITING_RETRY"
    ASSEMBLING = "ASSEMBLING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"

    @property
    def job_state(self) -> JobState:
        """
        Return the job state this event implies.

        The projection is total: every event has a ``JobState`` twin.
        ``SCANNING_REVERSE`` projects to ``JobState.SCANNING_REVERSE``, so the
        second pass of a manual-duplex scan is persisted as its own busy state
        and the job leaves ``AWAITING_FLIP`` the moment pass B starts -- which
        is what takes the flip prompt, and its Continue and Abort controls, off
        the screen while the backs feed.

        The three multi-page waits -- ``AWAITING_NEXT_PASS``,
        ``AWAITING_BLANK_DECISION`` and ``AWAITING_RETRY`` -- each project to the
        job state of the same name, so a job says which question it is waiting
        on, and the next ``SCANNING`` event is what takes that prompt off the
        screen when the operator asks for another pass.

        Note that the returned state is not an instruction to write it:
        ``DONE`` is terminal and the worker writes it only after the
        pipeline has returned.  Callers decide which states they apply.

        Returns:
            The matching JobState.

        Raises:
            AssertionError: If the value is not a PipelineEvent member.

        """
        match self:
            case PipelineEvent.SCANNING:
                state = JobState.SCANNING
            case PipelineEvent.AWAITING_FLIP:
                state = JobState.AWAITING_FLIP
            case PipelineEvent.SCANNING_REVERSE:
                state = JobState.SCANNING_REVERSE
            case PipelineEvent.AWAITING_NEXT_PASS:
                state = JobState.AWAITING_NEXT_PASS
            case PipelineEvent.AWAITING_BLANK_DECISION:
                state = JobState.AWAITING_BLANK_DECISION
            case PipelineEvent.AWAITING_RETRY:
                state = JobState.AWAITING_RETRY
            case PipelineEvent.ASSEMBLING:
                state = JobState.ASSEMBLING
            case PipelineEvent.UPLOADING:
                state = JobState.UPLOADING
            case PipelineEvent.DONE:
                state = JobState.DONE
            case _:
                assert_never(self)
        return state


def _wait_event(wait: PassWait) -> PipelineEvent:
    """
    Return the event a multi-page run emits when it opens ``wait``.

    Args:
        wait: The question the run is about to ask.

    Returns:
        The waiting event whose job state names that question.

    Raises:
        AssertionError: If the value is not a PassWait member.

    """
    match wait:
        case PassWait.NEXT_PASS:
            event = PipelineEvent.AWAITING_NEXT_PASS
        case PassWait.BLANK_DECISION:
            event = PipelineEvent.AWAITING_BLANK_DECISION
        case PassWait.RETRY:
            event = PipelineEvent.AWAITING_RETRY
        case _:
            assert_never(wait)
    return event


logger = logging.getLogger(__name__)

# The per-pass prefix each acquisition pass's file names carry, inside the
# workspace's spool directory: ``a-0001.png`` for a simplex job or
# a duplex job's fronts, ``b-0001.png`` for its backs.  The labels are
# for telling two passes apart in one directory; document order comes from the
# record list and never from these names.
#
# Named constants rather than literals at the call sites, for a lint reason
# worth recording so nobody "tidies" them back: ruff's S106 reads any keyword
# argument whose name contains "pass" as a possible hardcoded password, and
# ``pass_label=`` does, so a string literal there fails the lint and this
# project adds no suppressions. The constants are spelled ``_SPOOL_LABEL_*``
# and not ``_PASS_*_LABEL`` for the sibling rule S105, which reads the same
# substring in a variable's own name. Naming them also gives anything that
# asserts on the convention one place to import it from.
_SPOOL_LABEL_A: Final = "a"
_SPOOL_LABEL_B: Final = "b"

# The most pages a multi-page document may hold before the run stops offering
# another pass.  The same number as the per-pass cap, and defined by it, because
# the two are one judgement about paper -- how big a stack anyone scans as one
# document -- so if one moves the other must move with it.  The cap is checked
# only between passes, never inside one, so a document can reach
# ``MAX_DOCUMENT_PAGES - 1 + MAX_PAGES_PER_PASS`` pages: a pass that starts one
# page short of the cap is allowed to finish.
MAX_DOCUMENT_PAGES: Final = MAX_PAGES_PER_PASS

# Which half of a manual-duplex run a ``pass_count_callback`` call is reporting.
# Shared constants rather than a literal spelled once here and once in the
# worker, so the two halves of the channel can never drift apart, and so a test
# asserting on the ordering imports the label instead of retyping it.
#
# Public (no leading underscore) because ``worker.py`` compares against them,
# and named ``SCAN_LABEL_*`` rather than the obvious ``PASS_FRONT`` for exactly
# the lint reason recorded above ``_SPOOL_LABEL_A``: ruff's S105 reads any
# variable whose own name contains "pass" as a possible hardcoded password, and
# this project adds no suppressions.  Verified: ``PASS_FRONT: Final = "front"``
# raises S105 under this configuration.
SCAN_LABEL_FRONT: Final = "front"
SCAN_LABEL_BACK: Final = "back"


class FlipCoordinator(ABC):
    """
    The one way a manual-duplex run waits for the operator to flip the stack.

    Pass A has fed the fronts; before pass B the pipeline asks this seam a
    single question -- has the stack been turned? -- and gets back a single,
    total ``FlipOutcome``.  The web worker answers it from the Continue and
    Abort routes, and the CLI answers it from a terminal prompt.

    This is an ``ABC`` and not a ``typing.Protocol`` on purpose, and the rule is
    observable in the tree: ``Protocol`` describes shapes this project does not
    own (``SaneDevice`` for python-sane's handle, ``_SettingsFactory`` for
    pydantic's constructor), while ``ABC`` defines seams the project implements
    itself (``ScannerBackend``).  Where this seam is called a "protocol" in
    lower case, the word means "contract", not ``typing.Protocol``.
    """

    @abstractmethod
    def wait_for_flip(self, timeout: float) -> FlipOutcome:
        """
        Block until the flip wait resolves, for at most ``timeout`` seconds.

        An implementation answers once, and its answer is final: whichever of
        Continue, Abort or the clock resolves the wait first is what this
        returns, and a signal arriving after that is dropped.

        Args:
            timeout: The longest the wait may hold the calling thread, in
                seconds.  ``0`` is a valid bound and returns at once.

        Returns:
            ``CONTINUED`` when the operator flipped the stack, ``ABORTED`` when
            they gave up at the prompt, or ``TIMED_OUT`` when neither happened
            within ``timeout``.

        """

    @property
    def abort_cause(self) -> Exception | None:
        """
        Why the wait answered ``ABORTED``, when it was not the operator's choice.

        An ``ABORTED`` answer usually means someone gave up at the prompt, and
        the pipeline reports that as a cancellation.  But a coordinator can
        also answer ``ABORTED`` because its prompt broke -- a read error such as
        an I/O error or undecodable input -- and nobody chose to stop.  End of
        input, a closed terminal included, is not such a break: it is the
        operator's cancel.  Such a coordinator returns the exception here, so
        the pipeline records a failure rather than a cancellation without a
        fourth ``FlipOutcome`` member.

        Concrete rather than abstract, so a coordinator whose aborts are always
        an operator's needs no change.

        Returns:
            The exception that forced the abort, or ``None`` when there was
            none -- including whenever the answer was not ``ABORTED``.

        """
        return None


class PassCoordinator(ABC):
    """
    The one way a multi-page run asks the operator what happens next.

    Between passes the pipeline asks this seam one question at a time -- is
    there another page, what about the pages that look blank, what now that a
    pass failed -- and gets back a single ``PassAnswer``.  The web worker
    answers it from the multi-page routes, and the CLI from a terminal prompt.

    It is a sibling of ``FlipCoordinator`` and deliberately not a widening of
    it: the manual-duplex flip wait keeps its own seam and its own outcomes, so
    a multi-page answer can never reach a manual-duplex run.

    This is an ``ABC`` and not a ``typing.Protocol`` on purpose, and the rule is
    observable in the tree: ``Protocol`` describes shapes this project does not
    own (``SaneDevice`` for python-sane's handle, ``_SettingsFactory`` for
    pydantic's constructor), while ``ABC`` defines seams the project implements
    itself (``ScannerBackend``, ``FlipCoordinator``).
    """

    @abstractmethod
    def ask(self, prompt: PassPrompt) -> PassAnswer:
        """
        Put ``prompt`` to the operator and block until it resolves.

        The wait holds the calling thread for at most
        ``prompt.timeout_seconds``.  An implementation answers once, and its
        answer is final: whichever of the operator, the clock or a stop
        resolves the wait first is what this returns.

        A coordinator that could not show the prompt -- its terminal broke,
        say -- must report ``ABORT`` and set ``abort_cause``, so the pipeline
        records a failure rather than an operator's cancel.

        Args:
            prompt: The question, and the only answers it accepts.

        Returns:
            A member of ``prompt.offered``; ``TIMED_OUT`` when nobody answered
            within ``prompt.timeout_seconds``; or ``INTERRUPTED`` when saneless
            is stopping.

        """

    @property
    def abort_cause(self) -> Exception | None:
        """
        Why the wait answered ``ABORT``, when it was not the operator's choice.

        An ``ABORT`` answer usually means someone gave up at the prompt, and
        the pipeline reports that as a cancellation.  But a coordinator can
        also answer ``ABORT`` because its prompt broke -- a read error such as
        an I/O error or undecodable input -- and nobody chose to stop.  End of
        input, a closed terminal included, is not such a break: it is the
        operator's cancel.  Such a coordinator returns the exception here, so
        the pipeline records a failure rather than a cancellation without a
        further ``PassAnswer`` member.

        Concrete rather than abstract, so a coordinator whose aborts are always
        an operator's needs no change.

        Returns:
            The exception that forced the abort, or ``None`` when there was
            none -- including whenever the answer was not ``ABORT``.

        """
        return None

    @property
    def stopping(self) -> bool:
        """
        Whether saneless is stopping, so no further pass may start.

        An answer claimed just before a stop keeps its meaning at the prompt,
        but the pass it asks for must not begin: a pass outlasts the bounded
        stop, and a run killed inside one never reaches the guard that keeps
        its accepted pages.  The run checks this before every pass after the
        first and ends as interrupted instead.

        Concrete rather than abstract, so a coordinator that is never stopped
        this way -- the CLI's, where a signal interrupts the run directly --
        needs no change.

        Returns:
            True once stopping has begun; False otherwise.

        """
        return False


class AnswerSlot[T: StrEnum]:
    """
    One answer, claimed once, and final: the claim every coordinator shares.

    The web worker's and the CLI's coordinators differ in where an answer comes
    from -- the web routes, or a terminal prompt -- but not in how it is
    claimed.  That claim lives here, once, so a fix to it reaches both.  It is
    generic over the answer's enum, so the flip wait (``FlipOutcome``) and the
    multi-page waits (``PassAnswer``) share the claim without sharing anything
    else.  This is a concrete helper the coordinators compose, not a seam:
    ``FlipCoordinator`` and ``PassCoordinator`` stay the only contracts the
    pipeline waits on, and anything a coordinator adds on top -- the web one's
    arming, for instance -- stays in that coordinator.

    The answer is written under the lock *before* the event is set, so a waiter
    that wakes always finds an answer to read -- there is no window in which the
    event says "resolved" and the slot still says nothing.  That ordering is
    what makes it race-free by construction rather than by timing.
    """

    def __init__(self) -> None:
        """Start unanswered."""
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._outcome: T | None = None

    @property
    def answer(self) -> T | None:
        """The claimed answer, or ``None`` while the slot is unanswered."""
        with self._lock:
            return self._outcome

    def offer(self, outcome: T) -> bool:
        """
        Claim the answer with ``outcome`` if nothing has claimed it yet.

        An offer that loses leaves the slot and the event untouched.

        Args:
            outcome: The answer this caller is offering.

        Returns:
            Whether ``outcome`` became the answer.

        """
        with self._lock:
            if self._outcome is not None:
                return False
            self._outcome = outcome
        self._event.set()
        return True

    def settle(self, outcome: T) -> T:
        """
        Claim the answer with ``outcome`` unless one is claimed, and return it.

        This is the path that ends a wait: a timeout, or a CLI answer.
        Returning the answer in effect, rather than asserting one exists, is
        what narrows ``T | None`` to ``T`` without an
        ``assert`` -- which ``S101`` bans in ``src/``.

        Args:
            outcome: The answer this caller is offering.

        Returns:
            The claimed answer: ``outcome`` if it was first, otherwise the
            answer that beat it.

        """
        with self._lock:
            if self._outcome is None:
                self._outcome = outcome
            claimed = self._outcome
        self._event.set()
        return claimed

    def wait(self, timeout: float) -> None:
        """
        Block until the slot is answered, for at most ``timeout`` seconds.

        It reports nothing: the caller reads the result through ``settle``, which
        is right whether the wait was answered or expired.  A
        ``KeyboardInterrupt`` raised while waiting propagates to the caller.

        Args:
            timeout: The longest to wait, in seconds.

        """
        self._event.wait(timeout)


class FlipAnswerSlot(AnswerSlot[FlipOutcome]):
    """The flip wait's slot, by the name every caller and test already uses."""


@dataclass(frozen=True)
class _FlipContext:
    """
    What ``_PipelineRun._scan_manual_duplex`` needs to wait for a flip.

    One record, built before the run starts, so a manual-duplex request that
    cannot run is refused before the device is touched.

    It carries the coordinator as non-Optional.
    ``PipelineRequest.flip_coordinator`` is ``FlipCoordinator | None`` because a
    simplex run legitimately has none, so the narrowing to "there is one"
    happens exactly once, where this record is built, and the callee never
    needs an ``assert`` (which ``S101`` bans in ``src/``) to prove it.

    Attributes:
        coordinator: The seam that answers the flip wait.
        timeout: Seconds the wait may hold the pipeline, from
            ``output.operator_wait_timeout_seconds``.

    """

    coordinator: FlipCoordinator
    timeout: float


@dataclass(frozen=True)
class _MultiPageContext:
    """
    What ``_PipelineRun._scan_multi_page`` needs to ask between passes.

    One record, built before the run starts, so a multi-page request that
    cannot run is refused before the device is touched.

    It carries the coordinator as non-Optional.
    ``PipelineRequest.pass_coordinator`` is ``PassCoordinator | None`` because
    a single-pass run legitimately has none, so the narrowing to "there is
    one" happens exactly once, where this record is built, and the callee
    never needs an ``assert`` (which ``S101`` bans in ``src/``) to prove it.

    Attributes:
        coordinator: The seam that answers every multi-page prompt.
        timeout: Seconds each prompt may hold the pipeline, from
            ``output.operator_wait_timeout_seconds``.

    """

    coordinator: PassCoordinator
    timeout: float


@dataclass
class DeviceMemory:
    """
    The device the last auto-detecting run of one worker chose.

    With ``scanner.device`` empty each run scans on whichever device SANE lists
    first, so a scanner that appears on the LAN between two jobs takes the
    second one silently.  Comparing against this record is what lets that
    change be logged.

    One instance lives per worker and rides on every ``PipelineRequest`` it
    builds; the CLI runs one scan per process and passes none.  It is not
    module state, which would carry one test's device into the next and one
    worker's into another's.

    Attributes:
        last_id: The device id the previous auto-detection chose, or None
            before the first one.

    """

    last_id: str | None = None


class Settled:
    """
    Whether a run's outcome is fixed: a flag with no lock in it.

    It is set by the run and read by the CLI's signal handler, and both run on
    the main thread; nothing ever waits on it.  So it takes no lock, and that
    is the point.  A ``threading.Event`` takes one in ``set`` and
    ``clear``, and a signal whose handler raises in the instant after the
    lock is taken leaves that lock held for good: the next ``set`` -- the
    very retry that lets a run settle despite the signal -- then blocks
    forever, with both signals already ignored.  Here ``set`` is a single
    attribute store, so a signal lands either before it, when the flag is
    still clear and the retry simply sets it, or after it, when the flag is
    set and the handler defers.
    """

    def __init__(self) -> None:
        """Start clear: nothing is settled yet."""
        self._flag = False

    def set(self) -> None:
        """Mark the outcome as fixed."""
        self._flag = True

    def clear(self) -> None:
        """Forget it, once the command whose outcome it was is over."""
        self._flag = False

    def is_set(self) -> bool:
        """
        Say whether the outcome is fixed.

        Returns:
            Whether ``set`` was called since the last ``clear``.

        """
        return self._flag


@dataclass
class PipelineRequest:
    """
    Parameters for a scan pipeline run.

    ``job_id`` names the assembled PDF, via
    :func:`saneless.pdf.build_pdf_filename`, and is what makes two scans of the
    same title two distinct files rather than one overwriting the other. Both
    entry points supply a uuid4: the worker passes ``job.id``, and ``saneless
    scan`` mints one for the run. The empty default exists only so the dozens
    of tests that construct a request from a profile name and a title need not
    invent one, and an empty value simply drops the segment -- which also
    drops the uniqueness guarantee, so nothing in production may rely on it.

    It is defaulted rather than required, and sits with the other defaulted
    fields: moving it into the non-default block above would reorder the
    dataclass and break positional construction.
    """

    profile_name: str
    title: str
    job_id: str = ""
    tags: list[int] | None = None
    correspondent: int | None = None
    status_callback: Callable[[PipelineEvent], None] | None = None
    thumbnail_callback: Callable[[str], None] | None = None
    # How many pages a manual-duplex pass produced, announced while the run is
    # still going.  Its own channel, mirroring
    # ``thumbnail_callback``, because the three alternatives are all worse:
    # ``status_callback`` is ``Callable[[PipelineEvent], None]`` and carries no
    # payload, so ``SCANNING_REVERSE`` cannot carry ``len(front_pages)``;
    # widening it would touch every call site and hand the CLI an argument it
    # does not want; and a ``PipelineEvent`` member carrying the number is
    # rejected outright, because members of that enum are states, not payloads.
    # ``ScanResult`` is no help either -- it is returned once, at the end, and
    # the count is wanted while pass B is still feeding.
    pass_count_callback: Callable[[str, int], None] | None = None
    # One field, one atomic answer.  This replaced a flip event and an abort
    # event, where an Abort set both so the waiter woke and then had to inspect
    # the second to learn why -- the two-step that once left an Abort pressed
    # during pass B doing nothing at all.
    flip_coordinator: FlipCoordinator | None = None
    # Whether this run builds one document from several flatbed passes.  Its
    # own field, not a profile setting, because it is a choice about this
    # scan -- one sheet or a stack of them -- made when the scan is started.
    multi_page: bool = False
    # Where a multi-page run's answers come from: the web routes or a terminal
    # prompt.  Its own field beside ``flip_coordinator`` rather than a widening
    # of it, so neither kind of wait can be answered with the other's answers.
    pass_coordinator: PassCoordinator | None = None
    # The worker's record of the device its last auto-detection chose, so a
    # change between jobs is logged.  Defaulted, like every field here, so the
    # CLI -- one scan per process, nothing to compare with -- passes none.
    device_memory: DeviceMemory | None = None
    # Set while a failed run's pages are being preserved, and cleared once
    # that is done, so a stopping server can wait for the pages to land in
    # failed/ rather than exit half way through copying them.  The worker
    # passes one; the CLI, which has no stop join to extend, passes none.
    preserving: threading.Event | None = None
    # Set once the run's outcome is fixed -- the document delivered, or the
    # run failed, was cancelled or was interrupted -- and never cleared by the
    # run: whoever passed it owns it.  From that moment an interruption can
    # only undo what the run did, by abandoning the pages half way into
    # failed/ or by turning a delivered document into a failure, so the CLI's
    # signal handler defers a SIGTERM or SIGHUP while it is set.  The worker,
    # whose stop never raises into the run, passes none.  A ``Settled``, not a
    # ``threading.Event``, because a signal can land inside the call that
    # sets it.
    settled: Settled | None = None


@dataclass
class ScanResult:
    """
    How a scan pipeline run resolved, and how many pages it moved.

    Attributes:
        outcome: Whether the document reached paperless-ngx's API.
        pages_scanned: How many pages the scanner produced.
        pages_removed: How many of them blank-page detection removed.
        pages_uploaded: How many pages the delivered document carries.
        warning: What the operator must know about a delivered document, or
            None.
        removed_positions: The 1-based scanned positions, in document order,
            of the pages blank-page detection removed -- for manual duplex,
            places in the interleaved document.  Information shown beside
            ``pages_removed``, never a warning: removing blank pages is an
            ordinary success.  Empty when nothing was removed, when detection
            is off, and on the duplex-mismatch route, which never filters.

    """

    outcome: ScanOutcome
    pages_scanned: int
    pages_removed: int
    pages_uploaded: int
    warning: str | None = None
    removed_positions: tuple[int, ...] = ()


def _observe(request: PipelineRequest, what: str, call: Callable[[], None]) -> None:
    """
    Call one observer, and never let its failure change how the run ends.

    Every status, thumbnail and pass-count callback goes through here.  An
    observer is somebody watching the run -- the web worker writing progress
    to the job store, the CLI printing a line -- and none of what it does is
    part of the scan: the same facts arrive again in ``ScanResult`` at the
    end.  Letting its failure propagate would end a run whose sheets have
    already been fed over a progress write, and file a job-store fault as a
    scanner one.  So the failure is logged at WARNING with its traceback and
    the run carries on.

    Only ``Exception`` is caught.  ``KeyboardInterrupt`` and
    ``ScanInterrupted`` pass through: they end the run on purpose.

    Args:
        request: The request, whose title the log line names.
        what: Which observer call this is, for the log line.
        call: The observer, bound to its arguments.

    """
    try:
        call()
    except Exception:
        # The title is request input, bounded only in length and never in
        # character set, so it can contain newlines -- it goes into a log line
        # with %r, which is the discipline web/errors.py's module docstring
        # states, applied here because this module logs the same class of
        # value.  %r supplies its own quoting; the format string adds none.
        logger.warning(
            "Observer failed at %s for %r; the scan continues",
            what,
            request.title,
            exc_info=True,
        )


def _note_pass_count(request: PipelineRequest, label: str, count: int) -> None:
    """
    Announce one manual-duplex pass's page count to the request's observer.

    An absent observer is the normal case, and the CLI supplies none.  The
    count is a transient number the status area shows for the length of pass
    B, so an observer that fails here is logged by ``_observe`` and the scan
    continues.

    Args:
        request: The pipeline request, whose callback is fired if it has one.
        label: ``SCAN_LABEL_FRONT`` or ``SCAN_LABEL_BACK``.
        count: How many pages that pass produced.

    """
    callback = request.pass_count_callback
    if callback is None:
        return
    _observe(
        request,
        f"the {label} pass count",
        functools.partial(callback, label, count),
    )


def _check_disk_space(tmp_dir: Path, min_free_mb: int) -> None:
    """
    Raise ScanError if insufficient disk space in tmp_dir.

    This only measures: ``_open_workspace`` creates the directory first.

    Args:
        tmp_dir: The existing temporary directory used for scanning.
        min_free_mb: Minimum free space required in megabytes.

    Raises:
        ScanError: If free space is below the required threshold.
        OSError: If the directory cannot be measured; the caller translates
            it.

    """
    usage = shutil.disk_usage(tmp_dir)
    free_mb = usage.free // (1024 * 1024)
    if free_mb < min_free_mb:
        msg = (
            f"Insufficient disk space: {free_mb} MB free in {tmp_dir}, "
            f"{min_free_mb} MB required (configure min_free_space_mb to adjust)"
        )
        raise ScanError(msg)


@contextlib.contextmanager
def _open_workspace(
    tmp_dir: Path, min_free_mb: int, request: PipelineRequest
) -> Generator[JobWorkspace]:
    """
    Create this run's job workspace under ``tmp_dir``, checking for room.

    The workspace is a ``JobWorkspace``: named ``job-<id>-...`` after the job
    and locked for as long as the run holds it, so a sweep of ``tmp_dir``
    can tell a live run's workspace from one a killed process left behind.
    It is removed when the ``with`` block ends, however it ends, unless the
    run called its ``keep`` because pages in it could not be kept elsewhere.

    ``tmp_dir`` is created 0700 when missing and refused when it is not
    private (see ``saneless.private_dirs``); both failures are a
    ``ConfigError`` naming ``output.tmp_dir``.  The other two steps can raise a
    raw ``OSError`` -- a full disk, say.  That is the setup problem
    ``validate_settings_dirs`` reports at start-up, so it is a ``ConfigError``
    here too, not an UNKNOWN error the CLI would call a saneless bug.
    Only the workspace's creation is guarded: an ``OSError`` from the scan run
    inside it keeps its own type.

    Args:
        tmp_dir: The configured directory for temporary files.
        min_free_mb: Minimum free space required in megabytes.
        request: The run's request, whose job id, title and profile name the
            workspace records.

    Yields:
        The entered workspace, whose ``path`` is the directory, with its
        ``spool`` subdirectory created.

    Raises:
        ConfigError: If ``tmp_dir`` cannot be created or measured, or the
            workspace cannot be created in it; names ``tmp_dir``.
        ScanError: If free space is below ``min_free_mb``.

    """
    with contextlib.ExitStack() as stack:
        try:
            # Re-checked before every scan, not only at startup: a
            # temp-directory sweep can remove tmp_dir while the server runs,
            # and another local user can then create the name.  A refusal is a
            # ConfigError, which is not an OSError, so it passes the handler
            # below unwrapped.
            ensure_private_dir(tmp_dir, key="output.tmp_dir")
            _check_disk_space(tmp_dir, min_free_mb)
            workspace = JobWorkspace(
                tmp_dir,
                job_id=request.job_id,
                title=request.title,
                profile=request.profile_name,
            )
            stack.enter_context(workspace)
        except OSError as exc:
            msg = f"Could not prepare the working directory {tmp_dir}: {describe(exc)}"
            raise ConfigError(msg) from exc
        yield workspace


def _require_pages(batch: ScanBatch) -> None:
    """
    Raise ScanError if a scanner pass came back with no pages at all.

    This is the pipeline's own contract check against any ``ScannerBackend``.
    Without it an empty batch was misreported as "all pages
    blank" with empty-page detection on, and leaked img2pdf's bare
    ``ValueError`` with detection off or on an empty manual-duplex half.

    It never pre-empts the backend's truthful feeder message: the SANE
    backend raises its own, more specific ``FeederEmptyError`` for an empty
    feeder, or an all-unreadable ``ScanError``, before it ever returns a
    batch. What it guarantees is that ``_drop_blank_pages`` and
    ``assemble_pdf`` never see an empty list.

    Args:
        batch: The batch one ``scan_pages`` call returned.

    Raises:
        ScanError: If the batch carries no pages.

    """
    if not batch.pages:
        msg = "No pages were scanned"
        raise ScanError(msg)


# How a kept PDF reached paperless-ngx when the run was interrupted while it
# was being sent: the request left, and no answer said whether it landed.
_MAY_HAVE_ARRIVED: Final = (
    "was being sent to paperless-ngx when the scan was interrupted and may have arrived"
)


def _accepted_how(upload: UploadResult) -> str:
    """
    Say how paperless-ngx took an uploaded PDF, for the kept copy's caution.

    Args:
        upload: What the upload did.

    Returns:
        The words that follow the kept file's name.

    """
    if upload.task_uuid is not None:
        return (
            f"had already been accepted by paperless-ngx as task "
            f"{upload.task_uuid}, which had not confirmed it was consumed"
        )
    return "had already been saved to the paperless-ngx consume folder"


def _unlink_pages(records: Sequence[PageRecord]) -> None:
    """
    Delete the page files of a pass that was thrown away.

    A thrown-away pass must leave no file behind: the guard, on a failure, and
    the startup sweep, after a crash, both move every page file they find into
    ``failed/``, so a leftover page would come back as a pass the operator
    threw away.  A file already gone, or one that cannot be removed, does not
    stop the rest from going; one that cannot be removed is logged with its
    cause, so a page that later turns up in ``failed/`` can be traced back to
    the pass it was thrown away with.

    Args:
        records: The pass's page records, whose files are removed.

    """
    for record in records:
        try:
            record.path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "Could not delete %s from a discarded pass; it may reappear in failed/",
                record.path.name,
                exc_info=True,
            )


@dataclass
class _SpoolLedger:
    """
    What each acquisition pass has spooled so far, for the preservation guard.

    Mutable, and deliberately so. The run's guard reads it when a pass fails
    part way, and a frozen record could only be built after the pass
    returned, which is precisely the case where there is nothing to preserve.
    The pass list stays behind methods, so a later step that has to discard a
    pass can be given one here rather than reach into the list.

    Each pass registers itself **before** it starts, so a fault part-way
    through it still finds the sink that has been collecting its pages.

    There is no resolution here: each spooled page's record carries the dpi
    the device read back for it, so a pass interrupted before it returned a
    batch is still preserved at the device's resolution, not the profile's.

    Attributes:
        passes: One ``(title suffix, sink)`` pair per acquisition pass, in
            pass order. The suffix is what the preserved PDF's name says the
            half is: ``(partial)``, ``(fronts)`` or ``(backs)``.
        unreadable_sheets: How many sheets the passes that returned a batch
            reported they could not read, so a kept pair of halves is not
            said to pair by page number when they cannot.

    """

    passes: list[tuple[str, SpooledPageSink]] = field(default_factory=list)
    unreadable_sheets: int = 0

    def register(self, suffix: str, sink: SpooledPageSink) -> None:
        """
        Record a pass that is about to start, under the name it would be kept as.

        Args:
            suffix: The bracketed marker the preserved PDF's title carries.
            sink: The sink this pass spools into.

        """
        self.passes.append((suffix, sink))

    def forget(self, sink: SpooledPageSink) -> None:
        """
        Stop tracking an accepted pass, and leave its page files where they are.

        The pages now belong to the document, which the guard keeps on its
        own; tracking them here as well would keep them twice.

        Args:
            sink: The sink the accepted pass spooled into.

        """
        self.passes = [
            (suffix, kept) for suffix, kept in self.passes if kept is not sink
        ]

    def discard(self, sink: SpooledPageSink) -> None:
        """
        Stop tracking a thrown-away pass, then delete its page files.

        In that order, as ``_discard_last_pass`` does: a signal raises on the
        main thread wherever it is, and landing in between it can then leave
        at worst orphan files, never an entry the guard would try to keep
        from pages that are already gone.  The files go through
        ``_unlink_pages``, which says why none may be left behind.

        Args:
            sink: The sink the discarded pass spooled into.

        """
        self.forget(sink)
        _unlink_pages(sink.records)

    def spooled(self) -> list[tuple[str, tuple[PageRecord, ...]]]:
        """
        Return the passes that actually put pages on the spool.

        An empty pass is dropped rather than preserved as a zero-page PDF:
        ``assemble_pdf`` cannot build one, and a pass B that fed nothing is
        exactly the case that is answered by keeping the fronts alone.

        Returns:
            One ``(title suffix, records)`` pair per non-empty pass, in pass
            order.

        """
        return [(suffix, sink.records) for suffix, sink in self.passes if sink.records]

    def page_count(self) -> int:
        """
        Return how many pages reached the spool across every pass.

        Returns:
            The total, which is zero when there is nothing to preserve.

        """
        return sum(len(sink.records) for _, sink in self.passes)


def _drop_blank_pages(
    pages: Sequence[PageRecord],
    profile: ProfileConfig,
) -> BlankFilterResult:
    """
    Drop blank pages when the profile enables empty-page detection.

    Records in, records out. The judgement is made from the ink coverage and
    paper white each record already carries, measured once when the page was
    spooled, against the profile's ``empty_page_coverage_threshold``; nothing
    here re-opens a page file, and nothing here deletes or copies one.

    Args:
        pages: The scanned page records, in document order.
        profile: The profile whose toggle and threshold apply.

    Returns:
        The records to assemble and the 1-based positions removed: filtered
        when detection is on, every record and no positions when it is off.

    Raises:
        AllPagesBlankError: If every page of a non-empty batch was detected as
            blank.  Its own type, not a ``ScanError``: the scanner did nothing
            wrong, and the advice is to tune detection.  The input is never
            empty: ``_require_pages`` has already refused an empty batch with
            ``No pages were scanned``.

    """
    if not profile.enable_empty_page_detection:
        logger.info("Empty page detection disabled for profile")
        return BlankFilterResult(kept=list(pages), removed_positions=())

    result = filter_blank_pages(
        pages, coverage_threshold=profile.empty_page_coverage_threshold
    )
    if result.removed_positions:
        logger.info(
            "Blank-page filter: %d -> %d pages (removed pages %s)",
            len(pages),
            len(result.kept),
            ", ".join(str(position) for position in result.removed_positions),
        )
    if not result.kept:
        msg = (
            f"All {len(pages)} page(s) looked blank to empty-page detection, "
            "so nothing was uploaded"
        )
        raise AllPagesBlankError(msg)
    return result


def _consume_dir_warning(destination: Path | None) -> str:
    """
    Describe what a consume-directory delivery cost the document.

    A fallback is recorded in the FALLBACK state *with* a warning, because the
    two halves carry different information: the state says
    the document took the other route, and the warning says what that route
    did not do.  ``docs/explanation/consume-directory-fallback.md`` documents
    the same consequence -- paperless-ngx applies its own matching rules to a
    file it finds in the consume directory, so the title, tags and
    correspondent chosen for this scan are not applied to it.

    Nothing was lost and no rescan is needed, so the register is deliberately
    a warning rather than an error: the web UI renders it amber beside
    "Saved to folder", not red beside "Failed".

    Args:
        destination: Where the PDF was written.
            ``UploadResult.__post_init__`` guarantees this for every delivery
            that did not reach the API; the ``None`` arm exists only because
            the field is typed optional, and a warning that reaches the user
            must never be empty or read "None".

    Returns:
        The warning text recorded on the job and rendered in the status area.

    """
    where = f" at {destination}" if destination is not None else ""
    return (
        f"Saved to the paperless-ngx consume directory{where} instead of "
        "uploading through the API, so the title, tags and correspondent "
        "chosen for this scan were not applied -- paperless-ngx will apply "
        "its own matching rules to the file instead."
    )


@dataclass(frozen=True)
class _DuplexMismatch:
    """
    The two passes of a manual duplex run that cannot be paired by position.

    Either the page counts disagreed, or a pass could not read a sheet. After a
    lost sheet, equal counts prove nothing: when each pass loses a different
    sheet, the counts match and the interleave pairs fronts with the wrong
    backs.

    A named record rather than the bare ``(fronts, backs)`` tuple this used to
    be. The recovery path also needs the sheets the device could not read, and
    a tuple would make every call site remember an order. The resolution is
    not here: each record carries the dpi its page was read back at.

    Attributes:
        fronts: Page records produced by pass A, in pass order.
        backs: Page records produced by pass B, in pass order.
        unreadable_sheets: Sheets skipped across both passes for failing their
            integrity checks. Nonzero is the reason the run was split. Zero
            means the split is a count difference alone.

    """

    fronts: Sequence[PageRecord]
    backs: Sequence[PageRecord]
    unreadable_sheets: int


@dataclass(frozen=True)
class _AcceptedPass:
    """
    One pass a multi-page document accepted, as Re-scan needs it undone.

    Attributes:
        sink: The sink the pass spooled into, whose files a Re-scan deletes.
        pages: How many pages the pass added to the document, kept and
            skipped together: the last ``pages`` records are this pass's.
        kept: How many of them were kept.
        rejected: How many fed sheets the pass could not read.

    """

    sink: SpooledPageSink
    pages: int
    kept: int
    rejected: int


@dataclass
class _MultiPageDocument:
    """
    A multi-page document as it grows, pass by pass: the loop's mutable state.

    Its own record rather than locals of the loop, so the pass loop's steps can
    be separate methods and each stay within ruff's complexity limits.

    Attributes:
        records: Every accepted page, kept and skipped, in scan order.  The
            positions below number this list, which is also what a failed run
            keeps as the unfiltered document.
        removed: The 1-based positions in ``records`` of pages skipped as blank.
        passes: Every accepted pass, in scan order.
        unreadable: How many fed sheets the accepted passes could not read.
        warning: Why the document finished without the operator pressing
            Finish, or None when they did.
        prompts: The number of the last prompt put to the operator.
        started: How many passes have been started, thrown-away ones
            included.

    """

    records: list[PageRecord] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)
    passes: list[_AcceptedPass] = field(default_factory=list)
    unreadable: int = 0
    warning: str | None = None
    prompts: int = 0
    started: int = 0

    @property
    def kept(self) -> int:
        """How many pages the document holds: accepted and not skipped."""
        return len(self.records) - len(self.removed)

    def number_prompt(self) -> int:
        """
        Give the next prompt its number, one more than the last.

        Returns:
            The new prompt's 1-based number within the run.

        """
        self.prompts += 1
        return self.prompts

    def start_pass(self) -> int:
        """
        Give the next pass its number, one more than the last one started.

        Thrown-away passes keep their numbers, so no two passes of a run ever
        share one, nor the spool label made from it.

        Returns:
            The new pass's 1-based number within the run.

        """
        self.started += 1
        return self.started


def _duplex_resolution(front: ScanBatch, back: ScanBatch) -> int:
    """
    Reconcile the resolution the two manual-duplex passes reported.

    Both passes run with identical settings against one device, so the two
    values should be identical in practice. If they are not, the device changed
    its mind mid-job, and that is a fact worth saying out loud rather than
    resolving silently.

    Nothing has to be chosen for the PDF any more: each page's record carries
    the dpi its own pass read back, and assembly lays every page out at its
    own, so a disagreement costs no page its real size. Failing the run
    instead would throw away a scan that completed, over a disagreement the
    crop fallback already tolerates. The one place a single value is still
    needed is the interleaved batch's ``actual_resolution``, and pass A's is
    used there.

    Args:
        front: The batch pass A produced.
        back: The batch pass B produced.

    Returns:
        The resolution the interleaved batch reports: pass A's.

    """
    if front.actual_resolution != back.actual_resolution:
        logger.warning(
            "Manual duplex passes disagree on resolution: pass A reports %s dpi, "
            "pass B reports %s dpi; each page is laid out at its own pass's",
            front.actual_resolution,
            back.actual_resolution,
        )
    return front.actual_resolution


def _rejected_pages_warning(count: int) -> str | None:
    """
    Describe sheets the scanner could not read, and log them, if there were any.

    Worded so it cannot be mistaken for blank-page removal. The pipeline's blank
    count is empty-page detection, which the web UI shows users as pages
    removed for being blank; a sheet that failed its integrity checks is a
    different event with a different remedy, and the two must not be conflated.

    This is the only channel the count has. ``pages_scanned`` is the length of
    the pages that arrived, which already excludes a skipped sheet, so a
    ten-sheet stack with one unreadable page reports nine and nobody learns a
    page was lost. It logs as it builds, as the duplex-mismatch delivery
    likewise logs the warning it gives.

    Args:
        count: How many sheets the backend skipped.

    Returns:
        The warning text, or None when nothing was rejected.

    """
    if count <= 0:
        return None
    warning = (
        f"{count} page(s) could not be read by the scanner and were skipped. "
        f"They were not removed for being blank; rescan those sheets."
    )
    logger.warning(warning)
    return warning


def _join_warnings(*parts: str | None) -> str | None:
    """
    Combine into the single field that carries them everything a run has to say.

    ``ScanResult`` has one warning field and a run can have more than one thing
    to report: a consume-directory fallback and an unreadable sheet are
    independent events that can both happen. Letting either overwrite the other
    would silently drop something the operator needed to know.

    Args:
        parts: The candidate warnings, any of which may be None.

    Returns:
        The non-empty warnings joined by a space, or None if there were none.

    """
    present = [part for part in parts if part]
    return " ".join(present) if present else None


def _duplex_mismatch_warning(mismatch: _DuplexMismatch) -> str:
    """
    Say why the two manual-duplex passes were delivered as two PDFs.

    A lost sheet takes precedence over the counts: when each pass lost a
    different sheet the counts agree, and a "page count mismatch" sentence
    quoting two equal numbers would be false. It is the only sentence that
    mentions the lost sheets, so the generic unreadable-sheet warning is not
    added on top of it.

    Args:
        mismatch: Both passes, and the sheets the scanner could not read.

    Returns:
        The warning, naming the unreadable sheets when there were any and
        otherwise the two page counts.

    """
    count = mismatch.unreadable_sheets
    if count > 0:
        sheets = "1 sheet" if count == 1 else f"{count} sheets"
        return (
            f"The scanner could not read {sheets}, so the fronts and backs "
            f"could not be paired reliably; they were uploaded as two PDFs, "
            f"{preservation.FRONTS_SUFFIX} and {preservation.BACKS_SUFFIX}, for manual review."
        )
    return (
        f"Page count mismatch: {len(mismatch.fronts)} fronts, "
        f"{len(mismatch.backs)} backs. Partial PDFs saved."
    )


def _interleave_duplex(
    fronts: Sequence[PageRecord],
    backs: Sequence[PageRecord],
) -> list[PageRecord]:
    """
    Interleave front and back pages for manual duplex.

    Backs are reversed because the user flips the stack face-down,
    so the last front's back is scanned first in pass B.

    Records are reordered, never files. Nothing is renamed, moved or rewritten
    on the spool: after this runs, the ``a-`` and ``b-`` file names no longer
    sort into document order at all, and that is precisely why document order
    is the order of this list and never the directory's.

    Args:
        fronts: Front-side records from pass A.
        backs: Back-side records from pass B (in scan order).

    Returns:
        Interleaved records: A1, B1, A2, B2, ...

    Raises:
        ScanError: If front and back page counts do not match.

    """
    if len(fronts) != len(backs):
        msg = f"Page count mismatch: {len(fronts)} fronts, {len(backs)} backs"
        raise ScanError(msg)
    backs_reversed = list(reversed(backs))
    result: list[PageRecord] = []
    for front, back in zip(fronts, backs_reversed, strict=True):
        result.append(front)
        result.append(back)
    return result


def _flip_context(request: PipelineRequest, settings: Settings) -> _FlipContext:
    """
    Build the flip context for a manual-duplex run, refusing one with no coordinator.

    Called only by ``run_pipeline``, only for a ``duplex = "manual"`` profile, and
    before any scanner contact -- see the comment at the call site for why that
    placement matters.

    Args:
        request: The pipeline request, which must carry a flip coordinator.
        settings: Application settings, for
            ``output.operator_wait_timeout_seconds``.

    Returns:
        The coordinator, narrowed to non-Optional, with the configured timeout.

    Raises:
        ConfigError: If the request carries no flip coordinator.

    """
    if request.flip_coordinator is None:
        # Refusing is the only safe answer: with no coordinator, pass B would
        # start the instant pass A ends and re-feed an empty tray.
        # Starting anyway would turn a bad request into lost pages.
        msg = (
            f"Profile '{request.profile_name}' is manual duplex, which needs a "
            "flip coordinator to tell saneless when the stack has been turned "
            "over, and none was supplied"
        )
        raise ConfigError(msg)
    coordinator = request.flip_coordinator
    return _FlipContext(
        coordinator=coordinator,
        timeout=settings.output.operator_wait_timeout_seconds,
    )


def _multi_page_context(
    request: PipelineRequest, profile: ProfileConfig, settings: Settings
) -> _MultiPageContext | None:
    """
    Build the context a multi-page run asks its questions with, or refuse it.

    Called only by ``run_pipeline``, before any scanner contact, for the same
    reason ``_flip_context`` is: a request that cannot run must never touch
    the device.  The web form and the CLI refuse both combinations first;
    this is the last line, so a request that reached the pipeline some other
    way is refused just the same.

    Args:
        request: The pipeline request.
        profile: The request's scan profile.
        settings: Application settings, for
            ``output.operator_wait_timeout_seconds``.

    Returns:
        None for a single-pass request; otherwise the coordinator, narrowed to
        non-Optional, with the configured timeout.

    Raises:
        ConfigError: If a multi-page request names a manual-duplex profile, or
            carries no pass coordinator.

    """
    if not request.multi_page:
        return None
    if profile.duplex == "manual":
        # Each pass of a manual-duplex scan is itself two passes and a flip,
        # and nothing here knows how a "next page" would fit between them.
        msg = (
            f"Profile '{request.profile_name}' is manual duplex, and a "
            "multi-page scan is not available with manual duplex"
        )
        raise ConfigError(msg)
    if request.pass_coordinator is None:
        # With no coordinator nothing could answer the first prompt, and
        # scanning anyway would mean deciding for the operator.
        msg = (
            f"A multi-page scan with profile '{request.profile_name}' needs a "
            "pass coordinator to ask whether there is another page, and none "
            "was supplied"
        )
        raise ConfigError(msg)
    return _MultiPageContext(
        coordinator=request.pass_coordinator,
        timeout=settings.output.operator_wait_timeout_seconds,
    )


# The two ways a multi-page wait can end that no prompt offers, because they
# are not the operator's choice: the clock running out, and saneless stopping.
_ENDINGS_NOBODY_OFFERS: Final = frozenset(
    {PassAnswer.TIMED_OUT, PassAnswer.INTERRUPTED}
)


# The answers the prompt after a failed pass offers.  No Re-scan: the failed
# pass contributed nothing, so there is no last pass of its own to throw away,
# and Scan next is already the try-again.
_RETRY_ANSWERS: Final = frozenset(
    {PassAnswer.NEXT, PassAnswer.FINISH, PassAnswer.ABORT}
)


# The answers the prompt about a pass's blank pages offers: throw the whole
# pass away and scan it again, or take it without the pages that look blank,
# or with them.  Only those three: the question is about this pass alone, and
# Finish and Abort are left to the next-pass prompt that follows a Skip or a
# Keep.
_BLANK_ANSWERS: Final = frozenset(
    {PassAnswer.RESCAN, PassAnswer.SKIP_BLANKS, PassAnswer.KEEP_BLANKS}
)


def _returns_to_prompt(exc: Exception) -> bool:
    """
    Say whether a failed pass of a multi-page document is worth asking about.

    True for ``ErrorCategory.SCANNER`` and ``ErrorCategory.FEEDER`` only, and
    never for a ``SpoolError``.  A device fault or an empty feeder is one the
    operator can put right -- clear a jam, close a cover, load the next sheet
    -- and then try the pass again.  A ``SpoolError`` is a full disk, raised
    as a ``ScanError``, and the same pass would fail the same way at once.
    The other categories -- configuration, assembly, upload, and anything
    unclassified, a bug among them -- are not the scanner's, so trying the
    pass again cannot help.

    The caller also requires a page to be kept: with none there is nothing a
    return to the prompt would protect, and the failure ends the job as a
    single-pass failure does.

    Args:
        exc: What the pass raised.

    Returns:
        True if the run should go back to the operator instead of failing.

    """
    category = classify_error(exc)
    returnable = category in {ErrorCategory.SCANNER, ErrorCategory.FEEDER}
    return returnable and not isinstance(exc, SpoolError)


def _unoffered_answer(answer: PassAnswer, number: int) -> ScanError:
    """
    Build the failure for a coordinator that answered off the prompt's menu.

    A ``ScanError`` rather than obedience: the offered set is how a stale or
    forged answer is refused, so an answer outside it is a broken coordinator,
    and nobody chose it.  The guard keeps the accepted pages.

    Args:
        answer: What the coordinator answered.
        number: The prompt it answered.

    Returns:
        The error to raise, naming the answer and the prompt.

    """
    msg = (
        f"The multi-page coordinator answered {answer.value} to prompt "
        f"{number}, which did not offer it"
    )
    return ScanError(msg)


def _resolve_device(
    scanner: ScannerBackend,
    settings: Settings,
    memory: DeviceMemory | None = None,
) -> str:
    """
    Resolve the scanner device ID from settings or auto-detection.

    An auto-detected device that differs from the one ``memory`` recorded is
    logged at WARNING, naming both: the scan still runs, on the new device,
    because refusing would stop a household scanning over a warning.  The
    ids are logged with ``%r`` since discovery supplies them.

    Args:
        scanner: Scanner backend instance.
        settings: Application settings.
        memory: The worker's record of its last auto-detected device, updated
            here; None when there is nothing to compare with.

    Returns:
        SANE device identifier string.

    Raises:
        ConfigError: If no device is configured and auto-detection finds none.

    """
    device_id = settings.scanner.device
    if device_id:
        return device_id
    devices = scanner.get_devices()
    if not devices:
        msg = "No scanner found: settings.scanner.device is empty and auto-detection found no devices"
        raise ConfigError(msg)
    chosen = devices[0].name
    previous = memory.last_id if memory is not None else None
    if previous is not None and previous != chosen:
        logger.warning(
            "The auto-detected scanner changed from %r to %r; "
            "set [scanner] device to pin one",
            previous,
            chosen,
        )
    else:
        logger.info("Auto-detected scanner: %r", chosen)
    if memory is not None:
        memory.last_id = chosen
    return chosen


@dataclass(kw_only=True)
class _PipelineRun:
    """
    One scan run: what it has produced so far, and the one guard over all of it.

    Every step of a run -- acquisition, the blank-page filter, assembly, the
    upload and the poll -- is a method here, and ``execute`` runs them all
    inside a single ``try``.  That one handler is the whole of saneless's
    promise that a scanned page is either delivered or kept: nothing between
    two steps, no status update and no new step added later, can fall outside
    it, because there is no "between" left.  It replaced four separate guard
    windows whose gaps were exactly where pages used to be lost.

    Before each step the run records its ``stage`` on ``artefacts``, and each
    step records what it produced (the spooled passes, the unfiltered
    document, the assembled PDFs), so the guard always knows the most finished
    thing there is to keep.  ``preservation.preserve_most_finished`` decides
    what that is.

    Every observer the request carries -- the status, thumbnail and
    pass-count callbacks -- is called through ``_observe``, which logs a
    failure and carries on.  An observer is somebody watching the run, so its
    fault can never end one.

    A dataclass for its constructor alone: the run needs eleven values, and a
    hand-written ``__init__`` taking them would break ruff's ``PLR0913``
    argument limit, which this project neither raises nor suppresses.  Every
    one is keyword-only.

    Attributes:
        scanner: The scanner backend.
        paperless: The paperless-ngx client.
        settings: Application settings.
        request: The request being run.
        profile: The request's scan profile.
        device_id: The SANE device every pass scans through.
        scan_settings: The scan settings every pass runs with.
        flip: The flip coordinator and its timeout for a manual-duplex run;
            None for every other run.
        multi_page: The pass coordinator and its timeout for a multi-page
            run; None for every other run.
        workspace: The job's locked workspace directory, which holds the
            spool and the assembled PDFs and is removed when the run ends.
        keep_workspace: Leaves the workspace in place when the run ends, for
            the next sweep to recover; called when a failure's pages could
            not all be kept in ``failed/``.  Returns whether a sweep will
            recover it, which it will not for a workspace used unlocked.
        ledger: What each acquisition pass has spooled; built here.
        artefacts: What the run has produced and how far it got; built here.

    """

    scanner: ScannerBackend
    paperless: PaperlessClient
    settings: Settings
    request: PipelineRequest
    profile: ProfileConfig
    device_id: str
    scan_settings: ScanSettings
    flip: _FlipContext | None
    multi_page: _MultiPageContext | None
    workspace: Path
    keep_workspace: Callable[[], bool]
    ledger: _SpoolLedger = field(init=False)
    artefacts: preservation.RunArtefacts = field(init=False)

    def __post_init__(self) -> None:
        """Start the ledger and the artefacts before anything has been scanned."""
        self.ledger = _SpoolLedger()
        self.artefacts = preservation.RunArtefacts(
            job_id=self.request.job_id,
            title=self.request.title,
            workspace=self.workspace,
            spool_dir=self.spool_dir,
            failed_dir=self.settings.output.failed_dir,
            reserve_mb=self.settings.output.min_free_space_mb,
        )

    @property
    def spool_dir(self) -> Path:
        """
        The directory inside the workspace that holds the page files.

        ``JobWorkspace`` creates it with the workspace.  A subdirectory rather
        than the workspace itself, so the assembled PDFs are not written among
        the pages.

        Returns:
            The spool directory.

        """
        return self.workspace / SPOOL_DIR_NAME

    def execute(self) -> ScanResult:
        """
        Run the scan, keeping the most finished artefact if anything fails.

        This is the one preservation handler in the pipeline.  A failure at
        any point is handed to ``preservation.preserve_most_finished`` along
        with how far the run got, and the one sentence it returns -- naming
        what was kept and where, or that nothing could be -- is attached to
        the failure as a note.  The failure is then re-raised as itself: the
        same object, with its own type, attributes and traceback, so a
        ``TypeError`` from our own code is still a ``TypeError`` and a
        ``PaperlessTimeoutError`` is still that and not a plain
        ``PaperlessError``.  ``failure_text`` is how every surface renders the
        failure with its note.

        Returns:
            How the run resolved.

        Raises:
            ScanCancelledError: If the operator cancelled at the flip prompt
                or at a multi-page prompt; nothing is kept.
            ScanInterrupted: If a signal or a server shutdown interrupted the
                run; what it had is kept, and the note says where.
            Exception: Any other failure, re-raised as itself with the note.

        """
        # Every arm settles the run before anything else, so a signal arriving
        # while the pages are kept, or while the workspace is removed, cannot
        # abandon either half way.  A signal still pending as the failure
        # arrived lands at the settling call's own entry, before the run is
        # settled, and so is raised: each arm's first call is therefore inside
        # a try that absorbs it.  Nothing between the except clause matching
        # and that call gives a signal handler a chance to run.
        try:
            return self._run()
        except ScanCancelledError:
            # A cancel keeps nothing, and it is checked first on purpose.
            # ScanCancelledError is an ordinary Exception -- a direct
            # SanelessError child rather than a ScanError -- so the handler
            # below would otherwise file a scan the operator chose to abandon
            # into a directory saneless never prunes.  Nobody asked for those
            # pages to be kept, so they are not.
            try:
                self._settle()
            except ScanInterrupted as late:
                self._absorb(late)
            raise
        except (Exception, ScanInterrupted) as exc:
            # ScanInterrupted is a BaseException so that nothing on the way up
            # can re-type it, and it is caught here because an interruption is
            # not anyone's decision to stop: the pages are kept.
            try:
                self._settle()
            except ScanInterrupted as late:
                self._absorb(late)
            # The ledger knows what each pass spooled, and each record the
            # dpi it was read back at; the artefacts are what the
            # preservation reads.
            self.artefacts.passes = self.ledger.spooled()
            self.artefacts.unreadable_sheets = self.ledger.unreadable_sheets
            report = self._preserve()
            sentence = report.sentence()
            if sentence is not None:
                exc.add_note(sentence)
            if report.problems and has_pages_left(self.spool_dir):
                # Removing the workspace now would delete the only copy of
                # the pages the preservation could not move, so it stays for
                # the next sweep, which retries into failed/.
                if self.keep_workspace():
                    exc.add_note(
                        f"The pages that could not be kept are left in "
                        f"{self.spool_dir}, and are moved into failed/ the "
                        f"next time saneless serve starts or saneless scan runs"
                    )
                else:
                    exc.add_note(
                        f"The pages that could not be kept are left in "
                        f"{self.spool_dir}, which no sweep recovers because "
                        f"it could not be locked: move them into failed/ "
                        f"yourself"
                    )
            # A bare raise, never a rebuilt exception: the original object,
            # its type, attributes and traceback all survive, and
            # classify_error sees what really happened.
            raise
        except BaseException:
            # KeyboardInterrupt, above all: Ctrl-C is the operator's cancel,
            # and keeps nothing either.
            try:
                self._settle()
            except ScanInterrupted as late:
                self._absorb(late)
            raise

    def _settle(self) -> None:
        """Mark the run's outcome as fixed, for whoever passed ``settled``."""
        settled = self.request.settled
        if settled is not None:
            settled.set()

    def _absorb(self, late: ScanInterrupted) -> None:
        """
        Let the run's own outcome stand over a signal that landed as it settled.

        The signal handler ignores both signals before it raises, so no
        second one can follow this, and the run is settled here after all.
        Setting it again is safe because ``Settled`` holds no lock that the
        interrupted attempt could have left taken.

        Args:
            late: The interruption raised as the run settled.

        """
        logger.info(
            "%s arrived as the run's outcome was settled, so the run finishes "
            "with its own outcome",
            late,
        )
        self._settle()

    def _delivered(self) -> None:
        """
        Record that the document was delivered, which settles the run.

        Settled first, and the stage recorded only afterwards: a signal that
        lands as the run settles is absorbed, because the document is already
        delivered, and nothing may leave the run failing at stage DELIVERED,
        which keeps nothing.
        """
        try:
            self._settle()
        except ScanInterrupted as late:
            self._absorb(late)
        self.artefacts.stage = preservation.RunStage.DELIVERED

    def _preserve(self) -> preservation.PreservationReport:
        """
        Keep the most finished artefact, flagging the request while it runs.

        ``request.preserving`` is set for exactly as long as the pages are
        being kept, and cleared however that ends, so a stopping server
        waits for them only while there is something to wait for.

        Returns:
            What was kept and what went wrong.

        """
        preserving = self.request.preserving
        if preserving is not None:
            preserving.set()
        try:
            return preservation.preserve_most_finished(self.artefacts)
        finally:
            if preserving is not None:
                preserving.clear()

    def _run(self) -> ScanResult:
        """
        Acquire, then deliver the document, or both halves of a mismatch.

        Returns:
            How the run resolved.

        """
        self.artefacts.stage = preservation.RunStage.ACQUIRING
        self._notify(PipelineEvent.SCANNING)
        logger.info(
            "Scanning with profile %r on device %r",
            self.request.profile_name,
            self.device_id,
        )
        acquired = self._acquire()
        # A match with assert_never, not a dict or an isinstance chain, on
        # purpose: a variant missing from a dict draws no diagnostic from
        # either ty or pyrefly, while the same omission in a match is caught by
        # both, at edit time, before a new result type can fall silently
        # through an else.
        match acquired:
            case ScanBatch():
                return self._finish_document(acquired)
            case _DuplexMismatch():
                # Deliberately skips the blank-page filter -- see
                # _finish_mismatch for why.
                return self._finish_mismatch(acquired)
            case _MultiPageDocument():
                # Also skips the automatic blank-page filter -- see
                # _finish_multi_page for why.
                return self._finish_multi_page(acquired)
            case _:
                assert_never(acquired)

    def _acquire(self) -> ScanBatch | _DuplexMismatch | _MultiPageDocument:
        """
        Run whichever acquisition strategy this run's request asked for.

        ``run_pipeline`` never builds both contexts: a multi-page request on a
        manual-duplex profile is refused before the run exists.

        Returns:
            What acquisition produced.

        """
        if self.multi_page is not None:
            return self._scan_multi_page(self.multi_page)
        if self.flip is None:
            return self._scan_simplex()
        return self._scan_manual_duplex(self.flip)

    def _notify(self, event: PipelineEvent) -> None:
        """
        Report a progress event to the request's status observer, if any.

        Args:
            event: The event to report.

        """
        callback = self.request.status_callback
        if callback is not None:
            _observe(self.request, event.value, functools.partial(callback, event))

    def _thumbnail(self, thumbnail: str) -> None:
        """
        Hand the first page's thumbnail to the request's observer.

        Args:
            thumbnail: The base64 JPEG thumbnail.

        """
        callback = self.request.thumbnail_callback
        if callback is not None:
            _observe(
                self.request, "the thumbnail", functools.partial(callback, thumbnail)
            )

    def _thumbnail_observer(self) -> Callable[[str], None] | None:
        """
        Return what the first pass's sink should call with the thumbnail.

        None when nobody is watching, so the sink does not make a thumbnail
        nobody will see.

        Returns:
            The best-effort thumbnail observer, or None.

        """
        if self.request.thumbnail_callback is None:
            return None
        return self._thumbnail

    def _sink(
        self, label: str, thumbnail: Callable[[str], None] | None
    ) -> SpooledPageSink:
        """
        Build the sink one acquisition pass spools into.

        Args:
            label: The pass's file-name prefix, ``_SPOOL_LABEL_A`` or
                ``_SPOOL_LABEL_B``.
            thumbnail: What the sink calls with the first page's thumbnail, or
                None.

        Returns:
            A sink over this run's spool directory.

        """
        return SpooledPageSink(
            directory=self.spool_dir,
            pass_label=label,
            min_free_space_mb=self.settings.output.min_free_space_mb,
            thumbnail_callback=thumbnail,
        )

    def _scan_simplex(self) -> ScanBatch:
        """
        Perform a simplex / hardware duplex / flatbed scan.

        The one pass spools under the ``a`` label, the same label a manual-duplex
        job's fronts use, so a spool directory reads the same way whichever route
        produced it.

        The thumbnail is not generated here. It rides on the sink and fires while
        the first page is being spooled, which is both cheaper -- the page is in
        memory at that moment, rather than needing a second decode afterwards --
        and visibly earlier, since the strip appears during acquisition instead of
        after the last sheet.

        Returns:
            The batch the device produced: its page records, the resolution it
            actually used, and how many fed sheets it could not read.

        Raises:
            ScanError: ``No pages were scanned`` if the backend returned no pages.

        """
        sink = self._sink(_SPOOL_LABEL_A, self._thumbnail_observer())
        # Registered before the pass runs, not after it returns: the whole point is
        # the case where it never returns, and a fault part-way through has to find
        # the sink that has been collecting pages all along.
        self.ledger.register(preservation.PARTIAL_SUFFIX, sink)
        batch = self.scanner.scan_pages(self.device_id, self.scan_settings, sink)
        _require_pages(batch)
        logger.info("Scanned %d page(s)", len(batch.pages))
        return batch

    def _scan_manual_duplex(self, flip: _FlipContext) -> ScanBatch | _DuplexMismatch:
        """
        Perform a two-pass manual duplex scan with flip coordination.

        Between the passes the run waits on ``flip.coordinator``, bounded by
        ``flip.timeout``, so a forgotten flip prompt fails this job instead of
        parking the single worker thread forever.

        Each pass gets its own sink, labelled ``a`` for the fronts and ``b`` for
        the backs, so the two passes spool into names that can be told apart while
        sharing one directory. That is for debuggability only: document order comes
        from the record list, never from those names. Only pass A's sink
        carries the thumbnail callback, so the strip shows the first front and
        fires exactly once per job.

        Both sinks register themselves with the ledger before their pass
        starts, so every ending below that is not the operator's own decision
        keeps whatever reached the spool: a fault in either pass, an empty
        pass B, a flip-wait timeout and a broken flip prompt all leave pass A's
        fronts -- and any backs already fed -- as the two separately named
        partial PDFs the mismatch recovery also produces. The exception itself
        is unchanged here; ``execute``'s guard is what attaches the count and
        the destination to it.

        Args:
            flip: The flip coordinator and the timeout bounding its wait.

        Returns:
            A ScanBatch of interleaved pages when the two passes agree on count,
            carrying the device's resolution and the rejections from both passes;
            or a _DuplexMismatch holding both passes when the counts disagree or
            either pass could not read a sheet.

        Raises:
            ScanCancelledError: If the operator aborts at the flip prompt.  Raised
                before pass B starts, and the one ending here that preserves
                nothing, because somebody chose to stop.
            ScanError: ``No pages were scanned`` if pass A returns no pages, before
                anyone is asked to flip; ``No back pages were scanned in pass B``,
                naming pass A's count, if pass B returns none, before the count
                comparison. Otherwise, if the flip prompt itself failed, or if the
                flip wait times out.  Both raise before pass B starts.
            ScanInterrupted: If the server is stopping and answered the flip
                wait with ``INTERRUPTED``.  Raised before pass B starts, and
                pass A's fronts are kept.
            AssertionError: If the coordinator returns a value that is not a
                FlipOutcome member.

        """
        # Pass A: scan fronts.  The thumbnail callback rides on this sink, which
        # fires it while pass A is still running rather than after it returns
        # -- the page is in memory at that moment, and re-opening a 26 MB
        # page later to make a 300 px strip would be a second decode.
        front_sink = self._sink(_SPOOL_LABEL_A, self._thumbnail_observer())
        # Registered before the pass runs, under the same ``(fronts)`` name the
        # mismatch recovery gives this half, so every way pass A's sheets can be
        # lost from here on keeps them.  Registering afterwards would miss
        # the case the registration exists for: a fault part-way through pass A.
        self.ledger.register(preservation.FRONTS_SUFFIX, front_sink)
        front_batch = self.scanner.scan_pages(
            self.device_id, self.scan_settings, front_sink
        )
        self.ledger.unreadable_sheets += front_batch.pages_rejected
        # Before the flip prompt, so nobody is asked to flip nothing.
        _require_pages(front_batch)
        front_pages = front_batch.pages
        logger.info("Pass A: scanned %d front page(s)", len(front_pages))
        # Before AWAITING_FLIP, and so before SCANNING_REVERSE: an observer that
        # re-renders on either of those events must already hold the number, or the
        # render it triggers shows the count one transition late.
        _note_pass_count(self.request, SCAN_LABEL_FRONT, len(front_pages))

        self._notify(PipelineEvent.AWAITING_FLIP)
        outcome = flip.coordinator.wait_for_flip(flip.timeout)
        # A match with assert_never rather than an if-chain: a fourth FlipOutcome
        # member then fails ty and pyrefly at edit time instead of falling through
        # into pass B.  An explicit abort -- web Abort, n, Ctrl-D, Ctrl-C -- is a
        # cancellation, not a scanner failure.  A broken prompt (an ABORTED that
        # carries an abort_cause) and a timeout are failures, because nobody chose
        # to stop.  A server stop is its own answer, INTERRUPTED, and never
        # arrives as a cancel: it raises ScanInterrupted, which the guard in
        # ``execute`` answers by keeping the fronts.
        match outcome:
            case FlipOutcome.CONTINUED:
                pass
            case FlipOutcome.ABORTED:
                cause = flip.coordinator.abort_cause
                if cause is not None:
                    msg = f"Flip prompt failed: {describe(cause)}"
                    raise ScanError(msg) from cause
                # The one ending here that keeps NOTHING, and the asymmetry is
                # policy rather than oversight: pass A's fronts are preserved
                # after a flip timeout or a broken prompt precisely because nobody
                # chose to stop, and are deliberately not preserved here because
                # somebody did.  ``failed/`` is never pruned automatically, so
                # filing an abandoned scan into it would leave the operator tidying
                # up after a decision they already made.  ``execute``'s guard
                # lets this exception through untouched, by type; do not turn it
                # into a ScanError to "simplify" the handler.
                msg = "Manual duplex scan cancelled at the flip prompt"
                raise ScanCancelledError(msg)
            case FlipOutcome.TIMED_OUT:
                msg = (
                    f"Manual duplex flip wait timed out after {flip.timeout:g} "
                    "seconds: nobody confirmed the stack was flipped"
                )
                raise ScanError(msg)
            case FlipOutcome.INTERRUPTED:
                # The server is stopping.  That is nobody's decision to throw
                # the scan away, so it is an interruption rather than a
                # cancel, with no signal behind it.
                msg = "The server is stopping"
                raise ScanInterrupted(msg)
            case _:
                assert_never(outcome)

        # Pass B: scan backs.  No thumbnail callback on this sink: pass A already
        # fired it, and the strip is meant to show the first front.
        self._notify(PipelineEvent.SCANNING_REVERSE)
        back_sink = self._sink(_SPOOL_LABEL_B, None)
        # Registered under ``(backs)``, again before the pass runs.  The two halves
        # are a single document between them, so a failure on either has to keep
        # both -- the same rule, and the same two names, that the mismatch recovery
        # already applies.
        self.ledger.register(preservation.BACKS_SUFFIX, back_sink)
        back_batch = self.scanner.scan_pages(
            self.device_id, self.scan_settings, back_sink
        )
        self.ledger.unreadable_sheets += back_batch.pages_rejected
        # Before the count comparison, so no half is assembled from an empty list.
        # Not _require_pages: "No pages were scanned" is false once pass A fed the
        # fronts, so the message names the pass and what pass A scanned.
        if not back_batch.pages:
            msg = (
                "No back pages were scanned in pass B "
                f"(pass A scanned {len(front_pages)} front page(s))"
            )
            raise ScanError(msg)
        back_pages = back_batch.pages
        logger.info("Pass B: scanned %d back page(s)", len(back_pages))
        # Announced for symmetry and for the log-like observers that want both
        # halves; the worker deliberately ignores it, because by the time it lands
        # the run is a moment away from its ScanResult and replacing the front
        # count would change the number under the operator's eyes.
        _note_pass_count(self.request, SCAN_LABEL_BACK, len(back_pages))

        resolution = _duplex_resolution(front_batch, back_batch)
        # Summed, not picked: a sheet lost on either pass is a sheet lost.
        rejected = front_batch.pages_rejected + back_batch.pages_rejected

        # Interleaving pairs the pages by position, and position is proof of
        # pairing only while every sheet was read on both passes.  A sheet lost on
        # either pass shifts every later page of that pass by one, and when each
        # pass loses a different sheet the counts still agree -- so a lost sheet
        # sends the halves out separately even when the counts match.  Nothing
        # tries to pair the pages by physical sheet instead: the scanner reports
        # that a sheet was skipped, not which one.
        #
        # Compare the raw counts BEFORE empty-page detection: filtering first
        # could drop a blank back and turn two matching passes into a mismatch.
        lost_a_sheet = bool(front_batch.pages_rejected or back_batch.pages_rejected)
        if lost_a_sheet or len(front_pages) != len(back_pages):
            return _DuplexMismatch(
                fronts=front_pages,
                backs=back_pages,
                unreadable_sheets=rejected,
            )

        interleaved = _interleave_duplex(front_pages, back_pages)
        logger.info("Interleaved %d total pages", len(interleaved))
        return ScanBatch(
            pages=tuple(interleaved),
            actual_resolution=resolution,
            pages_rejected=rejected,
        )

    def _scan_multi_page(self, context: _MultiPageContext) -> _MultiPageDocument:
        """
        Build one document from as many passes as the operator asks for.

        After every accepted pass the operator is asked what happens next, and
        nothing more is scanned without an answer: Scan next starts another
        pass, Re-scan throws the last pass away and scans it again at once,
        Finish ends the document, and Abort cancels the job.  A prompt nobody
        answers in time finishes the document with what it has, but warned,
        because nobody said it was complete.

        Every pass spools under its own label -- ``a-00001``, ``a-00002``, and
        so on -- because a sink numbers its files from 1, so a label shared
        between passes would have each pass overwrite the one before.  The
        labels also sort into scan order, which is the order the startup
        sweep rebuilds a crashed run's pages in.

        Everything this runs is inside ``execute``'s one guard, and the loop
        keeps the guard informed rather than opening a window of its own.  An
        accepted pass joins ``artefacts.document`` straight away, while the
        stage is still ``ACQUIRING``, and a pass in flight stays registered
        with the ledger, so a failure or an interruption at any point keeps
        the accepted pages as one PDF and the pass in flight beside it.

        Only the first page's sink carries the thumbnail observer, and only
        while no page is kept, so the job's preview stays the document's first
        page and no later pass replaces it.

        Two things end the document without the operator: the page cap, once
        a pass takes the kept pages to it, and a prompt nobody answers.  A
        pass the scanner fails while a page is kept goes back to the operator
        instead of failing the job, with the failed pass thrown away.

        Blank pages are the operator's decision here, asked once per pass and
        only for a pass that has one, while a single-pass scan removes them on
        its own.  The operator is standing at the scanner between passes, so
        a page that only looks blank -- a faint form, a signature page -- can
        be kept, skipped, or scanned again, rather than silently lost.  For
        the same reason no blank-page filter runs over the finished document:
        a page kept on purpose must never be removed afterwards, and a
        document of pages kept on purpose must never be failed as all blank.

        Args:
            context: The pass coordinator, and the timeout on every prompt.

        Returns:
            The finished document, with the warning that explains a finish
            the operator did not press, if any.

        Raises:
            ScanCancelledError: If the operator aborts at a prompt.  Nothing
                is kept.
            ScanInterrupted: If saneless is stopping; every accepted page is
                kept.
            ScanError: If a prompt broke, or the coordinator answered with
                something the prompt did not offer; or a pass failed in a way
                that does not go back to the operator.

        """
        document = _MultiPageDocument()
        while True:
            prompt = self._after_pass(context, document)
            if prompt is None:
                return document
            # Asked here, outside the pass's try, so a prompt that broke can
            # never be taken for another scanner fault.
            answer = self._ask(context, prompt)
            # A match with assert_never rather than an if-chain, so a new
            # PassAnswer member fails ty and pyrefly here at edit time instead
            # of falling through into another pass.
            match answer:
                case PassAnswer.NEXT:
                    continue
                case PassAnswer.RESCAN:
                    self._discard_last_pass(document)
                case PassAnswer.FINISH:
                    return document
                case PassAnswer.TIMED_OUT:
                    logger.warning(
                        "Nobody answered multi-page prompt %d within %g seconds; "
                        "finishing the document with %d page(s)",
                        document.prompts,
                        context.timeout,
                        document.kept,
                    )
                    document.warning = timeout_finish_warning(
                        document.kept, context.timeout
                    )
                    return document
                case (
                    PassAnswer.ABORT
                    | PassAnswer.INTERRUPTED
                    | PassAnswer.SKIP_BLANKS
                    | PassAnswer.KEEP_BLANKS
                ):
                    # _ask has already ended the run for an abort or a stop,
                    # and refused the blank-page answers, which neither the
                    # next-pass nor the retry prompt offers; reaching this arm
                    # means that changed.
                    raise _unoffered_answer(answer, document.prompts)
                case _:
                    assert_never(answer)

    def _after_pass(
        self, context: _MultiPageContext, document: _MultiPageDocument
    ) -> PassPrompt | None:
        """
        Scan one pass, and say what the operator is asked next, if anything.

        A pass the operator throws away at its blank-page prompt is scanned
        again at once, with no other question in between, so this can scan
        more than one pass before it returns.

        Args:
            context: The coordinator, which says whether saneless is stopping,
                and the timeout every prompt carries.
            document: The document the pass is accepted into.

        Returns:
            The prompt after a failed pass, the next-pass prompt after an
            accepted one, or None when the document is finished: nobody
            answered about the pass's blank pages, or the pass took the
            document to the page cap.

        Raises:
            ScanInterrupted: If saneless began stopping after the answer
                that asked for this pass was claimed; every accepted page is
                kept.

        """
        while True:
            if document.started > 0 and context.coordinator.stopping:
                # The answer that asked for this pass was claimed before the
                # stop, so the prompt could not carry the stop to the run.
                # Starting the pass anyway would outlast the bounded stop and
                # lose the accepted document, which the guard only keeps if
                # the run ends here.
                msg = "The server is stopping"
                raise ScanInterrupted(msg)
            scanned = self._scan_pass(document, document.start_pass())
            if isinstance(scanned, Exception):
                return self._retry_prompt(context, document, scanned)
            decision = self._settle_blanks(context, document, *scanned)
            if decision is not PassAnswer.RESCAN:
                break
        if decision is PassAnswer.TIMED_OUT:
            return None
        # Read at call time rather than bound at import, and checked here,
        # between passes, only: the pass that crossed the cap is kept whole.
        cap = MAX_DOCUMENT_PAGES
        if document.kept >= cap:
            logger.warning(
                "The multi-page document holds %d page(s), at or past the cap "
                "of %d; finishing it",
                document.kept,
                cap,
            )
            document.warning = cap_finish_warning(document.kept, cap)
            return None
        return self._next_pass_prompt(context, document)

    def _scan_pass(
        self, document: _MultiPageDocument, pass_number: int
    ) -> Exception | tuple[SpooledPageSink, ScanBatch]:
        """
        Scan one pass of a multi-page document, and leave it undecided.

        The pass stays registered with the ledger as a pass in flight until it
        is accepted or thrown away, so a stop while its blank pages are being
        asked about keeps it beside the document.

        A pass that fails with a fault the operator can do something about,
        while a page is kept, is thrown away -- its page files deleted, and
        none of it counted -- and its failure is returned instead of raised.
        Only the pass itself is inside the ``try``: no prompt is ever asked
        from inside it.

        Args:
            document: The document the pass is for.
            pass_number: The pass's 1-based number within the run, counting
                passes that were later thrown away.

        Returns:
            The failure the operator is to be asked about, or the sink the
            pass spooled into and what the pass produced.

        Raises:
            ScanError: ``No pages were scanned`` if the pass returned no pages,
                or whatever else the pass raised, when no page is kept yet or
                the failure is not one worth asking about.

        """
        if pass_number > 1:
            # Takes the operator's prompt off the screen as the pass starts.
            self._notify(PipelineEvent.SCANNING)
        thumbnail = self._thumbnail_observer() if document.kept == 0 else None
        sink = self._sink(f"{_SPOOL_LABEL_A}-{pass_number:05d}", thumbnail)
        # Registered before the pass runs, as every pass is: a fault part-way
        # through it has to find the sink that has been collecting its pages.
        self.ledger.register(preservation.PARTIAL_SUFFIX, sink)
        try:
            if pass_number > 1:
                # A scanner host restarted during a long wait between passes
                # would otherwise leave SANE holding a stale control
                # connection, and every later pass would fail with an I/O
                # error.  This is the restart made at the top of every job,
                # made again before each later pass, when no device handle is
                # open: scan_pages closes the device at the end of every pass.
                self.scanner.reinitialise()
            batch = self.scanner.scan_pages(self.device_id, self.scan_settings, sink)
            _require_pages(batch)
        except Exception as exc:
            if document.kept == 0 or not _returns_to_prompt(exc):
                raise
            # Deleted now, before the operator is asked: the pass contributes
            # nothing, and a stop at the prompt must not keep it as a pass in
            # flight.
            self.ledger.discard(sink)
            logger.warning(
                "Multi-page pass %d failed, %d page(s) kept: %r",
                pass_number,
                document.kept,
                exc,
            )
            return exc
        return sink, batch

    def _blank_positions(self, batch: ScanBatch) -> tuple[int, ...]:
        """
        Say which pages of a pass look blank, when the profile asks.

        Args:
            batch: What the pass produced.

        Returns:
            The 1-based positions, within the pass, of the pages that look
            blank; none when the profile has empty-page detection off.

        """
        if not self.profile.enable_empty_page_detection:
            return ()
        verdict = filter_blank_pages(
            batch.pages,
            coverage_threshold=self.profile.empty_page_coverage_threshold,
        )
        return verdict.removed_positions

    def _settle_blanks(
        self,
        context: _MultiPageContext,
        document: _MultiPageDocument,
        sink: SpooledPageSink,
        batch: ScanBatch,
    ) -> PassAnswer | None:
        """
        Accept a scanned pass, asking the operator first if a page looks blank.

        One question for the whole pass, never one per page.  Skip accepts the
        pass without the pages that look blank, Keep accepts all of it, and
        Re-scan throws the whole pass away.  Nobody answering skips them and
        finishes the document, warned, because nobody said it was complete.

        Args:
            context: The coordinator to ask, and the timeout on the prompt.
            document: The document the pass is accepted into.
            sink: The sink the pass spooled into.
            batch: What the pass produced.

        Returns:
            The answer to the blank-page prompt, or None when no page looked
            blank and the pass was accepted without a question.

        Raises:
            ScanError: If the coordinator answered with something the prompt
                did not offer, or its prompt broke.
            ScanCancelledError: If the operator aborted.
            ScanInterrupted: If saneless is stopping; the undecided pass is
                kept beside the document.

        """
        blanks = self._blank_positions(batch)
        if not blanks:
            self._accept_pass(document, sink, batch, ())
            return None
        prompt = PassPrompt(
            number=document.number_prompt(),
            wait=PassWait.BLANK_DECISION,
            pages_kept=document.kept,
            offered=_BLANK_ANSWERS,
            timeout_seconds=context.timeout,
            pass_pages=len(batch.pages),
            blank_positions=blanks,
        )
        answer = self._ask(context, prompt)
        match answer:
            case PassAnswer.RESCAN:
                # Never accepted, so neither counted as scanned nor reported
                # as removed: the numbering matches the document being built.
                self.ledger.discard(sink)
                logger.info(
                    "Re-scan: discarded a pass of %d page(s), %d of them "
                    "blank; %d kept",
                    len(batch.pages),
                    len(blanks),
                    document.kept,
                )
            case PassAnswer.SKIP_BLANKS:
                self._accept_pass(document, sink, batch, blanks)
            case PassAnswer.KEEP_BLANKS:
                self._accept_pass(document, sink, batch, ())
            case PassAnswer.TIMED_OUT:
                self._accept_pass(document, sink, batch, blanks)
                logger.warning(
                    "Nobody answered multi-page prompt %d within %g seconds; "
                    "skipping the blank page(s) and finishing the document "
                    "with %d page(s)",
                    prompt.number,
                    context.timeout,
                    document.kept,
                )
                document.warning = blank_timeout_finish_warning(
                    document.kept, context.timeout
                )
            case (
                PassAnswer.NEXT
                | PassAnswer.FINISH
                | PassAnswer.ABORT
                | PassAnswer.INTERRUPTED
            ):
                # _ask has already ended the run for an abort or a stop, and
                # refused Scan next and Finish, which this prompt does not
                # offer; reaching this arm means that changed.
                raise _unoffered_answer(answer, prompt.number)
            case _:
                assert_never(answer)
        return answer

    def _retry_prompt(
        self,
        context: _MultiPageContext,
        document: _MultiPageDocument,
        failure: Exception,
    ) -> PassPrompt:
        """
        Build the question asked after a failed pass: try again, or stop here.

        Finish is always offered, because a pass only comes back to the
        operator while a page is kept.

        Args:
            context: The timeout every prompt carries.
            document: The document so far.
            failure: What the failed pass raised, whose text the prompt shows.

        Returns:
            The numbered prompt.

        """
        return PassPrompt(
            number=document.number_prompt(),
            wait=PassWait.RETRY,
            pages_kept=document.kept,
            offered=_RETRY_ANSWERS,
            timeout_seconds=context.timeout,
            error=describe(failure),
        )

    def _accept_pass(
        self,
        document: _MultiPageDocument,
        sink: SpooledPageSink,
        batch: ScanBatch,
        skipped: tuple[int, ...],
    ) -> None:
        """
        Add a finished pass's pages to the document the guard keeps.

        Every page of the pass joins the document, skipped ones included, so
        a failed run keeps them all.  A skipped page is recorded by its
        position in the document, which is how the finished document leaves
        it out and how it is reported as removed.

        The ledger stops tracking the pass, because its pages now belong to the
        document; tracking them in both places would keep them twice.

        Args:
            document: The document the pass joins.
            sink: The sink the pass spooled into.
            batch: What the pass produced.
            skipped: The 1-based positions, within the pass, of the pages the
                document leaves out as blank.

        """
        pages = len(batch.pages)
        before = len(document.records)
        document.removed.extend(before + position for position in skipped)
        document.records.extend(batch.pages)
        document.passes.append(
            _AcceptedPass(
                sink=sink,
                pages=pages,
                kept=pages - len(skipped),
                rejected=batch.pages_rejected,
            )
        )
        document.unreadable += batch.pages_rejected
        self.ledger.unreadable_sheets += batch.pages_rejected
        self.artefacts.document = tuple(document.records)
        self.ledger.forget(sink)
        logger.info(
            "Multi-page pass %d: scanned %d page(s), skipped %d; %d kept so far",
            len(document.passes),
            pages,
            len(skipped),
            document.kept,
        )

    def _discard_last_pass(self, document: _MultiPageDocument) -> None:
        """
        Throw the last accepted pass away, pages, files and all.

        The document the guard keeps is updated before a file is deleted, so
        an interruption in between can never keep a document whose pages are
        gone.  A thrown-away pass is not counted as scanned: the numbering
        matches the document the operator is building.

        Args:
            document: The document to take the pass back out of.

        """
        last = document.passes.pop()
        first = len(document.records) - last.pages
        del document.records[first:]
        document.removed = [
            position for position in document.removed if position <= first
        ]
        document.unreadable -= last.rejected
        self.ledger.unreadable_sheets -= last.rejected
        self.artefacts.document = tuple(document.records)
        _unlink_pages(last.sink.records)
        logger.info(
            "Re-scan: discarded the last pass's %d page(s); %d kept",
            last.pages,
            document.kept,
        )

    def _next_pass_prompt(
        self, context: _MultiPageContext, document: _MultiPageDocument
    ) -> PassPrompt:
        """
        Build the question asked after every pass: is there another page.

        Finish is offered only while a page is kept, so a document of no pages
        can never be finished by a press.

        Args:
            context: The timeout every prompt carries.
            document: The document so far, whose last pass the prompt reports.

        Returns:
            The numbered prompt.

        """
        offered = {PassAnswer.NEXT, PassAnswer.RESCAN, PassAnswer.ABORT}
        if document.kept > 0:
            offered.add(PassAnswer.FINISH)
        last = document.passes[-1] if document.passes else None
        return PassPrompt(
            number=document.number_prompt(),
            wait=PassWait.NEXT_PASS,
            pages_kept=document.kept,
            offered=frozenset(offered),
            timeout_seconds=context.timeout,
            last_pass_pages=last.pages if last is not None else 0,
            last_pass_kept=last.kept if last is not None else 0,
        )

    def _ask(self, context: _MultiPageContext, prompt: PassPrompt) -> PassAnswer:
        """
        Announce the wait, put ``prompt`` to the operator, and end the run if told.

        The waiting event is reported before the coordinator is asked, so the
        job says what it is waiting on for the whole of the wait.

        Never call this inside a ``try`` that wraps a scan: a prompt that broke
        must not be mistaken for a scanner fault.

        Args:
            context: The coordinator to ask.
            prompt: The question, and the only answers it accepts.

        Returns:
            The answer, when it is one the caller acts on: a member of
            ``prompt.offered`` other than ``ABORT``, or ``TIMED_OUT``.

        Raises:
            ScanError: If the coordinator answered with something the prompt
                did not offer, or answered ``ABORT`` because its prompt broke.
            ScanCancelledError: If the operator aborted.
            ScanInterrupted: If saneless is stopping.

        """
        self._notify(_wait_event(prompt.wait))
        answer = context.coordinator.ask(prompt)
        logger.info("Multi-page prompt %d answered %s", prompt.number, answer)
        if answer not in prompt.offered and answer not in _ENDINGS_NOBODY_OFFERS:
            # Set membership, not trust: a coordinator that answers off the
            # menu -- Finish with no page kept, say -- is refused, not obeyed.
            raise _unoffered_answer(answer, prompt.number)
        if answer is PassAnswer.ABORT:
            cause = context.coordinator.abort_cause
            if cause is not None:
                msg = f"Multi-page prompt failed: {describe(cause)}"
                raise ScanError(msg) from cause
            # The one ending that keeps NOTHING, and the asymmetry is policy
            # rather than oversight: the pages are kept after a timeout, a
            # broken prompt or a stop precisely because nobody chose to stop,
            # and are deliberately not kept here because somebody did.
            # ``failed/`` is never pruned automatically, so filing an abandoned
            # scan into it would leave the operator tidying up after a
            # decision they already made.  ``execute``'s guard lets this
            # exception through untouched, by type; do not turn it into a
            # ScanError to "simplify" the handler.
            msg = "Multi-page scan cancelled at the prompt"
            raise ScanCancelledError(msg)
        if answer is PassAnswer.INTERRUPTED:
            # The server is stopping.  That is nobody's decision to throw the
            # scan away, so it is an interruption rather than a cancel, and
            # the guard keeps every accepted page.
            msg = "The server is stopping"
            raise ScanInterrupted(msg)
        return answer

    def _finish_document(self, batch: ScanBatch) -> ScanResult:
        """
        Filter, assemble and deliver one document.

        Args:
            batch: What acquisition produced, in document order.

        Returns:
            How the run resolved and how many pages it moved.

        Raises:
            AllPagesBlankError: If empty-page detection judged every page
                blank; the guard keeps the unfiltered document as a PDF.

        """
        records = batch.pages
        # Recorded before the filter runs, so a failure from here on -- the
        # all-blank verdict included -- keeps the whole document as one PDF,
        # in document order, rather than as separate passes.
        self.artefacts.stage = preservation.RunStage.FILTERING
        self.artefacts.document = tuple(records)
        rejected_warning = _rejected_pages_warning(batch.pages_rejected)

        # There is no per-page EXIF strip here, and one would have nothing to
        # act on.  python-sane builds each page with ``Image.frombuffer``,
        # which carries no EXIF, and the pages are files the spool wrote:
        # Pillow writes EXIF into a PNG or a JPEG only when it is passed as
        # the ``exif`` argument, which neither the spool's PNG save nor the
        # thumbnail's JPEG save ever does.  An orientation tag that img2pdf
        # or a browser would act on therefore cannot reach either file.
        filtered = _drop_blank_pages(records, self.profile)

        # Each page is laid out at the dpi on its own record: the resolution
        # the device read back, not the one the profile asked for. SANE
        # substitutes silently -- measured, a request for 5000 comes back as
        # 1200 -- and the read-back value is the same one the backend's crop
        # arithmetic used, so the cropped shape and the declared page size
        # cannot disagree.
        pdf_path = self._assemble(filtered.kept)
        outcome, warning = self._deliver_document(pdf_path)

        result = ScanResult(
            outcome=outcome,
            pages_scanned=len(records),
            # Blank-page detection only. A sheet the scanner could not read is
            # reported through the warning instead, because the web UI shows
            # this number to users as pages removed for being blank.
            pages_removed=len(filtered.removed_positions),
            pages_uploaded=len(filtered.kept),
            warning=_join_warnings(warning, rejected_warning),
            # Information beside the count, never folded into the warning:
            # removing blank pages leaves a plain success.
            removed_positions=filtered.removed_positions,
        )
        self._notify(PipelineEvent.DONE)
        logger.info("Pipeline complete for %r", self.request.title)
        return result

    def _finish_multi_page(self, document: _MultiPageDocument) -> ScanResult:
        """
        Assemble and deliver a multi-page document, without filtering it again.

        The automatic blank-page filter does not run here, on purpose.  Every
        page in the document is one the loop accepted, so a page kept there
        must never be removed afterwards, and a document of pages kept on
        purpose must never be failed as all blank.

        Args:
            document: The finished document.

        Returns:
            How the run resolved.  Its warning says why the document finished
            without the operator pressing Finish, beside any delivery or
            unreadable-sheet warning.

        Raises:
            AllPagesBlankError: If no page is kept; the guard keeps the
                skipped pages as one PDF.

        """
        records = tuple(document.records)
        # Recorded before anything can raise, so a failure from here on --
        # the zero-kept verdict included -- keeps the whole document as one
        # PDF, in scan order, and never a pass beside it.
        self.artefacts.stage = preservation.RunStage.FILTERING
        self.artefacts.document = records
        if document.kept == 0:
            msg = (
                f"All {len(records)} page(s) of the multi-page scan were "
                "skipped as blank, so nothing was uploaded"
            )
            raise AllPagesBlankError(msg)
        removed = tuple(sorted(document.removed))
        # A set for the membership test: a document can run to several
        # hundred pages, and a tuple would be searched once per page.
        skipped = frozenset(removed)
        kept = [
            record
            for position, record in enumerate(records, start=1)
            if position not in skipped
        ]
        rejected_warning = _rejected_pages_warning(document.unreadable)
        pdf_path = self._assemble(kept)
        outcome, warning = self._deliver_document(pdf_path)
        result = ScanResult(
            outcome=outcome,
            pages_scanned=len(records),
            pages_removed=len(removed),
            pages_uploaded=len(kept),
            warning=_join_warnings(warning, rejected_warning, document.warning),
            removed_positions=removed,
        )
        self._notify(PipelineEvent.DONE)
        logger.info(
            "Pipeline complete for %r (%d page(s) across %d pass(es))",
            self.request.title,
            len(kept),
            len(document.passes),
        )
        return result

    def _assemble(self, records: Sequence[PageRecord]) -> Path:
        """
        Assemble the document's PDF inside the workspace.

        Refused before a byte is written when the disk cannot hold the singles
        and the output beside the spool.  A refusal or an assembly failure
        leaves the stage at ASSEMBLING, where the guard keeps the page files:
        building another PDF would fail the same way.

        Args:
            records: The pages to assemble, in document order, each laid out
                at its own read-back dpi.

        Returns:
            The assembled PDF, still inside the workspace.

        """
        self.artefacts.stage = preservation.RunStage.ASSEMBLING
        self._notify(PipelineEvent.ASSEMBLING)
        preservation.ensure_room_to_assemble(
            records, self.workspace, self.settings.output.min_free_space_mb
        )
        pdf_path = assemble_pdf(
            records,
            self.workspace,
            filename=build_pdf_filename(self.request.job_id, self.request.title),
            title=self.request.title,
        )
        logger.info("PDF assembled: %s", pdf_path)
        return pdf_path

    def _upload(self, pdf_path: Path, title: str) -> UploadResult:
        """
        Upload one PDF, without polling for its task.

        Args:
            pdf_path: The PDF, still inside the workspace.
            title: The title paperless-ngx should give it.

        Returns:
            What the upload did.

        """
        # From the moment the upload returns paperless-ngx has the document,
        # or will from its consume folder: if the run fails later -- the
        # poll, the other half of a mismatch, a signal -- the kept copy must
        # say so, or following the usual advice to upload it would make a
        # duplicate.  An interruption while the request is on its way is the
        # same risk unconfirmed: paperless-ngx may have created the document
        # before the answer was cut off.  The record is made inside the try,
        # so a signal landing between the two is still caught.
        try:
            upload = self.paperless.upload_document(
                pdf_path, title, self.request.tags, self.request.correspondent
            )
            self.artefacts.accepted[pdf_path] = _accepted_how(upload)
        except ScanInterrupted:
            self.artefacts.accepted.setdefault(pdf_path, _MAY_HAVE_ARRIVED)
            raise
        return upload

    def _poll(self, upload: UploadResult) -> None:
        """
        Wait for an upload's consume task, when it reached the API.

        ``UploadResult.__post_init__`` guarantees a task id iff the document
        reached the API, so the test is exactly ``delivered_to_api`` and also
        narrows the id to ``str``.  A document that only reached the consume
        directory has no task to wait for.

        The return value is discarded on purpose: ``poll_task`` raises on
        every failed task, so a successful poll means "it returned".

        Args:
            upload: What the upload did.

        """
        if upload.task_uuid is not None:
            self.paperless.poll_task(
                upload.task_uuid, timeout=self.settings.output.paperless_task_timeout
            )

    def _deliver_document(self, pdf_path: Path) -> tuple[ScanOutcome, str | None]:
        """
        Upload the assembled PDF, poll for its task, and report how it went.

        Args:
            pdf_path: The assembled PDF, still inside the workspace.

        Returns:
            SUCCESS with no warning when the document reached the API and its
            task finished, or FALLBACK with the consume-directory warning when
            it took the other route.

        """
        self.artefacts.pdfs = [pdf_path]
        self.artefacts.stage = preservation.RunStage.DELIVERING
        self._notify(PipelineEvent.UPLOADING)
        upload = self._upload(pdf_path, self.request.title)
        self._poll(upload)
        self._delivered()
        if upload.delivered_to_api:
            return ScanOutcome.SUCCESS, None
        # A state alone would leave the user to work out for themselves why the
        # title and tags they chose never appeared in paperless-ngx.
        return ScanOutcome.FALLBACK, _consume_dir_warning(upload.consume_dir_path)

    def _finish_mismatch(self, mismatch: _DuplexMismatch) -> ScanResult:
        """
        Deliver both halves of a mismatched manual-duplex run as two PDFs.

        Instead of discarding scanned data, fronts and backs are assembled into
        separate PDFs and both are uploaded for manual review.  The ``(backs)``
        PDF is in sheet order, the reverse of the order pass B produced its
        pages.  Its page N is the back of the ``(fronts)`` PDF's page N only
        when both passes fed every sheet exactly once, which a mismatch says
        they did not: from the sheet that was skipped, missed or fed twice on,
        the two halves drift apart, which is why they are not interleaved.

        The two halves are one document between them, so a failure on either
        keeps both: an assembly failure keeps every page file of both passes,
        and an upload or poll failure keeps both PDFs.  They get visibly
        distinct names, both carrying the job id, so they cannot overwrite
        each other on the way into ``failed/``.

        The blank-page filter does not run here, on purpose, so
        ``pages_removed`` is simply 0 and no position is named as removed.  A
        mismatched run is an anomaly sent to a person for manual review, and a
        blank back side is evidence about why the two passes disagreed.

        Args:
            mismatch: Both passes, whose records carry the resolution each
                page was read back at, and the sheets the device could not
                read.

        Returns:
            SUCCESS when both halves reached the paperless-ngx API, otherwise
            FALLBACK, always carrying the mismatch warning.

        """
        fronts = mismatch.fronts
        # Pass B runs over the flipped stack, so it produces the backs last sheet
        # first.  Reversed here, as _interleave_duplex does, the (backs) PDF runs
        # in the same sheet order as the (fronts) PDF.
        backs = list(reversed(mismatch.backs))
        # One composition per half, used for the PDF's own /Title and the
        # upload, so the two cannot drift apart.  The file name carries the
        # half as a part segment of its own instead: inside the title slug it
        # would be cut off a long title, and the halves would share one name.
        fronts_title = f"{self.request.title} {preservation.FRONTS_SUFFIX}"
        backs_title = f"{self.request.title} {preservation.BACKS_SUFFIX}"

        self.artefacts.stage = preservation.RunStage.ASSEMBLING
        self._notify(PipelineEvent.ASSEMBLING)
        # Summed: the fronts PDF is still in the workspace while the backs are
        # assembled, so the two halves together have to fit the rule.
        preservation.ensure_room_to_assemble(
            [*fronts, *backs], self.workspace, self.settings.output.min_free_space_mb
        )
        fronts_pdf = assemble_pdf(
            fronts,
            self.workspace / "fronts",
            filename=build_pdf_filename(
                self.request.job_id,
                self.request.title,
                part=preservation.FRONTS_SUFFIX,
            ),
            title=fronts_title,
        )
        backs_pdf = assemble_pdf(
            backs,
            self.workspace / "backs",
            filename=build_pdf_filename(
                self.request.job_id,
                self.request.title,
                part=preservation.BACKS_SUFFIX,
            ),
            title=backs_title,
        )
        logger.info(
            "Duplex mismatch: assembled %d fronts and %d backs as separate PDFs",
            len(fronts),
            len(backs),
        )

        self.artefacts.pdfs = [fronts_pdf, backs_pdf]
        self.artefacts.stage = preservation.RunStage.DELIVERING
        self._notify(PipelineEvent.UPLOADING)
        fronts_result = self._upload(fronts_pdf, fronts_title)
        backs_result = self._upload(backs_pdf, backs_title)
        # Both halves that reach the API are polled: a half that failed
        # consumption is not a half that was delivered, and this path is the
        # one most likely to be holding a document the user actually needs.
        self._poll(fronts_result)
        self._poll(backs_result)
        self._delivered()
        delivered = fronts_result.delivered_to_api and backs_result.delivered_to_api

        warning = _duplex_mismatch_warning(mismatch)
        logger.warning(warning)
        self._notify(PipelineEvent.DONE)
        logger.info(
            "Pipeline complete for %r (duplex mismatch recovery)", self.request.title
        )
        mismatch_pages = len(mismatch.fronts) + len(mismatch.backs)
        # _MAX_ADF_PAGES applies to each scan_pages call, so to each pass: each
        # pass can feed up to that many sheets.
        return ScanResult(
            outcome=ScanOutcome.SUCCESS if delivered else ScanOutcome.FALLBACK,
            pages_scanned=mismatch_pages,
            pages_removed=0,
            pages_uploaded=mismatch_pages,
            # Not joined with _rejected_pages_warning: a lost sheet is already the
            # reason this warning gives, and saying the count twice in one message
            # reads as two separate losses.
            warning=warning,
        )


def run_pipeline(
    scanner: ScannerBackend,
    paperless: PaperlessClient,
    settings: Settings,
    request: PipelineRequest,
) -> ScanResult:
    """
    Run the full scan-to-upload pipeline.

    Scans pages from the configured device, assembles them into a PDF,
    uploads to paperless-ngx, and polls for task completion. The run works in
    its own locked workspace under ``output.tmp_dir``, named after the job,
    which is removed when the run ends, however it ends.

    What deliberately escapes that removal is whatever a failure leaves worth
    keeping. One guard spans the whole run, from the first scanning event to
    delivery, and on any failure keeps the most finished artefact the run
    produced in ``settings.output.failed_dir``: each spooled pass as a PDF
    during acquisition, the unfiltered document as one PDF after it, the page
    files if assembly failed, the assembled PDF(s) if delivery failed. A
    sentence naming what was kept, and where, is attached to the failure as a
    note (``exceptions.failure_text`` renders it); the failure itself is
    re-raised unchanged. An operator's cancel keeps nothing, because the
    operator chose to stop and ``failed/`` is never pruned.

    Status, thumbnail and pass-count observers are best-effort: an observer
    that raises is logged at WARNING and the run carries on.

    Args:
        scanner: Scanner backend instance.
        paperless: Paperless-ngx API client.
        settings: Application settings.
        request: Pipeline request parameters.

    Returns:
        A ScanResult naming how the run resolved and how many pages were
        scanned, dropped as empty, and uploaded.

    Raises:
        ConfigError: If the profile or device is not configured, a manual
            duplex profile is run with no flip coordinator, a multi-page scan
            is asked for on a manual duplex profile or with no pass
            coordinator, or the working directory under ``tmp_dir`` cannot be
            created or measured. Nothing has been scanned yet, so nothing is
            kept.
        ScanCancelledError: If the operator aborts a manual duplex scan at the
            flip prompt, or a multi-page scan at any of its prompts. Nothing
            is kept.
        AllPagesBlankError: If empty-page detection judged every page blank,
            or a multi-page scan ended with no page kept. Nothing is uploaded,
            and the unfiltered pages are kept as one PDF.
        ScanInterrupted: If a signal or a server shutdown interrupted the run,
            a multi-page prompt included. What it had is kept, like any
            failure.
        ScanError: If scanning fails; ``No pages were scanned`` if a scan pass
            returned no pages; if a manual duplex flip prompt fails or its
            wait times out; or if a multi-page prompt fails or is answered
            with something it did not offer.
        PdfError: If the PDF cannot be assembled, or the free space is under
            twice the spooled pages plus ``min_free_space_mb``, which is
            checked before assembly starts.
        PaperlessError: If upload or polling fails, in whichever subtype the
            client raised.
        Exception: Any other failure, re-raised as itself. After the workspace
            exists, every failure but a cancel carries the preservation note.

    """
    if request.profile_name not in settings.profiles:
        msg = f"Unknown profile: {request.profile_name}"
        raise ConfigError(msg)

    profile = settings.profiles[request.profile_name]

    # The one place saneless decides a scan is manual duplex, and it reads
    # profile.duplex -- never source, which is a pure SANE value.
    #
    # _flip_context refuses a manual-duplex request that has no flip
    # coordinator, and where it is called matters: it must come before
    # _resolve_device, which calls get_devices() when scanner.device is empty,
    # so a request that cannot run never touches the device.  Refusing inside
    # _scan_manual_duplex would be too late -- that method scans pass A
    # first, so it would use up a full feeder pass before failing.  The
    # multi-page refusals come first and for the same reason.
    multi_page = _multi_page_context(request, profile, settings)
    manual_duplex = profile.duplex == "manual"
    flip = _flip_context(request, settings) if manual_duplex else None

    device_id = _resolve_device(scanner, settings, request.device_memory)

    scan_settings = ScanSettings(
        source=profile.source,
        resolution=profile.resolution,
        mode=profile.mode,
        auto_source_mode=profile.auto_source_mode,
        # The single conversion point from the config Literal to the scanner's
        # feeder-resolution flag. Do not add a second: the scanner
        # package never sees ProfileConfig.duplex or the job vocabulary.
        resolve_feeder_source=manual_duplex,
        paper_size=profile.paper_size,
    )

    # The guard runs INSIDE the workspace, because the workspace's exit
    # removes the spool: anything that has to outlive the job -- a preserved
    # scan -- must be moved out before then, and a guard placed outside this
    # block would run when the pages were already gone.
    with _open_workspace(
        settings.output.tmp_dir, settings.output.min_free_space_mb, request
    ) as workspace:
        run = _PipelineRun(
            scanner=scanner,
            paperless=paperless,
            settings=settings,
            request=request,
            profile=profile,
            device_id=device_id,
            scan_settings=scan_settings,
            flip=flip,
            multi_page=multi_page,
            workspace=workspace.path,
            keep_workspace=workspace.keep,
        )
        return run.execute()
