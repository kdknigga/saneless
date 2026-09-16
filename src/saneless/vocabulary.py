"""
Shared vocabulary for the saneless domain -- every word the system uses about itself.

This is a leaf module.  It must not import from ``job.py``, ``pipeline.py``,
``worker.py``, ``cli.py``, or anything under ``web/``.  Every consumer imports
from here; nothing here imports from a consumer.  The only intra-package import
permitted is ``saneless.exceptions``, which is itself a leaf.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Final, Literal, Protocol, assert_never

from saneless.exceptions import (
    ConfigError,
    FeederEmptyError,
    PaperlessError,
    PdfError,
    ScanError,
)

if TYPE_CHECKING:
    # Annotation-only, so the leaf rule is untouched either way -- ``datetime``
    # is stdlib and importing it would not make this module depend on a
    # consumer.
    from datetime import datetime

__all__ = [
    "ACTIVE_STATES",
    "BUSY_STATES",
    "LOCAL_TIME_FORMAT",
    "QUEUE_FULL_JOB_ERROR",
    "RESTART_REASON",
    "TERMINAL_STATES",
    "TITLE_MAX_LENGTH",
    "TOKEN_UNSET_JOB_ERROR",
    "WORKER_DEGRADED_JOB_ERROR",
    "WORKER_DOWN_JOB_ERROR",
    "ConnectionStatus",
    "ErrorAdvice",
    "ErrorCategory",
    "ExitCode",
    "FlipOutcome",
    "JobState",
    "PageCounted",
    "RequestRejection",
    "ScanOutcome",
    "SubmitResult",
    "WorkerHealth",
    "busy_line",
    "classify_error",
    "connection_status_message",
    "error_advice",
    "error_message",
    "error_next_step",
    "exit_code_for",
    "flip_answer_label",
    "job_state_for",
    "local_time",
    "page_counts",
    "progress_label",
    "rejection_message",
    "rejection_status_code",
    "state_label",
    "worker_health_detail",
]


class JobState(StrEnum):
    """States in the scan job lifecycle."""

    PENDING = "PENDING"
    SCANNING = "SCANNING"
    AWAITING_FLIP = "AWAITING_FLIP"
    SCANNING_REVERSE = "SCANNING_REVERSE"
    ASSEMBLING = "ASSEMBLING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"
    ERROR = "ERROR"
    FALLBACK = "FALLBACK"
    CANCELLED = "CANCELLED"


class ErrorCategory(StrEnum):
    """
    Categories of errors for programmatic handling.

    ``REJECTED`` is not a failure of a scan that ran.  It marks a job row
    written for a submit that was refused -- the queue was full, or the worker
    was down or degraded -- so the job never ran at all (D-05).
    ``JobStore.latest_run_job`` skips it, so the status area never reports a
    rejection as the job that just ended (D-06), while the history table still
    lists the row.

    ``ASSEMBLY`` means the scanned pages could not be assembled into a PDF --
    img2pdf or Pillow refused the images, or the output directory could not be
    written.  It is its own category so a full disk is never reported as a
    scanner failure (D-04).
    """

    FEEDER = "FEEDER"
    CONFIG = "CONFIG"
    SCANNER = "SCANNER"
    UPLOAD = "UPLOAD"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"
    ASSEMBLY = "ASSEMBLY"


@dataclass(frozen=True, slots=True)
class ErrorAdvice:
    """
    What a reader is told about an error category, and what to do next.

    The two halves travel together because they are always rendered together
    (APPL-04): a message without a next step leaves the reader stuck, and a
    next step without a message leaves them guessing what went wrong.  Frozen
    and slotted so a renderer cannot edit approved copy in place, and so a
    typo cannot quietly add a third field that nothing renders.
    """

    message: str
    next_step: str


class PageCounted(Protocol):
    """
    Anything carrying a job's three page counts.

    It exists so this leaf module can format the counts sentence without
    importing ``job.py``, which imports from here.  ``Job`` and ``JobResult``
    satisfy it structurally; neither has to know the protocol exists.

    All three are ``int | None`` because they are NULL on every row that never
    counted anything -- ERROR, CANCELLED, REJECTED and every row written before
    the columns existed.
    """

    @property
    def pages_scanned(self) -> int | None:
        """Pages the scanner produced, or None if nothing counted them."""

    @property
    def pages_removed(self) -> int | None:
        """Pages discarded as blank, or None if nothing counted them."""

    @property
    def pages_uploaded(self) -> int | None:
        """Pages sent to paperless-ngx, or None if nothing counted them."""


class ExitCode(IntEnum):
    """
    The process exit codes of the saneless CLI.

    This is the one definition of the CLI exit codes (D-07).  The documentation
    tables that list them are pinned to this enum by a doc-truth test, so the
    two cannot drift apart.  Dispatch onto it is a ``match`` with
    ``assert_never`` in ``exit_code_for``, so a new ``ErrorCategory`` member
    fails the type gate until it is given an exit code.

    ``CANCELLED`` is 130, the shell's convention for a process stopped by
    SIGINT, because a cancel -- Ctrl-C or the operator declining the flip -- is
    a deliberate stop and not a failure.
    """

    SUCCESS = 0
    SCAN = 1
    CONFIG = 2
    PAPERLESS = 3
    PDF = 4
    UNEXPECTED = 5
    CANCELLED = 130


class ScanOutcome(StrEnum):
    """
    How a scan attempt resolved.

    There is no ``FAILED`` member, and there is not going to be one: a failure
    raises.  The ``outcome`` column therefore stays ``NULL`` on the error path
    and ``JobState.ERROR`` carries the failure on its own.  A returned
    ``FAILED`` would record the same fact in a second column, and two columns
    about a job with exactly one fate are two columns that can disagree.
    """

    SUCCESS = "SUCCESS"
    FALLBACK = "FALLBACK"


class FlipOutcome(StrEnum):
    """
    How a manual-duplex flip wait resolved.

    The three members are exhaustive over the ways that wait can end: the
    operator turned the stack and said so, the operator gave up, or the clock
    ran out first.  ``FlipCoordinator.wait_for_flip`` returns exactly one of
    them, as one atomic answer -- which is the point, because the two events it
    replaced let a waiter wake up and then have to ask a second question to
    learn why.

    There is deliberately no "still waiting" member.  The method only returns
    once the wait has resolved, so a fourth value could never be observed, and
    a member no caller can receive is an arm every ``match`` would have to
    carry for nothing.
    """

    CONTINUED = "CONTINUED"
    ABORTED = "ABORTED"
    TIMED_OUT = "TIMED_OUT"


# The wire string for a rejected API token, named rather than written inline
# below.  Ruff's S105 reads any string literal assigned to a name containing
# "token" as a hardcoded credential; this is a public API value that
# ``docs/reference/web-api.md`` pins, not a secret, and neither the member name
# nor the string is free to change.
_REJECTED_WIRE_VALUE = "token_rejected"


class ConnectionStatus(StrEnum):
    """
    How a paperless-ngx connection test resolved.

    The values are lowercase snake_case and so break this module's otherwise
    uniform value-equals-name convention.  That is deliberate, not an
    oversight: ``web/routes.py:128`` serialises the value straight into the
    JSON body of ``GET /api/paperless/test`` and
    ``docs/reference/web-api.md:54-56`` documents the exact spelling of
    ``connected``, ``token_rejected`` and ``unreachable``.  Those three strings
    are a public wire contract and have to stay byte-identical; renaming them to
    match the member names would silently break every existing client.

    Only the *message* lookup below is a ``match`` with ``assert_never``.
    Deciding which member an HTTP response maps to is an ordered chain of
    status-code comparisons, and it lives in ``paperless.py`` -- for the same
    reason ``classify_error`` is an ``isinstance`` chain: it dispatches on a
    range of integers rather than on a closed set of enum members, so
    ``assert_never`` does not apply and a trailing fallback is the correct
    total answer.  This module imports no HTTP client and never maps a
    response status to a connection outcome.
    """

    CONNECTED = "connected"
    TOKEN_REJECTED = _REJECTED_WIRE_VALUE
    NOT_FOUND = "not_found"
    SERVER_ERROR = "server_error"
    UNREACHABLE = "unreachable"


class WorkerHealth(StrEnum):
    """
    How healthy the scan worker is, as ``/health`` reports it.

    ``DOWN`` means the worker thread is not alive.  ``DEGRADED`` means the
    thread is alive but has hit N consecutive loop-level failures -- job store
    writes, pruning -- and no store probe has succeeded since (D-10, D-12).
    ``HEALTHY`` is everything else.
    """

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


class SubmitResult(StrEnum):
    """
    What ``ScanWorker.submit`` reports instead of blocking (C-09).

    ``ACCEPTED`` means the job is queued.  ``QUEUE_FULL`` means the bounded
    queue had no room.  ``DOWN`` covers every state in which the worker cannot
    take work at all: not started, stopped, and stopping.  ``DEGRADED`` means
    the thread is alive but the job store is failing, so a job could not be
    recorded truthfully.
    """

    ACCEPTED = "ACCEPTED"
    QUEUE_FULL = "QUEUE_FULL"
    DOWN = "DOWN"
    DEGRADED = "DEGRADED"


# The member value for the unset-token refusal, named rather than written
# inline, for the same reason ``_REJECTED_WIRE_VALUE`` is: ruff's S105 reads any
# string literal assigned to a name containing "token" as a hardcoded
# credential.  The member name is fixed by APPL-07 and every StrEnum in this
# module has value == name, so the literal gets a name S105 does not flag rather
# than the convention getting an exception.
_UNSET_REJECTION_VALUE = "TOKEN_UNSET"


class RequestRejection(StrEnum):
    """
    Every error the web layer renders, one member per message.

    ``rejection_message`` and ``rejection_status_code`` give each member its
    user-facing sentence and its HTTP status.  The messages are developer
    constants: none of them contains request input or exception text, so
    nothing a client sent and nothing internal can reach the page through this
    path (V7).
    """

    QUEUE_FULL = "QUEUE_FULL"
    WORKER_DOWN = "WORKER_DOWN"
    WORKER_DEGRADED = "WORKER_DEGRADED"
    # Its own member rather than a reuse of WORKER_DEGRADED (D-15).  Degraded
    # says "the scan service was unavailable", which is untrue here: the
    # service is fine and nobody set the paperless-ngx API token.  Sharing the
    # member would send a household member looking for a broken server.
    TOKEN_UNSET = _UNSET_REJECTION_VALUE
    UNKNOWN_PROFILE = "UNKNOWN_PROFILE"
    TITLE_TOO_LONG = "TITLE_TOO_LONG"
    INVALID_REQUEST = "INVALID_REQUEST"
    CROSS_SITE = "CROSS_SITE"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    INTERNAL = "INTERNAL"
    CLIENT_ERROR = "CLIENT_ERROR"


# The one title length cap.  The ``Form(max_length=...)`` validation on the scan
# route, the ``maxlength`` attribute on the title input and the TITLE_TOO_LONG
# message all read this constant, so the three cannot drift apart (ROBU-08,
# UI-SPEC S5).
TITLE_MAX_LENGTH: Final = 256

# Job-row error texts.  A submit refused because the queue was full, the
# worker was down or degraded, or the paperless-ngx API token was never set
# still writes a job row, so history shows the attempt (D-05); these are that
# row's ``error``.  Like every other ``job.error`` they carry no trailing
# period.
QUEUE_FULL_JOB_ERROR: Final = "Not started: the scan queue was full"
WORKER_DOWN_JOB_ERROR: Final = "Not started: the scan service was not running"
WORKER_DEGRADED_JOB_ERROR: Final = "Not started: the scan service was unavailable"
# The literal is named first, and the exported constant aliases it, for the
# same reason ``_REJECTED_WIRE_VALUE`` is: ruff's S105 reads any string literal
# assigned to a name containing "token" as a hardcoded credential.  This is
# job-row copy *about* a token nobody set, not a token, and the exported name
# is fixed by APPL-07, so the literal gets a name S105 does not flag.
_UNSET_CREDENTIAL_JOB_ERROR = (
    "Not started: the paperless-ngx API token has not been set"
)
TOKEN_UNSET_JOB_ERROR: Final = _UNSET_CREDENTIAL_JOB_ERROR

# What startup recovery passes to ``JobStore.fail_active_jobs`` for a job the
# previous process left in flight (D-13), and what a flip wait aborted by
# shutdown records.
RESTART_REASON: Final = "The server restarted before this scan finished"

# How every user-facing timestamp is spelled, on the web page and in the CLI
# table alike (D-34, D-35).  ``%Z`` is on the format rather than in a column
# caption so the zone is named on every line and a copy-pasted timestamp is
# self-describing.  One constant, read by the Jinja filter and by ``cli.py``,
# is what stops the two surfaces from disagreeing.  Seconds are deliberately
# absent: they buy nothing a reader wants and they cost the CLI table three
# columns of title at 80 columns.
LOCAL_TIME_FORMAT: Final = "%Y-%m-%d %H:%M %Z"

# The separator between the pass-A front count and the progress prose on the
# manual-duplex busy line (D-33).  U+00B7 MIDDLE DOT with a space either side.
_BUSY_SEPARATOR: Final = "·"


ACTIVE_STATES: frozenset[JobState] = frozenset(
    {
        JobState.PENDING,
        JobState.SCANNING,
        JobState.AWAITING_FLIP,
        JobState.SCANNING_REVERSE,
        JobState.ASSEMBLING,
        JobState.UPLOADING,
    }
)
"""Job states where the job is still in flight.

