"""
Xfail test stubs for MetadataCache tests (Phase 3).

Covers requirement: PLSS-05.
"""

import pytest


@pytest.mark.xfail(reason="stub -- implemented in 03-02")
def test_cache_set_and_get() -> None:
    """Cache stores and retrieves values by key (PLSS-05)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-02")
def test_cache_ttl_expiry() -> None:
    """Cached entries expire after TTL elapses (PLSS-05)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-02")
def test_cache_invalidate() -> None:
    """Explicit invalidation removes cached entry (PLSS-05)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-02")
def test_cache_invalidate_nonexistent() -> None:
    """Invalidating a missing key does not raise (PLSS-05)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-02")
def test_cache_per_resource() -> None:
    """Different resources have independent cache entries (PLSS-05)."""
    pytest.fail("Not implemented")
