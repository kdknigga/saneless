"""
The status strip's routes, context and markup.

Covers requirements: APPL-01, APPL-02, APPL-06, APPL-11 (decisions D-04, D-05,
D-06, D-08, D-09, D-22).

This file exists because the strip is the **web half of the shared registry**.
``saneless.checks`` already owns every word a row can say and
``tests/test_checks.py`` pins those words; what nothing pinned until now is the
half that decides *when* a probe happens and *what markup* carries it.  Both
are load-bearing:

* **Zero probes in a request handler (D-04).**  An unplugged sane-net host is a
  TCP connect that hangs until the OS gives up, so a page that probed would be
  a page that hangs.  The mechanism is "the page reads a cache a background
  thread fills", and the only way to prove the page honours it is to spy on
  ``run_checks`` and assert the count is zero.
* **A poll that stops itself (D-06).**  The cold-start body carries
  ``hx-trigger``; the body that replaces it does not, so the poll ends.  A
  steady-state poll would keep D-05's lazy refresher awake for every abandoned
  browser tab, which is exactly what D-05 forbids.

The assertion style is ``tests/test_web_state_rendering.py``'s: compiled
regexes matched over the whole rendered page, so an attribute assertion cannot
be satisfied by markup somewhere else on the page.
"""

from __future__ import annotations

import inspect
import logging
import re
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saneless import checks as checks_module
from saneless.checks import (
    CHECKING_GLYPH,
    CHECKING_MESSAGE,
    CHECKING_STATE_CLASS,
    CHECKING_STATE_LABEL,
    SKIPPED_STATE_LABEL,
    CheckKey,
    CheckResult,
    CheckState,
    check_name,
    check_state_class,
    check_state_glyph,
    check_state_label,
)
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.paperless import PaperlessClient
from saneless.scanner.base import DeviceInfo
from saneless.vocabulary import ConnectionStatus, JobState, local_time
from saneless.web import app as app_module
from saneless.web import refresher as refresher_module
from saneless.web import routes as routes_module
from saneless.web.app import create_app
from saneless.web.checks_cache import CheckCache
from tests.conftest import StubScannerBackend

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Iterator

    from starlette.datastructures import State

    from saneless.checks import CheckContext
    from saneless.web.refresher import CheckRefresher

# The paused Scanner row, quoted from UI-SPEC S1 so the route test fails if the
# registry's sentence and the page's sentence ever drift apart (D-02).
PAUSED_SCANNER_MESSAGE = "Not checked while a scan is running."
PAUSED_PREFIX = "Paused during scan — "

# The strip body, captured whole.  Its attributes are asserted against this
# match rather than against the whole response, so "the page contains
# hx-trigger somewhere" can never pass for "the strip polls".
_CHECKS_BODY = re.compile(r'<div id="checks-body"(?P<attrs>[^>]*)>', re.DOTALL)
_HX_GET = re.compile(r'hx-get="(?P<url>[^"]+)"')
_CHECK_ROW = re.compile(r'<li class="check-row">(?P<row>.*?)</li>', re.DOTALL)
_CHECK_META = re.compile(r'<p class="check-meta">(?P<text>.*?)</p>', re.DOTALL)


class _StubScanner(StubScannerBackend):
    """
    The shared stub backend, but reporting one device instead of none.

    Copied from ``tests/test_web_state_rendering.py``: the profile dropdown and
    the worker's startup profile generation both read ``get_devices``, and a
    device list of one is what these tests render against.
    """

    def get_devices(self) -> list[DeviceInfo]:
        """
        Report a single fake device.

        Returns:
            A one-element list naming the device the settings point at.

        """
        return [
            DeviceInfo(
                name="test:device:001",
                vendor="Test",
                model="Stub",
                device_type="virtual",
            ),
        ]


class _RecordingRefresher:
    """
    Stands in for :class:`~saneless.web.refresher.CheckRefresher` in a request.

    It counts :meth:`note_watcher` and **swallows** it, which is what makes
    these tests deterministic.  The real refresher is still built and still
    started by the lifespan, but the routes stamp *this* object, so the real
    one never learns it has a watcher and its first guard returns on every
    tick -- no background probe can land mid-assertion and fill the cache a
    cold-start test just emptied.

    :meth:`build_context` is delegated rather than faked, because the refresh
    route probes through it and the context it hands over is the real one the
    application assembled.
    """

    def __init__(self, real: CheckRefresher) -> None:
        """Wrap the refresher the app built, with no watchers recorded yet."""
        self._real = real
        self.watch_count = 0

    def note_watcher(self) -> None:
        """Record one page render that said someone is looking."""
        self.watch_count += 1

    def build_context(self) -> CheckContext:
        """
        Hand over the real refresher's context.

        Returns:
            The context one probe would run against.

        """
        return self._real.build_context()

    def probe_now(self) -> bool:
        """
        Probe through the real refresher, which owns the one probe path.

        The boolean is forwarded rather than invented.  A stub returning
        ``None`` would make the handler read every call as a collapse, and the
        collapse tests below would pass for entirely the wrong reason.

        Returns:
            Whether this call took the probe or collapsed into one in flight.

        """
        return self._real.probe_now()

    @property
    def probe_in_flight(self) -> bool:
        """
        Delegate to the real refresher's reader, which owns the one lock.

        Returns:
            Whether a probe holds the single-flight lock right now.

        """
        return self._real.probe_in_flight

    @property
    def probe_lock(self) -> threading.Lock:
        """
        The real refresher's single-flight lock, so a test can hold it.

        Returns:
            The lock one checker takes for the length of one probe.

        """
        return self._real._probe_lock


class _ProbeSpy:
    """
    A stand-in for ``run_checks`` that counts calls and records its contexts.

    Returning developer-authored rows rather than delegating keeps every test
    that uses it entirely offline; the two tests that want the real registry's
    sentences call the real route with a stubbed Paperless client instead.
    """

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.calls = 0
        self.contexts: list[CheckContext] = []
        self.gates: list[threading.Lock | None] = []

    def __call__(
        self, context: CheckContext, *, scanner_gate: threading.Lock | None = None
    ) -> tuple[CheckResult, ...]:
        """
        Record the call and return one synthetic row per check.

        Args:
            context: The context the route built for this probe.
            scanner_gate: The gate the probe handed in rather than held.

        Returns:
            One ``OK`` result per ``CheckKey`` member, in member order.

        """
        self.calls += 1
        self.contexts.append(context)
        self.gates.append(scanner_gate)
        return _synthetic_results()


def _synthetic_results() -> tuple[CheckResult, ...]:
    """
    Build one distinguishable ``OK`` row per check.

    Returns:
        One result per ``CheckKey`` member, in member order.

    """
    return tuple(
        CheckResult(
            key=key,
            state=CheckState.OK,
            message=f"{key.value} row.",
        )
        for key in CheckKey
    )


def _results_with_a_skipped_scanner() -> tuple[CheckResult, ...]:
    """
    Build the five rows a refresh during a scan produces.

    The Scanner row carries UI-SPEC S1's paused sentence and the ``skipped``
    flag; its state is ``OK`` for the reason ``_scanner_skipped``'s is, which is
    exactly why a marker derived from the state alone would render it green.

    Returns:
        One result per ``CheckKey`` member, the first of them skipped.

    """
    return tuple(
        CheckResult(
            key=key,
            state=CheckState.OK,
            message=(
                PAUSED_SCANNER_MESSAGE
                if key is CheckKey.SCANNER
                else f"{key.value} row."
            ),
            skipped=key is CheckKey.SCANNER,
        )
        for key in CheckKey
    )


def _row_named(markup: str, name: str) -> str:
    """
    Return the one rendered row whose name column is ``name``.

    Row-level assertions are made against this rather than against the whole
    page, so "the page contains a tick somewhere" can never pass for "the
    Scanner row is green".

    Args:
        markup: The rendered page or partial.
        name: The check name column to find.

    Returns:
        The inner markup of that row.

    """
    wanted = f'<span class="check-name">{name}</span>'
    for row in _CHECK_ROW.findall(markup):
        if wanted in row:
            return row
    msg = f"no {name} row in the rendered strip"
    raise AssertionError(msg)


def _make_app(
    tmp_path: Path, *, stub_refresher: bool = True, stub_connection: bool = True
) -> FastAPI:
    """
    Build a real app with a stub scanner and no network calls, not yet started.

    Args:
        tmp_path: The directory the data and temp folders live in.
        stub_refresher: Whether to put ``_RecordingRefresher`` in front of the
            real one.  True for every test but the wiring test, because a real
            refresher that has been stamped will probe on its next tick and
            fill a cache a cold-start assertion just emptied.
        stub_connection: Whether to replace ``test_connection`` with a constant
            ``CONNECTED``.  False only for the test that counts Paperless
            requests at the transport, which needs the real method to reach the
            mock transport it would otherwise step over.

    Returns:
        The app, whose lifespan (and so its worker) starts with its TestClient.

    """
    settings = Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(url="http://localhost:8000", token="test-token"),
        output=OutputConfig(tmp_dir=str(tmp_path), data_dir=str(tmp_path)),
        # Two profiles, so the worker's startup generation (D-14) does not fire
        # and swap the profile set while these requests read it.
        profiles={
            "default": ProfileConfig(),
            "duplex": ProfileConfig(source="ADF Duplex"),
        },
    )
    app = create_app(settings, _StubScanner())
    app.state.paperless.get_tags = list
    app.state.paperless.get_correspondents = list
    if stub_connection:
        # Offline, and CONNECTED so the Paperless row is not the one that
        # varies.
        app.state.paperless.test_connection = lambda timeout=None: (
            ConnectionStatus.CONNECTED
        )
    if stub_refresher:
        app.state.refresher = _RecordingRefresher(app.state.refresher)
    return app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """TestClient over a real app with a stub scanner and no network calls."""
    with TestClient(_make_app(tmp_path)) as tc:
        yield tc


