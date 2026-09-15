"""TTL-based metadata cache for paperless-ngx tags and correspondents."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

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
        # Guards creation of the per-key locks, the generation counters, and
        # the store-if-unchanged check below; never a fetch.
        self._locks_guard = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        # Bumped by invalidate(), so a fetch that started before an invalidate
        # cannot store its pre-change data after it (WR-05).
        self._generations: dict[str, int] = {}

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

    def get_or_fetch(
        self, key: str, fetch: Callable[[], list[dict[str, object]]]
    ) -> list[dict[str, object]]:
        """
        Return a fresh cached value, fetching it once when it is missing.

        Single-flight: concurrent misses for one key share a per-key lock, and
        every thread that waited re-checks the cache before fetching, so the
        threadpool's concurrent requests do not stampede Paperless (ROBU-05).
        A ``fetch`` that raises propagates to its caller and caches nothing,
        so the next call fetches again.

        A fetch caches its result only if no :meth:`invalidate` for the key ran
        while it was in flight.  Otherwise the refresh that invalidated would
        find the older fetch's pre-change data on its re-check, return it, and
        keep it cached for another TTL (WR-05).

        Known limitation: while Paperless is unreachable, the waiting threads
        retry the fetch one after another, each paying the connect timeout.
        A separate connect timeout and caching the unreachable outcome are
        deferred (M-01 second half, 26-CONTEXT Deferred Ideas); serving stale
        data on error is SWP-04.

        Args:
            key: Cache key.
            fetch: Callable producing the value on a miss.

        Returns:
            The cached or freshly fetched list of dicts.

        """
        cached = self.get(key)
        if cached is not None:
            return cached
        with self._locks_guard:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            # Another thread may have filled the entry while this one waited.
            cached = self.get(key)
            if cached is not None:
                return cached
            with self._locks_guard:
                generation = self._generations.get(key, 0)
            data = fetch()
            with self._locks_guard:
                if self._generations.get(key, 0) == generation:
                    self.set(key, data)
            return data

    def invalidate(self, key: str) -> None:
        """
        Remove a specific key from the cache.

        A fetch already in flight for the key will not cache its result.

        Args:
            key: Cache key to invalidate.

        """
        with self._locks_guard:
            self._generations[key] = self._generations.get(key, 0) + 1
            self._store.pop(key, None)
