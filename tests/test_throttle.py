"""
The endpoints that reach paperless-ngx on every call have a rate floor.

``POST /api/cache/invalidate`` and ``GET /api/paperless/test`` are
unauthenticated on the LAN and each call could send a token-bearing request
upstream, so a loop against either must not become unbounded paperless-ngx
traffic.  Each invalidated resource has a minimum interval between refetches,
and the connection test shares one briefly reused result between callers.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import TYPE_CHECKING, NoReturn, get_args

import pytest
from fastapi.testclient import TestClient

from saneless.web import checks_cache as checks_cache_module
from saneless.web.app import create_app
from saneless.web.errors import RETRY_AFTER_SECONDS
from saneless.web.routes import MetadataResource
from saneless.web.throttle import (
    MIN_MANUAL_REFRESH_SECONDS,
    MinimumInterval,
    SingleFlightResult,
)
from tests.conftest import StubScannerBackend

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi import FastAPI

    from saneless.config import Settings


# How long a thread test waits for something that should happen at once.  Far
# below pytest-timeout's 60 s, so a failing test reports what it was waiting for.
_EVENT_TIMEOUT_SECONDS = 5.0

# How long a thread test watches for something that must *not* happen yet --
# a follower that should still be blocked.  Long enough that a follower which
# was going to return would have; short enough to keep the suite quick.
_STILL_BLOCKED_SECONDS = 0.2

# The number of calls a scripted loop makes in the route tests.
_LOOP_CALLS = 50


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


class TestMinimumInterval:
    """A minimum interval between granted claims, shared with manual refresh."""

    def test_the_floor_is_the_manual_refresh_floor(self) -> None:
        """Everything paperless-bound shares one 2 s floor, the cache's included."""
        assert MIN_MANUAL_REFRESH_SECONDS == 2.0
        assert (
            checks_cache_module.MIN_MANUAL_REFRESH_SECONDS == MIN_MANUAL_REFRESH_SECONDS
        )

    def test_the_first_claim_is_granted_with_its_stamp(self) -> None:
        """A fresh floor grants, and reports the clock reading it recorded."""
        floor = MinimumInterval(clock=_FakeClock())
        assert floor.claim() == 100.0

    def test_a_claim_at_zero_is_a_grant_not_a_refusal(self) -> None:
        """A 0.0 stamp is a real grant: callers test ``is None``, not truthiness."""
        floor = MinimumInterval(clock=_FakeClock(start=0.0))
        stamp = floor.claim()
        assert stamp is not None
        assert stamp == 0.0
        assert floor.claim() is None

    def test_a_second_claim_inside_the_interval_is_refused(self) -> None:
        """A second claim 1.9 s after a grant is refused."""
        clock = _FakeClock()
        floor = MinimumInterval(clock=clock)
        assert floor.claim() is not None
        clock.advance(1.9)
        assert floor.claim() is None

    def test_a_refusal_does_not_move_the_stamp(self) -> None:
        """
        A refused claim changes nothing, so hammering cannot hold the floor shut.

        Had the refusal at +1.5 s stamped, the claim at +2.0 s would be only
        0.5 s after it and refused too.
        """
        clock = _FakeClock()
        floor = MinimumInterval(clock=clock)
        assert floor.claim() is not None
        clock.advance(1.5)
        assert floor.claim() is None
        clock.advance(0.5)
        assert floor.claim() == 102.0

    def test_a_claim_after_the_interval_is_granted(self) -> None:
        """The floor is a floor, not a ban: after 2 s the next claim is granted."""
        clock = _FakeClock()
        floor = MinimumInterval(clock=clock)
        assert floor.claim() is not None
        clock.advance(MIN_MANUAL_REFRESH_SECONDS)
        assert floor.claim() is not None

    def test_the_interval_can_be_passed_per_claim(self) -> None:
        """A caller may ask for a different interval on one claim."""
        clock = _FakeClock()
        floor = MinimumInterval(clock=clock)
        assert floor.claim(min_interval=0.5) is not None
        clock.advance(0.4)
        assert floor.claim(min_interval=0.5) is None
        clock.advance(0.1)
        assert floor.claim(min_interval=0.5) is not None

    def test_releasing_the_recorded_stamp_reopens_the_floor(self) -> None:
        """A matching release clears the claim, so the next claim is granted."""
        floor = MinimumInterval(clock=_FakeClock())
        stamp = floor.claim()
        assert stamp is not None
        assert floor.release(stamp) is True
        assert floor.claim() is not None

    def test_releasing_a_stale_stamp_is_a_no_op(self) -> None:
        """Only the recorded grant can be given back; any other leaves the floor."""
        clock = _FakeClock()
        floor = MinimumInterval(clock=clock)
        old = floor.claim()
        assert old is not None
        clock.advance(MIN_MANUAL_REFRESH_SECONDS)
        assert floor.claim() is not None
        assert floor.release(old) is False
        assert floor.claim() is None

    def test_releasing_with_nothing_recorded_is_a_no_op(self) -> None:
        """A release before any claim clears nothing."""
        floor = MinimumInterval(clock=_FakeClock())
        assert floor.release(100.0) is False

    def test_concurrent_claims_get_exactly_one_grant(self) -> None:
        """Two request threads arriving together cannot both be granted."""
        floor = MinimumInterval(clock=_FakeClock())
        barrier = threading.Barrier(2, timeout=_EVENT_TIMEOUT_SECONDS)

        def claim() -> float | None:
            barrier.wait()
            return floor.claim()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            results = [f.result(timeout=_EVENT_TIMEOUT_SECONDS) for f in futures]

        assert sum(r is not None for r in results) == 1


