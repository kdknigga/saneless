"""
MetadataCache unit tests.

The cache takes its clock as a constructor parameter, so every assertion about
the TTL here moves a float instead of waiting for one to pass, and the
single-flight tests hold a fetch open on an Event rather than a pause.  Nothing
in this file sleeps.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, NoReturn
from unittest.mock import MagicMock

import httpx2
import pytest

from saneless.exceptions import PaperlessError
from saneless.paperless import PaperlessClient
from saneless.web.cache import (
    NEGATIVE_TTL_SECONDS,
    CachedList,
    CachedMetadataLookup,
    MetadataCache,
    MetadataUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

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
    """Cache stores and retrieves values by key."""
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
    """Explicit invalidation removes the cached entry."""
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
    """Different resources have independent cache entries."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    cache.set("tags", [{"id": 1, "name": "receipt"}])
    cache.set("correspondents", [{"id": 1, "name": "ACME Corp"}])
    cache.invalidate("tags")
    assert cache.get("tags") is None
    assert cache.get("correspondents") == [{"id": 1, "name": "ACME Corp"}]


def test_get_or_fetch_single_flight() -> None:
    """
    Concurrent misses for one key trigger a single fetch.

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

    threads = [
        threading.Thread(target=request, daemon=True) for _ in range(threads_count)
    ]
    for thread in threads:
        thread.start()
    try:
        assert fetch_started.wait(_WAIT_SECONDS)
        assert cache.enough_missed.wait(_WAIT_SECONDS)
    finally:
        gate.set()
        for thread in threads:
            thread.join(_WAIT_SECONDS)
    assert not any(thread.is_alive() for thread in threads), (
        "a request thread hung behind the single-flight fetch"
    )

    assert len(calls) == 1
    assert len(results) == threads_count
    assert all(result is results[0] for result in results)


def _counting_failure(calls: list[int]) -> Callable[[], _Rows]:
    """Return a fetch that records each call in ``calls`` and then fails."""

    def failing_fetch() -> _Rows:
        calls.append(1)
        msg = "paperless unreachable"
        raise ConnectionError(msg)

    return failing_fetch


def test_a_failure_is_remembered_for_the_negative_ttl() -> None:
    """
    With no previous value, a failure is remembered briefly instead of retried.

    The first call gets the fetch's own error.  Until the negative TTL passes,
    the next call is answered "unavailable" without asking paperless-ngx again;
    after it, the next call fetches again.
    """
    clock = _FakeClock()
    cache = MetadataCache(ttl=60, clock=clock)
    calls: list[int] = []
    failing_fetch = _counting_failure(calls)

    with pytest.raises(ConnectionError):
        cache.get_or_fetch("tags", failing_fetch)
    clock.advance(NEGATIVE_TTL_SECONDS - 1)
    with pytest.raises(MetadataUnavailableError):
        cache.get_or_fetch("tags", failing_fetch)
    assert calls == [1]

    clock.advance(2)
    with pytest.raises(ConnectionError):
        cache.get_or_fetch("tags", failing_fetch)
    assert calls == [1, 1]


def test_the_negative_window_never_outlasts_the_ttl() -> None:
    """A TTL shorter than the negative TTL bounds how long a failure is kept."""
    clock = _FakeClock()
    ttl = 5
    assert ttl < NEGATIVE_TTL_SECONDS
    cache = MetadataCache(ttl=ttl, clock=clock)
    calls: list[int] = []
    failing_fetch = _counting_failure(calls)

    with pytest.raises(ConnectionError):
        cache.get_or_fetch("tags", failing_fetch)
    clock.advance(ttl + 0.1)
    with pytest.raises(ConnectionError):
        cache.get_or_fetch("tags", failing_fetch)
    assert calls == [1, 1]


def test_invalidate_clears_the_negative_entry() -> None:
    """Refresh retries at once: an invalidate forgets the remembered failure."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    calls: list[int] = []

    with pytest.raises(ConnectionError):
        cache.get_or_fetch("tags", _counting_failure(calls))
    cache.invalidate("tags")
    rows: _Rows = [{"id": 1, "name": "receipt"}]

    assert cache.get_or_fetch_list("tags", lambda: rows) == CachedList(
        rows, current=True
    )
    assert calls == [1]


