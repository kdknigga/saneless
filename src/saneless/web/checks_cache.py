"""The status strip's TTL cache, which keeps the previous answer on purpose."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from saneless.web.throttle import MIN_MANUAL_REFRESH_SECONDS, MinimumInterval

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from saneless.checks import CheckResult

__all__ = ["MIN_MANUAL_REFRESH_SECONDS", "CachedChecks", "CheckCache"]


@dataclass(frozen=True, slots=True)
class CachedChecks:
    """
    What the cache hands a renderer: the results, when they were taken, and whether stale.

    Frozen, with a ``tuple`` of results, so nothing between the cache and a
    template can edit a value the next reader will also see.  ``results`` is
    ``None`` only before the first store; an expired entry is kept and marked
    ``stale``, because the strip shows the previous results with their age.

    Attributes:
        results: The last results stored, or None before the first store.
        checked_at: The aware wall-clock time of that store, or None.
        stale: True when results exist and are older than the TTL.  False at
            cold start: with nothing stored there is nothing to be stale.

    """

    results: tuple[CheckResult, ...] | None
    checked_at: datetime | None
    stale: bool


@dataclass(frozen=True, slots=True)
class _Entry:
    """One stored set of results with both of its timestamps."""

    results: tuple[CheckResult, ...]
    # Monotonic, so a system clock step cannot make a fresh entry look old.
    stamp: float
    # The wall-clock time of the same store, which the strip renders.
    checked_at: datetime


class CheckCache:
    """
    The status strip's cache: one TTL, one entry, and the previous answer kept.

    Unlike :class:`~saneless.web.cache.MetadataCache`, which hands out its
    last good copy only after a refresh has failed, an expired entry here is
    always returned, marked stale, so the strip never goes blank.  There is one
    entry rather than a keyed store, because every check is run and shown
    together.

    Args:
        ttl: How many seconds a stored entry counts as fresh.
        clock: The monotonic source the TTL is measured with.

    """

    def __init__(
        self,
        # Long enough that a reload and a few htmx swaps never re-probe, short
        # enough that an unplugged scanner surfaces quickly.  The default is
        # bound when the class is defined; a caller or a test that wants
        # another ttl passes it.
        ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize an empty cache with the given TTL and clock."""
        self._ttl = ttl
        self._clock = clock
        # Guards every read and rebind of self._entry, never a probe.  The
        # entry is rebound whole, so its results and both stamps always belong
        # to the same store.
        self._lock = threading.Lock()
        self._entry: _Entry | None = None
        # The manual-refresh floor has its own lock, and reads the clock
        # through _now so the cache and its floor share one clock.
        self._manual_floor = MinimumInterval(clock=self._now)

    def _now(self) -> float:
        """
        Read this cache's clock at call time.

        Returns:
            The current monotonic reading of whichever clock the cache holds.

        """
        return self._clock()

    def current(self) -> CachedChecks:
        """
        Return the cached results, whether or not they are still fresh.

        This never discards and never probes, so a page render can call it
        while the scanner host is unplugged and pay nothing.

        Returns:
            The last-known-good snapshot, marked stale once the TTL has passed.

        """
        with self._lock:
            entry = self._entry
        if entry is None:
            return CachedChecks(results=None, checked_at=None, stale=False)
        age = self._clock() - entry.stamp
        return CachedChecks(
            results=entry.results,
            checked_at=entry.checked_at,
            stale=age >= self._ttl,
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

        The Refresh button bypasses :meth:`is_fresh`, and the endpoint is
        reachable by a scripted LAN client, so without this floor each click is
        an unbounded probe that can park a submitted scan behind the unfair
        scanner gate.  The claim stamp is separate from the entry's, so a store
        grants nothing and a grant stores nothing, and a refusal changes no
        state, so hammering inside the interval cannot hold the floor shut.

        Args:
            min_interval: The shortest gap between two grants, in seconds.

        Returns:
            The stamp this grant recorded, which the caller may hand to
            :meth:`release_manual_claim`, or ``None`` when it is too soon to
            probe.  Test against ``None``: on Linux a monotonic stamp counts
            from boot, so a real grant can record 0.0.

        """
        return self._manual_floor.claim(min_interval)

    def release_manual_claim(self, stamp: float) -> bool:
        """
        Give a granted claim back, because the probe it bought never happened.

        The refresh route releases only when ``request_probe`` collapsed into
        a probe already in flight, so a clicker's next press is not refused
        for traffic nobody generated, and a loop that always collides still
        generates no probe traffic.  The release is a compare-and-clear, so a
        caller can give back only its own grant, never somebody else's.

        Args:
            stamp: The value :meth:`claim_manual_refresh` returned for the
                grant being given back.

        Returns:
            Whether this call cleared the claim, which is False when the stamp
            is not the recorded one and when no claim is recorded at all.

        """
        return self._manual_floor.release(stamp)
