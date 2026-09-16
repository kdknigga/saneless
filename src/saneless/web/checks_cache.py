"""The status strip's TTL cache, which keeps the previous answer on purpose."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from saneless.checks import CheckResult

__all__ = ["CachedChecks", "CheckCache"]


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
    TTL, a timestamp taken at store time, and one lock with a comment naming
    exactly what it guards.  It deviates from that class in three deliberate
    ways:

    1. **The clock is a constructor parameter.**  ``MetadataCache`` calls
       ``time.monotonic()`` inline (``cache.py:54`` and ``cache.py:66``), which
       is the sole reason ``tests/test_cache.py:28`` has to sleep for 1.1 real
       seconds to watch a TTL expire.  Injecting the clock lets every test here
       advance a float instead, so this cache costs the suite no wall-clock
       time and has no timing flake to inherit.
    2. **It keeps last-known-good.**  ``MetadataCache.get_or_fetch``
       (``cache.py:69-116``) re-raises when a fetch fails and caches nothing;
       ``routes.py:79-86`` is what swallows that and substitutes ``[]``.  D-08
       needs the opposite: an expired or unrefreshable entry keeps its results
       and its wall-clock stamp so the strip can say "Paused during scan --
       last checked 14:02" rather than going blank.
    3. **It is typed to ``CheckResult``,** not ``list[dict[str, object]]``.
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
        # Guards every read and every rebind of self._entry, and nothing else;
        # never a probe.  The entry is a frozen _Entry rebound as a whole, so
        # holding this for the rebind is what makes "results, stamp and
        # checked_at always belong to the same store" true for a concurrent
        # reader (T-30-30).
        self._lock = threading.Lock()
        self._entry: _Entry | None = None

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
