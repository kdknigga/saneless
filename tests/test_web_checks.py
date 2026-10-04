"""
The status strip decides when a probe happens and what markup carries it.

``saneless.checks`` owns every word a row can say; these tests pin the web
half of the shared registry:

* **No request handler probes.**  An unplugged sane-net host is a TCP connect
  that hangs until the OS gives up, so a page that probed would hang.  The page
  reads a cache a background thread fills, and a spy on ``run_checks`` proves
  the page makes no call.
* **The poll stops itself.**  The cold-start body carries ``hx-trigger`` and
  the body that replaces it does not, so an abandoned browser tab does not keep
  the lazy refresher awake.

Attributes are asserted against compiled regexes over the matched element, so
markup elsewhere on the page cannot satisfy them.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import logging
import re
import threading
import time
from contextlib import contextmanager
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import httpx2
import jinja2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import nodes

from saneless import checks as checks_module
from saneless.checks import (
    CHECKING_GLYPH,
    CHECKING_MESSAGE,
    CHECKING_STATE_CLASS,
    CHECKING_STATE_LABEL,
    SKIPPED_STATE_LABEL,
    CheckContext,
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
from saneless.paperless import ConnectionProbe, PaperlessClient
from saneless.scanner.base import DeviceInfo
from saneless.vocabulary import (
    CheckSurface,
    ConnectionStatus,
    JobState,
    ProfileStorage,
    connection_status_message,
    local_time,
    render_check_step,
)
from saneless.web import app as app_module
from saneless.web import refresher as refresher_module
from saneless.web import routes as routes_module
from saneless.web import strip_view
from saneless.web.app import create_app
from saneless.web.checks_cache import MIN_MANUAL_REFRESH_SECONDS, CheckCache
from saneless.web.refresher import CheckRefresher
from tests.conftest import StubScannerBackend, poll_until, services_of, stand_in
from tests.handler_source_support import (
    HANDLER_FAMILY,
    NOT_IN_HANDLER_FAMILY,
    handler_family_tree,
)
from tests.template_support import markup_start_tags, template_start_tags

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

    from saneless.web.checks_cache import CachedChecks
    from saneless.web.refresher import ManualProbe
    from saneless.web.services import Services

# The paused Scanner row, quoted from the interface spec so the route test fails
# if the registry's sentence and the page's sentence ever drift apart.
PAUSED_SCANNER_MESSAGE = "Not checked while a scan is running."
PAUSED_PREFIX = "Paused during scan — "

# The strip body, captured whole.  Its attributes are asserted against this
# match rather than against the whole response, so "the page contains
# hx-trigger somewhere" can never pass for "the strip polls".
_CHECKS_BODY = re.compile(r'<div id="checks-body"(?P<attrs>[^>]*)>', re.DOTALL)
_HX_GET = re.compile(r'hx-get="(?P<url>[^"]+)"')
_CHECK_ROW = re.compile(r'<li class="check-row">(?P<row>.*?)</li>', re.DOTALL)
_CHECK_META = re.compile(r'<p class="check-meta">(?P<text>.*?)</p>', re.DOTALL)

# The longest a held probe waits for its test to release it.  It bounds what a
# failing test leaks into the refresher thread, and nothing waits it out on a
# passing run.
_HELD_PROBE_SECONDS = 10.0


class _StubScanner(StubScannerBackend):
    """
    The shared stub backend, but reporting one device instead of none.

    The profile dropdown and the worker's startup profile generation both read
    ``get_devices``, and a device list of one is what these tests render
    against.
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


class _RecordingRefresher(CheckRefresher):
    """
    Stands in for :class:`~saneless.web.refresher.CheckRefresher` in a request.

    It counts :meth:`note_watcher` and **swallows** it, which is what makes
    these tests deterministic.  The real refresher is still built and still
    started by the lifespan, but the routes stamp *this* object, so the real
    one never learns it has a watcher and its first guard returns on every
    tick -- no background probe can land mid-assertion and fill the cache a
    cold-start test just emptied.

    :meth:`build_context` is delegated rather than faked, so the context any
    caller sees is the real one the application assembled.

    It subclasses the refresher only so it can stand where the app's services
    hold one.  It never runs the base constructor: every member a route reads
    is overridden here to reach the wrapped refresher, which owns the thread.
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

    def request_probe(self, *, wait: float) -> ManualProbe:
        """
        Ask the real refresher's thread for a probe, which owns the one probe path.

        The answer is forwarded rather than invented.  A stub returning a
        constant would make the handler read every call the same way, and the
        collapse tests below would pass for entirely the wrong reason.

        Args:
            wait: The most seconds the handler waits for the probe.

        Returns:
            Whether the request collapsed, was served in time, or is pending.

        """
        return self._real.request_probe(wait=wait)

    @property
    def thread_name(self) -> str:
        """
        The name of the real refresher's thread, the one every probe runs on.

        Returns:
            The thread name a probe spy should have recorded.

        """
        return self._real._thread.name

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
        self.threads: list[str] = []

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
        self.threads.append(threading.current_thread().name)
        return _synthetic_results()


class _HeldProbeSpy(_ProbeSpy):
    """A ``_ProbeSpy`` whose probe waits until the test lets it finish."""

    def __init__(self) -> None:
        """Start held, with nothing entered yet."""
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(
        self, context: CheckContext, *, scanner_gate: threading.Lock | None = None
    ) -> tuple[CheckResult, ...]:
        """
        Announce the probe, wait for the release, then record it as usual.

        The wait is bounded, so a test that never releases fails on its own
        assertions instead of wedging the refresher thread.

        Args:
            context: The context the refresher built for this probe.
            scanner_gate: The gate the probe handed in rather than held.

        Returns:
            One ``OK`` result per ``CheckKey`` member, in member order.

        """
        self.entered.set()
        self.release.wait(_HELD_PROBE_SECONDS)
        return super().__call__(context, scanner_gate=scanner_gate)


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
    Build the six rows a refresh during a scan produces.

    The Scanner row carries the paused sentence and the ``skipped``
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
        stub_connection: Whether to replace ``probe_connection`` with a
            constant ``CONNECTED``.  False only for the test that counts Paperless
            requests at the transport, which needs the real method to reach the
            mock transport it would otherwise step over.

    Returns:
        The app, whose lifespan (and so its worker) starts with its TestClient.

    """
    settings = Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(url="http://paperless.invalid", token="test-token"),
        output=OutputConfig(tmp_dir=str(tmp_path), data_dir=str(tmp_path)),
        # Two profiles, so the worker's startup generation does not fire
        # and swap the profile set while these requests read it.
        profiles={
            "default": ProfileConfig(),
            "duplex": ProfileConfig(source="ADF Duplex"),
        },
    )
    app = create_app(settings, _StubScanner())
    stand_in(services_of(app).paperless, "get_tags", lambda *, timeout=None: [])
    stand_in(
        services_of(app).paperless, "get_correspondents", lambda *, timeout=None: []
    )
    if stub_connection:
        # Offline, and CONNECTED so the Paperless row is not the one that
        # varies.
        stand_in(
            services_of(app).paperless,
            "probe_connection",
            lambda timeout=None: ConnectionProbe(ConnectionStatus.CONNECTED),
        )
    if stub_refresher:
        app.state.services = dataclasses.replace(
            services_of(app), refresher=_RecordingRefresher(services_of(app).refresher)
        )
    return app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """TestClient over a real app with a stub scanner and no network calls."""
    with TestClient(_make_app(tmp_path)) as tc:
        yield tc


