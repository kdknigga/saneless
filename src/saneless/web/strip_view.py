"""
The checks strip one page shows: a row per check and the sentence under them.

The full page, the strip's own poll, Check again and the scan submit's
out-of-band strip all render from the context built here, so they show the same
rows and freshness line for the same cache.  It reads the check cache, the
refresher and the worker and never probes, so a page waits on nothing but a
cache read.  The templates author none of the wording.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from saneless.checks import (
    CHECKING_GLYPH,
    CHECKING_MESSAGE,
    CHECKING_STATE_CLASS,
    CHECKING_STATE_LABEL,
    POLL_ATTEMPT_CAP,
    POLL_GAVE_UP_LINE,
    POLL_PROBE_ATTEMPT_CAP,
    POLL_STILL_CHECKING_LINE,
    CheckKey,
)
from saneless.vocabulary import local_time

if TYPE_CHECKING:
    from saneless.web.checks_cache import CachedChecks
    from saneless.web.services import Services

__all__ = ["checks_context", "checks_fallback_context"]

# The freshness line's variants; the templates own no vocabulary.  The dash is
# U+2014 with spaces on both sides.
_PAUSED_PREFIX: Final = "Paused during scan — "
_COLD_PAUSED_LINE: Final = f"{_PAUSED_PREFIX}not checked yet."


@dataclass(frozen=True, slots=True)
class _CheckingRow:
    """
    One cold-start placeholder row, before any probe has happened.

    Not a :class:`~saneless.checks.CheckResult`, because "not looked yet" is not
    a ``CheckState`` member, and a new member would owe ``saneless doctor`` an
    exit-code rule for a state the CLI, which probes synchronously, is never in.
    Every field but ``key`` is a constant from ``saneless.checks``.

    Attributes:
        key: Which check this row is standing in for.
        state_class: The muted colour class the glyph is drawn in.
        glyph: The neutral cold-start marker.
        state_label: The word a screen reader hears in the glyph's place.
        message: The cold-start copy.

    """

    key: CheckKey
    state_class: str = CHECKING_STATE_CLASS
    glyph: str = CHECKING_GLYPH
    state_label: str = CHECKING_STATE_LABEL
    message: str = CHECKING_MESSAGE


# One placeholder per CheckKey, in member order, so a cold strip still names
# every check rather than showing an empty list that reads as "nothing to report".
_CHECKING_ROWS: Final = tuple(_CheckingRow(key=key) for key in CheckKey)


def _freshness_line(cached: CachedChecks, *, scan_active: bool) -> str:
    """
    Compose the one sentence under the rows, for each situation.

    During a scan the checks are paused and the line says so, rather than
    showing stale rows silently or blanking ones that are still true.  The
    timestamp goes through ``local_time``, as ``saneless doctor``'s table does.

    """
    if cached.results is None or cached.checked_at is None:
        return _COLD_PAUSED_LINE if scan_active else CHECKING_MESSAGE
    stamp = local_time(cached.checked_at)
    if scan_active:
        return f"{_PAUSED_PREFIX}last checked {stamp}."
    return f"Last checked {stamp}."


def _poll_line(
    cached: CachedChecks,
    *,
    scan_active: bool,
    gave_up: bool,
    still_checking: bool,
) -> str:
    """
    Pick the sentence under the rows once the poll's own endings are counted.

    A chain out of attempts gives up and names Check again.  A chain still
    asking while a probe holds the lock says the check is running instead,
    because a click would only collapse into that probe.

    """
    if gave_up:
        return POLL_GAVE_UP_LINE
    if still_checking:
        return POLL_STILL_CHECKING_LINE
    return _freshness_line(cached, scan_active=scan_active)


def checks_context(
    svc: Services, *, attempt: int = 0, scan_active: bool | None = None
) -> dict[str, object]:
    """
    Build the context ``partials/checks.html`` renders from, without probing.

    A request never probes: an unplugged scanner host can hold a blocking
    connect for minutes, so the background refresher fills the cache instead.
    ``checks`` is None on a cold cache, which draws the placeholder rows.
    ``poll_attempt`` is the attempt the next request carries, or None to stop,
    so whether the browser asks again is decided here and never in the markup.

    The chain keeps asking while the cache is cold or a probe is in flight, so a
    render of the pre-probe entry still collects the probe's result.  It is
    bounded by ``POLL_ATTEMPT_CAP`` with nothing in flight and by
    ``POLL_PROBE_ATTEMPT_CAP``, sized to cover a probe's worst case, while one
    holds the lock; even the larger is a cap, because ``Lock.locked()`` stays
    true forever if the holder dies.  ``gave_up`` needs a cold cache, because
    its line says nothing has been checked.
    See docs/explanation/decisions/0011-lazy-bounded-health-strip.md.

    Args:
        svc: The app's services, for the check cache and the worker.
        attempt: Which attempt produced this render.  Zero for a page render
            and for the out-of-band strip, both of which start a fresh chain;
            the browser carries the rest back in the query string.
        scan_active: Whether a scan holds the scanner, when the caller knows
            better than the worker's record; None reads that record.  A scan
            submit passes True, because its job does not exist yet.

    Returns:
        ``checks``, ``checking_rows``, ``freshness_line``, ``scan_active`` and
        ``poll_attempt``.

    """
    # Read once, before the cache, so both decisions below agree and a probe
    # finishing between the two reads is seen as in flight.  ``locked()``
    # observes and never acquires.
    probe_in_flight = svc.refresher.probe_in_flight
    cached: CachedChecks = svc.checks.current()
    # The worker's own record of a job in flight, not the scanner gate: reading
    # the gate would mean acquiring it, and a render is not allowed to contend
    # for the lock a live scan holds.
    scanning: bool = (
        svc.worker.current_job_id is not None if scan_active is None else scan_active
    )
    applicable_cap = POLL_PROBE_ATTEMPT_CAP if probe_in_flight else POLL_ATTEMPT_CAP
    cold = cached.results is None
    gave_up = cold and attempt >= applicable_cap
    still_checking = (
        cold and probe_in_flight and not gave_up and attempt >= POLL_ATTEMPT_CAP
    )
    keep_asking = cold or probe_in_flight
    return {
        "checks": cached.results,
        "checking_rows": _CHECKING_ROWS,
        "freshness_line": _poll_line(
            cached,
            scan_active=scanning,
            gave_up=gave_up,
            still_checking=still_checking,
        ),
        "scan_active": scanning,
        "poll_attempt": (
            attempt + 1 if keep_asking and attempt < applicable_cap else None
        ),
    }


def checks_fallback_context() -> dict[str, object]:
    """
    Build the strip the route falls back to when its own render raises.

    Every value is a developer-authored constant, so no part of the exception
    reaches a page the whole LAN can read (ASVS 4.0.3 V7.4.1); the exception
    goes to ``logger.exception``.  The render that would have read the cache and
    the worker is the one that failed, so the body asserts nothing about either:
    placeholder rows, ``POLL_GAVE_UP_LINE`` naming the Check again button still
    on the page, and no scan.  ``poll_attempt`` is None, so the body carries no
    trigger and the browser stops asking.

    Returns:
        The same keys ``checks_context`` returns.

    """
    return {
        "checks": None,
        "checking_rows": _CHECKING_ROWS,
        "freshness_line": POLL_GAVE_UP_LINE,
        "scan_active": False,
        "poll_attempt": None,
    }
