"""
CheckCache unit tests.

This file deliberately inverts the approach ``tests/test_cache.py`` is forced
into.  That file has to sleep for 1.1 real seconds (``test_cache.py:28``) to
watch a TTL expire, because ``MetadataCache`` calls ``time.monotonic()``
inline at ``cache.py:54`` and ``cache.py:66`` and a test has no way to move it.
``CheckCache`` takes its clock as a constructor parameter instead, so every
assertion about the TTL here advances a float.

Nothing in this file sleeps, and nothing in it ever may: a suite that waits on
the wall clock to observe a timeout is both slow and flaky, and making that
unnecessary is the whole reason the clock is injectable.  A grep for the
sleeping call therefore finds no hit here, which is how the phase-wide "the
count must not rise" gate stays checkable by grep.

Covers requirements: APPL-02.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from saneless.checks import CheckKey, CheckResult, CheckState
from saneless.web.checks_cache import CheckCache

_BARRIER_TIMEOUT_SECONDS = 5.0


class _FakeClock:
    """A monotonic clock the test moves by hand instead of waiting for."""

    def __init__(self, start: float = 100.0) -> None:
        """Start the clock at ``start`` seconds."""
        self.now = start

    def __call__(self) -> float:
        """Return the current fake monotonic reading."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward, the way a real one would move on its own."""
        self.now += seconds


def _results(message: str = "All good.") -> tuple[CheckResult, ...]:
    """Build a small, distinguishable result tuple."""
    return (
        CheckResult(key=CheckKey.SCANNER, state=CheckState.OK, message=message),
        CheckResult(key=CheckKey.PAPERLESS, state=CheckState.OK, message=message),
    )


def test_cold_cache_has_no_results() -> None:
    """A cache nothing has stored into reports no results at all (D-06)."""
    cache = CheckCache(clock=_FakeClock())
    entry = cache.current()
    assert entry.results is None
    assert entry.checked_at is None
    assert entry.age_seconds is None


def test_cold_cache_is_not_fresh() -> None:
    """A cold cache is not fresh, so the refresher's first tick does work."""
    cache = CheckCache(clock=_FakeClock())
    assert cache.is_fresh() is False


def test_stored_results_are_fresh_inside_the_ttl() -> None:
    """Results stored at 100.0 are still fresh at 129.9 with a 30 s TTL (D-03)."""
    clock = _FakeClock(start=100.0)
    cache = CheckCache(clock=clock)
    stored = _results()
    cache.store(stored)
    clock.advance(29.9)
    entry = cache.current()
    assert entry.results == stored
    assert entry.stale is False
    assert cache.is_fresh() is True


def test_expired_entry_returns_the_same_results_marked_stale() -> None:
    """After the TTL the previous results are still returned, flagged stale (D-08)."""
    clock = _FakeClock(start=100.0)
    cache = CheckCache(clock=clock)
    stored = _results()
    cache.store(stored)
    clock.advance(30.1)
    entry = cache.current()
    assert entry.results == stored
    assert entry.stale is True
    assert cache.is_fresh() is False


def test_expired_entry_reports_its_age() -> None:
    """A stale entry carries how old it is, which is what the strip renders."""
    clock = _FakeClock(start=100.0)
    cache = CheckCache(clock=clock)
    cache.store(_results())
    clock.advance(30.1)
    entry = cache.current()
    assert entry.age_seconds is not None
    assert entry.age_seconds == pytest.approx(30.1)


def test_stale_entry_is_never_discarded() -> None:
    """Long past the TTL the last-known-good entry is still readable (Pitfall 6)."""
    clock = _FakeClock(start=100.0)
    cache = CheckCache(clock=clock)
    stored = _results("Scanner is ready.")
    cache.store(stored)
    first_stamp = cache.current().checked_at
    clock.advance(3600.0)
    entry = cache.current()
    assert entry.results == stored
    assert entry.checked_at == first_stamp
    assert entry.stale is True


def test_store_records_an_aware_wall_clock_stamp() -> None:
    """``checked_at`` is timezone-aware, because monotonic cannot render a time."""
    before = datetime.now(tz=UTC)
    cache = CheckCache(clock=_FakeClock())
    cache.store(_results())
    after = datetime.now(tz=UTC)
    checked_at = cache.current().checked_at
    assert checked_at is not None
    assert checked_at.tzinfo is not None
    assert checked_at.utcoffset() is not None
    assert before <= checked_at <= after


def test_default_ttl_is_thirty_seconds() -> None:
    """The default TTL is D-03's 30 seconds, not ``MetadataCache``'s 60."""
    clock = _FakeClock(start=100.0)
    cache = CheckCache(clock=clock)
    cache.store(_results())
    clock.advance(29.5)
    assert cache.is_fresh() is True
    clock.advance(1.0)
    assert cache.is_fresh() is False


