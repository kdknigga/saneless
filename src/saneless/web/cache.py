"""TTL-based metadata cache for paperless-ngx tags and correspondents."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Final

from saneless.exceptions import PaperlessError, describe

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["MetadataCache"]

logger = logging.getLogger(__name__)

_STALE_RE_ARMED: Final = (
    "Refreshing %s from paperless-ngx failed; "
    "serving the last good copy for another %ss: %s"
)

# The same failure when an invalidate ran during the fetch: the last good copy
# is still served, but it was not re-armed, so the next request fetches again.
_STALE_NOT_RE_ARMED: Final = (
    "Refreshing %s from paperless-ngx failed; "
    "serving the last good copy, and the next request fetches again: %s"
)


class MetadataCache:
    """
    In-memory cache with per-key TTL expiration and a last good copy per key.

    Stores lists of metadata dicts (tags, correspondents) fetched from
    paperless-ngx, expiring entries after a configurable number of seconds.
    The last value stored for each key is also kept apart from the expiring
    entry, so a refresh that fails can serve it instead of nothing.

    Args:
        ttl: Time-to-live in seconds for cached entries.
        clock: The monotonic source the TTL is measured with.

    """

    def __init__(
        self, ttl: int = 60, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Initialize the cache with the given TTL and clock."""
        self._ttl = ttl
        self._clock = clock
        self._store: dict[str, tuple[float, list[dict[str, object]]]] = {}
        # The last value stored for each key.  Unlike the entry in _store it
        # never expires and invalidate() leaves it alone: invalidating means
        # "fetch again", not "forget what Paperless last said".
        self._last_good: dict[str, list[dict[str, object]]] = {}
        # Guards creation of the per-key locks, the generation counters, and
        # the store-if-unchanged checks below; never a fetch.
        self._locks_guard = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        # Bumped by invalidate(), so a fetch that started before an invalidate
        # cannot store anything -- its own data or a re-armed fallback -- after it.
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
        if self._clock() - ts < self._ttl:
            return data
        return None

    def set(self, key: str, data: list[dict[str, object]]) -> None:
        """
        Store a value in the cache with the current timestamp.

        The value also becomes the key's last good copy.

        Args:
            key: Cache key.
            data: List of metadata dicts to cache.

        """
        self._store[key] = (self._clock(), data)
        self._last_good[key] = data

    def get_or_fetch(
        self, key: str, fetch: Callable[[], list[dict[str, object]]]
    ) -> list[dict[str, object]]:
        """
        Return a fresh cached value, fetching it once when it is missing.

        Single-flight: concurrent misses for one key share a per-key lock, and
        every thread that waited re-checks the cache before fetching, so the
        threadpool's concurrent requests do not stampede Paperless.

        A fetch caches its result only if no :meth:`invalidate` for the key ran
        while it was in flight.  Otherwise the refresh that invalidated would
        find the older fetch's pre-change data on its re-check, return it, and
        keep it cached for another TTL.

        When ``fetch`` raises and the key has a last good copy, that copy is
        returned and kept for one more TTL, and one warning names the key and
        the cause: an expected Paperless error without a traceback, anything
        else with one.  The threads queued behind the failed fetch find the
        re-armed entry, so an outage costs one failed fetch and one log line
        per TTL rather than one per page load.  The re-arm obeys the same
        invalidate check as a successful fetch; when an invalidate stops it,
        the copy is still returned but the warning says the next request
        fetches again rather than promising another TTL.  When there is no
        last good copy, the exception propagates, nothing is cached, and the
        next call fetches again.

        Known limitation: while Paperless is unreachable and there is no last
        good copy, the waiting threads retry the fetch one after another, each
        paying the connect timeout.

        Args:
            key: Cache key.
            fetch: Callable producing the value on a miss.

        Returns:
            The cached, freshly fetched, or last good list of dicts.

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
            try:
                data = fetch()
            except Exception as exc:
                stale, re_armed = self._re_arm(key, generation)
                if stale is None:
                    raise
                # An expected Paperless error is described by its message
                # alone; anything else is a surprise and keeps its traceback.
                traceback = None if isinstance(exc, PaperlessError) else exc
                if re_armed:
                    logger.warning(
                        _STALE_RE_ARMED,
                        key,
                        self._ttl,
                        describe(exc),
                        exc_info=traceback,
                    )
                else:
                    logger.warning(
                        _STALE_NOT_RE_ARMED, key, describe(exc), exc_info=traceback
                    )
                return stale
            with self._locks_guard:
                if self._generations.get(key, 0) == generation:
                    self.set(key, data)
            return data

    def _re_arm(
        self, key: str, generation: int
    ) -> tuple[list[dict[str, object]] | None, bool]:
        """
        Keep the key's last good copy for one more TTL after a failed fetch.

        Nothing is stored when an :meth:`invalidate` ran since ``generation``
        was read, for the same reason a successful fetch would not store.

        Args:
            key: Cache key whose fetch failed.
            generation: The key's generation when the fetch started.

        Returns:
            The last good copy, or None when the key has never had one, and
            whether it was stored for another TTL.

        """
        with self._locks_guard:
            stale = self._last_good.get(key)
            re_armed = stale is not None and self._generations.get(key, 0) == generation
            if stale is not None and re_armed:
                self._store[key] = (self._clock(), stale)
        return stale, re_armed

    def invalidate(self, key: str) -> None:
        """
        Remove a specific key from the cache.

        A fetch already in flight for the key will not cache its result.  The
        key's last good copy is kept, so a refetch that fails can still serve it.

        Args:
            key: Cache key to invalidate.

        """
        with self._locks_guard:
            self._generations[key] = self._generations.get(key, 0) + 1
            self._store.pop(key, None)