def test_zero_ttl_records_no_negative_entry() -> None:
    """A disabled cache remembers nothing, failures included."""
    cache = MetadataCache(ttl=0, clock=_FakeClock())
    calls: list[int] = []
    failing_fetch = _counting_failure(calls)

    with pytest.raises(ConnectionError):
        cache.get_or_fetch("tags", failing_fetch)
    with pytest.raises(ConnectionError):
        cache.get_or_fetch("tags", failing_fetch)
    assert calls == [1, 1]


def test_get_stays_none_during_the_negative_window() -> None:
    """
    A remembered failure is never read as an empty list.

    The pre-scan id check reads the cache's plain lookup; an empty list there
    would say "paperless-ngx has no tags", so during the negative window the
    lookup misses and the id check asks the client itself.
    """
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    with pytest.raises(ConnectionError):
        cache.get_or_fetch("tags", _counting_failure([]))

    assert cache.get("tags") is None
    client = _LookupClient(tags=[[{"id": 3}]])
    assert CachedMetadataLookup(cache, client).tag_ids(fresh=False) == frozenset({3})
    assert client.fetches == [("tags", 5.0)]


def test_waiters_behind_a_failing_fetch_return_at_once() -> None:
    """
    The threads queued behind a failing fetch do not each repeat it.

    The fetch is held open until every thread has missed the cache at least
    once, so each of them had the chance to start a fetch of its own; the
    fetching thread misses twice, before and after taking the lock.
    """
    threads_count = 8
    cache = _MissCountingCache(misses_wanted=threads_count + 1)
    barrier = threading.Barrier(threads_count)
    fetch_started = threading.Event()
    gate = threading.Event()
    calls: list[int] = []
    calls_lock = threading.Lock()
    outcomes: list[type[BaseException]] = []
    outcomes_lock = threading.Lock()

    def fetch() -> _Rows:
        with calls_lock:
            calls.append(1)
        fetch_started.set()
        gate.wait(_WAIT_SECONDS)
        msg = "paperless unreachable"
        raise ConnectionError(msg)

    def request() -> None:
        barrier.wait()
        try:
            cache.get_or_fetch("tags", fetch)
        except (ConnectionError, MetadataUnavailableError) as exc:
            with outcomes_lock:
                outcomes.append(type(exc))

    threads = [
        threading.Thread(target=request, daemon=True) for _ in range(threads_count)
    ]
    for thread in threads:
        thread.start()
    try:
        assert fetch_started.wait(_WAIT_SECONDS)
        assert cache.enough_missed.wait(_WAIT_SECONDS)
    finally:
        gate.set()
        for thread in threads:
            thread.join(_WAIT_SECONDS)
    assert not any(thread.is_alive() for thread in threads), (
        "a request thread hung behind the failing fetch"
    )

    assert len(calls) == 1
    assert sorted(outcomes, key=lambda kind: kind.__name__) == [ConnectionError] + [
        MetadataUnavailableError
    ] * (threads_count - 1)


def test_a_waiter_gives_up_after_its_lock_timeout() -> None:
    """
    A request does not wait out another thread's hung fetch.

    One thread's fetch is held open; a second request with a short lock
    timeout is answered "unavailable" without fetching, while the first is
    still in flight.
    """
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    entered = threading.Event()
    release = threading.Event()
    rows: _Rows = [{"id": 1, "name": "receipt"}]
    results: dict[str, CachedList] = {}
    second_calls: list[int] = []

    def hung_fetch() -> _Rows:
        entered.set()
        release.wait(_WAIT_SECONDS)
        return rows

    def second_fetch() -> _Rows:
        second_calls.append(1)
        return rows

    def in_flight() -> None:
        results["in_flight"] = cache.get_or_fetch_list("tags", hung_fetch)

    thread = threading.Thread(target=in_flight, daemon=True)
    thread.start()
    try:
        assert entered.wait(_WAIT_SECONDS)
        with pytest.raises(MetadataUnavailableError):
            cache.get_or_fetch_list("tags", second_fetch, lock_timeout=0.05)
        assert not release.is_set()
    finally:
        release.set()
        thread.join(_WAIT_SECONDS)
    assert not thread.is_alive(), "the in-flight fetch thread hung"

    assert second_calls == []
    assert results["in_flight"] == CachedList(rows, current=True)