def test_ttl_is_configurable() -> None:
    """A caller may shorten the TTL; the boundary follows the value given."""
    clock = _FakeClock(start=100.0)
    cache = CheckCache(ttl=5.0, clock=clock)
    cache.store(_results())
    clock.advance(4.9)
    assert cache.is_fresh() is True
    clock.advance(0.2)
    assert cache.is_fresh() is False


def test_a_later_store_replaces_the_previous_entry() -> None:
    """Storing again refreshes both the results and the TTL."""
    clock = _FakeClock(start=100.0)
    cache = CheckCache(clock=clock)
    cache.store(_results("first"))
    clock.advance(31.0)
    assert cache.is_fresh() is False
    second = _results("second")
    cache.store(second)
    assert cache.current().results == second
    assert cache.is_fresh() is True


def test_results_are_exposed_as_a_tuple() -> None:
    """The cached value is an immutable tuple of ``CheckResult``, not a list."""
    cache = CheckCache(clock=_FakeClock())
    cache.store(list(_results()))
    results = cache.current().results
    assert isinstance(results, tuple)
    assert all(isinstance(result, CheckResult) for result in results)


def test_concurrent_stores_leave_a_consistent_entry() -> None:
    """Two threads storing at once leave one whole entry, never a torn one."""
    clock = _FakeClock(start=100.0)
    cache = CheckCache(clock=clock)
    first = _results("first")
    second = _results("second")
    barrier = threading.Barrier(2, timeout=_BARRIER_TIMEOUT_SECONDS)

    def store(value: tuple[CheckResult, ...]) -> None:
        barrier.wait()
        cache.store(value)

    threads = [
        threading.Thread(target=store, args=(first,)),
        threading.Thread(target=store, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_BARRIER_TIMEOUT_SECONDS)
        assert not thread.is_alive()

    entry = cache.current()
    assert entry.results in (first, second)
    assert entry.checked_at is not None
    assert entry.stale is False


class TestClaimManualRefresh:
    """WR-05: a floor under the Refresh button's deliberate TTL bypass."""

    def test_the_first_claim_on_a_cold_appliance_is_granted(self) -> None:
        """The first click after a boot must work, or the floor is a wall."""
        cache = CheckCache(clock=_FakeClock())
        assert cache.claim_manual_refresh() is True

    def test_a_second_claim_at_the_same_instant_is_refused(self) -> None:
        """Two clicks the clock cannot tell apart are one honoured probe."""
        cache = CheckCache(clock=_FakeClock())
        assert cache.claim_manual_refresh() is True
        assert cache.claim_manual_refresh() is False

    def test_a_claim_after_the_interval_is_granted_again(self) -> None:
        """The floor is a rate, not a one-shot: waiting it out re-opens it."""
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        assert cache.claim_manual_refresh() is True
        clock.advance(3.0)
        assert cache.claim_manual_refresh() is True

    def test_the_default_interval_is_two_seconds(self) -> None:
        """2.0 s is below a human's click rate and above a loop's (WR-05)."""
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        assert cache.claim_manual_refresh() is True
        clock.advance(1.9)
        assert cache.claim_manual_refresh() is False
        clock.advance(0.2)
        assert cache.claim_manual_refresh() is True

    def test_the_interval_is_a_call_time_parameter(self) -> None:
        """A caller may shorten it; the boundary follows the value given."""
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        assert cache.claim_manual_refresh(min_interval=0.5) is True
        clock.advance(0.4)
        assert cache.claim_manual_refresh(min_interval=0.5) is False
        clock.advance(0.2)
        assert cache.claim_manual_refresh(min_interval=0.5) is True

    def test_a_refused_claim_does_not_move_the_stamp_forward(self) -> None:
        """
        A loop cannot starve a legitimate click by resetting the interval.

        If a refusal stamped, a caller hammering the endpoint every half
        second would hold the floor shut for ever and the household member at
        the appliance would never get their probe.  The stamp moves on a grant
        and only on a grant.
        """
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        assert cache.claim_manual_refresh() is True
        for _ in range(3):
            clock.advance(0.5)
            assert cache.claim_manual_refresh() is False
        clock.advance(0.6)
        assert cache.claim_manual_refresh() is True

    def test_a_store_does_not_grant_a_claim(self) -> None:
        """The floor is not the TTL: filling the cache does not re-open it."""
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        assert cache.claim_manual_refresh() is True
        cache.store(_results())
        assert cache.claim_manual_refresh() is False

    def test_a_granted_claim_stores_nothing(self) -> None:
        """A claim is permission to probe, never a record of results."""
        cache = CheckCache(clock=_FakeClock())
        assert cache.claim_manual_refresh() is True
        entry = cache.current()
        assert entry.results is None
        assert entry.checked_at is None
        assert cache.is_fresh() is False

    def test_an_expired_ttl_does_not_grant_a_claim(self) -> None:
        """
        The two intervals are independent, and deliberately so.

        Conflating them would make a refused click reset the freshness of
        results it never produced, and would tie a 2 s floor to a 30 s TTL for
        no reason beyond them both being durations.
        """
        clock = _FakeClock(start=100.0)
        cache = CheckCache(ttl=1.0, clock=clock)
        cache.store(_results())
        assert cache.claim_manual_refresh() is True
        clock.advance(1.5)
        assert cache.is_fresh() is False
        assert cache.claim_manual_refresh() is False

    def test_a_reentrant_second_caller_is_refused(self) -> None:
        """
        Two overlapping claims grant one probe, pinned without thread timing.

        The clock double claims once from inside the first claim's own clock
        read, so the overlap is a fact of the call stack rather than of
        scheduling luck -- the idiom plan 30-25 used for the probe lock.  It
        also pins that the clock is read *outside* the lock: a claim that read
        it inside would deadlock here rather than fail.
        """
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        inner: list[bool] = []
        entered = False

        def reentrant_clock() -> float:
            nonlocal entered
            if not entered:
                entered = True
                inner.append(cache.claim_manual_refresh())
            return clock.now

        cache._clock = reentrant_clock
        outer = cache.claim_manual_refresh()
        assert inner == [True]
        assert outer is False

    def test_concurrent_claims_grant_exactly_one(self) -> None:
        """Two request threads arriving together are one honoured probe."""
        cache = CheckCache(clock=_FakeClock(start=100.0))
        barrier = threading.Barrier(2, timeout=_BARRIER_TIMEOUT_SECONDS)
        granted: list[bool] = []
        granted_lock = threading.Lock()

        def claim() -> None:
            barrier.wait()
            outcome = cache.claim_manual_refresh()
            with granted_lock:
                granted.append(outcome)

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_BARRIER_TIMEOUT_SECONDS)
            assert not thread.is_alive()

        assert sorted(granted) == [False, True]


