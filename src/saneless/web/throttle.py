"""
Rate floors for the web endpoints that reach paperless-ngx or probe on demand.

The endpoints protected here are unauthenticated by design on a LAN, and a
scripted client passes ``CrossOriginGuard``, so without a floor a loop is
unbounded token-bearing traffic to paperless-ngx.  Every such endpoint uses
one of these two rather than a timestamp dict of its own:

- :class:`MinimumInterval` grants at most one call per interval and refuses
  the rest.  ``CheckCache``'s manual-refresh floor and each
  ``POST /api/cache/invalidate`` resource hold one.
- :class:`SingleFlightResult` lets one caller compute while the others reuse
  the last answer.  ``GET /api/paperless/test`` holds one.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "MIN_MANUAL_REFRESH_SECONDS",
    "PAPERLESS_TEST_WAIT_SECONDS",
    "MinimumInterval",
    "SingleFlightResult",
]

# The shortest gap between two honoured calls to anything on a floor: below
# the interval a human clicks at, far above a scripted loop's rate.  Read at
# call time, so a test can shorten it.
MIN_MANUAL_REFRESH_SECONDS: Final = 2.0

# How long the first connection test's followers wait for the in-flight one.
# It must outlast the probe's connect and read budgets, so a follower shares
# that answer, and stay below the idle server's stop grace period, so no waiting
# request holds a shutdown open.
PAPERLESS_TEST_WAIT_SECONDS: Final = 8.0


class MinimumInterval:
    """
    A floor under a call that must not run more often than every few seconds.

    A claim either is granted, and records its stamp, or is refused and
    changes nothing.  A granted claim can be given back with :meth:`release`
    when the work it paid for never happened.

    One lock guards the stamp and nothing else; the clock is read before the
    lock is taken, so this never calls out while holding it.

    Args:
        clock: The monotonic source the interval is measured with.

    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """Initialize a floor that has granted nothing yet."""
        self._clock = clock
        # Guards self._last_claim only; its read-and-rebind is one step, so two
        # request threads arriving together get one grant between them.
        self._lock = threading.Lock()
        # None, not 0.0: time.monotonic() on Linux counts from boot, so 0.0
        # would refuse the first call on a freshly booted appliance.
        self._last_claim: float | None = None

    def claim(self, min_interval: float = MIN_MANUAL_REFRESH_SECONDS) -> float | None:
        """
        Say whether the protected call may run now, and record that it did.

        A refusal changes no state, so a caller hammering just inside the
        interval cannot hold the floor shut.

        Args:
            min_interval: The shortest gap between two grants, in seconds.

        Returns:
            The stamp this grant recorded, which the caller may hand to
            :meth:`release` to give the grant back, or ``None`` when it is too
            soon.  Test against ``None``: a grant can really record 0.0.

        """
        now = self._clock()
        with self._lock:
            last = self._last_claim
            if last is not None and now - last < min_interval:
                return None
            self._last_claim = now
            return now

    def release(self, stamp: float) -> bool:
        """
        Give a granted claim back, because the work it paid for never happened.

        A compare-and-clear: the claim is cleared only while ``stamp`` is still
        the recorded one, so a release after somebody else's grant cannot open
        a hole in the floor.

        Args:
            stamp: The value :meth:`claim` returned for the grant being given
                back.

        Returns:
            Whether this call cleared the claim, which is False when the stamp
            is not the recorded one and when no claim is recorded at all.

        """
        with self._lock:
            if self._last_claim != stamp:
                return False
            self._last_claim = None
            return True