class _Gate:
    """A compute function that blocks until the test lets it finish."""

    def __init__(self, value: str) -> None:
        """Hold ``value`` until :meth:`finish` is called."""
        self.value = value
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self) -> str:
        """Record the call, say so, then wait for the test to release it."""
        self.calls += 1
        self.entered.set()
        if not self.release.wait(_EVENT_TIMEOUT_SECONDS):
            msg = "the test never released the gated compute"
            raise AssertionError(msg)
        return self.value


class _Counter:
    """A compute function that answers at once and counts its calls."""

    def __init__(self, value: str) -> None:
        """Answer ``value`` on every call."""
        self.value = value
        self.calls = 0

    def __call__(self) -> str:
        """Count the call and answer."""
        self.calls += 1
        return self.value


class TestSingleFlightResult:
    """One shared, briefly reused result, computed by one caller at a time."""

    @staticmethod
    def _flight(
        clock: _FakeClock, wait_bound: float = _EVENT_TIMEOUT_SECONDS
    ) -> SingleFlightResult[str]:
        """Build a result holder on the 2 s floor and the given clock."""
        return SingleFlightResult(
            ttl=MIN_MANUAL_REFRESH_SECONDS, wait_bound=wait_bound, clock=clock
        )

    def test_the_first_get_computes(self) -> None:
        """With nothing cached, the caller computes and gets the value."""
        flight = self._flight(_FakeClock())
        compute = _Counter("connected")
        assert flight.get(compute) == "connected"
        assert compute.calls == 1

    def test_a_get_inside_the_ttl_reuses_the_result(self) -> None:
        """A second caller inside the TTL is answered without computing."""
        clock = _FakeClock()
        flight = self._flight(clock)
        first = _Counter("connected")
        second = _Counter("unreachable")
        assert flight.get(first) == "connected"
        clock.advance(1.9)
        assert flight.get(second) == "connected"
        assert second.calls == 0

    def test_a_get_after_the_ttl_recomputes(self) -> None:
        """Once the TTL has passed, the next caller computes afresh."""
        clock = _FakeClock()
        flight = self._flight(clock)
        assert flight.get(_Counter("connected")) == "connected"
        clock.advance(MIN_MANUAL_REFRESH_SECONDS)
        second = _Counter("unreachable")
        assert flight.get(second) == "unreachable"
        assert second.calls == 1

    def test_a_follower_with_a_previous_result_does_not_block(self) -> None:
        """
        While a compute is in flight, a caller with an old answer gets it at once.

        This is the thread-pool guard: every request handler holds one of
        anyio's 40 worker threads, so a follower that blocked on the leader
        would hold one too, and a loop against an unreachable paperless would
        exhaust the pool.
        """
        clock = _FakeClock()
        flight = self._flight(clock)
        assert flight.get(_Counter("connected")) == "connected"
        clock.advance(MIN_MANUAL_REFRESH_SECONDS)
        leader = _Gate("unreachable")
        follower = _Counter("never")

        with ThreadPoolExecutor(max_workers=2) as pool:
            leading = pool.submit(flight.get, leader)
            try:
                assert leader.entered.wait(_EVENT_TIMEOUT_SECONDS)
                following = pool.submit(flight.get, follower)
                assert following.result(timeout=_EVENT_TIMEOUT_SECONDS) == "connected"
                assert not leader.release.is_set()
            finally:
                leader.release.set()
            assert leading.result(timeout=_EVENT_TIMEOUT_SECONDS) == "unreachable"

        assert follower.calls == 0
        assert flight.get(_Counter("never")) == "unreachable"

    def test_a_follower_with_no_result_waits_for_the_leader(self) -> None:
        """Before any result exists, a follower waits and shares the leader's."""
        flight = self._flight(_FakeClock())
        leader = _Gate("connected")
        follower = _Counter("never")

        with ThreadPoolExecutor(max_workers=2) as pool:
            leading = pool.submit(flight.get, leader)
            try:
                assert leader.entered.wait(_EVENT_TIMEOUT_SECONDS)
                following = pool.submit(flight.get, follower)
                with pytest.raises(FutureTimeout):
                    following.result(timeout=_STILL_BLOCKED_SECONDS)
            finally:
                leader.release.set()
            assert leading.result(timeout=_EVENT_TIMEOUT_SECONDS) == "connected"
            assert following.result(timeout=_EVENT_TIMEOUT_SECONDS) == "connected"

        assert leader.calls == 1
        assert follower.calls == 0

    def test_only_two_followers_with_no_result_wait(self) -> None:
        """
        A third caller before any result exists is refused at once.

        Each waiting follower holds one of anyio's 40 worker threads for up to
        the wait bound, so a burst of callers straight after a restart,
        against an unreachable paperless, would otherwise hold the whole pool
        and stall every other route.  The wait bound here is far longer than
        the test watches, so only the cap can answer the third caller in time.
        """
        flight = self._flight(_FakeClock(), wait_bound=30.0)
        leader = _Gate("connected")
        third = _Counter("never")

        with ThreadPoolExecutor(max_workers=4) as pool:
            leading = pool.submit(flight.get, leader)
            try:
                assert leader.entered.wait(_EVENT_TIMEOUT_SECONDS)
                waiting = [pool.submit(flight.get, _Counter("never")) for _ in "ab"]
                for follower in waiting:
                    with pytest.raises(FutureTimeout):
                        follower.result(timeout=_STILL_BLOCKED_SECONDS)
                refused = pool.submit(flight.get, third)
                error = refused.exception(timeout=_EVENT_TIMEOUT_SECONDS)
                assert isinstance(error, TimeoutError), error
                assert not leader.release.is_set()
            finally:
                leader.release.set()
            assert leading.result(timeout=_EVENT_TIMEOUT_SECONDS) == "connected"
            for follower in waiting:
                assert follower.result(timeout=_EVENT_TIMEOUT_SECONDS) == "connected"

        assert third.calls == 0
        assert flight.get(_Counter("never")) == "connected"

    def test_a_follower_gives_up_after_the_wait_bound(self) -> None:
        """A leader slower than the wait bound leaves its follower a TimeoutError."""
        flight = self._flight(_FakeClock(), wait_bound=0.05)
        leader = _Gate("connected")

        with ThreadPoolExecutor(max_workers=2) as pool:
            leading = pool.submit(flight.get, leader)
            try:
                assert leader.entered.wait(_EVENT_TIMEOUT_SECONDS)
                following = pool.submit(flight.get, _Counter("never"))
                with pytest.raises(TimeoutError):
                    following.result(timeout=_EVENT_TIMEOUT_SECONDS)
            finally:
                leader.release.set()
            assert leading.result(timeout=_EVENT_TIMEOUT_SECONDS) == "connected"

    def test_a_raising_compute_does_not_wedge_the_flight(self) -> None:
        """Compute must not raise; if it does, the next caller can still compute."""
        flight = self._flight(_FakeClock())

        def boom() -> str:
            msg = "boom"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError):
            flight.get(boom)
        assert flight.get(_Counter("connected")) == "connected"