def test_an_invalidate_during_the_failing_fetch_wins() -> None:
    """
    A Refresh pressed while a failing fetch is in flight is not undone.

    The failure lands after the invalidate, so it must not be remembered: the
    next call fetches again at once.  An invalidate made from inside the fetch
    runs while the fetch is in flight, without a second thread.
    """
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    calls: list[int] = []

    def failing_fetch() -> _Rows:
        calls.append(1)
        cache.invalidate("tags")
        msg = "paperless unreachable"
        raise ConnectionError(msg)

    with pytest.raises(ConnectionError):
        cache.get_or_fetch("tags", failing_fetch)
    with pytest.raises(ConnectionError):
        cache.get_or_fetch("tags", failing_fetch)
    assert calls == [1, 1]


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

    first = threading.Thread(target=in_flight, daemon=True)
    first.start()
    second = threading.Thread(target=refresh, daemon=True)
    try:
        assert entered.wait(_WAIT_SECONDS)
        cache.invalidate("tags")
        second.start()
    finally:
        release.set()
        first.join(_WAIT_SECONDS)
        if second.ident is not None:
            second.join(_WAIT_SECONDS)
    assert not first.is_alive(), "the stale in-flight fetch thread hung"
    assert not second.is_alive(), "the refresh thread hung"

    assert results["in_flight"] is stale
    assert results["refresh"] is fresh
    assert cache.get("tags") is fresh


def test_get_or_fetch_returns_fresh_value_without_fetching() -> None:
    """A fresh value is served from cache; an expired one is refetched."""
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


def test_the_last_good_copy_is_served_as_not_current() -> None:
    """
    A caller can tell the last good copy from a list paperless-ngx just gave.

    Only a current list may show an id gone; the copy predates anything
    created or deleted since.  The flag holds for the re-armed entry's whole
    extra TTL, and a successful refresh makes the list current again.
    """
    clock = _FakeClock()
    cache = MetadataCache(ttl=60, clock=clock)
    good: _Rows = [{"id": 1, "name": "receipt"}]

    fetched = cache.get_or_fetch_list("tags", lambda: good)
    assert fetched == CachedList(good, current=True)
    assert cache.get_or_fetch_list("tags", list) == fetched

    clock.advance(61)
    down = cache.get_or_fetch_list("tags", lambda: _raise(PaperlessError("down")))
    assert down == CachedList(good, current=False)
    clock.advance(1)
    rearmed = cache.get_or_fetch_list("tags", lambda: _raise(PaperlessError("down")))
    assert rearmed == CachedList(good, current=False)

    clock.advance(61)
    recovered: _Rows = [{"id": 2, "name": "invoice"}]
    assert cache.get_or_fetch_list("tags", lambda: recovered) == CachedList(
        recovered, current=True
    )


