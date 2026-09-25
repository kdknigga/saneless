"""
Rate floors for the web endpoints that reach paperless-ngx or probe on demand.

The endpoints protected here are unauthenticated by design on a LAN, and
``CrossOriginGuard`` allows a request carrying neither ``Sec-Fetch-Site`` nor
``Origin`` -- which is what ``curl`` in a loop sends.  Without a floor, each
call is one more token-bearing request to paperless-ngx, so a loop is an
unbounded amount of upstream traffic.  The protection is decided here, once,
so every such endpoint gets the same one rather than a timestamp dict of its
own:

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

# The shortest gap between two honoured calls to anything on a floor.  Two
# seconds is below the interval a human clicks at -- nobody presses Check or a
# refresh button twice in the same two seconds and expects two different
# answers -- and far above the rate at which a scripted loop is a problem,
# which is the only case this exists for.  It does not break the Refresh
# button's promise either: that promise is "do not make somebody wait out a
# 30 s TTL after plugging the scanner back in", and a 2 s floor leaves it
# intact.  Deliberately not configurable, and read at call time, so a test can
# shorten it.
MIN_MANUAL_REFRESH_SECONDS: Final = 2.0

# How long the first connection test's followers wait for the in-flight one.
# It only applies before any result exists -- once per process -- and it has
# to outlast the probe it waits on: ``PaperlessClient.test_connection`` with no
# argument runs on the client's own 30 s default timeout, which is what an
# unreachable host costs, so a bound of 35 s lets a follower share that answer
# rather than time out a few milliseconds before it lands.  A leader slower
# than this (a connect and a trickling read both near their budgets) leaves
# the follower a TimeoutError, which the route answers as its usual 502.
PAPERLESS_TEST_WAIT_SECONDS: Final = 35.0


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
        # Guards every read and every rebind of self._last_claim, and nothing
        # else.  The read-and-rebind has to be one step for two request
        # threads arriving together to get one grant between them.
        self._lock = threading.Lock()
        # When a claim was last granted, or None for "never".  None rather
        # than 0.0: time.monotonic() on Linux counts from boot, so 0.0 would
        # sit inside the interval and refuse the first call on a freshly
        # booted appliance -- the one call that certainly deserves to run.
        self._last_claim: float | None = None

    def claim(self, min_interval: float = MIN_MANUAL_REFRESH_SECONDS) -> float | None:
        """
        Say whether the protected call may run now, and record that it did.

        A refusal changes no state.  That matters: if a refusal stamped, a
        caller hammering the endpoint just inside the interval would hold the
        floor shut for ever, and the household member standing at the
        appliance would never get their call through.

        Args:
            min_interval: The shortest gap between two grants, in seconds.

        Returns:
            The stamp this grant recorded, which the caller may hand to
            :meth:`release` to give the grant back, or ``None`` when it is too
            soon.

            The truthiness of that value is not the contract: callers test
            against ``None``.  A stamp is a ``time.monotonic()`` reading, and
            on Linux that counts from boot, so 0.0 is a reading a grant can
            really record and it is falsey.  A caller branching on truthiness
            would read the first call after a boot as a refusal.

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

        The clear is a compare-and-clear, so a caller can only ever give back
        its own grant: the claim is cleared when ``stamp`` is still the
        recorded one and left alone when it is not, so a release arriving
        *after* somebody else's grant is a no-op rather than a hole in the
        floor.  A retry, a second caller or a handler that grew a second
        release cannot lower a floor whose whole job is to bound what an
        unauthenticated LAN endpoint can make the appliance do.

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

    **A follower with a previous result does not wait.**  The web handlers are
    plain ``def`` and each one holds one of anyio's 40 worker threads for as
    long as it runs, so a follower blocked on the leader holds one too.
    Against an unreachable paperless-ngx the leader takes the client's full
    timeout, and a loop of callers blocked behind it would exhaust the pool
    and stall every other route, ``/health`` included.  So the follower is
    answered the last result at once, stale by at most one probe.

    Only before any result exists does a follower wait, because it has
    nothing else to say.  That is once per process, and the wait is bounded
    by ``wait_bound``: a follower that outlasts it gets a ``TimeoutError``.

    ``compute`` must not raise: an outcome worth sharing, failure included, is
    a value.  If it raises regardless, nothing is stored, the exception
    reaches that caller and the flight is released for the next one.

    Args:
        ttl: How many seconds a stored result is reused for.
        wait_bound: How many seconds a follower with no result to fall back
            on waits for the leader before giving up.
        clock: The monotonic source the TTL is measured with.

    """

    def __init__(
        self,
        *,
        ttl: float,
        wait_bound: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize an empty holder with the given TTL, wait bound and clock."""
        self._ttl = ttl
        self._wait_bound = wait_bound
        self._clock = clock
        # Held by the leader for the length of one compute, and by nobody
        # else.  Never taken while _state_lock is held.
        self._flight = threading.Lock()
        # Guards every read and every rebind of self._last, and nothing else;
        # never a compute.
        self._state_lock = threading.Lock()
        # The last result and the monotonic reading it was stored at, as one
        # tuple rebound whole, so a reader never pairs a value with another
        # value's stamp.
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
            TimeoutError: If no result exists yet and the in-flight leader did
                not finish within the wait bound.

        """
        last = self._fresh()
        if last is not None:
            return last[0]
        if self._flight.acquire(blocking=False):
            return self._lead(compute)
        previous = self._read()
        if previous is not None:
            return previous[0]
        if not self._flight.acquire(timeout=self._wait_bound):
            msg = "the in-flight computation did not finish within the wait bound"
            raise TimeoutError(msg)
        # The leader this follower waited on has stored its result, unless it
        # raised; share it rather than repeat the work it just did.
        waited_for = self._read()
        if waited_for is not None:
            self._flight.release()
            return waited_for[0]
        return self._lead(compute)

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
