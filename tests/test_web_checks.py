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
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saneless.checks import (
    CHECKING_MESSAGE,
    CheckKey,
    CheckResult,
    CheckState,
)
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.scanner.base import DeviceInfo
from saneless.vocabulary import ConnectionStatus, local_time
from saneless.web import routes as routes_module
from saneless.web.app import create_app
from tests.conftest import StubScannerBackend

if TYPE_CHECKING:
    from collections.abc import Iterator

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

    def __call__(self, context: CheckContext) -> tuple[CheckResult, ...]:
        """
        Record the call and return one synthetic row per check.

        Args:
            context: The context the route built for this probe.

        Returns:
            One ``OK`` result per ``CheckKey`` member, in member order.

        """
        self.calls += 1
        self.contexts.append(context)
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


def _make_app(tmp_path: Path, *, stub_refresher: bool = True) -> FastAPI:
    """
    Build a real app with a stub scanner and no network calls, not yet started.

    Args:
        tmp_path: The directory the data and temp folders live in.
        stub_refresher: Whether to put ``_RecordingRefresher`` in front of the
            real one.  True for every test but the wiring test, because a real
            refresher that has been stamped will probe on its next tick and
            fill a cache a cold-start assertion just emptied.

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
    # Offline, and CONNECTED so the Paperless row is not the one that varies.
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
    Replace the route module's ``run_checks`` with a counting stand-in.

    The route module's own reference is the one patched, because that is the
    name the handler resolves at call time.  The refresher holds a separate
    reference and is never driven here.

    Returns:
        The spy, whose ``calls`` is the number of probes the request made.

    """
    spy = _ProbeSpy()
    monkeypatch.setattr(routes_module, "run_checks", spy)
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
        assert 'hx-get="/api/checks"' in attrs

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
        gate = _app(client).state.worker.scanner_gate
        gate.acquire()
        try:
            client.post("/api/checks/refresh")
        finally:
            gate.release()
        assert spy.calls == 1
        assert spy.contexts[0].skip_scanner is True

    def test_refresh_when_idle_does_not_skip_the_scanner(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the gate free the scanner row is probed like any other."""
        spy = _spy(monkeypatch)
        client.post("/api/checks/refresh")
        assert spy.contexts[0].skip_scanner is False

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
        gate = _app(client).state.worker.scanner_gate
        gate.acquire()
        try:
            markup = client.post("/api/checks/refresh").text
        finally:
            gate.release()
        assert PAUSED_SCANNER_MESSAGE in markup


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
        ``run_checks`` is called from the refresh handler and nowhere else.

        A second call site in this module would be a second way for a render
        to probe, which is the failure D-04 exists to prevent; a grep is the
        only thing that can see it.
        """
        source = Path(routes_module.__file__).read_text(encoding="utf-8")
        assert source.count("run_checks(") == 1
