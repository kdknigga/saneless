"""
MetadataCache unit tests.

Covers requirements: PLSS-05, ROBU-05.
"""

import threading
import time

import pytest

from saneless.web.cache import MetadataCache


def test_cache_set_and_get() -> None:
    """Cache stores and retrieves values by key (PLSS-05)."""
    cache = MetadataCache(ttl=60)
    cache.set("tags", [{"id": 1, "name": "receipt"}])
    result = cache.get("tags")
    assert result == [{"id": 1, "name": "receipt"}]


def test_cache_ttl_expiry() -> None:
    """Cached entries expire after TTL elapses (PLSS-05)."""
    cache = MetadataCache(ttl=1)
    cache.set("tags", [{"id": 1, "name": "receipt"}])
    assert cache.get("tags") is not None
    time.sleep(1.1)
    assert cache.get("tags") is None


def test_cache_invalidate() -> None:
    """Explicit invalidation removes cached entry (PLSS-05)."""
    cache = MetadataCache(ttl=60)
    cache.set("tags", [{"id": 1, "name": "receipt"}])
    cache.invalidate("tags")
    assert cache.get("tags") is None


def test_cache_invalidate_nonexistent() -> None:
    """Invalidating a missing key does not raise (PLSS-05)."""
    cache = MetadataCache(ttl=60)
    cache.invalidate("tags")


def test_cache_per_resource() -> None:
    """Different resources have independent cache entries (PLSS-05)."""
    cache = MetadataCache(ttl=60)
    cache.set("tags", [{"id": 1, "name": "receipt"}])
    cache.set("correspondents", [{"id": 1, "name": "ACME Corp"}])
    cache.invalidate("tags")
    assert cache.get("tags") is None
    assert cache.get("correspondents") == [{"id": 1, "name": "ACME Corp"}]


def test_get_or_fetch_single_flight() -> None:
    """Concurrent misses for one key trigger a single fetch (ROBU-05)."""
    cache = MetadataCache(ttl=60)
    threads_count = 8
    barrier = threading.Barrier(threads_count)
    calls: list[int] = []
    calls_lock = threading.Lock()
    results: list[list[dict[str, object]]] = []
    results_lock = threading.Lock()

    def fetch() -> list[dict[str, object]]:
        time.sleep(0.1)
        with calls_lock:
            calls.append(1)
        return [{"id": 1, "name": "receipt"}]

    def request() -> None:
        barrier.wait()
        data = cache.get_or_fetch("tags", fetch)
        with results_lock:
            results.append(data)

    threads = [threading.Thread(target=request) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert len(calls) == 1
    assert len(results) == threads_count
    assert all(result is results[0] for result in results)


def test_get_or_fetch_failure_is_not_cached() -> None:
    """A raising fetch propagates, caches nothing, and is retried (ROBU-05)."""
    cache = MetadataCache(ttl=60)
    calls: list[int] = []

    def failing_fetch() -> list[dict[str, object]]:
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

    WR-05: the refresh route invalidates and then fetches.  If an older fetch
    was in flight, its pre-change list must not land in the cache after the
    invalidate, or the refresh's re-check would return it.
    """
    cache = MetadataCache(ttl=60)
    stale: list[dict[str, object]] = [{"id": 1, "name": "old"}]
    fresh: list[dict[str, object]] = [{"id": 2, "name": "new"}]
    entered = threading.Event()
    release = threading.Event()
    results: dict[str, list[dict[str, object]]] = {}

    def slow_stale_fetch() -> list[dict[str, object]]:
        entered.set()
        release.wait(5)
        return stale

    def in_flight() -> None:
        results["in_flight"] = cache.get_or_fetch("tags", slow_stale_fetch)

    def refresh() -> None:
        results["refresh"] = cache.get_or_fetch("tags", lambda: fresh)

    first = threading.Thread(target=in_flight)
    first.start()
    assert entered.wait(5)
    cache.invalidate("tags")
    second = threading.Thread(target=refresh)
    second.start()
    release.set()
    first.join(5)
    second.join(5)

    assert results["in_flight"] is stale
    assert results["refresh"] is fresh
    assert cache.get("tags") is fresh


def test_get_or_fetch_returns_fresh_value_without_fetching() -> None:
    """A fresh value is served from cache; an expired one is refetched (ROBU-05)."""
    cache = MetadataCache(ttl=1)
    cached: list[dict[str, object]] = [{"id": 1, "name": "receipt"}]
    cache.set("tags", cached)
    calls: list[int] = []

    def fetch() -> list[dict[str, object]]:
        calls.append(1)
        return [{"id": 2, "name": "invoice"}]

    assert cache.get_or_fetch("tags", fetch) is cached
    assert calls == []

    time.sleep(1.1)
    assert cache.get_or_fetch("tags", fetch) == [{"id": 2, "name": "invoice"}]
    assert calls == [1]
