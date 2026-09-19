"""
MetadataCache unit tests.

The cache takes its clock as a constructor parameter, so every assertion about
the TTL here moves a float instead of waiting for one to pass, and the
single-flight tests hold a fetch open on an Event rather than a pause.  Nothing
in this file sleeps.

Covers requirements: PLSS-05, ROBU-05.
"""

from __future__ import annotations

import logging
import threading

import pytest

from saneless.exceptions import PaperlessError
from saneless.web.cache import MetadataCache

_WAIT_SECONDS = 5.0
_CACHE_LOGGER = "saneless.web.cache"

type _Rows = list[dict[str, object]]


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


class _MissCountingCache(MetadataCache):
    """A cache that sets an Event once a given number of lookups have missed."""

    def __init__(self, misses_wanted: int) -> None:
        """Count misses until ``misses_wanted`` of them have happened."""
        super().__init__(ttl=60, clock=_FakeClock())
        self._misses_wanted = misses_wanted
        self._misses = 0
        self._count_lock = threading.Lock()
        self.enough_missed = threading.Event()

    def get(self, key: str) -> _Rows | None:
        """Look ``key`` up, counting the lookup when it misses."""
        found = super().get(key)
        if found is None:
            with self._count_lock:
                self._misses += 1
                if self._misses >= self._misses_wanted:
                    self.enough_missed.set()
        return found


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the WARNING records the cache logged."""
    return [
        r
        for r in caplog.records
        if r.name == _CACHE_LOGGER and r.levelno == logging.WARNING
    ]


def _raise(exc: Exception) -> _Rows:
    """Stand in for a fetch that fails with ``exc``."""
    raise exc


def test_cache_set_and_get() -> None:
    """Cache stores and retrieves values by key (PLSS-05)."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    cache.set("tags", [{"id": 1, "name": "receipt"}])
    result = cache.get("tags")
    assert result == [{"id": 1, "name": "receipt"}]


def test_cache_ttl_expiry() -> None:
    """Cached entries expire once the TTL has elapsed on the cache's clock."""
    clock = _FakeClock()
    cache = MetadataCache(ttl=1, clock=clock)
    cache.set("tags", [{"id": 1, "name": "receipt"}])
    assert cache.get("tags") is not None
    clock.advance(0.9)
    assert cache.get("tags") is not None
    clock.advance(0.2)
    assert cache.get("tags") is None


def test_cache_invalidate() -> None:
    """Explicit invalidation removes cached entry (PLSS-05)."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    cache.set("tags", [{"id": 1, "name": "receipt"}])
    cache.invalidate("tags")
    assert cache.get("tags") is None


def test_cache_invalidate_nonexistent() -> None:
    """Invalidating a missing key does not raise or touch other entries."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    cache.set("correspondents", [{"id": 1, "name": "ACME Corp"}])
    cache.invalidate("tags")
    assert cache.get("tags") is None
    assert cache.get("correspondents") == [{"id": 1, "name": "ACME Corp"}]


def test_cache_per_resource() -> None:
    """Different resources have independent cache entries (PLSS-05)."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    cache.set("tags", [{"id": 1, "name": "receipt"}])
    cache.set("correspondents", [{"id": 1, "name": "ACME Corp"}])
    cache.invalidate("tags")
    assert cache.get("tags") is None
    assert cache.get("correspondents") == [{"id": 1, "name": "ACME Corp"}]


def test_get_or_fetch_single_flight() -> None:
    """
    Concurrent misses for one key trigger a single fetch (ROBU-05).

    The fetch is held open until every thread has missed the fresh-value check
    at least once, so each of them had the chance to start a fetch of its own.
    """
    threads_count = 8
    cache = _MissCountingCache(misses_wanted=threads_count)
    barrier = threading.Barrier(threads_count)
    fetch_started = threading.Event()
    gate = threading.Event()
    calls: list[int] = []
    calls_lock = threading.Lock()
    results: list[_Rows] = []
    results_lock = threading.Lock()

    def fetch() -> _Rows:
        with calls_lock:
            calls.append(1)
        fetch_started.set()
        gate.wait(_WAIT_SECONDS)
        return [{"id": 1, "name": "receipt"}]

    def request() -> None:
        barrier.wait()
        data = cache.get_or_fetch("tags", fetch)
        with results_lock:
            results.append(data)

    threads = [threading.Thread(target=request) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    try:
        assert fetch_started.wait(_WAIT_SECONDS)
        assert cache.enough_missed.wait(_WAIT_SECONDS)
    finally:
        gate.set()
        for thread in threads:
            thread.join(_WAIT_SECONDS)

    assert len(calls) == 1
    assert len(results) == threads_count
    assert all(result is results[0] for result in results)


def test_get_or_fetch_failure_is_not_cached() -> None:
    """With no previous value, a raising fetch propagates and caches nothing."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    calls: list[int] = []

    def failing_fetch() -> _Rows:
        calls.append(1)
        msg = "paperless unreachable"
        raise ConnectionError(msg)

    with pytest.raises(ConnectionError):
        cache.get_or_fetch("tags", failing_fetch)
    assert cache.get("tags") is None

    with pytest.raises(ConnectionError):
        cache.get_or_fetch("tags", failing_fetch)
    assert len(calls) == 2


