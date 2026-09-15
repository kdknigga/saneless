"""
Shared vocabulary for the saneless domain -- every word the system uses about itself.

This is a leaf module.  It must not import from ``job.py``, ``pipeline.py``,
``worker.py``, ``cli.py``, or anything under ``web/``.  Every consumer imports
from here; nothing here imports from a consumer.  The only intra-package import
permitted is ``saneless.exceptions``, which is itself a leaf.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Final, assert_never

from saneless.exceptions import (
    ConfigError,
    FeederEmptyError,
    PaperlessError,
    PdfError,
    ScanError,
)

__all__ = [
    "ACTIVE_STATES",
    "BUSY_STATES",
    "QUEUE_FULL_JOB_ERROR",
    "RESTART_REASON",
    "TERMINAL_STATES",
    "TITLE_MAX_LENGTH",
    "WORKER_DEGRADED_JOB_ERROR",
    "WORKER_DOWN_JOB_ERROR",
    "ConnectionStatus",
    "ErrorCategory",
    "ExitCode",
    "FlipOutcome",
    "JobState",
    "RequestRejection",
    "ScanOutcome",
    "SubmitResult",
    "WorkerHealth",
    "classify_error",
    "connection_status_message",
    "error_message",
    "exit_code_for",
    "flip_answer_label",
    "job_state_for",
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

# Job-row error texts.  A submit refused because the queue was full or the
# worker was down or degraded still writes a job row, so history shows the
# attempt (D-05); these are that row's ``error``.  Like every other
# ``job.error`` they carry no trailing period.
QUEUE_FULL_JOB_ERROR: Final = "Not started: the scan queue was full"
WORKER_DOWN_JOB_ERROR: Final = "Not started: the scan service was not running"
WORKER_DEGRADED_JOB_ERROR: Final = "Not started: the scan service was unavailable"

# What startup recovery passes to ``JobStore.fail_active_jobs`` for a job the
# previous process left in flight (D-13), and what a flip wait aborted by
# shutdown records.
RESTART_REASON: Final = "The server restarted before this scan finished"


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
    {JobState.DONE, JobState.ERROR, JobState.FALLBACK}
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

    ``DONE``, ``ERROR`` and ``FALLBACK`` have no progress prose in production:
    the status partial and the CLI both branch structurally for the three
    terminal states.  Their arms exist so the lookup is total and a future
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
        case _:
            assert_never(state)
    return label


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


def error_message(category: ErrorCategory) -> str:
    """
    Return the plain-language user message for an error category.

    Every message is a developer-authored constant: no exception text is
    interpolated, so nothing internal leaks through this path.

    This function is deliberately NOT wired to any template, route, or CLI
    output yet.  The status partial keeps rendering ``job.error`` verbatim,
    because the specific messages are more truthful today than a generic
    category sentence would be -- swapping them now would be a user-visible
    regression.  The plain-language display arrives with the error-message
    rework; until then the completeness test is this function's only consumer.

    Args:
        category: The error category to describe.

    Returns:
        A short sentence a non-technical reader can act on.

    Raises:
        AssertionError: If the value is not an ErrorCategory member.

    """
    match category:
        case ErrorCategory.FEEDER:
            message = "The document feeder is empty or jammed."
        case ErrorCategory.CONFIG:
            message = "The saneless configuration is invalid."
        case ErrorCategory.SCANNER:
            message = "The scanner could not complete the scan."
        case ErrorCategory.UPLOAD:
            message = "The document could not be sent to paperless-ngx."
        case ErrorCategory.UNKNOWN:
            message = "Something went wrong."
        case ErrorCategory.ASSEMBLY:
            message = "The scanned pages could not be assembled into a PDF."
        case ErrorCategory.REJECTED:
            message = (
                # Neutral on purpose: REJECTED also covers down and degraded
                # refusals, where no scan is running to wait for (IN-02).
                "This scan was not started. Check that saneless is ready to scan, "
                "then try again."
            )
        case _:
            assert_never(category)
    return message


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
        case RequestRejection.INVALID_REQUEST:
            message = "The request was not valid. Reload the page, then try again."
        case RequestRejection.CROSS_SITE:
            message = (
                "This request was blocked because it did not come from the "
                "saneless page. If saneless is behind a reverse proxy, make sure "
                "the proxy passes the original Host header."
            )
        case RequestRejection.NOT_FOUND:
            message = (
                "That page or action does not exist. Reload the page, then try again."
            )
        case RequestRejection.METHOD_NOT_ALLOWED:
            message = "That action is not allowed. Reload the page, then try again."
        case RequestRejection.INTERNAL:
            message = (
                "Something went wrong on the server. Check the server log for "
                "details, then try again."
            )
        case RequestRejection.CLIENT_ERROR:
            message = (
                "The request could not be completed. Reload the page, then try again."
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
        case RequestRejection.WORKER_DOWN | RequestRejection.WORKER_DEGRADED:
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