class _FakeClock:
    """
    A monotonic clock the test moves by hand instead of waiting for.

    Ten lines of duplication cost less than a dependency between two test
    modules.  ``CheckCache`` takes its clock as a parameter so that no test
    waits on the wall clock to watch an interval elapse.
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
    really have left the appliance, which is what the refresh floor bounds.
    """

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.count = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """
        Record the request and answer it successfully.

        Args:
            request: The request the client issued.

        Returns:
            An empty, successful page, which the check reads as CONNECTED.

        """
        self.count += 1
        return httpx2.Response(200, json={"count": 0, "results": []})


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
    replacing ``services_of(app).checks`` afterwards would leave the refresher holding
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
    # The refresh minimum interval is measured against CheckCache's clock, so this
    # fixture substitutes it exactly as ``clocked`` does. On the wall clock,
    # "twenty refreshes inside the window" would be an assumption about how fast
    # the runner is, and a loaded runner lets them fall outside it and probe.
    # The clock is never advanced here; this fixture's one test wants the window
    # held open, and a frozen clock is the only way to say that without timing luck.
    clock = _FakeClock()
    monkeypatch.setattr(app_module, "CheckCache", lambda: CheckCache(clock=clock))

    def build_client(
        *, url: str, token: str, consume_dir: Path | None = None
    ) -> PaperlessClient:
        """
        Build the app's Paperless client over a mock transport.

        Returns:
            A client that issues no real network traffic.

        """
        return PaperlessClient(
            url=url,
            token=token,
            consume_dir=consume_dir,
            transport=httpx2.MockTransport(counter),
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
    refresher = services_of(client.app).refresher
    if not isinstance(refresher, _RecordingRefresher):
        msg = "Expected the recording refresher"
        raise TypeError(msg)
    return refresher


def _spy(monkeypatch: pytest.MonkeyPatch) -> _ProbeSpy:
    """
    Replace the refresher module's ``run_checks`` with a counting stand-in.

    The refresher module's reference is the one patched, because the refresh
    handler runs no registry itself: it hands its probe to the refresher
    thread through ``CheckRefresher.request_probe``, and that thread's probe is
    the single implementation both the button and the background ticks share.

    Returns:
        The spy, whose ``calls`` is the number of probes the request made.

    """
    spy = _ProbeSpy()
    monkeypatch.setattr(refresher_module, "run_checks", spy)
    return spy


def _warm_the_cache(client: TestClient) -> None:
    """Store a set of results directly, so the cache is warm."""
    services_of(client.app).checks.store(_synthetic_results())


def _adopt_as_current_job(client: TestClient, job_id: str) -> None:
    """
    Point the live worker at `job_id` without submitting real work.

    This writes ``ScanWorker._current_job_id`` directly, which is private.  It
    is the single place in this module that does so: the routes read "is a
    scan running" through the worker, and driving it through the real submit
    path would run a pipeline these tests do not want.
    """
    services_of(client.app).worker._current_job_id = job_id


def _start_a_scan(client: TestClient) -> str:
    """
    Create a job row and make it the worker's current job.

    Returns:
        The id of the job now reported as running.

    """
    job = services_of(client.app).job_store.create_job(
        profile="default", title="Paused"
    )
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
        target: ``checks_context`` for the context build, ``note_watcher``
            for the watcher stamp that precedes it inside the same guard.

    """

    def boom(*_args: object, **_kwargs: object) -> None:
        """Fail the way an unexpected bug in this route would."""
        raise RuntimeError(_CHECKS_BOOM_MARKER)

    if target == "checks_context":
        monkeypatch.setattr(strip_view, "checks_context", boom)
    else:
        monkeypatch.setattr(services_of(client.app).refresher, target, boom)


class _RecordingCache(CheckCache):
    """
    The real cache, recording what a request was granted and what it gave back.

    A subclass rather than a delegating stand-in, because the refresher and
    the routes share the one cache instance and every other method -- the
    store the probe makes, the read the render does -- has to be the real one
    for the handler under test to behave as it does in production.  Only the
    two claim methods are observed, and both still do their real work.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        """Wrap a real cache on the given clock, with nothing recorded yet."""
        super().__init__(clock=clock)
        self.granted: list[float | None] = []
        self.released: list[float] = []

    def claim_manual_refresh(
        self, min_interval: float = MIN_MANUAL_REFRESH_SECONDS
    ) -> float | None:
        """
        Claim as the real cache does, recording what this call was told.

        Args:
            min_interval: The shortest gap between two grants, in seconds.

        Returns:
            The stamp this grant recorded, or None when it was too soon.

        """
        stamp = super().claim_manual_refresh(min_interval)
        self.granted.append(stamp)
        return stamp

    def release_manual_claim(self, stamp: float) -> bool:
        """
        Release as the real cache does, recording the stamp handed in.

        Args:
            stamp: The value the handler carried from its grant.

        Returns:
            Whether the claim was cleared.

        """
        self.released.append(stamp)
        return super().release_manual_claim(stamp)


@contextmanager
def _a_recording_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, start: float = 100.0
) -> Generator[tuple[TestClient, _RecordingCache]]:
    """
    Build a client whose check cache records the handler's claims and releases.

    Substituted where the app builds its cache, for the reason the ``clocked``
    fixture is: replacing ``services_of(app).checks`` afterwards would leave the
    refresher holding the original, and the two would be separate caches.

    Args:
        tmp_path: The directory the data and temp folders live in.
        monkeypatch: The patcher the substitution is installed with.
        start: The reading the cache's clock begins at.  0.0 is the reading a
            freshly booted appliance really has, because ``time.monotonic()``
            counts from boot on Linux.

    Yields:
        The client and the cache it and its refresher share.

    """
    cache = _RecordingCache(_FakeClock(start=start))
    monkeypatch.setattr(app_module, "CheckCache", lambda: cache)
    with TestClient(_make_app(tmp_path)) as tc:
        yield tc, cache


@contextmanager
def _a_probe_in_flight(client: TestClient) -> Generator[None]:
    """
    Hold the refresher's single-flight lock for the body of the ``with``.

    This is how a test makes ``probe_in_flight`` true without running a probe:
    the route reads ``Lock.locked()`` and never acquires, so a lock this thread
    holds is indistinguishable, to the render, from one a checker holds.

    Args:
        client: The client whose application owns the lock.

    Yields:
        Nothing; the lock is held for the duration of the block.

    """
    lock = _refresher(client).probe_lock
    assert lock.acquire(blocking=False) is True
    try:
        yield
    finally:
        lock.release()


class _AProbeLandsDuringTheRead(CheckCache):
    """
    A checks cache whose read lets the probe in flight land behind it.

    It wraps the cache the app built, the way ``_RecordingRefresher`` wraps the
    refresher, and subclasses it for the same reason, never running the base
    constructor: the read is the real one, and the store is the real one.  What
    it adds is the order.  ``current()`` takes its snapshot of the pre-probe
    entry, then the probe stores its results and releases the single-flight
    lock, and only then does the snapshot come back to the caller.  That is a
    probe finishing in the gap between a render's cache read and its flag read,
    forced rather than waited for.
    """

    def __init__(
        self,
        real: CheckCache,
        lock: threading.Lock,
        landed: tuple[CheckResult, ...],
    ) -> None:
        """Wrap ``real``, releasing ``lock`` and storing ``landed`` on the read."""
        self._real = real
        self._lock = lock
        self._landed = landed

    def current(self) -> CachedChecks:
        """
        Read the real cache, then let the probe land before answering.

        Returns:
            The entry as it stood before the probe stored.

        """
        snapshot = self._real.current()
        self._real.store(self._landed)
        self._lock.release()
        return snapshot


