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