class _FakeClock:
    """
    A monotonic clock the test moves by hand instead of waiting for.

    A local copy of ``tests/test_checks_cache.py``'s, rather than an import
    from another test module: ten lines of duplication costs less than a
    dependency between two test files, and nothing here may sleep -- the whole
    reason ``CheckCache`` takes its clock as a parameter is that a suite
    waiting on the wall clock to watch an interval elapse is slow and flaky.
    """

    def __init__(self, start: float = 100.0) -> None:
        """Start the clock at ``start`` seconds."""
        self.now = start

    def __call__(self) -> float:
        """Return the current fake monotonic reading."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward, the way a real one would move on its own."""
        self.now += seconds


class _PaperlessRequestCounter:
    """
    Counts the HTTP requests the Paperless check issues, at the transport.

    A probe spy counts calls into ``run_checks``; this counts what would
    really have left the appliance, which is the quantity WR-05 is about.
    """

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """
        Record the request and answer it successfully.

        Args:
            request: The request the client issued.

        Returns:
            An empty, successful page, which the check reads as CONNECTED.

        """
        self.count += 1
        return httpx.Response(200, json={"count": 0, "results": []})


# What the two fixtures below hand a test.  Named because the alternative is
# an unreadable tuple annotation repeated on every signature.
type _Clocked = tuple[TestClient, _FakeClock]
type _Counting = tuple[TestClient, _PaperlessRequestCounter]


@pytest.fixture
def clocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Clocked]:
    """
    Build a client whose check cache measures intervals on a movable clock.

    The cache is substituted at the point the app builds it, so the refresher
    and the routes share the one instance exactly as they do in production;
    replacing ``app.state.checks`` afterwards would leave the refresher holding
    the original.
    """
    clock = _FakeClock()
    monkeypatch.setattr(app_module, "CheckCache", lambda: CheckCache(clock=clock))
    with TestClient(_make_app(tmp_path)) as tc:
        yield tc, clock


@pytest.fixture
def counting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Counting]:
    """Build a client whose Paperless client answers from a counting transport."""
    counter = _PaperlessRequestCounter()

    def build_client(*, url: str, token: str, consume_dir: str = "") -> PaperlessClient:
        """
        Build the app's Paperless client over a mock transport.

        Returns:
            A client that issues no real network traffic.

        """
        return PaperlessClient(
            url=url,
            token=token,
            consume_dir=consume_dir,
            _transport=httpx.MockTransport(counter),
        )

    monkeypatch.setattr(app_module, "PaperlessClient", build_client)
    with TestClient(_make_app(tmp_path, stub_connection=False)) as tc:
        yield tc, counter


def _app(client: TestClient) -> FastAPI:
    """Extract the FastAPI app from a TestClient, helping the type checker."""
    app = client.app
    if not isinstance(app, FastAPI):
        msg = "Expected FastAPI app"
        raise TypeError(msg)
    return app


def _refresher(client: TestClient) -> _RecordingRefresher:
    """Return the recording refresher the routes stamp."""
    refresher = _app(client).state.refresher
    if not isinstance(refresher, _RecordingRefresher):
        msg = "Expected the recording refresher"
        raise TypeError(msg)
    return refresher


def _spy(monkeypatch: pytest.MonkeyPatch) -> _ProbeSpy:
    """
    Replace the refresher module's ``run_checks`` with a counting stand-in.

    The refresher module's reference is the one patched, because the refresh
    handler no longer runs the registry itself: it goes through
    ``CheckRefresher.probe_now``, which is the single probe implementation both
    the button and the background thread share.

    Returns:
        The spy, whose ``calls`` is the number of probes the request made.

    """
    spy = _ProbeSpy()
    monkeypatch.setattr(refresher_module, "run_checks", spy)
    return spy


def _warm_the_cache(client: TestClient) -> None:
    """Store a set of results directly, so the cache is no longer cold."""
    _app(client).state.checks.store(_synthetic_results())


def _adopt_as_current_job(client: TestClient, job_id: str) -> None:
    """
    Point the live worker at `job_id` without submitting real work.

    This writes ``ScanWorker._current_job_id`` directly, which is private.  It
    is the single place in this module that does so, deliberately, and it is
    the same reach-through ``tests/test_web_state_rendering.py`` documents: the
    routes read "is a scan running" through the worker, and driving it through
    the real submit path would run a pipeline these tests do not want.
    """
    _app(client).state.worker._current_job_id = job_id


def _start_a_scan(client: TestClient) -> str:
    """
    Create a job row and make it the worker's current job.

    Returns:
        The id of the job now reported as running.

    """
    job = _app(client).state.job_store.create_job(profile="default", title="Paused")
    _adopt_as_current_job(client, job.id)
    return job.id


def _body_attrs(markup: str) -> str:
    """
    Return the attribute text of the single ``#checks-body`` element.

    Returns:
        Everything between ``<div id="checks-body"`` and its closing ``>``.

    """
    match = _CHECKS_BODY.search(markup)
    assert match is not None, markup
    return match.group("attrs")


# The text an injected failure carries.  Deliberately unlike anything the strip
# can render, so "absent from the body" means the guard held rather than that
# the marker happened not to collide with the markup.
_CHECKS_BOOM_MARKER = "zz-checks-boom"


def _raise_inside_the_checks_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """
    Make one half of the checks route's guarded region raise.

    Args:
        client: The client whose application is patched.
        monkeypatch: The patcher, so the injected failure is undone after.
        target: ``_checks_context`` for the context build, ``note_watcher``
            for the watcher stamp that precedes it inside the same guard.

    """

    def boom(*_args: object, **_kwargs: object) -> None:
        """Fail the way an unexpected bug in this route would."""
        raise RuntimeError(_CHECKS_BOOM_MARKER)

    if target == "_checks_context":
        monkeypatch.setattr(routes_module, "_checks_context", boom)
    else:
        monkeypatch.setattr(_app(client).state.refresher, target, boom)


# How far a poll chain is followed before it is called unbounded.  Comfortably
# past any cap the strip could sanely carry, so a chain that reaches this many
# links has not been capped, it has been left running.
_POLL_CHAIN_LIMIT = 60


def _poll_target(markup: str) -> str | None:
    """
    Return the URL this body's poll would request next, or ``None`` if it has none.

    Reads the rendered attributes rather than the template source, so a poll
    that a comment describes but the markup does not emit cannot satisfy it.

    Args:
        markup: A rendered response carrying exactly one ``#checks-body``.

    Returns:
        The ``hx-get`` URL, unescaped, or ``None`` when no trigger is emitted.

    """
    attrs = _body_attrs(markup)
    if "hx-trigger" not in attrs:
        return None
    match = _HX_GET.search(attrs)
    assert match is not None, attrs
    return unescape(match.group("url"))


def _follow_the_poll(client: TestClient, limit: int) -> list[str]:
    """
    Walk the cold-start poll from one body to the next, the way a browser does.

    Each response names the URL the next request goes to, so following the
    chain is the only faithful way to ask "how many times can this strip be
    made to ask".  Re-requesting the bare route would answer a different
    question: the route is stateless, so a bare request is always the *first*
    link of a fresh chain and would look unbounded forever.

    Args:
        client: The client to request through.
        limit: The most links to follow before giving up on the chain ending.

    Returns:
        The URLs requested, in order.  Shorter than ``limit`` only if a
        response carried no trigger, which is the chain ending on its own.

    """
    requested: list[str] = []
    url = "/api/checks"
    while len(requested) < limit:
        requested.append(url)
        response = client.get(url)
        assert response.status_code == 200, (url, response.status_code)
        following = _poll_target(response.text)
        if following is None:
            break
        url = following
    return requested