def test_unexpected_refresh_failure_serves_the_last_good_value_by_class_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A failure that is not a Paperless error is served stale, named by class only.

    Its text is not saneless's and may quote a URL or a token, so neither the
    message nor a traceback is logged.
    """
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
    assert records[0].getMessage().endswith(": RuntimeError")
    assert "surprise" not in caplog.text
    assert records[0].exc_info is None


# Text no log record may carry: a stand-in for a configured token, and the
# userinfo of a URL that third-party exception text might quote.
_MARKER = "tok-MARKER-9b1e5c"
_MARKED_TEXT = f"refused: Token {_MARKER} user:pass@paperless.invalid"


def _marked_client_fetch() -> _Rows:
    """
    Fetch tags through a real client whose token is the marker, and fail.

    The transport raises httpx2's error quoting the token, so what reaches the
    cache is the ``PaperlessError`` the client itself builds.

    Returns:
        Never: the fetch always raises.

    """

    def refuse(request: httpx2.Request) -> NoReturn:
        msg = f"refused: {_MARKER}"
        raise httpx2.ConnectError(msg, request=request)

    client = PaperlessClient(
        url="http://paperless.invalid:8000",
        token=_MARKER,
        transport=httpx2.MockTransport(refuse),
    )
    return client.get_tags()


def _foreign_fetch() -> _Rows:
    """Fail the way no saneless code would, with text quoting the marker."""
    raise RuntimeError(_MARKED_TEXT)


@pytest.mark.parametrize(
    ("fetch", "cause"),
    [
        pytest.param(
            _marked_client_fetch,
            "Could not fetch tags from Paperless at http://paperless.invalid:8000",
            id="paperless-error",
        ),
        pytest.param(_foreign_fetch, "RuntimeError", id="other"),
    ],
)
@pytest.mark.parametrize(
    ("invalidate_during_fetch", "wording"),
    [
        pytest.param(False, "for another 60s: ", id="re-armed"),
        pytest.param(True, "the next request fetches again: ", id="not-re-armed"),
    ],
)
def test_stale_warning_never_carries_marker_text(
    caplog: pytest.LogCaptureFixture,
    fetch: Callable[[], _Rows],
    cause: str,
    *,
    invalidate_during_fetch: bool,
    wording: str,
) -> None:
    """
    Both stale-copy warnings name the cause without the marker or a traceback.

    An invalidate made from inside the fetch runs while the fetch is in
    flight, which is what stops the re-arm, without a second thread.
    """
    clock = _FakeClock()
    cache = MetadataCache(ttl=60, clock=clock)
    good: _Rows = [{"id": 1, "name": "receipt"}]
    cache.set("tags", good)
    clock.advance(61)

    def failing_fetch() -> _Rows:
        if invalidate_during_fetch:
            cache.invalidate("tags")
        return fetch()

    caplog.set_level(logging.DEBUG)
    served = cache.get_or_fetch("tags", failing_fetch)

    assert served is good
    assert _MARKER not in caplog.text
    assert "user:pass" not in caplog.text
    [record] = _warnings(caplog)
    assert record.exc_info is None
    assert wording + cause in record.getMessage()


def test_failure_without_a_last_good_value_is_negative_cached_outside_the_store(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    With nothing to fall back on the error reaches the caller, unlogged here.

    The failure is remembered, but not as a list: the plain lookup still
    misses, and the next call's "unavailable" answer logs nothing either.
    """
    cache = MetadataCache(ttl=60, clock=_FakeClock())

    with (
        caplog.at_level(logging.WARNING, logger=_CACHE_LOGGER),
        pytest.raises(PaperlessError, match="boom"),
    ):
        cache.get_or_fetch("tags", lambda: _raise(PaperlessError("boom")))

    assert cache.get("tags") is None
    with (
        caplog.at_level(logging.DEBUG, logger=_CACHE_LOGGER),
        pytest.raises(MetadataUnavailableError),
    ):
        cache.get_or_fetch("tags", lambda: _raise(PaperlessError("boom")))

    assert cache.get("tags") is None
    assert [r for r in caplog.records if r.name == _CACHE_LOGGER] == []


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

    thread = threading.Thread(target=in_flight, daemon=True)
    with caplog.at_level(logging.WARNING, logger=_CACHE_LOGGER):
        thread.start()
        try:
            assert entered.wait(_WAIT_SECONDS)
            cache.invalidate("tags")
        finally:
            release.set()
            thread.join(_WAIT_SECONDS)
    assert not thread.is_alive(), "the failing in-flight fetch thread hung"

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


class _LookupClient:
    """A client double for the metadata lookup, answering from a queue per list."""

    def __init__(
        self,
        *,
        tags: list[_Rows | Exception] | None = None,
        correspondents: list[_Rows | Exception] | None = None,
    ) -> None:
        """Take one answer per expected fetch, per list, in order."""
        self._answers = {"tags": tags or [], "correspondents": correspondents or []}
        self.fetches: list[tuple[str, float | None]] = []

    def get_tags(self, *, timeout: float | None = None) -> _Rows:
        """Answer the tag list."""
        return self._serve("tags", timeout)

    def get_correspondents(self, *, timeout: float | None = None) -> _Rows:
        """Answer the correspondent list."""
        return self._serve("correspondents", timeout)

    def _serve(self, what: str, timeout: float | None) -> _Rows:
        """Record the fetch, then raise or answer the next scripted value."""
        self.fetches.append((what, timeout))
        answer = self._answers[what].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_cached_lookup_reads_a_cached_list_without_a_request() -> None:
    """A cached list answers the first look; paperless-ngx is not asked."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    cache.set("tags", [{"id": 3}, {"id": 7}])
    client = _LookupClient()

    ids = CachedMetadataLookup(cache, client).tag_ids(fresh=False)

    assert ids == frozenset({3, 7})
    assert client.fetches == []


def test_cached_lookup_miss_fetches_briefly_and_stores() -> None:
    """Nothing cached: one 5 s fetch, and its list is cached for the pages."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    rows: _Rows = [{"id": 12}]
    client = _LookupClient(correspondents=[rows])

    ids = CachedMetadataLookup(cache, client).correspondent_ids(fresh=False)

    assert ids == frozenset({12})
    assert client.fetches == [("correspondents", 5.0)]
    assert cache.get("correspondents") is rows