# How far a poll chain is followed before it is called unbounded.  Comfortably
# past any cap the strip could sanely carry -- including
# ``POLL_PROBE_ATTEMPT_CAP``, the larger of the two -- so a chain that reaches
# this many links has not been capped, it has been left running.
_POLL_CHAIN_LIMIT = 200


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
    """Rendering reads the cache; only Refresh is allowed to probe."""

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
    """The refresher only works while a page says someone is looking."""

    def test_index_stamps_the_watcher(self, client: TestClient) -> None:
        """A page load stamps the watcher, without which the refresher never probes."""
        before = _refresher(client).watch_count
        client.get("/")
        assert _refresher(client).watch_count == before + 1

    def test_checks_route_stamps_the_watcher(self, client: TestClient) -> None:
        """The strip route keeps the window open while a tab is left open."""
        before = _refresher(client).watch_count
        client.get("/api/checks")
        assert _refresher(client).watch_count == before + 1

    def test_refresh_stamps_the_watcher(self, client: TestClient) -> None:
        """A Check again click stamps the watcher, as the clearest sign of one."""
        before = _refresher(client).watch_count
        client.post("/api/checks/refresh")
        assert _refresher(client).watch_count == before + 1

    def test_the_real_refresher_is_the_one_the_index_stamps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The wiring holds end to end, with no stand-in in the way.

        Every other test here replaces ``services_of(app).refresher``, so none of them
        would notice if the routes stamped something the lifespan does not
        start.  ``_last_watched`` is private and read deliberately: it is the
        only observable of ``note_watcher`` short of waiting out a tick.
        """
        monkeypatch.setattr(
            "saneless.web.refresher.run_checks", lambda _context: _synthetic_results()
        )
        app = _make_app(tmp_path, stub_refresher=False)
        refresher = services_of(app).refresher
        with TestClient(app) as tc:
            assert refresher._last_watched is None
            tc.get("/")
            assert refresher._last_watched is not None


class TestColdStart:
    """A cold strip is six ``Checking…`` rows and a poll that ends itself."""

    def test_cold_start_renders_one_checking_row_per_check(
        self, client: TestClient
    ) -> None:
        """All six names are on the page before any probe has happened."""
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
        """The swapped-in replacement has no trigger, so the poll stops."""
        _warm_the_cache(client)
        attrs = _body_attrs(client.get("/api/checks").text)
        assert "hx-trigger" not in attrs
        assert "aria-busy" not in attrs

    def test_the_index_never_polls_once_results_exist(self, client: TestClient) -> None:
        """A warm page load carries no steady-state poll either."""
        _warm_the_cache(client)
        assert "hx-trigger" not in _body_attrs(client.get("/").text)


class TestColdStartPollChain:
    """
    A cold strip asks a bounded number of times, followed link by link.

    A cache that is never filled never fires the strip's primary ending, so
    these tests follow the chain rather than looking at one link of it, which
    is what makes "does this ever stop" a question the suite can answer.
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
        A cache with results ends the chain at its first link.

        Stated as a chain rather than as one attribute so that it stays the
        *primary* terminating condition: results arriving stop the poll on the
        very next response.
        """
        _warm_the_cache(client)
        assert _follow_the_poll(client, _POLL_CHAIN_LIMIT) == ["/api/checks"]


class TestBoundedPoll:
    """
    The cold-start poll stops at an attempt cap, and leaves the way forward.

    Results landing in the cache is the primary ending.  The cap is the one
    that fires when nothing ever arrives, such as when the refresher thread has
    died: the server counts the attempts and stops emitting the trigger.  Only
    the first body carries ``load``, because htmx fires ``load`` on content it
    has just swapped in and a polled body carrying it would ask again at once.
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
        At the cap the swap target is inert; at attempt 0 it is not.

        Asserted against the rendered attributes, and both halves in one test:
        an absence is worth nothing without the same reading finding the
        attribute when it is present.  ``hx-`` and not merely ``hx-trigger``,
        because an ``outerHTML`` swap target carrying any unconditional htmx
        request attribute re-arms itself.
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

        htmx re-fires ``load`` for content it has swapped in, so a polled body
        carrying it would request again the moment it arrived, spend every
        attempt in a fraction of a second and cut off a legitimate cold start
        before the refresher's first tick.
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
        A warm cache carries no trigger at any attempt.

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
        An out-of-range ``attempt`` is a trigger-free 200, not a 422.

        An error response is the one thing the polling strip cannot usefully
        receive.  Above the cap the clamp lands on the cap, which is the give-up
        body, and the chain stops there.
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
        The clamp runs first, so only clamped numbers reach the context or body.

        An out-of-range value earns a 200, but the value itself is never
        trusted.
        """
        cap = checks_module.POLL_PROBE_ATTEMPT_CAP
        seen: list[int] = []
        real = strip_view.checks_context

        def recording(svc: Services, *, attempt: int = 0) -> dict[str, object]:
            """Record the attempt the handler passed on, then build normally."""
            seen.append(attempt)
            return real(svc, attempt=attempt)

        monkeypatch.setattr(strip_view, "checks_context", recording)
        for crafted in ("999999", "-5", str(cap + 1)):
            body = client.get(f"/api/checks?attempt={crafted}").text
            assert crafted not in unescape(_body_attrs(body)), (crafted, body)
        assert seen == [cap, 0, cap]

    @pytest.mark.parametrize("crafted", ["abc", "nine", "1e9", ""])
    def test_an_attempt_that_is_not_an_integer_is_still_a_422(
        self, client: TestClient, crafted: str
    ) -> None:
        """
        The clamp bounds the range, never the type.

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

    The strip is the one element that polls, so an error response is the one
    response it cannot usefully receive: retargeted into the message slot, it
    would leave ``#checks-body`` polling and failing every two seconds, and
    aimed at the strip it would replace the rows and the button.  Both routes
    render the cold-start body instead: six named rows, the give-up line naming
    the button, and no trigger.  The exception goes to the log and nowhere
    else, because the body is on a page the whole LAN can read.
    """

    def test_a_failure_inside_the_refresh_render_is_a_cold_strip_at_200(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The button's render falls back to the body its sibling does."""
        _raise_inside_the_checks_route(client, monkeypatch, "checks_context")

        response = client.post("/api/checks/refresh")
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
        for forbidden in (_CHECKS_BOOM_MARKER, "Traceback", "RuntimeError", ".py"):
            assert forbidden not in response.text, (forbidden, response.text)

    def test_the_refresh_failure_is_logged_with_its_traceback(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The refresh guard swallows nothing either: the log keeps the detail."""
        _raise_inside_the_checks_route(client, monkeypatch, "checks_context")

        with caplog.at_level(logging.ERROR, logger=routes_module.__name__):
            assert client.post("/api/checks/refresh").status_code == 200
        assert any(
            record.exc_info is not None and _CHECKS_BOOM_MARKER in str(record.exc_info)
            for record in caplog.records
        )

    def test_a_failing_re_run_leaves_the_strip_as_it_stood(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A re-run that raises answers the click at 200 with the strip as it stood.

        The refresher thread logs the failure and stores nothing, so the
        previous results stay in the cache and on the page, settled, and
        nothing about the exception reaches the body.
        """
        svc = services_of(client.app)
        svc.checks.store(_synthetic_results())
        before = svc.checks.current()
        threads: list[str] = []

        def boom(_context: CheckContext, **_kwargs: object) -> NoReturn:
            threads.append(threading.current_thread().name)
            raise RuntimeError(_CHECKS_BOOM_MARKER)

        monkeypatch.setattr(refresher_module, "run_checks", boom)
        response = client.post("/api/checks/refresh")

        assert response.status_code == 200
        assert threads == [_refresher(client).thread_name]
        after = svc.checks.current()
        assert after.results == before.results
        assert after.checked_at == before.checked_at
        for key in CheckKey:
            assert f"{key.value} row." in _row_named(response.text, check_name(key))
        assert "hx-trigger" not in _body_attrs(response.text)
        for forbidden in (_CHECKS_BOOM_MARKER, "Traceback", "RuntimeError", ".py"):
            assert forbidden not in response.text, (forbidden, response.text)

    @pytest.mark.parametrize(
        ("owner", "target"),
        [
            ("refresher", "note_watcher"),
            ("checks", "claim_manual_refresh"),
            ("refresher", "request_probe"),
        ],
    )
    def test_a_failure_before_the_refresh_render_is_a_failed_click(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        owner: str,
        target: str,
    ) -> None:
        """
        A click whose stamp, claim or probe request raises is an error, not a strip.

        Only the render is guarded. A failure in the click's own action
        reaches the message slot as a server error, so the strip on the page
        stays as it was rather than being swapped for a cold one that hides
        the failure.
        """

        def boom(*_args: object, **_kwargs: object) -> NoReturn:
            raise RuntimeError(_CHECKS_BOOM_MARKER)

        app = _app(client)
        monkeypatch.setattr(getattr(services_of(app), owner), target, boom)
        browser = TestClient(app, raise_server_exceptions=False)

        response = browser.post("/api/checks/refresh", headers={"HX-Request": "true"})

        assert response.status_code == 500
        assert response.headers["HX-Retarget"] == "#status-message"
        assert _CHECK_ROW.findall(response.text) == []
        assert _CHECKS_BOOM_MARKER not in response.text

    @pytest.mark.parametrize("target", ["checks_context", "note_watcher"])
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

    @pytest.mark.parametrize("target", ["checks_context", "note_watcher"])
    def test_no_part_of_the_exception_reaches_the_page(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, target: str
    ) -> None:
        """The failed render says nothing about why."""
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
        _raise_inside_the_checks_route(client, monkeypatch, "checks_context")

        with caplog.at_level(logging.ERROR, logger=routes_module.__name__):
            assert client.get("/api/checks").status_code == 200
        assert any(
            record.exc_info is not None and _CHECKS_BOOM_MARKER in str(record.exc_info)
            for record in caplog.records
        )


class TestRefreshButton:
    """The button re-probes, and cannot defeat the exclusive scanner."""

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
        assert services_of(client.app).checks.is_fresh()
        spy = _spy(monkeypatch)
        client.post("/api/checks/refresh")
        assert spy.calls == 1

    def test_refresh_stores_what_it_probed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The result lands in the cache, so the next render is warm."""
        _spy(monkeypatch)
        assert services_of(client.app).checks.current().results is None
        client.post("/api/checks/refresh")
        assert services_of(client.app).checks.current().results == _synthetic_results()

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
        """The gate reaches ``run_checks``, and the handler holds none."""
        spy = _spy(monkeypatch)
        client.post("/api/checks/refresh")
        assert spy.gates == [services_of(client.app).worker.scanner_gate]

    def test_refresh_during_a_scan_renders_the_paused_row(
        self, client: TestClient
    ) -> None:
        """
        The registry's own paused sentence reaches the page.

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
        Checker contention on an idle appliance is not "a scan".

        The probe lock turns a second checker away before it reaches the
        scanner gate, and the strip re-renders what the first one found, so an
        appliance with no job never reads "not checked while a scan is running".
        """
        _warm_the_cache(client)
        assert services_of(client.app).worker.current_job_id is None
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


class TestCheckAgainOnTheRefresherThread:
    """
    Check again hands its probe to the refresher thread and waits a bounded time.

    The request thread never enters the registry.  The scanner check reaches
    a C library that is not reentrant, and a probe can take far longer than a
    server is given to shut down, so a request thread still inside one would
    hold the process open.  The route signals, waits, and answers either way.
    """

    def test_check_again_probes_on_the_refresher_thread(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one probe a click asks for ran on the refresher thread."""
        spy = _spy(monkeypatch)
        assert client.post("/api/checks/refresh").status_code == 200
        assert spy.threads == [_refresher(client).thread_name]

    def test_a_fast_probe_renders_in_the_same_response(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A probe that finishes inside the wait is the response, settled."""
        _spy(monkeypatch)
        response = client.post("/api/checks/refresh")
        assert response.status_code == 200
        assert "hx-trigger" not in _body_attrs(response.text)
        assert len(_CHECK_ROW.findall(response.text)) == len(CheckKey)
        assert f"{CheckKey.SCANNER.value} row." in response.text

    def test_a_slow_probe_returns_the_settling_strip(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A probe still running at the end of the wait leaves the strip asking.

        The route waits ``CHECK_AGAIN_WAIT_SECONDS`` for the probe and no
        longer: the probe is held until after the response, so a response at
        all, with the request reported pending, is the wait ending on its own.
        The response carries the trigger that collects the answer, and the
        answer is there once the probe finishes.  No clock is read, so a slow
        machine cannot fail it.
        """
        monkeypatch.setattr(routes_module, "CHECK_AGAIN_WAIT_SECONDS", 0.2)
        spy = _HeldProbeSpy()
        monkeypatch.setattr(refresher_module, "run_checks", spy)
        refresher = _refresher(client)
        real_request_probe = refresher.request_probe
        asked: list[tuple[float, ManualProbe]] = []

        def recording_request_probe(*, wait: float) -> ManualProbe:
            outcome = real_request_probe(wait=wait)
            asked.append((wait, outcome))
            return outcome

        monkeypatch.setattr(refresher, "request_probe", recording_request_probe)
        try:
            response = client.post("/api/checks/refresh")

            assert response.status_code == 200
            assert asked == [(0.2, refresher_module.ManualProbe.PENDING)]
            assert spy.entered.wait(_HELD_PROBE_SECONDS) is True
            assert spy.calls == 0
            assert "hx-trigger" in _body_attrs(response.text)
        finally:
            spy.release.set()

        assert poll_until(
            lambda: not _refresher(client).probe_in_flight, _HELD_PROBE_SECONDS
        )
        settled = client.get("/api/checks")
        assert "hx-trigger" not in _body_attrs(settled.text)
        assert f"{CheckKey.SCANNER.value} row." in settled.text

    def test_a_refused_click_returns_at_once(
        self,
        clocked: _Clocked,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A click the floor refuses waits for nothing, and asks for nothing."""
        client, _clock = clocked
        monkeypatch.setattr(routes_module, "CHECK_AGAIN_WAIT_SECONDS", 0.2)
        spy = _HeldProbeSpy()
        monkeypatch.setattr(refresher_module, "run_checks", spy)
        try:
            assert client.post("/api/checks/refresh").status_code == 200
            began = time.monotonic()
            refused = client.post("/api/checks/refresh")
            elapsed = time.monotonic() - began
        finally:
            spy.release.set()

        assert refused.status_code == 200
        assert elapsed < 1.0
        assert poll_until(
            lambda: not _refresher(client).probe_in_flight, _HELD_PROBE_SECONDS
        )
        assert spy.calls == 1

    def test_a_collapsed_click_releases_its_claim(
        self,
        clocked: _Clocked,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A click that lands on a running probe returns at once and costs no claim.

        The third click is inside the floor of the second, so it is honoured
        only if the collapsed second click gave its claim back.
        """
        client, clock = clocked
        monkeypatch.setattr(routes_module, "CHECK_AGAIN_WAIT_SECONDS", 0.2)
        spy = _HeldProbeSpy()
        monkeypatch.setattr(refresher_module, "run_checks", spy)
        try:
            first = client.post("/api/checks/refresh")
            assert "hx-trigger" in _body_attrs(first.text)
            assert spy.entered.wait(_HELD_PROBE_SECONDS) is True
            clock.advance(MIN_MANUAL_REFRESH_SECONDS + 1.0)

            began = time.monotonic()
            collapsed = client.post("/api/checks/refresh")
            elapsed = time.monotonic() - began
        finally:
            spy.release.set()

        assert collapsed.status_code == 200
        assert elapsed < 1.0
        assert "hx-trigger" in _body_attrs(collapsed.text)
        assert poll_until(
            lambda: not _refresher(client).probe_in_flight, _HELD_PROBE_SECONDS
        )
        assert spy.calls == 1
        assert client.post("/api/checks/refresh").status_code == 200
        assert spy.calls == 2


class TestRefreshMinimumInterval:
    """The one handler allowed to probe has a floor under it."""

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
        household member about.
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
        """Twenty refreshes in a loop are one run of the registry."""
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
        """A refresh after the floor probes again, with no TTL to wait out."""
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

    def test_the_first_click_after_a_boot_is_a_grant_and_not_a_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The handler reads the grant as ``is not None``, not as truthy.

        ``time.monotonic()`` counts from boot on Linux, so a grant on a freshly
        booted appliance can be the falsey 0.0.  Read as a refusal, the first
        click after a boot would never probe.  The cache records the grant, so
        the assertion is against the value the handler was really given.
        """
        with _a_recording_cache(tmp_path, monkeypatch, start=0.0) as (client, cache):
            spy = _spy(monkeypatch)
            assert client.post("/api/checks/refresh").status_code == 200
            assert cache.granted == [0.0]
            assert spy.calls == 1

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
    The one manual control always puts an answer on the page.

    A click landing while a background probe holds the lock collapses into it.
    The collapsed body carries a trigger, so the page collects the result that
    probe stores, and the click gives its claim back, so the next click is
    honoured.  The window is one background probe's duration out of every TTL,
    and the Paperless read budget alone is five seconds, so it is not narrow.
    """

    def test_a_collapsed_refresh_asks_the_page_to_come_back_for_the_result(
        self, client: TestClient
    ) -> None:
        """
        A collapsed click's body carries a trigger.

        Asserted against the rendered attributes, so only markup the server
        really emits can satisfy it.
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

        The clock never moves, so all three clicks land inside the 2 s floor,
        and the third is honoured only if both collapses gave their claim back.
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

    def test_a_collapsed_click_gives_back_the_stamp_it_was_granted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The handler carries its own grant from the claim to the release.

        The release is a compare-and-clear, so what it is handed decides
        whether anything is given back at all.  The two values compared here
        are both the cache's own records -- what it granted this request and
        what it was asked to release -- so a handler that released a stamp it
        computed for itself, from a fresh clock reading or from anywhere else,
        fails this even when the two happen to be numerically close.
        """
        with _a_recording_cache(tmp_path, monkeypatch) as (client, cache):
            _spy(monkeypatch)
            _warm_the_cache(client)
            with _a_probe_in_flight(client):
                assert client.post("/api/checks/refresh").status_code == 200
            assert len(cache.granted) == 1
            assert cache.granted[0] is not None
            assert cache.released == cache.granted

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

        By the time the handler renders, its own probe has released the lock
        and stored, so there is nothing left to wait for.
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

    def test_the_settling_poll_stops_at_the_cap_that_applies_to_a_live_probe(
        self, client: TestClient
    ) -> None:
        """
        A probe wedged in a getaddrinfo cannot make a tab ask forever.

        The settling poll rides the same ``attempt`` parameter, and while a
        probe demonstrably holds the single-flight lock the bound that applies
        to it is ``POLL_PROBE_ATTEMPT_CAP`` rather than ``POLL_ATTEMPT_CAP``.
        It is still a bound: ``Lock.locked()`` stays true forever if the holder
        dies, so the larger window is a cap and not an exemption.
        """
        _warm_the_cache(client)
        with _a_probe_in_flight(client):
            markup = client.get(
                f"/api/checks?attempt={checks_module.POLL_PROBE_ATTEMPT_CAP}"
            ).text
        assert "hx-" not in _body_attrs(markup)

    def test_a_settling_poll_that_runs_out_keeps_its_last_checked_line(
        self, client: TestClient
    ) -> None:
        """
        The give-up line stays cold-start-only: there are results, and they hold.

        ``POLL_GAVE_UP_LINE`` says the checks have not run yet.  Printing it
        beside six rows that did run would be a lie the strip tells about
        itself, so ``gave_up`` requires an empty cache.
        """
        _warm_the_cache(client)
        with _a_probe_in_flight(client):
            markup = client.get(
                f"/api/checks?attempt={checks_module.POLL_PROBE_ATTEMPT_CAP}"
            ).text
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
        The cap it ends at is ``POLL_PROBE_ATTEMPT_CAP``, because the lock is
        held for the whole walk.
        """
        _warm_the_cache(client)
        with _a_probe_in_flight(client):
            followed = _follow_the_poll(client, _POLL_CHAIN_LIMIT)
        record_property("settling_poll_chain_length", len(followed))
        assert len(followed) == checks_module.POLL_PROBE_ATTEMPT_CAP + 1, followed

    def test_results_and_an_idle_probe_still_carry_no_trigger(
        self, client: TestClient
    ) -> None:
        """The strip asks only while something in flight will change the answer."""
        _warm_the_cache(client)
        assert _refresher(client).probe_in_flight is False
        assert "hx-" not in _body_attrs(client.get("/api/checks").text)
        assert "hx-" not in _body_attrs(client.get("/").text)

    def test_a_probe_landing_between_the_reads_is_collected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The forced interleaving: a probe that lands mid-render still gets fetched.

        A render reads two things, the cached rows and whether a probe is in
        flight.  If the rows come first, a probe can store and release its lock
        between the two reads: the rows are the pre-probe ones, the flag then
        says nothing is in flight, and the body carries no trigger -- so the
        results that probe just stored never reach the page.  Reading the flag
        first sees the probe as in flight, and the body asks once more, which
        collects them.
        """
        app = _app(client)
        real: CheckCache = services_of(app).checks
        real.store(_results_with_a_skipped_scanner())
        landed = _synthetic_results()
        lock = _refresher(client).probe_lock
        assert lock.acquire(blocking=False) is True
        app.state.services = dataclasses.replace(
            services_of(app), checks=_AProbeLandsDuringTheRead(real, lock, landed)
        )
        try:
            context = strip_view.checks_context(services_of(app), attempt=1)
        finally:
            if lock.locked():
                lock.release()

        assert context["checks"] == _results_with_a_skipped_scanner()
        assert real.current().results == landed
        assert context["poll_attempt"] == 2


class TestTheWindowFollowsTheProbe:
    """
    The strip stops asking, but never while the first check is running.

    ``POLL_ATTEMPT_CAP`` is about twenty seconds, and a first probe of a
    silently unreachable scanner can take roughly 127 s in ``get_devices()``.
    Giving up then would print "Press Check again" for a check that is already
    running.  While a checker holds the single-flight lock the chain runs to
    ``POLL_PROBE_ATTEMPT_CAP`` instead and the give-up line is withheld.  It is
    still a cap: ``Lock.locked()`` stays true forever if the holder dies.
    """

    def test_the_second_cap_bounds_the_worst_case_this_module_documents(self) -> None:
        """
        The larger cap is larger, and large enough for the 127 s enumeration.

        A cap trimmed back below the enumeration it exists to outlast fails
        here.
        """
        assert checks_module.POLL_PROBE_ATTEMPT_CAP > checks_module.POLL_ATTEMPT_CAP, (
            "the probe window must be the larger of the two"
        )
        assert checks_module.POLL_PROBE_ATTEMPT_CAP * 2 > 127

    def test_the_still_checking_line_names_nothing_a_lan_reader_may_not_see(
        self,
    ) -> None:
        """
        The still-checking line is held to ``POLL_GAVE_UP_LINE``'s rule.

        It renders on a page the whole LAN can read, so it names no host, port,
        path, URL or exception text -- only what is happening and roughly how
        long it can take.
        """
        line = checks_module.POLL_STILL_CHECKING_LINE
        for forbidden in ("http", "://", "/", "\\", ":", "Error", "Traceback"):
            assert forbidden not in line, (forbidden, line)

    def test_a_cold_chain_with_nothing_in_flight_still_gives_up_at_the_first_cap(
        self, client: TestClient
    ) -> None:
        """
        A cold chain with nothing in flight gives up at ``POLL_ATTEMPT_CAP``.

        That is the bound for an appliance whose refresher thread has died and
        a tab left open in front of it, and the contrast case for this class.
        """
        markup = client.get(
            f"/api/checks?attempt={checks_module.POLL_ATTEMPT_CAP}"
        ).text
        assert "hx-" not in _body_attrs(markup)
        meta = _CHECK_META.search(markup)
        assert meta is not None, markup
        assert meta.group("text").strip() == checks_module.POLL_GAVE_UP_LINE

    def test_a_cold_chain_waiting_on_a_probe_keeps_asking_past_the_first_cap(
        self, client: TestClient
    ) -> None:
        """A chain that is waiting on a live probe is not out of attempts."""
        cap = checks_module.POLL_ATTEMPT_CAP
        with _a_probe_in_flight(client):
            attrs = unescape(_body_attrs(client.get(f"/api/checks?attempt={cap}").text))
        assert f"/api/checks?attempt={cap + 1}" in attrs, attrs

    def test_a_cold_chain_waiting_on_a_probe_says_the_first_check_is_still_running(
        self, client: TestClient
    ) -> None:
        """
        The give-up line is withheld while the thing the button starts is running.

        "Press Check again to try now" beside a probe that is demonstrably in
        flight tells a household member to do the one thing that cannot help.
        """
        with _a_probe_in_flight(client):
            markup = client.get(
                f"/api/checks?attempt={checks_module.POLL_ATTEMPT_CAP}"
            ).text
        meta = _CHECK_META.search(markup)
        assert meta is not None, markup
        line = meta.group("text").strip()
        assert line == checks_module.POLL_STILL_CHECKING_LINE
        assert line != checks_module.POLL_GAVE_UP_LINE

    def test_a_probe_that_never_ends_still_stops_the_asking(
        self, client: TestClient
    ) -> None:
        """
        Under a live probe the poll still gives up at the probe attempt cap.

        A thread that died holding the single-flight lock leaves ``locked()``
        true forever, so the larger window has to end on its own too.
        """
        with _a_probe_in_flight(client):
            markup = client.get(
                f"/api/checks?attempt={checks_module.POLL_PROBE_ATTEMPT_CAP}"
            ).text
        assert "hx-" not in _body_attrs(markup)
        meta = _CHECK_META.search(markup)
        assert meta is not None, markup
        assert meta.group("text").strip() == checks_module.POLL_GAVE_UP_LINE

    def test_the_cold_chain_under_a_stuck_probe_is_bounded_by_the_second_cap(
        self, client: TestClient, record_property: Callable[[str, object], None]
    ) -> None:
        """
        Followed link by link with the lock never released, the chain still ends.

        One longer than the cap, for the same reason every chain in this file
        is: its first link is the bare route, the body a page render already
        carries.
        """
        with _a_probe_in_flight(client):
            followed = _follow_the_poll(client, _POLL_CHAIN_LIMIT)
        record_property("cold_probe_chain_length", len(followed))
        assert len(followed) == checks_module.POLL_PROBE_ATTEMPT_CAP + 1, len(followed)

    def test_a_settling_chain_past_the_first_cap_keeps_its_last_checked_line(
        self, client: TestClient
    ) -> None:
        """
        Rows that did run keep their own sentence, whichever cap is in force.

        Neither the give-up line nor the still-checking line belongs beside
        six results: one says nothing has been checked and the other says the
        *first* check is running, and both would be false here.
        """
        cap = checks_module.POLL_ATTEMPT_CAP
        _warm_the_cache(client)
        with _a_probe_in_flight(client):
            markup = client.get(f"/api/checks?attempt={cap + 5}").text
        attrs = unescape(_body_attrs(markup))
        assert f"/api/checks?attempt={cap + 6}" in attrs, attrs
        meta = _CHECK_META.search(markup)
        assert meta is not None, markup
        assert meta.group("text").strip().startswith("Last checked ")

    def test_the_route_accepts_the_second_cap_and_clamps_anything_larger(
        self, client: TestClient
    ) -> None:
        """
        The clamp's upper bound is the larger cap, so a real attempt survives it.

        Clamping a legitimate settling attempt down to ten would end the chain
        early by arithmetic the browser never asked for.  Past the larger cap
        the clamp lands on it, which is the ending, so an out-of-range counter
        is still a trigger-free 200 and never a status code.
        """
        probe_cap = checks_module.POLL_PROBE_ATTEMPT_CAP
        at_the_cap = client.get(f"/api/checks?attempt={probe_cap}")
        assert at_the_cap.status_code == 200
        assert "hx-" not in _body_attrs(at_the_cap.text)

        with _a_probe_in_flight(client):
            beyond = client.get(f"/api/checks?attempt={probe_cap + 1}")
            legitimate = client.get(f"/api/checks?attempt={probe_cap - 1}")
        assert beyond.status_code == 200
        assert "hx-" not in _body_attrs(beyond.text)
        assert f"/api/checks?attempt={probe_cap}" in unescape(
            _body_attrs(legitimate.text)
        )


class TestFreshnessLine:
    """The freshness line has four situations and four exact sentences."""

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
        checked_at = services_of(client.app).checks.current().checked_at
        assert checked_at is not None
        assert (
            self._meta(client.get("/api/checks").text)
            == f"Last checked {local_time(checked_at)}."
        )

    def test_results_and_scanning(self, client: TestClient) -> None:
        """Results during a scan read "Paused during scan — last checked …"."""
        _warm_the_cache(client)
        _start_a_scan(client)
        checked_at = services_of(client.app).checks.current().checked_at
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

        An ``async def`` here would run a socket probe and an httpx2 call on the
        event loop and stall ``/health`` and the status poll with it.
        """
        handler = getattr(routes_module, name)
        assert not inspect.iscoroutinefunction(handler)

    def test_refresh_is_refused_cross_site(self, client: TestClient) -> None:
        """
        ``CrossOriginGuard`` covers the refresh POST with no per-route dependency.

        The guard is app-wide middleware on every non-safe method
        (``web/app.py``), so a route added later cannot forget the check.
        """
        response = client.post(
            "/api/checks/refresh", headers={"Sec-Fetch-Site": "cross-site"}
        )
        assert response.status_code == 403

    def test_the_handlers_probe_in_exactly_one_place(self) -> None:
        """
        The refresh handler reaches the one probe path, and nothing else does.

        A second call in the handlers or the modules they call would be a
        second way for a render to probe.  Calls are counted in the parsed
        modules, so a comment or docstring may name the method freely.
        """
        calls = [
            node.lineno
            for node in ast.walk(handler_family_tree())
            if isinstance(node, ast.Call) and _called_name(node) == "request_probe"
        ]
        assert len(calls) == 1, calls

    def test_the_handlers_run_no_registry_of_their_own(self) -> None:
        """
        The handler owns no probe implementation, so none can drift from the other.

        Two copies of the acquire/run/store block would be two probe paths to
        keep in step; code that names ``run_checks`` is what this counts.
        """
        assert _references(handler_family_tree(), "run_checks") == []

    def test_the_handlers_touch_no_scanner_gate(self) -> None:
        """
        A request handler has no business holding the lock a live scan wants.

        The gate lives in exactly one probe path, and that path is the
        refresher's.
        """
        assert _references(handler_family_tree(), "scanner_gate") == []

    def test_the_family_tree_holds_every_family_module(self) -> None:
        """
        The tree the checks above walk defines a function from every module.

        A module dropped from the family would leave its calls unseen and the
        checks passing on less than they claim, so one function each module
        defines is looked for by name.
        """
        defined = {
            node.name
            for node in handler_family_tree().body
            if isinstance(node, ast.FunctionDef)
        }
        expected = {
            "saneless.web.routes": "start_scan",
            "saneless.web.owner": "is_owner",
            "saneless.web.strip_view": "checks_context",
            "saneless.web.metadata_view": "tag_list_context",
            "saneless.web.status_view": "status_context",
            "saneless.web.profile_view": "profile_options",
            "saneless.web.scan_block": "block_for",
            "saneless.web.job_view": "build_job_view",
        }
        assert {module.__name__ for module in HANDLER_FAMILY} == set(expected)
        missing = set(expected.values()) - defined
        assert not missing, missing

    def test_the_family_holds_every_web_module_the_routes_import(self) -> None:
        """
        Every ``saneless.web`` module routes imports is in the family or excused.

        Read from routes' own imports rather than kept by hand, so a module the
        handlers start calling is walked by the checks above unless it is named,
        with a reason, in ``NOT_IN_HANDLER_FAMILY``.
        """
        tree = ast.parse(Path(routes_module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "saneless.web":
                imported.update(f"saneless.web.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "saneless.web."
            ):
                imported.add(str(node.module))
        family = {module.__name__ for module in HANDLER_FAMILY} - {
            routes_module.__name__
        }
        assert set(NOT_IN_HANDLER_FAMILY) <= imported, (
            set(NOT_IN_HANDLER_FAMILY) - imported
        )
        assert imported - set(NOT_IN_HANDLER_FAMILY) == family


# The stylesheet and the templates, located the way the app locates them so a
# moved package cannot make these source tests silently pass on nothing.
_PACKAGE_DIR = Path(app_module.__file__).parent
_APP_CSS = _PACKAGE_DIR / "static" / "app.css"
_CHECKS_TEMPLATE = _PACKAGE_DIR / "templates" / "partials" / "checks.html"

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


def _called_name(call: ast.Call) -> str | None:
    """
    Name the function a call reaches, whether bare or through an attribute.

    Returns:
        The called name, or None for a call through any other expression.

    """
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _references(tree: ast.AST, name: str) -> list[int]:
    """
    List the lines where code names ``name`` as an identifier.

    A bare name, an attribute, an import, a keyword argument and a parameter
    all count; a string, a comment or a docstring does not.

    Returns:
        The line number of every reference, in walk order.

    """
    found: list[int] = []
    for node in ast.walk(tree):
        named = (
            (isinstance(node, ast.Name) and node.id == name)
            or (isinstance(node, ast.Attribute) and node.attr == name)
            or (isinstance(node, ast.arg) and node.arg == name)
        )
        if named:
            found.append(getattr(node, "lineno", 0))
        elif (isinstance(node, ast.alias) and name in {node.name, node.asname}) or (
            isinstance(node, ast.keyword) and node.arg == name
        ):
            found.append(node.lineno)
    return found


def _dict_value(node: ast.expr, key: str) -> object:
    """
    Read the constant a dict literal maps ``key`` to.

    Returns:
        The constant, or None when ``node`` is not a dict literal holding a
        constant under that key.

    """
    if not isinstance(node, ast.Dict):
        return None
    for name, value in zip(node.keys, node.values, strict=True):
        if (
            isinstance(name, ast.Constant)
            and name.value == key
            and isinstance(value, ast.Constant)
        ):
            return value.value
    return None


def _context_values(tree: ast.AST, key: str) -> list[object]:
    """
    Collect every constant a dict literal in ``tree`` gives ``key``.

    Returns:
        One value per dict literal entry under that key, in walk order.

    """
    return [
        value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for name, value in zip(node.keys, node.values, strict=True)
        if isinstance(name, ast.Constant)
        and name.value == key
        and isinstance(value, ast.Constant)
    ]


def _template_renders(tree: ast.AST, template: str) -> list[tuple[int, list[ast.expr]]]:
    """
    Find every render of ``template`` and the arguments its context is in.

    A ``TemplateResponse`` names the template and takes its context in the
    same call; a ``get_template`` call names it and the ``render`` call on its
    result takes the context.

    Returns:
        The line and the context-bearing arguments of each render.

    """

    def names_template(call: ast.Call) -> bool:
        """Say whether ``call`` passes the template's name as an argument."""
        return any(
            isinstance(argument, ast.Constant) and argument.value == template
            for argument in call.args
        )

    renders: list[tuple[int, list[ast.expr]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        if (
            _called_name(node) == "render"
            and (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Call)
                and names_template(node.func.value)
            )
        ) or (_called_name(node) == "TemplateResponse" and names_template(node)):
            renders.append((node.lineno, arguments))
    return renders


def _filters_used(path: Path) -> set[str]:
    """
    Name every filter a template applies.

    Returns:
        The filter names, read from the parsed template rather than its text.

    """
    return {node.name for node in _jinja_tree(path).find_all(nodes.Filter)}


def _jinja_tree(path: Path) -> nodes.Template:
    """
    Parse a template with Jinja's own parser.

    Returns:
        The template's syntax tree, so Jinja comments never count.

    """
    return jinja2.Environment(autoescape=True).parse(path.read_text(encoding="utf-8"))


class TestStripPlacement:
    """The strip sits first on the page and leaves the status block in place."""

    def test_the_card_is_the_first_article(self, client: TestClient) -> None:
        """At a glance means first: the strip sits above the Scan card."""
        markup = client.get("/").text
        assert markup.index('id="checks-card"') < markup.index("<h2>Scan</h2>")

    def test_the_status_message_still_precedes_the_status_area(
        self, client: TestClient
    ) -> None:
        """
        ``#status-message`` is the immediate sibling above the status block.

        The status block is ``#status-live`` holding ``#status-area`` as its
        first child, and a card at the top of the page moves neither.
        """
        markup = client.get("/").text
        assert re.search(
            r'<div id="status-message" role="alert"></div>\s*'
            r'<div id="status-live" role="status">\s*<div id="status-area"',
            markup,
        )

    def test_the_form_gained_no_attribute(self, client: TestClient) -> None:
        """
        The scan form carries its one ``hx-disinherit`` and the strip adds none.

        The strip is deliberately outside the scan form so the form's
        ``hx-disabled-elt`` cannot reach it.
        """
        assert client.get("/").text.count('hx-disinherit="hx-disabled-elt"') == 1

    def test_the_page_has_one_body_and_one_strip(self, client: TestClient) -> None:
        """Duplicate ids would break both the swap target and the live region."""
        markup = client.get("/").text
        assert markup.count('id="checks-body"') == 1
        assert markup.count('id="checks-strip"') == 1


class TestStripAccessibility:
    """A health list never interrupts a screen reader."""

    def test_the_strip_is_not_a_live_region(self, client: TestClient) -> None:
        """
        The strip is silent; its rows stay reachable by navigation.

        A health list that spoke on its own would compete with the status
        area, which is the one thing on the page that announces a scan.
        """
        match = _CHECKS_STRIP.search(client.get("/").text)
        assert match is not None
        assert "aria-live" not in match.group("attrs")

    def test_the_page_keeps_exactly_one_assertive_region(
        self, client: TestClient
    ) -> None:
        """The page has one ``role="alert"``, and it is #status-message."""
        assert len(_ROLE_ALERT.findall(client.get("/").text)) == 1

    def test_the_partial_declares_no_alert(self) -> None:
        """Not even a future edit to the strip may add a second one."""
        roles = [
            attrs.get("role") for _tag, attrs in template_start_tags(_CHECKS_TEMPLATE)
        ]
        assert "alert" not in roles

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
        string_comparisons = [
            node.lineno
            for node in _jinja_tree(_CHECKS_TEMPLATE).find_all(nodes.Compare)
            if any(
                isinstance(operand, nodes.Const) and isinstance(operand.value, str)
                for operand in [node.expr, *(op.expr for op in node.ops)]
            )
        ]
        assert string_comparisons == []

    @pytest.mark.parametrize(
        "filter_name",
        ["check_name", "check_row_class", "check_row_glyph", "check_row_label"],
    )
    def test_the_template_reaches_for_each_filter(self, filter_name: str) -> None:
        """All four registered filters are the ones the rows are drawn with."""
        assert filter_name in _filters_used(_CHECKS_TEMPLATE)

    @pytest.mark.parametrize(
        "filter_name",
        ["check_state_class", "check_state_glyph", "check_state_label"],
    )
    def test_the_template_draws_no_row_from_a_state_alone(
        self, filter_name: str
    ) -> None:
        """
        The marker is chosen from the whole result, flag included.

        A state filter at the call site would render a skipped row as a green
        tick, so the template reaches for none of the three.

        Args:
            filter_name: The state filter that must not appear.

        """
        assert filter_name not in _filters_used(_CHECKS_TEMPLATE)

    def test_a_warn_row_renders_its_next_step(self, client: TestClient) -> None:
        """
        A row that is not green says what to do about it.

        A red row a household member can only escalate is the failure this
        element exists to prevent.
        """
        services_of(client.app).checks.store(
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
    The strip never ticks a row it never looked at.

    ``_scanner_skipped`` and ``_scanner_busy`` both return ``CheckState.OK``
    with ``skipped`` set, so the row has a colour and a scripted health gate
    stays green for a probe nobody took.  Rendered from the state alone, a
    paused Scanner row would read "✓ Scanner  Not checked while a scan is
    running." and be announced as "OK: Scanner".
    """

    def test_a_skipped_row_renders_the_neutral_marker(self, client: TestClient) -> None:
        """The glyph, the colour and the spoken word all say nothing was checked."""
        services_of(client.app).checks.store(_results_with_a_skipped_scanner())
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
        services_of(client.app).checks.store(_results_with_a_skipped_scanner())
        row = _row_named(client.get("/").text, "Scanner")
        ok_word = f'<span class="sr-only">{check_state_label(CheckState.OK)}:</span>'
        assert check_state_class(CheckState.OK) not in row
        assert check_state_glyph(CheckState.OK) not in row
        assert ok_word not in row

    def test_the_skipped_row_still_says_what_was_not_checked(
        self, client: TestClient
    ) -> None:
        """The neutral marker replaces the tick, not the registry's sentence."""
        services_of(client.app).checks.store(_results_with_a_skipped_scanner())
        row = _row_named(client.get("/").text, "Scanner")
        assert PAUSED_SCANNER_MESSAGE in row
        assert "check-next" not in row

    def test_the_other_four_rows_are_unaffected(self, client: TestClient) -> None:
        """One skipped row does not neutralise the rows that were probed."""
        services_of(client.app).checks.store(_results_with_a_skipped_scanner())
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
        The six cold rows still say "Checking".

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

        The template never reads ``skipped``, so the substitution is a Python
        decision the CLI reads too.  The spoken word is counted inside its own
        span, because the row's sentence ("Not checked while a scan is
        running.") begins with the same two words and a bare count would be
        satisfied by the message alone.
        """
        services_of(client.app).checks.store(_results_with_a_skipped_scanner())
        row = _row_named(client.get("/").text, "Scanner")
        spoken = f'<span class="sr-only">{SKIPPED_STATE_LABEL}:</span>'
        assert row.count(CHECKING_STATE_CLASS) == 1
        assert row.count(spoken) == 1
        attributes = {
            node.attr for node in _jinja_tree(_CHECKS_TEMPLATE).find_all(nodes.Getattr)
        }
        assert "skipped" not in attributes


class TestCheckAgainButton:
    """The `Check again` button is visible text wired to the refresh route."""

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
    """The strip's styles are aliases of existing tokens, with no new value."""

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
        """Every layout class the strip's markup uses has a rule."""
        assert f"{selector} {{" in _css()

    def test_the_warn_class_reads_the_existing_amber(self) -> None:
        """
        One amber serves two jobs, so it reads the same property.

        A degraded success and a warning check are the same colour, and the
        property is not renamed because the vendored-asset contract pins it.
        """
        assert _css().count("var(--saneless-status-fallback)") == 2

    def test_the_touch_target_floor_is_met(self) -> None:
        """
        The button is at least 44 px tall, the WCAG 2.5.5 touch target.

        Asserted as the declaration rather than as a measurement, because the
        browser suite measures and this file pins what it measures.
        """
        assert "min-height: 2.75rem;" in _css()


# The two hidden loaders, and the out-of-band wrapper, matched as literals: an
# htmx trigger is only as good as its exact attribute spelling.
_HISTORY_LOADER = 'hx-get="/api/jobs/history"'
_STRIP_LOADER = 'hx-get="/api/checks"'
_OOB_CHECKS = re.compile(
    r'<div(?=[^>]*\sid="checks-body")(?=[^>]*\shx-swap-oob="true")\s[^>]*>'
)
# The out-of-band Scan button, found by its attributes in any order.
_OOB_SCAN_BTN = re.compile(
    r'<button(?=[^>]*\sid="scan-btn")(?=[^>]*\shx-swap-oob="true")\s[^>]*>'
)

_PARTIALS_DIR = _PACKAGE_DIR / "templates" / "partials"
_STATUS_TEMPLATE = _PARTIALS_DIR / "status.html"
_TERMINAL_RELOAD_TEMPLATE = _PARTIALS_DIR / "terminal_reload.html"


def _templates_loading(url: str) -> list[str]:
    """
    Name every template file with a tag whose ``hx-get`` is ``url``.

    Returns:
        The matching file names, sorted, so an assertion can name them.

    """
    return sorted(
        path.name
        for path in _PACKAGE_DIR.joinpath("templates").rglob("*.html")
        if any(attrs.get("hx-get") == url for _tag, attrs in template_start_tags(path))
    )


def _terminal_reload_includes() -> list[nodes.Include]:
    """
    Find every include of the terminal-reload partial in ``status.html``.

    Returns:
        The include nodes, in document order.

    """
    return [
        node
        for node in _jinja_tree(_STATUS_TEMPLATE).find_all(nodes.Include)
        if isinstance(node.template, nodes.Const)
        and node.template.value == "partials/terminal_reload.html"
    ]


def _finish_a_job(client: TestClient, state: JobState) -> str:
    """
    Create a job, drive it to `state`, and make it the worker's current.

    Returns:
        The job's id, for a route that names the job in its URL or form.

    """
    job_store = services_of(client.app).job_store
    job = job_store.create_job(profile="default", title="Terminal")
    job_store.update_state(job.id, state, error="disk on fire")
    _adopt_as_current_job(client, job.id)
    return job.id


# The two loaders exactly as terminal_reload.html renders them.  Matched whole,
# not by their hx-get alone: the strip's own cold-start poll also asks
# /api/checks, and that one belongs on the full page.
_HISTORY_RELOAD_DIV = (
    '<div hx-get="/api/jobs/history" hx-target="#history-body" '
    'hx-swap="outerHTML" hx-trigger="load" class="htmx-hidden"></div>'
)
_STRIP_RELOAD_DIV = (
    '<div hx-get="/api/checks" hx-target="#checks-body" '
    'hx-swap="outerHTML" hx-trigger="load" class="htmx-hidden"></div>'
)


class TestTerminalReloadPartial:
    """One partial holds the hidden loaders every terminal status reloads with."""

    def test_the_partial_holds_both_loaders(self) -> None:
        """
        History and the strip are reloaded by the same terminal event.

        The strip loader is here rather than left to the TTL because the paused
        note has to clear the moment the scan ends; waiting out thirty seconds
        would leave the page claiming a scan is still running.
        """
        loaders = [
            attrs.get("hx-get")
            for _tag, attrs in template_start_tags(_TERMINAL_RELOAD_TEMPLATE)
            if attrs.get("hx-trigger") == "load" and attrs.get("class") == "htmx-hidden"
        ]
        assert loaders == ["/api/jobs/history", "/api/checks"]

    def test_status_html_holds_no_loader_markup_of_its_own(self) -> None:
        """``status.html`` holds no loader markup outside the partial."""
        tags = template_start_tags(_STATUS_TEMPLATE)
        assert [
            attrs
            for _tag, attrs in tags
            if "htmx-hidden" in (attrs.get("class") or "").split()
            or attrs.get("hx-get") == "/api/jobs/history"
        ] == []

    def test_every_terminal_branch_includes_it(self) -> None:
        """DONE, FALLBACK, CANCELLED and ERROR all reload the same way."""
        assert len(_terminal_reload_includes()) == 4

    def test_every_include_is_behind_the_terminal_reload_flag(self) -> None:
        """
        The partial is included only when the route asks for it.

        The full page has just rendered history and the strip, so a reload
        there would fetch both again the moment the page is parsed.  Each of the
        four includes has to sit alone inside ``{% if terminal_reload %}``; one
        that did not would reload on the full page for its state alone.
        """
        guarded = [
            node
            for node in _jinja_tree(_STATUS_TEMPLATE).find_all(nodes.If)
            if isinstance(node.test, nodes.Name)
            and node.test.name == "terminal_reload"
            and not node.elif_
            and not node.else_
            and len(node.body) == 1
            and node.body[0] in _terminal_reload_includes()
        ]
        assert len(guarded) == len(_terminal_reload_includes()) == 4

    def test_every_status_poll_response_sets_the_flag(self) -> None:
        """
        Each route rendering the status-poll partial asks for the reload.

        A new route that renders it without the flag would leave a finished
        scan's history row and the strip's paused note on screen until the
        next page load, so the two are counted against each other.
        """
        renders = _template_renders(
            handler_family_tree(), "partials/status_response.html"
        )
        assert renders
        unflagged = [
            lineno
            for lineno, arguments in renders
            if not any(
                _dict_value(argument, "terminal_reload") is True
                for argument in arguments
            )
        ]
        assert unflagged == []

    @pytest.mark.parametrize(
        "state",
        [JobState.DONE, JobState.FALLBACK, JobState.CANCELLED, JobState.ERROR],
    )
    def test_the_full_page_does_not_reload_what_it_just_rendered(
        self, client: TestClient, state: JobState
    ) -> None:
        """GET / with a finished current job carries neither loader."""
        _finish_a_job(client, state)
        page = client.get("/").text
        assert 'id="history-body"' in page
        assert 'id="checks-body"' in page
        assert _HISTORY_RELOAD_DIV not in page
        assert _STRIP_RELOAD_DIV not in page

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/jobs/current/status"),
            ("GET", "/api/jobs/{job_id}/status"),
            ("POST", "/api/flip/continue"),
            ("POST", "/api/flip/abort"),
        ],
    )
    def test_every_status_response_for_a_finished_job_reloads_both(
        self, client: TestClient, method: str, path: str
    ) -> None:
        """
        The same finished job, rendered as a status response, carries both.

        A flip answer for a job that is not waiting claims nothing and renders
        the current job, which is the finished one here.
        """
        job_id = _finish_a_job(client, JobState.DONE)
        if method == "GET":
            response = client.get(path.format(job_id=job_id))
        else:
            response = client.post(path, data={"job_id": job_id})
        assert response.status_code == 200
        assert _HISTORY_RELOAD_DIV in response.text
        assert _STRIP_RELOAD_DIV in response.text

    def test_the_history_loader_lives_in_exactly_two_templates(self) -> None:
        """
        One copy for the status branches, one for the request-error slot.

        ``partials/error.html`` keeps its own because an error is rendered
        without a status area at all, so it cannot share the status partial's.
        """
        assert _templates_loading("/api/jobs/history") == [
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
    """Which responses carry the strip out of band, asserted response by response."""

    def test_a_scan_submit_carries_the_strip_out_of_band(
        self, client: TestClient
    ) -> None:
        """
        The paused note appears the instant the scan is accepted.

        Without this the strip would keep claiming a live Scanner reading for
        up to a full TTL after the scanner became unavailable.
        """
        response = client.post(
            "/api/scan", data={"profile": "default", "title": "OOB proof"}
        )
        assert response.status_code == 200
        assert _OOB_CHECKS.search(response.text) is not None

    def test_a_scan_submit_on_an_idle_server_says_the_checks_are_paused(
        self, client: TestClient
    ) -> None:
        """
        The strip the submit carries is rendered as a scan in progress.

        It is built before the job exists, when the worker's record still
        says idle; read from there, the paused note it is carried for would
        never appear.
        """
        assert services_of(client.app).worker.current_job_id is None
        response = client.post(
            "/api/scan", data={"profile": "default", "title": "Paused proof"}
        )
        assert response.status_code == 200
        assert PAUSED_PREFIX in response.text

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

    def test_an_htmx_error_response_never_carries_the_strip(
        self, client: TestClient
    ) -> None:
        """
        An htmx error response never carries the strip out of band.

        The error lands in the message slot. It may hand focus back with an
        out-of-band Scan button, but the strip is not its to re-render.
        """
        response = client.post(
            "/api/scan",
            data={"profile": "no-such-profile", "title": ""},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 422
        assert response.headers["HX-Retarget"] == "#status-message"
        assert _OOB_CHECKS.search(response.text) is None

    def test_a_refresh_does_not_re_render_the_scan_button(
        self, client: TestClient
    ) -> None:
        """
        The strip is not a status response, so it owns no button state.

        An OOB ``#scan-btn`` here would let a Check again click re-enable a
        button the server had deliberately disabled.
        """
        response = client.post("/api/checks/refresh")
        assert _OOB_SCAN_BTN.search(response.text) is None
        assert not [
            attributes
            for _tag, attributes in markup_start_tags(response.text)
            if attributes.get("id") == "scan-btn"
        ]

    def test_the_flag_is_set_by_exactly_one_handler(self) -> None:
        """
        The flag is turned on in one place and defaulted off in one place.

        A second setter would be a second response re-rendering the strip, and
        the reason the flag exists at all is that only the submit has news.
        It is a context-dict key rather than a keyword argument because
        ``TemplateResponse`` takes its context as a mapping.
        """
        tree = handler_family_tree()
        assert _context_values(tree, "refresh_checks").count(True) == 1
        # Defaulted off once in ``status_context``, and forced off once more
        # in the canonical poll rendering ``_status_token`` hashes.  That
        # second one is the shape of a poll, never a response anybody is sent,
        # so it cannot re-render the strip.
        assert _context_values(tree, "refresh_checks").count(False) == 2


def _paperless_row(status: ConnectionStatus) -> CheckResult:
    """
    Build the red Paperless row the registry draws for one probe outcome.

    Args:
        status: What the connection test found.

    Returns:
        The row, with the message and next step the registry gives it.

    """
    return CheckResult(
        key=CheckKey.PAPERLESS,
        state=CheckState.FAIL,
        message=connection_status_message(status),
        next_step=checks_module._paperless_next_step(status),
    )


def _check_failed_row() -> CheckResult:
    """
    Build the row the registry draws for a check that raised.

    Returns:
        The red Paperless row a raising probe becomes.

    """
    return CheckResult(
        key=CheckKey.PAPERLESS,
        state=CheckState.FAIL,
        message=checks_module._CHECK_FAILED_MESSAGE,
        next_step=checks_module._CHECK_FAILED_NEXT_STEP,
    )


class TestTheStripNamesItsOwnRetry:
    """
    One row, two endings: the strip keeps saying press Check again.

    The registry stores a retrying next step with a placeholder, and
    ``saneless doctor`` renders it as "run saneless doctor again".  The strip
    renders the same row with the button that is beside it, word for word as
    it always has, and never shows the placeholder itself.
    """

    @pytest.mark.parametrize(
        ("row", "expected"),
        [
            pytest.param(
                checks_module._scanner_configured_missing_row,
                "Check it is switched on and connected, then press Check again. "
                "If saneless devices does not list it, set [scanner] device to "
                "one it lists, then restart saneless.",
                id="scanner-not-found",
            ),
            pytest.param(
                checks_module._scanner_listing_timed_out_row,
                "Check the scanner, and its scanner host if it has one, are "
                "switched on and reachable, then press Check again.",
                id="listing-timed-out",
            ),
            pytest.param(
                checks_module._scanner_listing_crashed_row,
                "Press Check again.",
                id="listing-crashed",
            ),
            pytest.param(
                lambda: _paperless_row(ConnectionStatus.UNREACHABLE),
                "Check paperless-ngx is running and on the network, then press "
                "Check again.",
                id="paperless-unreachable",
            ),
            pytest.param(
                lambda: _paperless_row(ConnectionStatus.SERVER_ERROR),
                "Check paperless-ngx is healthy, then press Check again.",
                id="paperless-500",
            ),
            pytest.param(
                _check_failed_row,
                "Restart saneless, then press Check again.",
                id="check-raised",
            ),
        ],
    )
    def test_the_strip_still_says_check_again(
        self,
        client: TestClient,
        row: Callable[[], CheckResult],
        expected: str,
    ) -> None:
        """
        The row's next step names the button, and no placeholder reaches the page.

        Args:
            client: A client over the real app.
            row: Builds the row the registry would draw.
            expected: The sentence the strip shows under it.

        """
        failing = row()
        services_of(client.app).checks.store(
            tuple(
                failing
                if key is failing.key
                else CheckResult(key=key, state=CheckState.OK, message="Fine.")
                for key in CheckKey
            )
        )
        rendered = _row_named(client.get("/").text, check_name(failing.key))
        assert f'<span class="check-next">{expected}</span>' in rendered
        assert "{" not in rendered
        assert "saneless doctor again" not in rendered

    def test_the_strip_renders_next_steps_through_the_shared_function(self) -> None:
        """
        The template reaches the one renderer doctor uses, not a copy of it.

        The filter is the vocabulary function itself, so the strip's ending and
        doctor's cannot be chosen by two implementations.
        """
        templates = app_module._build_templates()
        assert templates.env.filters["check_step"] is render_check_step
        assert "check_step" in _filters_used(_CHECKS_TEMPLATE)
        assert templates.env.globals["CheckSurface"] is CheckSurface


class TestTheStripNeverShowsTheRedirectTarget:
    """
    Where paperless-ngx redirected to is terminal-only.

    The registry keeps the sanitised target on the row for ``saneless doctor``
    to print, and the strip is a page anyone on the LAN can load, so the
    template never renders it.
    """

    def test_the_strip_never_shows_the_redirect_target(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The row the registry draws carries the target, and the page does not.

        Args:
            client: A client over the real app.
            monkeypatch: pytest's patcher.

        """
        app = _app(client)
        target = "https://paperless.example/"
        monkeypatch.setattr(
            services_of(app).paperless,
            "probe_connection",
            lambda *, timeout=None: ConnectionProbe(
                ConnectionStatus.REDIRECTED, redirect_target=target
            ),
        )
        row = checks_module._check_paperless(
            CheckContext(
                settings=services_of(app).settings,
                scanner=None,
                paperless=services_of(app).paperless,
                profile_storage=ProfileStorage.PERSISTED,
            )
        )
        assert target in row.terminal_detail
        services_of(app).checks.store(
            tuple(
                row
                if key is row.key
                else CheckResult(key=key, state=CheckState.OK, message="Fine.")
                for key in CheckKey
            )
        )
        page = client.get("/").text
        rendered = _row_named(page, check_name(CheckKey.PAPERLESS))
        assert connection_status_message(ConnectionStatus.REDIRECTED) in rendered
        assert "paperless.example" not in page
        assert "paperless.example" not in client.get("/api/checks").text
