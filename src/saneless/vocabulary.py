"""
Shared vocabulary for the saneless domain -- every word the system uses about itself.

This is a leaf module.  It must not import from ``job.py``, ``pipeline.py``,
``worker.py``, ``cli.py``, or anything under ``web/``.  Every consumer imports
from here; nothing here imports from a consumer.  The only intra-package import
permitted is ``saneless.exceptions``, which is itself a leaf.
"""

from __future__ import annotations

from enum import StrEnum
from typing import assert_never

from saneless.exceptions import (
    ConfigError,
    FeederEmptyError,
    PaperlessError,
    ScanError,
)

__all__ = [
    "ACTIVE_STATES",
    "BUSY_STATES",
    "TERMINAL_STATES",
    "ConnectionStatus",
    "ErrorCategory",
    "FlipOutcome",
    "JobState",
    "ScanOutcome",
    "classify_error",
    "connection_status_message",
    "error_message",
    "job_state_for",
    "progress_label",
    "state_label",
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
    """Categories of errors for programmatic handling."""

    FEEDER = "FEEDER"
    CONFIG = "CONFIG"
    SCANNER = "SCANNER"
    UPLOAD = "UPLOAD"
    UNKNOWN = "UNKNOWN"


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
    total answer.  This module imports no HTTP client and knows no status codes.
    """

    CONNECTED = "connected"
    TOKEN_REJECTED = _REJECTED_WIRE_VALUE
    NOT_FOUND = "not_found"
    SERVER_ERROR = "server_error"
    UNREACHABLE = "unreachable"


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


def classify_error(exc: Exception) -> ErrorCategory:
    """
    Map an exception to its error category.

    The checks are ordered, not matched: ``FeederEmptyError`` subclasses
    ``ScanError``, so the narrower class has to be tested first.  This is an
    ``isinstance`` chain rather than a ``match`` because it dispatches on
    exception type instead of on an enum, so ``assert_never`` does not apply
    and the trailing ``UNKNOWN`` is the correct total fallback.

    Args:
        exc: The caught exception.

    Returns:
        The appropriate ErrorCategory value.

    """
    if isinstance(exc, FeederEmptyError):
        return ErrorCategory.FEEDER
    if isinstance(exc, ConfigError):
        return ErrorCategory.CONFIG
    if isinstance(exc, ScanError):
        return ErrorCategory.SCANNER
    if isinstance(exc, PaperlessError):
        return ErrorCategory.UPLOAD
    return ErrorCategory.UNKNOWN