def test_cached_lookup_fresh_goes_to_paperless_and_stores() -> None:
    """A refetch bypasses the cached copy and replaces it on success."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    cache.set("tags", [{"id": 3}])
    rows: _Rows = [{"id": 3}, {"id": 9}]
    client = _LookupClient(tags=[rows])

    ids = CachedMetadataLookup(cache, client).tag_ids(fresh=True)

    assert ids == frozenset({3, 9})
    assert client.fetches == [("tags", 5.0)]
    assert cache.get("tags") is rows


def test_cached_lookup_failed_refetch_is_unavailable_not_the_stale_copy() -> None:
    """A failed refetch says 'cannot tell'; the last good copy proves nothing."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    cached: _Rows = [{"id": 3}]
    cache.set("tags", cached)
    client = _LookupClient(tags=[PaperlessError("down")])

    assert CachedMetadataLookup(cache, client).tag_ids(fresh=True) is None
    assert cache.get("tags") is cached


def test_cached_lookup_failed_first_fetch_is_unavailable() -> None:
    """Nothing cached and paperless-ngx down: the ids go unchecked."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    client = _LookupClient(tags=[PaperlessError("down")])

    assert CachedMetadataLookup(cache, client).tag_ids(fresh=False) is None
    assert cache.get("tags") is None


def test_cached_lookup_never_overwrites_a_refresh_that_ran_during_its_fetch() -> None:
    """
    An older fetch finishing after a Refresh leaves the refreshed list cached.

    The scan's fetch starts; the operator adds a tag in paperless-ngx and
    presses Refresh, which invalidates the cache and stores the new list; then
    the scan's fetch returns the list as it was.  Stored, that older list would
    hide the new tag from the picker for a whole TTL and become the last good
    copy.
    """
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    older: _Rows = [{"id": 3}]
    refreshed: _Rows = [{"id": 3}, {"id": 9}]

    class _RefreshedMidFetch:
        """A client whose tag fetch overlaps the operator's Refresh."""

        def get_tags(self, *, timeout: float | None = None) -> _Rows:
            """Let the Refresh run to completion, then answer the older list."""
            del timeout
            cache.invalidate("tags")
            cache.get_or_fetch("tags", lambda: refreshed)
            return older

        def get_correspondents(self, *, timeout: float | None = None) -> _Rows:
            """Not asked for in this test."""
            del timeout
            return []

    ids = CachedMetadataLookup(cache, _RefreshedMidFetch()).tag_ids(fresh=True)

    assert ids == frozenset({3})
    assert cache.get("tags") is refreshed


def test_store_if_current_declines_after_an_invalidate() -> None:
    """The guard itself: a generation read before an invalidate stores nothing."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    generation = cache.generation("tags")
    cache.invalidate("tags")

    assert cache.store_if_current("tags", [{"id": 1}], generation) is False
    assert cache.get("tags") is None
    assert cache.store_if_current("tags", [{"id": 1}], cache.generation("tags"))
    assert cache.get("tags") == [{"id": 1}]


def test_cached_lookup_never_caches_a_non_list() -> None:
    """A stub's answer that is not a list is 'cannot tell', and is not stored."""
    cache = MetadataCache(ttl=60, clock=_FakeClock())
    lookup = CachedMetadataLookup(cache, MagicMock(spec=PaperlessClient))

    assert lookup.tag_ids(fresh=False) is None
    assert cache.get("tags") is None