class SingleFlightResult[T]:
    """
    One shared result, computed by one caller at a time and reused briefly.

    A result younger than ``ttl`` is handed to every caller without
    computing.  Past that, the first caller to arrive computes -- it is the
    leader -- and every caller that arrives while it does is a follower.

    **A follower with a previous result does not wait.**  Each plain ``def``
    handler holds one of anyio's 40 worker threads while it runs, so followers
    blocked behind a leader stuck on an unreachable paperless-ngx would exhaust
    the pool and stall every route, ``/health`` included.

    Only before any result exists does a follower wait, bounded by
    ``wait_bound``, and at most ``max_waiters`` wait at once; the rest get a
    ``TimeoutError``.

    ``compute`` must not raise: an outcome worth sharing, failure included, is
    a value.  If it raises regardless, nothing is stored, the exception
    reaches that caller and the flight is released for the next one.

    Args:
        ttl: How many seconds a stored result is reused for.
        wait_bound: How many seconds a follower with no result to fall back
            on waits for the leader before giving up.
        max_waiters: How many followers with no result to fall back on may
            wait at once.
        clock: The monotonic source the TTL is measured with.

    """

    def __init__(
        self,
        *,
        ttl: float,
        wait_bound: float,
        max_waiters: int = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize an empty holder with the given TTL, bounds and clock."""
        self._ttl = ttl
        self._wait_bound = wait_bound
        self._max_waiters = max_waiters
        self._clock = clock
        # Held by the leader for the length of one compute, and by nobody
        # else.  Never taken while _state_lock is held.
        self._flight = threading.Lock()
        # Guards every read and every rebind of self._last and of
        # self._waiters, and nothing else; never a compute.
        self._state_lock = threading.Lock()
        self._waiters = 0
        # Rebound whole, so a reader never pairs a value with another's stamp.
        self._last: tuple[T, float] | None = None

    def get(self, compute: Callable[[], T]) -> T:
        """
        Return a fresh result, computing it here only when nobody else is.

        Args:
            compute: Produces the result.  Must not raise.

        Returns:
            The cached result while it is fresh; otherwise this caller's own
            result as leader, or the previous result while a leader computes.

        Raises:
            TimeoutError: If no result exists yet and either the in-flight
                leader did not finish within the wait bound or as many
                followers as may wait already are.

        """
        last = self._fresh()
        if last is not None:
            return last[0]
        if self._flight.acquire(blocking=False):
            return self._lead(compute)
        previous = self._read()
        if previous is not None:
            return previous[0]
        if not self._wait_for_leader():
            msg = "the in-flight computation did not finish within the wait bound"
            raise TimeoutError(msg)
        # The leader this follower waited on has stored its result, unless it
        # raised; share it rather than repeat the work it just did.
        waited_for = self._read()
        if waited_for is not None:
            self._flight.release()
            return waited_for[0]
        return self._lead(compute)

    def _wait_for_leader(self) -> bool:
        """
        Wait, bounded, for the flight lock, unless enough followers already do.

        Returns:
            Whether the flight lock was acquired.

        Raises:
            TimeoutError: If ``max_waiters`` followers are already waiting.

        """
        with self._state_lock:
            if self._waiters >= self._max_waiters:
                msg = "too many callers are already waiting for the computation"
                raise TimeoutError(msg)
            self._waiters += 1
        try:
            return self._flight.acquire(timeout=self._wait_bound)
        finally:
            with self._state_lock:
                self._waiters -= 1

    def _lead(self, compute: Callable[[], T]) -> T:
        """
        Compute and store a result, with the flight lock already held.

        A leader re-checks first: another leader may have stored a fresh
        result between this caller's cache read and its acquire.

        Returns:
            The stored result.

        """
        try:
            last = self._fresh()
            if last is not None:
                return last[0]
            value = compute()
            stored = (value, self._clock())
            with self._state_lock:
                self._last = stored
            return value
        finally:
            self._flight.release()

    def _read(self) -> tuple[T, float] | None:
        """
        Return the last stored result and its stamp, fresh or not.

        Returns:
            The stored pair, or None before the first store.

        """
        with self._state_lock:
            return self._last

    def _fresh(self) -> tuple[T, float] | None:
        """
        Return the last stored result and its stamp while it is younger than the TTL.

        Returns:
            The stored pair, or None when there is none or it has expired.

        """
        last = self._read()
        if last is None or self._clock() - last[1] >= self._ttl:
            return None
        return last