def test_invalidate_during_an_in_flight_fetch_is_not_undone() -> None:
    """
    A fetch that started before an invalidate does not cache its stale data.

    The refresh route invalidates and then fetches.  If an older fetch was in
    flight, its pre-change list must not land in the cache after the
    invalidate, or the refresh's re-check would return it.
    """
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    stale: _Rows = [{"id": 1, "name": "old"}]
    fresh: _Rows = [{"id": 2, "name": "new"}]
    entered = threading.Event()
    release = threading.Event()
    results: dict[str, _Rows] = {}

    def slow_stale_fetch() -> _Rows:
        entered.set()
        release.wait(_WAIT_SECONDS)
        return stale

    def in_flight() -> None:
        results["in_flight"] = cache.get_or_fetch("tags", slow_stale_fetch)

    def refresh() -> None:
        results["refresh"] = cache.get_or_fetch("tags", lambda: fresh)

    first = threading.Thread(target=in_flight)
    first.start()
    assert entered.wait(_WAIT_SECONDS)
    cache.invalidate("tags")
    second = threading.Thread(target=refresh)
    second.start()
    release.set()
    first.join(_WAIT_SECONDS)
    second.join(_WAIT_SECONDS)

    assert results["in_flight"] is stale
    assert results["refresh"] is fresh
    assert cache.get("tags") is fresh


def test_get_or_fetch_returns_fresh_value_without_fetching() -> None:
    """A fresh value is served from cache; an expired one is refetched (ROBU-05)."""
    clock = _FakeClock()
    cache = MetadataCache(ttl=1, clock=clock)
    cached: _Rows = [{"id": 1, "name": "receipt"}]
    cache.set("tags", cached)
    calls: list[int] = []

    def fetch() -> _Rows:
        calls.append(1)
        return [{"id": 2, "name": "invoice"}]

    assert cache.get_or_fetch("tags", fetch) is cached
    assert calls == []

    clock.advance(1.1)
    assert cache.get_or_fetch("tags", fetch) == [{"id": 2, "name": "invoice"}]
    assert calls == [1]


def test_failed_refresh_serves_the_last_good_value_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A refresh that fails with a Paperless error serves the previous value.

    The expected failure is logged once, at WARNING, naming the resource and
    the cause, and without a traceback: the message already says what went
    wrong.
    """
    clock = _FakeClock()
    cache = MetadataCache(ttl=60, clock=clock)
    good: _Rows = [{"id": 1, "name": "receipt"}]
    cache.set("tags", good)
    clock.advance(61)

    with caplog.at_level(logging.WARNING, logger=_CACHE_LOGGER):
        served = cache.get_or_fetch("tags", lambda: _raise(PaperlessError("boom")))

    assert served is good
    records = _warnings(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "tags" in message
    assert "boom" in message
    assert records[0].exc_info is None


def test_failed_refresh_re_arms_the_entry_for_one_more_ttl(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    After serving the previous value, the cache does not retry until a TTL passes.

    An outage then costs one failed fetch and one log line per TTL, not one
    per page load.
    """
    clock = _FakeClock()
    cache = MetadataCache(ttl=60, clock=clock)
    good: _Rows = [{"id": 1, "name": "receipt"}]
    cache.set("tags", good)
    clock.advance(61)
    calls: list[int] = []

    def failing_fetch() -> _Rows:
        calls.append(1)
        msg = "Could not fetch tags from Paperless at http://paperless:8000: refused"
        raise PaperlessError(msg)

    with caplog.at_level(logging.WARNING, logger=_CACHE_LOGGER):
        assert cache.get_or_fetch("tags", failing_fetch) is good
        clock.advance(30)
        assert cache.get_or_fetch("tags", failing_fetch) is good
        assert cache.get("tags") is good
        assert calls == [1]
        assert len(_warnings(caplog)) == 1

        clock.advance(31)
        assert cache.get_or_fetch("tags", failing_fetch) is good
        assert calls == [1, 1]
        assert len(_warnings(caplog)) == 2

    for record in _warnings(caplog):
        assert "Token" not in record.getMessage()
        assert record.getMessage().startswith(
            "Refreshing tags from paperless-ngx failed; "
            "serving the last good copy for another 60s: "
        )