@pytest.fixture
def app(make_settings: Callable[..., Settings]) -> FastAPI:
    """Build the web app on the suite's settings and the stub scanner."""
    return create_app(make_settings(), StubScannerBackend())


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Yield a client with the app's lifespan entered for the whole test."""
    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def clock() -> _FakeClock:
    """Return the clock the route tests hand the app's floors."""
    return _FakeClock()


class _MetadataCounter:
    """A paperless metadata fetch that answers fixed rows and counts calls."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        """Answer ``rows`` on every call."""
        self.rows = rows
        self.calls = 0

    def __call__(self, *, timeout: object = None) -> list[dict[str, object]]:
        """Count the call and answer a copy of the rows, whatever the budget."""
        del timeout
        self.calls += 1
        return list(self.rows)


@pytest.fixture
def upstream(app: FastAPI, clock: _FakeClock) -> dict[str, _MetadataCounter]:
    """
    Count the app's paperless fetches, and put its floors on the fake clock.

    The floors are rebuilt on ``clock`` so a loop of 50 requests is inside the
    2 s floor however slowly the machine runs it.  That the app builds its own
    floors is ``test_create_app_installs_one_floor_per_resource``'s job.
    """
    tags = _MetadataCounter([{"id": 1, "name": "receipt"}])
    correspondents = _MetadataCounter([{"id": 1, "name": "ACME Corp"}])
    app.state.paperless.get_tags = tags
    app.state.paperless.get_correspondents = correspondents
    app.state.invalidate_floors = {
        resource: MinimumInterval(clock=clock)
        for resource in get_args(MetadataResource)
    }
    return {"tags": tags, "correspondents": correspondents}


class TestInvalidateFloor:
    """``POST /api/cache/invalidate`` refetches at most once per floor window."""

    # The client runs the lifespan, whose shutdown closes the job store.
    @pytest.mark.usefixtures("client")
    def test_create_app_installs_one_floor_per_resource(self, app: FastAPI) -> None:
        """Every invalidatable resource has its own floor, built by the app."""
        floors = app.state.invalidate_floors
        assert set(floors) == set(get_args(MetadataResource))
        assert all(isinstance(floor, MinimumInterval) for floor in floors.values())
        assert floors["tags"] is not floors["correspondents"]

    def test_a_tags_loop_reaches_paperless_once(
        self, client: TestClient, upstream: dict[str, _MetadataCounter]
    ) -> None:
        """
        Fifty invalidates inside the floor cost one refetch, and every one renders.

        The cold fetch is the one the first ``GET /api/tags`` needs anyway; the
        loop may add exactly one more.  A too-soon call is not an error: it is
        answered from the cache with the same partial.
        """
        assert client.get("/api/tags").status_code == 200
        assert upstream["tags"].calls == 1

        for _ in range(_LOOP_CALLS):
            response = client.post("/api/cache/invalidate?resource=tags")
            assert response.status_code == 200
            assert 'id="tags-list"' in response.text
            assert "receipt" in response.text

        assert upstream["tags"].calls == 2

    def test_a_correspondents_loop_reaches_paperless_once(
        self, client: TestClient, upstream: dict[str, _MetadataCounter]
    ) -> None:
        """The correspondent list has the same floor, and renders every time."""
        for _ in range(_LOOP_CALLS):
            response = client.post("/api/cache/invalidate?resource=correspondents")
            assert response.status_code == 200
            assert "ACME Corp" in response.text

        assert upstream["correspondents"].calls == 1

    def test_a_tags_invalidate_leaves_the_correspondents_floor_open(
        self, client: TestClient, upstream: dict[str, _MetadataCounter]
    ) -> None:
        """Each resource has its own floor; one does not spend the other's."""
        assert client.get("/api/correspondents").status_code == 200
        assert upstream["correspondents"].calls == 1
        assert client.post("/api/cache/invalidate?resource=tags").status_code == 200

        response = client.post("/api/cache/invalidate?resource=correspondents")

        assert response.status_code == 200
        assert upstream["correspondents"].calls == 2

    def test_an_invalidate_after_the_floor_refetches(
        self,
        client: TestClient,
        upstream: dict[str, _MetadataCounter],
        clock: _FakeClock,
    ) -> None:
        """The floor is a floor, not a ban: after 2 s the refresh button works."""
        assert client.post("/api/cache/invalidate?resource=tags").status_code == 200
        assert client.post("/api/cache/invalidate?resource=tags").status_code == 200
        assert upstream["tags"].calls == 1

        clock.advance(MIN_MANUAL_REFRESH_SECONDS)
        assert client.post("/api/cache/invalidate?resource=tags").status_code == 200

        assert upstream["tags"].calls == 2


