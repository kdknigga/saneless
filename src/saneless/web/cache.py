"""TTL-based metadata cache for paperless-ngx tags and correspondents."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Final

from saneless.exceptions import ConfigError, PaperlessError, describe
from saneless.scan_metadata import fetch_metadata, metadata_ids

if TYPE_CHECKING:
    from collections.abc import Callable

    from saneless.scan_metadata import MetadataKind, MetadataSource

__all__ = ["CachedMetadataLookup", "MetadataCache"]

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

    def generation(self, key: str) -> int:
        """
        Read the key's generation, to hand back to :meth:`store_if_current`.

        Read it before a fetch starts: an :meth:`invalidate` during the fetch
        bumps it, and the store then declines.

        Args:
            key: Cache key.

        Returns:
            The key's current generation.

        """
        with self._locks_guard:
            return self._generations.get(key, 0)

    def store_if_current(
        self, key: str, data: list[dict[str, object]], generation: int
    ) -> bool:
        """
        Store a fetch's result only if no invalidate ran while it was in flight.

        The guard :meth:`get_or_fetch` applies to its own fetch, for a caller
        that fetched by itself.  Without it, a fetch that started before the
        operator's Refresh could finish after it and overwrite the refreshed
        list, and become the last good copy, with the older data.

        Args:
            key: Cache key.
            data: The fetched list.
            generation: What :meth:`generation` read before the fetch started.

        Returns:
            Whether the list was stored.

        """
        with self._locks_guard:
            if self._generations.get(key, 0) != generation:
                return False
            self.set(key, data)
            return True

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
        the cause, without a traceback: the message of a Paperless client
        error, or the class name of anything else.  The threads queued behind
        the failed fetch find the re-armed entry, so an outage costs one
        failed fetch and one log line per TTL rather than one per page load.
        The re-arm obeys the same invalidate check as a successful fetch;
        when an invalidate stops it, the copy is still returned but the
        warning says the next request fetches again rather than promising
        another TTL.  When there is no last good copy, the exception
        propagates, nothing is cached, and the next call fetches again.

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
            generation = self.generation(key)
            try:
                data = fetch()
            except Exception as exc:
                stale, re_armed = self._re_arm(key, generation)
                if stale is None:
                    raise
                # The web tier's client-exception rule, stated above
                # routes._get_cached_or_fetch.
                reason = (
                    describe(exc)
                    if isinstance(exc, PaperlessError | ConfigError)
                    else type(exc).__name__
                )
                if re_armed:
                    logger.warning(_STALE_RE_ARMED, key, self._ttl, reason)
                else:
                    logger.warning(_STALE_NOT_RE_ARMED, key, reason)
                return stale
            self.store_if_current(key, data, generation)
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


class CachedMetadataLookup:
    """
    The id check's lookup for the web: the page's cache first, then the client.

    A first look reads the list the tag and correspondent pickers were served
    from, so a scan whose ids are all known costs no request.  A fresh look,
    made only when an id seemed to be missing, goes to the client directly:
    the cache's own fetch serves the last good copy when paperless-ngx is
    down, and that copy would make an unreachable paperless-ngx look like
    proof the id is still missing.  Every list the client returns is stored,
    so the pickers see it too, unless the cache was invalidated while the
    fetch was in flight: a Refresh that ran meanwhile stored a newer list, and
    this older one must not replace it.  A failed fetch stores nothing and
    answers None.

    Args:
        cache: The web tier's metadata cache.
        client: The paperless-ngx client.

    """

    def __init__(self, cache: MetadataCache, client: MetadataSource) -> None:
        """Keep the cache and the client the lookups read."""
        self._cache = cache
        self._client = client

    def tag_ids(self, *, fresh: bool) -> frozenset[int] | None:
        """Return the ids of paperless-ngx's tags, or None when unknown."""
        return self._ids("tags", fresh=fresh)

    def correspondent_ids(self, *, fresh: bool) -> frozenset[int] | None:
        """Return the ids of paperless-ngx's correspondents, or None when unknown."""
        return self._ids("correspondents", fresh=fresh)

    def _ids(self, kind: MetadataKind, *, fresh: bool) -> frozenset[int] | None:
        """Return one list's ids from the cache, or from the client."""
        if not fresh:
            cached = self._cache.get(kind)
            if cached is not None:
                return metadata_ids(cached)
        generation = self._cache.generation(kind)
        rows = fetch_metadata(self._client, kind)
        if rows is None:
            return None
        self._cache.store_if_current(kind, rows, generation)
        return metadata_ids(rows)