def test_recovered_refresh_replaces_the_last_good_value() -> None:
    """Once Paperless answers again, the new list is stored and served."""
    clock = _FakeClock()
    cache = MetadataCache(ttl=60, clock=clock)
    cache.set("tags", [{"id": 1, "name": "receipt"}])
    clock.advance(61)
    cache.get_or_fetch("tags", lambda: _raise(PaperlessError("down")))
    clock.advance(61)
    recovered: _Rows = [{"id": 2, "name": "invoice"}]

    assert cache.get_or_fetch("tags", lambda: recovered) is recovered
    assert cache.get("tags") is recovered


def test_unexpected_refresh_failure_serves_the_last_good_value_with_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure that is not a Paperless error is served stale and logged with exc_info."""
    clock = _FakeClock()
    cache = MetadataCache(ttl=60, clock=clock)
    good: _Rows = [{"id": 1, "name": "receipt"}]
    cache.set("tags", good)
    clock.advance(61)

    with caplog.at_level(logging.WARNING, logger=_CACHE_LOGGER):
        served = cache.get_or_fetch("tags", lambda: _raise(RuntimeError("surprise")))

    assert served is good
    records = _warnings(caplog)
    assert len(records) == 1
    assert "surprise" in records[0].getMessage()
    assert records[0].exc_info is not None


def test_failure_without_a_last_good_value_propagates_and_stores_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With nothing to fall back on the error reaches the caller, unlogged here."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())

    with (
        caplog.at_level(logging.WARNING, logger=_CACHE_LOGGER),
        pytest.raises(PaperlessError, match="boom"),
    ):
        cache.get_or_fetch("tags", lambda: _raise(PaperlessError("boom")))

    assert cache.get("tags") is None
    assert _warnings(caplog) == []


def test_invalidate_racing_a_failed_refresh_is_not_overwritten(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    An invalidate during a failing fetch is not undone by the re-arm.

    The in-flight caller still gets the previous value, but the entry is not
    re-armed, so the next caller fetches; its result is stored normally.  The
    warning says so, rather than promising the copy for another TTL.
    """
    clock = _FakeClock()
    cache = MetadataCache(ttl=60, clock=clock)
    good: _Rows = [{"id": 1, "name": "receipt"}]
    cache.set("tags", good)
    clock.advance(61)
    entered = threading.Event()
    release = threading.Event()
    results: dict[str, _Rows] = {}

    def slow_failing_fetch() -> _Rows:
        entered.set()
        release.wait(_WAIT_SECONDS)
        msg = "paperless unreachable"
        raise PaperlessError(msg)

    def in_flight() -> None:
        results["in_flight"] = cache.get_or_fetch("tags", slow_failing_fetch)

    thread = threading.Thread(target=in_flight)
    with caplog.at_level(logging.WARNING, logger=_CACHE_LOGGER):
        thread.start()
        try:
            assert entered.wait(_WAIT_SECONDS)
            cache.invalidate("tags")
        finally:
            release.set()
            thread.join(_WAIT_SECONDS)

    assert results["in_flight"] is good
    assert cache.get("tags") is None
    [record] = _warnings(caplog)
    assert record.getMessage() == (
        "Refreshing tags from paperless-ngx failed; serving the last good copy, "
        "and the next request fetches again: paperless unreachable"
    )

    fresh: _Rows = [{"id": 2, "name": "invoice"}]
    assert cache.get_or_fetch("tags", lambda: fresh) is fresh
    assert cache.get("tags") is fresh


def test_invalidate_keeps_the_fallback() -> None:
    """Invalidate means refetch, not forget: a failing refetch serves the old list."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    good: _Rows = [{"id": 1, "name": "receipt"}]
    cache.set("tags", good)
    cache.invalidate("tags")

    served = cache.get_or_fetch("tags", lambda: _raise(PaperlessError("down")))

    assert served is good
    assert cache.get("tags") is good
