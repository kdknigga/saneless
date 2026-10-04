"""
The checks strip one page shows: its six rows and the sentence under them.

The strip is rendered by a full page, by its own poll, by Check again and out
of band by a scan submit, and every one of them must show the same rows and
the same freshness line for the same cache.  The context they render from is
built here, from the check cache, the refresher and the worker, and never by
probing: a page waits on nothing but a cache read.  The templates author none
of the wording; every sentence and glyph comes from ``saneless.checks`` and
``saneless.vocabulary`` through this module.
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

# The freshness line's variants, composed here rather than in the
# template: the strip's templates own no vocabulary, and a page that assembled
# its own prose would be a second place for the copy to drift.  The dash is
# U+2014 with spaces on both sides.
_PAUSED_PREFIX: Final = "Paused during scan — "
_COLD_PAUSED_LINE: Final = f"{_PAUSED_PREFIX}not checked yet."


@dataclass(frozen=True, slots=True)
class _CheckingRow:
    """
    One cold-start placeholder row, before any probe has happened.

    It is not a :class:`~saneless.checks.CheckResult` because "we have not
    looked yet" is not one of the three ``CheckState`` members, and inventing a
    fourth would owe an exit-code rule to ``saneless doctor`` for a state the
    CLI cannot ever be in -- it probes synchronously and always has an answer.
    So the row carries the class, glyph and screen-reader word directly, and
    every one of them is a constant imported from ``saneless.checks``: the
    template still authors none of them.

    Only ``key`` varies, so one row per ``CheckKey`` is built once at import.

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


# One placeholder per CheckKey, in member order, so a cold strip still renders
# six *named* rows rather than an empty list that reads as "nothing to report".
_CHECKING_ROWS: Final = tuple(_CheckingRow(key=key) for key in CheckKey)


