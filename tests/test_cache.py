"""
MetadataCache unit tests.

Covers requirement: PLSS-05.
"""

import time

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
