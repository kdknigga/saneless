"""TTL-based metadata cache for paperless-ngx tags and correspondents."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Final, NamedTuple

from saneless.exceptions import ConfigError, PaperlessError, describe
from saneless.scan_metadata import fetch_metadata, metadata_ids

if TYPE_CHECKING:
    from collections.abc import Callable

    from saneless.scan_metadata import MetadataKind, MetadataSource

__all__ = [
    "NEGATIVE_TTL_SECONDS",
    "CachedList",
    "CachedMetadataLookup",
    "MetadataCache",
    "MetadataUnavailableError",
]

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


NEGATIVE_TTL_SECONDS: Final = 15.0
"""
How long a failed fetch with no last good copy is remembered, in seconds.

Short, so a page left open on a hallway tablet recovers soon after
paperless-ngx comes back; long enough that the requests arriving meanwhile
answer at once instead of each waiting out a connect timeout.  Never longer
than the cache's own TTL, and nothing at all when the cache is disabled.
"""


class MetadataUnavailableError(Exception):
    """
    A metadata list cannot be had right now, and no fetch was attempted.

    Raised when a fetch for the list failed a moment ago and there is no last
    good copy to serve, or when this caller could not take its turn to fetch
    within the time it was given because another fetch is still in flight.
    """


class CachedList(NamedTuple):
    """
    A list the cache served, and whether paperless-ngx vouches for it now.

    Attributes:
        rows: The list.
        current: True when it came from a fetch that succeeded within the TTL,
            or from :meth:`MetadataCache.set`; False when it is the last good
            copy, served because refreshing it failed.  Only a current list
            can show that an id is gone: the last good copy predates anything
            created or deleted since.

    """

    rows: list[dict[str, object]]
    current: bool


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
        # Each entry is when it was stored, the list, and whether the list is
        # current (see CachedList) rather than a re-armed last good copy.
        self._store: dict[str, tuple[float, list[dict[str, object]], bool]] = {}
        # The last value stored for each key.  Unlike the entry in _store it
        # never expires and invalidate() leaves it alone: invalidating means
        # "fetch again", not "forget what Paperless last said".
        self._last_good: dict[str, list[dict[str, object]]] = {}
        # When a failed fetch with no last good copy stops being remembered.
        # Kept apart from _store: CachedMetadataLookup._ids reads get(), and an
        # empty list there would read as "paperless-ngx has none".
        self._failed_until: dict[str, float] = {}
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
        ts, data, _current = entry
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
        self._store[key] = (self._clock(), data, True)
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
        Return :meth:`get_or_fetch_list`'s list alone.

        Args:
            key: Cache key.
            fetch: Callable producing the value on a miss.

        Returns:
            The cached, freshly fetched, or last good list of dicts.

        """
        return self.get_or_fetch_list(key, fetch).rows

    def get_or_fetch_list(
        self,
        key: str,
        fetch: Callable[[], list[dict[str, object]]],
        *,
        lock_timeout: float | None = None,
    ) -> CachedList:
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
        propagates and no list is cached, but the failure is remembered for
        ``min(ttl, NEGATIVE_TTL_SECONDS)``: until then every call, including
        the threads queued behind the failed fetch, raises
        :class:`MetadataUnavailableError` at once and logs nothing, so an
        unreachable Paperless is asked once per window rather than once per
        waiting thread.  :meth:`invalidate` forgets the failure, a zero TTL
        never records one, and an invalidate during the failing fetch stops
        it from being recorded.

        A caller that must answer within a budget passes ``lock_timeout``:
        when another thread's fetch holds the key for longer than that, the
        call raises :class:`MetadataUnavailableError` instead of waiting it
        out, which bounds a fetch stuck resolving the host name.

        The answer says which it is: a last good copy -- served now, or
        re-armed by an earlier failure and still within its extra TTL -- is
        not current, so a caller can refuse to treat it as proof.

        Args:
            key: Cache key.
            fetch: Callable producing the value on a miss.
            lock_timeout: The longest to wait, in seconds, for another
                thread's fetch of the same key; None waits for as long as
                it takes.

        Returns:
            The cached, freshly fetched, or last good list, and whether it is
            current.

        Raises:
            MetadataUnavailableError: A fetch for the key failed within the
                negative TTL and there is no last good copy, or the key's
                lock was not free within ``lock_timeout``.

        """
        cached = self._served(key)
        if cached is not None:
            return cached
        self._raise_if_failed_recently(key)
        with self._locks_guard:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        if not key_lock.acquire(timeout=-1 if lock_timeout is None else lock_timeout):
            msg = f"Another request is still fetching {key} from paperless-ngx"
            raise MetadataUnavailableError(msg)
        try:
            # Another thread may have filled the entry while this one waited,
            # or found paperless-ngx unreachable.
            cached = self._served(key)
            if cached is not None:
                return cached
            self._raise_if_failed_recently(key)
            generation = self.generation(key)
            try:
                data = fetch()
            except Exception as exc:
                stale, re_armed = self._re_arm(key, generation)
                if stale is None:
                    self._remember_failure(key, generation)
                    raise
                # The web tier's client-exception rule, stated above
                # routes._cached_list_or_none.
                reason = (
                    describe(exc)
                    if isinstance(exc, PaperlessError | ConfigError)
                    else type(exc).__name__
                )
                if re_armed:
                    logger.warning(_STALE_RE_ARMED, key, self._ttl, reason)
                else:
                    logger.warning(_STALE_NOT_RE_ARMED, key, reason)
                return CachedList(stale, current=False)
            self.store_if_current(key, data, generation)
            return CachedList(data, current=True)
        finally:
            key_lock.release()

    def _raise_if_failed_recently(self, key: str) -> None:
        """
        Refuse at once while a failed fetch for ``key`` is still remembered.

        Args:
            key: Cache key.

        Raises:
            MetadataUnavailableError: The key's negative entry has not expired.

        """
        failed_until = self._failed_until.get(key)
        if failed_until is not None and self._clock() < failed_until:
            msg = f"Could not fetch {key} from paperless-ngx a moment ago"
            raise MetadataUnavailableError(msg)

    def _remember_failure(self, key: str, generation: int) -> None:
        """
        Remember that fetching ``key`` just failed with nothing to fall back on.

        Nothing is recorded when the cache is disabled, or when an
        :meth:`invalidate` ran since ``generation`` was read: the Refresh that
        invalidated asked for a fresh attempt, and a failure that predates it
        must not refuse that attempt.  The negative TTL is read here, not
        bound at import, so it can be changed at run time.

        Args:
            key: Cache key whose fetch failed.
            generation: The key's generation when the fetch started.

        """
        if self._ttl <= 0:
            return
        with self._locks_guard:
            if self._generations.get(key, 0) != generation:
                return
            window = min(self._ttl, NEGATIVE_TTL_SECONDS)
            self._failed_until[key] = self._clock() + window

    def _served(self, key: str) -> CachedList | None:
        """
        Return the unexpired entry for ``key`` with whether it is current.

        The lookup itself goes through :meth:`get`, so the TTL rule lives in
        one place; the flag is read from the entry that list came from.

        Args:
            key: Cache key.

        Returns:
            The entry, or None when it is missing or expired.

        """
        cached = self.get(key)
        if cached is None:
            return None
        entry = self._store.get(key)
        current = entry is not None and entry[1] is cached and entry[2]
        return CachedList(cached, current=current)

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
                self._store[key] = (self._clock(), stale, False)
        return stale, re_armed

    def invalidate(self, key: str) -> None:
        """
        Remove a specific key from the cache.

        A fetch already in flight for the key will not cache its result, and a
        remembered failure is forgotten, so the next call fetches at once.  The
        key's last good copy is kept, so a refetch that fails can still serve it.

        Args:
            key: Cache key to invalidate.

        """
        with self._locks_guard:
            self._generations[key] = self._generations.get(key, 0) + 1
            self._store.pop(key, None)
            self._failed_until.pop(key, None)


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
