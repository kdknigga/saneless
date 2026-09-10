"""
Shared vocabulary for the saneless domain -- every word the system uses about itself.

This is a leaf module.  It must not import from ``job.py``, ``pipeline.py``,
``worker.py``, ``cli.py``, or anything under ``web/``.  Every consumer imports
from here; nothing here imports from a consumer.  The only intra-package import
permitted is ``saneless.exceptions``, which is itself a leaf.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ACTIVE_STATES",
    "BUSY_STATES",
    "TERMINAL_STATES",
    "ErrorCategory",
    "JobState",
    "ScanOutcome",
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
    # FAILED belongs to the ADF fallback work; it is added together with the
    # code path that produces it, not ahead of it.


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

BUSY_STATES = ACTIVE_STATES - {JobState.AWAITING_FLIP}
"""Job states where the machine itself is working.

Derived from ``ACTIVE_STATES`` so the two can never drift apart.  The
distinction is "the machine is working" versus "we are waiting for the human":
``AWAITING_FLIP`` is active -- the job is not finished -- but the scanner is
idle and the person has to act.  The web UI's scan-button text and its
``aria-busy`` attribute depend on that difference, so they read ``BUSY_STATES``
and not ``ACTIVE_STATES``.
"""
