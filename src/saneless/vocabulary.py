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
    "ErrorCategory",
    "JobState",
    "ScanOutcome",
    "classify_error",
    "error_message",
    "progress_label",
    "state_label",
]


class JobState(StrEnum):
    """States in the scan job lifecycle."""

    PENDING = "PENDING"
    SCANNING = "SCANNING"
    AWAITING_FLIP = "AWAITING_FLIP"
    ASSEMBLING = "ASSEMBLING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"
    ERROR = "ERROR"


class ErrorCategory(StrEnum):
    """Categories of errors for programmatic handling."""

    FEEDER = "FEEDER"
    CONFIG = "CONFIG"
    SCANNER = "SCANNER"
    UPLOAD = "UPLOAD"
    UNKNOWN = "UNKNOWN"


class ScanOutcome(StrEnum):
    """How a scan attempt resolved."""

    SUCCESS = "SUCCESS"
    FALLBACK = "FALLBACK"
    # FAILED belongs to the honest-outcomes work, which decides whether a
    # failure is a returned outcome or a raised exception. It is added together
    # with the code path that produces it, not ahead of it.


ACTIVE_STATES: frozenset[JobState] = frozenset(
    {
        JobState.PENDING,
        JobState.SCANNING,
        JobState.AWAITING_FLIP,
        JobState.ASSEMBLING,
        JobState.UPLOADING,
    }
)
"""Job states where the job is still in flight.

A job in one of these states has not reached an outcome yet, so the UI keeps
polling it.
"""

TERMINAL_STATES: frozenset[JobState] = frozenset({JobState.DONE, JobState.ERROR})
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
        case JobState.ASSEMBLING:
            label = "Assembling"
        case JobState.UPLOADING:
            label = "Uploading"
        case JobState.DONE:
            label = "Complete"
        case JobState.ERROR:
            label = "Failed"
        case _:
            assert_never(state)
    return label


def progress_label(state: JobState) -> str:
    """
    Return the progress prose for a job state.

    This is the longer sentence the status area shows while a scan is running,
    and the line the CLI echoes.  The five in-flight strings are byte identical
    to what shipped before -- including the literal three-period spelling of
    the trailing ellipsis, which is three ASCII periods and not U+2026.

    ``DONE`` and ``ERROR`` have no progress prose in production: the status
    partial and the CLI both branch structurally for those two.  Their arms
    exist so the lookup is total and a future member cannot be forgotten; they
    have no production caller in this phase.

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
        case JobState.ASSEMBLING:
            label = "Assembling PDF..."
        case JobState.UPLOADING:
            label = "Uploading to paperless-ngx..."
        case JobState.DONE:
            label = "Complete"
        case JobState.ERROR:
            label = "Failed"
        case _:
            assert_never(state)
    return label


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
