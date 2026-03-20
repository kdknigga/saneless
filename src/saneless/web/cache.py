"""TTL-based metadata cache for paperless-ngx tags and correspondents."""

from __future__ import annotations

import time

__all__ = ["MetadataCache"]


class MetadataCache:
    """
    In-memory cache with per-key TTL expiration.

    Stores lists of metadata dicts (tags, correspondents) fetched from
    paperless-ngx, expiring entries after a configurable number of seconds.

    Args:
        ttl: Time-to-live in seconds for cached entries.

    """

    def __init__(self, ttl: int = 60) -> None:
        """Initialize the cache with the given TTL."""
        self._ttl = ttl
        self._store: dict[str, tuple[float, list[dict[str, object]]]] = {}

    def get(self, key: str) -> list[dict[str, object]] | None:
        """
        Retrieve a cached value if it exists and has not expired.

        Args:
            key: Cache key to look up.

        Returns:
            The cached list of dicts, or None if missing or expired.

        """
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, data = entry
        if time.monotonic() - ts < self._ttl:
            return data
        return None

    def set(self, key: str, data: list[dict[str, object]]) -> None:
        """
        Store a value in the cache with the current timestamp.

        Args:
            key: Cache key.
            data: List of metadata dicts to cache.

        """
        self._store[key] = (time.monotonic(), data)

    def invalidate(self, key: str) -> None:
        """
        Remove a specific key from the cache.

        Args:
            key: Cache key to invalidate.

        """
        self._store.pop(key, None)