class TestNoProbeInARequest:
    """D-04: rendering reads the cache; only Refresh is allowed to probe."""

    def test_index_renders_without_probing(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``GET /`` is a cache read, so an unplugged scanner costs it nothing."""
        spy = _spy(monkeypatch)
        response = client.get("/")
        assert response.status_code == 200
        assert spy.calls == 0

    def test_checks_route_renders_without_probing(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``GET /api/checks`` is a cache read too, not a probe."""
        spy = _spy(monkeypatch)
        assert client.get("/api/checks").status_code == 200
        assert spy.calls == 0

    def test_index_renders_the_cached_rows(self, client: TestClient) -> None:
        """A warm cache renders one real row per check on the page itself."""
        _warm_the_cache(client)
        markup = client.get("/").text
        assert len(_CHECK_ROW.findall(markup)) == len(CheckKey)
        assert "SCANNER row." in markup


class TestWatcherStamping:
    """D-05: the refresher only works while a page says someone is looking."""

    def test_index_stamps_the_watcher(self, client: TestClient) -> None:
        """Without this stamp the lazy refresher never probes at all."""
        before = _refresher(client).watch_count
        client.get("/")
        assert _refresher(client).watch_count == before + 1

    def test_checks_route_stamps_the_watcher(self, client: TestClient) -> None:
        """The strip route keeps the window open while a tab is left open."""
        before = _refresher(client).watch_count
        client.get("/api/checks")
        assert _refresher(client).watch_count == before + 1

    def test_refresh_stamps_the_watcher(self, client: TestClient) -> None:
        """An explicit click is the clearest evidence of a watcher there is."""
        before = _refresher(client).watch_count
        client.post("/api/checks/refresh")
        assert _refresher(client).watch_count == before + 1

    def test_the_real_refresher_is_the_one_the_index_stamps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The wiring holds end to end, with no stand-in in the way.

        Every other test here replaces ``app.state.refresher``, so none of them
        would notice if the routes stamped something the lifespan does not
        start.  ``_last_watched`` is private and read deliberately: it is the
        only observable of ``note_watcher`` short of waiting out a tick.
        """
        monkeypatch.setattr(
            "saneless.web.refresher.run_checks", lambda _context: _synthetic_results()
        )
        app = _make_app(tmp_path, stub_refresher=False)
        refresher = app.state.refresher
        with TestClient(app) as tc:
            assert refresher._last_watched is None
            tc.get("/")
            assert refresher._last_watched is not None


class TestColdStart:
    """D-06: five ``Checking…`` rows and a poll that ends itself."""

    def test_cold_start_renders_one_checking_row_per_check(
        self, client: TestClient
    ) -> None:
        """All five names are on the page before any probe has happened."""
        markup = client.get("/api/checks").text
        rows = _CHECK_ROW.findall(markup)
        assert len(rows) == len(CheckKey)
        assert all(CHECKING_MESSAGE in row for row in rows)

    def test_cold_start_body_polls_and_is_busy(self, client: TestClient) -> None:
        """The cold body carries the trigger and announces itself busy."""
        attrs = _body_attrs(client.get("/api/checks").text)
        assert 'aria-busy="true"' in attrs
        assert "every 2s" in attrs
        # The URL carries the attempt number the cap is counted against, so the
        # spelling here is the whole first link of the chain, not the bare route.
        assert 'hx-get="/api/checks?attempt=1"' in attrs

    def test_a_body_with_results_does_not_poll(self, client: TestClient) -> None:
        """The swapped-in replacement has no trigger, so the poll stops (D-06)."""
        _warm_the_cache(client)
        attrs = _body_attrs(client.get("/api/checks").text)
        assert "hx-trigger" not in attrs
        assert "aria-busy" not in attrs

    def test_the_index_never_polls_once_results_exist(self, client: TestClient) -> None:
        """A warm page load carries no steady-state poll either (T-30-48)."""
        _warm_the_cache(client)
        assert "hx-trigger" not in _body_attrs(client.get("/").text)


class TestColdStartPollChain:
    """
    IN-07: how many times a cold strip can be made to ask (the before-state).

    ``TestColdStart`` above asserts that *a* cold body polls and that *a* warm
    body does not.  Neither says anything about the case IN-07 is about: a
    cache that is never filled, where the only terminating condition the strip
    has can never fire.  These follow the chain instead of looking at one link
    of it, which is what makes "does this ever stop" a question the suite can
    answer.
    """

    def test_the_cold_poll_chain_ends_at_the_cap(
        self, client: TestClient, record_property: Callable[[str, object], None]
    ) -> None:
        """
        Followed link by link, the cold poll runs out of attempts and stops.

        The chain is one longer than the cap because its first link is the bare
        route -- attempt 0, the body a page render already carries -- and the
        attempts it then walks are 1 through the cap.  A browser therefore
        issues exactly ``POLL_ATTEMPT_CAP`` requests; this loop, which starts by
        asking for the body the page would have been served, issues one more.
        """
        followed = _follow_the_poll(client, _POLL_CHAIN_LIMIT)
        record_property("cold_poll_chain_length", len(followed))
        record_property("cold_poll_attempt_cap", checks_module.POLL_ATTEMPT_CAP)
        assert len(followed) == checks_module.POLL_ATTEMPT_CAP + 1, followed
        assert followed[-1].endswith(f"attempt={checks_module.POLL_ATTEMPT_CAP}")

    def test_results_end_the_poll_chain_at_its_first_link(
        self, client: TestClient
    ) -> None:
        """
        A cache with results ends the chain immediately, which is D-06 unchanged.

        Stated as a chain rather than as one attribute so that it stays the
        *primary* terminating condition: whatever else is added, results
        arriving must still stop the poll on the very next response.
        """
        _warm_the_cache(client)
        assert _follow_the_poll(client, _POLL_CHAIN_LIMIT) == ["/api/checks"]


class TestBoundedPoll:
    """
    IN-07: the cold-start poll's second ending, and what it leaves behind.

    Until now the poll had exactly one terminating condition -- results landing
    in the cache -- so an appliance whose refresher thread had died left every
    open tab asking forever.  Measured in Chromium before this cap existed,
    "forever" was 42 requests a second, not the one every two seconds the
    markup claimed: htmx fires ``load`` on content it has just swapped in, and
    this body swaps in a copy of itself carrying ``load, every 2s``.

    So the fix is two things and both are pinned here.  The server counts the
    attempts and stops emitting the trigger, and the polling body no longer
    carries ``load`` -- the interval in the markup is now the interval the
    browser uses.  Results arriving stays the *primary* ending; the cap is the
    one that fires when nothing ever arrives.
    """

    def test_a_poll_below_the_cap_names_the_next_attempt(
        self, client: TestClient
    ) -> None:
        """Each cold body asks for the one after it, so the count can advance."""
        cap = checks_module.POLL_ATTEMPT_CAP
        for attempt in (0, 1, cap - 1):
            attrs = unescape(
                _body_attrs(client.get(f"/api/checks?attempt={attempt}").text)
            )
            assert f"/api/checks?attempt={attempt + 1}" in attrs, (attempt, attrs)

    def test_the_capped_body_carries_no_request_attribute_and_the_first_one_does(
        self, client: TestClient
    ) -> None:
        """
        At the cap the swap target is inert; at attempt 0 it is not (T-30-27-04).

        Asserted against the attributes the server really rendered rather than
        against the template source, because a conditional a comment describes
        and the markup does not honour would satisfy a source grep and leave
        the loop running.  Both halves live in one test on purpose: an
        assertion that something is absent is worth nothing without the paired
        assertion that the same reading finds it when it is present.

        ``hx-`` and not merely ``hx-trigger``: an ``outerHTML`` swap target
        that carries any unconditional htmx request attribute re-arms itself,
        which is the trap this file's template comment exists to prevent.
        """
        first = _body_attrs(client.get("/api/checks?attempt=0").text)
        assert "hx-get" in first
        assert "hx-trigger" in first
        assert 'aria-busy="true"' in first

        capped = _body_attrs(
            client.get(f"/api/checks?attempt={checks_module.POLL_ATTEMPT_CAP}").text
        )
        assert "hx-" not in capped, capped
        assert "aria-busy" not in capped, capped

    def test_only_the_first_body_asks_the_browser_to_load_at_once(
        self, client: TestClient
    ) -> None:
        """
        ``load`` rides only on the body a page first parses, never on a polled one.

        This is the half of the fix the attempt counter alone would not give.
        htmx re-fires ``load`` for content it has swapped in, so a polled body
        carrying it requests again the moment it arrives -- measured at 42
        requests a second against the 0.5 the markup advertises.  With the cap
        in place that would spend every attempt in a quarter of a second, and
        the legitimate cold start would be cut off before the refresher's first
        tick.
        """
        first = _body_attrs(client.get("/api/checks?attempt=0").text)
        assert "load," in first
        assert "every 2s" in first

        polled = _body_attrs(client.get("/api/checks?attempt=1").text)
        assert "load" not in polled, polled
        assert "every 2s" in polled

    def test_the_capped_body_still_shows_the_rows_and_the_button(
        self, client: TestClient
    ) -> None:
        """
        Giving up is not going blank: the way forward is still on the page.

        A strip that stopped polling and also lost its rows or its button would
        have turned a dead background thread into a dead page.
        """
        markup = client.get(
            f"/api/checks?attempt={checks_module.POLL_ATTEMPT_CAP}"
        ).text
        rows = _CHECK_ROW.findall(markup)
        assert len(rows) == len(CheckKey)
        assert all(CHECKING_MESSAGE in row for row in rows)
        assert 'hx-post="/api/checks/refresh"' in markup
        assert "Check again" in markup

    def test_the_capped_body_says_the_checks_have_not_run_yet(
        self, client: TestClient
    ) -> None:
        """
        The meta line becomes the give-up sentence, and it comes from ``checks``.

        Compared against the constant rather than against a quoted string, so
        the template still owns no vocabulary: a sentence written into the
        markup instead would fail here even if it read identically.
        """
        markup = client.get(
            f"/api/checks?attempt={checks_module.POLL_ATTEMPT_CAP}"
        ).text
        meta = _CHECK_META.search(markup)
        assert meta is not None, markup
        assert meta.group("text").strip() == checks_module.POLL_GAVE_UP_LINE

    def test_results_end_the_poll_whatever_the_attempt_claims(
        self, client: TestClient
    ) -> None:
        """
        A warm cache carries no trigger at any attempt: D-06 is still primary.

        The cap is a second ending, not a replacement for the first one, and a
        change that made the attempt number decide instead of the cache would
        pass every other test in this class.
        """
        _warm_the_cache(client)
        for attempt in (0, 3, checks_module.POLL_ATTEMPT_CAP):
            attrs = _body_attrs(client.get(f"/api/checks?attempt={attempt}").text)
            assert "hx-trigger" not in attrs, (attempt, attrs)
            meta = _CHECK_META.search(client.get(f"/api/checks?attempt={attempt}").text)
            assert meta is not None
            assert meta.group("text").strip() != checks_module.POLL_GAVE_UP_LINE

    def test_an_attempt_outside_the_bound_ends_the_chain_instead_of_erroring(
        self, client: TestClient
    ) -> None:
        """
        An out-of-range ``attempt`` is a trigger-free 200, not a 422 (R3-CR-02).

        The bound used to be a ``Query`` constraint, so a number past the cap
        was rejected during request validation and came back through
        ``render_error``.  That was the wrong failure mode for the one element
        on the page that polls: an error response is the thing the strip cannot
        usefully receive, and the ordinary way past the cap is exactly the
        ending the strip wanted anyway.  Above the cap the clamp lands on the
        cap, which is the give-up body; the chain stops there.
        """
        cap = checks_module.POLL_ATTEMPT_CAP
        response = client.get(f"/api/checks?attempt={cap + 1}")
        assert response.status_code == 200
        assert "hx-trigger" not in _body_attrs(response.text)

        far_past = client.get("/api/checks?attempt=999999")
        assert far_past.status_code == 200
        assert "hx-trigger" not in _body_attrs(far_past.text)

    def test_a_negative_attempt_is_the_start_of_a_chain(
        self, client: TestClient
    ) -> None:
        """
        Below zero clamps to zero: a fresh chain, not a status code.

        A negative counter says nothing the server needs to act on -- there is
        no state behind it -- so the honest reading is "this browser has not
        asked yet", which is what attempt zero means everywhere else.
        """
        response = client.get("/api/checks?attempt=-5")
        assert response.status_code == 200
        assert "/api/checks?attempt=1" in unescape(_body_attrs(response.text))

    def test_a_crafted_attempt_never_reaches_the_context_or_the_body(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The clamp runs first, so only clamped numbers travel (T-30-32-02).

        This is the property the removed ``Query`` bound was defending, and it
        is unchanged: what changed is the status code an out-of-range value
        earns, never whether the value itself is trusted.
        """
        cap = checks_module.POLL_ATTEMPT_CAP
        seen: list[int] = []
        real = routes_module._checks_context

        def recording(state: State, *, attempt: int = 0) -> dict[str, object]:
            """Record the attempt the handler passed on, then build normally."""
            seen.append(attempt)
            return real(state, attempt=attempt)

        monkeypatch.setattr(routes_module, "_checks_context", recording)
        for crafted in ("99", "-5", str(cap + 1)):
            body = client.get(f"/api/checks?attempt={crafted}").text
            assert crafted not in unescape(_body_attrs(body)), (crafted, body)
        assert seen == [cap, 0, cap]

    @pytest.mark.parametrize("crafted", ["abc", "nine", "1e9", ""])
    def test_an_attempt_that_is_not_an_integer_is_still_a_422(
        self, client: TestClient, crafted: str
    ) -> None:
        """
        The clamp replaces a range bound, never the type (T-30-32-02).

        A value that is not a number at all is still refused by request
        validation, and with the strip's retarget exemption in place that 422
        replaces the strip and ends the poll rather than being redirected into
        the message slot.
        """
        assert client.get(f"/api/checks?attempt={crafted}").status_code == 422

    def test_the_full_page_starts_the_poll_at_its_first_attempt(
        self, client: TestClient
    ) -> None:
        """A page load begins a fresh chain, not one inherited from anywhere."""
        attrs = unescape(_body_attrs(client.get("/").text))
        assert "/api/checks?attempt=1" in attrs
        assert "load," in attrs

    def test_the_out_of_band_strip_starts_the_poll_at_its_first_attempt(
        self, client: TestClient
    ) -> None:
        """
        The strip a scan submit carries out of band starts counting from zero too.

        It is newly parsed content, so it is the one other place ``load``
        belongs, and a scan submit must not hand the browser a chain that is
        already half spent.
        """
        response = client.post(
            "/api/scan", data={"profile": "default", "title": "Poll start"}
        )
        assert response.status_code == 200
        attrs = unescape(_body_attrs(response.text))
        assert 'hx-swap-oob="true"' in attrs
        assert "/api/checks?attempt=1" in attrs
        assert "load," in attrs

    def test_check_again_after_the_cap_still_probes_and_still_renders(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The button the give-up state points at really is still the way forward.

        Asserted after the capped body has been served, because the cap is
        server-side arithmetic over a query parameter and a refresh that had
        been made to inherit it would be a button that did nothing on the one
        page that needs it.
        """
        spy = _spy(monkeypatch)
        capped = client.get(f"/api/checks?attempt={checks_module.POLL_ATTEMPT_CAP}")
        assert capped.status_code == 200

        response = client.post("/api/checks/refresh")
        assert response.status_code == 200
        assert spy.calls == 1
        assert len(_CHECK_ROW.findall(response.text)) == len(CheckKey)
        # Results landed, so the strip is settled rather than polling again.
        assert "hx-trigger" not in _body_attrs(response.text)


class TestTheStripSurvivesItsOwnFailure:
    """
    An internal failure in the strip's render is a cold strip, not an error.

    The strip is the one element that polls, so an error response to it is the
    one response it cannot usefully receive: before R3-CR-02 an exception in
    ``_checks_context`` became a 500 that was retargeted into the message slot,
    leaving ``#checks-body`` on the page still polling, still failing, still
    overwriting the scan-progress line every two seconds.

    So this route catches its own failures and renders the cold-start body
    instead: five named rows, the give-up line naming the button that is still
    on the page, and no trigger.  The exception goes to the log and nowhere
    else -- this body is rendered on a page the whole LAN can read (ASVS V7,
    Phase 26 D-10, T-30-32-03).
    """

    @pytest.mark.parametrize("target", ["_checks_context", "note_watcher"])
    def test_a_failure_inside_the_render_is_a_cold_strip_at_200(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, target: str
    ) -> None:
        """Both halves of the guarded region fall back to the same body."""
        _raise_inside_the_checks_route(client, monkeypatch, target)

        response = client.get("/api/checks")
        assert response.status_code == 200
        rows = _CHECK_ROW.findall(response.text)
        assert len(rows) == len(CheckKey)
        assert all(CHECKING_MESSAGE in row for row in rows)
        meta = _CHECK_META.search(response.text)
        assert meta is not None, response.text
        assert meta.group("text").strip() == checks_module.POLL_GAVE_UP_LINE
        assert 'hx-post="/api/checks/refresh"' in response.text
        assert "Check again" in response.text
        assert "hx-trigger" not in _body_attrs(response.text)

    @pytest.mark.parametrize("target", ["_checks_context", "note_watcher"])
    def test_no_part_of_the_exception_reaches_the_page(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, target: str
    ) -> None:
        """The failed render says nothing about why (ASVS V7, T-30-32-03)."""
        _raise_inside_the_checks_route(client, monkeypatch, target)

        body = client.get("/api/checks").text
        for forbidden in (_CHECKS_BOOM_MARKER, "Traceback", "RuntimeError", ".py"):
            assert forbidden not in body, (forbidden, body)

    def test_the_failure_is_logged_with_its_traceback(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Nothing is swallowed: the page loses the detail, the log keeps it."""
        _raise_inside_the_checks_route(client, monkeypatch, "_checks_context")

        with caplog.at_level(logging.ERROR, logger=routes_module.__name__):
            assert client.get("/api/checks").status_code == 200
        assert any(
            record.exc_info is not None and _CHECKS_BOOM_MARKER in str(record.exc_info)
            for record in caplog.records
        )


class TestRefreshButton:
    """D-09: the button re-probes, and cannot defeat the exclusive scanner."""

    def test_refresh_probes_once(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One click is one run of the registry, not zero and not two."""
        spy = _spy(monkeypatch)
        assert client.post("/api/checks/refresh").status_code == 200
        assert spy.calls == 1

    def test_refresh_bypasses_a_fresh_cache(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The button is not a no-op while the cache is still fresh.

        This is the whole of the button: the refresher's own tick returns early
        on a fresh cache, so a Refresh routed through the refresher's policy
        would do nothing at all for the first thirty seconds after a page load
        -- which is exactly when a household member presses it.
        """
        _warm_the_cache(client)
        assert _app(client).state.checks.is_fresh()
        spy = _spy(monkeypatch)
        client.post("/api/checks/refresh")
        assert spy.calls == 1

    def test_refresh_stores_what_it_probed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The result lands in the cache, so the next render is warm."""
        _spy(monkeypatch)
        assert _app(client).state.checks.current().results is None
        client.post("/api/checks/refresh")
        assert _app(client).state.checks.current().results == _synthetic_results()

    def test_refresh_returns_a_body_that_does_not_poll(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The response carries results, so it ends any cold-start poll."""
        _spy(monkeypatch)
        attrs = _body_attrs(client.post("/api/checks/refresh").text)
        assert "hx-trigger" not in attrs

    def test_refresh_during_a_scan_skips_the_scanner(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit click does not get to enter SANE behind a live scan."""
        spy = _spy(monkeypatch)
        _start_a_scan(client)
        client.post("/api/checks/refresh")
        assert spy.calls == 1
        assert spy.contexts[0].skip_scanner is True

    def test_refresh_when_idle_does_not_skip_the_scanner(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no job in flight the scanner row is probed like any other."""
        spy = _spy(monkeypatch)
        client.post("/api/checks/refresh")
        assert spy.contexts[0].skip_scanner is False

    def test_refresh_hands_the_scanner_gate_to_the_registry(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WR-03: the gate reaches ``run_checks``, and the handler holds none."""
        spy = _spy(monkeypatch)
        client.post("/api/checks/refresh")
        assert spy.gates == [_app(client).state.worker.scanner_gate]

    def test_refresh_during_a_scan_renders_the_paused_row(
        self, client: TestClient
    ) -> None:
        """
        The registry's own paused sentence reaches the page (D-08).

        No spy here: this drives the real ``run_checks`` so the sentence in the
        markup is the one ``saneless doctor`` would print, not one this test
        made up.  Every probe it runs is local -- the stub scanner, a stubbed
        Paperless client and two temp directories.
        """
        _start_a_scan(client)
        markup = client.post("/api/checks/refresh").text
        assert PAUSED_SCANNER_MESSAGE in markup

    def test_a_refresh_landing_mid_probe_renders_no_paused_row(
        self, client: TestClient
    ) -> None:
        """
        WR-04: checker contention on an idle appliance is not "a scan".

        A click landing while a refresher tick is in flight used to fail its
        non-blocking attempt on the scanner gate, and the handler read that
        failure as a running scan -- so the strip rendered "not checked while a
        scan is running" beside "Last checked 14:02" on an appliance with no
        job at all.  The probe lock now turns the second checker away before it
        reaches the gate, and the strip re-renders what the first one found.
        """
        _warm_the_cache(client)
        assert _app(client).state.worker.current_job_id is None
        lock = _refresher(client).probe_lock
        assert lock.acquire(blocking=False) is True
        try:
            markup = client.post("/api/checks/refresh").text
        finally:
            lock.release()
        assert PAUSED_SCANNER_MESSAGE not in markup
        assert PAUSED_PREFIX not in markup

    def test_a_refresh_landing_mid_probe_stores_nothing_new(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Concurrent refreshes collapse to one probe, not one probe each."""
        spy = _spy(monkeypatch)
        lock = _refresher(client).probe_lock
        assert lock.acquire(blocking=False) is True
        try:
            assert client.post("/api/checks/refresh").status_code == 200
        finally:
            lock.release()
        assert spy.calls == 0


class TestRefreshMinimumInterval:
    """WR-05: the one handler allowed to probe has a floor under it."""

    def test_the_first_refresh_probes(
        self, clocked: _Clocked, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A floor is not a wall: the first click always gets its probe."""
        client, _clock = clocked
        spy = _spy(monkeypatch)
        assert client.post("/api/checks/refresh").status_code == 200
        assert spy.calls == 1

    def test_an_immediate_second_refresh_does_not_probe(
        self, clocked: _Clocked, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two clicks inside the interval are one Paperless request."""
        client, _clock = clocked
        spy = _spy(monkeypatch)
        client.post("/api/checks/refresh")
        client.post("/api/checks/refresh")
        assert spy.calls == 1

    def test_a_refused_refresh_is_a_200_carrying_the_strip(
        self, clocked: _Clocked, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The refusal is invisible: same status, same partial, no error.

        The click is not wrong, only early, so there is nothing to tell the
        household member about -- and UI-SPEC S1 has no row for an error here.
        """
        client, _clock = clocked
        _spy(monkeypatch)
        client.post("/api/checks/refresh")
        refused = client.post("/api/checks/refresh")
        assert refused.status_code == 200
        assert len(_CHECK_ROW.findall(refused.text)) == len(CheckKey)
        assert "hx-trigger" not in _body_attrs(refused.text)
        assert "check-refresh" in refused.text

    def test_a_refused_refresh_renders_what_a_cache_read_would(
        self, clocked: _Clocked, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Byte for byte the strip as it stands: nothing a user could notice."""
        client, _clock = clocked
        _spy(monkeypatch)
        client.post("/api/checks/refresh")
        refused = client.post("/api/checks/refresh")
        assert refused.text == client.get("/api/checks").text

    def test_twenty_refreshes_in_a_loop_cost_one_probe(
        self, clocked: _Clocked, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The review's amplifier, shut: 20 requests, one run of the registry."""
        client, _clock = clocked
        spy = _spy(monkeypatch)
        for _ in range(20):
            assert client.post("/api/checks/refresh").status_code == 200
        assert spy.calls == 1

    def test_twenty_refreshes_issue_one_paperless_request(
        self, counting: _Counting
    ) -> None:
        """
        Counted where it costs something: at the wire, not at the spy.

        No spy here -- this drives the real registry against a Paperless
        client whose transport records every request, so the assertion is
        about traffic that would really have left the appliance.
        """
        client, counter = counting
        before = counter.count
        for _ in range(20):
            assert client.post("/api/checks/refresh").status_code == 200
        assert counter.count - before == 1

    def test_a_refresh_after_the_interval_probes_again(
        self, clocked: _Clocked, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D-09 survives: nobody has to wait out a TTL, only a moment."""
        client, clock = clocked
        spy = _spy(monkeypatch)
        client.post("/api/checks/refresh")
        clock.advance(3.0)
        client.post("/api/checks/refresh")
        assert spy.calls == 2

    def test_a_refused_refresh_still_stamps_the_watcher(
        self, clocked: _Clocked, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clicking repeatedly is watching, so the lazy refresher stays awake."""
        client, _clock = clocked
        _spy(monkeypatch)
        before = _refresher(client).watch_count
        client.post("/api/checks/refresh")
        client.post("/api/checks/refresh")
        assert _refresher(client).watch_count == before + 2

    def test_the_read_only_route_neither_claims_nor_probes(
        self, clocked: _Clocked, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``GET /api/checks`` is untouched: it takes no claim from the click."""
        client, _clock = clocked
        spy = _spy(monkeypatch)
        for _ in range(5):
            assert client.get("/api/checks").status_code == 200
        assert spy.calls == 0
        assert client.post("/api/checks/refresh").status_code == 200
        assert spy.calls == 1


class TestCollapsedRefreshStillDelivers:
    """
    WR-03: the one manual control must always put an answer on the page.

    ``POST /api/checks/refresh`` did three things in sequence and they composed
    badly.  A click landing while a background probe held the lock collapsed,
    rendered the cache *as it stood* -- the pre-probe entry, because the probe
    in flight had not stored yet -- and, because results already existed,
    carried no trigger.  Nothing on the page was ever going to pick up the
    result that probe landed a second later.  And the claim had already been
    stamped before the collapse was discovered, so the next click inside two
    seconds was refused as well: the button visibly doing nothing, twice.

    The window is one background probe's duration out of every TTL, and the
    Paperless read budget alone is five seconds, so it is not a narrow one.
    """

    def test_a_collapsed_refresh_asks_the_page_to_come_back_for_the_result(
        self, client: TestClient
    ) -> None:
        """
        The review's own case: the collapsed body carries a trigger.

        Asserted against the rendered attributes, not the template source: a
        conditional a comment describes and the markup does not honour would
        satisfy a source grep and still leave the answer undelivered.
        """
        _warm_the_cache(client)
        lock = _refresher(client).probe_lock
        assert lock.acquire(blocking=False) is True
        try:
            attrs = _body_attrs(client.post("/api/checks/refresh").text)
        finally:
            lock.release()
        assert 'hx-get="/api/checks' in attrs
        assert "every 2s" in attrs

    def test_a_collapsed_refresh_does_not_spend_the_manual_floor(
        self, clocked: _Clocked, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Two collapsed clicks cost no claim, so the next real one is honoured.

        The clock never moves, so all three clicks land inside the 2 s floor.
        Before the release existed, the first collapse stamped and the third
        click -- the one with the lock free and a probe genuinely available --
        was refused: ``spy.calls`` would be 0.
        """
        client, _clock = clocked
        spy = _spy(monkeypatch)
        lock = _refresher(client).probe_lock
        assert lock.acquire(blocking=False) is True
        try:
            assert client.post("/api/checks/refresh").status_code == 200
            assert client.post("/api/checks/refresh").status_code == 200
        finally:
            lock.release()
        assert spy.calls == 0
        assert client.post("/api/checks/refresh").status_code == 200
        assert spy.calls == 1

    def test_both_collapsed_refreshes_ask_for_the_result(
        self, clocked: _Clocked, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second collapsed click is honoured and asks again, as the first did."""
        client, _clock = clocked
        _spy(monkeypatch)
        _warm_the_cache(client)
        lock = _refresher(client).probe_lock
        assert lock.acquire(blocking=False) is True
        try:
            first = client.post("/api/checks/refresh").text
            second = client.post("/api/checks/refresh").text
        finally:
            lock.release()
        assert 'hx-get="/api/checks' in _body_attrs(first)
        assert _body_attrs(second) == _body_attrs(first)

    def test_an_honoured_refresh_with_nothing_in_flight_carries_no_trigger(
        self, clocked: _Clocked, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The uncollided click is unchanged: same status, same partial, no poll.

        This is the property 30-27 established and the reason the new disjunct
        is a disjunct rather than a replacement.  By the time the handler
        renders, its own probe has released the lock and stored, so there is
        nothing left to wait for.
        """
        client, _clock = clocked
        spy = _spy(monkeypatch)
        response = client.post("/api/checks/refresh")
        assert response.status_code == 200
        assert spy.calls == 1
        assert "hx-" not in _body_attrs(response.text)
        assert len(_CHECK_ROW.findall(response.text)) == len(CheckKey)

    def test_the_strip_asks_again_while_a_probe_is_in_flight(
        self, client: TestClient
    ) -> None:
        """``GET /api/checks`` mid-probe names the next attempt, results or not."""
        _warm_the_cache(client)
        lock = _refresher(client).probe_lock
        assert lock.acquire(blocking=False) is True
        try:
            attrs = unescape(_body_attrs(client.get("/api/checks?attempt=3").text))
        finally:
            lock.release()
        assert "/api/checks?attempt=4" in attrs

    def test_the_settling_poll_stops_at_the_same_cap_the_cold_start_uses(
        self, client: TestClient
    ) -> None:
        """
        T-30-29-02: a probe wedged in a getaddrinfo cannot make a tab ask forever.

        The settling poll rides the same ``attempt`` parameter, already bounded
        at the route by ``Query(le=POLL_ATTEMPT_CAP)``, so it ends where the
        cold start's does.
        """
        _warm_the_cache(client)
        lock = _refresher(client).probe_lock
        assert lock.acquire(blocking=False) is True
        try:
            markup = client.get(
                f"/api/checks?attempt={checks_module.POLL_ATTEMPT_CAP}"
            ).text
        finally:
            lock.release()
        assert "hx-" not in _body_attrs(markup)

    def test_a_settling_poll_that_runs_out_keeps_its_last_checked_line(
        self, client: TestClient
    ) -> None:
        """
        The give-up line stays cold-start-only: there are results, and they hold.

        ``POLL_GAVE_UP_LINE`` says the checks have not run yet.  Printing it
        beside five rows that did run would be a lie the strip tells about
        itself, so ``gave_up`` still requires an empty cache.
        """
        _warm_the_cache(client)
        lock = _refresher(client).probe_lock
        assert lock.acquire(blocking=False) is True
        try:
            markup = client.get(
                f"/api/checks?attempt={checks_module.POLL_ATTEMPT_CAP}"
            ).text
        finally:
            lock.release()
        match = _CHECK_META.search(markup)
        assert match is not None, markup
        meta = match.group("text").strip()
        assert meta != checks_module.POLL_GAVE_UP_LINE
        assert meta.startswith("Last checked ")

    def test_the_settling_poll_chain_is_bounded_even_with_a_probe_stuck(
        self, client: TestClient, record_property: Callable[[str, object], None]
    ) -> None:
        """
        Followed link by link with the lock never released, the chain still ends.

        One longer than the cap, for the same reason the cold chain is: its
        first link is the bare route, the body a page render already carries.
        """
        _warm_the_cache(client)
        lock = _refresher(client).probe_lock
        assert lock.acquire(blocking=False) is True
        try:
            followed = _follow_the_poll(client, _POLL_CHAIN_LIMIT)
        finally:
            lock.release()
        record_property("settling_poll_chain_length", len(followed))
        assert len(followed) == checks_module.POLL_ATTEMPT_CAP + 1, followed

    def test_results_and_an_idle_probe_still_carry_no_trigger(
        self, client: TestClient
    ) -> None:
        """The strip asks only while something in flight will change the answer."""
        _warm_the_cache(client)
        assert _refresher(client).probe_in_flight is False
        assert "hx-" not in _body_attrs(client.get("/api/checks").text)
        assert "hx-" not in _body_attrs(client.get("/").text)


class TestFreshnessLine:
    """UI-SPEC S1 § Freshness line: four situations, four exact sentences."""

    def _meta(self, markup: str) -> str:
        """
        Return the freshness paragraph's text.

        Returns:
            The contents of the single ``.check-meta`` paragraph.

        """
        match = _CHECK_META.search(markup)
        assert match is not None, markup
        return match.group("text").strip()

    def test_cold_and_idle(self, client: TestClient) -> None:
        """Nothing has been checked and nothing is in the way."""
        assert self._meta(client.get("/api/checks").text) == CHECKING_MESSAGE

    def test_cold_and_scanning(self, client: TestClient) -> None:
        """A cold cache during a scan says so rather than claiming a time."""
        _start_a_scan(client)
        assert (
            self._meta(client.get("/api/checks").text)
            == f"{PAUSED_PREFIX}not checked yet."
        )

    def test_results_and_idle(self, client: TestClient) -> None:
        """The timestamp is rendered through the shared ``local_time`` filter."""
        _warm_the_cache(client)
        checked_at = _app(client).state.checks.current().checked_at
        assert checked_at is not None
        assert (
            self._meta(client.get("/api/checks").text)
            == f"Last checked {local_time(checked_at)}."
        )

    def test_results_and_scanning(self, client: TestClient) -> None:
        """D-08's specimen, em dash and all."""
        _warm_the_cache(client)
        _start_a_scan(client)
        checked_at = _app(client).state.checks.current().checked_at
        assert checked_at is not None
        assert (
            self._meta(client.get("/api/checks").text)
            == f"{PAUSED_PREFIX}last checked {local_time(checked_at)}."
        )


class TestRouteShape:
    """The structural promises the handlers themselves have to keep."""

    @pytest.mark.parametrize("name", ["get_checks", "refresh_checks"])
    def test_handlers_are_plain_defs(self, name: str) -> None:
        """
        Both handlers block, so both must run on FastAPI's threadpool.

        An ``async def`` here would run a socket probe and an httpx call on the
        event loop and stall ``/health`` and the status poll with it (ROBU-05).
        """
        handler = getattr(routes_module, name)
        assert not inspect.iscoroutinefunction(handler)

    def test_refresh_is_refused_cross_site(self, client: TestClient) -> None:
        """
        ``CrossOriginGuard`` covers the new POST with no per-route dependency.

        The guard is app-wide middleware on every non-safe method
        (``web/app.py``), which is the reason a route added later cannot forget
        the check (T-30-46, D-23).
        """
        response = client.post(
            "/api/checks/refresh", headers={"Sec-Fetch-Site": "cross-site"}
        )
        assert response.status_code == 403

    def test_routes_py_probes_in_exactly_one_place(self) -> None:
        """
        The refresh handler reaches the one probe path, and nothing else does.

        A second call site in this module would be a second way for a render
        to probe, which is the failure D-04 exists to prevent; a grep is the
        only thing that can see it.
        """
        source = Path(routes_module.__file__).read_text(encoding="utf-8")
        assert source.count("probe_now()") == 1

    def test_routes_py_runs_no_registry_of_its_own(self) -> None:
        """
        The handler owns no probe implementation, so none can drift from the other.

        WR-03, WR-04 and WR-05 were all consequences of the same
        acquire/run/store block existing in both ``_tick`` and this module.
        """
        source = Path(routes_module.__file__).read_text(encoding="utf-8")
        assert "run_checks" not in source

    def test_routes_py_touches_no_scanner_gate(self) -> None:
        """
        A request handler has no business holding the lock a live scan wants.

        The gate now lives in exactly one probe path, and that path is the
        refresher's.
        """
        source = Path(routes_module.__file__).read_text(encoding="utf-8")
        assert "scanner_gate" not in source


# The stylesheet and the templates, located the way the app locates them so a
# moved package cannot make these source tests silently pass on nothing.
_PACKAGE_DIR = Path(app_module.__file__).parent
_APP_CSS = _PACKAGE_DIR / "static" / "app.css"
_CHECKS_TEMPLATE = _PACKAGE_DIR / "templates" / "partials" / "checks.html"

# Every six-digit colour literal app.css is allowed to contain, pinned as an
# ordered list.  This phase introduces no new colour value: the strip's amber
# is the fallback amber, read through the same custom property.  A new literal
# would be an unmeasured colour on a LAN-visible page.
_EXPECTED_HEX_LITERALS = ["#a16207", "#ca8a04", "#ca8a04"]

# The three declarations Phase 26's coupling contract and tests/test_browser.py
# both pin.  Renaming the property or changing either value breaks the
# vendored-asset contract, so they are asserted byte for byte.
_FALLBACK_DECLARATIONS = [
    "--saneless-status-fallback: #a16207;",
    "--saneless-status-fallback: #ca8a04;",
    "--saneless-status-fallback: #ca8a04;",
]

_HEX_LITERAL = re.compile(r"#[0-9a-fA-F]{6}")
_ROLE_ALERT = re.compile(r'role="alert"')
_CHECKS_STRIP = re.compile(r'<div id="checks-strip"(?P<attrs>[^>]*)>')
_REFRESH_BUTTON = re.compile(
    r'<button type="button" class="check-refresh secondary"'
    r"(?P<attrs>[^>]*)>(?P<text>[^<]*)</button>",
    re.DOTALL,
)


def _css() -> str:
    """
    Read the shipped stylesheet.

    Returns:
        The whole of ``app.css``.

    """
    return _APP_CSS.read_text(encoding="utf-8")


def _template() -> str:
    """
    Read the shipped strip partial.

    Returns:
        The whole of ``partials/checks.html``.

    """
    return _CHECKS_TEMPLATE.read_text(encoding="utf-8")


class TestStripPlacement:
    """UI-SPEC S1 § Placement, and the Phase 26 invariant it must not disturb."""

    def test_the_card_is_the_first_article(self, client: TestClient) -> None:
        """At a glance means first: the strip sits above the Scan card."""
        markup = client.get("/").text
        assert markup.index('id="checks-card"') < markup.index("<h2>Scan</h2>")

    def test_the_status_message_still_precedes_the_status_area(
        self, client: TestClient
    ) -> None:
        """
        Phase 26's invariant survives untouched.

        ``#status-message`` is the immediate sibling above ``#status-area``;
        inserting a card at the top of the block must not have moved either.
        """
        markup = client.get("/").text
        assert re.search(
            r'<div id="status-message" role="alert"></div>\s*<div id="status-area"',
            markup,
        )

    def test_the_form_gained_no_attribute(self, client: TestClient) -> None:
        """
        The C-10 ``hx-disinherit`` fix is exactly as it was.

        The strip is deliberately outside the scan form so the
        ``hx-disabled-elt`` landmine cannot reach it, which is what makes
        touching the form unnecessary.
        """
        assert client.get("/").text.count('hx-disinherit="hx-disabled-elt"') == 1

    def test_the_page_has_one_body_and_one_strip(self, client: TestClient) -> None:
        """Duplicate ids would break both the swap target and the live region."""
        markup = client.get("/").text
        assert markup.count('id="checks-body"') == 1
        assert markup.count('id="checks-strip"') == 1


class TestStripAccessibility:
    """T-30-51: a health list must never interrupt a screen reader."""

    def test_the_strip_is_a_polite_live_region(self, client: TestClient) -> None:
        """The persistent container is what carries the announcement."""
        match = _CHECKS_STRIP.search(client.get("/").text)
        assert match is not None
        assert 'aria-live="polite"' in match.group("attrs")

    def test_the_page_keeps_exactly_one_assertive_region(
        self, client: TestClient
    ) -> None:
        """Phase 26 allows one ``role="alert"``, and it is #status-message."""
        assert len(_ROLE_ALERT.findall(client.get("/").text)) == 1

    def test_the_partial_declares_no_alert(self) -> None:
        """Not even a future edit to the strip may add a second one."""
        assert 'role="alert"' not in _template()

    def test_every_row_carries_a_glyph_and_a_spoken_word(
        self, client: TestClient
    ) -> None:
        """
        Colour is never the only channel (WCAG 1.4.1).

        The glyph is ``aria-hidden`` and an ``.sr-only`` word replaces it, so a
        monochrome or listening reader loses nothing.
        """
        _warm_the_cache(client)
        rows = _CHECK_ROW.findall(client.get("/").text)
        assert len(rows) == len(CheckKey)
        for row in rows:
            assert 'aria-hidden="true"' in row
            assert '<span class="sr-only">' in row
            assert '<span class="check-name">' in row

    def test_a_cold_row_is_spoken_too(self, client: TestClient) -> None:
        """The cold-start marker has a word in the glyph's place as well."""
        rows = _CHECK_ROW.findall(client.get("/api/checks").text)
        assert rows
        assert all(CHECKING_STATE_LABEL in row for row in rows)


class TestStripVocabulary:
    """Pattern C: the template owns no label, class, glyph or timestamp."""

    def test_the_template_compares_no_state_to_a_string(self) -> None:
        """
        A ``{% if state == 'FAIL' %}`` would be a second source of truth.

        The class, glyph and spoken word all come from filters implemented in
        ``saneless.checks``, which is the only reason the strip and
        ``saneless doctor`` cannot drift apart.
        """
        source = _template()
        assert "== '" not in source
        assert '== "' not in source

    @pytest.mark.parametrize(
        "filter_name",
        ["check_name", "check_row_class", "check_row_glyph", "check_row_label"],
    )
    def test_the_template_reaches_for_each_filter(self, filter_name: str) -> None:
        """All four registered filters are the ones the rows are drawn with."""
        assert filter_name in _template()

    @pytest.mark.parametrize(
        "filter_name",
        ["check_state_class", "check_state_glyph", "check_state_label"],
    )
    def test_the_template_draws_no_row_from_a_state_alone(
        self, filter_name: str
    ) -> None:
        """
        R3-WR-03: the marker is chosen from the whole result, flag included.

        A state filter at the call site is what rendered a skipped row as a
        green tick, so the template reaches for none of the three.

        Args:
            filter_name: The state filter that must not appear.

        """
        assert filter_name not in _template()

    def test_a_warn_row_renders_its_next_step(self, client: TestClient) -> None:
        """
        APPL-04: a row that is not green says what to do about it.

        A red row a household member can only escalate is the failure this
        element exists to prevent.
        """
        _app(client).state.checks.store(
            (
                CheckResult(
                    key=CheckKey.FALLBACK,
                    state=CheckState.WARN,
                    message="Not configured.",
                    next_step="Set a fallback folder.",
                ),
            )
        )
        markup = client.get("/").text
        assert '<span class="check-next">Set a fallback folder.</span>' in markup

    def test_an_ok_row_renders_no_next_step(self, client: TestClient) -> None:
        """An ``OK`` row has nothing to act on, so it shows no second line."""
        _warm_the_cache(client)
        assert "check-next" not in client.get("/").text


class TestASkippedRowIsNotAPassingRow:
    """
    R3-WR-03: the strip may not tick a row it never looked at.

    ``_scanner_skipped`` and ``_scanner_busy`` both return ``CheckState.OK``
    with ``skipped`` set -- the state is there so the row has a colour and so a
    scripted health gate stays green for a probe nobody took (D-01).  Until the
    call site read the flag, that made a paused Scanner row render as
    "✓ Scanner  Not checked while a scan is running.", announced to a screen
    reader as "OK: Scanner".  These tests are what stops that coming back.
    """

    def test_a_skipped_row_renders_the_neutral_marker(self, client: TestClient) -> None:
        """The glyph, the colour and the spoken word all say nothing was checked."""
        _app(client).state.checks.store(_results_with_a_skipped_scanner())
        row = _row_named(client.get("/").text, "Scanner")
        assert f'<span class="check-glyph {CHECKING_STATE_CLASS}"' in row
        assert CHECKING_GLYPH in row
        assert f'<span class="sr-only">{SKIPPED_STATE_LABEL}:</span>' in row

    def test_a_skipped_row_carries_no_ok_marker(self, client: TestClient) -> None:
        """
        The absence is the point, and it is asserted inside the row.

        A page-wide ``"✓" not in markup`` would be satisfied by the four green
        rows beside this one, so every assertion here is scoped to the Scanner
        row's own markup.
        """
        _app(client).state.checks.store(_results_with_a_skipped_scanner())
        row = _row_named(client.get("/").text, "Scanner")
        ok_word = f'<span class="sr-only">{check_state_label(CheckState.OK)}:</span>'
        assert check_state_class(CheckState.OK) not in row
        assert check_state_glyph(CheckState.OK) not in row
        assert ok_word not in row

    def test_the_skipped_row_still_says_what_was_not_checked(
        self, client: TestClient
    ) -> None:
        """The neutral marker replaces the tick, not the registry's sentence."""
        _app(client).state.checks.store(_results_with_a_skipped_scanner())
        row = _row_named(client.get("/").text, "Scanner")
        assert PAUSED_SCANNER_MESSAGE in row
        assert "check-next" not in row

    def test_the_other_four_rows_are_unaffected(self, client: TestClient) -> None:
        """One skipped row does not neutralise the rows that were probed."""
        _app(client).state.checks.store(_results_with_a_skipped_scanner())
        markup = client.get("/").text
        ok_class = check_state_class(CheckState.OK)
        ok_word = f'<span class="sr-only">{check_state_label(CheckState.OK)}:</span>'
        for key in CheckKey:
            if key is CheckKey.SCANNER:
                continue
            row = _row_named(markup, check_name(key))
            assert f'<span class="check-glyph {ok_class}"' in row
            assert check_state_glyph(CheckState.OK) in row
            assert ok_word in row

    def test_the_cold_branch_still_says_checking(self, client: TestClient) -> None:
        """
        D-06's five cold rows are untouched.

        ``_CheckingRow`` is not a ``CheckResult`` and still hands its own trio
        to the macro, so the cold word stays "Checking" and does not become
        "Not checked": on a cold start a probe really is coming.
        """
        rows = _CHECK_ROW.findall(client.get("/api/checks").text)
        assert len(rows) == len(CheckKey)
        for row in rows:
            assert f'<span class="check-glyph {CHECKING_STATE_CLASS}"' in row
            assert f'<span class="sr-only">{CHECKING_STATE_LABEL}:</span>' in row
            assert SKIPPED_STATE_LABEL not in row

    def test_the_skipped_row_authors_no_vocabulary_of_its_own(
        self, client: TestClient
    ) -> None:
        """
        Every class, glyph and word in the row comes from ``saneless.checks``.

        Pattern C applied to the case that broke it: the template gained no
        ``{% if c.skipped %}`` and never reads the attribute at all, so the
        substitution is a Python decision the CLI reads too.

        The spoken word is counted inside its own span rather than anywhere in
        the row, because UI-SPEC S1's sentence for this row happens to begin
        with the same two words ("Not checked while a scan is running.") and a
        bare count would be satisfied by the message alone.
        """
        _app(client).state.checks.store(_results_with_a_skipped_scanner())
        row = _row_named(client.get("/").text, "Scanner")
        spoken = f'<span class="sr-only">{SKIPPED_STATE_LABEL}:</span>'
        assert row.count(CHECKING_STATE_CLASS) == 1
        assert row.count(spoken) == 1
        assert "c.skipped" not in _template()


class TestCheckAgainButton:
    """UI-SPEC S1 § `Check again` (D-09)."""

    def test_the_button_is_readable_and_wired(self, client: TestClient) -> None:
        """
        Visible text, not an icon: a household member is the reader.

        ``type="button"`` matters because the control would otherwise submit
        the page's form if it ever moved inside one.
        """
        match = _REFRESH_BUTTON.search(client.get("/").text)
        assert match is not None
        assert match.group("text").strip() == "Check again"
        assert 'hx-post="/api/checks/refresh"' in match.group("attrs")
        assert 'hx-target="#checks-body"' in match.group("attrs")
        assert 'hx-swap="outerHTML"' in match.group("attrs")


class TestStripStyles:
    """UI-SPEC § Color and § Spacing Scale: aliases only, no new value."""

    @pytest.mark.parametrize(
        "selector",
        [".check-ok", ".check-warn", ".check-fail", ".check-checking"],
    )
    def test_the_four_state_classes_exist(self, selector: str) -> None:
        """Every class ``check_state_class`` can return has a rule."""
        assert f"{selector} {{" in _css()

    @pytest.mark.parametrize(
        "selector",
        [
            ".check-list",
            ".check-row",
            ".check-glyph",
            ".check-name",
            ".check-next",
            ".check-meta",
            ".check-refresh",
        ],
    )
    def test_the_layout_classes_exist(self, selector: str) -> None:
        """The seven layout rows of UI-SPEC's exhaustive class table."""
        assert f"{selector} {{" in _css()

    def test_the_warn_class_reads_the_existing_amber(self) -> None:
        """
        One amber serves two jobs, so it reads the same property.

        A degraded success and a warning check are the same colour, and the
        property is not renamed because the vendored-asset contract pins it.
        """
        assert _css().count("var(--saneless-status-fallback)") == 2

    def test_no_colour_literal_was_added(self) -> None:
        """
        The stylesheet's hex literals are exactly the three it already had.

        Every colour the strip renders is an existing token whose contrast is
        already measured in both schemes; an unmeasured colour on a LAN-visible
        page is what this pin prevents.
        """
        assert _HEX_LITERAL.findall(_css()) == _EXPECTED_HEX_LITERALS

    def test_the_fallback_declarations_are_untouched(self) -> None:
        """All three ``--saneless-status-fallback`` declarations, byte for byte."""
        found = [
            line.strip()
            for line in _css().splitlines()
            if "--saneless-status-fallback:" in line
        ]
        assert found == _FALLBACK_DECLARATIONS

    def test_the_touch_target_floor_is_met(self) -> None:
        """
        WCAG 2.5.5: the button is at least 44 px tall (D-30).

        Asserted as the declaration rather than as a measurement, because the
        browser suite measures and this file pins what it measures.
        """
        assert "min-height: 2.75rem;" in _css()


# The two hidden loaders, and the out-of-band wrapper, matched as literals: an
# htmx trigger is only as good as its exact attribute spelling.
_HISTORY_LOADER = 'hx-get="/api/jobs/history"'
_STRIP_LOADER = 'hx-get="/api/checks"'
_OOB_CHECKS = re.compile(r'<div id="checks-body" hx-swap-oob="true"')
_OOB_SCAN_BTN = 'id="scan-btn" hx-swap-oob="true"'

_PARTIALS_DIR = _PACKAGE_DIR / "templates" / "partials"
_STATUS_TEMPLATE = _PARTIALS_DIR / "status.html"
_TERMINAL_RELOAD_TEMPLATE = _PARTIALS_DIR / "terminal_reload.html"


def _templates_containing(needle: str) -> list[str]:
    """
    Name every template file holding `needle`.

    Returns:
        The matching file names, sorted, so an assertion can name them.

    """
    return sorted(
        path.name
        for path in _PACKAGE_DIR.joinpath("templates").rglob("*.html")
        if needle in path.read_text(encoding="utf-8")
    )


def _finish_a_job(client: TestClient, state: JobState) -> None:
    """Create a job, drive it to `state`, and make it the worker's current."""
    job_store = _app(client).state.job_store
    job = job_store.create_job(profile="default", title="Terminal")
    job_store.update_state(job.id, state, error="disk on fire")
    _adopt_as_current_job(client, job.id)


class TestTerminalReloadPartial:
    """The four identical hidden loaders became one partial that does more."""

    def test_the_partial_holds_both_loaders(self) -> None:
        """
        History and the strip are reloaded by the same terminal event.

        The strip loader is here rather than left to the TTL because D-08's
        paused note has to clear the moment the scan ends; waiting out thirty
        seconds would leave the page claiming a scan is still running.
        """
        source = _TERMINAL_RELOAD_TEMPLATE.read_text(encoding="utf-8")
        assert _HISTORY_LOADER in source
        assert _STRIP_LOADER in source
        assert source.count('hx-trigger="load"') == 2
        assert source.count('class="htmx-hidden"') == 2

    def test_status_html_holds_no_loader_markup_of_its_own(self) -> None:
        """The four copies are gone, not merely joined by a fifth."""
        source = _STATUS_TEMPLATE.read_text(encoding="utf-8")
        assert "htmx-hidden" not in source
        assert _HISTORY_LOADER not in source

    def test_every_terminal_branch_includes_it(self) -> None:
        """DONE, FALLBACK, CANCELLED and ERROR all reload the same way."""
        source = _STATUS_TEMPLATE.read_text(encoding="utf-8")
        assert source.count('include "partials/terminal_reload.html"') == 4

    def test_the_history_loader_lives_in_exactly_two_templates(self) -> None:
        """
        One copy for the status branches, one for the request-error slot.

        ``partials/error.html`` keeps its own because an error is rendered
        without a status area at all, so it cannot share the status partial's.
        """
        assert _templates_containing(_HISTORY_LOADER) == [
            "error.html",
            "terminal_reload.html",
        ]

    @pytest.mark.parametrize(
        "state",
        [JobState.DONE, JobState.FALLBACK, JobState.CANCELLED, JobState.ERROR],
    )
    def test_a_terminal_status_reloads_the_strip(
        self, client: TestClient, state: JobState
    ) -> None:
        """Whatever ended the scan, the strip un-pauses at once."""
        _finish_a_job(client, state)
        markup = client.get("/api/jobs/current/status").text
        assert _HISTORY_LOADER in markup
        assert _STRIP_LOADER in markup

    def test_an_active_job_reloads_neither(self, client: TestClient) -> None:
        """A scan still running has nothing terminal to report yet."""
        _finish_a_job(client, JobState.SCANNING)
        markup = client.get("/api/jobs/current/status").text
        assert _HISTORY_LOADER not in markup
        assert _STRIP_LOADER not in markup


class TestWhichResponsesCarryWhat:
    """UI-SPEC § Interaction Map: the out-of-band table, asserted row by row."""

    def test_a_scan_submit_carries_the_strip_out_of_band(
        self, client: TestClient
    ) -> None:
        """
        D-08's paused note appears the instant the scan is accepted.

        Without this the strip would keep claiming a live Scanner reading for
        up to a full TTL after the scanner became unavailable.
        """
        response = client.post(
            "/api/scan", data={"profile": "default", "title": "OOB proof"}
        )
        assert response.status_code == 200
        assert _OOB_CHECKS.search(response.text) is not None

    def test_the_status_poll_does_not(self, client: TestClient) -> None:
        """
        A poll carrying it would re-render the strip every second for nothing.

        That is the same reason ``clear_message`` is scan-only, and it is why
        the flag is set in exactly one handler.
        """
        assert _OOB_CHECKS.search(client.get("/api/jobs/current/status").text) is None

    def test_a_flip_answer_does_not(self, client: TestClient) -> None:
        """A flip answer changes the job, not the appliance's health."""
        response = client.post("/api/flip/continue", data={"job_id": "no-such-job"})
        assert response.status_code == 200
        assert _OOB_CHECKS.search(response.text) is None

    def test_an_error_response_carries_neither(self, client: TestClient) -> None:
        """An error slot renders the sentence and nothing out-of-band."""
        response = client.post(
            "/api/scan", data={"profile": "no-such-profile", "title": ""}
        )
        assert response.status_code != 200
        assert _OOB_CHECKS.search(response.text) is None
        assert _OOB_SCAN_BTN not in response.text

    def test_a_refresh_does_not_re_render_the_scan_button(
        self, client: TestClient
    ) -> None:
        """
        The strip is not a status response, so it owns no button state.

        An OOB ``#scan-btn`` here would let a Check again click re-enable a
        button the server had deliberately disabled.
        """
        response = client.post("/api/checks/refresh")
        assert _OOB_SCAN_BTN not in response.text
        assert 'id="scan-btn"' not in response.text

    def test_the_flag_is_set_by_exactly_one_handler(self) -> None:
        """
        The flag is turned on in one place and defaulted off in one place.

        A second setter would be a second response re-rendering the strip, and
        the reason the flag exists at all is that only the submit has news.
        It is a context-dict key rather than a keyword argument because
        ``TemplateResponse`` takes its context as a mapping.
        """
        source = Path(routes_module.__file__).read_text(encoding="utf-8")
        assert source.count('"refresh_checks": True') == 1
        assert source.count('"refresh_checks": False') == 1