class TestReleaseManualClaim:
    """WR-03: a click whose probe collapsed must not spend the floor."""

    def test_a_released_claim_is_granted_again_at_once(self) -> None:
        """
        The floor exists to bound probe traffic, and a collapse made none.

        ``probe_now`` returning False means another checker already owned the
        probe, so this call issued no Paperless request, no saned dial and no
        filesystem write.  Charging it the interval would refuse the very next
        click for no traffic saved -- which is the second consequence WR-03
        named, the button appearing to do nothing twice in a row.
        """
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        assert cache.claim_manual_refresh() is True
        cache.release_manual_claim()
        assert cache.claim_manual_refresh() is True

    def test_releasing_a_claim_that_was_never_granted_is_a_no_op(self) -> None:
        """A release on a cold appliance must not raise or open anything new."""
        cache = CheckCache(clock=_FakeClock())
        cache.release_manual_claim()
        assert cache.claim_manual_refresh() is True

    def test_releasing_twice_leaves_the_floor_where_one_release_left_it(self) -> None:
        """The clear is idempotent: a doubled release is not a doubled grant."""
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        assert cache.claim_manual_refresh() is True
        cache.release_manual_claim()
        cache.release_manual_claim()
        assert cache.claim_manual_refresh() is True
        assert cache.claim_manual_refresh() is False

    def test_a_release_then_a_grant_leaves_the_new_grants_floor_standing(self) -> None:
        """
        T-30-29-01: the release gives a claim back, it does not disable the floor.

        Grant, release, grant: the second grant stamps like any other, so a
        third call inside the interval is still refused and a scripted loop
        that does get a probe granted still pays for it.
        """
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        assert cache.claim_manual_refresh() is True
        cache.release_manual_claim()
        assert cache.claim_manual_refresh() is True
        assert cache.claim_manual_refresh() is False
        clock.advance(2.1)
        assert cache.claim_manual_refresh() is True

    def test_a_release_stores_nothing(self) -> None:
        """Giving the floor back is not a record of results, as a claim is not."""
        cache = CheckCache(clock=_FakeClock())
        assert cache.claim_manual_refresh() is True
        cache.release_manual_claim()
        entry = cache.current()
        assert entry.results is None
        assert entry.checked_at is None
        assert cache.is_fresh() is False
