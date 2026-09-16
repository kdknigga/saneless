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
    CHECKING_STATE_LABEL,
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
from saneless.vocabulary import ConnectionStatus, JobState, local_time
from saneless.web import app as app_module
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
        ["check_name", "check_state_class", "check_state_glyph", "check_state_label"],
    )
    def test_the_template_reaches_for_each_filter(self, filter_name: str) -> None:
        """All four registered filters are the ones the rows are drawn with."""
        assert filter_name in _template()

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
