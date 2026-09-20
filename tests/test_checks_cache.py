"""
CheckCache unit tests.

``CheckCache`` takes its clock as a constructor parameter, as
``MetadataCache`` does, so every assertion about the TTL here advances a float
instead of waiting for real seconds to pass.

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
        assert cache.claim_manual_refresh() is not None

    def test_a_granted_claim_reports_the_stamp_it_wrote(self) -> None:
        """
        The grant is identified, so its holder can give back that grant and no other.

        The stamp is the clock reading the claim recorded, which is what
        :meth:`release_manual_claim` compares against (R3-IN-03).
        """
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        assert cache.claim_manual_refresh() == 100.0

    def test_a_stamp_of_zero_is_a_grant_and_not_a_refusal(self) -> None:
        """
        ``None`` is the refusal, not falseness: a stamp is a timestamp.

        ``time.monotonic()`` counts from boot, so 0.0 is a reading a claim can
        really write, and it is falsey.  A caller branching on truthiness would
        read the first grant after a boot as a refusal and never probe, which
        is why the contract is ``is not None`` and why the release accepts the
        zero stamp like any other.
        """
        cache = CheckCache(clock=_FakeClock(start=0.0))
        stamp = cache.claim_manual_refresh()
        assert stamp == 0.0
        assert stamp is not None
        assert cache.release_manual_claim(stamp) is True

    def test_a_second_claim_at_the_same_instant_is_refused(self) -> None:
        """Two clicks the clock cannot tell apart are one honoured probe."""
        cache = CheckCache(clock=_FakeClock())
        assert cache.claim_manual_refresh() is not None
        assert cache.claim_manual_refresh() is None

    def test_a_claim_after_the_interval_is_granted_again(self) -> None:
        """The floor is a rate, not a one-shot: waiting it out re-opens it."""
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        assert cache.claim_manual_refresh() is not None
        clock.advance(3.0)
        assert cache.claim_manual_refresh() is not None

    def test_the_default_interval_is_two_seconds(self) -> None:
        """2.0 s is below a human's click rate and above a loop's (WR-05)."""
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        assert cache.claim_manual_refresh() is not None
        clock.advance(1.9)
        assert cache.claim_manual_refresh() is None
        clock.advance(0.2)
        assert cache.claim_manual_refresh() is not None

    def test_the_interval_is_a_call_time_parameter(self) -> None:
        """A caller may shorten it; the boundary follows the value given."""
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        assert cache.claim_manual_refresh(min_interval=0.5) is not None
        clock.advance(0.4)
        assert cache.claim_manual_refresh(min_interval=0.5) is None
        clock.advance(0.2)
        assert cache.claim_manual_refresh(min_interval=0.5) is not None

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
        assert cache.claim_manual_refresh() is not None
        for _ in range(3):
            clock.advance(0.5)
            assert cache.claim_manual_refresh() is None
        clock.advance(0.6)
        assert cache.claim_manual_refresh() is not None

    def test_a_store_does_not_grant_a_claim(self) -> None:
        """The floor is not the TTL: filling the cache does not re-open it."""
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        assert cache.claim_manual_refresh() is not None
        cache.store(_results())
        assert cache.claim_manual_refresh() is None

    def test_a_granted_claim_stores_nothing(self) -> None:
        """A claim is permission to probe, never a record of results."""
        cache = CheckCache(clock=_FakeClock())
        assert cache.claim_manual_refresh() is not None
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
        assert cache.claim_manual_refresh() is not None
        clock.advance(1.5)
        assert cache.is_fresh() is False
        assert cache.claim_manual_refresh() is None

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
        inner: list[float | None] = []
        entered = False

        def reentrant_clock() -> float:
            nonlocal entered
            if not entered:
                entered = True
                inner.append(cache.claim_manual_refresh())
            return clock.now

        cache._clock = reentrant_clock
        outer = cache.claim_manual_refresh()
        assert inner == [clock.now]
        assert outer is None

    def test_concurrent_claims_grant_exactly_one(self) -> None:
        """Two request threads arriving together are one honoured probe."""
        cache = CheckCache(clock=_FakeClock(start=100.0))
        barrier = threading.Barrier(2, timeout=_BARRIER_TIMEOUT_SECONDS)
        granted: list[float | None] = []
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

        assert sorted(outcome is not None for outcome in granted) == [False, True]


class TestReleaseManualClaim:
    """
    WR-03: a click whose probe collapsed must not spend the floor.

    R3-IN-03: and it gives back *its own* claim.  The release is a
    compare-and-clear, so every case below that hands over a stamp which is no
    longer the recorded one asserts that nothing moved -- the property that
    used to be a paragraph of reasoning about one call site.
    """

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
        stamp = cache.claim_manual_refresh()
        assert stamp is not None
        assert cache.release_manual_claim(stamp) is True
        assert cache.claim_manual_refresh() is not None

    def test_releasing_a_claim_that_was_never_granted_is_a_no_op(self) -> None:
        """A release on a cold appliance must not raise or open anything new."""
        cache = CheckCache(clock=_FakeClock())
        assert cache.release_manual_claim(100.0) is False
        assert cache.claim_manual_refresh() is not None

    def test_releasing_twice_leaves_the_floor_where_one_release_left_it(self) -> None:
        """
        The clear is idempotent: a doubled release is not a doubled grant.

        The second release reports that it cleared nothing, because by then
        the stamp it carries is no longer recorded -- the same answer a stale
        stamp gets, reached by the shortest route.
        """
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        stamp = cache.claim_manual_refresh()
        assert stamp is not None
        assert cache.release_manual_claim(stamp) is True
        assert cache.release_manual_claim(stamp) is False
        assert cache.claim_manual_refresh() is not None
        assert cache.claim_manual_refresh() is None

    def test_a_release_then_a_grant_leaves_the_new_grants_floor_standing(self) -> None:
        """
        T-30-29-01: the release gives a claim back, it does not disable the floor.

        Grant, release, grant: the second grant stamps like any other, so a
        third call inside the interval is still refused and a scripted loop
        that does get a probe granted still pays for it.
        """
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        stamp = cache.claim_manual_refresh()
        assert stamp is not None
        assert cache.release_manual_claim(stamp) is True
        assert cache.claim_manual_refresh() is not None
        assert cache.claim_manual_refresh() is None
        clock.advance(2.1)
        assert cache.claim_manual_refresh() is not None

    def test_a_release_stores_nothing(self) -> None:
        """Giving the floor back is not a record of results, as a claim is not."""
        cache = CheckCache(clock=_FakeClock())
        stamp = cache.claim_manual_refresh()
        assert stamp is not None
        assert cache.release_manual_claim(stamp) is True
        entry = cache.current()
        assert entry.results is None
        assert entry.checked_at is None
        assert cache.is_fresh() is False

    def test_a_stale_stamp_does_not_clear_another_callers_claim(self) -> None:
        """
        R3-IN-03: a release arriving after somebody else's grant is a no-op.

        The old code cleared unconditionally and defended that with an
        argument about the one call site: the release follows its grant by
        microseconds on the same thread, and a competing claimer inside that
        window is refused without writing.  The argument was sound and it was
        still an argument -- a second caller, a retry, or a handler that grew
        a second release would have lowered the 2 s floor under an
        unauthenticated LAN endpoint.  Here the first caller releases late,
        after the second caller's grant, and the floor holds: the third claim
        inside the interval is still refused.

        Against an unconditional clear the release reports True and the third
        claim is granted, so this case is the mutation detector the plan names.
        """
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        first = cache.claim_manual_refresh()
        assert first is not None
        assert cache.release_manual_claim(first) is True
        clock.advance(0.1)
        second = cache.claim_manual_refresh()
        assert second is not None
        assert second != first
        assert cache.release_manual_claim(first) is False
        assert cache.claim_manual_refresh() is None

    def test_a_thread_refused_inside_the_interval_loses_nothing(self) -> None:
        """
        The window the old docstring described, exercised by two real threads.

        A holds a grant; B claims inside the interval from a second thread and
        is refused.  :meth:`claim_manual_refresh` promises a refusal writes
        nothing, and this is where that promise is load-bearing: A's stamp is
        still the recorded one, so A's release clears A's own grant and B --
        which never held one -- loses nothing by it.
        """
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        granted = cache.claim_manual_refresh()
        assert granted is not None
        refused: list[float | None] = []
        barrier = threading.Barrier(2, timeout=_BARRIER_TIMEOUT_SECONDS)

        def compete() -> None:
            barrier.wait()
            refused.append(cache.claim_manual_refresh())

        thread = threading.Thread(target=compete)
        thread.start()
        barrier.wait()
        thread.join(timeout=_BARRIER_TIMEOUT_SECONDS)
        assert not thread.is_alive()

        assert refused == [None]
        assert cache.release_manual_claim(granted) is True
        assert cache.claim_manual_refresh() is not None

    def test_a_late_release_from_one_thread_leaves_the_others_grant(self) -> None:
        """
        The stale-stamp case with the two callers really on two threads.

        The releasing thread claims, gives its grant back, and then -- after a
        second thread has been granted a claim of its own -- releases the same
        old stamp again, the way a retried or duplicated request would.  The
        second grant survives it and the floor is still shut, which is the
        whole of R3-IN-03 stated as an observation rather than as reasoning.
        """
        clock = _FakeClock(start=100.0)
        cache = CheckCache(clock=clock)
        stamps: list[float | None] = []
        releases: list[bool] = []
        released = threading.Event()
        regranted = threading.Event()

        def claim_release_and_release_again() -> None:
            stamp = cache.claim_manual_refresh()
            stamps.append(stamp)
            if stamp is None:
                released.set()
                return
            releases.append(cache.release_manual_claim(stamp))
            released.set()
            regranted.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
            releases.append(cache.release_manual_claim(stamp))

        thread = threading.Thread(target=claim_release_and_release_again)
        thread.start()
        assert released.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
        clock.advance(0.1)
        second = cache.claim_manual_refresh()
        regranted.set()
        thread.join(timeout=_BARRIER_TIMEOUT_SECONDS)
        assert not thread.is_alive()

        assert stamps == [100.0]
        assert releases == [True, False]
        assert second is not None
        assert second == clock.now
        assert cache.claim_manual_refresh() is None
        assert cache.release_manual_claim(second) is True