def _freshness_line(cached: CachedChecks, *, scan_active: bool) -> str:
    """
    Compose the one sentence under the rows, for each situation.

    Two axes produce the variants: whether any results exist yet, and
    whether a scan is holding the scanner.  The paused wording is why the
    checks can be skipped during a scan at all -- a strip that silently showed a
    half-hour-old Scanner row during a scan would be lying by omission, and one
    that blanked would throw away the rows that are still true.

    The timestamp goes through the shared ``local_time`` filter, which is the
    same object ``saneless doctor``'s table uses, so the two surfaces cannot
    disagree about the zone or the format.

    Args:
        cached: The cache snapshot this render is showing.
        scan_active: Whether the worker currently has a job in flight.

    Returns:
        The exact sentence for this situation.

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

    ``_freshness_line`` answers "how fresh is what is on the page", which is
    the question whenever the poll is still an ordinary one.  Two endings
    replace it, and neither is about freshness at all.

    A chain that ran out of attempts with nothing in flight says so and points
    at the ``Check again`` button, which is the one way forward left.  A chain
    that is still asking because a probe demonstrably holds the single-flight
    lock says the first check is still running instead: the button that
    ``POLL_GAVE_UP_LINE`` names starts the very probe that is already running,
    so naming it there would be advice that cannot help.  Once the
    larger cap is reached the give-up line comes back even over a probe that is
    still held, because the chain has stopped: the button is again the only
    thing that can put an answer on the page, and a lock a dead thread holds
    stays held forever.  The two flags are mutually exclusive by construction
    -- ``still_checking`` is false whenever ``gave_up`` is true -- so the order
    here does not decide between them.

    Args:
        cached: The cache snapshot this render is showing.
        scan_active: Whether the worker currently has a job in flight.
        gave_up: Whether the chain ended with nothing in flight and nothing
            ever checked.
        still_checking: Whether the chain is past ``POLL_ATTEMPT_CAP`` on a
            cold cache with a probe demonstrably in flight.

    Returns:
        The exact sentence this render puts under the rows.

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

    This reads the cache and never probes.  Probing inside a request handler is
    what an unplugged scanner host would make hang -- a sane-net connect that
    Linux retries six times costs roughly two minutes inside a blocking C call,
    and a page that waited for it would be a page that never arrives.  The
    background refresher is what fills the cache; this only reads what is
    already there.

    ``checks`` is ``None`` on a cold cache, and that is the primary fact the
    template branches on: no results means the placeholder rows are drawn,
    results means the real ones.

    ``poll_attempt`` is the second.  It is the number the *next* request should
    carry, or ``None`` when there is to be no next request -- either because
    there is nothing left to wait for, which is the cold-start ending and still
    the one that matters, or because the applicable cap has been reached, which
    is how a chain ends when results never arrive.  The template emits its
    request attributes only when this is set, so "should the browser ask
    again" is decided here and never in the markup.

    There are two caps, and which one applies is decided by the same
    ``probe_in_flight`` read the disjunct below uses.  With nothing
    in flight the bound is ``POLL_ATTEMPT_CAP`` -- ten attempts, about twenty
    seconds -- which is the case that cap was sized for: a refresher thread
    that has died and a tab left open in front of it.  While a checker
    demonstrably holds the single-flight lock the bound is
    ``POLL_PROBE_ATTEMPT_CAP`` instead, about three minutes, because the worst
    case this application's own probe can cost is the socket pre-probe, plus
    the listing child's deadline (``LISTING_DEADLINE_SECONDS``, 30 s), plus
    unbounded name resolution, and a chain that stopped at twenty seconds
    never collected the answer it was waiting for.  The three-minute cap still
    covers that sum.
    The larger window is still a cap: ``Lock.locked()`` stays true forever if
    the holder dies.

    "Nothing left to wait for" is two facts, not one.
    An empty cache is the cold start.  A probe in flight is the case where
    results *do* exist but the answer on the page is about to be superseded: the
    probe has not stored yet, so this render is of the pre-probe entry, and with
    only the first fact nothing on the page was ever going to fetch the result
    the probe lands a second later.  The strip was refetched by another click,
    the terminal-state reload, or a page load -- so the Check again button could
    visibly do nothing.  What the disjunct costs: a render landing during a
    background probe issues a small, capped number of extra cache reads before
    it settles.  Each is a cache read and never a probe, and the count is
    bounded by ``POLL_PROBE_ATTEMPT_CAP``.
    ``probe_in_flight`` is a ``locked()`` read and two counter reads, never an
    acquire, so no request thread can be parked behind the probe it is asking
    about.  It is also true for a Check again probe the refresher has been
    asked for and not yet finished, which is what keeps a click answered as
    pending asking for its result.

    ``gave_up`` means "this cold chain has stopped", and it is measured against
    the *applicable* cap rather than always against ``POLL_ATTEMPT_CAP``.  It
    still requires a cold cache, because its line says the checks have not run
    yet and that would be a lie printed beside rows that did run -- so a
    settling poll that runs out of attempts leaves the normal last-checked line
    alone.  What changed is that a cold chain at ``POLL_ATTEMPT_CAP`` with a
    probe in flight has *not* stopped: it keeps asking up to
    ``POLL_PROBE_ATTEMPT_CAP`` and shows ``POLL_STILL_CHECKING_LINE`` in place
    of the cold-start ``Checking…`` line.  The give-up line is
    withheld there because it points at the ``Check again`` button, and a click
    on that button while a probe holds the lock collapses into the probe
    already running -- advice that cannot help.  At the larger cap it comes
    back, because by then the chain really has stopped and the button really is
    the only way forward.  At whichever cap ends the chain on a cold cache the
    freshness line is replaced rather than augmented: there is no freshness to
    report, because nothing has ever been checked.

    Args:
        svc: The app's services, for the check cache and the worker.
        attempt: Which attempt produced this render.  Zero for a page render
            and for the out-of-band strip, both of which start a fresh chain;
            the browser carries the rest back in the query string.
        scan_active: Whether a scan holds the scanner, when the caller knows
            better than the worker's record; None reads that record.  A scan
            submit passes True: it builds this before its job exists, so the
            record still says idle, yet the strip it carries is due to say
            the checks are paused.

    Returns:
        ``checks``, ``checking_rows``, ``freshness_line``, ``scan_active`` and
        ``poll_attempt``.

    """
    # Read once, so the two decisions below cannot disagree about it.  This is
    # `Lock.locked()`: an observation, never an acquire.  It is read before the
    # cache so that a probe finishing between the two reads is seen as in
    # flight, which asks once more and collects what it stored.
    probe_in_flight = svc.refresher.probe_in_flight
    cached: CachedChecks = svc.checks.current()
    # The worker's own record of a job in flight, not the scanner gate: reading
    # the gate would mean acquiring it, and a render is not allowed to contend
    # for the lock a live scan holds.
    scanning: bool = (
        svc.worker.current_job_id is not None if scan_active is None else scan_active
    )
    # Which cap applies is the probe's decision, and it is taken once from the
    # one read above so the line and the trigger cannot disagree about it.
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

    Every value here is a developer-authored constant, and that is the whole
    point: this body is rendered on a page the whole LAN can read, so no part
    of the exception that produced it may reach the context (ASVS 4.0.3 V7.4.1).  The
    exception goes to ``logger.exception`` instead.

    ``checks`` is ``None`` and ``checking_rows`` is the cold-start six, so the
    strip shows six *named* rows rather than an empty list that would read as
    "nothing to report".  ``freshness_line`` is ``POLL_GAVE_UP_LINE``, and not
    because nothing has been checked: the guard around this body covers the
    whole render, so a raise from the worker's job lookup, the refresher's
    lock or the clock produces it on an appliance whose cache may well hold
    six true rows.  It is chosen because the render that would have read the
    cache is the one that failed, so this body asserts nothing about the cache
    beyond the fact that it could not be shown -- and the line names the
    ``Check again`` button that is still on the page, which is the one way
    forward left.  ``scan_active`` is ``False`` for the same
    reason: the render that would have read the worker is the one that failed.
    ``poll_attempt`` is ``None``, and that is what ends the chain: the template
    emits its request attributes only when it is set, so the body this context
    renders carries no trigger and the browser stops asking.

    Returns:
        The same five keys ``checks_context`` returns.

    """
    return {
        "checks": None,
        "checking_rows": _CHECKING_ROWS,
        "freshness_line": POLL_GAVE_UP_LINE,
        "scan_active": False,
        "poll_attempt": None,
    }