class _ConnectionProbe:
    """A ``test_connection`` stand-in that counts calls, or raises on each."""

    def __init__(self, answer: str | None = "connected") -> None:
        """Answer ``answer``, or raise ``RuntimeError`` when it is None."""
        self.answer = answer
        self.calls = 0

    def __call__(self, *, timeout: object = None) -> str:
        """Count the call, then answer or raise, whatever the timeout."""
        _ = timeout
        self.calls += 1
        if self.answer is None:
            msg = "boom"
            raise RuntimeError(msg)
        return self.answer


def _shared_result(clock: _FakeClock) -> SingleFlightResult[object]:
    """Build a connection-test result holder on the fake clock."""
    return SingleFlightResult(
        ttl=MIN_MANUAL_REFRESH_SECONDS, wait_bound=_EVENT_TIMEOUT_SECONDS, clock=clock
    )


class TestPaperlessTestSingleFlight:
    """``GET /api/paperless/test`` shares one result for the floor window."""

    # The client runs the lifespan, whose shutdown closes the job store.
    @pytest.mark.usefixtures("client")
    def test_create_app_installs_a_shared_result(self, app: FastAPI) -> None:
        """The app builds the connection test's shared result itself."""
        assert isinstance(app.state.paperless_test_result, SingleFlightResult)

    def test_a_loop_reaches_paperless_once(
        self, app: FastAPI, client: TestClient, clock: _FakeClock
    ) -> None:
        """Fifty calls inside the TTL make one probe and give one answer."""
        probe = _ConnectionProbe("connected")
        app.state.paperless.test_connection = probe
        app.state.paperless_test_result = _shared_result(clock)

        for _ in range(_LOOP_CALLS):
            response = client.get("/api/paperless/test")
            assert response.status_code == 200
            assert response.json() == {"status": "connected"}

        assert probe.calls == 1

    def test_a_failing_probe_is_shared_the_same_way(
        self, app: FastAPI, client: TestClient, clock: _FakeClock
    ) -> None:
        """A probe that raised is answered from the cache as the same 500."""
        probe = _ConnectionProbe(None)
        app.state.paperless.test_connection = probe
        app.state.paperless_test_result = _shared_result(clock)

        bodies = []
        for _ in range(_LOOP_CALLS):
            response = client.get("/api/paperless/test")
            assert response.status_code == 500
            bodies.append(response.json())

        assert probe.calls == 1
        assert bodies == [{"status": "error", "detail": "RuntimeError"}] * _LOOP_CALLS

    def test_a_call_after_the_ttl_probes_again(
        self, app: FastAPI, client: TestClient, clock: _FakeClock
    ) -> None:
        """Once the result is older than the TTL, the next call probes afresh."""
        probe = _ConnectionProbe("connected")
        app.state.paperless.test_connection = probe
        app.state.paperless_test_result = _shared_result(clock)
        assert client.get("/api/paperless/test").json() == {"status": "connected"}

        clock.advance(MIN_MANUAL_REFRESH_SECONDS)
        probe.answer = "unreachable"

        assert client.get("/api/paperless/test").json() == {"status": "unreachable"}
        assert probe.calls == 2

    def test_a_follower_that_gives_up_gets_the_error_shape(
        self, app: FastAPI, client: TestClient
    ) -> None:
        """A bounded wait that expires is a 503 with Retry-After, naming TimeoutError."""

        class _TimesOut:
            """A shared result whose wait for the leader always expires."""

            def get(self, compute: Callable[[], object]) -> NoReturn:
                """Give up without computing, as an expired wait does."""
                _ = compute
                msg = "the in-flight connection test did not finish"
                raise TimeoutError(msg)

        app.state.paperless_test_result = _TimesOut()

        response = client.get("/api/paperless/test")

        assert response.status_code == 503
        assert response.headers["Retry-After"] == str(RETRY_AFTER_SECONDS)
        assert response.json() == {"status": "error", "detail": "TimeoutError"}