A job in one of these states has not reached an outcome yet, so the UI keeps
polling it.
"""

TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.DONE, JobState.ERROR, JobState.FALLBACK, JobState.CANCELLED}
)
"""Job states where the job has reached its final outcome.

Together with ``ACTIVE_STATES`` this partitions ``JobState``: every member is in
exactly one of the two sets.
"""

BUSY_STATES: frozenset[JobState] = ACTIVE_STATES - {JobState.AWAITING_FLIP}
"""Job states where the machine itself is working.

Derived from ``ACTIVE_STATES`` so the two can never drift apart.  The
distinction is "the machine is working" versus "we are waiting for the human":
``AWAITING_FLIP`` is active -- the job is not finished -- but the scanner is
idle and the person has to act.  The web UI's scan-button text and its
``aria-busy`` attribute depend on that difference, so they read ``BUSY_STATES``
and not ``ACTIVE_STATES``.
"""


def state_label(state: JobState) -> str:
    """
    Return the short human label for a job state.

    These are the labels the history table has always rendered; they are part
    of the user-visible surface and must not be reworded casually.

    Args:
        state: The job state to label.

    Returns:
        The user-facing label, e.g. ``"Waiting for flip"``.

    Raises:
        AssertionError: If the value is not a JobState member.

    """
    match state:
        case JobState.PENDING:
            label = "Pending"
        case JobState.SCANNING:
            label = "Scanning"
        case JobState.AWAITING_FLIP:
            label = "Waiting for flip"
        case JobState.SCANNING_REVERSE:
            label = "Scanning backs"
        case JobState.ASSEMBLING:
            label = "Assembling"
        case JobState.UPLOADING:
            label = "Uploading"
        case JobState.DONE:
            label = "Complete"
        case JobState.ERROR:
            label = "Failed"
        case JobState.FALLBACK:
            label = "Saved to folder"
        case JobState.CANCELLED:
            label = "Cancelled"
        case _:
            assert_never(state)
    return label


def progress_label(state: JobState) -> str:
    """
    Return the progress prose for a job state.

    This is the longer sentence the status area shows while a scan is running,
    and the line the CLI echoes.  The six in-flight strings are byte identical
    to what shipped before -- including the literal three-period spelling of
    the trailing ellipsis, which is three ASCII periods and not U+2026.
    ``SCANNING_REVERSE`` is the newest of them, and its prose is the exact line
    the CLI printed for the second duplex pass before the state existed, so
    giving pass B its own state changed no CLI output.

    ``DONE``, ``ERROR``, ``FALLBACK`` and ``CANCELLED`` have no progress prose
    in production: the status partial and the CLI both branch structurally for
    the four terminal states.  Their arms exist so the lookup is total and a future
    member cannot be forgotten; they have no production caller in this phase.

    Args:
        state: The job state to describe.

    Returns:
        The user-facing progress sentence, e.g. ``"Assembling PDF..."``.

    Raises:
        AssertionError: If the value is not a JobState member.

    """
    match state:
        case JobState.PENDING:
            label = "Starting scan..."
        case JobState.SCANNING:
            label = "Scanning..."
        case JobState.AWAITING_FLIP:
            label = "Awaiting flip..."
        case JobState.SCANNING_REVERSE:
            label = "Scanning reverse sides..."
        case JobState.ASSEMBLING:
            label = "Assembling PDF..."
        case JobState.UPLOADING:
            label = "Uploading to paperless-ngx..."
        case JobState.DONE:
            label = "Complete"
        case JobState.ERROR:
            label = "Failed"
        case JobState.FALLBACK:
            label = "Saved to folder"
        case JobState.CANCELLED:
            label = "Cancelled"
        case _:
            assert_never(state)
    return label


def busy_line(
    state: JobState,
    *,
    queue_title: str | None = None,
    queue_ahead: int | None = None,
    front_pages: int | None = None,
) -> str:
    """
    Return the one line the status area shows while a job is in flight.

    Three branches in strict precedence (D-33, APPL-08):

    1. The job is queued behind another one, so it is told what it is waiting
       for and how many jobs are ahead.  This wins outright: a job that has not
       started has nothing else worth saying.
    2. The job is on the second manual-duplex pass and the front count is
       known, so the count leads the progress prose.
    3. Otherwise the progress prose alone, exactly as before.

    ``(0 ahead of you)`` is never produced.  It is technically true and reads
    like a bug, so the last job in the queue is told it is ``next in line``
    (D-25).

    The trailing phrase in branch 2 is ``progress_label(SCANNING_REVERSE)`` --
    master-pinned copy with its own tests -- and not the history table's
    ``state_label``, which is a different owner with a different string.  This
    is a deliberate, recorded deviation from D-33's specimen wording; the
    count, the separator and the live behaviour are as D-33 specifies.

    ``queue_title`` is the only user data any string here carries.  It is
    returned as plain text, escaped by Jinja's autoescape at render time, and
    is never logged from this module.

    Args:
        state: The state of the job being followed.
        queue_title: The title of the job ahead, when there is one.
        queue_ahead: How many jobs are ahead of the followed job.
        front_pages: Pages counted on the first manual-duplex pass, when that
            count is known.

    Returns:
        One line of plain text.

    """
    if queue_title is not None and queue_ahead is not None:
        position = "next in line" if queue_ahead == 0 else f"{queue_ahead} ahead of you"
        return f"Waiting for '{queue_title}' to finish ({position})"
    label = progress_label(state)
    if state is JobState.SCANNING_REVERSE and front_pages is not None:
        noun = "page" if front_pages == 1 else "pages"
        return f"Front: {front_pages} {noun} {_BUSY_SEPARATOR} {label}"
    return label


def local_time(value: datetime) -> str:
    """
    Render a timestamp in the server's local zone, with the zone named.

    The argument must be timezone-aware.  Every timestamp saneless persists is
    (``JobStore`` writes ``datetime.now(tz=UTC)`` and ``_row_to_job`` parses it
    back from the isoformat string), so a naive value reaching here is a bug in
    the caller, not a case to guess at.

    ``astimezone()`` is called with no argument, so the zone is the process's
    own whatever zone the value carries.  That makes ``TZ`` load-bearing: a
    container reports UTC unless it is set, which satisfies APPL-12 on paper
    and helps nobody.  No ``zoneinfo`` import and no new config key is
    involved -- the operator's ``TZ`` is the single source.

    Args:
        value: A timezone-aware timestamp.

    Returns:
        The timestamp as ``2026-09-16 14:03 CDT``.

    """
    return value.astimezone().strftime(LOCAL_TIME_FORMAT)


def page_counts(job: PageCounted) -> str | None:
    """
    Return the page-count sentence for a job, or None if it has no counts.

    A NULL count renders nothing at all -- no element, no empty line -- and one
    NULL is enough to suppress the whole sentence, because a sentence naming
    two of three counts invites the reader to wonder about the third (D-32).
    This is the common path, not an edge: four of the six terminal cases have
    no counts by construction (ERROR, CANCELLED, REJECTED and every row written
    before the columns existed).

    A measured ``0`` is not a NULL and renders as ``0``.  A scan where nothing
    was blank really did remove 0 pages.  Consumers must therefore guard on
    ``is not None`` and never on truthiness, in Python and in Jinja alike
    (``is not none``), or a real zero disappears.

    Only the first clause carries a noun, so only the first clause pluralises.

    Args:
        job: Anything carrying the three page counts.

    Returns:
        ``"12 pages scanned, 2 blank removed, 10 uploaded"``, or None if any
        of the three counts is NULL.

    """
    scanned = job.pages_scanned
    removed = job.pages_removed
    uploaded = job.pages_uploaded
    if scanned is None or removed is None or uploaded is None:
        return None
    noun = "page" if scanned == 1 else "pages"
    return f"{scanned} {noun} scanned, {removed} blank removed, {uploaded} uploaded"


def flip_answer_label(outcome: FlipOutcome) -> str:
    """
    Return the acknowledgment the status area shows for an answered flip wait.

    Once a job's flip wait has been answered but the worker has not yet
    persisted the job's next state, the status area shows this sentence in
    place of the flip prompt, so the Continue and Abort buttons do not come
    back as though the click did nothing (CR-01).  The trailing ellipsis is
    three ASCII periods, matching ``progress_label``.

    ``TIMED_OUT`` has an arm for totality: a timed-out job moves to ``ERROR``
    within the same worker step, so the status area is not expected to show
    it.

    Args:
        outcome: The answer the job's flip wait received.

    Returns:
        The user-facing acknowledgment, e.g. ``"Aborting scan..."``.

    Raises:
        AssertionError: If the value is not a FlipOutcome member.

    """
    match outcome:
        case FlipOutcome.CONTINUED:
            label = "Flip confirmed. Scanning reverse sides next..."
        case FlipOutcome.ABORTED:
            label = "Aborting scan..."
        case FlipOutcome.TIMED_OUT:
            label = "Flip wait timed out..."
        case _:
            assert_never(outcome)
    return label


def job_state_for(outcome: ScanOutcome) -> JobState:
    """
    Return the terminal job state a resolved scan outcome implies.

    A ``ScanOutcome`` only exists once the pipeline has finished, so every arm
    lands in ``TERMINAL_STATES``.  ``FALLBACK`` gets a state of its own rather
    than being folded into ``DONE``: the document reached the consume directory
    but its title, tags and correspondent were not applied, and calling that
    "Complete" is the silent success this vocabulary exists to remove.

    This is a ``match`` with ``assert_never`` and not a
    ``dict[ScanOutcome, JobState]`` on purpose.  A dict missing a member draws
    no diagnostic from either ``ty`` or ``pyrefly``; the same enum in a match
    is caught by both, at edit time, before a third outcome can fall silently
    through an ``else``.

    Args:
        outcome: The outcome the pipeline resolved to.

    Returns:
        The terminal JobState to persist for that outcome.

    Raises:
        AssertionError: If the value is not a ScanOutcome member.

    """
    match outcome:
        case ScanOutcome.SUCCESS:
            state = JobState.DONE
        case ScanOutcome.FALLBACK:
            state = JobState.FALLBACK
        case _:
            assert_never(outcome)
    return state


def error_advice(category: ErrorCategory) -> ErrorAdvice:
    """
    Return what to tell a reader about an error category, and what to do next.

    This is the one ``match`` over ``ErrorCategory`` in this module (D-10,
    D-11).  ``error_message`` and ``error_next_step`` are one-line accessors
    over it rather than lookups of their own, so a category can never end up
    with a message and no next step, or with two lookups that drift apart.

    The wording is surface-neutral.  Both the web page and the CLI render the
    same string, so a next step never says "press Scan" (the CLI has no
    button) and never says "run the command" (the page has no command line);
    it says "start the scan again", which is true on both.

    Every string is a developer-authored constant.  None of them interpolates
    exception text, request input, a URL, a token or a filesystem path, so
    nothing internal can reach a screen through this path (ASVS V7).

    Args:
        category: The error category to describe.

    Returns:
        The plain-language message and the next step, paired.

    Raises:
        AssertionError: If the value is not an ErrorCategory member.

    """
    match category:
        case ErrorCategory.FEEDER:
            advice = ErrorAdvice(
                message="The document feeder is empty or jammed.",
                next_step=(
                    "Load the pages squarely in the feeder, clear any jam, "
                    "then start the scan again."
                ),
            )
        case ErrorCategory.CONFIG:
            advice = ErrorAdvice(
                message="The saneless configuration is invalid.",
                next_step=(
                    "Correct the saneless configuration file, then restart saneless."
                ),
            )
        case ErrorCategory.SCANNER:
            advice = ErrorAdvice(
                message="The scanner could not complete the scan.",
                next_step=(
                    "Check the scanner is switched on and connected, then "
                    "start the scan again."
                ),
            )
        case ErrorCategory.UPLOAD:
            advice = ErrorAdvice(
                message="The document could not be sent to paperless-ngx.",
                next_step=(
                    "Check paperless-ngx is running and the API token is "
                    "correct, then start the scan again."
                ),
            )
        case ErrorCategory.UNKNOWN:
            advice = ErrorAdvice(
                message="Something went wrong.",
                next_step=(
                    "Start the scan again. If it keeps failing, check the saneless log."
                ),
            )
        case ErrorCategory.ASSEMBLY:
            advice = ErrorAdvice(
                message="The scanned pages could not be assembled into a PDF.",
                next_step=(
                    "Start the scan again. If it keeps failing, check the "
                    "server's free disk space."
                ),
            )
        case ErrorCategory.REJECTED:
            advice = ErrorAdvice(
                # Neutral on purpose: REJECTED also covers down and degraded
                # refusals, where no scan is running to wait for (IN-02).
                message=(
                    "This scan was not started. Check that saneless is ready "
                    "to scan, then try again."
                ),
                next_step=(
                    "Check the system status list for anything marked Failed, "
                    "then start the scan again."
                ),
            )
        case _:
            assert_never(category)
    return advice


def error_message(category: ErrorCategory) -> str:
    """
    Return the plain-language user message for an error category.

    APPL-04 is the arrival this function's docstring used to promise: the
    plain-language display is wired, and the message is now half of an
    ``ErrorAdvice`` rather than a lookup of its own.  The D-10 rule is that
    there is exactly one ``match`` over ``ErrorCategory`` in this module, in
    ``error_advice``; this accessor reads it so the message and the next step
    cannot drift apart.

    Args:
        category: The error category to describe.

    Returns:
        A short sentence a non-technical reader can act on.

    Raises:
        AssertionError: If the value is not an ErrorCategory member.

    """
    return error_advice(category).message


def error_next_step(category: ErrorCategory) -> str:
    """
    Return the action a reader should take after an error category.

    The companion of ``error_message`` and, like it, a one-line accessor over
    ``error_advice`` (D-10, D-11).

    Args:
        category: The error category to advise on.

    Returns:
        One imperative sentence -- two for ASSEMBLY and UNKNOWN -- that names
        what to do next without naming the web page or the command line.

    Raises:
        AssertionError: If the value is not an ErrorCategory member.

    """
    return error_advice(category).next_step


def connection_status_message(status: ConnectionStatus) -> str:
    """
    Return the plain-language user message for a connection-test outcome.

    Every message is a developer-authored constant.  No status code, no
    response body, no URL and no token is interpolated, so a paperless-ngx
    error page cannot reach the UI through this path.

    Args:
        status: The connection-test outcome to describe.

    Returns:
        A short sentence a non-technical reader can act on.

    Raises:
        AssertionError: If the value is not a ConnectionStatus member.

    """
    match status:
        case ConnectionStatus.CONNECTED:
            message = "Connected to paperless-ngx."
        case ConnectionStatus.TOKEN_REJECTED:
            message = "Paperless-ngx rejected the API token."
        case ConnectionStatus.NOT_FOUND:
            message = "The paperless-ngx API was not found at that URL."
        case ConnectionStatus.SERVER_ERROR:
            message = "Paperless-ngx returned a server error."
        case ConnectionStatus.UNREACHABLE:
            message = "Could not reach paperless-ngx."
        case _:
            assert_never(status)
    return message


def worker_health_detail(health: WorkerHealth) -> str:
    """
    Return the ``/health`` detail string for a worker health state.

    These strings are a wire contract: the ``/health`` response body and
    ``docs/reference/web-api.md`` pin them, so they must not be reworded.

    Args:
        health: The worker health state to describe.

    Returns:
        The short detail string, e.g. ``"job store failing"``.

    Raises:
        AssertionError: If the value is not a WorkerHealth member.

    """
    match health:
        case WorkerHealth.HEALTHY:
            detail = "ok"
        case WorkerHealth.DEGRADED:
            detail = "job store failing"
        case WorkerHealth.DOWN:
            detail = "worker thread is down"
        case _:
            assert_never(health)
    return detail


def _reload_page_message(
    rejection: Literal[
        RequestRejection.INVALID_REQUEST,
        RequestRejection.NOT_FOUND,
        RequestRejection.METHOD_NOT_ALLOWED,
        RequestRejection.CLIENT_ERROR,
    ],
) -> str:
    """
    Return the message for a rejection whose remedy is "reload the page".

    These four are one group, not four unrelated arms: each names a different
    thing the browser got wrong and all four end in the same sentence, because
    reloading is the only thing a reader can usefully do about any of them.
    Grouping them keeps ``rejection_message`` readable as the twelfth member
    joins it.

    The parameter is typed as the four members this arm can pass, so the
    ``assert_never`` below still fails the type gate if the group ever grows,
    and ``rejection_message``'s own ``assert_never`` still fails it if
    ``RequestRejection`` grows.

    Args:
        rejection: One of the four reload-remedy rejections.

    Returns:
        The approved sentence for that rejection.

    Raises:
        AssertionError: If the value is outside the four-member group.

    """
    match rejection:
        case RequestRejection.INVALID_REQUEST:
            message = "The request was not valid. Reload the page, then try again."
        case RequestRejection.NOT_FOUND:
            message = (
                "That page or action does not exist. Reload the page, then try again."
            )
        case RequestRejection.METHOD_NOT_ALLOWED:
            message = "That action is not allowed. Reload the page, then try again."
        case RequestRejection.CLIENT_ERROR:
            message = (
                "The request could not be completed. Reload the page, then try again."
            )
        case _:
            assert_never(rejection)
    return message


def rejection_message(rejection: RequestRejection) -> str:
    """
    Return the user-facing message for a web-layer rejection.

    This is the approved copy from 26-UI-SPEC S3, and the only place it lives.
    Every message is a developer-authored constant that ends with a period.
    The TITLE_TOO_LONG message is built from ``TITLE_MAX_LENGTH`` so the number
    it names cannot drift from the cap the form enforces.

    Args:
        rejection: The rejection to describe.

    Returns:
        A sentence that says what happened and what to do next.

    Raises:
        AssertionError: If the value is not a RequestRejection member.

    """
    match rejection:
        case RequestRejection.QUEUE_FULL:
            message = (
                "The scan queue is full. Wait for a scan to finish, then try again."
            )
        case RequestRejection.WORKER_DOWN:
            message = (
                "The scan service is not running, so the scan was not started. "
                "Restart saneless, then try again."
            )
        case RequestRejection.WORKER_DEGRADED:
            message = (
                "Job history cannot be saved right now, so the scan was not "
                "started. Check the server's free disk space and log, then try "
                "again."
            )
        case RequestRejection.TOKEN_UNSET:
            message = (
                # Names the problem and the file to edit, never the token
                # value and never the paperless-ngx URL, which may carry
                # ``user:pass@`` credentials (ASVS V7).
                "The paperless-ngx API token has not been set, so the scan was "
                "not started. Put a real API token in the saneless config "
                "file, then restart saneless."
            )
        case RequestRejection.UNKNOWN_PROFILE:
            message = (
                "That scan profile does not exist. Reload the page to see the "
                "current profiles."
            )
        case RequestRejection.TITLE_TOO_LONG:
            message = (
                "The title is too long. Shorten it to "
                f"{TITLE_MAX_LENGTH} characters or fewer."
            )
        case (
            RequestRejection.INVALID_REQUEST
            | RequestRejection.NOT_FOUND
            | RequestRejection.METHOD_NOT_ALLOWED
            | RequestRejection.CLIENT_ERROR
        ):
            message = _reload_page_message(rejection)
        case RequestRejection.CROSS_SITE:
            message = (
                "This request was blocked because it did not come from the "
                "saneless page. If saneless is behind a reverse proxy, make sure "
                "the proxy passes the original Host header."
            )
        case RequestRejection.INTERNAL:
            message = (
                "Something went wrong on the server. Check the server log for "
                "details, then try again."
            )
        case _:
            assert_never(rejection)
    return message


def rejection_status_code(rejection: RequestRejection) -> int:
    """
    Return the HTTP status code a web-layer rejection is sent with.

    Args:
        rejection: The rejection to map.

    Returns:
        The HTTP status code, e.g. ``429`` for a full queue.

    Raises:
        AssertionError: If the value is not a RequestRejection member.

    """
    match rejection:
        case RequestRejection.QUEUE_FULL:
            status_code = 429
        case (
            RequestRejection.WORKER_DOWN
            | RequestRejection.WORKER_DEGRADED
            | RequestRejection.TOKEN_UNSET
        ):
            status_code = 503
        case (
            RequestRejection.UNKNOWN_PROFILE
            | RequestRejection.TITLE_TOO_LONG
            | RequestRejection.INVALID_REQUEST
        ):
            status_code = 422
        case RequestRejection.CROSS_SITE:
            status_code = 403
        case RequestRejection.NOT_FOUND:
            status_code = 404
        case RequestRejection.METHOD_NOT_ALLOWED:
            status_code = 405
        case RequestRejection.INTERNAL:
            status_code = 500
        case RequestRejection.CLIENT_ERROR:
            status_code = 400
        case _:
            assert_never(rejection)
    return status_code


def exit_code_for(category: ErrorCategory) -> ExitCode:
    """
    Return the CLI exit code for an error category.

    Two outcomes are resolved by exception type before a caller classifies at
    all, and so never reach this function:

    * A cancel is not a category.  ``ScanCancelledError`` and
      ``KeyboardInterrupt`` map to ``ExitCode.CANCELLED``.
    * ``StorageError`` classifies as ``UNKNOWN``, but it is a setup problem, so
      the CLI guard maps it to ``ExitCode.CONFIG`` (exit 2) by type (D-07
      amendment).  The mapping is by type rather than by making
      ``classify_error`` return ``CONFIG`` because ``ErrorCategory`` is
      persisted on job records, and a store that cannot open is not a job's
      configuration failure.  An ``ErrorCategory.STORAGE`` member was rejected
      for the same reason: it would be a new persisted value no job could ever
      carry.

    ``UNKNOWN`` therefore reaches ``UNEXPECTED`` only for exceptions that are
    not saneless types -- and for a bare ``SanelessError``, which is itself a
    bug.

    Args:
        category: The error category to map.

    Returns:
        The exit code, e.g. ``ExitCode.PAPERLESS`` for an upload failure.

    Raises:
        AssertionError: If the value is not an ErrorCategory member.

    """
    match category:
        case ErrorCategory.FEEDER | ErrorCategory.SCANNER:
            exit_code = ExitCode.SCAN
        case ErrorCategory.CONFIG:
            exit_code = ExitCode.CONFIG
        case ErrorCategory.UPLOAD:
            exit_code = ExitCode.PAPERLESS
        case ErrorCategory.ASSEMBLY:
            exit_code = ExitCode.PDF
        case ErrorCategory.UNKNOWN | ErrorCategory.REJECTED:
            exit_code = ExitCode.UNEXPECTED
        case _:
            assert_never(category)
    return exit_code


def classify_error(exc: Exception) -> ErrorCategory:
    """
    Map an exception to its error category.

    The checks are ordered, not matched: ``FeederEmptyError`` subclasses
    ``ScanError``, so the narrower class has to be tested first.  This is an
    ``isinstance`` chain rather than a ``match`` because it dispatches on
    exception type instead of on an enum, so ``assert_never`` does not apply
    and the trailing ``UNKNOWN`` is the correct total fallback.

    ``ScanCancelledError`` is deliberately left ``UNKNOWN``: a cancel is not a
    failure category, and callers test for it before they classify (D-01).
    ``StorageError`` is also ``UNKNOWN``; its exit code is assigned by type, as
    ``exit_code_for`` explains.

    Args:
        exc: The caught exception.

    Returns:
        The appropriate ErrorCategory value.

    """
    category = ErrorCategory.UNKNOWN
    if isinstance(exc, FeederEmptyError):
        category = ErrorCategory.FEEDER
    elif isinstance(exc, ConfigError):
        category = ErrorCategory.CONFIG
    elif isinstance(exc, ScanError):
        category = ErrorCategory.SCANNER
    elif isinstance(exc, PaperlessError):
        category = ErrorCategory.UPLOAD
    elif isinstance(exc, PdfError):
        category = ErrorCategory.ASSEMBLY
    return category
