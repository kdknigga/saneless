"""The status strip's TTL cache, which keeps the previous answer on purpose."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from saneless.checks import CheckResult

__all__ = ["MIN_MANUAL_REFRESH_SECONDS", "CachedChecks", "CheckCache"]

# The shortest gap between two honoured Refresh clicks (WR-05).  Two seconds is
# below the interval a human clicks at -- nobody presses Check again twice in
# the same two seconds and expects two different answers -- and far above the
# rate at which a scripted loop is a problem, which is the only case this
# exists for.  It does not break D-09's promise either: that promise is "do not
# make somebody wait out a 30 s TTL after plugging the scanner back in", and a
# 2 s floor leaves it intact.  Deliberately not configurable, and read at call
# time, so a test can shorten it.
MIN_MANUAL_REFRESH_SECONDS: Final = 2.0


@dataclass(frozen=True, slots=True)
class CachedChecks:
    """
    What the cache hands a renderer: the results, when they were taken, and how old.

    Frozen for the same reason :class:`~saneless.checks.CheckResult` is frozen.
    This is a snapshot of probes that have already happened, and the results it
    carries are a ``tuple``, so nothing between the cache and a template can
    edit a value the next reader will also see.

    ``results`` is ``None`` only at cold start, before the refresher's first
    tick -- that is the state D-06 renders as ``Checking…`` per row.  It is
    never ``None`` again afterwards: an expired entry is marked ``stale`` and
    returned unchanged rather than discarded, because D-08's
    "Paused during scan -- last checked 14:02" needs the previous results *and*
    their age, and a cache that threw them away could offer neither.

    Attributes:
        results: The last results stored, or None before the first store.
        checked_at: The aware wall-clock time of that store, or None.
        stale: True when results exist and are older than the TTL.  False at
            cold start: with nothing stored there is nothing to be stale.
        age_seconds: How long ago the results were stored, or None.

    """

    results: tuple[CheckResult, ...] | None
    checked_at: datetime | None
    stale: bool
    age_seconds: float | None


@dataclass(frozen=True, slots=True)
class _Entry:
    """One stored set of results with both of its timestamps."""

    results: tuple[CheckResult, ...]
    # The monotonic reading the TTL is measured against.  Monotonic because a
    # system clock step must not make a fresh entry look an hour old.
    stamp: float
    # The aware wall-clock time the same store happened at.  A separate field
    # because monotonic readings are meaningless in isolation and the strip
    # renders an actual time of day; neither one can do the other's job.
    checked_at: datetime


class CheckCache:
    """
    The status strip's cache: one TTL, one entry, and the previous answer kept.

    The shape is copied from :class:`~saneless.web.cache.MetadataCache` -- a
    TTL, a timestamp taken at store time, a clock passed to the constructor,
    and one lock with a comment naming exactly what it guards.  Both caches
    take the clock as a parameter so their tests advance a float instead of
    waiting out a TTL, and cost the suite no wall-clock time.  This one
    deviates from that class in two deliberate ways:

    1. **Its previous answer is always on show.**  ``MetadataCache`` keeps a
       last good copy too, but hands it out only after a refresh has failed;
       otherwise an expired entry is a miss.  Here an expired or unrefreshable
       entry is returned with its results and its wall-clock stamp, marked
       stale, so the strip can say "Paused during scan -- last checked 14:02"
       rather than going blank.
    2. **It is typed to ``CheckResult``,** not ``list[dict[str, object]]``.
       ``run_checks`` already returns a ``tuple`` of frozen results, so the
       cached value is immutable all the way down and a renderer cannot mutate
       what the next renderer will read.

    There is one entry rather than a keyed store: the five checks are run and
    shown together, so there is nothing to key on.

    Args:
        ttl: How many seconds a stored entry counts as fresh.
        clock: The monotonic source the TTL is measured with.

    """

    def __init__(
        self,
        # D-03: 30 seconds is long enough that a reload and a few htmx swaps
        # never re-probe the network, and short enough that unplugging the
        # scanner surfaces before the operator gives up; the Refresh button
        # covers impatience.  A default rather than a module constant, so it is
        # read at call time and a caller or a test can shorten it.
        ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize an empty cache with the given TTL and clock."""
        self._ttl = ttl
        self._clock = clock
        # Guards every read and every rebind of self._entry and of
        # self._last_manual_claim, and nothing else; never a probe.  The entry
        # is a frozen _Entry rebound as a whole, so holding this for the rebind
        # is what makes "results, stamp and checked_at always belong to the
        # same store" true for a concurrent reader (T-30-30).  The claim stamp
        # shares it because its read-and-rebind has to be one step for two
        # request threads arriving together to get one grant between them
        # (T-30-26-01); the two pieces of state are otherwise unrelated.
        self._lock = threading.Lock()
        self._entry: _Entry | None = None
        # When a manual refresh was last granted, or None for "never".  None
        # rather than 0.0 for the reason CheckRefresher._last_watched is None:
        # time.monotonic() on Linux counts from boot, so 0.0 would sit inside
        # the interval and refuse the first click on a freshly booted
        # appliance -- the one click that certainly deserves a probe.
        self._last_manual_claim: float | None = None

    def current(self) -> CachedChecks:
        """
        Return the cached results, whether or not they are still fresh.

        This never discards and never probes, so a page render can call it
        while the scanner host is unplugged and pay nothing (D-04).

        Returns:
            The last-known-good snapshot, marked stale once the TTL has passed.

        """
        with self._lock:
            entry = self._entry
        if entry is None:
            return CachedChecks(
                results=None, checked_at=None, stale=False, age_seconds=None
            )
        age = self._clock() - entry.stamp
        return CachedChecks(
            results=entry.results,
            checked_at=entry.checked_at,
            stale=age >= self._ttl,
            age_seconds=age,
        )

    def store(self, results: Sequence[CheckResult]) -> None:
        """
        Replace the cached entry with a fresh set of results.

        Both timestamps are taken here rather than by the caller, so the TTL
        always measures from the moment the results landed.

        Args:
            results: The results a check run just produced.

        """
        entry = _Entry(
            results=tuple(results),
            stamp=self._clock(),
            checked_at=datetime.now(tz=UTC),
        )
        with self._lock:
            self._entry = entry

    def is_fresh(self) -> bool:
        """
        Report whether a probe would be redundant right now.

        This is the refresher's guard: a cold cache is not fresh, so the first
        tick with a watcher present does work.

        Returns:
            True when results are stored and younger than the TTL.

        """
        entry = self.current()
        return entry.results is not None and not entry.stale

    def claim_manual_refresh(
        self, min_interval: float = MIN_MANUAL_REFRESH_SECONDS
    ) -> float | None:
        """
        Say whether a manual refresh may probe right now, and record that it did.

        This is a floor under a deliberate bypass, not a second TTL.  D-09's
        Refresh button exists precisely to ignore :meth:`is_fresh`, so the TTL
        cannot bound its cost; without something that can, a click is an
        unbounded probe.  ``POST /api/checks/refresh`` is unauthenticated by
        design on a LAN and ``CrossOriginGuard`` allows a request carrying
        neither ``Sec-Fetch-Site`` nor ``Origin`` -- documented, and exactly
        what ``curl`` in a loop sends -- so every call issues a Paperless
        request, up to N saned TCP dials and two filesystem writes.  The worst
        of that is not the traffic: ``ScanWorker._scan_job`` blocks on a
        scanner gate that is not a fair lock, so an unbounded loop can park a
        submitted job whose row already reads ``SCANNING`` (WR-05).

        The claim stamp is deliberately not the entry's own timestamp.
        Conflating them would let a refused click reset the freshness of
        results it never produced, and would tie a 2 s floor to a 30 s TTL for
        no better reason than both being durations.  So a :meth:`store` grants
        nothing, a grant stores nothing, and an expired entry grants nothing.

        A refusal changes no state.  That matters: if a refusal stamped, a
        caller hammering the endpoint just inside the interval would hold the
        floor shut for ever and the household member standing at the appliance
        would never get their probe.

        The clock is read before the lock is taken, so this never calls out
        while holding it -- the same discipline the lock's comment states.

        Args:
            min_interval: The shortest gap between two grants, in seconds.

        Returns:
            The stamp this grant recorded, which the caller may hand to
            :meth:`release_manual_claim` to give the grant back, or ``None``
            when it is too soon to probe.

            The truthiness of that value is not the contract: callers test
            against ``None``.  A stamp is a ``time.monotonic()`` reading, and
            on Linux that counts from boot, so 0.0 is a reading a grant can
            really record and it is falsey.  A caller branching on truthiness
            would read the first click after a boot as a refusal -- the one
            click that certainly deserves a probe, for the same reason
            ``_last_manual_claim`` starts at ``None`` rather than at 0.0.

        """
        now = self._clock()
        with self._lock:
            last = self._last_manual_claim
            if last is not None and now - last < min_interval:
                return None
            self._last_manual_claim = now
            return now

    def release_manual_claim(self, stamp: float) -> bool:
        """
        Give a granted claim back, because the probe it bought never happened.

        ``POST /api/checks/refresh`` claims before it probes, and it has to:
        the claim is what decides whether it may probe at all.  But
        ``CheckRefresher.probe_now`` collapses into an in-flight probe and
        does nothing, and a click that collapsed spent the floor for no probe
        -- so the clicker's very next press, inside two seconds, was refused
        for traffic nobody generated.  That is WR-03's second consequence: the
        button appearing to do nothing, twice in a row.  This hands the claim
        back on exactly that branch.

        The clear is a compare-and-clear, so a caller can only ever give back
        its own grant.  The stamp handed in is the one
        :meth:`claim_manual_refresh` returned; the claim is cleared when that
        is still the recorded one and left alone when it is not, so a release
        arriving *after* somebody else's grant is a no-op rather than a hole
        in the floor (R3-IN-03).  This used to be an argument instead of a
        check -- the one caller releases microseconds after its grant on the
        same thread, and a competing claimer inside that window is refused
        without writing -- and the argument was true of that call site and of
        nothing else.  A retry, a second caller, or a handler that grew a
        second release would each have lowered a floor whose whole job is to
        bound what an unauthenticated LAN endpoint can make the appliance do.

        It cannot be abused to defeat the floor (WR-05, T-30-29-01).  The
        release happens only where ``probe_now`` returned False, and that
        branch issued no Paperless request, no saned TCP dial and no
        filesystem write, so a scripted loop that always collides always gets
        its claim back and still generates zero probe traffic.  The moment a
        probe is actually granted, the stamp stands and the next call inside
        the interval is refused like any other.

        Args:
            stamp: The value :meth:`claim_manual_refresh` returned for the
                grant being given back.

        Returns:
            Whether this call cleared the claim, which is False when the stamp
            is not the recorded one and when no claim is recorded at all.

        """
        with self._lock:
            if self._last_manual_claim != stamp:
                return False
            self._last_manual_claim = None
            return True
