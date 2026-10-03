"""
Web UI behaviour in a real browser, against a live uvicorn server.

A session-scoped server runs with a stub scanner. Every test runs behind an
egress gate that fails it on any request not addressed to the test server and
on any Content-Security-Policy violation, so the UI works offline and needs
nothing the policy refuses. A stylesheet whose bytes do not match its
``integrity`` pin is refused silently and reads like a palette fault in bulk;
``test_pico_css_applied`` is the load canary that says so in plain words.

Requires pytest-playwright and the chromium and firefox browsers
(``uv run playwright install chromium firefox``); Firefox runs the reload test only.
"""

from __future__ import annotations

import base64
import io
import json
import re
import socket
import sqlite3
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from itertools import pairwise
from typing import TYPE_CHECKING, Literal, NamedTuple, NoReturn
from urllib.parse import parse_qs, urlencode, urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator
    from contextlib import AbstractContextManager
    from pathlib import Path

    from fastapi import FastAPI
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Dialog,
        Locator,
        Page,
        Playwright,
        Request,
        Response,
        Route,
    )
    from starlette.types import ASGIApp, Receive, Scope, Send

    from saneless.job import Job, JobStore
    from saneless.scanner.base import PageSink, ScanSettings
    from tests.conftest import SocketGuard

import httpx2
import pytest
import uvicorn
from PIL import Image
from playwright.sync_api import expect

from saneless.auto_profiles import generate_profiles
from saneless.checks import (
    CHECKING_GLYPH,
    CHECKING_MESSAGE,
    CHECKING_STATE_CLASS,
    POLL_ATTEMPT_CAP,
    POLL_GAVE_UP_LINE,
    SKIPPED_STATE_LABEL,
    CheckKey,
    CheckState,
    check_name,
    check_state_class,
    check_state_glyph,
    check_state_label,
)
from saneless.config import (
    CONFIG_FILENAME,
    LEGACY_CONFIG_FILENAME,
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
    WebConfig,
    discover_config,
    resolve_job_title,
)
from saneless.job import JobResult
from saneless.paperless import ApiDelivery, TaskFiled, UploadResult
from saneless.scanner.base import DeviceCapabilities, DeviceInfo, ScanBatch
from saneless.vocabulary import (
    CORRESPONDENTS_LOADING,
    CORRESPONDENTS_UNAVAILABLE,
    HIDDEN_JOB_TITLE,
    HIDDEN_PRESERVED_ERROR,
    LOST_CONTACT_LINE,
    MULTI_PAGE_DISABLED_REASON,
    MULTI_PAGE_HELP,
    MULTI_PAGE_LABEL,
    NO_SCRIPT_HEADING,
    NO_SCRIPT_LINE,
    NO_SCRIPT_PAGE_TITLE,
    NOTHING_TO_FINISH,
    SCAN_BLOCKED_REASON,
    TAG_FILTER_LABEL,
    TAGS_LOADING,
    TAGS_UNAVAILABLE,
    TERMINAL_STATES,
    TITLE_MAX_LENGTH,
    UNCONFIRMED_FILING_LABEL,
    ErrorCategory,
    FlipOutcome,
    JobState,
    PassAnswer,
    PassPrompt,
    PassWait,
    ScanOutcome,
    WorkerHealth,
    abort_question,
    busy_line,
    error_message,
    error_next_step,
    flip_answer_label,
    flip_heading,
    job_label,
    local_time,
    non_owner_wait_line,
    pass_answer_label,
    progress_label,
    scan_hold_reason,
)
from saneless.web import cache as cache_module
from saneless.web import routes as routes_module
from saneless.web.app import TEMPLATE_DIR, create_app
from saneless.worker import WorkerFlipCoordinator, WorkerPassCoordinator
from tests.conftest import (
    StubScannerBackend,
    browser_quiet_window,
    poll_until,
    refusing_paperless_client,
    scan_batch,
    wait_for_state,
)
from tests.fake_clock import FakeClock

# Every palette value these tests assert against, in one place. All of them are
# valid only for Pico 2.1.1, the version vendored as
# static/vendor/pico-2.1.1.min.css. .github/dependabot.yml watches
# "github-actions" only, so that file sits outside every automated update path.
#
# The drift is caught on the enforcing boundary: the CI `browser` job runs this
# module, offline behind the egress gate. A Pico bump is therefore a vendoring
# change -- new file, new `integrity` in base.html, new hash in
# tests/test_vendor_assets.py -- plus these literals. Collecting the expectations
# here makes that last part a one-line edit with a named reason instead of four
# scattered literals that have to be found by grep.
_PICO_SURFACE = {"light": "rgb(255, 255, 255)", "dark": "rgb(19, 23, 31)"}
"""Pico's page surface under each colour scheme, as the browser computes it."""

_AMBER = {"light": "rgb(161, 98, 7)", "dark": "rgb(202, 138, 4)"}
"""The app's fallback amber (#a16207 / #ca8a04, from app.css), as computed."""

_ERROR_RED = {"light": "rgb(136, 57, 53)", "dark": "rgb(206, 126, 123)"}
"""Pico's ``--pico-del-color`` behind ``.status-error``, as computed."""

_MUTED = {"light": "rgb(100, 107, 121)", "dark": "rgb(123, 132, 149)"}
"""Pico's ``--pico-muted-color`` (#646b79 / #7b8495) behind ``.status-cancelled``."""

_SUCCESS_GREEN = {"light": "rgb(29, 106, 84)", "dark": "rgb(98, 175, 154)"}
"""Pico's ``--pico-ins-color`` behind ``.check-ok``, as computed."""

# The status strip's three verdict colours, each pinned to the Pico token its
# state reads. The amber is the existing _AMBER rather than a second
# pair of literals: .check-warn and .status-fallback share one custom property
# on purpose, so a drift in either must be reported by both.
_CHECK_STATE_COLOURS = {
    "check-ok": _SUCCESS_GREEN,
    "check-warn": _AMBER,
    "check-fail": _ERROR_RED,
}


_SCAN_GATE_TIMEOUT = 30.0
"""Longest a closed gate holds ``scan_pages``, so a test that forgets it cannot hang."""

_RENDERING_BROWSER = "the-browser-these-rows-were-staged-for"
"""
The owner token a staged row records when its test reads the row's detail.

A job's title, preview and error or warning text reach only the browser that
started it, so a test that stages a row straight into the store and then reads
its title has to be that browser: ``_as_owner`` hands the page the cookie.
"""


def _as_owner(page: Page, url: str) -> str:
    """
    Give the page's browser the owner cookie that ``_RENDERING_BROWSER`` rows record.

    Args:
        page: The page whose context should hold the cookie.
        url: The server the cookie is for.

    Returns:
        The token, for ``create_job(owner_token=...)``.

    """
    page.context.add_cookies(
        [{"name": "saneless_owner", "value": _RENDERING_BROWSER, "url": url}]
    )
    return _RENDERING_BROWSER


def _spool_pages(sink: PageSink, count: int, resolution: int) -> ScanBatch:
    """
    Spool ``count`` pages with content on them and batch the records back.

    Each page is half black rather than blank: the default profile's empty-page
    detection would drop an all-white page and end the job in ERROR, and the
    browser tests need scans that can reach DONE.

    Shared by the one-page stub below and the manual-duplex stub further down,
    which differ only in how many sheets a pass produces and in which pass the
    gate holds.

    Args:
        sink: The pipeline's own sink, which receives each page.
        count: How many sheets this pass produces.
        resolution: The resolution to report the device settled on.

    Returns:
        A batch of the records the sink returned, in order.

    """
    records = [Image.new("RGB", (100, 100), "white") for _ in range(count)]
    for page in records:
        page.paste((0, 0, 0), (0, 0, 50, 100))
    return scan_batch(
        [sink.add(page, dpi=resolution) for page in records],
        resolution=resolution,
    )


class _BrowserTestScanner(StubScannerBackend):
    """
    Concrete scanner stub for browser tests, with a gate on ``scan_pages``.

    The gate is open by default, so a scan returns at once. A test closes it
    (``gate.clear()``) to hold a job in SCANNING while it looks at the page, and
    opens it again (``gate.set()``) to let the job finish. The wait is bounded,
    so a gate left closed by a failing test ends the scan rather than the run.

    Capabilities come from ``StubScannerBackend``, a flatbed at 300 dpi in
    colour. Only ``get_devices`` differs, because these tests want a device
    with a recognisable name.
    """

    def __init__(self) -> None:
        """Create the stub with its gate open."""
        self.gate = threading.Event()
        self.gate.set()

    def get_devices(self) -> list[DeviceInfo]:
        """
        Report a single fake device.

        Returns:
            A one-element list naming the browser test device.

        """
        return [
            DeviceInfo(
                name="test:browser:001",
                vendor="Test",
                model="Browser Scanner",
                device_type="virtual",
            ),
        ]

    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Wait for the gate, then spool one page with content on it.

        Args:
            device_id: Ignored; this stub scans nothing real.
            settings: Only ``resolution`` is used, as the pages' dpi and the batch's.
            sink: The pipeline's own sink, which receives the page.

        Returns:
            A batch of the single record the sink returned.

        """
        self.gate.wait(timeout=_SCAN_GATE_TIMEOUT)
        return _spool_pages(sink, 1, settings.resolution)


class _BrowserServer(NamedTuple):
    """
    The live test server: its base URL and the app object behind it.

    Carrying the app alongside the URL lets a test drive a job to FALLBACK and
    then look at it through a real browser; carrying the scanner lets a test
    hold a real scan in SCANNING through its gate.
    """

    url: str
    app: FastAPI
    scanner: _BrowserTestScanner


# The two sentences the profile description swap moves between.  They are
# written here rather than taken from the generator so the browser proof is
# about the swap and not about what auto-profiles happens to produce.
_FLATBED_DESCRIPTION = "Scans one page from the glass."
_FEEDER_DESCRIPTION = "Scans both sides of every page using the document feeder."


def _browser_test_settings(tmp_dir: Path) -> Settings:
    """
    Build the settings every browser test server runs with, rooted at ``tmp_dir``.

    Two profiles are configured so the worker never generates profiles at
    startup, and Paperless names a host that cannot resolve. The servers build
    their client over the refusing test transport as well, so an unstubbed
    upload fails at once and nothing reaches the network.
    """
    return Settings(
        scanner=ScannerConfig(device="test:browser:001"),
        paperless=PaperlessConfig(
            url="http://paperless.invalid",
            token="fake-token",
        ),
        output=OutputConfig(
            tmp_dir=str(tmp_dir),
            data_dir=str(tmp_dir),
            log_file=str(tmp_dir / "saneless.log"),
        ),
        profiles={
            # Both carry a description and neither carries a human name, so
            # the live description swap has two distinct sentences to prove
            # itself with while the option text stays the profile name, which
            # is what the dropdown test above reads.
            "default": ProfileConfig(description=_FLATBED_DESCRIPTION),
            "duplex": ProfileConfig(
                source="ADF Manual Duplex", description=_FEEDER_DESCRIPTION
            ),
        },
    )


class _RunningUvicorn(NamedTuple):
    """A uvicorn server running on a daemon thread, and the socket it serves."""

    server: uvicorn.Server
    thread: threading.Thread
    sock: socket.socket
    port: int


def _start_uvicorn(app: ASGIApp, host: str) -> _RunningUvicorn:
    """
    Run ``app`` under uvicorn on a daemon thread, bound to ``host`` on a free port.

    The socket is bound and listening before the thread starts, so the port is
    known at once and a connection made before uvicorn is serving waits in the
    listen queue instead of being refused.  uvicorn accepts nothing until its
    lifespan start-up has finished, so one request is enough to know it has:
    crash recovery, the start-up prune and the worker have all run, and a test
    that writes to the job store next cannot have its row failed by recovery.
    That request is answered, not polled for, so nothing here sleeps.

    Raises:
        RuntimeError: When uvicorn does not answer that first request.

    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, 0))
    sock.listen()
    port = sock.getsockname()[1]
    # uvicorn.Server.capture_signals already skips signal handling when it runs
    # in a non-main thread, so no special config is needed.
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    running = _RunningUvicorn(server=server, thread=thread, sock=sock, port=port)
    try:
        httpx2.get(
            f"http://127.0.0.1:{port}/static/app.css", timeout=10
        ).raise_for_status()
    except httpx2.HTTPError as exc:
        _stop_uvicorn(running)
        msg = f"Uvicorn server failed to start: {exc}"
        raise RuntimeError(msg) from exc
    return running


def _stop_uvicorn(running: _RunningUvicorn) -> None:
    """Stop a server started by ``_start_uvicorn`` and assert its thread ended."""
    running.server.should_exit = True
    running.thread.join(timeout=5)
    # uvicorn closes the socket on a normal shutdown, but not when its start-up
    # failed; closing it again is harmless.
    running.sock.close()
    # join() reports nothing on timeout. A uvicorn thread that fails to stop
    # leaves a bound port and a live app behind for the rest of the session,
    # so the outcome is asserted rather than discarded.
    assert not running.thread.is_alive(), "uvicorn test server did not shut down"


@contextmanager
def _serve(
    settings: Settings, scanner: _BrowserTestScanner
) -> Generator[_BrowserServer]:
    """
    Run a private app on loopback for the length of one test, then shut it down.

    The tests that need a *cold* check cache, a scan held mid-flight or a
    second address cannot use the session server -- its cache is warm within a
    second of the first page load and its scanner is shared with every other
    test in the module.

    The caller must add the yielded URL to ``egress_allowlist``: the gate knows
    only the session server, and a page served from here would otherwise be
    aborted on its very first request.

    Args:
        settings: The configuration the private app runs with.
        scanner: The stub backend it scans through.

    Yields:
        The running server, its app and its scanner.

    """
    app = create_app(settings, scanner)
    running = _start_uvicorn(app, host="127.0.0.1")
    try:
        yield _BrowserServer(
            url=f"http://127.0.0.1:{running.port}", app=app, scanner=scanner
        )
    finally:
        # Unconditionally, and before the shutdown: lifespan shutdown joins the
        # worker on a 5 s bound, and a gate a failing test left closed would
        # hold that worker inside scan_pages for the stub's own 30 s timeout.
        # The failure would then be reported as a server that would not stop.
        scanner.gate.set()
        _stop_uvicorn(running)


def _create_offline_app(settings: Settings, scanner: _BrowserTestScanner) -> FastAPI:
    """
    Build a session server's app over the refusing Paperless transport.

    A session-scoped server is built before any test's own fixtures run, so
    the suite-wide refusing client never reaches it; it is patched in here,
    for as long as ``create_app`` takes to build the client.

    Returns:
        The app, holding a Paperless client that never opens a socket.

    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "saneless.web.app.PaperlessClient", refusing_paperless_client(FakeClock())
        )
        return create_app(settings, scanner)


@pytest.fixture(scope="session")
def browser_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_BrowserServer]:
    """Start a real uvicorn server for browser tests."""
    tmp_dir = tmp_path_factory.mktemp("browser")
    scanner = _BrowserTestScanner()
    app = _create_offline_app(_browser_test_settings(tmp_dir), scanner)
    running = _start_uvicorn(app, host="127.0.0.1")
    yield _BrowserServer(
        url=f"http://127.0.0.1:{running.port}", app=app, scanner=scanner
    )
    _stop_uvicorn(running)


@pytest.fixture(scope="session")
def browser_server_url(browser_server: _BrowserServer) -> str:
    """Return the live server's base URL, for tests that need nothing else."""
    return browser_server.url


@pytest.fixture
def egress_allowlist(browser_server: _BrowserServer) -> list[str]:
    """
    Return the base URLs a browser test may reach: the test server, and nothing else.

    It is a list rather than a single URL so a test that deliberately serves the
    page from a second origin can append that base; the gate reads the list when
    each request arrives, so an append made inside the test still counts.
    """
    return [browser_server.url]


def _is_allowed(url: str, allowlist: list[str]) -> bool:
    """Say whether ``url`` is addressed to one of the allowlisted base URLs."""
    return any(url == base or url.startswith(base + "/") for base in allowlist)


# A test that needs two browser sessions at once builds its own contexts with
# ``browser.new_context()`` -- the owner/non-owner proof of the flip prompt is
# exactly that shape -- and a hand-made context carries none of the overridden
# ``context`` fixture's routing. Without a gate built here its page could reach
# the real internet in CI and nothing in the suite would notice, because the
# only ``blocked`` list asserted on would belong to a context that test never
# used. The escape is silent, which is what makes it dangerous.
#
# The rule this factory enforces is therefore twofold, and both halves matter:
# every context created in this module must install a gate built here, and
# every one of them must be covered by a ``blocked`` assertion at teardown. One
# list may be shared by several contexts, which is the point -- a single
# assertion then speaks for all of them.
def _make_gate(
    blocked: list[str], allowlist: list[str], seen: list[str]
) -> Callable[[Route], None]:
    """
    Build the no-egress route handler every context in this module installs.

    Args:
        blocked: The list each refused URL is appended to. The caller asserts it
            empty at teardown; sharing one list across contexts is supported and
            is how a multi-context test keeps a single assertion honest.
        allowlist: The base URLs a page may reach. Read when each request
            arrives rather than captured, so a base appended after the gate is
            installed still counts.
        seen: The list every handled URL is appended to, allowed or refused
            alike, so it answers "did this gate handle any traffic at all?".
            An empty ``blocked`` list means nothing on its own; an empty
            ``blocked`` beside a non-empty ``seen`` means the gate was
            installed, saw requests, and refused none of them. Required rather
            than defaulted, so a context added later cannot quietly opt out of
            the very check this records.

    Returns:
        A handler suitable for ``context.route("**/*", ...)``.

    """

    def _gate(route: Route) -> None:
        url = route.request.url
        # Recorded before the decision, so an allowed request counts as traffic
        # just as a refused one does.
        seen.append(url)
        if _is_allowed(url, allowlist):
            route.continue_()
        else:
            blocked.append(url)
            route.abort()

    return _gate


# Reports every Content-Security-Policy violation a page raises to the test.
# It runs as an init script, which the page's policy does not govern, so it is
# in place before the first byte of the page is parsed.
_CSP_LISTENER = """
document.addEventListener("securitypolicyviolation", (event) => {
    window.__reportCsp(`${event.violatedDirective} ${event.blockedURI}`);
});
"""


# The same two-part rule as ``_make_gate``, for the same reason: a context made
# by hand from the ``browser`` fixture carries none of the overridden
# fixture's set-up, so without this factory its pages could break the policy
# and nothing would say so.  Every context this module creates installs a
# listener built here, and every one of them is covered by an assertion at
# teardown that its ``violations`` list is empty.  A browser that refuses
# something under the policy does not fail the page loudly -- an inline style
# is simply not applied, a script simply does not run -- so the listener is
# how the whole browser suite becomes the proof that no page needs anything
# the policy refuses.
#
# The page's report reaches the test only while the test is inside a
# Playwright call, as the egress gate's route handler does.
def _make_csp_gate(context: BrowserContext, violations: list[str]) -> None:
    """
    Record every Content-Security-Policy violation a page in ``context`` raises.

    Args:
        context: The browser context to install the listener in, before any
            of its pages loads.
        violations: The list each violation is appended to, as the violated
            directive and the blocked URI.  The caller asserts it empty at
            teardown; one list may be shared by several contexts.

    """

    def _report(_source: object, text: str) -> None:
        violations.append(text)

    context.expose_binding("__reportCsp", _report)
    context.add_init_script(_CSP_LISTENER)


@pytest.fixture
def csp_violations() -> list[str]:
    """
    Return the list the ``context`` fixture's policy listener records into.

    A fixture of its own so that the one test which provokes a violation on
    purpose can read the list, and clear it, before teardown asserts it empty.
    """
    return []


@pytest.fixture
def context(
    context: BrowserContext, egress_allowlist: list[str], csp_violations: list[str]
) -> Iterator[BrowserContext]:
    """
    Route every request the page makes through a no-egress gate.

    This overrides pytest-playwright's ``context`` fixture, so every ``page`` in
    this module -- the dark-mode tests included -- is built from a context that
    continues requests addressed to the test server and aborts and records
    everything else. The test then fails if anything was recorded. That proves
    the UI needs no internet rather than assuming it from the network the run
    happens to have, and it is what lets every browser test run offline in the
    CI ``browser`` job.

    The gate itself comes from ``_make_gate`` rather than being written here, so
    a hand-made context can install the identical one; see that factory's note.

    Teardown asserts the gate saw traffic before it asserts nothing was blocked,
    because the second assertion alone would pass over a context that was never
    routed at all -- an empty list is what a gate that does not exist produces.

    The context also records every Content-Security-Policy violation its pages
    raise (``_make_csp_gate``), and the test fails if there was any, so every
    browser test is also a check that the page runs under the policy.
    ``TestTheCspGate`` shows the listener does record one.
    """
    blocked: list[str] = []
    seen: list[str] = []
    context.route("**/*", _make_gate(blocked, egress_allowlist, seen))
    _make_csp_gate(context, csp_violations)
    yield context
    # Order matters: an ungated context must be reported as ungated, not handed
    # the clean bill of health an empty ``blocked`` list would otherwise give it.
    assert seen, (
        "the gate handled no request at all, so the no-egress assertion below "
        "would have passed for a context whose routing was never installed"
    )
    assert blocked == [], f"the page tried to reach the network: {blocked}"
    assert csp_violations == [], (
        f"the page violated its Content-Security-Policy: {csp_violations}"
    )


@pytest.fixture
def delivering_paperless(
    browser_server: _BrowserServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Make the live app's Paperless client deliver every upload at once.

    The worker holds the same client instance as ``app.state.paperless``, so
    patching its methods affects real scans. Without this the refusing test
    transport fails the upload and the job ends ERROR, which is not the outcome
    under test. ``poll_task``
    returns a filed task, as a real one does when paperless-ngx files the
    document. ``monkeypatch`` restores both methods at teardown.
    """
    _make_paperless_deliver(browser_server.app, monkeypatch)


def _make_paperless_deliver(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``app``'s started Paperless client so every upload is delivered at once."""
    paperless = app.state.paperless

    def _upload_document(*_args: object, **_kwargs: object) -> UploadResult:
        return ApiDelivery(task_id="browser-test-task")

    def _poll_task(*_args: object, **_kwargs: object) -> TaskFiled:
        return TaskFiled(task={"status": "SUCCESS"})

    monkeypatch.setattr(paperless, "upload_document", _upload_document)
    monkeypatch.setattr(paperless, "poll_task", _poll_task)


class _ScanHarness(NamedTuple):
    """What a test that runs real scans needs: the server and the jobs before it."""

    server: _BrowserServer
    job_store: JobStore
    existing_job_ids: frozenset[str]

    def created_job_ids(self) -> list[str]:
        """Return the ids of jobs created since the harness was set up."""
        return [
            job.id
            for job in self.job_store.list_recent(100)
            if job.id not in self.existing_job_ids
        ]


_JOB_FINISH_TIMEOUT = 30.0
"""How long teardown waits for a scan a test started to reach a terminal state."""


@pytest.fixture
def scan_harness(
    browser_server: _BrowserServer,
    delivering_paperless: None,
) -> Iterator[_ScanHarness]:
    """
    Let a test run real scans, then put the session-scoped server back to idle.

    Depends on ``delivering_paperless`` so its teardown runs first: every job
    is finished while uploads are still stubbed. The teardown opens the scanner
    gate, waits for every job the test created to reach a terminal state, then
    deletes those rows and clears the worker's pointer, the same discipline as
    ``fallback_page`` -- otherwise the most-recent-job fallback would show this
    test's job on every later test's supposedly idle page.
    """
    job_store: JobStore = browser_server.app.state.job_store
    harness = _ScanHarness(
        server=browser_server,
        job_store=job_store,
        existing_job_ids=frozenset(job.id for job in job_store.list_recent(100)),
    )
    try:
        yield harness
    finally:
        browser_server.scanner.gate.set()
        created = harness.created_job_ids()
        try:
            for job_id in created:
                wait_for_state(
                    job_store, job_id, TERMINAL_STATES, timeout=_JOB_FINISH_TIMEOUT
                )
        finally:
            for job_id in created:
                job_store.delete_job(job_id)
            browser_server.app.state.worker._current_job_id = None
        # Reached only when every job finished, so it cannot mask the failure
        # that stopped a wait. A row left behind here would follow every later
        # test onto its supposedly idle page.
        assert harness.created_job_ids() == [], "a test's job rows were not deleted"


# The probe the test below aims at the gate. ``.invalid`` is reserved so that
# it can never resolve, so even a gate that had stopped aborting would contact
# nothing real: the test would fail on an empty ``blocked`` list rather than
# putting a request on somebody's server.
_OFF_ALLOWLIST_URL = "http://egress-probe.invalid/should-never-be-reached"
_EGRESS_PROBE_BUDGET = 5.0


@pytest.mark.browser
class TestTheEgressGateRefuses:
    """
    The egress gate refuses, and records, a request outside the allowlist.

    Every other browser test asserts that nothing was blocked, which is a claim
    about the page. This one asserts that something *was* blocked, which is a
    claim about the gate: that its abort path still fires and still records
    what it refused. Without it an empty ``blocked`` list could not distinguish
    a page wanting no internet from a gate that had quietly stopped refusing.
    """

    def test_a_request_outside_the_allowlist_is_recorded_and_aborted(
        self,
        browser: Browser,
        browser_server: _BrowserServer,
        egress_allowlist: list[str],
    ) -> None:
        """
        A navigation aimed off the allowlist is recorded and never leaves.

        The page is served by the test server first, so the probe is issued by
        a live document through the same gate every other browser test relies
        on. It is a navigation rather than a ``fetch``: the page's own
        Content-Security-Policy refuses a ``fetch`` to another origin before
        any request is made, so a ``fetch`` would never reach the gate. A
        top-level navigation is the one way out that policy does not govern,
        which is why the gate is still needed.
        """
        # Not the module ``context`` fixture: its teardown asserts ``blocked``
        # is empty, and refusing something on purpose is this test's subject.
        # The gate installed here is the identical one, which is the reason
        # ``_make_gate`` is a factory rather than a closure in that fixture.
        blocked: list[str] = []
        seen: list[str] = []
        violations: list[str] = []
        ctx = browser.new_context()
        try:
            ctx.route("**/*", _make_gate(blocked, egress_allowlist, seen))
            _make_csp_gate(ctx, violations)
            page = ctx.new_page()
            page.goto(browser_server.url)
            # Waiting for the failed request is what lets the gate run at all:
            # the sync API only dispatches route handlers while the caller is
            # inside a Playwright call.  The navigation is started from a
            # timer so that the evaluate call has returned before the page
            # starts to leave.
            with page.expect_event(
                "requestfailed",
                lambda request: request.url == _OFF_ALLOWLIST_URL,
                timeout=_EGRESS_PROBE_BUDGET * 1000,
            ):
                page.evaluate(
                    "(url) => { setTimeout(() => window.location.assign(url), 0); }",
                    _OFF_ALLOWLIST_URL,
                )
            assert blocked == [_OFF_ALLOWLIST_URL], (
                f"the probe, and only the probe, should have been refused: {blocked}"
            )
            assert violations == [], (
                f"the page violated its Content-Security-Policy: {violations}"
            )
        finally:
            ctx.close()


_CSP_PROBE_BUDGET = 5.0


@pytest.mark.browser
class TestTheCspGate:
    """
    The policy listener records a violation, and htmx runs inside the policy.

    Every other browser test asserts that its pages raised no violation, which
    is a claim about the pages.  The first test here asserts that a violation
    *was* recorded, which is a claim about the listener: without it an empty
    list could not tell a page that keeps to the policy from a listener that
    was never installed, or a policy that was never sent.
    """

    def test_an_inline_style_is_recorded_as_a_csp_violation(
        self,
        page: Page,
        browser_server_url: str,
        csp_violations: list[str],
    ) -> None:
        """
        An inline style set from script is refused and recorded, exactly once.

        The page's policy has no ``style-src``, so ``default-src 'self'``
        governs style attributes and refuses them.  The list is cleared once
        the violation has been seen, so the fixture's teardown still checks
        that nothing else was recorded.
        """
        page.goto(browser_server_url)
        assert csp_violations == [], csp_violations
        page.evaluate("document.body.setAttribute('style', 'color: red')")

        def violation_recorded() -> bool:
            # The round trip lets Playwright deliver the page's report, as the
            # egress probe's poll does for its route handler.
            page.evaluate("0")
            return bool(csp_violations)

        assert poll_until(violation_recorded, budget=_CSP_PROBE_BUDGET), (
            "the listener recorded no violation for an inline style"
        )
        assert len(csp_violations) == 1, csp_violations
        assert csp_violations[0].startswith("style-src"), csp_violations
        csp_violations.clear()

    def test_htmx_config_turns_off_eval_and_injects_no_style(
        self, page: Page, browser_server_url: str
    ) -> None:
        """
        The page runs htmx with eval and script tags off, and without its style.

        The meta configuration is read before htmx would inject its indicator
        style, so the switch takes effect on the first load.
        """
        page.goto(browser_server_url)
        assert page.evaluate("htmx.config.allowEval") is False
        assert page.evaluate("htmx.config.allowScriptTags") is False
        assert page.locator("head style").count() == 0


@pytest.mark.browser
class TestBrowserRendering:
    """The page loads styled by Pico, with every scan form control present."""

    def test_page_loads_with_title(self, page: Page, browser_server_url: str) -> None:
        """Main page loads and has the saneless title."""
        page.goto(browser_server_url)
        assert "saneless" in page.title().lower()

    def test_pico_css_applied(self, page: Page, browser_server_url: str) -> None:
        """PicoCSS is loaded: <main> is capped and centred by Pico's container."""
        page.goto(browser_server_url)
        main = page.locator("main")
        assert main.count() >= 1
        box = main.first.bounding_box()
        assert box is not None
        viewport = page.viewport_size
        assert viewport is not None
        # bounding_box() returns a box for any rendered element, styled or not,
        # so its existence proves nothing. Neither does "x > 0": unstyled,
        # <main> is full-bleed inside <body>'s default 8px margin -- x=8,
        # width=1264 at a 1280px viewport -- so a bare x > 0 passes unstyled as
        # well. Pico's .container caps the width and centres what is left
        # (x=40, width=1200), so it is the *cap* that tells the two states apart.
        assert box["width"] <= viewport["width"] - 64, (
            f"PicoCSS did not load (main spans {box['width']}px of a "
            f"{viewport['width']}px viewport; Pico would cap it well below that)"
        )
        assert box["x"] >= 16, f"PicoCSS did not load (main at x={box['x']})"

    def test_scan_form_elements_present(
        self, page: Page, browser_server_url: str
    ) -> None:
        """Scan form has profile dropdown, title field, and scan button."""
        page.goto(browser_server_url)
        # Profile dropdown
        profile_select = page.locator("select[name='profile']")
        assert profile_select.count() == 1
        # Title input
        title_input = page.locator("input[name='title']")
        assert title_input.count() == 1
        # Scan button
        scan_button = page.locator("button[type='submit'], input[type='submit']")
        assert scan_button.count() >= 1

    def test_profile_dropdown_has_options(
        self, page: Page, browser_server_url: str
    ) -> None:
        """Profile dropdown contains the configured profiles."""
        page.goto(browser_server_url)
        options = page.locator("select[name='profile'] option")
        option_texts = [options.nth(i).text_content() for i in range(options.count())]
        assert any("default" in t.lower() for t in option_texts if t)


@pytest.mark.browser
class TestOfflinePage:
    """
    The page structure the offline UI depends on, read from the live DOM.

    Each of these is a property of what the browser actually loaded and built:
    which htmx ran and with which config, whether a deleted script is still
    fetched, and how an empty alert region lays out. A template string test sees
    the markup, not the result.
    """

    def test_vendored_htmx_is_the_pinned_version_with_error_swapping(
        self, page: Page, browser_server_url: str
    ) -> None:
        """
        The vendored htmx 2.0.10 runs with all three response rules.

        htmx merges the meta config shallowly, so a config holding only the
        ``[45]..`` entry would replace the whole array and stop every 2xx swap;
        the count of three and the error entry's ``swap`` are both asserted. No
        ``data-theme`` and no ``pico.colors`` stylesheet keep the page following
        the OS colour scheme.
        """
        page.goto(browser_server_url)
        assert page.evaluate("htmx.version") == "2.0.10"
        assert page.evaluate("htmx.config.responseHandling.length") == 3
        error_rule_swaps = page.evaluate(
            "htmx.config.responseHandling.find((rule) => rule.code === '[45]..').swap"
        )
        assert error_rule_swaps is True
        assert (
            page.evaluate("document.documentElement.hasAttribute('data-theme')")
            is False
        )
        assert (
            page.evaluate(
                "document.querySelectorAll(\"link[href*='pico.colors']\").length"
            )
            == 0
        )

    def test_app_js_is_gone(self, page: Page, browser_server: _BrowserServer) -> None:
        """
        No ``app.js`` script is referenced, and none is served.

        A ``<script>`` tag pointing at a 404 loads nothing yet is still wrong,
        and a served file would let a cached page run stale button logic, so
        both halves are checked.
        """
        page.goto(browser_server.url)
        assert page.locator("script[src*='app.js']").count() == 0
        response = page.request.get(browser_server.url + "/static/app.js")
        assert response.status == 404

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_status_message_slot_is_empty_and_zero_height(
        self,
        page: Page,
        browser_server_url: str,
        scheme: Literal["light", "dark"],
    ) -> None:
        """
        The request-error slot is one empty alert region taking no space.

        It must be present and empty in the initial HTML so a later insertion is
        announced, and it must not push the status area down while empty.
        """
        page.emulate_media(color_scheme=scheme)
        page.goto(browser_server_url)
        slot = page.locator("#status-message")
        assert slot.count() == 1
        assert slot.get_attribute("role") == "alert"
        assert slot.evaluate("(el) => el.childNodes.length") == 0
        assert slot.evaluate("(el) => el.getBoundingClientRect().height") == 0
        assert (
            page.evaluate("document.querySelectorAll('[id=\"status-message\"]').length")
            == 1
        )
        slot_precedes_status_area = page.evaluate(
            "() => Boolean("
            "document.getElementById('status-message').compareDocumentPosition("
            "document.getElementById('status-area')) "
            "& Node.DOCUMENT_POSITION_FOLLOWING)"
        )
        assert slot_precedes_status_area is True

    def test_title_input_caps_at_118(self, page: Page, browser_server_url: str) -> None:
        """
        The title input stops at the server's 118-character cap.

        118 is what paperless-ngx keeps whole even with " (fronts)" appended.
        The server's 422 stays authoritative; this is what keeps a browser user
        from ever meeting it.
        """
        page.goto(browser_server_url)
        title_input = page.locator("#title-input")
        assert title_input.get_attribute("maxlength") == "118"
        title_input.fill("x" * 300)
        assert len(title_input.input_value()) == 118


@pytest.mark.browser
class TestFlipPromptUI:
    """The flip prompt shows only for a waiting job, and Continue answers it."""

    def test_flip_continue_click_answers_the_waiting_job(
        self, page: Page, browser_server: _BrowserServer
    ) -> None:
        """
        A real Continue click answers its own job and is acknowledged.

        The string tests in ``test_web.py`` see the ``hx-vals`` attribute but not
        what htmx actually sends.  Here Chromium clicks the rendered button: if
        the job id did not reach the route through htmx's form encoding, the
        route would answer 422, nothing would swap, and the coordinator would
        stay unanswered.
        """
        app = browser_server.app
        job_store: JobStore = app.state.job_store
        worker = app.state.worker
        job = job_store.create_job(profile="duplex", title="Flip In Browser")
        job_store.update_state(job.id, JobState.AWAITING_FLIP)
        coordinator = WorkerFlipCoordinator(job.id)
        coordinator.arm()
        worker._current_job_id = job.id
        worker._flip_coordinator = coordinator
        try:
            page.goto(browser_server.url)
            continue_button = page.locator(
                "#status-area button[hx-post='/api/flip/continue']"
            )
            expect(continue_button).to_be_visible()

            continue_button.click()

            status = page.locator("#status-area")
            expect(status).to_contain_text(
                "Flip confirmed. Scanning reverse sides next..."
            )
            expect(status.locator("button")).to_have_count(0)
            assert coordinator.answer is FlipOutcome.CONTINUED
        finally:
            # Session-scoped server and store: clear the worker's pointers and
            # delete the row, as fallback_page does, so later tests see the idle
            # page rather than this job through the most-recent-job fallback.
            worker._flip_coordinator = None
            worker._current_job_id = None
            job_store.delete_job(job.id)


_COUNT_ARRAY_SCAN_BUTTONS = "document.querySelectorAll('[id=\"scan-btn\"]').length"

# Counts the swaps of the two in-form controls the refresh buttons re-fetch.
# htmx sets and clears hx-disabled-elt's `disabled` inside the request's onload
# handler, after the swap and before htmx:afterSettle, so once both events have
# fired an inherited disable has either reached the button or it has not.
# Registered as an init script so the listener exists before anything on the
# page can ask.
#
# afterSettle, not afterRequest: the tag list is swapped outerHTML, and htmx
# fires afterRequest on the element it requested *after* that swap has already
# detached it, so the event never reaches this document-level listener. The
# settle pass runs on the elements that are now in the document, on htmx's
# default 20 ms settle delay -- which is later still, so it remains a sound
# "the inherited disable has had its chance" signal. The correspondent select
# is swapped innerHTML and settles as itself; the new tag list settles under
# the same id the old one carried, because the partial renders its own wrapper.
_RECORD_CONTROL_SWAPS = """
window.__controlSwapsFinished = 0;
document.addEventListener("htmx:afterSettle", (event) => {
    const id = event.detail.elt && event.detail.elt.id;
    if (id === "tags-list" || id === "correspondent-select") {
        window.__controlSwapsFinished += 1;
    }
});
"""


# True once the lazy list load has landed and htmx has settled it: the hold
# line is emptied (or the page has none), neither list still says it is
# loading, and nothing new is still carrying htmx's added class.  The last
# part matters: htmx wires a swapped-in element's own triggers only when it
# settles, so a profile change made before then is heard by nothing.
_LISTS_SETTLED = """
([tagsLoading, correspondentsLoading]) => {
    const text = (id) => {
        const node = document.getElementById(id);
        return node === null ? "" : node.textContent;
    };
    const hold = document.getElementById("scan-hold-reason");
    return (hold === null || hold.textContent === "")
        && !text("tags-list").includes(tagsLoading)
        && !text("correspondent-help").includes(correspondentsLoading)
        && document.querySelector(".htmx-added") === null;
}
"""


def _await_the_lists(page: Page) -> None:
    """
    Wait until the page's lazy list load has landed and settled.

    ``/`` renders the lists loading and Scan held; one request after the page
    brings both and releases Scan.  A test that reads a tick, an option, a
    profile marker or the Scan button once, rather than with a retrying
    ``expect``, reads it after this.

    Args:
        page: A page already loaded on the scan form.

    """
    page.wait_for_function(_LISTS_SETTLED, arg=[TAGS_LOADING, CORRESPONDENTS_LOADING])


def _refresh_both_lists(page: Page) -> None:
    """
    Click the tags and the correspondents refresh buttons and wait for both swaps.

    Needs ``_RECORD_CONTROL_SWAPS`` installed before the page loaded.  The
    lazy list load lands first, and its own swaps are not counted: the wait
    is for two more.

    Args:
        page: The browser page, already on the scan form.

    """
    _await_the_lists(page)
    before = page.evaluate("() => window.__controlSwapsFinished")
    for resource in ("tags", "correspondents"):
        with page.expect_response(
            lambda r, name=resource: r.url.endswith(f"resource={name}")
        ) as refreshed:
            page.click(f"#{resource}-refresh")
        assert refreshed.value.status == 200, resource
    # A function, not a bare expression: Playwright evals a bare expression
    # inside the page, and the page's Content-Security-Policy refuses eval.
    page.wait_for_function(
        "(before) => window.__controlSwapsFinished >= before + 2", arg=before
    )


@pytest.mark.browser
class TestServerOwnedScanButton:
    """
    The server alone owns the Scan button through a real scan, in Chromium.

    The server re-renders the button out of band with every status response
    and no client script touches it, so the button has one owner. These tests
    drive real scans through the live worker and watch the button the user
    sees.
    """

    def _assert_released(self, page: Page) -> None:
        """Assert the terminal state: one enabled ``Scan`` button, not busy."""
        scan_btn = page.locator("#scan-btn")
        expect(scan_btn).to_be_enabled()
        assert (scan_btn.text_content() or "").strip() == "Scan"
        assert scan_btn.get_attribute("aria-busy") is None
        assert page.evaluate(_COUNT_ARRAY_SCAN_BUTTONS) == 1

    def test_scan_button_re_enables_after_a_real_scan(
        self, page: Page, scan_harness: _ScanHarness
    ) -> None:
        """
        Click Scan, reach DONE, and the button is usable again.

        The button is enabled again with no app script on the page and exactly
        one button, so the release is the server's out-of-band render and not
        a duplicate.
        """
        page.goto(scan_harness.server.url)
        assert page.locator("script[src*='app.js']").count() == 0

        page.locator("#scan-btn").click()

        expect(page.locator("#status-area .status-done")).to_be_visible(timeout=15_000)
        self._assert_released(page)

    def test_scan_button_shows_busy_while_the_job_is_held(
        self, page: Page, scan_harness: _ScanHarness
    ) -> None:
        """
        While a scan is in flight the button is disabled and busy.

        The gate holds the job in SCANNING, so the out-of-band button every
        poll delivers has to say so; releasing the gate then has to give the
        button back.
        """
        scanner = scan_harness.server.scanner
        page.goto(scan_harness.server.url)
        scanner.gate.clear()
        try:
            page.locator("#scan-btn").click()

            scan_btn = page.locator("#scan-btn")
            expect(scan_btn).to_be_disabled(timeout=3_000)
            expect(scan_btn).to_have_attribute("aria-busy", "true", timeout=3_000)
            expect(scan_btn).to_have_text("Scanning…", timeout=3_000)
            assert page.evaluate(_COUNT_ARRAY_SCAN_BUTTONS) == 1
        finally:
            scanner.gate.set()

        expect(page.locator("#status-area .status-done")).to_be_visible(timeout=15_000)
        self._assert_released(page)

    def test_document_title_follows_state_and_not_204(
        self, page: Page, scan_harness: _ScanHarness
    ) -> None:
        """
        The tab names the stage, keeps it across unchanged polls, and ends idle.

        While the scan is held every poll is answered 204, and a 204 swaps
        nothing, so a title the page set by hand survives two of them: a poll
        answered 200 would put the state's title back.  The scan ending is a
        change, and it names the outcome; a reload reports the scan as past,
        and the tab is plain ``saneless`` again.
        """
        scanner = scan_harness.server.scanner
        page.goto(scan_harness.server.url)
        expect(page).to_have_title("saneless")
        scanner.gate.clear()
        try:
            page.locator("#scan-btn").click()
            expect(page).to_have_title("Scanning — saneless", timeout=5_000)

            page.evaluate("() => { document.title = 'untouched by a 204'; }")
            for _ in range(2):
                with page.expect_response(
                    lambda r: "/api/jobs/" in r.url and "/status" in r.url,
                    timeout=5_000,
                ) as polled:
                    pass
                assert polled.value.status == HTTPStatus.NO_CONTENT
            assert page.title() == "untouched by a 204"
        finally:
            scanner.gate.set()

        expect(page).to_have_title("Done — saneless", timeout=15_000)
        page.reload()
        expect(page).to_have_title("saneless")
        expect(page.locator("#status-area .last-scan")).to_contain_text(
            "Last scan: ✓ Done: "
        )

    def test_queued_button_reads_queued_in_the_browser(
        self, page: Page, context: BrowserContext, scan_harness: _ScanHarness
    ) -> None:
        """
        A scan pressed while another is held reads Queued, not Scanning.

        The second tab opened on the idle page before the first scan began,
        so its Scan button was still enabled: pressing it queues a job behind
        the held one, and that tab's button and title say so.
        """
        scanner = scan_harness.server.scanner
        second = context.new_page()
        page.goto(scan_harness.server.url)
        second.goto(scan_harness.server.url)
        scanner.gate.clear()
        try:
            page.locator("#scan-btn").click()
            expect(page.locator("#scan-btn")).to_have_text("Scanning…", timeout=5_000)

            second.locator("#scan-btn").click()
            queued = second.locator("#scan-btn")
            expect(queued).to_have_text("Queued…", timeout=5_000)
            expect(queued).to_be_disabled()
            expect(second).to_have_title("Queued — saneless")
            expect(second.locator("#status-area")).to_contain_text("next in line")
        finally:
            scanner.gate.set()

        expect(second.locator("#status-area .status-done")).to_be_visible(
            timeout=15_000
        )

    def test_page_loaded_during_an_active_job_keeps_the_button_disabled(
        self,
        page: Page,
        scan_harness: _ScanHarness,
    ) -> None:
        """
        A page opened mid-scan keeps Scan disabled after its lists refresh.

        The form carries ``hx-disabled-elt="#scan-btn"``. Without
        ``hx-disinherit`` the tags and correspondents refresh buttons inherit
        it, so each of their requests would take charge of the ``disabled``
        attribute on a button the server rendered disabled and release it on
        every refresh during a scan.  What is asserted is this project's
        behaviour and not htmx's: a button the server rendered disabled is
        still disabled after both lists have refreshed.
        """
        server = scan_harness.server
        server.scanner.gate.clear()
        try:
            response = httpx2.post(
                server.url + "/api/scan", data={"profile": "default"}
            )
            assert response.status_code == 200, response.text
            created = scan_harness.created_job_ids()
            assert len(created) == 1, created
            wait_for_state(scan_harness.job_store, created[0], JobState.SCANNING)

            page.add_init_script(_RECORD_CONTROL_SWAPS)
            page.goto(server.url)
            _refresh_both_lists(page)

            # Read once, without retrying. expect(...).to_be_disabled() polls for
            # up to 5 s, and the status poll re-renders the button disabled
            # every second, so a retrying check waits out the trap and passes
            # with it sprung, as it does with hx-disinherit removed.
            assert page.locator("#scan-btn").is_disabled(), (
                "a list refresh during an active scan re-enabled the Scan button"
            )
        finally:
            server.scanner.gate.set()


# Builds one <p> per status class, reads the colour the cascade actually
# resolved, and removes it again. Reading all four from the same live page is
# the only way to compare them: only one status renders at a time, so there is
# never a moment when all four exist in the document on their own.
_PROBE_STATUS_COLOURS = """
() => {
    const out = {};
    const classes = [
        "status-done", "status-error", "status-fallback", "status-cancelled",
    ];
    for (const cls of classes) {
        const probe = document.createElement("p");
        probe.className = cls;
        probe.textContent = "probe";
        document.body.appendChild(probe);
        out[cls] = getComputedStyle(probe).color;
        probe.remove();
    }
    return out;
}
"""

# Resolves Pico's muted token the same way a status class would, through a
# probe's `color`, so the answer is an rgb() string comparable with the probes
# above. Reading the custom property itself returns the declared hex instead.
_PROBE_MUTED_TOKEN = """
() => {
    const probe = document.createElement("p");
    probe.style.color = "var(--pico-muted-color)";
    probe.textContent = "probe";
    document.body.appendChild(probe);
    const colour = getComputedStyle(probe).color;
    probe.remove();
    return colour;
}
"""

_SWAP_STATUS_AREA = """
() => htmx.ajax("GET", "/api/jobs/current/status",
                {target: "#status-area", swap: "outerHTML"})
"""


def _show_the_live_outcome(page: Page, selector: str) -> None:
    """
    Swap in the outcome a poll that watched the job end would have shown.

    A page loaded after a job ended reports it as the last scan, muted and
    with no alert, so the live outcome's colour and markup are reached the
    way the operator who watched the scan reached them: through a status
    response.

    Args:
        page: A page already loaded over a finished job.
        selector: What the live outcome renders, to wait for.

    """
    expect(page.locator("#status-area .last-scan").first).to_be_visible()
    # The lists land first, so the Scan button a test reads next is released
    # or held by the job alone, never by the page's own wait for its lists.
    _await_the_lists(page)
    page.evaluate(_SWAP_STATUS_AREA)
    page.wait_for_selector(selector)


# Reads the page surface from the root element. Pico paints its background on
# :root, so <html> is where the scheme shows up; <body> is transparent in both
# schemes and would read the same whether dark mode engaged or not, so it is
# never the thing measured.
_READ_ROOT_SURFACE = """
() => {
    const style = getComputedStyle(document.documentElement);
    return {background: style.backgroundColor, colorScheme: style.colorScheme};
}
"""

# Places one status probe where status text really lives -- a paragraph in the
# status area or a cell in the history table -- and reads the colour it resolves
# to plus the layers it actually sits on, because a history cell has its own
# surface while a status paragraph shows the page through. The walk collects
# every painted layer out to the first fully opaque one and returns the stack;
# a translucent layer does not hide what is under it, so compositing is left to
# _flatten rather than being decided here. Adding, reading and removing all
# happen in this one call, so no htmx swap can land in between.
_JS_BACKGROUND_STACK_OF = """
    const alphaOf = (value) => {
        if (value === "transparent") return 0;
        if (!value.startsWith("rgba(")) return 1;
        return parseFloat(value.slice(value.lastIndexOf(",") + 1));
    };
    const backgroundStackOf = (start) => {
        // Fully transparent layers paint nothing and are skipped; translucent
        // ones are collected and the walk continues, because what is beneath
        // them still shows through. Only a fully opaque layer ends the walk.
        const backgroundStack = [];
        for (let el = start; el !== null; el = el.parentElement) {
            const value = getComputedStyle(el).backgroundColor;
            const alpha = alphaOf(value);
            if (alpha === 0) continue;
            backgroundStack.push(value);
            if (alpha === 1) break;
        }
        const last = backgroundStack[backgroundStack.length - 1];
        if (backgroundStack.length === 0 || alphaOf(last) < 1) {
            // Nothing out to <html> painted an opaque layer, so what shows
            // through is the browser canvas, which is white.
            backgroundStack.push("rgb(255, 255, 255)");
        }
        return backgroundStack;
    };
"""
"""The background walk shared by the contrast probes, as two JS declarations."""

_PROBE_CONTEXT_CONTRAST = (
    """
({cls, context}) => {
"""
    + _JS_BACKGROUND_STACK_OF
    + """
    const probe = document.createElement(context === "history-cell" ? "td" : "p");
    probe.className = cls;
    probe.textContent = "probe";
    let added = probe;
    if (context === "status-area") {
        document.getElementById("status-area").appendChild(probe);
    } else if (context === "card") {
        // Any <article> is Pico's secondary surface, which is the one a line
        // placed inside a card sits on -- the status strip's card and the Scan
        // card are the same colour, so the first one serves for both.
        document.querySelector("article").appendChild(probe);
    } else {
        const row = document.createElement("tr");
        row.appendChild(probe);
        document.getElementById("history-body").appendChild(row);
        added = row;
    }
    const colour = getComputedStyle(probe).color;
    const backgroundStack = backgroundStackOf(probe);
    added.remove();
    return {colour, backgroundStack};
}
"""
)

# The same measurement taken on an element already in the page rather than on a
# probe: the rendered markup is what is under test, so nothing is added.
_READ_ELEMENT_CONTRAST = (
    """
(selector) => {
"""
    + _JS_BACKGROUND_STACK_OF
    + """
    const element = document.querySelector(selector);
    return {
        colour: getComputedStyle(element).color,
        backgroundStack: backgroundStackOf(element),
    };
}
"""
)


def _parse_rgba(css_colour: str) -> tuple[float, float, float, float]:
    """
    Split an ``rgb()`` or ``rgba()`` string into its channels and its alpha.

    Asserts rather than letting ``str.index`` raise ``ValueError``: if a browser
    ever returns ``color(srgb ...)`` or a bare keyword, the failure should name
    the string it was handed instead of pointing at a slice inside a helper.
    """
    assert css_colour.startswith(("rgb(", "rgba(")), (
        f"expected an rgb()/rgba() colour, got {css_colour!r}"
    )
    inner = css_colour[css_colour.index("(") + 1 : css_colour.index(")")]
    parts = [float(part) for part in inner.split(",")]
    alpha = parts[3] if len(parts) > 3 else 1.0
    return parts[0], parts[1], parts[2], alpha


def _flatten(background_stack: list[str]) -> str:
    """
    Composite a stack of painted layers, nearest first, into one opaque colour.

    The probe walks outward from the status element collecting everything that
    paints, so the stack can carry translucent layers above the surface that
    finally stops it. Reading only the channels of the nearest layer -- which is
    what ignoring alpha does -- measures a background of
    ``rgba(111, 120, 135, 0.0375)``, which renders as near-white over a white
    page, as solid ``#6F7887``. That turns a genuinely passing ratio into a
    reported ~1.1:1: a failure with a number no user would ever experience.
    """
    assert background_stack, "the probe returned no background layers"
    *overlays, base = background_stack
    red, green, blue, alpha = _parse_rgba(base)
    assert alpha == 1.0, f"the bottom layer of {background_stack} is not opaque"
    for layer in reversed(overlays):
        over_red, over_green, over_blue, over_alpha = _parse_rgba(layer)
        red = over_red * over_alpha + red * (1 - over_alpha)
        green = over_green * over_alpha + green * (1 - over_alpha)
        blue = over_blue * over_alpha + blue * (1 - over_alpha)
    return f"rgb({red}, {green}, {blue})"


def _relative_luminance(css_colour: str) -> float:
    """Return the WCAG relative luminance of an ``rgb()`` or ``rgba()`` string."""
    # Alpha is deliberately ignored here: compositing is _flatten's job, and by
    # the time a colour reaches this function it is meant to be opaque.
    red, green, blue, _alpha = _parse_rgba(css_colour)
    channels = [red / 255, green / 255, blue / 255]
    linear = [
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(foreground: str, background: str) -> float:
    """Return the WCAG contrast ratio between two computed CSS colours."""
    first = _relative_luminance(foreground)
    second = _relative_luminance(background)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


class TestContrastHelper:
    """
    The contrast helper reproduces known WCAG ratios.

    The dark-mode contrast tests only mean something if the formula is right.
    A helper that returned a large number for everything would let every one of
    them pass without measuring anything, so these checks tie it to known
    ratios. They need no browser and run in CI.
    """

    def test_black_on_white_is_the_maximum_ratio(self) -> None:
        """Black on white is the largest ratio WCAG defines, 21:1."""
        assert _contrast_ratio("rgb(0, 0, 0)", "rgb(255, 255, 255)") == pytest.approx(
            21.0
        )

    def test_ratio_is_symmetric(self) -> None:
        """Swapping foreground and background does not change the ratio."""
        amber = _AMBER["light"]
        surface = _PICO_SURFACE["dark"]
        assert _contrast_ratio(amber, surface) == pytest.approx(
            _contrast_ratio(surface, amber)
        )

    @pytest.mark.parametrize(
        ("foreground", "background", "expected"),
        [
            (_AMBER["light"], _PICO_SURFACE["light"], 4.92),
            (_AMBER["dark"], _PICO_SURFACE["dark"], 6.11),
            (_AMBER["light"], _PICO_SURFACE["dark"], 3.65),
        ],
    )
    def test_reference_ratios_match_the_ui_spec(
        self, foreground: str, background: str, expected: float
    ) -> None:
        """Amber on each Pico surface gives its known ratio, the 3.65:1 failure too."""
        assert _contrast_ratio(foreground, background) == pytest.approx(
            expected, abs=0.01
        )

    def test_rgba_strings_parse(self) -> None:
        """An ``rgba()`` string is read by its colour channels; alpha is ignored."""
        assert _relative_luminance("rgba(255, 255, 255, 1)") == pytest.approx(1.0)

    def test_an_opaque_stack_composites_to_itself(self) -> None:
        """A single opaque layer flattens to the colour it already was."""
        surface = _PICO_SURFACE["dark"]
        assert _relative_luminance(_flatten([surface])) == pytest.approx(
            _relative_luminance(surface)
        )

    def test_a_translucent_layer_is_blended_rather_than_ignored(self) -> None:
        """A 3.75%-opacity grey over white reads as near-white, not as the grey."""
        # This is Pico's striped-row colour. It is unreachable today -- the
        # history table carries no class="striped", so Pico 2.1.1 does not
        # stripe it -- but adding that class is a one-attribute change, and
        # reading it as opaque would report a passing ratio as ~1.1:1.
        stripe = "rgba(111, 120, 135, 0.0375)"
        flattened = _flatten([stripe, _PICO_SURFACE["light"]])
        assert _relative_luminance(flattened) > 0.9
        # The same colour read as opaque, which is what ignoring alpha does.
        assert _relative_luminance(stripe) < 0.3

    def test_a_colour_the_parser_cannot_read_is_named(self) -> None:
        """An unreadable colour asserts with the string, not a slice ValueError."""
        with pytest.raises(AssertionError, match=r"color\(srgb"):
            _relative_luminance("color(srgb 1 1 1)")


@pytest.mark.browser
class TestFallbackStatusRendering:
    """
    The FALLBACK status renders amber, releases Scan and repaints history.

    The amber differs from the success green and the failure red once the
    cascade has resolved, the Scan button recovers after a fallback swap, and
    the history table refreshes. A template assertion cannot see any of them --
    the first is a cascade outcome, and the other two depend on JavaScript and
    htmx actually running.
    """

    @pytest.fixture
    def fallback_page(
        self, page: Page, browser_server: _BrowserServer
    ) -> Iterator[Page]:
        """Drive the live app's current job to FALLBACK, then clear it again."""
        app = browser_server.app
        job_store: JobStore = app.state.job_store
        job = job_store.create_job(
            profile="default",
            title="Fallback Doc",
            owner_token=_as_owner(page, browser_server.url),
        )
        # finish_job is the public writer for the warning column, and the worker
        # reaches FALLBACK through it. Writing the column through
        # job_store._conn instead would break the store's own rules: every
        # public writer is wrapped in @_locked because the web and worker
        # threads share one connection opened with check_same_thread=False, and
        # sqlite3 connection context managers do not nest -- an inner
        # `with conn:` commits the outer transaction.
        job_store.finish_job(
            job.id,
            JobState.FALLBACK,
            result=JobResult(
                outcome=ScanOutcome.FALLBACK,
                warning="Title, tags and correspondent were not applied.",
                pages_scanned=1,
                pages_removed=0,
                pages_uploaded=0,
            ),
        )
        app.state.worker._current_job_id = job.id
        try:
            yield page
        finally:
            # The server and its store are both session-scoped, so the teardown
            # undoes both halves. Clearing the pointer alone is not enough:
            # index() and current_job_status() each fall back to
            # list_recent(limit=1) when there is no current job, so a row left
            # here would follow every later test onto what should be the idle
            # page. Deleting the row is what restores the idle state.
            app.state.worker._current_job_id = None
            job_store.delete_job(job.id)

    def _goto(self, page: Page, url: str, scheme: Literal["light", "dark"]) -> None:
        """Load the page under an emulated OS colour-scheme preference."""
        page.emulate_media(color_scheme=scheme)
        page.goto(url)
        _show_the_live_outcome(page, "#status-area .status-fallback")

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_fallback_copy_and_class_render(
        self,
        fallback_page: Page,
        browser_server: _BrowserServer,
        scheme: Literal["light", "dark"],
    ) -> None:
        """The status area shows the arrow, the title and the warning."""
        self._goto(fallback_page, browser_server.url, scheme)
        status = fallback_page.locator("#status-area")
        text = status.inner_text()
        assert "→ Saved to folder: Fallback Doc" in text
        assert "Title, tags and correspondent were not applied." in text
        assert status.locator("p.status-fallback").count() == 2

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_fallback_colour_differs_from_done_and_error(
        self,
        fallback_page: Page,
        browser_server: _BrowserServer,
        scheme: Literal["light", "dark"],
    ) -> None:
        """
        Amber resolves to a colour that is neither the ins green nor the del red.

        The amber is visually distinct from both, under both colour schemes,
        because a token that is legible in one and invisible in the other is not
        distinct at all.
        """
        self._goto(fallback_page, browser_server.url, scheme)
        colours = fallback_page.evaluate(_PROBE_STATUS_COLOURS)
        assert len(set(colours.values())) == 4, colours

        rendered = fallback_page.evaluate(
            "() => getComputedStyle("
            "document.querySelector('#status-area p.status-fallback')).color"
        )
        # The probe measured the class; this proves the real markup wears it.
        assert rendered == colours["status-fallback"]

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_scan_button_re_enables_after_a_fallback_swap(
        self,
        fallback_page: Page,
        browser_server: _BrowserServer,
        scheme: Literal["light", "dark"],
    ) -> None:
        """
        A fallback swap releases a stale disabled Scan button.

        A FALLBACK is a finished scan, so a button still disabled when it lands
        would look like a locked-up application. What releases it is the
        server's out-of-band button in the status response, not JavaScript:
        the page carries no app script, so the only thing that can re-enable a
        button forced disabled here is the swapped-in server render.
        """
        self._goto(fallback_page, browser_server.url, scheme)
        fallback_page.evaluate("document.getElementById('scan-btn').disabled = true")
        assert fallback_page.locator("#scan-btn").is_disabled()

        fallback_page.evaluate(_SWAP_STATUS_AREA)
        fallback_page.wait_for_selector("#scan-btn:not([disabled])")
        scan_btn = fallback_page.locator("#scan-btn")
        assert (scan_btn.text_content() or "").strip() == "Scan"
        assert scan_btn.get_attribute("aria-busy") is None

    def test_fallback_swap_refreshes_the_history_table(
        self, fallback_page: Page, browser_server: _BrowserServer
    ) -> None:
        """
        The hidden reload div in the FALLBACK branch actually fires.

        The status partial is swapped in with a stale history table below it;
        the reload div carried in the new markup has to repaint that table
        without a page load.
        """
        self._goto(fallback_page, browser_server.url, "light")
        fallback_page.evaluate(
            "() => document.querySelectorAll('#history-body td.status-fallback')"
            ".forEach((cell) => cell.classList.remove('status-fallback'))"
        )
        assert fallback_page.locator("#history-body td.status-fallback").count() == 0

        fallback_page.evaluate(_SWAP_STATUS_AREA)
        fallback_page.wait_for_selector("#history-body td.status-fallback")
        cell = fallback_page.locator("#history-body td.status-fallback").first
        assert cell.inner_text().strip() == "Saved to folder"


@pytest.mark.browser
class TestWarnedDoneStatusRendering:
    """
    An upload that lost a sheet reads amber in a browser, in both schemes.

    The template tests prove the headline wears `status-fallback`; only a
    resolved cascade proves that class reads as the fallback amber, and not as
    the success green a green tick would have shown, in both colour schemes.
    """

    @pytest.fixture
    def warned_done_page(
        self, page: Page, browser_server: _BrowserServer
    ) -> Iterator[Page]:
        """Drive the live app's current job to a warned DONE, then clear it again."""
        app = browser_server.app
        job_store: JobStore = app.state.job_store
        job = job_store.create_job(
            profile="default",
            title="Warned Doc",
            owner_token=_as_owner(page, browser_server.url),
        )
        job_store.finish_job(
            job.id,
            JobState.DONE,
            result=JobResult(
                outcome=ScanOutcome.SUCCESS,
                warning=(
                    "1 page(s) could not be read by the scanner and were skipped. "
                    "They were not removed for being blank; rescan those sheets."
                ),
                pages_scanned=1,
                pages_removed=0,
                pages_uploaded=1,
            ),
        )
        app.state.worker._current_job_id = job.id
        try:
            yield page
        finally:
            # The server and its store are session-scoped: clearing the pointer
            # alone leaves this row as the most recent job, which the idle page
            # of every later test would then render. Deleting it restores idle.
            app.state.worker._current_job_id = None
            job_store.delete_job(job.id)

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_warned_done_colour_matches_the_fallback_amber(
        self,
        warned_done_page: Page,
        browser_server: _BrowserServer,
        scheme: Literal["light", "dark"],
    ) -> None:
        """
        The warned headline resolves to the fallback amber, never the done green.

        Run under both colour schemes, because an amber that turned green in
        one of them would tell half the operators their stack was complete.
        """
        warned_done_page.emulate_media(color_scheme=scheme)
        warned_done_page.goto(browser_server.url)
        _show_the_live_outcome(warned_done_page, "#status-area .status-fallback")

        status = warned_done_page.locator("#status-area")
        text = status.inner_text()
        assert "Uploaded with a warning: Warned Doc" in text
        assert "rescan those sheets." in text
        assert "Done: Warned Doc" not in text

        colours = warned_done_page.evaluate(_PROBE_STATUS_COLOURS)
        assert colours["status-fallback"] != colours["status-done"], colours
        headline = warned_done_page.evaluate(
            "() => getComputedStyle("
            "document.querySelector('#status-area p.status-fallback')).color"
        )
        assert headline == colours["status-fallback"]
        assert headline != colours["status-done"]


_POLL_OBSERVATION_MS = 2500
"""How long a terminal page is watched for status polls: two and a half 1 s ticks."""


@pytest.mark.browser
class TestCancelledStatusRendering:
    """
    The CANCELLED status renders muted, with no alert, and behaves as terminal.

    A cancel is a deliberate stop, not a failure, so it must never look like
    one: muted rather than red, and no alert. Whether the grey is really a
    fourth colour, and whether it is legible on each surface, is a cascade
    outcome no template assertion can see; that the state behaves as terminal
    -- polling stops, the Scan button comes back, history repaints -- depends
    on htmx actually running.
    """

    @pytest.fixture
    def cancelled_page(
        self, page: Page, browser_server: _BrowserServer
    ) -> Iterator[Page]:
        """Drive the live app's current job to CANCELLED, then clear it again."""
        app = browser_server.app
        job_store: JobStore = app.state.job_store
        job = job_store.create_job(
            profile="default",
            title="Cancelled Doc",
            owner_token=_as_owner(page, browser_server.url),
        )
        job_store.finish_job(
            job.id,
            JobState.CANCELLED,
            error="Manual duplex scan cancelled at the flip prompt",
        )
        app.state.worker._current_job_id = job.id
        try:
            yield page
        finally:
            # Both halves, for the reason fallback_page gives: clearing only
            # the pointer leaves this job as list_recent's most recent row, and
            # every later "idle" page would render it.
            app.state.worker._current_job_id = None
            job_store.delete_job(job.id)

    def _goto(self, page: Page, url: str, scheme: Literal["light", "dark"]) -> None:
        """Load the page under an emulated OS colour-scheme preference."""
        page.emulate_media(color_scheme=scheme)
        page.goto(url)
        _show_the_live_outcome(page, "#status-area .status-cancelled")

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_cancelled_copy_renders_without_an_alert(
        self,
        cancelled_page: Page,
        browser_server: _BrowserServer,
        scheme: Literal["light", "dark"],
    ) -> None:
        """The status area shows the cancel line, and nothing in it is an alert."""
        self._goto(cancelled_page, browser_server.url, scheme)
        status = cancelled_page.locator("#status-area")
        line = status.locator("p.status-cancelled")
        expect(line).to_be_visible()
        assert line.inner_text().strip() == "⊘ Cancelled: Cancelled Doc"
        assert status.locator('[role="alert"]').count() == 0

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_cancelled_colour_is_the_muted_token_not_the_error_red(
        self,
        cancelled_page: Page,
        browser_server: _BrowserServer,
        scheme: Literal["light", "dark"],
    ) -> None:
        """
        The cancelled line resolves to Pico's muted grey, a fourth status colour.

        Four distinct colours is the proof that the grey is not the ins green,
        the del red or the fallback amber once the cascade resolves; equality
        with the muted token is the proof it is that token and not merely the
        inherited body colour, which is also distinct from the other three.
        """
        self._goto(cancelled_page, browser_server.url, scheme)
        colours = cancelled_page.evaluate(_PROBE_STATUS_COLOURS)
        assert len(set(colours.values())) == 4, colours
        assert colours["status-cancelled"] != colours["status-error"], colours

        muted = cancelled_page.evaluate(_PROBE_MUTED_TOKEN)
        assert colours["status-cancelled"] == muted, (colours, muted)
        assert muted == _MUTED[scheme], muted

        rendered = cancelled_page.evaluate(
            "() => getComputedStyle("
            "document.querySelector('#status-area p.status-cancelled')).color"
        )
        # The probe measured the class; this proves the real markup wears it.
        assert rendered == colours["status-cancelled"]

    def test_cancelled_page_is_terminal_and_stops_polling(
        self, cancelled_page: Page, browser_server: _BrowserServer
    ) -> None:
        """
        A CANCELLED current job leaves the page idle-ready, as a terminal state.

        The Scan button is enabled with no busy marker, the status area carries
        no polling trigger, and -- the behaviour the attribute stands for -- no
        status request goes out across more than two poll intervals.
        """
        self._goto(cancelled_page, browser_server.url, "light")
        # Installed after the one status request the live outcome was swapped
        # in with, so what it counts is polling alone.
        polls: list[str] = []
        cancelled_page.on(
            "request",
            lambda request: (
                polls.append(request.url)
                if "/api/jobs/current/status" in request.url
                else None
            ),
        )

        scan_btn = cancelled_page.locator("#scan-btn")
        assert scan_btn.is_enabled()
        assert scan_btn.get_attribute("aria-busy") is None
        assert (scan_btn.text_content() or "").strip() == "Scan"
        assert (
            cancelled_page.locator("#status-area").get_attribute("hx-trigger") is None
        )

        browser_quiet_window(cancelled_page, _POLL_OBSERVATION_MS)
        assert polls == [], polls

    def test_scan_button_re_enables_after_a_cancelled_swap(
        self, cancelled_page: Page, browser_server: _BrowserServer
    ) -> None:
        """A cancelled swap releases a stale disabled Scan button, as FALLBACK does."""
        self._goto(cancelled_page, browser_server.url, "light")
        cancelled_page.evaluate("document.getElementById('scan-btn').disabled = true")
        assert cancelled_page.locator("#scan-btn").is_disabled()

        cancelled_page.evaluate(_SWAP_STATUS_AREA)
        cancelled_page.wait_for_selector("#scan-btn:not([disabled])")
        scan_btn = cancelled_page.locator("#scan-btn")
        assert (scan_btn.text_content() or "").strip() == "Scan"
        assert scan_btn.get_attribute("aria-busy") is None

    def test_cancelled_swap_refreshes_the_history_table(
        self, cancelled_page: Page, browser_server: _BrowserServer
    ) -> None:
        """The hidden reload div in the CANCELLED branch repaints history."""
        self._goto(cancelled_page, browser_server.url, "light")
        cancelled_page.evaluate(
            "() => document.querySelectorAll('#history-body td.status-cancelled')"
            ".forEach((cell) => cell.classList.remove('status-cancelled'))"
        )
        assert cancelled_page.locator("#history-body td.status-cancelled").count() == 0

        cancelled_page.evaluate(_SWAP_STATUS_AREA)
        cancelled_page.wait_for_selector("#history-body td.status-cancelled")
        cell = cancelled_page.locator("#history-body td.status-cancelled").first
        assert cell.inner_text().strip() == "Cancelled"


@pytest.mark.browser
class TestDarkModeEngagement:
    """
    Dark mode engages from the OS preference and keeps status text legible.

    Pico's dark palette is not what is in doubt here; it is a published build.
    What is in doubt is this app's cascade: whether <html> leaves Pico's
    automatic-dark selector free to match, whether the app's own amber follows
    the scheme, and what surface each status line really sits on. None of that
    is visible in the template or the stylesheet alone, so the proof is the
    colour a real browser computes.
    """

    def _goto(self, page: Page, url: str, scheme: Literal["light", "dark"]) -> None:
        """Load the idle page under an emulated OS colour-scheme preference."""
        page.emulate_media(color_scheme=scheme)
        page.goto(url)
        # The idle page renders "Ready to scan." and no .status-* line, so there
        # is nothing status-shaped to wait for -- wait for the history table
        # instead. This holds because every fixture that stages a job deletes it
        # on teardown; a surviving row would put a finished job's page here.
        page.wait_for_selector("#history-body")

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_root_surface_follows_the_os_scheme(
        self,
        page: Page,
        browser_server_url: str,
        scheme: Literal["light", "dark"],
    ) -> None:
        """The root element paints Pico's surface for the OS scheme."""
        expected = {
            "light": (_PICO_SURFACE["light"], "light"),
            "dark": (_PICO_SURFACE["dark"], "dark"),
        }
        self._goto(page, browser_server_url, scheme)
        surface = page.evaluate(_READ_ROOT_SURFACE)
        background, color_scheme = expected[scheme]
        assert surface["background"] == background, surface
        assert surface["colorScheme"] == color_scheme, surface

    # The probe context is named "placement" here because pytest-playwright
    # already owns a fixture called "context" (the browser context that "page"
    # is built from); a parameter of that name would replace it with a string.
    @pytest.mark.parametrize("placement", ["status-area", "history-cell", "card"])
    @pytest.mark.parametrize(
        "cls",
        [
            "status-done",
            "status-error",
            "status-fallback",
            "status-cancelled",
            # The counts line renders in the status area and in the
            # history Title cell alike, so it belongs in exactly
            # the same three placements as the four status colours. It reads
            # --pico-muted-color, the property .status-cancelled reads, which
            # is why the value assertion below covers both from one constant:
            # a drift in that token must be reported by both or by neither.
            "page-counts",
        ],
    )
    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_status_colour_meets_aa_contrast(
        self,
        page: Page,
        browser_server_url: str,
        scheme: Literal["light", "dark"],
        cls: Literal[
            "status-done",
            "status-error",
            "status-fallback",
            "status-cancelled",
            "page-counts",
        ],
        placement: Literal["status-area", "history-cell", "card"],
    ) -> None:
        """
        Every status colour reaches WCAG AA where it is really shown.

        The card placement covers a status line inside an ``<article>``; the
        dark card is lighter than the dark page,
        so it is the tighter of the two for the muted cancelled grey.

        The fallback amber, the cancelled grey and the counts line are also
        checked by value, so a palette drift is reported by name rather than
        only as a ratio that happens to pass.
        """
        self._goto(page, browser_server_url, scheme)
        probe = page.evaluate(
            _PROBE_CONTEXT_CONTRAST, {"cls": cls, "context": placement}
        )
        colour = probe["colour"]
        background = _flatten(probe["backgroundStack"])
        ratio = _contrast_ratio(colour, background)
        assert ratio >= 4.5, (colour, background, ratio)
        if cls == "status-fallback":
            assert colour == _AMBER[scheme], (colour, background, ratio)
        if cls in {"status-cancelled", "page-counts"}:
            assert colour == _MUTED[scheme], (colour, background, ratio)


_QUEUE_FULL_TEXT = (
    "✗ The scan queue is full. Wait for a scan to finish, then try again."
)
"""The slot's exact text for a 429: the error partial's cross plus its copy."""

_DEGRADED_TEXT = (
    "✗ Job history cannot be saved right now, so the scan was not started. "
    "Check the server's free disk space and log, then try again."
)
"""The slot's exact text for a 503 WORKER_DEGRADED, the longest slot message."""

_UNKNOWN_PROFILE_TEXT = "That scan profile does not exist."
"""The start of the 422 UNKNOWN_PROFILE message."""

_SLOT_MESSAGE = "#status-message p.status-error"
"""
The slot's message paragraph, named rather than taken as the slot's first p.

A collapsed "Technical details" disclosure sits beside the message, so the
slot holds more than the message and more than one paragraph.  Everything
asserting what the user reads -- the exact text, the red, the left edge, the
wrapped line count -- names this paragraph, so a change to the disclosure
cannot be measured by mistake.
"""

_FILL_ATTEMPTS = 15
"""Most submits the queue fill makes: one running job, ten queued, a 429, and slack."""

_TOO_MANY_REQUESTS = 429

# The left edge of the first visible character of an element's first non-blank
# text node. The paragraph box is not what the eye lines up -- the text is -- so
# the measurement is a Range over one glyph rather than a bounding box.
_READ_TEXT_LEFT_EDGE = """
(selector) => {
    const element = document.querySelector(selector);
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
        const start = node.textContent.search(/\\S/);
        if (start === -1) continue;
        const range = document.createRange();
        range.setStart(node, start);
        range.setEnd(node, start + 1);
        return range.getBoundingClientRect().left;
    }
    return null;
}
"""

# Where an element's content box starts, border and padding included. Text-edge
# probing is wrong for a <summary>, whose disclosure marker may or may not sit
# before its first character depending on the display mode; the content box is
# the same measurement the CSS inset actually controls.
_READ_CONTENT_BOX_LEFT = """
(selector) => {
    const element = document.querySelector(selector);
    if (element === null) return null;
    const style = getComputedStyle(element);
    return element.getBoundingClientRect().left
        + parseFloat(style.borderLeftWidth)
        + parseFloat(style.paddingLeft);
}
"""

# How many line boxes the slot message's text occupies.
_COUNT_SLOT_TEXT_LINES = """
() => {
    const paragraph = document.querySelector("#status-message p.status-error");
    const range = document.createRange();
    range.selectNodeContents(paragraph);
    const tops = new Set(
        Array.from(range.getClientRects(), (rect) => Math.round(rect.top))
    );
    return tops.size;
}
"""

# Fires the unknown-profile submit through htmx, the way the page's own form
# would, without awaiting htmx.ajax's promise: a 4xx settles it in a way the
# test has no use for, and the swap itself is what the test waits on.
_SUBMIT_UNKNOWN_PROFILE = """
() => {
    htmx.ajax("POST", "/api/scan", {values: {profile: "zz-nonexistent-profile"}});
}
"""

# The status poll names the job this browser follows once it has one, and
# the current-job path only until then, so a poll is recognised by the
# shape of its URL rather than by one literal path.  The URL carries the token
# of what the page shows as a query string, so the path may end there too.
_POLL_URL = re.compile(r"/api/jobs/[^/?]+/status(\?|$)")
# How long one status poll may take to arrive: the poll fires every second, so
# this is generous headroom for a loaded CI runner.
_ONE_POLL_BUDGET_MS = 5_000

# Counts the status polls htmx swapped into the page and settled, one per
# response, and the status polls answered 204, which swap nothing.  htmx fires
# htmx:afterSettle once for every element a swap settles, and a poll's body
# settles two, the status area and the out-of-band Scan button, so a swapped
# poll is counted by its request rather than by its events.  The settle is
# counted rather than the response, because a response arrives before htmx
# swaps it.
_RECORD_POLL_SETTLES = r"""
(() => {
    window.__pollsSettled = 0;
    window.__pollsUnchanged = 0;
    const settled = new WeakSet();
    const isPoll = (xhr) => Boolean(xhr && xhr.responseURL)
        && /^\/api\/jobs\/[^/]+\/status$/.test(new URL(xhr.responseURL).pathname);
    document.addEventListener("htmx:afterSettle", (event) => {
        const xhr = event.detail.xhr;
        if (isPoll(xhr) && !settled.has(xhr)) {
            settled.add(xhr);
            window.__pollsSettled += 1;
        }
    });
    document.addEventListener("htmx:afterRequest", (event) => {
        const xhr = event.detail.xhr;
        if (isPoll(xhr) && xhr.status === 204) {
            window.__pollsUnchanged += 1;
        }
    });
})();
"""


def _hold_the_status_polls(page: Page) -> tuple[list[Route], Callable[[], None]]:
    """
    Hold every status poll the page sends, unanswered, until released.

    Releasing sends each held poll on with an empty ``seen``, which no
    rendering's token matches, so each is answered with a body whatever the
    page was showing when it asked.  Polls sent after the release pass through
    untouched.

    Args:
        page: The browser page, before it loads.

    Returns:
        The held polls, in order, filled as they are sent, and the release.

    """
    held: list[Route] = []
    released = False

    def _hold(route: Route) -> None:
        if released:
            route.continue_()
        else:
            held.append(route)

    def _release() -> None:
        nonlocal released
        released = True
        for route in held:
            parts = urlsplit(route.request.url)
            query = parse_qs(parts.query, keep_blank_values=True)
            query["seen"] = [""]
            route.continue_(
                url=parts._replace(query=urlencode(query, doseq=True)).geturl()
            )

    page.route(_POLL_URL, _hold)
    return held, _release


def _fill_queue_until_rejected(url: str) -> None:
    """
    Submit scans over plain HTTP until one is refused with 429, and assert it was.

    With the gate closed the first job sits in SCANNING and the next ten fill
    the queue, so the twelfth submit is the first refusal. The count is not
    assumed: the worker may not have taken the first job off the queue yet when
    the eleventh arrives, so the loop stops at the first 429 whenever it comes.
    """
    statuses: list[int] = []
    for _ in range(_FILL_ATTEMPTS):
        response = httpx2.post(url + "/api/scan", data={"profile": "default"})
        statuses.append(response.status_code)
        if response.status_code == _TOO_MANY_REQUESTS:
            return
    pytest.fail(f"the queue never refused a submit: {statuses}")


@pytest.mark.browser
class TestRequestErrorSlot:
    """
    A request error shows in ``#status-message``, legible, aligned and lasting.

    A 429 that is returned but never shown is the failure this class guards:
    the TestClient tests prove the response, and only a browser proves that htmx
    retargets it into ``#status-message``, that the text is legible and aligned,
    that polling does not erase it, and that a successful scan does. The Scan
    button is disabled while a job is active, so the queue is filled over HTTP
    after an idle page has loaded.
    """

    @pytest.fixture
    def queue_full_page(
        self, page: Page, scan_harness: _ScanHarness
    ) -> Callable[..., Page]:
        """
        Return an opener that loads an idle page and then fills the queue behind it.

        The opener takes the colour scheme and viewport width, because both
        have to be set before ``goto``. ``scan_harness`` owns the teardown: it
        opens the gate, waits for every created row -- the queued jobs and the
        rejected attempts -- to be terminal, deletes them, clears the worker's
        pointer and asserts nothing was left behind.
        """
        server = scan_harness.server

        def _open(
            scheme: Literal["light", "dark"] = "light", width: int = 1280
        ) -> Page:
            page.set_viewport_size({"width": width, "height": 812})
            page.emulate_media(color_scheme=scheme)
            server.scanner.gate.clear()
            page.goto(server.url)
            expect(page.locator("#scan-btn")).to_be_enabled()
            _fill_queue_until_rejected(server.url)
            page.locator("#scan-btn").click()
            expect(page.locator(_SLOT_MESSAGE)).to_have_text(_QUEUE_FULL_TEXT)
            return page

        return _open

    def test_queue_full_message_is_visible_and_recorded(
        self, queue_full_page: Callable[..., Page]
    ) -> None:
        """
        A 429 lands in the slot, as announced text, and in Job History.

        The error goes to the slot, not the status area, and the rejected
        attempt is recorded. The slot itself is the alert, so the message must
        not carry a second ``role="alert"``.  Focus returns to the Scan
        button the refused press came from, enabled, and never moves into
        ``#status-message``: the slot speaks the error from where it is.
        """
        page = queue_full_page()
        slot = page.locator("#status-message")
        expect(slot.locator("p.status-error")).to_have_count(1)
        expect(slot.locator('[role="alert"]')).to_have_count(0)
        expect(page.locator("#status-area")).to_have_count(1)
        expect(page.locator("#scan-btn")).to_be_enabled()
        expect(page.locator("#scan-btn")).to_be_focused()
        expect(page.locator("#scan-btn")).to_have_count(1)
        focus_in_slot = page.evaluate(
            "document.getElementById('status-message').contains(document.activeElement)"
        )
        assert focus_in_slot is False

        status_cell = page.locator("#history-body tr").first.locator("td").nth(3)
        expect(status_cell).to_have_text("Failed", timeout=5_000)
        expect(status_cell).to_have_class(re.compile(r"\bstatus-error\b"))

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_error_message_colour_and_contrast(
        self,
        queue_full_page: Callable[..., Page],
        scheme: Literal["light", "dark"],
    ) -> None:
        """
        The slot message is the error red at WCAG AA in both schemes.

        The colour is asserted by value so a palette drift is named, and the
        ratio is measured against the layers the real paragraph sits on.
        """
        page = queue_full_page(scheme=scheme)
        reading = page.evaluate(_READ_ELEMENT_CONTRAST, _SLOT_MESSAGE)
        colour = reading["colour"]
        background = _flatten(reading["backgroundStack"])
        ratio = _contrast_ratio(colour, background)
        assert colour == _ERROR_RED[scheme], (colour, background, ratio)
        assert ratio >= 4.5, (colour, background, ratio)

    @pytest.mark.parametrize("width", [1280, 375])
    def test_error_text_aligns_with_status_text(
        self, queue_full_page: Callable[..., Page], width: int
    ) -> None:
        """
        The slot's text starts at the same x as the status text.

        ``app.css`` insets the slot paragraph by the status area's border and
        padding; the check is on the glyphs, within 1 px, at desktop and phone
        widths.
        """
        page = queue_full_page(width=width)
        expect(page.locator("#status-area p")).to_have_count(1)
        slot_left = page.evaluate(_READ_TEXT_LEFT_EDGE, _SLOT_MESSAGE)
        status_left = page.evaluate(_READ_TEXT_LEFT_EDGE, "#status-area p")
        assert slot_left is not None
        assert status_left is not None
        assert abs(slot_left - status_left) <= 1, (slot_left, status_left)

    @pytest.mark.parametrize("width", [1280, 375])
    def test_technical_details_aligns_with_the_message(
        self, queue_full_page: Callable[..., Page], width: int
    ) -> None:
        """
        The disclosure starts at the same x as the message it belongs to.

        ``app.css`` insets the slot's children by the status area's own border
        and padding so the slot and the status area share a left edge. The
        message is a sibling of the disclosure, not an only child, so the inset
        has to cover the disclosure too, or "Technical details" hangs a rem to
        the left of the sentence it explains.
        """
        page = queue_full_page(width=width)
        message = page.evaluate(_READ_CONTENT_BOX_LEFT, _SLOT_MESSAGE)
        details = page.evaluate(
            _READ_CONTENT_BOX_LEFT, "#status-message > details.tech-details"
        )
        assert message is not None
        assert details is not None
        assert abs(details - message) <= 1, (details, message)

    def test_long_error_wraps_on_a_narrow_viewport(
        self,
        page: Page,
        scan_harness: _ScanHarness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The longest slot message wraps at 375 px with no horizontal overflow.

        The route reads ``worker.health`` before any submit, so replacing the
        property makes the 503 deterministic. Setting the worker's degraded
        Event instead would race its idle probe, which clears the flag on a
        healthy store.
        """
        server = scan_harness.server
        worker = server.app.state.worker
        monkeypatch.setattr(
            type(worker), "health", property(lambda _self: WorkerHealth.DEGRADED)
        )
        page.set_viewport_size({"width": 375, "height": 812})
        page.goto(server.url)

        page.locator("#scan-btn").click()

        expect(page.locator(_SLOT_MESSAGE)).to_have_text(_DEGRADED_TEXT)
        assert page.evaluate(_COUNT_SLOT_TEXT_LINES) > 1
        overflow = page.evaluate(
            "() => { const main = document.querySelector('main');"
            " return [main.scrollWidth, main.clientWidth]; }"
        )
        assert overflow[0] <= overflow[1], overflow

    def test_error_survives_status_polling(
        self, page: Page, scan_harness: _ScanHarness
    ) -> None:
        """
        An error shown mid-scan outlives a status poll's body and the 204s after it.

        The poll re-renders ``#status-area`` every second while a job is active.
        If a poll response carried the slot clear that a successful scan
        carries, the message would vanish within a second -- before a user
        could read it.  Only a poll with a body can carry the clear, and once
        the area shows the job, every later poll finds nothing changed and is
        answered 204, so the polls are held until the error is shown and the
        first is then answered with a body.  The submitted profile is also
        checked not to be echoed.
        """
        server = scan_harness.server
        page.add_init_script(_RECORD_POLL_SETTLES)
        held, release = _hold_the_status_polls(page)
        server.scanner.gate.clear()
        page.goto(server.url)
        page.locator("#scan-btn").click()
        expect(page.locator("#status-area p.busy-line")).to_be_visible()

        page.evaluate(_SUBMIT_UNKNOWN_PROFILE)

        slot = page.locator("#status-message")
        expect(slot).to_contain_text(_UNKNOWN_PROFILE_TEXT)
        assert "zz-nonexistent-profile" not in slot.inner_text()

        def _a_poll_is_held() -> bool:
            # A synchronous Playwright client delivers the route to its handler
            # only while it talks to the browser, so each check is a round trip.
            page.evaluate("() => true")
            return bool(held)

        assert poll_until(_a_poll_is_held, _ONE_POLL_BUDGET_MS / 1000), (
            "the page sent no status poll"
        )
        # Nothing has been swapped or answered 204 yet: every poll is held.
        # Functions, not bare expressions, because the page's
        # Content-Security-Policy refuses eval.
        counts = "() => [window.__pollsSettled, window.__pollsUnchanged]"
        assert page.evaluate(counts) == [0, 0]
        release()
        page.wait_for_function(
            "() => window.__pollsSettled >= 1 && window.__pollsUnchanged >= 1",
            timeout=2 * _ONE_POLL_BUDGET_MS,
        )
        # Read once, without retrying: the claim is that the message is there
        # now, after the polls, not that it can be found again within a timeout.
        assert _UNKNOWN_PROFILE_TEXT in slot.inner_text(), "a poll erased the error"
        assert page.locator("#status-area").count() == 1

    def test_successful_scan_clears_the_error(
        self, page: Page, scan_harness: _ScanHarness
    ) -> None:
        """
        A successful scan empties the slot back to zero height.

        The slot must also stay the same ``role="alert"`` element: the clear
        replaces its contents, not the element, so the next error is still
        announced.
        """
        server = scan_harness.server
        page.goto(server.url)
        page.evaluate(_SUBMIT_UNKNOWN_PROFILE)
        slot = page.locator("#status-message")
        expect(slot).to_contain_text(_UNKNOWN_PROFILE_TEXT)

        page.locator("#scan-btn").click()

        # A function, not a bare expression, which Playwright would eval under
        # the page's Content-Security-Policy (see _refresh_both_lists).
        page.wait_for_function(
            "() => document.getElementById('status-message').childNodes.length === 0"
        )
        assert slot.evaluate("(el) => el.getBoundingClientRect().height") == 0
        assert slot.get_attribute("role") == "alert"
        expect(page.locator("#status-area .status-done")).to_be_visible(timeout=15_000)


def _non_loopback_ipv4() -> str | None:
    """
    Return this machine's outbound IPv4 address, or None when it has none.

    Connecting a UDP socket sends no packet; it only asks the kernel which
    local address would route to the target. The target is TEST-NET-1
    (192.0.2.1, RFC 5737), which is never assigned to a real host. A runner
    with no default route raises, and a loopback-only one answers 127.x.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = str(probe.getsockname()[0])
    except OSError:
        address = None
    finally:
        probe.close()
    if address is None or address.startswith("127."):
        return None
    return address


class _ScanHeaderRecorder:
    """
    An ASGI wrapper that records the headers of every ``POST /api/scan`` it passes on.

    The browser's own view of a request cannot answer whether it sent
    ``Sec-Fetch-Site``: under the egress gate's ``context.route``, Playwright's
    ``Request.all_headers()`` omits the ``Sec-Fetch-*`` headers even when the
    server receives them (a routed 127.0.0.1 page reports none while the server
    gets ``same-origin``). So the headers are read where the guard
    reads them, in front of the app. Every scope, lifespan included, is passed
    through unchanged.
    """

    def __init__(self, app: FastAPI) -> None:
        """Wrap ``app`` with an empty record."""
        self.app = app
        self.scan_headers: list[dict[str, str]] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Record a scan submit's headers, then hand the request to the app."""
        if (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"] == "/api/scan"
        ):
            self.scan_headers.append(
                {
                    name.decode("latin-1").lower(): value.decode("latin-1")
                    for name, value in scope["headers"]
                }
            )
        await self.app(scope, receive, send)


class _LanServer(NamedTuple):
    """A function-scoped app served on a non-loopback address over plain HTTP."""

    url: str
    app: FastAPI
    scan_headers: list[dict[str, str]]


@pytest.fixture
def lan_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_LanServer]:
    """
    Serve a separate app on this machine's LAN address, then shut it down.

    It is its own app with its own store under ``tmp_path``, so nothing it does
    touches the session server. Uvicorn binds all interfaces on a free port
    because the browser has to reach it by the LAN address, and it runs only for
    this one test, with a stub scanner and a stubbed Paperless.
    """
    address = _non_loopback_ipv4()
    if address is None:
        pytest.skip("no non-loopback IPv4 address")
    app = create_app(_browser_test_settings(tmp_path), _BrowserTestScanner())
    recorder = _ScanHeaderRecorder(app)
    running = _start_uvicorn(recorder, host="0.0.0.0")
    try:
        _make_paperless_deliver(app, monkeypatch)
        yield _LanServer(
            url=f"http://{address}:{running.port}",
            app=app,
            scan_headers=recorder.scan_headers,
        )
    finally:
        _stop_uvicorn(running)


@pytest.mark.browser
class TestPlainHttpLanOrigin:
    """
    The cross-site guard lets the app's own page scan from a LAN address.

    saneless's documented deployment is plain HTTP on a LAN address, and there a
    browser sends no ``Sec-Fetch-Site`` at all, so only the guard's Origin
    branch stands between a user and a 403 on every scan.
    """

    def test_same_origin_scan_from_a_lan_address_is_not_blocked(
        self,
        page: Page,
        egress_allowlist: list[str],
        lan_server: _LanServer,
    ) -> None:
        """
        A same-origin Scan click from ``http://<lan-ip>`` is accepted.

        The Fetch Metadata spec adds ``Sec-Fetch-*`` only for potentially
        trustworthy URLs, so Chromium omits it on a plain HTTP LAN origin and
        the guard decides on ``Origin`` against ``Host``. ``localhost`` and
        ``127.0.0.1`` cannot prove this: they are secure contexts and always
        get ``Sec-Fetch-Site``. The headers are read as the server received
        them, because Playwright's request object hides ``Sec-Fetch-*`` under
        routing.
        """
        lan_url = lan_server.url
        egress_allowlist.append(lan_url)
        page.goto(lan_url)

        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/api/scan")
            )
        ) as response_info:
            page.locator("#scan-btn").click()

        assert response_info.value.status == 200, response_info.value.status
        assert len(lan_server.scan_headers) == 1, lan_server.scan_headers
        headers = lan_server.scan_headers[0]
        assert "sec-fetch-site" not in headers, (
            f"Chromium sent Sec-Fetch-Site={headers['sec-fetch-site']!r} to "
            f"{lan_url}, so this test does not exercise the guard's Origin branch: the "
            "runner's address is being treated as potentially trustworthy"
        )
        assert headers.get("origin") == lan_url, headers
        expect(page.locator("#status-message")).to_be_empty()
        expect(page.locator("#status-area .status-done")).to_be_visible(timeout=15_000)


# The shipped stand-in `config.is_placeholder_token` refuses. A second
# live server runs with it so the blocked Scan button can be looked at in a
# real browser rather than only in rendered markup.
_SHIPPED_PLACEHOLDER = "changeme"

_BLOCKED_REASON_TEXT = (
    "The paperless-ngx API token has not been set \N{EM DASH} see System status above."
)

_BLOCKED_REASON_SELECTOR = "#scan-blocked-reason"


def _blocked_settings(tmp_dir: Path) -> Settings:
    """
    Build the browser settings with the shipped placeholder token in place.

    One definition, because two servers run with it: the session-scoped one
    below, and the private one serving the claims that write a job row. A second copy of this ``model_copy`` would be a second place for the
    placeholder to drift from ``is_placeholder_token``'s idea of one.
    """
    configured = _browser_test_settings(tmp_dir)
    return configured.model_copy(
        update={
            "paperless": PaperlessConfig(
                url=configured.paperless.url,
                token=_SHIPPED_PLACEHOLDER,
            )
        }
    )


@pytest.fixture(scope="session")
def blocked_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_BrowserServer]:
    """
    Start a second live server whose paperless-ngx token is a placeholder.

    The verdict is read from ``Settings`` once at process start, so there is no
    runtime setter to reach for: a second server is the only honest way to put
    a real browser in front of the blocked page.
    """
    tmp_dir = tmp_path_factory.mktemp("browser-blocked")
    scanner = _BrowserTestScanner()
    app = _create_offline_app(_blocked_settings(tmp_dir), scanner)
    running = _start_uvicorn(app, host="127.0.0.1")
    yield _BrowserServer(
        url=f"http://127.0.0.1:{running.port}", app=app, scanner=scanner
    )
    _stop_uvicorn(running)


@pytest.mark.browser
class TestBlockedScanButtonInABrowser:
    """
    A placeholder token blocks the Scan button and shows why, in Chromium.

    Two of these claims cannot be made from rendered markup. Whether the
    ``disabled`` attribute survives the page's own requests is the
    ``hx-disabled-elt`` inheritance trap, which only a browser running htmx can spring; and whether
    the reason line is actually legible is a question about computed colour on
    the layers it really sits on.

    The button is still only the courtesy: the refusal that holds is the route
    guard, proved in tests/test_web_errors.py against a client with no button
    at all.
    """

    def _open(
        self,
        page: Page,
        blocked_server: _BrowserServer,
        egress_allowlist: list[str],
        scheme: Literal["light", "dark"] = "light",
    ) -> None:
        """Load the blocked appliance's page, after allowing its origin."""
        egress_allowlist.append(blocked_server.url)
        page.emulate_media(color_scheme=scheme)
        page.goto(blocked_server.url)

    def test_the_blocked_button_is_disabled_and_described_in_the_live_dom(
        self,
        page: Page,
        blocked_server: _BrowserServer,
        egress_allowlist: list[str],
    ) -> None:
        """One disabled button, still labelled Scan, pointing at its reason."""
        self._open(page, blocked_server, egress_allowlist)

        button = page.locator("#scan-btn")
        expect(button).to_be_disabled()
        assert (button.text_content() or "").strip() == "Scan"
        assert button.get_attribute("aria-describedby") == "scan-blocked-reason"
        assert button.get_attribute("aria-busy") is None
        assert page.evaluate(_COUNT_ARRAY_SCAN_BUTTONS) == 1

    def test_the_reason_line_is_visible_text_beneath_the_button(
        self,
        page: Page,
        blocked_server: _BrowserServer,
        egress_allowlist: list[str],
    ) -> None:
        """
        The reason is on the page, not in a tooltip, and it sits below.

        A disabled button cannot be focused or reliably hovered, so a ``title``
        would be unreachable by keyboard and unreliable on touch. The geometry
        is asserted because "beneath it" is the whole of the layout claim.
        """
        self._open(page, blocked_server, egress_allowlist)

        reason = page.locator(_BLOCKED_REASON_SELECTOR)
        expect(reason).to_be_visible()
        assert (reason.text_content() or "").strip() == _BLOCKED_REASON_TEXT

        button_box = page.locator("#scan-btn").bounding_box()
        reason_box = reason.bounding_box()
        assert button_box is not None
        assert reason_box is not None
        assert reason_box["y"] >= button_box["y"] + button_box["height"]

    def test_the_blocked_button_survives_the_form_s_own_requests(
        self,
        page: Page,
        blocked_server: _BrowserServer,
        egress_allowlist: list[str],
    ) -> None:
        """
        The requests inside the form do not re-enable the blocked button.

        An inherited ``hx-disabled-elt`` puts a child request in charge of the
        button's ``disabled`` attribute, which is why the form disinherits it.
        With no job active there is no one-second status poll to put the
        attribute back, so a blocked button that lost it here would stay
        clickable -- which is exactly why the flag lives in the one button
        partial.  The two refresh buttons supply the form's own requests.
        """
        egress_allowlist.append(blocked_server.url)
        page.add_init_script(_RECORD_CONTROL_SWAPS)
        page.goto(blocked_server.url)
        _refresh_both_lists(page)

        # Read once, without retrying: nothing re-renders this button while no
        # job is active, so a retrying check would only hide a sprung trap
        # behind a timeout.
        assert page.locator("#scan-btn").is_disabled(), (
            "the form's own refresh requests re-enabled a blocked Scan button"
        )
        assert page.locator(_BLOCKED_REASON_SELECTOR).is_visible()

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_the_reason_line_is_the_error_red_at_aa(
        self,
        page: Page,
        blocked_server: _BrowserServer,
        egress_allowlist: list[str],
        scheme: Literal["light", "dark"],
    ) -> None:
        """
        It borrows ``.status-error``, measured on the layers it really sits on.

        No new colour is introduced, so the value is asserted rather than only
        the ratio: a palette drift should be named here, not absorbed.
        """
        self._open(page, blocked_server, egress_allowlist, scheme)

        reading = page.evaluate(_READ_ELEMENT_CONTRAST, _BLOCKED_REASON_SELECTOR)
        colour = reading["colour"]
        background = _flatten(reading["backgroundStack"])
        ratio = _contrast_ratio(colour, background)

        assert colour == _ERROR_RED[scheme], (colour, background, ratio)
        assert ratio >= 4.5, (colour, background, ratio)

    def test_a_configured_appliance_shows_no_reason_line(
        self, page: Page, browser_server: _BrowserServer
    ) -> None:
        """
        The courtesy is absent when there is nothing to be courteous about.

        Read once the lists have landed: until then the page holds Scan for
        them, with its own reason.
        """
        page.goto(browser_server.url)
        _await_the_lists(page)

        assert page.locator(_BLOCKED_REASON_SELECTOR).count() == 0
        assert page.locator("#scan-btn").get_attribute("aria-describedby") is None
        # The other half of the same flag, asserted here so the blocked case
        # below is a difference and not just a presence.
        expect(page.locator("#scan-btn")).to_be_enabled()


_OWNER_COOKIE_NAME = "saneless_owner"

# The owner cookie lives a year, so a browser keeps its ownership across
# restarts. Playwright reports a cookie's expiry in Unix seconds, and a session
# cookie -- one with neither Max-Age nor Expires -- as -1, so an expiry more
# than 364 days out proves the year-long lifetime arrived. The one day short of
# a year is slack for the clock moving between the response and the check.
_OWNER_COOKIE_MIN_LIFETIME_SECONDS = 364 * 24 * 60 * 60


def _outlives_364_days(expires: float) -> bool:
    """
    Report whether a cookie jar's expiry is more than 364 days from now.

    Args:
        expires: The ``expires`` field Playwright reports, in Unix seconds.

    Returns:
        True for the owner cookie's year-long lifetime; False for a session
        cookie (-1) or anything shorter.

    """
    return expires > time.time() + _OWNER_COOKIE_MIN_LIFETIME_SECONDS


@pytest.mark.browser
class TestOwnerCookieInABrowser:
    """
    The owner cookie an htmx submit sets persists in Chromium, out of scripts' reach.

    The token persists only if a browser processes ``Set-Cookie`` on an htmx
    XHR response exactly as it does on a navigation. Were that false the whole
    ownership gate would be decorative, so it is asserted here instead of
    reasoned about.
    """

    def test_scan_submit_sets_a_persistent_owner_cookie(
        self, page: Page, scan_harness: _ScanHarness
    ) -> None:
        """A real htmx submit leaves an HttpOnly, Lax, year-long cookie behind."""
        server = scan_harness.server
        server.scanner.gate.clear()
        page.goto(server.url)

        page.locator("#scan-btn").click()
        expect(page.locator("#status-area p.busy-line")).to_be_visible()

        minted = [
            cookie
            for cookie in page.context.cookies()
            if cookie["name"] == _OWNER_COOKIE_NAME
        ]
        assert len(minted) == 1
        cookie = minted[0]
        assert cookie["httpOnly"] is True
        assert cookie["sameSite"] == "Lax"
        assert cookie["path"] == "/"
        assert _outlives_364_days(cookie["expires"]), cookie["expires"]
        assert cookie["secure"] is False
        # HttpOnly proved from inside the page, not from the header: this is
        # the claim that the token cannot reach a script, and there
        # is no script file that could read it in the first place.
        assert _OWNER_COOKIE_NAME not in page.evaluate("() => document.cookie")

    def test_a_non_owning_browser_gets_no_flip_buttons_in_the_dom(
        self, page: Page, browser_server: _BrowserServer
    ) -> None:
        """
        The gate leaves the buttons out of the DOM; it does not hide them.

        A CSS-hidden control is still in the page and still reachable from the
        console, which would be an ASVS V4 failure. This asserts absence in a
        real browser rather than absence from a string.
        """
        app = browser_server.app
        job_store: JobStore = app.state.job_store
        worker = app.state.worker
        job = job_store.create_job(
            profile="duplex",
            title="Someone Elses Flip",
            owner_token="a-browser-that-is-not-this-one",
        )
        job_store.update_state(job.id, JobState.AWAITING_FLIP)
        coordinator = WorkerFlipCoordinator(job.id)
        coordinator.arm()
        worker._current_job_id = job.id
        worker._flip_coordinator = coordinator
        try:
            page.goto(browser_server.url)

            status = page.locator("#status-area")
            deadline = worker.flip_deadline(job.id)
            assert deadline is not None
            expect(status).to_contain_text(
                non_owner_wait_line(JobState.AWAITING_FLIP, deadline=deadline)
            )
            assert status.locator("button").count() == 0
            assert coordinator.answer is None
        finally:
            # Session-scoped server and store, so the pointers are cleared and
            # the row deleted, as the other flip browser test does.
            worker._flip_coordinator = None
            worker._current_job_id = None
            job_store.delete_job(job.id)


@pytest.mark.browser
class TestProfileDescriptionSwap:
    """The sentence under the select follows the selection, live."""

    def test_choosing_another_profile_swaps_the_description_in_place(
        self, page: Page, browser_server_url: str
    ) -> None:
        """
        A change on the select replaces the slot's text and nothing else.

        This is the one claim the server-side tests cannot make: that htmx
        really issues the GET on ``change``, that the select's own value rides
        it, and that the swap lands inside the slot rather than over it.  The
        element identity is asserted after the swap, which is what
        ``innerHTML`` buys and ``outerHTML`` would lose along with the id
        ``aria-describedby`` points at and the live region.
        """
        page.goto(browser_server_url)
        slot = page.locator("#profile-description")
        expect(slot).to_have_text(_FLATBED_DESCRIPTION)

        page.select_option("#profile-select", "duplex")

        expect(slot).to_have_text(_FEEDER_DESCRIPTION)
        expect(page.locator("#profile-description")).to_have_count(1)
        expect(slot).to_have_attribute("aria-live", "polite")
        expect(page.locator("#profile-select")).to_have_attribute(
            "aria-describedby", "profile-description"
        )

    def test_the_description_is_the_controls_only_help_line(
        self, page: Page, browser_server_url: str
    ) -> None:
        """
        One help line under Profile, and it is the live one.

        The adjacent-sibling selector is Pico's own help-text rule, so this
        asserts the styling hook and the "no second help line" contract at
        once: if anything were inserted between the control and the slot, the
        muted styling would go with it.
        """
        page.goto(browser_server_url)

        helps = page.locator("#profile-select + small")

        expect(helps).to_have_count(1)
        expect(helps).to_have_id("profile-description")


# The third profile the Multiple pages tests switch to: a feeder that is not
# manual duplex, so a tick has somewhere to go that allows it.
_MULTI_PAGE_FEEDER = "feeder"
_MULTI_PAGE_FEEDER_DESCRIPTION = "Scans every page in the document feeder."


@pytest.fixture
def multi_page_server(
    tmp_path: Path, egress_allowlist: list[str]
) -> Iterator[_BrowserServer]:
    """
    Serve a private app with a flatbed, a manual-duplex and a plain feeder profile.

    Private because the session server has only the first two, and the tick
    that survives a profile change needs a second profile that allows it.  No
    scan is started here, so Paperless needs no stub.
    """
    base = _browser_test_settings(tmp_path)
    profiles = {
        **base.profiles,
        _MULTI_PAGE_FEEDER: ProfileConfig(
            source="ADF", description=_MULTI_PAGE_FEEDER_DESCRIPTION
        ),
    }
    settings = base.model_copy(update={"profiles": profiles})
    with _serve(settings, _BrowserTestScanner()) as server:
        egress_allowlist.append(server.url)
        yield server


def _choose_profile(page: Page, profile: str) -> Locator:
    """
    Choose ``profile`` and wait for the Multiple pages field to be replaced.

    The field is swapped whole, so the old checkbox is marked first and the
    wait is for the mark to be gone.  Waiting for the response alone would let
    an assertion read the old checkbox, and a tick that survives the change
    reads the same on the old one as on the new one.

    The wait then continues until htmx has processed the new field, which it
    does only when the swap settles, a moment after the field is in the page.
    Until then the new field is not listening for the select's next change, so
    a test that changed the profile again straight away would lose that change
    some of the time.  htmx marks new content with ``htmx-added`` and removes
    the class in the same step that processes it, so the class going is the
    signal.

    Returns:
        The checkbox as it is after the swap.

    """
    page.locator("#multi-page").evaluate("box => box.setAttribute('data-stale', '')")
    page.select_option("#profile-select", profile)
    expect(page.locator("#multi-page[data-stale]")).to_have_count(0)
    expect(page.locator("#multi-page-field.htmx-added")).to_have_count(0)
    box = page.locator("#multi-page")
    expect(box).to_have_count(1)
    return box


def _drop_cache_control(route: Route) -> None:
    """Pass a request to the server and its response back without ``Cache-Control``."""
    response = route.fetch()
    headers = {
        name: value
        for name, value in response.headers.items()
        if name.lower() != "cache-control"
    }
    route.fulfill(response=response, headers=headers)


@pytest.mark.browser
class TestMultiPageCheckbox:
    """The Multiple pages checkbox in a real browser, including Firefox's reload."""

    def test_it_is_visible_enabled_and_unticked_on_load(
        self, page: Page, multi_page_server: _BrowserServer
    ) -> None:
        """Named by its label, described by its help line, never ticked on load."""
        page.goto(multi_page_server.url)
        box = page.locator("#multi-page")

        expect(box).to_be_visible()
        expect(box).to_be_enabled()
        expect(box).not_to_be_checked()
        expect(box).to_have_accessible_name(MULTI_PAGE_LABEL)
        expect(box).to_have_accessible_description(MULTI_PAGE_HELP)
        expect(page.locator("#multi-page-help")).to_be_visible()

    def test_manual_duplex_disables_it_and_another_profile_restores_it(
        self, page: Page, multi_page_server: _BrowserServer
    ) -> None:
        """
        The reason replaces the help line while manual duplex is chosen.

        A disabled checkbox is not sent with the refresh, so coming back from
        manual duplex always finds it unticked, even if it was ticked before.
        """
        page.goto(multi_page_server.url)
        page.locator("#multi-page").check()

        box = _choose_profile(page, "duplex")

        expect(box).to_be_disabled()
        expect(box).not_to_be_checked()
        expect(page.locator("#multi-page-help")).to_be_visible()
        expect(box).to_have_accessible_description(MULTI_PAGE_DISABLED_REASON)

        box = _choose_profile(page, "default")

        expect(box).to_be_enabled()
        expect(box).not_to_be_checked()
        expect(box).to_have_accessible_description(MULTI_PAGE_HELP)

    def test_a_tick_survives_a_change_to_another_profile_that_allows_it(
        self, page: Page, multi_page_server: _BrowserServer
    ) -> None:
        """A profile switch must not silently drop a choice."""
        page.goto(multi_page_server.url)
        page.locator("#multi-page").check()

        box = _choose_profile(page, _MULTI_PAGE_FEEDER)

        expect(box).to_be_enabled()
        expect(box).to_be_checked()

    @pytest.mark.parametrize("served", ["as-served", "without-no-store"])
    def test_multi_page_checkbox_unchecked_on_reload_in_firefox(
        self,
        playwright: Playwright,
        browser_server_url: str,
        egress_allowlist: list[str],
        served: Literal["as-served", "without-no-store"],
    ) -> None:
        """
        A tick does not come back when Firefox reloads the page.

        Firefox restores a form control's state on reload unless something
        stops it, and Chromium does not, so this is the one browser that can
        tell.  The page's ``Cache-Control: no-store`` and the checkbox's
        ``autocomplete="off"`` each stop it alone, so the second case strips
        ``no-store`` on the way to the browser and leaves the attribute as the
        only guard; the first keeps the page exactly as served.  The read after
        the reload is a single ``is_checked``, so a retry cannot pass on a
        moment before a restore.
        """
        blocked: list[str] = []
        seen: list[str] = []
        violations: list[str] = []
        firefox = playwright.firefox.launch()
        try:
            ctx = firefox.new_context()
            ctx.route("**/*", _make_gate(blocked, egress_allowlist, seen))
            if served == "without-no-store":
                # Registered after the gate, so it takes the page request
                # itself; the page's address is on the allowlist either way.
                ctx.route(f"{browser_server_url}/", _drop_cache_control)
            _make_csp_gate(ctx, violations)
            page = ctx.new_page()
            page.goto(browser_server_url)
            box = page.locator("#multi-page")
            box.check()
            expect(box).to_be_checked()

            page.reload()

            expect(box).to_have_count(1)
            assert not box.is_checked(), "Firefox restored the tick on reload"
        finally:
            firefox.close()
        assert seen, "the hand-built context's gate handled no request"
        assert blocked == [], f"a page tried to reach the network: {blocked}"
        assert violations == [], (
            f"a page violated its Content-Security-Policy: {violations}"
        )


# The tags the filter tests run against. The shared browser server points at a
# closed Paperless port, so its tag list is empty by design; these three are
# patched in for the tests that need something to tick, and the ids are well
# clear of any other number on the page so a value assertion cannot match by
# accident.
_BROWSER_TAGS: list[dict[str, object]] = [
    {"id": 11, "name": "receipt"},
    {"id": 12, "name": "invoice"},
    {"id": 13, "name": "Recipes"},
]


_PAGE_LOAD_TAGS: list[dict[str, object]] = [
    {"id": 31, "name": "bank"},
    {"id": 32, "name": "school"},
]
_PAGE_LOAD_CORRESPONDENTS: list[dict[str, object]] = [
    {"id": 41, "name": "Acme Water"},
]


@pytest.mark.browser
class TestPageLoadAsksForNoList:
    """
    The page asks for both lists once, together, after it has rendered.

    ``/`` renders the lists loading, and its one loader asks
    ``/api/metadata`` for both: only the server can tell when both are done,
    which is when Scan is released.  So a load asks that route exactly once
    and never ``/api/tags`` or ``/api/correspondents``, and each refresh
    button still fetches and swaps its own list.
    """

    def test_one_metadata_request_on_load_and_both_refreshes_still_swap(
        self, page: Page, tmp_path: Path, egress_allowlist: list[str]
    ) -> None:
        """
        A load asks ``/api/metadata`` once and no per-list route; refreshes swap.

        The lazy request and the refresh requests are recorded by the same
        listener, so the empty record of per-list requests at load is not a
        listener that heard nothing at all.
        """
        tags = list(_PAGE_LOAD_TAGS)
        correspondents = list(_PAGE_LOAD_CORRESPONDENTS)
        requested: list[str] = []

        def _record(request: Request) -> None:
            requested.append(urlsplit(request.url).path)

        with _serve(_browser_test_settings(tmp_path), _BrowserTestScanner()) as server:
            egress_allowlist.append(server.url)
            paperless = server.app.state.paperless
            paperless.get_tags = lambda *, timeout=None: list(tags)
            paperless.get_correspondents = lambda *, timeout=None: list(correspondents)

            page.on("request", _record)
            page.goto(server.url)
            _await_the_lists(page)
            page.wait_for_load_state("networkidle")

            on_load = [
                path
                for path in requested
                if path in {"/api/tags", "/api/correspondents", "/api/metadata"}
            ]
            assert on_load == ["/api/metadata"], on_load
            expect(page.locator("label.tag-option")).to_have_count(len(tags))
            expect(
                page.locator('#correspondent-select option[value="41"]')
            ).to_have_text("Acme Water")

            tags.append({"id": 33, "name": "garden"})
            correspondents.append({"id": 42, "name": "Globex Power"})
            with page.expect_response(
                lambda r: r.url.endswith("resource=tags")
            ) as tag_refresh:
                page.click("#tags-refresh")
            assert tag_refresh.value.status == 200
            expect(page.locator("label.tag-option")).to_have_count(len(tags))
            expect(page.locator("#tags-list")).to_contain_text("garden")

            with page.expect_response(
                lambda r: r.url.endswith("resource=correspondents")
            ) as correspondent_refresh:
                page.click("#correspondents-refresh")
            assert correspondent_refresh.value.status == 200
            expect(
                page.locator('#correspondent-select option[value="42"]')
            ).to_have_text("Globex Power")

        assert requested.count("/api/cache/invalidate") == 2, requested


@pytest.fixture
def tagged_server(browser_server: _BrowserServer) -> Iterator[_BrowserServer]:
    """
    Serve a fixed tag list from the shared server for the length of one test.

    The client is patched rather than a third server started: the tag list is
    the one thing on this page that comes from Paperless at request time, so
    changing what the client answers is enough. The cache is dropped on the way
    in and on the way out, so neither this test nor the next one reads the
    other's list.
    """
    paperless = browser_server.app.state.paperless
    original = paperless.get_tags
    # The keyword is the real client's: every list route, the page's lazy
    # list load among them, and the check a scan makes before it starts ask
    # with a timeout.
    paperless.get_tags = lambda *, timeout=None: list(_BROWSER_TAGS)
    browser_server.app.state.cache.invalidate("tags")
    try:
        yield browser_server
    finally:
        paperless.get_tags = original
        browser_server.app.state.cache.invalidate("tags")


@pytest.mark.browser
class TestTagFilterInChromium:
    """
    The tag picker keeps a filtered-out tick and never submits the filter text.

    Neither is reachable from a server-side test. The first is about what the
    DOM holds after an htmx swap, and the second is about which form a browser
    considers an input to belong to -- the HTML form-owner association, which
    only a browser implements.
    """

    def _load_tags(self, page: Page, url: str) -> None:
        """
        Open the page and wait for its server-rendered tag list.

        Args:
            page: The browser page.
            url: The test server's base URL.

        """
        page.goto(url)
        expect(page.locator("label.tag-option")).to_have_count(len(_BROWSER_TAGS))

    def test_filtering_keeps_a_ticked_tag_and_pins_it_above_the_list(
        self, page: Page, tagged_server: _BrowserServer
    ) -> None:
        """
        A tick the filter excludes stays in the DOM, above the list.

        A swap that re-rendered only the matches would take an already-chosen tag out of the document, and a tag
        that is not in the document is not in the next submit either -- with
        nothing to warn the user, who would simply find it was not applied.
        """
        self._load_tags(page, tagged_server.url)
        page.check('#tags-list input[value="11"]')
        page.check('#tags-list input[value="12"]')

        with page.expect_response(lambda r: "q=rec" in r.url):
            page.locator("#tag-filter").press_sequentially("rec")

        # Three rows again: the two matches, plus "invoice" pinned above them
        # because it is ticked and the filter excludes it.
        expect(page.locator("label.tag-option")).to_have_count(3)
        expect(page.locator('#tags-list input[value="12"]')).to_be_checked()
        expect(page.locator('#tags-list input[value="11"]')).to_be_checked()
        values = page.locator("#tags-list input[type=checkbox]").evaluate_all(
            "boxes => boxes.map(box => box.value)"
        )
        assert values[0] == "12", values

    def test_a_scan_submits_every_ticked_tag_and_never_the_filter_text(
        self, page: Page, tagged_server: _BrowserServer
    ) -> None:
        """
        The filtered-out tick rides along and the filter text does not.

        The submit is intercepted rather than served, so this reads what the
        browser actually put on the wire without starting a real scan on the
        shared server. A page route takes precedence over the context's egress
        gate, so the gate does not see this request at all.
        """
        submitted: list[str] = []

        def _capture(route: Route) -> None:
            submitted.append(route.request.post_data or "")
            route.fulfill(status=200, body="")

        self._load_tags(page, tagged_server.url)
        page.check('#tags-list input[value="11"]')
        page.check('#tags-list input[value="12"]')
        with page.expect_response(lambda r: "q=rec" in r.url):
            page.locator("#tag-filter").press_sequentially("rec")

        page.route("**/api/scan", _capture)
        with page.expect_request("**/api/scan"):
            page.click("#scan-btn")

        assert submitted, "no scan submit was captured"
        fields = parse_qs(submitted[0])
        assert sorted(fields.get("tags", [])) == ["11", "12"], fields
        assert "q" not in fields, fields

    def test_enter_in_the_filter_filters_the_list_and_starts_no_scan(
        self, page: Page, tagged_server: _BrowserServer
    ) -> None:
        """
        Enter performs implicit submission of the filter's own form.

        Without the form attribute the input would belong to the scan form, and
        a household member typing a filter and pressing Enter would start a
        scan. The filter form is empty apart from this input and has no submit
        button, which is exactly the shape the implicit-submission rules submit.
        """
        posted: list[str] = []

        def _record(request: Request) -> None:
            if request.method == "POST":
                posted.append(request.url)

        self._load_tags(page, tagged_server.url)
        page.on("request", _record)

        with page.expect_response(lambda r: "q=rec" in r.url):
            page.locator("#tag-filter").press_sequentially("rec")
            page.press("#tag-filter", "Enter")

        expect(page.locator("label.tag-option")).to_have_count(2)
        assert posted == [], posted

    def test_every_tag_row_clears_the_touch_target_floor(
        self, page: Page, tagged_server: _BrowserServer
    ) -> None:
        """
        Every row is at least 44 px tall and spans the list (WCAG 2.5.5).

        Measured in the browser, because this is a cascade outcome and not a
        stylesheet fact: Pico shrinks a checkbox label to the width of its text
        through `label:has([type=checkbox])`, a rule of exactly the same weight
        as the app's own, so what decides it is that app.css loads second. A
        source assertion on the rule would pass with that order reversed.
        """
        self._load_tags(page, tagged_server.url)
        list_box = page.locator("#tags-list").bounding_box()
        assert list_box is not None

        rows = page.locator("label.tag-option")
        for index in range(rows.count()):
            box = rows.nth(index).bounding_box()
            assert box is not None, index
            assert box["height"] >= 44, box
            assert box["width"] >= list_box["width"] - 2, (box, list_box)


# ---------------------------------------------------------------------------
# A profile's default tags and correspondent, in a real browser.
#
# The lists paperless-ngx is patched to answer.  Tag 99 is in neither, so the
# profile that names it has a default paperless-ngx does not have.
# ---------------------------------------------------------------------------

_DEFAULTS_TAGS: list[dict[str, object]] = [
    {"id": 31, "name": "bank"},
    {"id": 32, "name": "school"},
    {"id": 33, "name": "garden"},
]
_DEFAULTS_CORRESPONDENTS: list[dict[str, object]] = [
    {"id": 41, "name": "Acme Water"},
    {"id": 42, "name": "Globex Power"},
]
# The second profile, whose defaults differ from the first's in every id, and
# the third, whose one default tag paperless-ngx does not list.
_RECEIPTS = "receipts"
_ORPHANED = "orphaned"
_GONE_TAG = 99
# What the operator reads, spelled out rather than imported: the row's label
# before Scan, and the job's warning after it.
_GONE_TAG_LABEL = "tag 99 (no longer in paperless-ngx; will be skipped)"
_GONE_TAG_WARNING = "tag 99 no longer exists in paperless-ngx and was not applied."


def _answer_list(
    items: list[dict[str, object]],
) -> Callable[..., list[dict[str, object]]]:
    """
    Build a stand-in for ``get_tags`` or ``get_correspondents``.

    Args:
        items: The list it answers with.

    Returns:
        A callable taking the real client's keyword-only ``timeout``, which
        every list route and the check a scan makes before it starts pass.

    """

    def _answer(*, timeout: float | None = None) -> list[dict[str, object]]:
        """Answer a copy of the list, whatever the timeout."""
        return list(items)

    return _answer


@pytest.fixture
def defaults_server(
    tmp_path: Path, egress_allowlist: list[str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[_BrowserServer]:
    """
    Serve a private app whose profiles carry different metadata defaults.

    ``default`` ticks tag 31 and picks correspondent 41; ``receipts`` ticks
    32 and 33 and picks 42; ``orphaned`` ticks only tag 99, which paperless-ngx
    does not list, and picks no correspondent.  Both lists are patched in and
    every upload is delivered at once, so a real scan can finish.  Private,
    because the session server's profiles carry no defaults.
    """
    base = _browser_test_settings(tmp_path)
    profiles = {
        "default": ProfileConfig(
            description=_FLATBED_DESCRIPTION,
            default_tags=[31],
            default_correspondent=41,
        ),
        _RECEIPTS: ProfileConfig(default_tags=[32, 33], default_correspondent=42),
        _ORPHANED: ProfileConfig(default_tags=[_GONE_TAG]),
    }
    settings = base.model_copy(update={"profiles": profiles})
    with _serve(settings, _BrowserTestScanner()) as server:
        egress_allowlist.append(server.url)
        paperless = server.app.state.paperless
        monkeypatch.setattr(paperless, "get_tags", _answer_list(_DEFAULTS_TAGS))
        monkeypatch.setattr(
            paperless, "get_correspondents", _answer_list(_DEFAULTS_CORRESPONDENTS)
        )
        server.app.state.cache.invalidate("tags")
        server.app.state.cache.invalidate("correspondents")
        _make_paperless_deliver(server.app, monkeypatch)
        yield server


def _ticked_tags(page: Page) -> list[str]:
    """
    Return the value of every ticked tag box, in page order.

    Args:
        page: The browser page.

    Returns:
        The ticked boxes' values.

    """
    return page.locator("#tags-list input[type=checkbox]").evaluate_all(
        "boxes => boxes.filter(box => box.checked).map(box => box.value)"
    )


def _choose_profile_defaults(page: Page, profile: str) -> None:
    """
    Choose ``profile`` and wait until the tag list and the select are replaced.

    Both are swapped whole, so each is marked first and the wait is for the
    marks to be gone, then for htmx to have settled the new elements, as
    ``_choose_profile`` waits for the Multiple pages field.  The page's lazy
    list load lands first: it replaces both elements too, and would take the
    marks with it.

    Args:
        page: The browser page.
        profile: The profile to choose.

    """
    _await_the_lists(page)
    for target in ("#tags-list", "#correspondent-select"):
        page.locator(target).evaluate("node => node.setAttribute('data-stale', '')")
    page.select_option("#profile-select", profile)
    expect(page.locator("#tags-list[data-stale]")).to_have_count(0)
    expect(page.locator("#correspondent-select[data-stale]")).to_have_count(0)
    expect(
        page.locator("#tags-list.htmx-added, #correspondent-select.htmx-added")
    ).to_have_count(0)


def _captured_submit(page: Page) -> dict[str, list[str]]:
    """
    Press Scan and return what the browser posted, without starting a scan.

    Args:
        page: The browser page.

    Returns:
        The posted form, field by field.

    """
    submitted: list[str] = []

    def _capture(route: Route) -> None:
        submitted.append(route.request.post_data or "")
        route.fulfill(status=200, body="")

    page.route("**/api/scan", _capture)
    with page.expect_request("**/api/scan"):
        page.click("#scan-btn")
    assert submitted, "no scan submit was captured"
    return parse_qs(submitted[0], keep_blank_values=True)


@pytest.mark.browser
class TestProfileDefaultsInTheBrowser:
    """The form opens on, follows, keeps and submits a profile's defaults."""

    def test_first_paint_ticks_the_defaults_and_an_untouched_submit_sends_them(
        self, page: Page, defaults_server: _BrowserServer
    ) -> None:
        """The first profile's tag is ticked, its correspondent chosen, and both sent."""
        page.goto(defaults_server.url)

        expect(page.locator("#profile-select")).to_have_value("default")
        expect(page.locator('#tags-list input[value="31"]')).to_be_checked()
        assert _ticked_tags(page) == ["31"]
        expect(page.locator("#correspondent-select")).to_have_value("41")

        fields = _captured_submit(page)

        assert fields["profile"] == ["default"], fields
        assert fields["tags"] == ["31"], fields
        assert fields["correspondent"] == ["41"], fields

    def test_choosing_another_profile_swaps_to_its_defaults(
        self, page: Page, defaults_server: _BrowserServer
    ) -> None:
        """
        The new profile's defaults replace the first's, ticks and choice alike.

        Tag 31 is unticked by the change, not by the operator: a profile's
        defaults are what the untouched form means, so the form never shows a
        mix of two profiles' answers.
        """
        page.goto(defaults_server.url)

        _choose_profile_defaults(page, _RECEIPTS)

        assert _ticked_tags(page) == ["32", "33"]
        expect(page.locator('#tags-list input[value="31"]')).not_to_be_checked()
        expect(page.locator("#correspondent-select")).to_have_value("42")

        _choose_profile_defaults(page, "default")

        assert _ticked_tags(page) == ["31"]
        expect(page.locator("#correspondent-select")).to_have_value("41")

        _choose_profile_defaults(page, _RECEIPTS)
        fields = _captured_submit(page)

        assert fields["profile"] == [_RECEIPTS], fields
        assert fields["tags"] == ["32", "33"], fields
        assert fields["correspondent"] == ["42"], fields

    @pytest.mark.parametrize("served", ["as-served", "without-no-store"])
    def test_firefox_reload_restores_the_server_rendered_defaults(
        self,
        playwright: Playwright,
        defaults_server: _BrowserServer,
        egress_allowlist: list[str],
        served: Literal["as-served", "without-no-store"],
    ) -> None:
        """
        A reload shows the profile's defaults, not the operator's hand edits.

        Firefox restores form state on reload unless something stops it, and
        Chromium does not, so Firefox is the browser that can tell.  The second
        case strips ``no-store`` from the page, so ``autocomplete="off"`` on the
        box and on the select is all that keeps an untick, or a picked
        correspondent, from coming back over the server's defaults.  The reads
        after the reload are single reads, so they cannot pass on a moment
        before a restore.
        """
        url = defaults_server.url
        blocked: list[str] = []
        seen: list[str] = []
        violations: list[str] = []
        firefox = playwright.firefox.launch()
        try:
            ctx = firefox.new_context()
            ctx.route("**/*", _make_gate(blocked, egress_allowlist, seen))
            if served == "without-no-store":
                ctx.route(f"{url}/", _drop_cache_control)
            _make_csp_gate(ctx, violations)
            page = ctx.new_page()
            page.goto(url)
            box = page.locator('#tags-list input[value="31"]')
            select = page.locator("#correspondent-select")
            expect(box).to_be_checked()
            box.uncheck()
            select.select_option("42")
            expect(box).not_to_be_checked()
            expect(select).to_have_value("42")

            page.reload()

            expect(box).to_have_count(1)
            assert box.is_checked(), "Firefox restored the untick on reload"
            assert select.input_value() == "41", "Firefox restored the choice"
        finally:
            firefox.close()
        assert seen, "the hand-built context's gate handled no request"
        assert blocked == [], f"a page tried to reach the network: {blocked}"
        assert violations == [], (
            f"a page violated its Content-Security-Policy: {violations}"
        )

    @pytest.mark.parametrize("served", ["as-served", "without-no-store"])
    def test_firefox_reload_after_a_profile_change_keeps_profile_and_ticks_together(
        self,
        playwright: Playwright,
        defaults_server: _BrowserServer,
        egress_allowlist: list[str],
        served: Literal["as-served", "without-no-store"],
    ) -> None:
        """
        A reload never pairs one profile with another's defaults.

        Restoring the Profile select fires no change, so nothing would follow
        it: the page would name ``receipts`` over the first profile's ticks,
        and an untouched Scan would file them under ``receipts``.  As in the
        other reload tests, the second case strips ``no-store`` so the
        select's own ``autocomplete="off"`` is what is being tested.  The
        submit is captured rather than served: what the browser sends is the
        claim, and the scan's handling of it is covered without a browser.

        The reads after the reload are single reads, not retrying assertions,
        so they cannot pass on a moment before a restore.
        """
        url = defaults_server.url
        blocked: list[str] = []
        seen: list[str] = []
        violations: list[str] = []
        firefox = playwright.firefox.launch()
        try:
            ctx = firefox.new_context()
            ctx.route("**/*", _make_gate(blocked, egress_allowlist, seen))
            if served == "without-no-store":
                ctx.route(f"{url}/", _drop_cache_control)
            _make_csp_gate(ctx, violations)
            page = ctx.new_page()
            page.goto(url)
            _choose_profile_defaults(page, _RECEIPTS)
            assert _ticked_tags(page) == ["32", "33"]

            page.reload()

            _await_the_lists(page)
            profile = page.locator("#profile-select").input_value()
            ticks = _ticked_tags(page)
            correspondent = page.locator("#correspondent-select").input_value()
            assert profile == "default", "Firefox restored the profile on reload"
            assert ticks == ["31"], ticks
            assert correspondent == "41", correspondent

            fields = _captured_submit(page)
        finally:
            firefox.close()
        assert fields["profile"] == ["default"], fields
        assert fields["tags"] == ["31"], fields
        assert fields["tags_profile"] == ["default"], fields
        assert fields["correspondent_profile"] == ["default"], fields
        assert seen, "the hand-built context's gate handled no request"
        assert blocked == [], f"a page tried to reach the network: {blocked}"
        assert violations == [], (
            f"a page violated its Content-Security-Policy: {violations}"
        )

    def test_a_failed_profile_swap_files_the_chosen_profile_s_defaults(
        self, page: Page, defaults_server: _BrowserServer
    ) -> None:
        """
        Ticks left over from the previous profile never reach the scan.

        Both profile-change requests are cut off, so the list and the select
        keep the first profile's defaults while the select names
        ``receipts`` -- the state a slow or failed swap leaves behind.  The
        markers still name the first profile, so the scan files
        ``receipts``' own defaults rather than the ticks on screen.
        """
        page.goto(defaults_server.url)
        _await_the_lists(page)
        page.route("**/api/profiles/tags*", lambda route: route.abort())
        page.route("**/api/profiles/correspondent*", lambda route: route.abort())
        with page.expect_request("**/api/profiles/correspondent*"):
            page.select_option("#profile-select", _RECEIPTS)
        assert _ticked_tags(page) == ["31"]
        page.fill("#title-input", "Swap Cut Off")

        with page.expect_request("**/api/scan") as submitted:
            page.click("#scan-btn")

        fields = parse_qs(submitted.value.post_data or "")
        assert fields["profile"] == [_RECEIPTS], fields
        assert fields["tags"] == ["31"], fields
        assert fields["tags_profile"] == ["default"], fields
        status = page.locator("#status-area")
        expect(status.locator(".status-done").first).to_be_visible(timeout=15_000)
        job_store: JobStore = defaults_server.app.state.job_store
        job = next(
            job for job in job_store.list_recent(100) if job.title == "Swap Cut Off"
        )
        assert job.profile == _RECEIPTS
        assert job.tags == [32, 33]
        assert job.correspondent == 42

    def test_stale_defaults_stay_ticked_with_their_note_and_end_warned(
        self, page: Page, defaults_server: _BrowserServer
    ) -> None:
        """
        A default paperless-ngx lost is shown, sent, dropped and named.

        The row stays ticked so the untouched submit still carries the id;
        the scan then drops it and the job ends as an upload with a warning
        that names the tag.
        """
        page.goto(defaults_server.url)
        _choose_profile_defaults(page, _ORPHANED)

        stale = page.locator(f'#tags-list input[value="{_GONE_TAG}"]')
        expect(stale).to_be_checked()
        assert _ticked_tags(page) == [str(_GONE_TAG)]
        # The row's own text is the label: the box inside it has none.
        row = page.locator("#tags-list label.tag-option").filter(
            has=page.locator(f'input[value="{_GONE_TAG}"]')
        )
        expect(row).to_have_text(_GONE_TAG_LABEL)
        page.fill("#title-input", "Stale Default")

        with page.expect_request("**/api/scan") as submitted:
            page.click("#scan-btn")

        fields = parse_qs(submitted.value.post_data or "")
        assert fields["tags"] == [str(_GONE_TAG)], fields
        status = page.locator("#status-area")
        expect(status.locator(".status-fallback").first).to_be_visible(timeout=15_000)
        expect(status).to_contain_text("Uploaded with a warning: Stale Default")
        expect(status).to_contain_text(_GONE_TAG_WARNING)
        expect(status).not_to_contain_text("Done: Stale Default")

    def test_unticked_stale_defaults_are_not_sent_and_end_clean(
        self, page: Page, defaults_server: _BrowserServer
    ) -> None:
        """Unticked, the id is not submitted and the scan is a plain Done."""
        page.goto(defaults_server.url)
        _choose_profile_defaults(page, _ORPHANED)
        page.uncheck(f'#tags-list input[value="{_GONE_TAG}"]')
        page.fill("#title-input", "Cleared Default")

        with page.expect_request("**/api/scan") as submitted:
            page.click("#scan-btn")

        fields = parse_qs(submitted.value.post_data or "")
        assert "tags" not in fields, fields
        status = page.locator("#status-area")
        expect(status.locator(".status-done").first).to_be_visible(timeout=15_000)
        expect(status).to_contain_text("Done: Cleared Default")
        expect(status).not_to_contain_text(_GONE_TAG_WARNING)


# ---------------------------------------------------------------------------
# The lists arrive after the page.
#
# ``/`` asks paperless-ngx for nothing: it renders each list loading and Scan
# held, and one lazy request brings both lists and releases Scan, whether they
# arrived or could not be loaded.  A list that could not be loaded asks again
# by itself until it can.
# ---------------------------------------------------------------------------

# A budget short enough that a fetch into the black hole gives up within the
# test, and long enough that the held page can be read first.
_SHORT_FETCH_BUDGET = httpx2.Timeout(2.0, connect=2.0)

# A budget a fetch into the black hole always waits out, long enough to put
# its failure's end well after the moment it was asked.
_BLACK_HOLE_FETCH_BUDGET = httpx2.Timeout(1.0, connect=1.0)

# The limit the page must render within with paperless-ngx black-holed.
_PAGE_RENDER_LIMIT_MS = 1000

# Seconds to wait for the page's lazy list load to reach a test's route.
_HELD_LOAD_BUDGET = 10.0


@pytest.fixture
def black_holed_paperless(socket_guard: SocketGuard) -> Iterator[socket.socket]:
    """
    Listen on a loopback port and never accept, so paperless-ngx never answers.

    The kernel completes each connection into the listen queue, so a client
    connects and sends its request, and then waits for an answer that never
    comes: a paperless-ngx that is up but hung, the worst case for a page
    that asks it anything.  Server-side only, so the browser's egress gate
    is unaffected.  Closed at teardown, if the test did not close it first.
    The port stays allowed through the socket guard after the hole closes,
    so a fetch made then is refused by the kernel, as it would be in use.

    Yields:
        The listening socket.

    """
    hole = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    hole.bind(("127.0.0.1", 0))
    hole.listen()
    socket_guard.allow_port(hole.getsockname()[1])
    try:
        yield hole
    finally:
        hole.close()


@contextmanager
def _serve_black_holed(
    hole: socket.socket, tmp_path: Path, egress_allowlist: list[str]
) -> Generator[_BrowserServer]:
    """
    Serve a private app whose paperless-ngx is ``hole``, with nothing cached.

    The hole is closed before the server stops: the kernel then resets every
    connection still queued on it, so a fetch still waiting fails at once
    rather than holding the shutdown for its whole budget.

    Args:
        hole: The black-holed paperless-ngx.
        tmp_path: Where the app keeps its files.
        egress_allowlist: The browser gate's list, which the server joins.

    Yields:
        The running server.

    """
    url = f"http://127.0.0.1:{hole.getsockname()[1]}"
    base = _browser_test_settings(tmp_path)
    settings = base.model_copy(
        update={"paperless": PaperlessConfig(url=url, token="fake-token")}
    )
    with _serve(settings, _BrowserTestScanner()) as server:
        egress_allowlist.append(server.url)
        try:
            yield server
        finally:
            hole.close()


def _hold_the_list_load(page: Page) -> list[Route]:
    """
    Hold every lazy list load the page sends, unanswered, until released.

    Each held request is kept, so a test can read the page as it stands
    while the lists are loading and then let the answer through with
    ``continue_``.

    Args:
        page: The browser page, before it loads.

    Returns:
        The held requests, in order, filled as they are sent.

    """
    held: list[Route] = []

    def _hold(route: Route) -> None:
        held.append(route)

    page.route(lambda url: urlsplit(url).path == "/api/metadata", _hold)
    return held


def _await_the_held_load(page: Page, held: list[Route]) -> Route:
    """
    Wait until the page's loader has sent its request, and return it held.

    Args:
        page: The browser page, loading.
        held: What ``_hold_the_list_load`` returned.

    Returns:
        The one held request.

    """
    expect(page.locator("#metadata-loader")).to_have_class(
        re.compile(r"\bhtmx-request\b")
    )

    def _delivered() -> bool:
        # The page marks the loader before the request reaches the route, and
        # a synchronous Playwright client delivers the route to its handler
        # only while it talks to the browser, so each check is a round trip.
        page.evaluate("() => true")
        return bool(held)

    assert poll_until(_delivered, _HELD_LOAD_BUDGET), "the list load was never held"
    assert len(held) == 1, held
    return held[0]


def _flagged_list(
    answering: threading.Event, items: list[dict[str, object]]
) -> Callable[..., list[dict[str, object]]]:
    """
    Build a list fetch that fails until ``answering`` is set, then answers.

    Args:
        answering: Set when paperless-ngx comes back.
        items: What it answers with then.

    Returns:
        A stand-in for ``get_tags`` taking the real client's ``timeout``.

    """

    def _fetch(
        *, timeout: float | httpx2.Timeout | None = None
    ) -> list[dict[str, object]]:
        """Fail as an unreachable paperless-ngx does, or answer the list."""
        del timeout
        if not answering.is_set():
            msg = "paperless unreachable"
            raise ConnectionError(msg)
        return list(items)

    return _fetch


# Counts the page's requests from send until htmx has handled the answer, and
# lists the ones finished.  The loadend listener is added after htmx's own load
# handler, which swaps at once, and loadend comes after load, so a request
# counted finished has been swapped, and whatever its answer triggered has
# already been sent and counted.
_COUNT_REQUESTS = """
(() => {
    window.__requestsInFlight = 0;
    window.__requestsFinished = [];
    const open = XMLHttpRequest.prototype.open;
    const send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
        this.__requestUrl = String(url);
        return open.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.send = function (...args) {
        window.__requestsInFlight += 1;
        this.addEventListener("loadend", () => {
            window.__requestsInFlight -= 1;
            window.__requestsFinished.push(this.__requestUrl);
        });
        return send.apply(this, args);
    };
})();
"""

# True once a list retry has been answered and handled, and htmx has settled.
_RETRY_HANDLED = """
() => window.__requestsFinished.some(
        (url) => url.startsWith("/api/metadata") && !/[?&]profile=/.test(url)
    )
    && document.querySelector(".htmx-added, .htmx-settling") === null
"""

# True once nothing the page sent is still unanswered, and htmx has settled.
_REQUESTS_SETTLED = """
() => window.__requestsInFlight === 0
    && document.querySelector(".htmx-added, .htmx-settling") === null
"""

# How many of the tag list's retries the page has had answered and handled.
_TAG_RETRIES_ANSWERED = (
    "window.__requestsFinished.filter("
    '(url) => url.includes("/api/metadata/probe?resource=tags")).length'
)

# The two requests a profile change sends for the lists.
_PROFILE_CHANGE_PATHS = frozenset({"/api/profiles/tags", "/api/profiles/correspondent"})


def _is_list_retry(url: str) -> bool:
    """
    Say whether a request is a list asking again rather than the page's load.

    The page's first list load names the profile it shows; a list asking
    again after it could not be loaded names none.

    Args:
        url: The request's URL.

    Returns:
        Whether the request is a list retry.

    """
    parts = urlsplit(url)
    return parts.path.startswith("/api/metadata") and "profile" not in parse_qs(
        parts.query
    )


def _await_held(page: Page, held: list[Route], count: int) -> None:
    """
    Wait until ``count`` requests have been held.

    Args:
        page: The browser page.
        held: The held requests, filled as they are sent.
        count: How many to wait for.

    """

    def _arrived() -> bool:
        # A synchronous Playwright client delivers a route to its handler
        # only while it talks to the browser, so each check is a round trip.
        page.evaluate("() => true")
        return len(held) >= count

    assert poll_until(_arrived, _HELD_LOAD_BUDGET), ("never held", count, held)


def _fail_a_list(
    server: _BrowserServer,
    monkeypatch: pytest.MonkeyPatch,
    resource: Literal["tags", "correspondents"],
) -> threading.Event:
    """
    Make one list unloadable until the returned event is set.

    The failure is remembered for one second rather than fifteen, and the
    retry's floor is lowered to match, so the page asks again within a test.

    Args:
        server: The server whose paperless-ngx client to patch.
        monkeypatch: The test's monkeypatch.
        resource: The list to fail.

    Returns:
        Set it to have paperless-ngx answer the list.

    """
    monkeypatch.setattr(cache_module, "NEGATIVE_TTL_SECONDS", 1.0)
    monkeypatch.setattr(routes_module, "METADATA_RETRY_FLOOR_SECONDS", 1)
    answering = threading.Event()
    if resource == "tags":
        fetch = _flagged_list(answering, _DEFAULTS_TAGS)
        monkeypatch.setattr(server.app.state.paperless, "get_tags", fetch)
    else:
        fetch = _flagged_list(answering, _DEFAULTS_CORRESPONDENTS)
        monkeypatch.setattr(server.app.state.paperless, "get_correspondents", fetch)
    server.app.state.cache.invalidate(resource)
    return answering


def _hold_the_first_retry(
    page: Page, *, hold_profile_changes: bool
) -> tuple[list[Route], list[Route]]:
    """
    Hold the page's first list retry, and its profile-change requests if asked.

    Every other request goes through.  Requests are counted, so a test can
    wait until the page has handled them (see ``_COUNT_REQUESTS``).

    Args:
        page: The browser page, before it loads.
        hold_profile_changes: Whether to hold the lists' profile-change
            requests too.

    Returns:
        The held retry and the held profile-change requests, each filled as
        they are sent.

    """
    page.add_init_script(_COUNT_REQUESTS)
    retries: list[Route] = []
    changes: list[Route] = []

    def _route(route: Route) -> None:
        url = route.request.url
        if _is_list_retry(url) and not retries:
            retries.append(route)
        elif hold_profile_changes and urlsplit(url).path in _PROFILE_CHANGE_PATHS:
            changes.append(route)
        else:
            route.continue_()

    page.route(lambda url: urlsplit(url).path.startswith("/api/"), _route)
    return retries, changes


def _scan_and_read_the_job(page: Page, server: _BrowserServer, title: str) -> Job:
    """
    Scan under ``title`` from the page, wait until it is done, and read its job.

    Args:
        page: The browser page, on the scan form.
        server: The server the page talks to.
        title: A title no other job in the test has.

    Returns:
        The finished job.

    """
    page.fill("#title-input", title)
    with page.expect_request("**/api/scan"):
        page.click("#scan-btn")
    status = page.locator("#status-area")
    expect(status.locator(".status-done").first).to_be_visible(timeout=15_000)
    job_store: JobStore = server.app.state.job_store
    return next(job for job in job_store.list_recent(100) if job.title == title)


@pytest.mark.browser
class TestLazyListsInTheBrowser:
    """
    The page renders at once, holds Scan for its lists, and recovers by itself.

    With paperless-ngx up but hung the page still renders within a second,
    because it asks paperless-ngx nothing.  Scan says why it waits, and one
    answer, lists or no lists, releases it.  A list that could not be loaded
    says so, and fills in by itself once paperless-ngx answers.
    """

    @pytest.mark.real_paperless_transport
    def test_index_renders_under_a_second_with_paperless_black_holed(
        self,
        page: Page,
        tmp_path: Path,
        egress_allowlist: list[str],
        black_holed_paperless: socket.socket,
    ) -> None:
        """The first request after start, nothing cached, renders in under 1 s."""
        with _serve_black_holed(
            black_holed_paperless, tmp_path, egress_allowlist
        ) as server:
            assert server.app.state.cache.get("tags") is None
            assert server.app.state.cache.get("correspondents") is None
            page.goto(server.url)
            loaded = page.evaluate(
                "() => performance.getEntriesByType('navigation')[0]"
                ".domContentLoadedEventEnd"
            )
            expect(page.locator("#tags-list")).to_have_text(TAGS_LOADING)
            expect(page.locator("#scan-btn")).to_be_disabled()

        assert 0 < loaded < _PAGE_RENDER_LIMIT_MS, loaded

    @pytest.mark.real_paperless_transport
    def test_lists_loading_holds_scan_then_releases_it(
        self,
        page: Page,
        tmp_path: Path,
        egress_allowlist: list[str],
        black_holed_paperless: socket.socket,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Scan waits, says why, and is released by lists that could not load.

        The fetch budget is shortened so the black hole's answer, none, lands
        within the test: a failed list releases Scan exactly as an arrived
        one does.
        """
        monkeypatch.setattr(
            routes_module, "_REQUEST_FETCH_TIMEOUT", _SHORT_FETCH_BUDGET
        )
        with _serve_black_holed(
            black_holed_paperless, tmp_path, egress_allowlist
        ) as server:
            page.goto(server.url)
            scan = page.locator("#scan-btn")
            hold = page.locator("#scan-hold-reason")

            expect(scan).to_be_disabled()
            expect(scan).to_have_attribute("aria-describedby", "scan-hold-reason")
            expect(hold).to_be_visible()
            expect(hold).to_have_text(
                scan_hold_reason(tags=True, correspondents=True) or ""
            )
            expect(page.locator("#tags-list")).to_have_text(TAGS_LOADING)
            expect(page.locator("#correspondent-help")).to_have_text(
                CORRESPONDENTS_LOADING
            )

            expect(page.locator("#tags-list")).to_contain_text(
                TAGS_UNAVAILABLE, timeout=15_000
            )
            expect(page.locator("#correspondent-help")).to_contain_text(
                CORRESPONDENTS_UNAVAILABLE
            )
            expect(hold).to_be_empty()
            expect(hold).to_be_hidden()
            expect(scan).to_be_enabled()
            expect(scan).not_to_have_attribute("aria-describedby", re.compile(".*"))
            # The loader is gone, and each unavailable list carries its retry.
            expect(page.locator("#metadata-loader")).to_have_count(0)
            for retry in (
                '#tags-list [hx-get="/api/metadata/probe?resource=tags"]',
                '#correspondent-help [hx-get="/api/metadata/probe'
                '?resource=correspondents"]',
            ):
                expect(page.locator(retry)).to_have_attribute(
                    "hx-trigger", re.compile(r"^every \d+s$")
                )

    def test_unavailable_lists_recover_without_a_click(
        self,
        page: Page,
        defaults_server: _BrowserServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Once paperless-ngx answers, the tag rows appear and the retry goes.

        The failure is remembered for one second here, not fifteen, and the
        retry's floor is lowered to match, so the retry asks paperless-ngx
        again within the test.  What the operator
        changed while the list was unavailable is carried through the
        recovery: the default tag they unticked stays unticked, and the
        correspondent they chose stays chosen.
        """
        monkeypatch.setattr(cache_module, "NEGATIVE_TTL_SECONDS", 1.0)
        monkeypatch.setattr(routes_module, "METADATA_RETRY_FLOOR_SECONDS", 1)
        answering = threading.Event()
        paperless = defaults_server.app.state.paperless
        monkeypatch.setattr(
            paperless, "get_tags", _flagged_list(answering, _DEFAULTS_TAGS)
        )
        defaults_server.app.state.cache.invalidate("tags")

        page.goto(defaults_server.url)
        tags_list = page.locator("#tags-list")
        expect(tags_list).to_contain_text(TAGS_UNAVAILABLE)
        _await_the_lists(page)
        retry = tags_list.locator('[hx-get="/api/metadata/probe?resource=tags"]')
        expect(retry).to_have_attribute("hx-trigger", "every 1s")
        expect(page.locator("#scan-btn")).to_be_enabled()
        default_tag = page.locator('#tags-list input[value="31"]')
        expect(default_tag).to_be_checked()
        default_tag.uncheck()
        page.select_option("#correspondent-select", "42")

        answering.set()

        expect(tags_list.locator("label.tag-option")).to_have_count(
            len(_DEFAULTS_TAGS), timeout=15_000
        )
        expect(tags_list).not_to_contain_text(TAGS_UNAVAILABLE)
        expect(retry).to_have_count(0)
        expect(page.locator('#tags-list input[value="31"]')).not_to_be_checked()
        expect(page.locator("#correspondent-select")).to_have_value("42")
        assert _ticked_tags(page) == []

    def test_a_focused_tag_keeps_focus_when_the_list_fills_in(
        self,
        page: Page,
        defaults_server: _BrowserServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Focus on a tag row survives the list filling in by itself.

        While the tags cannot be loaded, the profile's default tag is a
        focusable row of its own.  Focus is put there, then paperless-ngx
        answers and the list is swapped in with no action of the person's.
        Focus is on the same tag's box in the new list, not left on the page.
        """
        answering = _fail_a_list(defaults_server, monkeypatch, "tags")
        page.goto(defaults_server.url)
        tags_list = page.locator("#tags-list")
        expect(tags_list).to_contain_text(TAGS_UNAVAILABLE)
        _await_the_lists(page)
        page.locator('#tags-list input[value="31"]').focus()
        expect(page.locator('#tags-list input[value="31"]')).to_be_focused()

        answering.set()

        expect(tags_list.locator("label.tag-option")).to_have_count(
            len(_DEFAULTS_TAGS), timeout=15_000
        )
        expect(tags_list).not_to_contain_text(TAGS_UNAVAILABLE)
        expect(page.locator('#tags-list input[value="31"]')).to_be_focused()
        expect(page.locator('#tags-list input[value="31"]')).to_be_checked()

    def test_unavailable_correspondents_recover_with_no_correspondent_chosen(
        self,
        page: Page,
        defaults_server: _BrowserServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The correspondents fill in with "No correspondent" still chosen.

        The select sends "No correspondent" as an empty value, and the
        request that fills the list in once paperless-ngx answers carries it.
        Each retry before then is answered quietly, the alert slot stays
        empty, and the options appear with the choice left as it was.
        """
        answering = _fail_a_list(defaults_server, monkeypatch, "correspondents")

        page.goto(defaults_server.url)
        select = page.locator("#correspondent-select")
        expect(page.locator("#correspondent-help")).to_contain_text(
            CORRESPONDENTS_UNAVAILABLE
        )
        _await_the_lists(page)
        page.select_option("#correspondent-select", "")
        expect(select).to_have_value("")

        with page.expect_response(
            lambda r: "/api/metadata/probe?resource=correspondents" in r.url
        ) as retried:
            pass
        assert retried.value.status == 200
        assert "hx-trigger" not in retried.value.headers
        expect(page.locator("#status-message")).to_be_empty()

        with page.expect_response(
            lambda r: (
                urlsplit(r.url).path == "/api/correspondents"
                and re.search(r"[?&]correspondent=(&|$)", r.url) is not None
            ),
            timeout=15_000,
        ) as filled:
            answering.set()
        assert filled.value.status == 200

        expect(select.locator("option")).to_have_count(
            1 + len(_DEFAULTS_CORRESPONDENTS)
        )
        expect(page.locator("#correspondent-help")).not_to_contain_text(
            CORRESPONDENTS_UNAVAILABLE
        )
        expect(select).to_have_value("")
        expect(page.locator("#status-message")).to_be_empty()

    @pytest.mark.real_paperless_transport
    def test_every_retry_asks_paperless_ngx_again(
        self,
        page: Page,
        tmp_path: Path,
        egress_allowlist: list[str],
        black_holed_paperless: socket.socket,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        With paperless-ngx black-holed, each retry of the tag list asks it again.

        Every fetch waits out its whole one-second budget before it fails,
        and the cache remembers each failure for 1.5 s from when it ended.
        The retry asks 2 s after each answer lands, so it is past that memory
        every time, and every retry is a fetch.  A retry polling at a fixed
        2 s rate would ask one second after its last fetch failed, inside the
        memory, and every other ask would be answered without asking.
        """
        monkeypatch.setattr(
            routes_module, "_REQUEST_FETCH_TIMEOUT", _BLACK_HOLE_FETCH_BUDGET
        )
        monkeypatch.setattr(cache_module, "NEGATIVE_TTL_SECONDS", 1.5)
        monkeypatch.setattr(routes_module, "METADATA_RETRY_FLOOR_SECONDS", 2)
        page.add_init_script(_COUNT_REQUESTS)
        with _serve_black_holed(
            black_holed_paperless, tmp_path, egress_allowlist
        ) as server:
            paperless = server.app.state.paperless
            fetches: list[object] = []
            real_fetch = paperless.get_tags

            def _counted(
                *, timeout: float | httpx2.Timeout | None = None
            ) -> list[dict[str, object]]:
                """Note the fetch, then ask the black hole."""
                fetches.append(timeout)
                return real_fetch(timeout=timeout)

            monkeypatch.setattr(paperless, "get_tags", _counted)

            page.goto(server.url)
            tags_list = page.locator("#tags-list")
            expect(tags_list).to_contain_text(TAGS_UNAVAILABLE, timeout=15_000)
            expect(
                tags_list.locator('[hx-get="/api/metadata/probe?resource=tags"]')
            ).to_have_attribute("hx-trigger", "every 2s")
            page.wait_for_function(
                f"() => {_TAG_RETRIES_ANSWERED} >= 3", timeout=30_000
            )
            answered = page.evaluate(f"() => {_TAG_RETRIES_ANSWERED}")
            fetched = len(fetches)

        # One fetch for the page's own load, and one for every retry.
        assert fetched == 1 + answered, (fetched, answered)

    def test_a_job_store_failure_during_the_load_releases_scan_quietly(
        self,
        page: Page,
        defaults_server: _BrowserServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The lists land and Scan is released though the job store cannot be read.

        The lazy request is held while the store's reads are broken, so it
        certainly meets the failure.  Nothing is written into the alert slot,
        the lists arrive with the profile's defaults, and Scan is released
        with its hold line emptied.
        """
        held = _hold_the_list_load(page)
        page.goto(defaults_server.url)
        load = _await_the_held_load(page, held)
        expect(page.locator("#scan-btn")).to_be_disabled()

        job_store: JobStore = defaults_server.app.state.job_store

        def _fail(*_args: object, **_kwargs: object) -> NoReturn:
            msg = "disk I/O error"
            raise sqlite3.OperationalError(msg)

        for name in ("get_job", "latest_run_job", "list_pending"):
            monkeypatch.setattr(job_store, name, _fail)
        with page.expect_response(
            lambda r: urlsplit(r.url).path == "/api/metadata"
        ) as answered:
            load.continue_()
        assert answered.value.status == 200

        _await_the_lists(page)
        expect(page.locator('#tags-list input[value="31"]')).to_be_checked()
        expect(page.locator("#correspondent-select")).to_have_value("41")
        expect(page.locator("#scan-btn")).to_be_enabled()
        expect(page.locator("#scan-hold-reason")).to_be_empty()
        expect(page.locator("#metadata-loader")).to_have_count(0)
        expect(page.locator("#status-message")).to_be_empty()

    def test_opening_profile_defaults_arrive_ticked(
        self, page: Page, defaults_server: _BrowserServer
    ) -> None:
        """
        The page shows the lists loading, then the opening profile's defaults.

        The lazy request is held, so the loading state is read for certain
        rather than raced; released, it ticks ``default``'s tag and selects
        its correspondent, with both profile markers naming it.
        """
        held = _hold_the_list_load(page)
        page.goto(defaults_server.url)
        load = _await_the_held_load(page, held)

        expect(page.locator("#tags-list")).to_have_text(TAGS_LOADING)
        expect(page.locator("#correspondent-select option")).to_have_count(1)
        expect(page.locator("#scan-btn")).to_be_disabled()

        load.continue_()

        expect(page.locator('#tags-list input[value="31"]')).to_be_checked()
        expect(page.locator("#correspondent-select")).to_have_value("41")
        _await_the_lists(page)
        assert _ticked_tags(page) == ["31"]
        expect(page.locator("#tags-profile")).to_have_value("default")
        expect(page.locator("#correspondent-profile")).to_have_value("default")
        expect(page.locator("#scan-btn")).to_be_enabled()
        expect(page.locator("#metadata-loader")).to_have_count(0)

    def test_profile_change_during_the_load_keeps_list_and_marker_in_step(
        self, page: Page, defaults_server: _BrowserServer
    ) -> None:
        """
        A profile chosen before the lists land is the one the form shows.

        The lazy request, asked for ``default``, is held while ``receipts``
        is chosen.  The change sends the load again for ``receipts`` and
        abandons the first request, so the answer for the abandoned profile
        never lands.  The lists, their markers and the Profile select
        then agree on ``receipts``, and the scan files its own defaults.
        """
        held = _hold_the_list_load(page)
        page.goto(defaults_server.url)
        first = _await_the_held_load(page, held)

        page.select_option("#profile-select", _RECEIPTS)
        expect(page.locator("#tags-profile")).to_have_value(_RECEIPTS)
        expect(page.locator("#correspondent-profile")).to_have_value(_RECEIPTS)
        expect(page.locator('#tags-list input[value="32"]')).to_be_checked()

        def _replaced() -> bool:
            # Each check is a round trip, so the route and the failure of the
            # abandoned request reach this client (see _await_the_held_load).
            page.evaluate("() => true")
            return len(held) == 2 and first.request.failure is not None

        assert poll_until(_replaced, _HELD_LOAD_BUDGET), (
            "the profile change did not replace the list load",
            len(held),
            first.request.failure,
        )
        second = held[1]
        assert parse_qs(urlsplit(second.request.url).query)["profile"] == [_RECEIPTS]

        second.continue_()
        _await_the_lists(page)

        expect(page.locator("#profile-select")).to_have_value(_RECEIPTS)
        expect(page.locator("#tags-profile")).to_have_value(_RECEIPTS)
        expect(page.locator("#correspondent-profile")).to_have_value(_RECEIPTS)
        expect(page.locator("#correspondent-select")).to_have_value("42")
        assert _ticked_tags(page) == ["32", "33"]
        page.fill("#title-input", "Changed While Loading")

        with page.expect_request("**/api/scan"):
            page.click("#scan-btn")

        status = page.locator("#status-area")
        expect(status.locator(".status-done").first).to_be_visible(timeout=15_000)
        job_store: JobStore = defaults_server.app.state.job_store
        job = next(
            job
            for job in job_store.list_recent(100)
            if job.title == "Changed While Loading"
        )
        assert job.profile == _RECEIPTS
        assert job.tags == [32, 33]
        assert job.correspondent == 42

    @pytest.mark.parametrize("lands", ["after the change", "during the change"])
    @pytest.mark.parametrize("failing", ["tags", "correspondents"])
    def test_a_retry_never_brings_back_the_profile_chosen_before(
        self,
        page: Page,
        defaults_server: _BrowserServer,
        monkeypatch: pytest.MonkeyPatch,
        failing: Literal["tags", "correspondents"],
        lands: str,
    ) -> None:
        """
        A list retry in flight across a profile change changes nothing.

        One list cannot be loaded, so the page asks for it again, and that
        request is held while Profile changes to ``receipts`` and
        paperless-ngx comes back.  It is let through after the change has
        landed, or while the change's own requests are still held and land
        after it, so the list it brings back is asked for during the change.
        Either way the form ends on ``receipts``' defaults with both markers
        naming ``receipts``, and the scan files those defaults.
        """
        answering = _fail_a_list(defaults_server, monkeypatch, failing)
        hold_the_change = lands == "during the change"
        retries, changes = _hold_the_first_retry(
            page, hold_profile_changes=hold_the_change
        )
        page.goto(defaults_server.url)
        _await_the_lists(page)
        if failing == "tags":
            expect(page.locator("#tags-list")).to_contain_text(TAGS_UNAVAILABLE)
        else:
            expect(page.locator("#correspondent-help")).to_contain_text(
                CORRESPONDENTS_UNAVAILABLE
            )
        _await_held(page, retries, 1)
        answering.set()
        defaults_server.app.state.cache.invalidate(failing)

        page.select_option("#profile-select", _RECEIPTS)
        if hold_the_change:
            _await_held(page, changes, 2)
            retries[0].continue_()
            page.wait_for_function(_RETRY_HANDLED)
            for change in changes:
                change.continue_()
        else:
            expect(page.locator("#tags-profile")).to_have_value(_RECEIPTS)
            expect(page.locator("#correspondent-profile")).to_have_value(_RECEIPTS)
            expect(page.locator("#correspondent-select")).to_have_value("42")
            retries[0].continue_()
            page.wait_for_function(_RETRY_HANDLED)
        page.wait_for_function(_REQUESTS_SETTLED)

        expect(page.locator("#profile-select")).to_have_value(_RECEIPTS)
        expect(page.locator("#tags-profile")).to_have_value(_RECEIPTS)
        expect(page.locator("#correspondent-profile")).to_have_value(_RECEIPTS)
        expect(page.locator("#correspondent-select")).to_have_value("42")
        assert _ticked_tags(page) == ["32", "33"]
        expect(page.locator("#tags-list")).not_to_contain_text(TAGS_UNAVAILABLE)
        expect(page.locator("#correspondent-help")).not_to_contain_text(
            CORRESPONDENTS_UNAVAILABLE
        )

        job = _scan_and_read_the_job(
            page, defaults_server, f"Retry for {failing} {lands}"
        )
        assert job.profile == _RECEIPTS
        assert job.tags == [32, 33]
        assert job.correspondent == 42

    def test_edits_made_while_a_retry_is_in_flight_survive_it(
        self,
        page: Page,
        defaults_server: _BrowserServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A tick and a choice made while a list retry is in flight are kept.

        Only the correspondents cannot be loaded.  While the page's request
        for them again is held, the default tag is unticked, another is
        ticked and "No correspondent" is chosen.  Then paperless-ngx answers
        and the request is let through: the correspondents fill in, and
        nothing the operator changed is put back.
        """
        answering = _fail_a_list(defaults_server, monkeypatch, "correspondents")
        retries, _ = _hold_the_first_retry(page, hold_profile_changes=False)
        page.goto(defaults_server.url)
        _await_the_lists(page)
        help_line = page.locator("#correspondent-help")
        expect(help_line).to_contain_text(CORRESPONDENTS_UNAVAILABLE)
        _await_held(page, retries, 1)

        page.locator('#tags-list input[value="31"]').uncheck()
        page.locator('#tags-list input[value="32"]').check()
        page.select_option("#correspondent-select", "")
        answering.set()
        defaults_server.app.state.cache.invalidate("correspondents")
        retries[0].continue_()
        page.wait_for_function(_RETRY_HANDLED)
        page.wait_for_function(_REQUESTS_SETTLED)

        select = page.locator("#correspondent-select")
        expect(select.locator("option")).to_have_count(
            1 + len(_DEFAULTS_CORRESPONDENTS)
        )
        expect(help_line).not_to_contain_text(CORRESPONDENTS_UNAVAILABLE)
        expect(select).to_have_value("")
        assert _ticked_tags(page) == ["32"]

    def test_a_released_scan_hides_the_hold_line(
        self, page: Page, defaults_server: _BrowserServer
    ) -> None:
        """
        A Scan button released during the load is not described as waiting.

        A status poll renders the button from the job alone, so a job ending
        during the load brings it back enabled while the hold line still
        says Scan waits.  That is done here by hand, with the lazy request
        held, as the poll's out-of-band swap would: the line must go with the
        wait it describes, and come back if the button is held again.
        """
        held = _hold_the_list_load(page)
        page.goto(defaults_server.url)
        load = _await_the_held_load(page, held)
        scan = page.locator("#scan-btn")
        hold = page.locator("#scan-hold-reason")
        try:
            expect(scan).to_be_disabled()
            expect(hold).to_be_visible()

            scan.evaluate("button => button.removeAttribute('disabled')")
            expect(scan).to_be_enabled()
            expect(hold).to_be_hidden()

            scan.evaluate("button => button.setAttribute('disabled', '')")
            expect(hold).to_be_visible()
        finally:
            load.continue_()
        _await_the_lists(page)
        expect(hold).to_be_hidden()

    def test_a_scan_released_during_the_load_files_the_profile_defaults(
        self, page: Page, defaults_server: _BrowserServer
    ) -> None:
        """
        Scan pressed while the lists still load files the profile's defaults.

        The page holds Scan until the lists land, but a status poll renders
        the button from the job alone: a job ending during the load brings it
        back enabled.  That is done here by hand, with the lazy request held,
        so the press certainly comes first.  The tag list holds no ticks and
        the select no choice, and the job still carries ``default``'s tag and
        correspondent, as an untouched form does once the lists are in.
        """
        held = _hold_the_list_load(page)
        page.goto(defaults_server.url)
        load = _await_the_held_load(page, held)
        scan = page.locator("#scan-btn")
        expect(scan).to_be_disabled()
        expect(page.locator("#tags-list")).to_have_text(TAGS_LOADING)

        scan.evaluate("button => button.removeAttribute('disabled')")
        page.fill("#title-input", "Released Early")
        try:
            with page.expect_response(
                lambda r: r.url.endswith("/api/scan")
            ) as submitted:
                scan.click()
            assert submitted.value.status == 200
        finally:
            load.continue_()

        status = page.locator("#status-area")
        expect(status.locator(".status-done").first).to_be_visible(timeout=15_000)
        job_store: JobStore = defaults_server.app.state.job_store
        job = next(
            job for job in job_store.list_recent(100) if job.title == "Released Early"
        )
        assert job.profile == "default"
        assert job.tags == [31]
        assert job.correspondent == 41


# ---------------------------------------------------------------------------
# Which profile the page opens on, and what the Profile select offers.
#
# Every other browser fixture lists ``default`` first, so none of them can tell
# "the page opens on default" from "the page opens on the first option".  Each
# set here puts something else first.
# ---------------------------------------------------------------------------

_OPENING_DESCRIPTION = "The everyday scan: bank letters, filed to Acme Water."
"""The description only ``default`` carries in the opening-profile set."""

_FLATBED_AND_FEEDER = DeviceCapabilities(
    sources=["Flatbed", "ADF"], resolutions=[150, 300], modes=["Color", "Gray"]
)
"""A device with a glass and a single-sided feeder."""

_FEEDERS_ONLY = DeviceCapabilities(
    sources=["Auto", "ADF Duplex", "ADF"],
    resolutions=[150, 300],
    modes=["Color", "Gray"],
)
"""A sheet-fed device: an automatic source and two feeders, and no glass."""


@pytest.fixture
def serve_profiles(
    tmp_path: Path, egress_allowlist: list[str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[dict[str, ProfileConfig]], _BrowserServer]]:
    """
    Return an opener that serves a private app with the given profiles.

    Built like ``defaults_server``: the server is added to the egress
    allowlist and paperless-ngx is patched to answer the defaults tag and
    correspondent lists, so a profile's ticks and choice can render.  Nothing
    here scans -- every submit is captured -- so no delivery is patched in.
    The server stops when the test ends.
    """
    with ExitStack() as stack:

        def _open(profiles: dict[str, ProfileConfig]) -> _BrowserServer:
            """Serve ``profiles`` and return the running server."""
            base = _browser_test_settings(tmp_path)
            settings = base.model_copy(update={"profiles": profiles})
            server = stack.enter_context(_serve(settings, _BrowserTestScanner()))
            egress_allowlist.append(server.url)
            paperless = server.app.state.paperless
            monkeypatch.setattr(paperless, "get_tags", _answer_list(_DEFAULTS_TAGS))
            monkeypatch.setattr(
                paperless,
                "get_correspondents",
                _answer_list(_DEFAULTS_CORRESPONDENTS),
            )
            server.app.state.cache.invalidate("tags")
            server.app.state.cache.invalidate("correspondents")
            return server

        yield _open


def _option_texts(page: Page) -> list[str]:
    """
    Return the Profile select's option texts, in page order.

    Args:
        page: The browser page.

    Returns:
        What each option reads.

    """
    return page.locator("#profile-select option").all_text_contents()


@pytest.mark.browser
class TestProfileDefaultsPreselection:
    """The page opens on what ``saneless scan`` uses, each option offered once."""

    def test_the_page_opens_on_default_listed_last(
        self,
        page: Page,
        serve_profiles: Callable[[dict[str, ProfileConfig]], _BrowserServer],
    ) -> None:
        """
        ``default`` is selected, described, ticked and submitted from last place.

        The two profiles ahead of it carry defaults and a description of their
        own, so a page that opened on the first option would show and send
        tag 32, no correspondent and the glass sentence instead.
        """
        server = serve_profiles(
            {
                "flatbed": ProfileConfig(
                    source="Flatbed",
                    description=_FLATBED_DESCRIPTION,
                    default_tags=[32],
                ),
                "adf": ProfileConfig(source="ADF", description=_FEEDER_DESCRIPTION),
                "default": ProfileConfig(
                    source="Flatbed",
                    description=_OPENING_DESCRIPTION,
                    default_tags=[31],
                    default_correspondent=41,
                ),
            }
        )
        page.goto(server.url)

        assert _option_texts(page) == ["flatbed", "adf", "default"]
        expect(page.locator("#profile-select")).to_have_value("default")
        expect(page.locator("#profile-description")).to_have_text(_OPENING_DESCRIPTION)
        expect(page.locator('#tags-list input[value="31"]')).to_be_checked()
        assert _ticked_tags(page) == ["31"]
        expect(page.locator("#correspondent-select")).to_have_value("41")

        fields = _captured_submit(page)

        assert fields["profile"] == ["default"], fields
        assert fields["tags"] == ["31"], fields
        assert fields["correspondent"] == ["41"], fields

    def test_a_generated_twin_default_is_not_offered(
        self,
        page: Page,
        serve_profiles: Callable[[dict[str, ProfileConfig]], _BrowserServer],
    ) -> None:
        """
        A generated ``default`` equal to the flatbed is hidden; the flatbed opens.

        The set is the generator's own output for a flatbed-and-feeder device,
        so its ``default`` is a whole-model copy of ``flatbed``, label and all.
        Offered, it would be a second "Glass (flatbed)" in the list.
        """
        generated = generate_profiles(_FLATBED_AND_FEEDER)
        assert list(generated) == ["flatbed", "adf", "default"]
        assert generated["default"] == generated["flatbed"]
        server = serve_profiles(generated)
        page.goto(server.url)

        expect(page.locator('#profile-select option[value="default"]')).to_have_count(0)
        expect(page.locator("#profile-select option")).to_have_text(
            [generated["flatbed"].label, generated["adf"].label]
        )
        expect(page.locator("#profile-select")).to_have_value("flatbed")
        expect(page.locator("#profile-description")).to_have_text(
            generated["flatbed"].description
        )

        fields = _captured_submit(page)

        assert fields["profile"] == ["flatbed"], fields
        # The stand-in's defaults are the generator's: no tag and no
        # correspondent, which is what the twin it stands in for carries too.
        assert "tags" not in fields, fields
        assert fields.get("correspondent", [""]) == [""], fields

    def test_duplicate_labels_read_differently(
        self,
        page: Page,
        serve_profiles: Callable[[dict[str, ProfileConfig]], _BrowserServer],
    ) -> None:
        """Two hand-written profiles labelled alike read "<label> (<name>)"."""
        server = serve_profiles(
            {
                "a": ProfileConfig(source="Flatbed", label="Scan"),
                "b": ProfileConfig(source="ADF", label="Scan"),
                "default": ProfileConfig(),
            }
        )
        page.goto(server.url)

        expect(page.locator("#profile-select option")).to_have_text(
            ["Scan (a)", "Scan (b)", "default"]
        )
        texts = _option_texts(page)
        assert len(set(texts)) == len(texts), texts

    def test_sheet_fed_opens_on_the_stand_in_not_the_first_option(
        self,
        page: Page,
        serve_profiles: Callable[[dict[str, ProfileConfig]], _BrowserServer],
    ) -> None:
        """
        With no glass the feeders still lead, and the page opens on ``adf``.

        ``auto`` is listed first in the configuration and still renders last,
        so the feeder-first order is observed and not assumed; ``duplex`` then
        leads the list, so opening on the first option would pick it and not
        the profile the hidden ``default`` copies.
        """
        generated = generate_profiles(_FEEDERS_ONLY)
        profiles = {
            "auto": generated["auto"],
            "duplex": generated["adf-duplex"],
            "adf": generated["adf"],
            "default": generated["adf"].model_copy(deep=True),
        }
        server = serve_profiles(profiles)
        page.goto(server.url)

        expect(page.locator("#profile-select option")).to_have_count(3)
        option_values = page.locator("#profile-select option").evaluate_all(
            "options => options.map(option => option.value)"
        )
        assert option_values == ["duplex", "adf", "auto"]
        expect(page.locator("#profile-select")).to_have_value("adf")

        fields = _captured_submit(page)

        assert fields["profile"] == ["adf"], fields


# ---------------------------------------------------------------------------
# The status strip, in Chromium.
#
# Every browser-visible claim about the strip is automated, here and in the
# classes below, and none is left for a person to look at. The one claim
# outside the suite is scanner reachability against physical hardware, which
# cannot be stubbed.
# ---------------------------------------------------------------------------

_PAUSED_PREFIX = "Paused during scan \N{EM DASH} "
"""The freshness line's prefix while a scan holds the scanner."""

_SCANNER_PAUSED_MESSAGE = "Not checked while a scan is running."
"""The Scanner row's message for the same situation."""


# How many line boxes each strip row's message text occupies. The message is
# the holder span's own text node; .check-next is a child element set to
# display: block, so counting the holder's rects wholesale would report two
# lines for every row that carries a next step and nothing about wrapping.
_COUNT_CHECK_MESSAGE_LINES = """
() => Array.from(document.querySelectorAll("#checks-body .check-row"), (row) => {
    const holder = row.lastElementChild;
    const text = Array.from(holder.childNodes).find(
        (node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim() !== ""
    );
    if (!text) return 0;
    const range = document.createRange();
    range.selectNodeContents(text);
    const tops = new Set(
        Array.from(range.getClientRects(), (rect) => Math.round(rect.top))
    );
    return tops.size;
})
"""


# Every name column whose text does not fit the column the CSS declares for
# it, as ``[text, text width, declared width, used width]``.
#
# The comparison is against ``flex-basis`` rather than against the element's
# own box, and that is what makes this falsifiable.  ``.check-name`` is a flex
# item with ``flex-shrink: 0``, so a name wider than the basis does not clip --
# CSS's automatic minimum size grows the *item* instead, silently, and
# ``scrollWidth <= clientWidth`` stays true while the column it was supposed to
# be has stopped existing for that one row.  What the reader loses is the
# alignment: one row's message starts further right than the other five.
_NAME_COLUMNS_TOO_NARROW = """
() => Array.from(document.querySelectorAll("#checks-body .check-name"), (el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    const style = getComputedStyle(el);
    return [
        el.textContent.trim(),
        range.getBoundingClientRect().width,
        parseFloat(style.flexBasis),
        el.getBoundingClientRect().width,
    ];
}).filter(([, text, basis]) => text > basis)
"""


# Every row's name, the top of its name, the top of its message and the left
# edge of its message, rounded to whole pixels.  A message that sits in its
# column shares its name's top and every other message's left edge.
_CHECK_MESSAGE_POSITIONS = """
() => Array.from(document.querySelectorAll("#checks-body .check-row"), (row) => {
    const name = row.querySelector(".check-name").getBoundingClientRect();
    const message = row.querySelector(".check-message").getBoundingClientRect();
    return [
        row.querySelector(".check-name").textContent.trim(),
        Math.round(name.top),
        Math.round(message.top),
        Math.round(message.left),
    ];
})
"""


def _check_row(page: Page, name: str) -> Locator:
    """
    Return the strip row whose name column reads exactly ``name``.

    Locating by the rendered name rather than by index is deliberate: the row
    order is ``CheckKey`` member order, which is a contract the enum states, but
    a test that read row 0 would silently start asserting about a different
    check the day a sixth member is inserted above it.

    Args:
        page: The browser page.
        name: The name column's exact text, e.g. ``"Scanner"``.

    Returns:
        A locator for the one matching ``.check-row``.

    """
    return page.locator(f'.check-row:has(.check-name:text-is("{name}"))')


# Seconds ``_probe_now`` waits for the probe it asked for to finish.  The test
# scanner answers at once, so this bounds only what a broken probe would hang.
_REQUESTED_PROBE_BUDGET = 30.0


def _no_tick() -> None:
    """Stand in for the refresher's background tick, and probe nothing."""


def _pause_background_ticks(
    server: _BrowserServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Keep the refresher thread running, but stop it probing on its own.

    Left ticking, the refresher fills the cache on its first tick after a page
    says someone is looking, so the cold window would be "however long a tick
    takes" and a cold-start assertion would be racing a stopwatch. The thread
    itself has to stay up: Check again hands its probe to that thread and runs
    none of its own, so with the thread stopped no click would ever be
    answered. Only the tick is replaced, and a requested probe never goes
    through it.

    Args:
        server: The private server whose refresher is paused.
        monkeypatch: Restores the real tick at teardown.

    """
    monkeypatch.setattr(server.app.state.refresher, "_tick", _no_tick)


@pytest.fixture
def cold_strip_server(
    tmp_path: Path, egress_allowlist: list[str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[_BrowserServer]:
    """
    Serve a private app whose check cache is cold and stays cold until asked.

    The background refresher's ticks are paused rather than raced, so the
    strip stays cold until the test itself asks for a probe through
    ``POST /api/checks/refresh``, which is the app's own probe-and-store path
    and not a reimplementation of it.
    """
    with _serve(_browser_test_settings(tmp_path), _BrowserTestScanner()) as server:
        _pause_background_ticks(server, monkeypatch)
        egress_allowlist.append(server.url)
        yield server


@pytest.fixture
def stale_config_strip_server(
    tmp_path: Path, egress_allowlist: list[str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[_BrowserServer]:
    """
    Serve a private app that found only a file under the superseded config name.

    A file sits in the third searched directory under the superseded name,
    nothing is loaded, and the appliance runs on defaults.  The recording is made by the real ``discover_config`` over
    three directories inside ``tmp_path``, so the search that the page reports
    is a search that really ran and the host's own ``/etc`` is never touched.

    The refresher's ticks are paused for the reason ``cold_strip_server``
    pauses them: the test asks for the probe itself, so nothing here is racing
    a tick.
    """
    candidates = tuple(
        tmp_path / name / CONFIG_FILENAME for name in ("cfg-cwd", "cfg-xdg", "cfg-etc")
    )
    for candidate in candidates:
        candidate.parent.mkdir(parents=True, exist_ok=True)
    candidates[2].with_name(LEGACY_CONFIG_FILENAME).write_text("", encoding="utf-8")
    settings = _browser_test_settings(tmp_path)
    settings._config_discovery = discover_config(candidates)
    with _serve(settings, _BrowserTestScanner()) as server:
        _pause_background_ticks(server, monkeypatch)
        egress_allowlist.append(server.url)
        yield server


@pytest.fixture
def refused_scanner_strip_server(
    tmp_path: Path, egress_allowlist: list[str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[_BrowserServer]:
    """
    Serve a private app whose scanner host refuses the connection.

    The host is a loopback port held bound but never listening, so the
    server's own pre-probe is refused at once by the kernel.  A refused host is still enumerated, the
    browser test scanner is listed beside it, and the Scanner row is the amber
    ready-but-refused row, one of the longest messages the strip carries.
    Nothing leaves the machine: the dial is the server's, to 127.0.0.1, and
    the browser never makes it.

    The refresher's ticks are paused for the reason ``cold_strip_server``
    pauses them.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port: int = probe.getsockname()[1]
        base = _browser_test_settings(tmp_path)
        scanner = base.scanner.model_copy(update={"host": f"127.0.0.1:{closed_port}"})
        settings = base.model_copy(update={"scanner": scanner})
        with _serve(settings, _BrowserTestScanner()) as server:
            _pause_background_ticks(server, monkeypatch)
            egress_allowlist.append(server.url)
            yield server


def _probe_now(server: _BrowserServer) -> None:
    """
    Make the appliance probe and store its checks, from outside the browser.

    ``POST /api/checks/refresh`` is the "Check again" route: it hands a probe
    to the refresher thread, which sets ``skip_scanner`` from the worker's job
    record, runs the probes and stores them, and it waits a bounded time for
    the answer. This waits on until the probe has finished, so a caller that
    opens the page next finds the results already stored. Calling it over HTTP
    rather than clicking the button leaves the *page* untouched, so what
    discovers the new results is the strip's own poll -- which is the half of
    the cold-start contract under test. Neither ``Origin`` nor
    ``Sec-Fetch-Site`` is sent, which is the non-browser branch
    ``CrossOriginGuard`` allows by design.

    Args:
        server: The private server to probe.

    """
    response = httpx2.post(f"{server.url}/api/checks/refresh", timeout=30.0)
    assert response.status_code == 200, response.status_code
    refresher = server.app.state.refresher
    assert poll_until(lambda: not refresher.probe_in_flight, _REQUESTED_PROBE_BUDGET), (
        "the requested probe never finished"
    )


@pytest.mark.browser
class TestStatusStripInChromium:
    """
    The health strip, read from a real page.

    Four claims live only here. Which card a household member's eye lands on
    first is a document-order fact about the rendered page; the poll starting
    and then stopping is htmx acting on an attribute that a template assertion
    can see but not follow; the three verdict colours are cascade outcomes; and
    the paused rendering is what two pieces of server state look like once they
    have been through Jinja and Pico.
    """

    def test_the_strip_is_the_first_card_and_leaves_phase_26s_slot_alone(
        self, page: Page, browser_server_url: str
    ) -> None:
        """
        ``#checks-card`` is the first card, and the alert slot keeps its place.

        The strip is read at a glance, and at a glance means first, so the
        ordinal is asserted rather than merely the presence. ``#status-message``
        stays the immediate element sibling above the status block --
        ``#status-live``, the persistent region whose one child is
        ``#status-area`` -- with the strip's card above the form.
        """
        page.goto(browser_server_url)

        expect(page.locator("main > article").nth(0)).to_have_id("checks-card")
        strip_precedes_scan_card = page.evaluate(
            "() => Boolean("
            "document.getElementById('checks-card').compareDocumentPosition("
            "document.querySelector('main > article:not(#checks-card)')) "
            "& Node.DOCUMENT_POSITION_FOLLOWING)"
        )
        assert strip_precedes_scan_card is True

        # One swap target, never two: the partial renders its own wrapper and is
        # also emitted out of band on a scan submit, so a duplicated id is the
        # specific way that arrangement could go wrong.
        assert page.evaluate("document.querySelectorAll('#checks-body').length") == 1
        assert (
            page.evaluate(
                "() => document.getElementById('status-message').nextElementSibling.id"
            )
            == "status-live"
        )
        live = page.locator("#status-message + #status-live")
        expect(live).to_have_attribute("role", "status")
        expect(live.locator("> *")).to_have_count(1)
        expect(live.locator("> #status-area")).to_have_count(1)

    def test_a_cold_strip_polls_itself_until_results_land_and_then_stops(
        self, page: Page, cold_strip_server: _BrowserServer
    ) -> None:
        """
        The cold body carries the 2 s poll; the body that replaces it does not.

        This is the in-tree "the response ends its own poll" idiom, and it is
        the one thing about it a template assertion cannot say: that htmx really
        re-requests on the trigger, and that the trigger-less body arriving
        really is the last swap. The results are stored from outside the
        browser, so the swap observed here is the page's own poll finding them.
        """
        page.goto(cold_strip_server.url)

        body = page.locator("#checks-body")
        expect(body).to_have_attribute("hx-trigger", re.compile(r"every 2s"))
        rows = page.locator("#checks-body .check-row")
        expect(rows).to_have_count(len(CheckKey))
        expect(body).to_contain_text(CHECKING_MESSAGE)

        _probe_now(cold_strip_server)

        # ":not([hx-trigger])" rather than a negated attribute assertion: it is
        # the attribute's *absence* that ends the poll, and a locator that
        # matches only the trigger-less body auto-waits for exactly that.
        expect(page.locator("#checks-body:not([hx-trigger])")).to_have_count(
            1, timeout=10_000
        )
        expect(rows).to_have_count(len(CheckKey))
        expect(page.locator("#checks-body")).not_to_contain_text(CHECKING_MESSAGE)

    @pytest.mark.parametrize("cls", ["check-ok", "check-warn", "check-fail"])
    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_every_check_state_colour_clears_aa_on_the_card(
        self,
        page: Page,
        browser_server_url: str,
        scheme: Literal["light", "dark"],
        cls: Literal["check-ok", "check-warn", "check-fail"],
    ) -> None:
        """
        The three verdict colours are legible where the strip really is.

        The card is the binding surface: the strip lives inside an
        ``<article>``, and the dark card is lighter than the dark page, so it is
        the tighter of the two. Each colour is also checked by value, so a
        palette drift is reported as the wrong colour rather than only as a
        ratio that still happens to clear 4.5.
        """
        page.emulate_media(color_scheme=scheme)
        page.goto(browser_server_url)

        probe = page.evaluate(_PROBE_CONTEXT_CONTRAST, {"cls": cls, "context": "card"})
        colour = probe["colour"]
        background = _flatten(probe["backgroundStack"])
        ratio = _contrast_ratio(colour, background)
        assert colour == _CHECK_STATE_COLOURS[cls][scheme], (colour, background)
        assert ratio >= 4.5, (colour, background, ratio)

    def test_a_scan_in_flight_pauses_the_scanner_row_and_says_so(
        self, page: Page, cold_strip_server: _BrowserServer
    ) -> None:
        """
        During a scan the Scanner row is skipped, marked neutral, and says why.

        The inputs are what a live scan produces -- a current job on the
        worker, and the scanner gate held -- set directly rather than by
        running one, and they render as a paused strip rather than a stale or
        blank one. The skipped row's state is ``CheckState.OK``, so this is
        the one place a real skipped row proves it shows the neutral glyph and
        the skipped label, not a green tick announced as "OK: Scanner".
        """
        server = cold_strip_server
        job_store: JobStore = server.app.state.job_store
        worker = server.app.state.worker
        job = job_store.create_job(profile="default", title="Scanning Doc")
        job_store.update_state(job.id, JobState.SCANNING)
        worker._current_job_id = job.id
        try:
            page.goto(server.url)
            with worker.scanner_gate:
                _probe_now(server)

                scanner_row = _check_row(page, "Scanner")
                expect(scanner_row).to_contain_text(
                    _SCANNER_PAUSED_MESSAGE, timeout=10_000
                )

                glyph = scanner_row.locator(".check-glyph")
                classes = glyph.get_attribute("class") or ""
                assert CHECKING_STATE_CLASS in classes.split(), classes
                assert check_state_class(CheckState.OK) not in classes.split(), classes
                assert glyph.inner_text() == CHECKING_GLYPH
                assert check_state_glyph(CheckState.OK) not in scanner_row.inner_html()
                spoken = scanner_row.locator(".sr-only").inner_text()
                assert spoken == f"{SKIPPED_STATE_LABEL}:", spoken
                assert spoken != f"{check_state_label(CheckState.OK)}:"

                meta = page.locator(".check-meta")
                expect(meta).to_contain_text(_PAUSED_PREFIX)
                assert meta.inner_text().startswith(_PAUSED_PREFIX), meta.inner_text()
        finally:
            worker._current_job_id = None
            job_store.delete_job(job.id)

    def test_check_again_replaces_the_body_it_is_aimed_at(
        self, page: Page, cold_strip_server: _BrowserServer
    ) -> None:
        """
        A real click on Check again swaps ``#checks-body`` ``outerHTML``.

        The button replaces the element it targets, trigger attribute and all,
        and the witness set from the test tells replacement from update. The
        checks are probed before the page opens, so the body arrives with no
        poll trigger and the click is the only thing that can swap it.
        """
        server = cold_strip_server
        _probe_now(server)
        page.goto(server.url)

        body = page.locator("#checks-body")
        expect(body).to_have_count(1)
        # No trigger on arrival: results are already stored, so the strip is in
        # its settled state and the button is the only remaining swap source.
        expect(page.locator("#checks-body:not([hx-trigger])")).to_have_count(1)
        body.evaluate("(el) => el.setAttribute('data-witness', 'before-refresh')")

        with page.expect_response(lambda r: r.url.endswith("/api/checks/refresh")):
            page.click(".check-refresh")

        # The witness is gone because the element carrying it is gone. An
        # innerHTML swap would have left the attribute sitting on the surviving
        # wrapper and every other assertion here would still have passed.
        expect(page.locator("#checks-body:not([data-witness])")).to_have_count(1)
        expect(page.locator("#checks-body")).to_have_count(1)
        expect(page.locator("#checks-body .check-row")).to_have_count(len(CheckKey))
        expect(page.locator("#checks-body")).not_to_contain_text(CHECKING_MESSAGE)
        # And the replacement carries its own button, so the strip can be
        # refreshed twice; a swap that dropped it would look fine once.
        expect(page.locator(".check-refresh")).to_have_count(1)

    def test_htmx_request_timeout_is_configured(
        self, page: Page, browser_server_url: str
    ) -> None:
        """
        The loaded htmx really reads the 20 s timeout from the meta.

        The template test proves the JSON says so; only the running library
        proves the meta was merged, and that a stalled request will be let go
        instead of holding its element for as long as the socket stays open.
        """
        page.goto(browser_server_url)
        expect(page.locator("#checks-body")).to_have_count(1)

        assert page.evaluate("() => htmx.config.timeout") == 20000

    def test_focus_map_check_again_keeps_focus(
        self, page: Page, cold_strip_server: _BrowserServer
    ) -> None:
        """
        A keyboard press on Check again leaves focus on the new Check again.

        The button sits inside the body it replaces, so the element that had
        focus is detached by its own swap.  htmx puts focus back on the element
        with the same id, which is the only thing standing between a keyboard
        user and a focus that has fallen to the top of the page.  The witness
        proves the body itself is swapped out, so the focus assertion is about
        the new button and not the old one surviving an in-place update.
        """
        server = cold_strip_server
        _probe_now(server)
        page.goto(server.url)
        expect(page.locator("#checks-body:not([hx-trigger])")).to_have_count(1)
        page.locator("#checks-body").evaluate(
            "(el) => el.setAttribute('data-witness', 'before-refresh')"
        )

        page.focus("#checks-refresh")
        with page.expect_response(lambda r: r.url.endswith("/api/checks/refresh")):
            page.keyboard.press("Enter")

        expect(page.locator("#checks-body:not([data-witness])")).to_have_count(1)
        expect(page.locator("#checks-refresh")).to_be_focused()

    def test_the_strip_fits_a_320px_phone_without_a_sideways_scrollbar(
        self, page: Page, cold_strip_server: _BrowserServer
    ) -> None:
        """
        At 320 px the rows wrap instead of widening the page.

        Each row is a wrapping flex row with a fixed 1 rem glyph gutter and a
        7 rem name column; those two columns plus a message must not push the
        document wider than the viewport, and the message must wrap rather
        than overflow. A household member who has to scroll sideways to read a
        health verdict has not been told anything at a glance.
        """
        server = cold_strip_server
        _probe_now(server)
        page.set_viewport_size({"width": 320, "height": 640})
        page.goto(server.url)
        expect(page.locator("#checks-body .check-row")).to_have_count(len(CheckKey))

        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth "
            "- document.documentElement.clientWidth"
        )
        assert overflow <= 0, f"the page scrolls sideways by {overflow}px at 320px"

        viewport = page.viewport_size
        assert viewport is not None
        rows = page.locator("#checks-body .check-row")
        name_columns: list[tuple[float, float]] = []
        for index in range(rows.count()):
            box = rows.nth(index).bounding_box()
            assert box is not None, index
            assert box["x"] >= 0, (index, box)
            assert box["x"] + box["width"] <= viewport["width"], (index, box)
            name_box = rows.nth(index).locator(".check-name").bounding_box()
            assert name_box is not None, index
            name_columns.append((name_box["x"], name_box["width"]))

        # The fixed gutter and name column still hold at 320 px, so the six
        # messages still start at one x. A layout that "fitted" by letting the
        # name column collapse per row would clear the overflow check above and
        # be unreadable.
        assert len(set(name_columns)) == 1, name_columns

        # And one x because the column is wide enough for the longest name,
        # not because nothing has outgrown it yet. This is what the width in
        # app.css is answerable to.
        too_narrow = page.evaluate(_NAME_COLUMNS_TOO_NARROW)
        assert too_narrow == [], too_narrow

        # And the fit is a wrap, not a squeeze: at least one message occupies
        # more than one line box. .check-next is excluded by the probe, because
        # it is display: block and would otherwise look like a wrap on every
        # row that carries one.
        lines = page.evaluate(_COUNT_CHECK_MESSAGE_LINES)
        assert max(lines) >= 2, lines

        # The refresh control keeps its touch target at the narrowest width,
        # where a full-width Pico button would have been the easy regression.
        refresh = page.locator(".check-refresh").bounding_box()
        assert refresh is not None
        assert refresh["height"] >= 44, refresh
        assert refresh["x"] + refresh["width"] <= viewport["width"], refresh

    def test_every_check_name_fits_its_column_at_the_desktop_width(
        self, page: Page, cold_strip_server: _BrowserServer
    ) -> None:
        """
        The name column is sized for the longest name, at the default viewport.

        The 320 px test asserts this too, but it asserts it about a layout
        that is already under pressure; a column too narrow for one name is a
        mis-sized constant, not a narrow-screen effect, and it should be
        caught where the page is otherwise comfortable.
        """
        server = cold_strip_server
        _probe_now(server)
        page.goto(server.url)
        expect(page.locator("#checks-body .check-row")).to_have_count(len(CheckKey))

        too_narrow = page.evaluate(_NAME_COLUMNS_TOO_NARROW)
        assert too_narrow == [], too_narrow

    def test_a_long_message_stays_in_its_column_at_the_desktop_width(
        self, page: Page, refused_scanner_strip_server: _BrowserServer
    ) -> None:
        """
        The longest Scanner message wraps beside its name, not under it.

        At 1280 px every message, the refused row's included, starts at one x
        and on its name's line, with nothing scrolling sideways.  A message
        that moved as a whole onto its own line would leave one row out of
        step with the other five.
        """
        server = refused_scanner_strip_server
        _probe_now(server)
        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto(server.url)
        expect(page.locator("#checks-body .check-row")).to_have_count(len(CheckKey))
        scanner = _check_row(page, check_name(CheckKey.SCANNER))
        expect(scanner).to_contain_text(
            "is ready, but the scanner service is not running on the scanner host."
        )
        expect(scanner.locator(".check-glyph")).to_have_class(
            re.compile(rf"\b{check_state_class(CheckState.WARN)}\b")
        )

        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth "
            "- document.documentElement.clientWidth"
        )
        assert overflow <= 0, f"the page scrolls sideways by {overflow}px"
        positions = page.evaluate(_CHECK_MESSAGE_POSITIONS)
        assert len({left for _name, _top, _message_top, left in positions}) == 1, (
            positions
        )
        for name, name_top, message_top, _left in positions:
            assert message_top == name_top, (name, positions)

    def test_a_leftover_old_name_config_is_the_first_row_and_is_red(
        self, page: Page, stale_config_strip_server: _BrowserServer, tmp_path: Path
    ) -> None:
        """
        A config file left under the superseded name is the first row, and red.

        The row names the cause: it carries the rename and the file's
        documented spelling -- and no host path, which is the one thing the
        exception to the no-path rule is not allowed to become.
        """
        server = stale_config_strip_server
        _probe_now(server)
        page.goto(server.url)

        row = _check_row(page, "Configuration")
        expect(row).to_have_count(1)
        expect(row.locator(".check-glyph")).to_have_class(
            f"check-glyph {check_state_class(CheckState.FAIL)}"
        )
        expect(row).to_contain_text(f"saneless now reads {CONFIG_FILENAME}")
        expect(row).to_contain_text(f"/etc/saneless/{LEGACY_CONFIG_FILENAME}")

        first = page.locator("#checks-body .check-row").nth(0)
        expect(first.locator(".check-name")).to_have_text("Configuration")
        assert str(tmp_path) not in page.locator("#checks-body").inner_text()


# How long the strip is watched after it has stopped, in milliseconds. Three of
# the 2 s intervals the markup names, so a poll that had merely paused would
# have fired at least once inside it and been caught.
_POLL_SETTLE_MS = 6000

# The longest the give-up is waited for. The chain is POLL_ATTEMPT_CAP requests
# two seconds apart, so it ends around 2 * (cap - 1) seconds; the bound is
# generous enough to survive a slow CI machine and still well inside pytest's
# own 60 s timeout.
_POLL_GIVE_UP_MS = 40_000

# The longest a cold start that really fills is given to reach its results. A
# private server's probe is a stub scanner plus a refused connection to a
# closed Paperless port, so it is a small multiple of one tick; the bound
# exists so a hung probe is reported as a timeout rather than as a test that
# waits out pytest's own 60 s limit.
_COLD_START_FILL_MS = 30_000


def _record_checks_requests(page: Page) -> list[str]:
    """
    Start recording every ``GET /api/checks`` the page issues.

    Registered as a page event rather than through ``context.route``: the
    module's egress gate is the one handler allowed to decide a request's fate,
    and a second route handler installed here would silently take priority over
    it. This only watches.

    ``/api/checks/refresh`` is excluded because it shares the path prefix and is
    never the poll -- it is a click, and counting it would let a test that
    presses the button look like a test that measured a poll.

    Args:
        page: The page to watch. Call this before navigating: a handler
            registered after ``goto`` misses the page's first requests.

    Returns:
        The list each polled URL is appended to, as the requests arrive.

    """
    polled: list[str] = []

    def _record(request: Request) -> None:
        url = request.url
        if "/api/checks" in url and "/refresh" not in url:
            polled.append(url)

    page.on("request", _record)
    return polled


@pytest.fixture
def storing_strip_server(
    tmp_path: Path, egress_allowlist: list[str]
) -> Iterator[_BrowserServer]:
    """
    Serve a private app whose refresher is alive and will fill the cache.

    The mirror image of ``cold_strip_server``: the refresher is left running,
    so the first tick after the page stamps a watcher probes and stores. That
    makes the strip genuinely cold when the page arrives -- the refresher does
    nothing until somebody is looking -- and genuinely warm a tick later,
    without the test touching the cache or the probe path itself.

    It cannot be the session server: that one's cache is warm within a second
    of the first page load, so a cold start cannot be observed against it.
    """
    with _serve(_browser_test_settings(tmp_path), _BrowserTestScanner()) as server:
        egress_allowlist.append(server.url)
        yield server


@pytest.mark.browser
class TestColdStartPollIsBounded:
    """
    The cold-start poll a tab nobody closes runs at its interval and then stops.

    The markup says ``every 2s``, but whether the browser obeys it is a
    request count, not an attribute: htmx fires a ``load`` trigger on content
    it has just swapped in, and this body swaps in a copy of itself, so a
    trigger list naming ``load`` would run at the round-trip rate. The server
    caps the attempts, and a cap counted in attempts is only a cap in time if
    the interval is honest.
    """

    def test_an_abandoned_cold_strip_stops_asking(
        self,
        page: Page,
        cold_strip_server: _BrowserServer,
        record_property: Callable[[str, object], None],
    ) -> None:
        """
        A cache that is never filled runs the poll out of attempts, then stops.

        ``cold_strip_server`` pauses the refresher's ticks, which to the page is
        an appliance whose background thread has died, so results never arrive
        to end the poll. The count equals the cap, and the stillness afterwards
        is asserted separately: a poll that had paused rather than stopped
        would fire at least three times inside the settle window. The give-up
        state keeps its rows, its button and its line, because "stops asking"
        would be a bad trade for a strip that had gone blank.
        """
        polled = _record_checks_requests(page)
        page.goto(cold_strip_server.url)
        expect(page.locator("#checks-body[hx-trigger]")).to_have_count(1)

        # ":not([hx-trigger])" rather than a negated attribute assertion: it is
        # the attribute's absence that ends the poll, and a locator matching
        # only the trigger-less body auto-waits for exactly that.
        expect(page.locator("#checks-body:not([hx-trigger])")).to_have_count(
            1, timeout=_POLL_GIVE_UP_MS
        )
        at_give_up = len(polled)
        record_property("cold_start_poll_requests", at_give_up)
        record_property("cold_start_poll_attempt_cap", POLL_ATTEMPT_CAP)
        assert at_give_up == POLL_ATTEMPT_CAP, polled

        browser_quiet_window(page, _POLL_SETTLE_MS)
        assert len(polled) == at_give_up, polled[at_give_up:]

        # Still a strip, and still a way forward.
        expect(page.locator("#checks-body .check-row")).to_have_count(len(CheckKey))
        expect(page.locator(".check-refresh")).to_have_count(1)
        expect(page.locator(".check-meta")).to_have_text(POLL_GAVE_UP_LINE)

    def test_a_cold_start_that_fills_reaches_its_results_unaided(
        self,
        page: Page,
        storing_strip_server: _BrowserServer,
        record_property: Callable[[str, object], None],
    ) -> None:
        """
        The legitimate cold start ends in results with nobody touching the page.

        This is the property the cap must leave alone, and it is the reason the
        cap is ten rather than two: the strip has to be allowed to keep asking
        for as long as a real appliance could plausibly take to answer. The
        request count is recorded, not asserted.
        """
        polled = _record_checks_requests(page)
        page.goto(storing_strip_server.url)

        expect(page.locator("#checks-body:not([hx-trigger])")).to_have_count(
            1, timeout=_COLD_START_FILL_MS
        )
        expect(page.locator("#checks-body")).not_to_contain_text(CHECKING_MESSAGE)
        expect(page.locator("#checks-body .check-row")).to_have_count(len(CheckKey))
        record_property("cold_start_requests_until_results", len(polled))


# How long the error-path poll is watched after the failure has been swapped
# in, in milliseconds. Four and a half of the 2 s intervals the markup names, so
# a poll that had survived its own failed request would fire four more times
# inside it and be caught.
_ERROR_POLL_WINDOW_MS = 9000

# base.html's htmx-config, captured whole. Single-quoted in the template
# because its value is JSON, so the group ends at the next apostrophe.
_HTMX_CONFIG_META = re.compile(
    r'<meta name="htmx-config"\s+content=\'(?P<json>[^\']+)\'', re.DOTALL
)

BASE_HTML = TEMPLATE_DIR / "base.html"
"""The layout whose htmx-config decides what an error response does to the strip."""

# The ``attempt`` the tampered poll carries. It is not an integer, so FastAPI's
# own request validation refuses it before the handler runs and the application
# builds the 422 through ``render_error`` -- which is the whole point: the
# browser has to receive the response this application really sends, headers
# included, not one written here.
_TAMPERED_ATTEMPT = "notanumber"

_ATTEMPT_PARAM = re.compile(r"attempt=[^&]*")


def _tampered_url(url: str) -> str:
    """
    Return ``url`` with its ``attempt`` value replaced by a non-integer.

    Args:
        url: The poll URL the page asked for.

    Returns:
        The same URL with an ``attempt`` the server must refuse.

    """
    if "attempt=" in url:
        return _ATTEMPT_PARAM.sub(f"attempt={_TAMPERED_ATTEMPT}", url)
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}attempt={_TAMPERED_ATTEMPT}"


def _tamper_the_checks_poll(page: Page) -> None:
    """
    Make every ``GET /api/checks`` reach the server with an attempt it refuses.

    The request is tampered with and passed on, never answered here. A handler
    that fulfilled the request itself would be measuring a response this
    application never sends, with hand-written headers or none at all.
    ``route.continue_`` keeps the application in the loop, so the browser
    receives the real 422 that ``render_error`` builds, with the real headers
    on it.

    A page-level handler takes priority over the module's context-level egress
    gate for the URLs it matches, which ``_record_checks_requests`` explains is
    normally forbidden here. It is admissible for exactly these requests and no
    others: they are same-origin requests to the private test server, which is
    what the gate would have continued anyway, so no egress an assertion would
    have caught can hide behind this handler. Everything else -- including
    ``/api/checks/refresh``, which shares the path prefix -- is handed back
    with ``route.fallback()`` and still meets the gate.

    Args:
        page: The page whose poll is to be tampered with.

    """

    def _handler(route: Route) -> None:
        request = route.request
        if request.method != "GET" or "/api/checks/refresh" in request.url:
            route.fallback()
            return
        route.continue_(url=_tampered_url(request.url))

    page.route("**/api/checks*", _handler)


def _record_checks_responses(page: Page) -> list[tuple[int, dict[str, str]]]:
    """
    Start recording the status and headers of every poll response.

    The headers decide where an error response lands, so they are read off the wire
    rather than inferred from what the page ended up looking like.

    Args:
        page: The page to watch. Call this before navigating.

    Returns:
        The list each ``(status, headers)`` pair is appended to, in order.
        Header names are lower-cased by Playwright.

    """
    seen: list[tuple[int, dict[str, str]]] = []

    def _record(response: Response) -> None:
        url = response.url
        if "/api/checks" in url and "/refresh" not in url:
            seen.append((response.status, dict(response.headers)))

    page.on("response", _record)
    return seen


@pytest.mark.browser
class TestPollEndsOnAnErrorResponse:
    """
    The strip's own failed poll replaces the strip, and so ends the poll.

    An error response to a request whose ``HX-Target`` is the strip carries no
    ``HX-Retarget`` and no ``HX-Reswap``, so the polling element's own
    ``hx-target="this" hx-swap="outerHTML"`` swaps it out and htmx stops
    re-arming a trigger on a detached element. With a retarget, htmx would
    write the error into ``#status-message`` and leave ``#checks-body``
    polling and failing for as long as the tab stayed open. Every case drives
    the failure through the server, so the headers are the application's own.
    """

    def test_a_failed_poll_request_replaces_the_strip_and_stops(
        self,
        page: Page,
        cold_strip_server: _BrowserServer,
        record_property: Callable[[str, object], None],
    ) -> None:
        """
        One request, then the application's own error body, then stillness.

        ``cold_strip_server`` is the appliance whose cache never fills, so the
        poll's own terminating conditions cannot fire inside the window and the
        only thing that can end the chain is the failure itself.

        The stillness is asserted separately from the count, the way
        ``TestColdStartPollIsBounded`` does it: a poll that had merely paused
        would have fired four more times inside ``_ERROR_POLL_WINDOW_MS``.
        """
        polled = _record_checks_requests(page)
        answered = _record_checks_responses(page)
        _tamper_the_checks_poll(page)
        page.goto(cold_strip_server.url)

        expect(page.locator("#checks-strip .status-error")).to_have_count(
            1, timeout=_POLL_SETTLE_MS
        )
        expect(page.locator("#checks-body")).to_have_count(0)
        at_swap = len(polled)

        browser_quiet_window(page, _ERROR_POLL_WINDOW_MS)
        record_property("error_poll_requests", len(polled))
        record_property("error_poll_window_ms", _ERROR_POLL_WINDOW_MS)
        assert len(polled) == at_swap, polled[at_swap:]
        assert len(polled) < POLL_ATTEMPT_CAP, polled
        assert answered, "the poll response was never observed"
        assert answered[0][0] == 422, answered

    def test_the_failing_poll_response_carries_no_retarget(
        self,
        page: Page,
        cold_strip_server: _BrowserServer,
        record_property: Callable[[str, object], None],
    ) -> None:
        """
        The failing poll response carries no retarget, read off the wire.

        Asserted on its own because the swap above is downstream of it: if
        ``HX-Retarget`` came back, the error would land in ``#status-message``
        and the strip would still be polling, and a reader would have to infer
        the header from the symptom.
        """
        answered = _record_checks_responses(page)
        _tamper_the_checks_poll(page)
        page.goto(cold_strip_server.url)

        expect(page.locator("#checks-strip .status-error")).to_have_count(
            1, timeout=_POLL_SETTLE_MS
        )
        assert answered, "the poll response was never observed"
        status, headers = answered[0]
        record_property("error_poll_status", status)
        assert status == 422, answered
        assert "hx-retarget" not in headers, headers
        assert "hx-reswap" not in headers, headers

    def test_the_failing_poll_never_writes_the_status_message_slot(
        self,
        page: Page,
        cold_strip_server: _BrowserServer,
    ) -> None:
        """
        A failing poll leaves ``#status-message`` exactly as it was.

        A scan's progress line lives there, and a poll failing every two
        seconds must not replace it with a generic error sentence.
        """
        _tamper_the_checks_poll(page)
        page.goto(cold_strip_server.url)

        expect(page.locator("#checks-strip .status-error")).to_have_count(
            1, timeout=_POLL_SETTLE_MS
        )
        before = page.locator("#status-message").inner_text()
        browser_quiet_window(page, _ERROR_POLL_WINDOW_MS)
        assert page.locator("#status-message").inner_text() == before
        expect(page.locator("#status-message .status-error")).to_have_count(0)

    def test_the_htmx_config_meta_swaps_error_responses(self) -> None:
        """
        The htmx-config meta swaps ``[45]..`` responses and flags them as errors.

        An error body is swapped at all only because of this rule. Under
        htmx's default -- ``swap`` false for ``[45]..`` -- the strip's error
        response would be discarded, the polling body would survive its own
        failed request, and the chain would run for as long as the tab stayed
        open.
        """
        match = _HTMX_CONFIG_META.search(BASE_HTML.read_text(encoding="utf-8"))
        assert match is not None
        handling = json.loads(match.group("json"))["responseHandling"]
        errors = [rule for rule in handling if rule["code"] == "[45].."]
        assert len(errors) == 1, handling
        assert errors[0]["swap"] is True
        assert errors[0]["error"] is True


# The counts footnote's wording, for a measured set and for a measured zero in
# the middle clause.
_MEASURED_COUNTS = "12 pages scanned, 2 blank removed, 10 uploaded"
_ZERO_BLANK_COUNTS = "12 pages scanned, 0 blank removed, 12 uploaded"

# The category the error rendering is read through. UPLOAD rather than UNKNOWN:
# UNKNOWN's next step ends "check the saneless log", and a test whose own
# fixture data contains the word the no-log-path assertion is hunting for would
# be arguing with itself.
_ERROR_CATEGORY = ErrorCategory.UPLOAD

_ERROR_DETAIL = "connect to paperless-ngx failed: [Errno 111] Connection refused"
"""The specific message the disclosure must still carry, relocated but not removed."""


@pytest.fixture
def empty_history_server(
    tmp_path: Path, egress_allowlist: list[str]
) -> Iterator[_BrowserServer]:
    """
    Serve a private app whose job history starts empty.

    Some assertions below are about the whole page: an ERROR page carries no
    log-file path anywhere, and a done job's counts sit in the first history
    row. Neither claim means anything against the session server, whose store
    and whose configured log path are shared with every other test in this
    module.
    """
    with _serve(_browser_test_settings(tmp_path), _BrowserTestScanner()) as server:
        egress_allowlist.append(server.url)
        yield server


@pytest.mark.browser
class TestErrorRenderingInChromium:
    """
    The plain-language failure, read from a rendered page.

    What a screen reader is handed is a property of the DOM and not of the
    template text: whether the next step falls inside the alert, and whether
    "Technical details" falls outside it, are both facts about the elements the
    browser built. So is whether the disclosure is really closed on arrival,
    and whether the page a whole LAN can read names a host filesystem path.
    """

    def test_one_alert_carries_the_sentence_and_the_next_step_and_no_log_path(
        self, page: Page, empty_history_server: _BrowserServer
    ) -> None:
        """
        One alert, the details outside it and shut, and no path anywhere.

        The count of one is the point of the wrapping ``<div role="alert">``:
        the sentence and the next step are announced together, once, and the
        disclosure is not announced with them. The closed state matters because
        an ``open`` disclosure would put the raw exception in front of a
        household member as though it were the message.
        """
        server = empty_history_server
        job_store: JobStore = server.app.state.job_store
        worker = server.app.state.worker
        job = job_store.create_job(
            profile="default",
            title="Broken Doc",
            owner_token=_as_owner(page, server.url),
        )
        job_store.finish_job(
            job.id,
            JobState.ERROR,
            error=_ERROR_DETAIL,
            error_category=_ERROR_CATEGORY,
        )
        worker._current_job_id = job.id
        try:
            page.goto(server.url)
            _show_the_live_outcome(page, '#status-area [role="alert"]')

            alert = page.locator('#status-area [role="alert"]')
            expect(alert).to_have_count(1)
            expect(alert).to_contain_text(error_message(_ERROR_CATEGORY))
            expect(alert).to_contain_text(error_next_step(_ERROR_CATEGORY))

            details = page.locator("#status-area details.tech-details")
            expect(details).to_have_count(1)
            assert details.get_attribute("open") is None
            # Outside the alert, not merely after it: a disclosure nested in the
            # live region would be read out with the failure.
            expect(page.locator('[role="alert"] details.tech-details')).to_have_count(0)

            details.locator("summary").click()
            expect(details).to_contain_text(_ERROR_DETAIL)
            expect(details).to_contain_text(f"Category: {_ERROR_CATEGORY.value}")
            expect(details).to_contain_text(f"Job: {job.id}")

            source = page.content()
            log_file = str(server.app.state.settings.output.log_file)
            assert log_file not in source, log_file
            # Not just this deployment's path: any filename that looks like a
            # log would be a host filesystem detail on a page the whole LAN can
            # read, so the substring is what is refused.
            assert ".log" not in source
        finally:
            worker._current_job_id = None
            job_store.delete_job(job.id)


@pytest.mark.browser
class TestAmberErrorRenderingInChromium:
    """
    A failure that may already be in paperless-ngx, proven amber in a browser.

    The template tests prove which class and role the markup carries; only a
    resolved cascade proves the headline reads as the fallback amber and not
    the failure red, and only the built DOM proves no live region announces it.
    An upload failure staged beside it is still a red alert, so the amber is
    the category's doing and not a change to every failure.
    """

    @staticmethod
    def _finish_error(
        job_store: JobStore, owner: str, title: str, category: ErrorCategory
    ) -> str:
        """
        Stage a finished ERROR row in `category`.

        Returns:
            The row's id.

        """
        job = job_store.create_job(profile="default", title=title, owner_token=owner)
        job_store.finish_job(
            job.id, JobState.ERROR, error=_ERROR_DETAIL, error_category=category
        )
        return job.id

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_amber_failure_is_amber_unannounced_and_labelled_by_category(
        self,
        page: Page,
        empty_history_server: _BrowserServer,
        scheme: Literal["light", "dark"],
    ) -> None:
        """
        The amber row reads amber with no alert; the upload failure stays red.

        Run under both colour schemes, because an amber that turned red in one
        of them would tell half the operators to scan a filed document again.
        """
        server = empty_history_server
        job_store: JobStore = server.app.state.job_store
        worker = server.app.state.worker
        owner = _as_owner(page, server.url)
        upload_id = self._finish_error(
            job_store, owner, "Refused Doc", ErrorCategory.UPLOAD
        )
        amber_id = self._finish_error(
            job_store, owner, "Unconfirmed Doc", ErrorCategory.UNCONFIRMED_FILING
        )
        page.emulate_media(color_scheme=scheme)
        try:
            worker._current_job_id = amber_id
            page.goto(server.url)
            _show_the_live_outcome(page, "#status-area p.status-fallback")

            expect(page.locator('#status-area [role="alert"]')).to_have_count(0)
            expect(page.locator("#status-area .status-error")).to_have_count(0)
            status = page.locator("#status-area")
            expect(status).to_contain_text(
                error_message(ErrorCategory.UNCONFIRMED_FILING)
            )
            expect(status).to_contain_text(
                error_next_step(ErrorCategory.UNCONFIRMED_FILING)
            )
            expect(page.locator("#status-area details.tech-details")).to_have_count(1)

            colours = page.evaluate(_PROBE_STATUS_COLOURS)
            assert colours["status-fallback"] == _AMBER[scheme], colours
            headline = page.evaluate(
                "() => getComputedStyle(document.querySelector('#status-area p')).color"
            )
            assert headline == colours["status-fallback"]
            assert headline != colours["status-error"]

            amber_cell = page.locator("#history-body td.status-fallback")
            expect(amber_cell).to_have_count(1)
            assert amber_cell.inner_text().strip() == UNCONFIRMED_FILING_LABEL
            red_cell = page.locator("#history-body td.status-error")
            expect(red_cell).to_have_count(1)
            assert red_cell.inner_text().strip() == job_label(JobState.ERROR, None)

            worker._current_job_id = upload_id
            page.goto(server.url)
            _show_the_live_outcome(page, '#status-area [role="alert"]')
            alert = page.locator('#status-area [role="alert"]')
            expect(alert).to_have_count(1)
            expect(alert).to_contain_text(error_message(ErrorCategory.UPLOAD))
            expect(page.locator("#status-area .status-fallback")).to_have_count(0)
        finally:
            worker._current_job_id = None
            job_store.delete_job(amber_id)
            job_store.delete_job(upload_id)


@pytest.mark.browser
class TestPageCountsInChromium:
    """
    The counts footnote, in both places it renders.

    One class and one rule serve the status area and the Title cell alike, and
    the guard for both is the filter returning None rather than a count's own
    truthiness. The failure mode that matters -- a measured zero rendered as
    nothing -- is a rendering outcome, so it is read off the page rather than
    off the filter.
    """

    @pytest.mark.parametrize(
        ("counts", "expected"),
        [
            ((12, 2, 10), _MEASURED_COUNTS),
            ((12, 0, 12), _ZERO_BLANK_COUNTS),
        ],
        ids=["measured", "measured-zero"],
    )
    def test_a_done_job_renders_its_counts_in_both_places(
        self,
        page: Page,
        empty_history_server: _BrowserServer,
        counts: tuple[int, int, int],
        expected: str,
    ) -> None:
        """
        Measured counts, a zero among them, render in both places.

        Each case is its own page load on a server with an empty history, so
        the first history row is the job under test.
        """
        server = empty_history_server
        job_store: JobStore = server.app.state.job_store
        worker = server.app.state.worker
        scanned, removed, uploaded = counts
        job = job_store.create_job(profile="default", title="Counted Doc")
        job_store.finish_job(
            job.id,
            JobState.DONE,
            result=JobResult(
                outcome=ScanOutcome.SUCCESS,
                warning=None,
                pages_scanned=scanned,
                pages_removed=removed,
                pages_uploaded=uploaded,
            ),
        )
        worker._current_job_id = job.id
        try:
            page.goto(server.url)
            _show_the_live_outcome(page, "#status-area .status-done")

            expect(page.locator("#status-area .page-counts")).to_have_text(expected)
            title_cell = page.locator("#history-body tr").first.locator("td").nth(2)
            expect(title_cell.locator(".page-counts")).to_have_text(expected)
        finally:
            worker._current_job_id = None
            job_store.delete_job(job.id)


# Every word in the history headers and in the Time, Profile and Status cells,
# each measured as a Range: a word that fits on one line has exactly one client
# rect, and a word split across lines has one per line.  A word is cut after
# each hyphen it holds, so "2026-09-30" is measured as "2026-", "09-" and "30":
# a line break straight after a printed hyphen is an ordinary one the browser
# may take, and the reader sees the hyphen that says the word goes on.  What
# is refused is a break with no hyphen to mark it, such as "Compl" and "ete".
# The Title column is left out on purpose, because a title is user text of any
# length and is the one cell allowed to break anywhere.  A function
# expression, because the Content-Security-Policy refuses the eval a bare
# expression would need.
_SPLIT_HISTORY_WORDS = """
() => {
  const table = document.querySelector(".history-table-wrap table");
  const cells = [
    ...table.querySelectorAll("thead th"),
    ...table.querySelectorAll(
      "#history-body td:nth-child(1), #history-body td:nth-child(2), "
        + "#history-body td:nth-child(4)"
    ),
  ];
  const split = [];
  let words = 0;
  for (const cell of cells) {
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      for (const match of node.data.matchAll(/[^\\s-]*-|[^\\s-]+/g)) {
        const range = document.createRange();
        range.setStart(node, match.index);
        range.setEnd(node, match.index + match[0].length);
        const lines = range.getClientRects().length;
        words += 1;
        if (lines !== 1) {
          split.push([match[0], lines]);
        }
      }
    }
  }
  return {cells: cells.length, words, split};
}
"""


@pytest.mark.browser
class TestHistoryTableOnAPhone:
    """
    The history table at 390 px keeps every word whole.

    Whether a word breaks is a line-layout outcome of the cascade, the table
    algorithm and the font together, so only a browser can say.  390 px is a
    common phone width, and the rows staged here carry the longest Status
    labels the table can show beside a default title.
    """

    def test_history_390_keeps_every_word_on_one_line(
        self, page: Page, empty_history_server: _BrowserServer
    ) -> None:
        """
        No header, time, profile or status word is split, and nothing scrolls.

        A fixed table layout would give every column a quarter of the width
        whatever it held, and break a long label mid-word.  The document
        width is checked as well, because keeping words whole by widening the
        page would trade one failure for another.

        A break straight after a printed hyphen is allowed, as
        ``_SPLIT_HISTORY_WORDS`` explains: at this width the date and
        "paperless-ngx" cannot both stay whole and leave the Title column
        room for more than a few letters, and a hyphen at the line end shows
        the reader the word goes on.
        """
        server = empty_history_server
        job_store: JobStore = server.app.state.job_store
        owner = _as_owner(page, server.url)
        title = resolve_job_title(None, None, now=datetime.now(tz=UTC))
        warning = "The scanner skipped a sheet."

        waiting = job_store.create_job(
            profile="default", title=title, owner_token=owner
        )
        job_store.update_state(waiting.id, JobState.AWAITING_RETRY)
        unconfirmed = job_store.create_job(
            profile="default", title=title, owner_token=owner
        )
        job_store.finish_job(
            unconfirmed.id,
            JobState.ERROR,
            error=_ERROR_DETAIL,
            error_category=ErrorCategory.UNCONFIRMED_SEND,
        )
        warned = job_store.create_job(profile="default", title=title, owner_token=owner)
        job_store.finish_job(
            warned.id,
            JobState.DONE,
            result=JobResult(
                outcome=ScanOutcome.SUCCESS,
                warning=warning,
                pages_scanned=None,
                pages_removed=None,
                pages_uploaded=None,
            ),
        )
        try:
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(server.url)

            statuses = page.locator("#history-body td:nth-child(4)")
            expect(statuses).to_have_count(3)
            for label in (
                job_label(JobState.DONE, warning),
                job_label(JobState.ERROR, None, ErrorCategory.UNCONFIRMED_SEND),
                job_label(JobState.AWAITING_RETRY, None),
            ):
                expect(statuses.filter(has_text=label)).to_have_count(1)

            measured = page.evaluate(_SPLIT_HISTORY_WORDS)
            # Four headers and three cells in each of three rows, so an empty
            # measurement cannot pass for a clean one.
            assert measured["cells"] == 13, measured
            assert measured["words"] > measured["cells"], measured
            assert measured["split"] == [], measured["split"]

            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth "
                "- document.documentElement.clientWidth"
            )
            assert overflow <= 0, f"the page scrolls sideways by {overflow}px at 390px"
        finally:
            for job in (waiting, unconfirmed, warned):
                job_store.delete_job(job.id)


_FRONT_PAGES = 12
"""How many sheets pass A produces, so the busy line has a known front count."""

_FRONT_COUNT_PREFIX = f"Front: {_FRONT_PAGES} pages \N{MIDDLE DOT} "
"""The busy line's opening, separator included."""


class _ManualDuplexScanner(_BrowserTestScanner):
    """
    A stub whose passes produce a known number of sheets, gated on pass B.

    ``_BrowserTestScanner`` spools one page per pass, which would pin the front
    count at 1 and leave the singular branch of the busy line as the only thing
    a browser could ever be shown. Twelve arrives by the real route: pass A
    returns twelve records, the pipeline counts them and hands the count to the
    worker through the pass-count callback, before it announces AWAITING_FLIP.

    Only pass B waits on the inherited gate, so the job parks in
    SCANNING_REVERSE for exactly as long as the assertions need.
    """

    def __init__(self, pages_per_pass: int) -> None:
        """Create the stub with its gate open and no passes run yet."""
        super().__init__()
        self._pages_per_pass = pages_per_pass
        self._passes = 0

    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Spool a pass, waiting on the gate for the second one only.

        Args:
            device_id: Ignored; this stub scans nothing real.
            settings: Only ``resolution`` is used, as the pages' dpi and the batch's.
            sink: The pipeline's own sink, which receives each page.

        Returns:
            A batch of this pass's records.

        """
        self._passes += 1
        if self._passes > 1:
            self.gate.wait(timeout=_SCAN_GATE_TIMEOUT)
        return _spool_pages(sink, self._pages_per_pass, settings.resolution)


@pytest.fixture
def duplex_server(
    tmp_path: Path, egress_allowlist: list[str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[_BrowserServer]:
    """
    Serve a private app that can run one real manual-duplex scan.

    ``monkeypatch`` is requested by this fixture rather than by the test so the
    ordering is guaranteed: a fixture is torn down before anything it depends
    on, so the stubbed Paperless client is still in place when ``_serve``'s
    shutdown finishes whatever job is left in flight. Requested by the test
    instead, it could be restored first and the shutdown would then spend its
    bounded join inside upload retries.
    """
    scanner = _ManualDuplexScanner(_FRONT_PAGES)
    with _serve(_browser_test_settings(tmp_path), scanner) as server:
        _make_paperless_deliver(server.app, monkeypatch)
        egress_allowlist.append(server.url)
        yield server


@pytest.mark.browser
class TestManualDuplexFrontCountInChromium:
    """
    The front count leads the busy line at SCANNING_REVERSE.

    The number exists only between the end of pass A and the end of the job,
    and it lives on the worker rather than in a job column, so the only way to
    see it rendered is to park a real scan in the reverse pass and look. That
    is what this does: a real submit, a real flip click, and the pipeline's own
    pass-count callback carrying the number.
    """

    def test_the_busy_line_leads_with_the_front_count(
        self,
        page: Page,
        duplex_server: _BrowserServer,
    ) -> None:
        """
        At ``SCANNING_REVERSE`` the busy line opens ``Front: 12 pages ·``.

        ``startswith`` rather than a containment check: the count leads the
        line, because it is the thing the operator standing at the feeder wants
        first, and a version that merely mentioned it somewhere would satisfy a
        containment assertion while failing the contract.
        """
        server = duplex_server
        job_store: JobStore = server.app.state.job_store
        # Closed before the submit, so pass B is already held by the time the
        # flip is answered and the job cannot run past the state under test.
        server.scanner.gate.clear()

        page.goto(server.url)
        page.select_option("#profile-select", "duplex")
        with page.expect_response(lambda r: r.url.endswith("/api/scan")):
            page.click("#scan-btn")
        recent = job_store.list_recent(1)
        assert recent, "the scan submit created no job row"
        job_id = recent[0].id

        try:
            wait_for_state(job_store, job_id, JobState.AWAITING_FLIP, timeout=20.0)
            continue_button = page.locator(
                "#status-area button[hx-post='/api/flip/continue']"
            )
            expect(continue_button).to_be_visible()
            continue_button.click()
            wait_for_state(job_store, job_id, JobState.SCANNING_REVERSE, timeout=20.0)

            busy = page.locator("#status-area p.busy-line")
            expect(busy).to_contain_text(_FRONT_COUNT_PREFIX.strip())
            assert busy.inner_text().startswith(_FRONT_COUNT_PREFIX), busy.inner_text()
        finally:
            # Released here and not left to the fixture: the job has to reach a
            # terminal state before the server shuts down, or the shutdown's
            # bounded worker join is what reports this test's failure.
            server.scanner.gate.set()
            wait_for_state(
                job_store, job_id, TERMINAL_STATES, timeout=_JOB_FINISH_TIMEOUT
            )


# The two profiles the swap needs beyond the shared pair, plus the one that has
# nothing to say. The fall-back to the profile's own name is only observable
# when some profile carries a human name and another does not, so both cases
# are configured.
_LABELLED_PROFILE = "labelled"
_LABELLED_NAME = "Everyday scan"
_UNNAMED_PROFILE = "unnamed"
_SILENT_PROFILE = "silent"
_UNNAMED_DESCRIPTION = "Scans whatever is on the glass, once."


def _described_profile_settings(tmp_dir: Path) -> Settings:
    """
    Build settings with four profiles, covering every shape the dropdown has.

    ``default`` is the flatbed and ``duplex`` the feeder, both inherited from
    the shared settings so the two sentences the swap moves between stay the
    ones the rest of the module uses. ``labelled`` carries a human name and
    ``unnamed`` carries none, which is the pair the fall-back to the profile's
    own name needs to be visible at all; ``silent`` carries no description, which is what the
    ``:empty`` rule exists for.

    Args:
        tmp_dir: The directory the private server's data and log live under.

    Returns:
        The settings, with the four profiles in dropdown order.

    """
    configured = _browser_test_settings(tmp_dir)
    return configured.model_copy(
        update={
            "profiles": {
                **configured.profiles,
                _LABELLED_PROFILE: ProfileConfig(
                    label=_LABELLED_NAME, description=_UNNAMED_DESCRIPTION
                ),
                _UNNAMED_PROFILE: ProfileConfig(description=_UNNAMED_DESCRIPTION),
                _SILENT_PROFILE: ProfileConfig(),
            }
        }
    )


@pytest.fixture
def described_profile_server(
    tmp_path: Path, egress_allowlist: list[str]
) -> Iterator[_BrowserServer]:
    """
    Serve a private app carrying all four dropdown shapes.

    The profile set is read from ``Settings`` once at startup, so a second
    server is the only way to put a browser in front of a profile with no
    human name or no description; the session server's pair cannot show either.
    """
    with _serve(_described_profile_settings(tmp_path), _BrowserTestScanner()) as server:
        egress_allowlist.append(server.url)
        yield server


@pytest.mark.browser
class TestProfileDescriptionInChromium:
    """
    The dropdown explains itself live, without losing the slot.

    ``TestProfileDescriptionSwap`` above proves the swap happens and that the
    id, the live region and the ``aria-describedby`` target survive it. What is
    added here is the element *identity* -- an ``outerHTML`` swap would satisfy
    every one of those assertions with a brand-new node -- and the two profile
    shapes the session server has no example of.
    """

    def test_the_slot_that_survives_the_swap_is_the_same_node(
        self, page: Page, described_profile_server: _BrowserServer
    ) -> None:
        """
        The text changes and the element does not.

        A witness attribute set from the test is what tells "this element was
        updated" apart from "an identical element was put in its place". Only
        the first keeps the node ``aria-describedby`` points at, its live region
        and its adjacency to the control, and only ``innerHTML`` gives it.
        """
        page.goto(described_profile_server.url)
        slot = page.locator("#profile-description")
        expect(slot).to_have_text(_FLATBED_DESCRIPTION)
        slot.evaluate("(el) => { el.dataset.witness = 'original'; }")

        page.select_option("#profile-select", "duplex")

        expect(slot).to_have_text(_FEEDER_DESCRIPTION)
        expect(slot).to_have_attribute("data-witness", "original")
        expect(page.locator("#profile-description")).to_have_count(1)
        expect(slot).to_have_attribute("aria-live", "polite")
        expect(page.locator("#profile-select")).to_have_attribute(
            "aria-describedby", "profile-description"
        )

    def test_a_profile_with_no_human_name_is_offered_under_its_own_name(
        self, page: Page, described_profile_server: _BrowserServer
    ) -> None:
        """
        ``label or name``, both halves, in the rendered options.

        A profile with no label is offered under its key rather than as a
        blank, unusable row. The labelled profile is asserted alongside so the
        fallback cannot pass by never being reached.
        """
        page.goto(described_profile_server.url)

        expect(
            page.locator(f'#profile-select option[value="{_UNNAMED_PROFILE}"]')
        ).to_have_text(_UNNAMED_PROFILE)
        expect(
            page.locator(f'#profile-select option[value="{_LABELLED_PROFILE}"]')
        ).to_have_text(_LABELLED_NAME)

    def test_a_profile_with_nothing_to_say_leaves_no_gap(
        self, page: Page, described_profile_server: _BrowserServer
    ) -> None:
        """
        An emptied slot is hidden outright, not left as a stray margin.

        The route answers a description-less profile with a byte-empty body so
        ``:empty`` still matches; a single whitespace text node would be a child
        and the rule would stop applying, leaving Pico's help-text margins
        behind under a control with no help text.
        """
        page.goto(described_profile_server.url)
        slot = page.locator("#profile-description")
        expect(slot).to_have_text(_FLATBED_DESCRIPTION)

        page.select_option("#profile-select", _SILENT_PROFILE)

        # Count first: ``to_be_hidden`` is satisfied by an element that is not
        # there at all, and a slot htmx had removed would pass it while breaking
        # everything the id is pointed at.
        expect(slot).to_have_count(1)
        expect(slot).to_be_hidden()
        expect(slot).to_have_text("")
        assert slot.evaluate("(el) => getComputedStyle(el).display") == "none"


_TIME_CELL_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} (\S+)$")
"""The history Time cell's shape, with the zone token captured for reuse."""


@pytest.mark.browser
class TestTimestampZonesInChromium:
    """
    Every timestamp on the page names its zone.

    The zone is named on every row rather than once as a column caption, so a
    line somebody copies into a message is self-describing. Both surfaces go
    through the same ``local_time`` filter, and this is the proof that they
    really agree once rendered -- the zone is read off the Time cell and the
    freshness line is required to end with that same token, so the test says
    nothing about which zone the host happens to be in.
    """

    def test_the_time_cell_and_the_freshness_line_name_the_same_zone(
        self, page: Page, cold_strip_server: _BrowserServer
    ) -> None:
        """
        The Time cell matches the pinned shape and the strip echoes its zone.

        The checks are probed before the page is opened, so the strip renders
        its "Last checked" line in the first response and there is no swap to
        wait on: what is under test here is the format, not the poll.
        """
        server = cold_strip_server
        job_store: JobStore = server.app.state.job_store
        job = job_store.create_job(profile="default", title="Timed Doc")
        _probe_now(server)
        try:
            page.goto(server.url)

            cell = page.locator("#history-body tr").first.locator("td").nth(0)
            rendered = cell.inner_text().strip()
            match = _TIME_CELL_PATTERN.match(rendered)
            assert match is not None, rendered
            zone = match.group(1)

            meta = page.locator(".check-meta")
            expect(meta).to_contain_text("Last checked")
            freshness = meta.inner_text().strip()
            # The token is taken from the cell rather than written down, so the
            # test is about the two surfaces agreeing and not about the runner's
            # own TZ.
            assert freshness.endswith(f"{zone}."), (freshness, zone)
        finally:
            job_store.delete_job(job.id)


# ---------------------------------------------------------------------------
# The owner gate, in two real browsers at once.
#
# Every other owner-gate assertion in this module is made from one browser: the
# non-owner case is staged by writing a foreign token onto a job row, which
# proves the server branches correctly but says nothing about the two cookie
# jars that decision exists to tell apart. The gate is about two people at one
# appliance, so it is checked here with two of them.
# ---------------------------------------------------------------------------

_FLIP_CONTROL_SELECTOR = "[hx-post^='/api/flip/']"
"""Every control that could answer a flip prompt, matched by where it posts."""

_WAITING_LINE = non_owner_wait_line(JobState.AWAITING_FLIP, deadline=None).removesuffix(
    "."
)
"""
What the non-owner's line at AWAITING_FLIP says with or without a deadline.

The line's no-deadline form, less its full stop: a real scan's page may render
before the worker records when the wait began, and either form carries this.
"""

_ABORT_CONFIRMATION = "Abort this scan? It will stop and cannot be resumed."
"""The question ``hx-confirm`` puts in the native dialog."""

# There is no third way out of the flip prompt: no override, no take-over, no
# force-continue. The absence is asserted on the words as well as on the
# controls, because a page offering one in prose would smuggle the same
# affordance past a control count.
_NO_THIRD_WAY_OUT = re.compile(r"override|take over|force", re.IGNORECASE)

_FLIP_JOB_TITLE = "Two Browsers One Stack"
"""The title only the owner's page may show, so its absence elsewhere means something."""


@pytest.fixture
def flip_server(
    tmp_path: Path, egress_allowlist: list[str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[_BrowserServer]:
    """
    Serve a private app that can park one real manual-duplex scan at the prompt.

    Private rather than the session server for two reasons: the owner token
    minted here must not follow later tests around, and the job row left behind
    must not appear on another test's supposedly idle page.

    ``monkeypatch`` is requested by this fixture rather than by the test, the
    ordering ``duplex_server`` established: a fixture is torn down before
    anything it depends on, so the stubbed Paperless client is still in place
    while ``_serve``'s shutdown finishes whatever job is left in flight.
    """
    with _serve(_browser_test_settings(tmp_path), _BrowserTestScanner()) as server:
        _make_paperless_deliver(server.app, monkeypatch)
        egress_allowlist.append(server.url)
        yield server


def _drive_to_flip_prompt(
    page: Page,
    server: _BrowserServer,
    title: str,
) -> str:
    """
    Submit a manual-duplex scan from ``page`` and park it at ``AWAITING_FLIP``.

    The submit is a real one, so the response's ``Set-Cookie`` is what makes
    this page's context the owner -- which is the point: a token written onto a
    row by a test proves nothing about a browser's cookie jar.

    Args:
        page: The browser page that submits, and so becomes the owner.
        server: The private server to submit to.
        title: The title to submit, so both browsers can be asked to agree.

    Returns:
        The id of the job now waiting at the flip prompt.

    """
    job_store: JobStore = server.app.state.job_store
    page.goto(server.url)
    page.select_option("#profile-select", "duplex")
    page.fill("#title-input", title)
    with page.expect_response(lambda r: r.url.endswith("/api/scan")):
        page.click("#scan-btn")
    recent = job_store.list_recent(1)
    assert recent, "the scan submit created no job row"
    job_id = recent[0].id
    wait_for_state(job_store, job_id, JobState.AWAITING_FLIP, timeout=20.0)
    return job_id


def _history_row_text(page: Page) -> tuple[str, str]:
    """Return the newest history row's Title and Status cells, as rendered."""
    cells = page.locator("#history-body tr").first.locator("td")
    return (
        (cells.nth(2).inner_text() or "").strip(),
        (cells.nth(3).inner_text() or "").strip(),
    )


def _assert_only_the_owner_is_named(owner: Page, viewer: Page, title: str) -> None:
    """
    Assert the newest history row reads the same to both but for its title.

    Args:
        owner: The page of the browser that started the scan.
        viewer: The page of any other browser.
        title: The scan's real title.

    """
    owner_title, owner_state = _history_row_text(owner)
    viewer_title, viewer_state = _history_row_text(viewer)
    assert owner_state == viewer_state
    assert owner_title.startswith(title)
    assert viewer_title.startswith(HIDDEN_JOB_TITLE)
    assert title not in viewer.content()


@pytest.mark.browser
class TestTwoBrowsersOneStack:
    """
    One appliance, two browsers, one owner.

    The owner is offered the flip, and is the only one shown the scan's title
    and preview; the second browser sees that a scan is waiting, and how it
    ends, under the generic title. The owner gate is a statement about two
    cookie jars: a browser keeps the ``Set-Cookie`` an htmx XHR returned,
    sends it back on the next poll, and a second browser has none. Writing a
    foreign token onto a job row cannot show that, so two contexts do.
    """

    def test_the_owner_is_offered_the_flip_and_the_second_browser_is_not(
        self,
        browser: Browser,
        flip_server: _BrowserServer,
        egress_allowlist: list[str],
    ) -> None:
        """
        Two cookie jars, one prompt, and nothing else differs.

        Both contexts are built by hand, so neither inherits the overridden
        ``context`` fixture's routing: each installs ``_make_gate`` explicitly
        and both share one ``blocked`` list, which is the arrangement that
        factory exists for. Without it these two pages would be the only ones
        in the module able to reach the real internet, and nothing in the
        suite would say so.
        """
        server = flip_server
        job_store: JobStore = server.app.state.job_store

        blocked: list[str] = []
        # One ``seen`` list for the same reason as one ``blocked`` list: both
        # pages are navigated below, so a single record speaks for both gates.
        seen: list[str] = []
        # And one policy-violation list, under the same one-assertion rule.
        violations: list[str] = []
        owner_ctx = browser.new_context()
        viewer_ctx = browser.new_context()
        try:
            owner_ctx.route("**/*", _make_gate(blocked, egress_allowlist, seen))
            viewer_ctx.route("**/*", _make_gate(blocked, egress_allowlist, seen))
            _make_csp_gate(owner_ctx, violations)
            _make_csp_gate(viewer_ctx, violations)
            owner_page = owner_ctx.new_page()
            viewer_page = viewer_ctx.new_page()

            job_id = _drive_to_flip_prompt(owner_page, server, _FLIP_JOB_TITLE)
            try:
                # Both pages are (re)loaded from the same server state, so the
                # only thing that differs between the two requests is the
                # cookie one of them carries. It also makes the owner's prompt
                # a decision taken on the presented token rather than a
                # leftover of the response that minted it, and it gives both
                # pages the Job History the empty pre-submit table did not have.
                owner_page.reload()
                viewer_page.goto(server.url)

                # The owner is offered both answers and nothing else.
                owner_status = owner_page.locator("#status-area")
                expect(
                    owner_status.locator("button[hx-post='/api/flip/continue']")
                ).to_have_text("Continue")
                expect(
                    owner_status.locator("button[hx-post='/api/flip/abort']")
                ).to_have_text("Abort scan")
                expect(owner_page.locator(_FLIP_CONTROL_SELECTOR)).to_have_count(2)

                # The second browser is told what is happening and given
                # nothing to press. Absence from the DOM, not a hidden control:
                # anything merely hidden is still reachable from the console.
                viewer_status = viewer_page.locator("#status-area")
                expect(viewer_status).to_contain_text(_WAITING_LINE)
                expect(viewer_page.locator(_FLIP_CONTROL_SELECTOR)).to_have_count(0)

                # The owner's jar holds the cookie the scan response minted,
                # and the second jar does not.
                owner_cookies = [
                    cookie
                    for cookie in owner_ctx.cookies()
                    if cookie["name"] == _OWNER_COOKIE_NAME
                ]
                assert len(owner_cookies) == 1, owner_ctx.cookies()
                cookie = owner_cookies[0]
                assert cookie["httpOnly"] is True
                assert cookie["sameSite"] == "Lax"
                # The year-long lifetime, read through the jar: ownership
                # outlives a browser restart rather than ending with it.
                assert _outlives_364_days(cookie["expires"]), cookie["expires"]
                assert [
                    c for c in viewer_ctx.cookies() if c["name"] == _OWNER_COOKIE_NAME
                ] == []

                # The controls are one difference and the title is the other.
                # The comparison is made where both pages report the same job,
                # the newest Job History row: its state reads the same to
                # both, and its title names the scan to the owner alone.
                _assert_only_the_owner_is_named(
                    owner_page, viewer_page, _FLIP_JOB_TITLE
                )

                # No third way out of the prompt, in words, on either page.
                for reader in (owner_page, viewer_page):
                    body = reader.locator("body").inner_text()
                    assert _NO_THIRD_WAY_OUT.search(body) is None, body
            finally:
                # Answered here rather than left to the flip timeout, which is
                # ten minutes by default: the job has to reach a terminal state
                # before _serve shuts the app down, or the shutdown's bounded
                # worker join is what would report this test's failure.
                server.app.state.worker.abort_flip(job_id)
                wait_for_state(
                    job_store, job_id, TERMINAL_STATES, timeout=_JOB_FINISH_TIMEOUT
                )
        finally:
            owner_ctx.close()
            viewer_ctx.close()
            # One list, two contexts: this single assertion speaks for both,
            # which is the contract _make_gate's note states. Order matters for
            # the same reason it does in the context fixture -- these two are
            # built by hand, so an assertion on ``blocked`` alone would give a
            # clean bill of health to contexts whose routing was never
            # installed, which is the hole the gate exists to close.
            assert seen, (
                "neither gate handled a request, so the no-egress assertion "
                "below would have passed for two contexts that were never gated"
            )
            assert blocked == [], f"a page tried to reach the network: {blocked}"
            assert violations == [], (
                f"a page violated its Content-Security-Policy: {violations}"
            )

    def test_flip_group_has_an_accessible_name(
        self,
        page: Page,
        flip_server: _BrowserServer,
    ) -> None:
        """
        The owner's buttons are named by the job line; the pictures are silent.

        Read from the browser's own accessibility tree, not from the markup:
        the group's name is computed through ``aria-labelledby``, and a hidden
        picture is one the tree leaves out, which only a browser decides.  The
        captions under the pictures stay in the tree, so nothing they said is
        lost.
        """
        server = flip_server
        job_id = _drive_to_flip_prompt(page, server, _FLIP_JOB_TITLE)
        try:
            page.reload()
            status = page.locator("#status-area")
            # The prompt alone: the owner's page also carries the first-page
            # preview, a real picture with its own name, below it.
            prompt = status.locator(".flip-prompt")

            expect(status.get_by_role("group")).to_have_accessible_name(
                flip_heading(_FLIP_JOB_TITLE)
            )
            expect(prompt.locator("svg")).to_have_count(2)
            expect(prompt.get_by_role("img")).to_have_count(0)
            snapshot = prompt.aria_snapshot()
            assert "img" not in snapshot, snapshot
            assert "Long edge (correct)" in snapshot, snapshot
            assert "Short edge (incorrect)" in snapshot, snapshot
        finally:
            _end_the_flip(server, job_id)

    def test_non_owner_sees_the_way_forward_and_deadline(
        self,
        page: Page,
        browser: Browser,
        flip_server: _BrowserServer,
        egress_allowlist: list[str],
    ) -> None:
        """
        A second browser is told where the flip can be answered, and until when.

        The deadline is the worker's own, rendered in local time, and the
        second browser is still given no button and no title.  Its context is
        built by hand, installs the egress gate and the policy recorder itself,
        and is checked after it closes.
        """
        server = flip_server
        worker = server.app.state.worker
        job_id = _drive_to_flip_prompt(page, server, _FLIP_JOB_TITLE)
        blocked: list[str] = []
        seen: list[str] = []
        violations: list[str] = []
        viewer_ctx = browser.new_context()
        try:
            assert poll_until(
                lambda: worker.flip_deadline(job_id) is not None, _JOB_FINISH_TIMEOUT
            ), "the worker never recorded when the flip wait began"
            deadline = worker.flip_deadline(job_id)
            assert deadline is not None
            viewer_ctx.route("**/*", _make_gate(blocked, egress_allowlist, seen))
            _make_csp_gate(viewer_ctx, violations)
            viewer = viewer_ctx.new_page()
            viewer.goto(server.url)

            viewer_status = viewer.locator("#status-area")
            expect(viewer_status).to_contain_text(local_time(deadline))
            expect(viewer_status).to_contain_text(
                "from the device that started this scan"
            )
            expect(viewer_status).to_contain_text(
                non_owner_wait_line(JobState.AWAITING_FLIP, deadline=deadline)
            )
            expect(viewer_status.get_by_role("button", name="Continue")).to_have_count(
                0
            )
            expect(viewer.locator(_FLIP_CONTROL_SELECTOR)).to_have_count(0)
            expect(viewer_status).not_to_contain_text(_FLIP_JOB_TITLE)
            # The owner, at the same moment, still has the prompt.
            expect(page.locator("#flip-continue")).to_be_visible()
        finally:
            viewer_ctx.close()
            _end_the_flip(server, job_id)
            assert seen, "the hand-built context's gate handled no request"
            assert blocked == [], f"a page tried to reach the network: {blocked}"
            assert violations == [], (
                f"a page violated its Content-Security-Policy: {violations}"
            )

    def test_abort_asks_first_and_does_nothing_when_the_answer_is_no(
        self,
        page: Page,
        flip_server: _BrowserServer,
    ) -> None:
        """
        The confirm is really raised, and dismissing it aborts nothing.

        ``hx-confirm`` is one attribute in a template, and a template test can
        only see that it is there. Whether a browser raises a dialog, whether
        the question it shows is the expected copy, and above all whether a "no"
        really stops the request are three facts about htmx and Chromium
        together. The "no" half is asserted from a recorded request list rather
        than from a timeout, so it says "no abort was sent" and not "none was
        sent in the first second".
        """
        server = flip_server
        job_store: JobStore = server.app.state.job_store

        asked: list[str] = []
        answer = ["dismiss"]
        posted: list[str] = []

        def _on_dialog(dialog: Dialog) -> None:
            asked.append(dialog.message)
            if answer[0] == "accept":
                dialog.accept()
            else:
                dialog.dismiss()

        def _on_request(request: Request) -> None:
            if request.method == "POST":
                posted.append(request.url)

        page.on("dialog", _on_dialog)
        page.on("request", _on_request)

        job_id = _drive_to_flip_prompt(page, server, "Abort Me")
        abort_button = page.locator("#status-area button[hx-post='/api/flip/abort']")
        expect(abort_button).to_be_visible()

        abort_button.click()
        assert asked == [_ABORT_CONFIRMATION], asked

        # A completed status poll after the dismissal, so the claim below is
        # "the browser had a whole round trip's worth of chance and sent
        # nothing" rather than a read taken in the same instant as the click.
        with page.expect_response(lambda r: _POLL_URL.search(r.url) is not None):
            pass
        assert [url for url in posted if url.endswith("/api/flip/abort")] == [], posted
        unanswered = job_store.get_job(job_id)
        assert unanswered is not None
        assert unanswered.state is JobState.AWAITING_FLIP
        expect(abort_button).to_be_visible()

        answer[0] = "accept"
        with page.expect_response(lambda r: r.url.endswith("/api/flip/abort")):
            abort_button.click()

        assert asked == [_ABORT_CONFIRMATION, _ABORT_CONFIRMATION], asked
        expect(page.locator("#status-area")).to_contain_text("Aborting scan")
        expect(page.locator("#status-area .status-cancelled")).to_be_visible(
            timeout=15_000
        )
        wait_for_state(
            job_store, job_id, JobState.CANCELLED, timeout=_JOB_FINISH_TIMEOUT
        )


_CONTINUE_URL = "/api/flip/continue"


@pytest.mark.browser
class TestFlipPromptSurvivesPolls:
    """
    The flip prompt stays put under the status poll, in Chromium.

    The area is polled every second while the job waits for the flip.  Nothing
    changes while nobody answers, so every poll is answered 204 and the
    Continue button the operator reached -- by keyboard or by pointer -- is
    never replaced under them.  Every wait is a completed poll or request,
    never a sleep, and every POST is counted from a request listener.
    """

    @staticmethod
    def _record_continues(page: Page) -> list[str]:
        """
        Record every Continue POST the page sends, from now on.

        Args:
            page: The page to listen on.

        Returns:
            The list the listener appends each Continue URL to.

        """
        posted: list[str] = []

        def _on_request(request: Request) -> None:
            if request.method == "POST" and request.url.endswith(_CONTINUE_URL):
                posted.append(request.url)

        page.on("request", _on_request)
        return posted

    @staticmethod
    def _settled_prompt(page: Page, server: _BrowserServer) -> str:
        """
        Park a real scan at the flip prompt and wait until the page shows it.

        Args:
            page: The page that submits, and so owns the job.
            server: The private flip server.

        Returns:
            The id of the job waiting at the flip prompt.

        """
        job_id = _drive_to_flip_prompt(page, server, "Held Flip")
        expect(page.locator("#flip-continue")).to_be_visible(timeout=10_000)
        # One completed poll after the prompt appeared, so the area on the page
        # is the one the unchanged polls will leave in place.
        _await_status_poll(page)
        return job_id

    @staticmethod
    def _finish(server: _BrowserServer, job_id: str) -> None:
        """
        Bring the job to an end before the server shuts down.

        An Abort after Continue already won is dropped, so this ends a job the
        test left at the prompt and leaves an answered one to finish.

        Args:
            server: The private flip server.
            job_id: The job to end.

        """
        job_store: JobStore = server.app.state.job_store
        server.app.state.worker.abort_flip(job_id)
        wait_for_state(job_store, job_id, TERMINAL_STATES, timeout=_JOB_FINISH_TIMEOUT)

    def test_flip_buttons_have_stable_ids(
        self,
        page: Page,
        flip_server: _BrowserServer,
    ) -> None:
        """Continue and Abort carry the ids focus is restored by."""
        job_id = self._settled_prompt(page, flip_server)
        try:
            expect(page.locator("#flip-continue")).to_have_text("Continue")
            expect(page.locator("#flip-abort")).to_have_text("Abort scan")
        finally:
            self._finish(flip_server, job_id)

    def test_focused_continue_survives_two_polls_and_enter_sends_the_post(
        self,
        page: Page,
        flip_server: _BrowserServer,
    ) -> None:
        """
        A keyboard-focused Continue keeps focus through two polls, and Enter works.

        The focused node is marked, so "still focused" means the very element
        the keyboard reached, not a replacement that happens to share its id.
        """
        posted = self._record_continues(page)
        job_id = self._settled_prompt(page, flip_server)
        try:
            page.focus("#flip-continue")
            page.locator("#flip-continue").evaluate(
                "button => button.setAttribute('data-focused', '')"
            )

            assert [_await_status_poll(page) for _ in range(2)] == [204, 204]
            expect(page.locator("#flip-continue[data-focused]")).to_be_focused()

            with page.expect_response(
                lambda r: r.url.endswith(_CONTINUE_URL)
            ) as answered:
                page.keyboard.press("Enter")

            assert answered.value.status == 200
            assert len(posted) == 1, posted
        finally:
            self._finish(flip_server, job_id)

    def test_space_held_across_a_poll_sends_one_continue(
        self,
        page: Page,
        flip_server: _BrowserServer,
    ) -> None:
        """
        A Space press that straddles a poll activates Continue exactly once.

        A button activates on Space's keyup, so the press is only delivered if
        the button that saw the keydown is still there when the key comes up.
        """
        posted = self._record_continues(page)
        job_id = self._settled_prompt(page, flip_server)
        try:
            page.focus("#flip-continue")
            page.keyboard.down("Space")
            assert _await_status_poll(page) == 204
            with page.expect_response(lambda r: r.url.endswith(_CONTINUE_URL)):
                page.keyboard.up("Space")

            assert len(posted) == 1, posted
        finally:
            self._finish(flip_server, job_id)

    def test_pointer_held_across_a_poll_sends_one_continue(
        self,
        page: Page,
        flip_server: _BrowserServer,
    ) -> None:
        """A mouse press held across a poll clicks Continue exactly once."""
        posted = self._record_continues(page)
        job_id = self._settled_prompt(page, flip_server)
        try:
            # hover() scrolls the button into view and puts the pointer on its
            # centre; the press itself is the raw mouse, so it can be held.
            page.locator("#flip-continue").hover()
            page.mouse.down()
            assert _await_status_poll(page) == 204
            with page.expect_response(lambda r: r.url.endswith(_CONTINUE_URL)):
                page.mouse.up()

            assert len(posted) == 1, posted
        finally:
            self._finish(flip_server, job_id)


_OWNED_TITLE = "Owned By One Browser"
"""The title of the finished scan only its owner's page may show."""


def _tiny_jpeg() -> str:
    """Return a real 8x8 JPEG, base64-encoded, so the preview is a decodable image."""
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="JPEG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.mark.browser
class TestOnlyTheOwnerSeesTheScan:
    """
    Two browsers looking at one finished scan, in Chromium.

    The browser that started the scan sees its title and preview; any other
    browser on the LAN sees that a scan finished, and how, under the generic
    title and with no preview at all -- not a hidden one.
    """

    def test_a_second_browser_sees_neither_the_title_nor_the_preview(
        self,
        page: Page,
        browser: Browser,
        browser_server: _BrowserServer,
        egress_allowlist: list[str],
    ) -> None:
        """
        One owned row, two cookie jars, two renderings.

        The second context is built by hand, so it installs ``_make_gate`` and
        the policy recorder itself and is checked after it closes, as the
        two-browser flip test does; ``page`` comes from the gated fixture.
        """
        app = browser_server.app
        job_store: JobStore = app.state.job_store
        job = job_store.create_job(
            profile="default",
            title=_OWNED_TITLE,
            owner_token=_as_owner(page, browser_server.url),
        )
        job_store.update_thumbnail(job.id, _tiny_jpeg())
        job_store.finish_job(
            job.id,
            JobState.DONE,
            result=JobResult(
                outcome=ScanOutcome.SUCCESS,
                warning=None,
                pages_scanned=1,
                pages_removed=0,
                pages_uploaded=1,
            ),
        )
        app.state.worker._current_job_id = job.id

        blocked: list[str] = []
        seen: list[str] = []
        violations: list[str] = []
        viewer_ctx = browser.new_context()
        try:
            viewer_ctx.route("**/*", _make_gate(blocked, egress_allowlist, seen))
            _make_csp_gate(viewer_ctx, violations)
            viewer_page = viewer_ctx.new_page()

            page.goto(browser_server.url)
            viewer_page.goto(browser_server.url)

            # A fresh page names the finished scan as the last one, under the
            # title the owner gate allows each browser.
            expect(page.locator("#status-area .last-scan")).to_contain_text(
                _OWNED_TITLE
            )
            expect(viewer_page.locator("#status-area .last-scan")).to_contain_text(
                HIDDEN_JOB_TITLE
            )
            # The outcome as the poll that watched it end shows it, preview
            # and all -- for the owner alone.
            _show_the_live_outcome(page, "#status-area .status-done")
            _show_the_live_outcome(viewer_page, "#status-area .status-done")

            expect(page.locator("#status-area")).to_contain_text(_OWNED_TITLE)
            expect(page.locator("#status-area img.thumbnail")).to_have_count(1)
            expect(page.locator("#history-body")).to_contain_text(_OWNED_TITLE)

            expect(viewer_page.locator("#status-area .status-done")).to_contain_text(
                HIDDEN_JOB_TITLE
            )
            expect(viewer_page.locator("img")).to_have_count(0)
            expect(viewer_page.locator("#history-body")).to_contain_text(
                HIDDEN_JOB_TITLE
            )
            assert _OWNED_TITLE not in viewer_page.content()
            assert "data:image/jpeg" not in viewer_page.content()
        finally:
            viewer_ctx.close()
            app.state.worker._current_job_id = None
            job_store.delete_job(job.id)
            assert seen, "the hand-built context's gate handled no request"
            assert blocked == [], f"a page tried to reach the network: {blocked}"
            assert violations == [], (
                f"a page violated its Content-Security-Policy: {violations}"
            )


# ---------------------------------------------------------------------------
# The simpler form: [web] show_tags = false.
# ---------------------------------------------------------------------------

_SIMPLE_FORM_DEFAULT_TAGS = [11, 13]
"""The profile's own tags, which a form with no tag picker must still apply."""

# Every id and hook the Tags block contributes to the page. The claim is that
# turning the block off removes the markup rather than hiding it, so the list
# is the whole block and not just its most visible element.
_TAG_MARKUP_SELECTORS = (
    "#tags-list",
    "#tag-filter",
    "#tag-filter-form",
    "label.tag-option",
    "fieldset [hx-post*='resource=tags']",
)


def _simple_form_settings(tmp_dir: Path) -> Settings:
    """
    Build settings with the Tags block off and a profile that carries its own.

    The default profile's ``default_tags`` is what makes the profile's tags
    observable at all: with no tag picker on the page the submit carries no ``tags`` field,
    so whatever lands on the job row came from the profile and from nowhere
    else.
    """
    configured = _browser_test_settings(tmp_dir)
    return configured.model_copy(
        update={
            "web": WebConfig(show_tags=False),
            "profiles": {
                "default": ProfileConfig(
                    description=_FLATBED_DESCRIPTION,
                    default_tags=_SIMPLE_FORM_DEFAULT_TAGS,
                ),
                "duplex": configured.profiles["duplex"],
            },
        }
    )


@pytest.fixture
def simple_form_server(
    tmp_path: Path, egress_allowlist: list[str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[_BrowserServer]:
    """
    Serve a private app whose scan form has no Tags block.

    The form shape comes from ``Settings`` and is read once per request from a
    config the process loaded at start, so there is no runtime setter: a second
    server is the only way to put a real browser in front of the simpler form,
    the same reason ``blocked_server`` exists. It is private rather than
    session-scoped because the claim is about what is absent from a whole page,
    and it runs a real scan, which the session server's history would carry
    into every later test.
    """
    with _serve(_simple_form_settings(tmp_path), _BrowserTestScanner()) as server:
        _make_paperless_deliver(server.app, monkeypatch)
        egress_allowlist.append(server.url)
        yield server


@pytest.mark.browser
class TestSimplerFormInChromium:
    """
    ``show_tags = false`` changes the form and never the scan.

    Two halves, and the second is the one that could go wrong quietly. The
    first is an absence claim about a whole rendered page, which is why this
    class gets its own server. The second is that a household member scanning
    from the simpler form still gets the profile's tags applied -- asserted on
    the job row, because the page has nothing left to say about tags and a
    markup assertion could not tell "applied" from "never asked for".
    """

    def test_the_tag_block_is_absent_and_the_profile_tags_still_apply(
        self,
        page: Page,
        simple_form_server: _BrowserServer,
    ) -> None:
        """
        No tag markup anywhere, and a scan still files under the profile's tags.

        Absent, never hidden: what is not in the markup cannot be re-shown from
        devtools, cannot be read out by a screen reader and cannot be tabbed
        into. The submit carries no ``tags`` field at all, so the two ids on
        the created job can only have come from the profile.
        """
        server = simple_form_server
        job_store: JobStore = server.app.state.job_store
        page.goto(server.url)
        # The form is present and usable, so the absences below are about the
        # Tags block rather than about a page that failed to render.
        expect(page.locator("#scan-btn")).to_be_enabled()

        for selector in _TAG_MARKUP_SELECTORS:
            expect(page.locator(selector)).to_have_count(0)
        # The help line goes with its fieldset: an orphan sentence describing a
        # control that is no longer there would be the tidier-looking bug.
        expect(page.locator("#tags-help")).to_have_count(0)

        with page.expect_response(lambda r: r.url.endswith("/api/scan")):
            page.click("#scan-btn")
        recent = job_store.list_recent(1)
        assert recent, "the scan submit created no job row"
        job = recent[0]
        assert job.tags == _SIMPLE_FORM_DEFAULT_TAGS, job.tags

        expect(page.locator("#status-area .status-done")).to_be_visible(timeout=15_000)
        wait_for_state(job_store, job.id, TERMINAL_STATES, timeout=_JOB_FINISH_TIMEOUT)


# ---------------------------------------------------------------------------
# The courtesy and the enforcement.
#
# The disabled Scan button and the route guard are two different promises, and
# only one of them is load-bearing. The first class below proves the button
# stays greyed out through a run of real status responses; the second removes
# the attribute the way anybody with devtools can and proves the scan is
# refused anyway.
# ---------------------------------------------------------------------------

_SERVICE_UNAVAILABLE = 503
"""The status a refused submit answers with, shared by all three refusals."""

_POLL_TICKS = 3
"""How many completed status responses count as "the poll ran for a while"."""

_TOKEN_UNSET_SLOT_TEXT = (
    "✗ The paperless-ngx API token has not been set, so the scan was not "
    "started. Put a real API token in the saneless config file, then restart "
    "saneless."
)
"""The slot's exact text for the refusal: the error partial's cross, then the copy."""

_TOKEN_UNSET_ROW_ERROR = "Not started: the paperless-ngx API token has not been set"
"""The job row's own error, written as the literal because it is locked copy."""


@pytest.fixture
def private_blocked_server(
    tmp_path: Path, egress_allowlist: list[str]
) -> Iterator[_BrowserServer]:
    """
    Serve a private app whose paperless-ngx token is the shipped placeholder.

    Private rather than the session-scoped ``blocked_server``: both tests below
    write to the job store -- one parks an active job on the worker, the other
    leaves a refused row behind -- and either would follow the rest of that
    fixture's class onto its supposedly idle page.

    No Paperless stub is installed, and that is not an oversight: on this
    server no scan can ever reach an upload, which is the whole point of it.
    """
    with _serve(_blocked_settings(tmp_path), _BrowserTestScanner()) as server:
        egress_allowlist.append(server.url)
        yield server


@pytest.mark.browser
class TestBlockedButtonThroughTheStatusPoll:
    """
    A run of real status responses does not give the blocked button back.

    ``test_the_blocked_button_survives_its_own_page_load_requests`` covers the
    requests the form issues on load. This covers the other stream of requests
    a real page makes: the one-second status poll, each response of which
    re-renders the button out of band from server state. The blocked flag has
    to survive every one of them, and the last one -- taken once nothing is
    active any more -- is where the flag is the only thing still holding the
    button shut.
    """

    def test_the_blocked_button_stays_blocked_across_a_run_of_polls(
        self, page: Page, private_blocked_server: _BrowserServer
    ) -> None:
        """
        The button stays disabled through three polls and after the job ends.

        Once the job ends, the blocked flag is the only thing still holding the
        button shut, so the last swap is the tightest of the four readings. The
        job is parked directly rather than scanned, because the guard refuses
        every submit on this server. The waits are completed responses, never a
        sleep: "several ticks" is a count of round trips.
        """
        server = private_blocked_server
        job_store: JobStore = server.app.state.job_store
        worker = server.app.state.worker
        job = job_store.create_job(profile="default", title="Polled Doc")
        job_store.update_state(job.id, JobState.SCANNING)
        worker._current_job_id = job.id
        try:
            page.goto(server.url)
            button = page.locator("#scan-btn")
            expect(button).to_be_disabled()

            for _ in range(_POLL_TICKS):
                with page.expect_response(
                    lambda r: _POLL_URL.search(r.url) is not None
                ):
                    pass

            # Read without retrying: a retrying assertion on a button that is
            # re-rendered every second would hide a trap that sprung and then
            # healed on the following tick.
            assert button.is_disabled(), "a status poll re-enabled a blocked button"
            assert button.get_attribute("aria-describedby") == "scan-blocked-reason"

            # Retire the job so the next out-of-band render is made with
            # nothing active. What is left holding the button shut is the
            # blocked flag and nothing else, which is the state the rest of
            # this module's blocked assertions are made in -- reached here
            # through a real swap rather than a fresh page load.
            job_store.finish_job(job.id, JobState.CANCELLED)
            worker._current_job_id = None

            expect(page.locator("#status-area .status-cancelled")).to_be_visible(
                timeout=10_000
            )
            assert button.is_disabled(), "the final swap re-enabled a blocked button"
            assert (button.text_content() or "").strip() == "Scan"
            assert button.get_attribute("aria-describedby") == "scan-blocked-reason"
            expect(page.locator(_BLOCKED_REASON_SELECTOR)).to_be_visible()
        finally:
            worker._current_job_id = None
            job_store.delete_job(job.id)


@pytest.mark.browser
class TestTheGuardBehindTheBlockedButton:
    """
    The button is the courtesy; the route guard is the refusal.

    The greyed-out button is a convenience anyone with devtools can take
    away, and taking it away buys nothing: the attribute is removed, the
    button clicked, and the server still refuses the scan.
    """

    def test_a_tampered_button_still_cannot_start_a_scan(
        self, page: Page, private_blocked_server: _BrowserServer
    ) -> None:
        """
        ``disabled`` removed, clicked, refused, and recorded as Failed.

        Three separate claims, because a refusal that left any one of them
        unmet would be a different bug: the person is told why in the slot, no
        scan started, and the attempt is in Job History rather than silently
        swallowed. The row's error is asserted through the job store
        because Job History renders a state label and not the sentence -- the
        sentence is what ``saneless jobs`` and the API surface.
        """
        server = private_blocked_server
        job_store: JobStore = server.app.state.job_store
        page.goto(server.url)
        # The lazy list load re-renders the button, still disabled because the
        # appliance is blocked; the tamper has to come after it, or that
        # answer puts the attribute back before the click.
        _await_the_lists(page)
        button = page.locator("#scan-btn")
        expect(button).to_be_disabled()

        page.evaluate(
            "() => document.getElementById('scan-btn').removeAttribute('disabled')"
        )
        expect(button).to_be_enabled()

        with page.expect_response(lambda r: r.url.endswith("/api/scan")) as caught:
            button.click()
        assert caught.value.status == _SERVICE_UNAVAILABLE, caught.value.status

        # Told why, in the slot reserved for request errors.
        expect(page.locator(_SLOT_MESSAGE)).to_have_text(_TOKEN_UNSET_SLOT_TEXT)
        # And nothing started: the status area was never the target of this
        # response, and it still says what this idle appliance says -- why no
        # scan can start, not that one can.
        expect(page.locator("#status-area")).to_have_text(SCAN_BLOCKED_REASON)
        expect(page.locator("#status-area [aria-busy]")).to_have_count(0)

        status_cell = page.locator("#history-body tr").first.locator("td").nth(3)
        expect(status_cell).to_have_text("Failed", timeout=5_000)
        expect(status_cell).to_have_class(re.compile(r"\bstatus-error\b"))

        recent = job_store.list_recent(1)
        assert recent, "the refused submit recorded no job row"
        written = recent[0]
        assert written.error == _TOKEN_UNSET_ROW_ERROR, written.error
        assert written.error_category is ErrorCategory.REJECTED


# ---------------------------------------------------------------------------
# Pages removed as blank, and a scan in which every page looked blank.
# ---------------------------------------------------------------------------

_REMOVED_NOTE = "Removed as blank: pages 2, 4 of 4 scanned."
"""The note a DONE job with blank backs at scanned positions 2 and 4 carries."""

_ALL_BLANK_FILE = "blank-scan.pdf"
"""The name of the PDF the all-blank job's error says was kept in failed/."""


@pytest.mark.browser
class TestRemovedBlankPagesRendering:
    """
    The removed-page note and the all-blank failure, proven in a browser.

    The note is information, not a warning, so the green tick has to survive
    it once the cascade has resolved -- which only a browser can see.  The
    all-blank failure has its own advice, and its kept PDF must reach the
    owner as a path under ``failed/`` and nobody else at all.
    """

    @pytest.fixture
    def removed_blank_page(
        self, page: Page, browser_server: _BrowserServer
    ) -> Iterator[Page]:
        """Drive the live app's current job to a DONE that removed two blank pages."""
        app = browser_server.app
        job_store: JobStore = app.state.job_store
        job = job_store.create_job(
            profile="default",
            title="Blank Backs Doc",
            owner_token=_as_owner(page, browser_server.url),
        )
        job_store.finish_job(
            job.id,
            JobState.DONE,
            result=JobResult(
                outcome=ScanOutcome.SUCCESS,
                warning=None,
                pages_scanned=4,
                pages_removed=2,
                pages_uploaded=2,
                removed_positions=(2, 4),
            ),
        )
        app.state.worker._current_job_id = job.id
        try:
            yield page
        finally:
            # Both halves, for the reason fallback_page gives: clearing only
            # the pointer leaves this job as list_recent's most recent row, and
            # every later "idle" page would render it.
            app.state.worker._current_job_id = None
            job_store.delete_job(job.id)

    @pytest.fixture
    def all_blank_error_page(
        self, page: Page, browser_server: _BrowserServer
    ) -> Iterator[tuple[Page, str]]:
        """Drive the live app's current job to an all-blank ERROR, then clear it."""
        app = browser_server.app
        job_store: JobStore = app.state.job_store
        settings: Settings = app.state.settings
        kept = settings.output.failed_dir / _ALL_BLANK_FILE
        job = job_store.create_job(
            profile="default",
            title="All Blank Doc",
            owner_token=_as_owner(page, browser_server.url),
        )
        job_store.finish_job(
            job.id,
            JobState.ERROR,
            error=(
                "All 4 page(s) looked blank, so nothing was uploaded. "
                f"The 4 scanned page(s) were preserved at {kept}"
            ),
            error_category=ErrorCategory.ALL_BLANK,
        )
        app.state.worker._current_job_id = job.id
        try:
            yield page, str(kept)
        finally:
            app.state.worker._current_job_id = None
            job_store.delete_job(job.id)

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_removed_positions_note_is_shown_and_done_stays_green(
        self,
        removed_blank_page: Page,
        browser_server: _BrowserServer,
        scheme: Literal["light", "dark"],
    ) -> None:
        """
        The note sits beside the counts, and the headline keeps the plain green.

        Removing blank backs is not a warning, so the DONE headline must
        resolve to the done colour and never to the warned amber.
        """
        removed_blank_page.emulate_media(color_scheme=scheme)
        removed_blank_page.goto(browser_server.url)
        _show_the_live_outcome(removed_blank_page, "#status-area .status-done")

        status = removed_blank_page.locator("#status-area")
        expect(status.locator("p.page-counts", has_text=_REMOVED_NOTE)).to_be_visible()
        expect(status).to_contain_text("Done: Blank Backs Doc")
        expect(status.locator(".status-fallback")).to_have_count(0)

        row = removed_blank_page.locator("#history-body tr", has_text="Blank Backs Doc")
        expect(row.locator("span.page-counts", has_text=_REMOVED_NOTE)).to_be_visible()
        expect(row.locator("td.status-done")).to_have_count(1)

        colours = removed_blank_page.evaluate(_PROBE_STATUS_COLOURS)
        headline = removed_blank_page.evaluate(
            "() => getComputedStyle("
            "document.querySelector('#status-area p.status-done')).color"
        )
        assert headline == colours["status-done"], colours
        assert headline != colours["status-fallback"], colours

    def test_all_blank_error_shows_tuning_advice(
        self,
        all_blank_error_page: tuple[Page, str],
        browser: Browser,
        browser_server: _BrowserServer,
        egress_allowlist: list[str],
    ) -> None:
        """
        The owner reads the advice and the kept file under failed/; others no path.

        The second context is built by hand, so it installs ``_make_gate`` and
        the policy recorder itself and is checked after it closes, as the
        two-browser ownership test does.
        """
        page, kept = all_blank_error_page
        message = error_message(ErrorCategory.ALL_BLANK)
        next_step = error_next_step(ErrorCategory.ALL_BLANK)
        assert "empty_page_coverage_threshold" in next_step

        page.goto(browser_server.url)
        _show_the_live_outcome(page, "#status-area p.status-error")
        status = page.locator("#status-area")
        expect(status.locator("p.status-error")).to_contain_text(message)
        expect(status).to_contain_text(next_step)
        status.locator("details.tech-details summary").click()
        expect(status.locator("details.tech-details")).to_contain_text(
            f"preserved at failed/{_ALL_BLANK_FILE}"
        )
        assert kept not in page.content()

        blocked: list[str] = []
        seen: list[str] = []
        violations: list[str] = []
        viewer_ctx = browser.new_context()
        try:
            viewer_ctx.route("**/*", _make_gate(blocked, egress_allowlist, seen))
            _make_csp_gate(viewer_ctx, violations)
            viewer_page = viewer_ctx.new_page()
            viewer_page.goto(browser_server.url)
            _show_the_live_outcome(viewer_page, "#status-area p.status-error")

            viewer_status = viewer_page.locator("#status-area")
            expect(viewer_status.locator("p.status-error")).to_contain_text(message)
            expect(viewer_status).to_contain_text(next_step)
            viewer_status.locator("details.tech-details summary").click()
            expect(viewer_status.locator("details.tech-details")).to_contain_text(
                HIDDEN_PRESERVED_ERROR
            )
            content = viewer_page.content()
            assert "failed/" not in content
            assert _ALL_BLANK_FILE not in content
            assert kept not in content
        finally:
            viewer_ctx.close()
            assert seen, "the hand-built context's gate handled no request"
            assert blocked == [], f"a page tried to reach the network: {blocked}"
            assert violations == [], (
                f"a page violated its Content-Security-Policy: {violations}"
            )


# ---------------------------------------------------------------------------
# The multi-page prompt, clicked through in a real browser.
#
# The route and template tests prove what the server renders and accepts.
# What only a browser can show is that the page is usable: that htmx sends the
# answers the buttons carry, that Abort's native confirmation really stands
# between a click and the request, that the row of buttons fits a phone, and
# that keyboard focus survives the status area being replaced every second.
# ---------------------------------------------------------------------------

_MULTI_PAGE_TITLE = "Two Pages One Document"
"""The title the click-through scans under, so its history row can be found."""

_PROMPT_BUTTON_ORDER = ["mp-next", "mp-finish", "mp-rescan", "mp-abort"]
"""The between-pass prompt's buttons, in the order the page must show them."""

_WAITING_FOR_YOU = "Waiting for you…"
"""The Scan button's caption while a multi-page document waits for its operator."""

_TOUCH_FLOOR_PX = 44
"""The smallest height a prompt button may have: the touch target floor."""

_ANSWER_URL = "/api/multi-page/answer"
"""Where every prompt button posts its answer."""

# One read of the prompt's layout, taken in a single call so no status swap can
# land between the measurements.  A function, not a bare expression: the
# page's Content-Security-Policy refuses eval.
_MEASURE_PROMPT = """
() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    overflowing: Array.from(document.body.querySelectorAll("*"))
        .filter((el) => el.getBoundingClientRect().right
            > document.documentElement.clientWidth)
        .map((el) => `${el.tagName.toLowerCase()}#${el.id}.${el.className}`
            + ` right=${el.getBoundingClientRect().right}`),
    overflowingText: (() => {
        const out = [];
        const walker = document.createTreeWalker(
            document.body, NodeFilter.SHOW_TEXT);
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
            const range = document.createRange();
            range.selectNodeContents(node);
            const right = range.getBoundingClientRect().right;
            if (right > document.documentElement.clientWidth) {
                out.push(`${node.parentElement.tagName}#${node.parentElement.id}`
                    + `.${node.parentElement.className}: `
                    + `${node.textContent.trim().slice(0, 60)} right=${right}`);
            }
        }
        return out;
    })(),
    buttons: Array.from(
        document.querySelectorAll("#status-area .prompt-actions button"),
    ).map((button) => {
        const box = button.getBoundingClientRect();
        return {
            id: button.id,
            top: box.top,
            left: box.left,
            right: box.right,
            height: box.height,
        };
    }),
})
"""


@pytest.fixture
def multi_page_scan_server(
    tmp_path: Path, egress_allowlist: list[str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[_BrowserServer]:
    """
    Serve a private app that runs real multi-page scans on its flatbed profile.

    Private for the reasons ``flip_server`` is: the owner cookie a real submit
    mints, and the rows these scans leave behind, must not follow later tests
    onto the session server.  The scanner is the one-page stub, so every pass
    keeps exactly one page, and uploads are delivered at once so Finish reaches
    DONE.  ``monkeypatch`` is requested here rather than by the test so the
    stubbed Paperless client outlives the server's shutdown.
    """
    with _serve(_browser_test_settings(tmp_path), _BrowserTestScanner()) as server:
        _make_paperless_deliver(server.app, monkeypatch)
        egress_allowlist.append(server.url)
        yield server


def _await_status_poll(page: Page) -> int:
    """
    Wait for the next status poll to come back and for htmx to settle the area.

    A poll that finds nothing changed is answered 204 and leaves the area in
    place; one that finds a change swaps it.  Either way, once the response is
    in and htmx has processed whatever area is now on the page, that area is
    the one a click or a keypress lands on: an unchanged one stays for as long
    as nothing changes, and a changed one is not replaced again until the next
    change.  Content the test needs to see is asserted separately, with
    Playwright's retrying ``expect``, never inferred from a swap having
    happened -- under the 204 rule a swap may never come.

    Args:
        page: The browser page, showing an active job.

    Returns:
        The poll response's status code: 204 when nothing changed, 200 when
        the area was re-rendered.

    """
    with page.expect_response(lambda r: _POLL_URL.search(r.url) is not None) as polled:
        pass
    expect(page.locator("#status-area.htmx-added")).to_have_count(0)
    return polled.value.status


def _start_multi_page_scan(
    page: Page,
    server: _BrowserServer,
    title: str,
) -> str:
    """
    Tick Multiple pages, press Scan, and wait for the first between-pass prompt.

    The submit is a real one, so this page's browser becomes the job's owner
    through the cookie the response sets.

    Args:
        page: The browser page that submits, and so becomes the owner.
        server: The private server to submit to.
        title: The title to submit.

    Returns:
        The id of the job now waiting for its second page.

    """
    job_store: JobStore = server.app.state.job_store
    page.goto(server.url)
    page.locator("#multi-page").check()
    page.fill("#title-input", title)
    with page.expect_response(lambda r: r.url.endswith("/api/scan")):
        page.click("#scan-btn")
    recent = job_store.list_recent(1)
    assert recent, "the scan submit created no job row"
    job_id = recent[0].id
    wait_for_state(job_store, job_id, JobState.AWAITING_NEXT_PASS, timeout=20.0)
    expect(page.locator("#status-area .pages-prompt")).to_contain_text(
        "1 page kept so far.", timeout=10_000
    )
    return job_id


def _abort_the_document(server: _BrowserServer, job_id: str) -> None:
    """
    End a multi-page job a test left running, by answering Abort to its prompt.

    A job left waiting would hold the server's shutdown until the operator
    timeout, so every test that starts one ends it here.  A job mid-pass is
    given until its next prompt; a job that has already ended is left alone.

    Args:
        server: The private server the job runs on.
        job_id: The job to end.

    """
    job_store: JobStore = server.app.state.job_store
    worker = server.app.state.worker

    def _ended_or_aborted() -> bool:
        job = job_store.get_job(job_id)
        if job is None or job.state in TERMINAL_STATES:
            return True
        prompt = worker.pass_prompt(job_id)
        return prompt is not None and worker.answer_pass(
            job_id, prompt.number, PassAnswer.ABORT
        )

    assert poll_until(_ended_or_aborted, _JOB_FINISH_TIMEOUT), (
        f"job {job_id} neither ended nor reached a prompt that could be aborted"
    )
    wait_for_state(job_store, job_id, TERMINAL_STATES, timeout=_JOB_FINISH_TIMEOUT)


class _StagedPrompt(NamedTuple):
    """A multi-page job staged at an open prompt, and the thread asking it."""

    job_id: str
    coordinator: WorkerPassCoordinator
    asker: threading.Thread


@contextmanager
def _staged_prompt(
    server: _BrowserServer, owner_token: str, prompt: PassPrompt
) -> Generator[_StagedPrompt]:
    """
    Stage a job waiting on ``prompt`` without scanning anything.

    Some prompts cannot be reached with the one-page stub scanner -- one with
    no page kept needs every page so far skipped as blank -- so the worker's
    current job and pass coordinator are set directly, the reach-through the
    flip browser tests use.  The prompt is asked on a thread of its own, as the
    worker thread would ask it, and is interrupted on the way out.

    Args:
        server: The server whose worker and store are staged.
        owner_token: The owner token the row records.
        prompt: The open question.

    Yields:
        The staged job, its coordinator and the asking thread.

    """
    job_store: JobStore = server.app.state.job_store
    worker = server.app.state.worker
    job = job_store.create_job(
        profile="default", title="Staged Pages", owner_token=owner_token
    )
    job_store.update_state(job.id, JobState.AWAITING_NEXT_PASS)
    coordinator = WorkerPassCoordinator(job.id, stopping=threading.Event())
    asker = threading.Thread(target=coordinator.ask, args=(prompt,), daemon=True)
    worker._current_job_id = job.id
    worker._pass_coordinator = coordinator
    asker.start()
    try:
        assert poll_until(
            lambda: coordinator.open_prompt == prompt, _JOB_FINISH_TIMEOUT
        ), "the staged prompt was never published"
        yield _StagedPrompt(job_id=job.id, coordinator=coordinator, asker=asker)
    finally:
        coordinator.interrupt_for_shutdown()
        asker.join(_JOB_FINISH_TIMEOUT)
        worker._pass_coordinator = None
        worker._current_job_id = None
        job_store.delete_job(job.id)
        assert not asker.is_alive(), "the staged prompt's ask never returned"


@pytest.mark.browser
class TestMultiPagePromptInTheBrowser:
    """
    A multi-page scan answered through its buttons, in Chromium.

    Every test but one drives a real scan: the Multiple pages box is ticked and
    Scan pressed, so the page is the job's owner by the cookie its own submit
    received, and each click goes through htmx to the answer route and on to
    the worker that is really waiting.
    """

    def test_a_two_page_document_is_scanned_page_by_page_and_finished(
        self,
        page: Page,
        multi_page_scan_server: _BrowserServer,
    ) -> None:
        """
        Tick, Scan, Scan next page, Finish document, and the document is uploaded.

        Between passes the page says what it is waiting for and the Scan
        button says whose move it is, so the scan never looks hung.  Only
        Abort asks for confirmation, and the buttons come in their fixed order.
        """
        server = multi_page_scan_server
        job_store: JobStore = server.app.state.job_store
        job_id = _start_multi_page_scan(page, server, _MULTI_PAGE_TITLE)
        try:
            prompt = page.locator("#status-area .pages-prompt")
            scan_btn = page.locator("#scan-btn")
            expect(scan_btn).to_have_text(_WAITING_FOR_YOU)
            expect(scan_btn).to_be_disabled()
            expect(scan_btn).not_to_have_attribute("aria-busy", "true")
            expect(page.locator("#status-area [aria-busy]")).to_have_count(0)
            buttons = prompt.locator(".prompt-actions button")
            expect(buttons).to_have_count(len(_PROMPT_BUTTON_ORDER))
            assert [
                buttons.nth(i).get_attribute("id")
                for i in range(len(_PROMPT_BUTTON_ORDER))
            ] == _PROMPT_BUTTON_ORDER
            confirming = prompt.locator("[hx-confirm]")
            expect(confirming).to_have_count(1)
            expect(confirming).to_have_id("mp-abort")

            _await_status_poll(page)
            with page.expect_response(lambda r: r.url.endswith(_ANSWER_URL)) as sent:
                page.click("#mp-next")
            assert sent.value.status == 200

            expect(prompt).to_contain_text("2 pages kept so far.", timeout=15_000)

            _await_status_poll(page)
            with page.expect_response(lambda r: r.url.endswith(_ANSWER_URL)) as sent:
                page.click("#mp-finish")
            assert sent.value.status == 200

            expect(page.locator("#status-area .status-done")).to_contain_text(
                _MULTI_PAGE_TITLE, timeout=15_000
            )
            done = wait_for_state(job_store, job_id, JobState.DONE)
            assert done.pages_uploaded == 2, done
            newest = page.locator("#history-body tr").first
            expect(newest).to_contain_text(_MULTI_PAGE_TITLE)
            status_cell = newest.locator("td").nth(3)
            expect(status_cell).to_have_text(job_label(JobState.DONE, None))
            expect(status_cell).to_have_class("status-done")
            expect(scan_btn).to_be_enabled()
            expect(scan_btn).to_have_text("Scan")
        finally:
            _abort_the_document(server, job_id)

    def test_abort_asks_first_and_does_nothing_when_the_answer_is_no(
        self,
        page: Page,
        multi_page_scan_server: _BrowserServer,
    ) -> None:
        """
        Abort raises the native confirmation naming the kept page; "no" keeps it.

        The "no" half is asserted from a recorded list of requests after a
        whole status poll has come back, so it says no answer was sent rather
        than that none was sent in the same instant as the click.
        """
        server = multi_page_scan_server
        job_store: JobStore = server.app.state.job_store
        asked: list[str] = []
        answer = ["dismiss"]
        posted: list[str] = []

        def _on_dialog(dialog: Dialog) -> None:
            asked.append(dialog.message)
            if answer[0] == "accept":
                dialog.accept()
            else:
                dialog.dismiss()

        def _on_request(request: Request) -> None:
            if request.method == "POST":
                posted.append(request.url)

        page.on("dialog", _on_dialog)
        page.on("request", _on_request)
        job_id = _start_multi_page_scan(page, server, "Abort Pages")
        try:
            _await_status_poll(page)
            page.click("#mp-abort")
            assert asked == [abort_question(1)], asked

            with page.expect_response(lambda r: _POLL_URL.search(r.url) is not None):
                pass
            assert [url for url in posted if url.endswith(_ANSWER_URL)] == [], posted
            waiting = job_store.get_job(job_id)
            assert waiting is not None
            assert waiting.state is JobState.AWAITING_NEXT_PASS
            expect(page.locator("#status-area .pages-prompt")).to_contain_text(
                "1 page kept so far."
            )

            answer[0] = "accept"
            _await_status_poll(page)
            with page.expect_response(lambda r: r.url.endswith(_ANSWER_URL)):
                page.click("#mp-abort")

            assert asked == [abort_question(1), abort_question(1)], asked
            expect(page.locator("#status-area .status-cancelled")).to_be_visible(
                timeout=15_000
            )
            wait_for_state(
                job_store, job_id, JobState.CANCELLED, timeout=_JOB_FINISH_TIMEOUT
            )
        finally:
            _abort_the_document(server, job_id)

    def test_finish_is_disabled_with_its_reason_while_no_page_is_kept(
        self, page: Page, multi_page_scan_server: _BrowserServer
    ) -> None:
        """
        With nothing kept, Finish keeps its place, disabled, with the reason shown.

        Staged rather than scanned: a prompt with no page kept needs every page
        so far skipped as blank, which the one-page stub never produces.
        """
        server = multi_page_scan_server
        prompt = PassPrompt(
            number=1,
            wait=PassWait.NEXT_PASS,
            pages_kept=0,
            offered=frozenset({PassAnswer.NEXT, PassAnswer.RESCAN, PassAnswer.ABORT}),
            timeout_seconds=600,
            last_pass_pages=1,
            last_pass_kept=0,
        )
        with _staged_prompt(server, _as_owner(page, server.url), prompt):
            page.goto(server.url)

            finish = page.locator("#mp-finish")
            reason = page.locator("#finish-blocked-reason")
            expect(page.locator("#status-area .pages-prompt")).to_contain_text(
                "No pages kept yet."
            )
            expect(finish).to_be_disabled()
            expect(finish).to_have_accessible_description(NOTHING_TO_FINISH)
            expect(reason).to_be_visible()
            expect(reason).to_have_text(NOTHING_TO_FINISH)
            for enabled in ("#mp-next", "#mp-rescan", "#mp-abort"):
                expect(page.locator(enabled)).to_be_enabled()

    def test_a_second_browser_sees_the_waiting_line_and_no_buttons(
        self,
        page: Page,
        browser: Browser,
        multi_page_scan_server: _BrowserServer,
        egress_allowlist: list[str],
    ) -> None:
        """
        Only the browser that started the scan is asked; anyone else is told.

        Two cookie jars: the owner's page holds the cookie its submit received,
        and a second context built by hand holds none.  The second is shown the
        plain waiting line with no spinner, and no control at all, rather than
        hidden ones.  It installs the egress gate and the policy recorder
        itself and is checked after it closes.
        """
        server = multi_page_scan_server
        job_id = _start_multi_page_scan(page, server, "Not Yours")
        blocked: list[str] = []
        seen: list[str] = []
        violations: list[str] = []
        viewer_ctx = browser.new_context()
        try:
            viewer_ctx.route("**/*", _make_gate(blocked, egress_allowlist, seen))
            _make_csp_gate(viewer_ctx, violations)
            viewer_page = viewer_ctx.new_page()
            viewer_page.goto(server.url)

            viewer_status = viewer_page.locator("#status-area")
            deadline = server.app.state.worker.pass_deadline(job_id)
            assert deadline is not None
            expect(viewer_status).to_contain_text(
                non_owner_wait_line(JobState.AWAITING_NEXT_PASS, deadline=deadline)
            )
            expect(viewer_status).not_to_contain_text("Not Yours")
            expect(viewer_status.locator("button")).to_have_count(0)
            expect(viewer_status.locator(".pages-prompt")).to_have_count(0)
            expect(viewer_status.locator("[aria-busy]")).to_have_count(0)
            expect(viewer_page.locator(f"[hx-post='{_ANSWER_URL}']")).to_have_count(0)
            # The owner, polling at the same moment, is still asked.
            expect(page.locator("#status-area .pages-prompt")).to_be_visible()
        finally:
            viewer_ctx.close()
            _abort_the_document(server, job_id)
            assert seen, "the hand-built context's gate handled no request"
            assert blocked == [], f"a page tried to reach the network: {blocked}"
            assert violations == [], (
                f"a page violated its Content-Security-Policy: {violations}"
            )

    def test_the_buttons_wrap_on_a_320px_phone_without_a_sideways_scrollbar(
        self,
        page: Page,
        multi_page_scan_server: _BrowserServer,
    ) -> None:
        """
        Four buttons fit a 320 px screen by wrapping, each a full touch target.

        The layout is read after a status poll has settled, so it is the layout
        the page keeps rather than the one before htmx processed the swap.
        """
        server = multi_page_scan_server
        page.set_viewport_size({"width": 320, "height": 640})
        job_id = _start_multi_page_scan(page, server, "Phone Pages")
        try:
            _await_status_poll(page)
            layout = page.evaluate(_MEASURE_PROMPT)

            assert layout["scrollWidth"] <= layout["clientWidth"], (
                "the page scrolls sideways at 320px; wider than the viewport: "
                f"{layout['overflowing']} {layout['overflowingText']}"
            )
            buttons = layout["buttons"]
            assert [button["id"] for button in buttons] == _PROMPT_BUTTON_ORDER
            for button in buttons:
                assert button["height"] >= _TOUCH_FLOOR_PX, button
                assert button["left"] >= 0, button
                assert button["right"] <= layout["clientWidth"], button
            rows = {round(button["top"]) for button in buttons}
            assert len(rows) > 1, f"the buttons did not wrap: {buttons}"
        finally:
            _abort_the_document(server, job_id)

    def test_keyboard_focus_on_a_button_survives_the_status_poll(
        self,
        page: Page,
        multi_page_scan_server: _BrowserServer,
    ) -> None:
        """
        A keyboard user keeps their place while the status area is polled.

        The area is polled once a second while the job waits.  Nothing changes
        while the question stays open, so every poll is answered 204 and the
        area -- the focused button with it -- is never replaced.  The button
        the keyboard reached is marked, and after two polls that each came
        back 204 the same marked node still holds focus: not merely a button
        with the same id, the very element the keyboard landed on.  It is read
        once, not retried.
        """
        server = multi_page_scan_server
        job_id = _start_multi_page_scan(page, server, "Focus Pages")
        try:
            _await_status_poll(page)
            page.locator("#mp-finish").focus()
            page.keyboard.press("Shift+Tab")
            assert page.evaluate("() => document.activeElement.id") == "mp-next"
            page.locator("#mp-next").evaluate(
                "button => button.setAttribute('data-focused', '')"
            )

            assert [_await_status_poll(page) for _ in range(2)] == [204, 204]

            expect(page.locator("#mp-next[data-focused]")).to_have_count(1)
            assert page.evaluate(
                "() => document.activeElement.hasAttribute('data-focused')"
            )
            assert page.evaluate("() => document.activeElement.id") == "mp-next"
        finally:
            _abort_the_document(server, job_id)


# Counts every write into the alert slot: a child added or removed, or text
# changed, anywhere under ``#status-message``.  Attributes are left out on
# purpose -- htmx toggles its own classes on any element it settles, and what
# the operator would read is the slot's content.  Registered as an init script
# and attached once the document is parsed, so the count covers the page's
# whole life, from before the first poll.
_RECORD_SLOT_MUTATIONS = """
window.__slotMutations = 0;
document.addEventListener("DOMContentLoaded", () => {
    const slot = document.getElementById("status-message");
    new MutationObserver((records) => {
        window.__slotMutations += records.length;
    }).observe(slot, {childList: true, subtree: true, characterData: true});
});
"""


@pytest.mark.browser
class TestStatusPollLostContact:
    """
    A job store that cannot be read never reaches the alert slot, in Chromium.

    The alert slot is for the operator's own failed actions.  A status poll
    failing into it would re-write the page's one alert on every tick and
    leave an error standing above "Done" once the store healed.  These tests break
    the store's job reads under a live, held scan and watch both elements.
    """

    def test_lost_contact_never_touches_the_alert_slot(
        self,
        page: Page,
        scan_harness: _ScanHarness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The status area shows the retrying line; the slot is never written.

        ``get_job`` raises on every thread but the worker's, so the scan
        itself carries on while every status poll fails.  Once the store is
        healed and the gate opened, the same page reaches "Done" with the
        slot still empty: nothing was left above it.  The waits are completed
        responses and retrying assertions, never a sleep.
        """
        server = scan_harness.server
        job_store = scan_harness.job_store
        worker_thread = server.app.state.worker._thread
        original = job_store.get_job
        broken = threading.Event()

        def get_job(job_id: str) -> Job | None:
            if broken.is_set() and threading.current_thread() is not worker_thread:
                msg = "disk I/O error"
                raise sqlite3.OperationalError(msg)
            return original(job_id)

        monkeypatch.setattr(job_store, "get_job", get_job)
        page.add_init_script(_RECORD_SLOT_MUTATIONS)
        page.goto(server.url)
        server.scanner.gate.clear()
        try:
            page.locator("#scan-btn").click()
            expect(page.locator("#status-area p.busy-line")).to_be_visible()
            created = scan_harness.created_job_ids()
            assert len(created) == 1, created
            wait_for_state(job_store, created[0], JobState.SCANNING)

            broken.set()
            # Two failing polls: by the time the second is answered, the
            # first one's response has been swapped wherever it was going.
            for _ in range(2):
                with page.expect_response(
                    lambda r: _POLL_URL.search(r.url) is not None, timeout=10_000
                ):
                    pass
            assert page.evaluate("() => window.__slotMutations") == 0, (
                "a failing status poll wrote into the alert slot"
            )
            expect(page.locator("#status-area .status-fallback")).to_contain_text(
                LOST_CONTACT_LINE
            )
        finally:
            broken.clear()
            server.scanner.gate.set()

        expect(page.locator("#status-area .status-done")).to_be_visible(timeout=30_000)
        expect(page.locator("#status-message")).to_be_empty()
        assert page.evaluate("() => window.__slotMutations") == 0


# Records one signature per swap of the status area inside its persistent
# region: the region's text with runs of whitespace collapsed, then the alt
# text of every image in it.  The alt is part of the signature because the
# preview arriving is a visible change whose text is otherwise the same, so a
# text-only signature would read that one swap as a repeat.  Registered as an
# init script, which the page's Content-Security-Policy does not govern, and
# attached once the document is parsed: the area the page loads with is not a
# swap, and a region that is missing records nothing.
_STATUS_SWAP_RECORDER = """
window.__statusSwaps = [];
document.addEventListener("DOMContentLoaded", () => {
    const region = document.getElementById("status-live");
    if (region === null) {
        return;
    }
    new MutationObserver((records) => {
        const swapped = records.some((record) => Array.from(record.addedNodes)
            .some((node) => node.id === "status-area"));
        if (swapped) {
            const text = region.textContent.replace(/\\s+/g, " ").trim();
            const alts = Array.from(region.querySelectorAll("img"))
                .map((img) => img.alt);
            window.__statusSwaps.push([text, ...alts].join(" | "));
        }
    }).observe(region, {childList: true, subtree: true});
});
"""

_HELD_POLLS = 2
"""How many unchanged polls a held state must survive without a new swap."""

_SETTLE_POLLS = 5
"""How many polls a page may take to catch up with a state before it is held."""


def _status_swaps(page: Page) -> list[str]:
    """
    Return every status-area swap ``_STATUS_SWAP_RECORDER`` has seen, in order.

    Args:
        page: A page whose context installed the recorder before it loaded.

    Returns:
        One signature per swap.

    """
    # A function, not a bare expression: the page's policy refuses eval.
    swaps: list[str] = page.evaluate("() => window.__statusSwaps")
    return swaps


def _await_an_unchanged_poll(page: Page) -> None:
    """
    Wait until a status poll is answered 204: the page shows what the server has.

    Args:
        page: A page showing an active job.

    """
    statuses = [_await_status_poll(page) for _ in range(_SETTLE_POLLS)]
    assert HTTPStatus.NO_CONTENT in statuses, (
        f"no poll found the page up to date in {_SETTLE_POLLS} tries: {statuses}"
    )


@pytest.mark.browser
class TestStatusAnnouncements:
    """
    The status region changes once per visible change, and never while held.

    A screen reader announces a polite region each time its content is
    replaced, so the number of swaps inside ``#status-live`` is the number of
    announcements.  The recorder counts them; a held state must add none, and
    a whole scan must never swap the same thing in twice running.
    """

    def test_a_held_state_is_announced_once(
        self, page: Page, scan_harness: _ScanHarness
    ) -> None:
        """
        A scan held in SCANNING is swapped in once, then polled in silence.

        The page is first allowed to catch up -- a poll answered 204 is the
        server saying the page already shows the state -- and from there two
        further polls are both 204 and the recorder gains nothing.
        """
        server = scan_harness.server
        page.context.add_init_script(_STATUS_SWAP_RECORDER)
        page.goto(server.url)
        expect(page.locator("#status-message + #status-live")).to_have_count(1)
        server.scanner.gate.clear()
        try:
            page.locator("#scan-btn").click()
            area = page.locator("#status-live > #status-area")
            expect(area).to_contain_text(progress_label(JobState.SCANNING))
            _await_an_unchanged_poll(page)

            held = _status_swaps(page)
            assert held, "the scan's status was never swapped into the region"
            assert progress_label(JobState.SCANNING) in held[-1], held

            polls = [_await_status_poll(page) for _ in range(_HELD_POLLS)]
            assert polls == [HTTPStatus.NO_CONTENT] * _HELD_POLLS, polls
            assert _status_swaps(page) == held
        finally:
            server.scanner.gate.set()
        expect(page.locator("#status-area .status-done")).to_be_visible(timeout=15_000)

    def test_a_scan_is_announced_once_per_state(
        self, page: Page, scan_harness: _ScanHarness
    ) -> None:
        """
        From Scan to Done, no two swaps running are the same announcement.

        The scan is held long enough to be seen scanning and then let go, so
        the record covers the submit, the progress states and the outcome.
        """
        server = scan_harness.server
        page.context.add_init_script(_STATUS_SWAP_RECORDER)
        page.goto(server.url)
        server.scanner.gate.clear()
        try:
            page.locator("#scan-btn").click()
            expect(page.locator("#status-area")).to_contain_text(
                progress_label(JobState.SCANNING)
            )
        finally:
            server.scanner.gate.set()
        expect(page.locator("#status-area .status-done")).to_be_visible(timeout=15_000)

        swaps = _status_swaps(page)
        assert len(swaps) >= 2, swaps
        assert any(progress_label(JobState.SCANNING) in swap for swap in swaps), swaps
        assert "Done" in swaps[-1], swaps
        repeats = [
            (before, after) for before, after in pairwise(swaps) if before == after
        ]
        assert repeats == [], f"a state was announced twice running: {swaps}"


# One read of the page's width, taken in a single call so no swap can land
# between the measurements.  The overflowing text is there to name the culprit
# when the assertion fails: a line that will not wrap runs out of its own box,
# so it is the text, not the element, that reaches past the edge.  A function:
# the page's policy refuses eval.
_MEASURE_REFLOW = """
() => {
    const edge = document.documentElement.clientWidth;
    const overflowing = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const range = document.createRange();
        range.selectNodeContents(node);
        if (range.getBoundingClientRect().right > edge) {
            overflowing.push(`${node.parentElement.tagName}.`
                + `${node.parentElement.className}: `
                + node.textContent.trim().slice(0, 60));
        }
    }
    return {
        overflow: document.documentElement.scrollWidth - edge,
        overflowing: overflowing,
        stripBusy: document.querySelector('#checks-body[aria-busy="true"]') !== null,
    };
}
"""

_QUEUE_AHEAD = 2
"""How many queued jobs the reflow test's own queued job waits behind."""

_FRONT_PAGE_COUNT = 3
"""The first-pass page count the manual-duplex busy line leads with."""

_KEPT_PAGE_COUNT = 3
"""The pages a multi-page document holds while its later pass scans."""


class _Stage(NamedTuple):
    """What a reflow staging needs: the page, its private server, a patcher."""

    page: Page
    server: _BrowserServer
    monkeypatch: pytest.MonkeyPatch


@contextmanager
def _parked(
    stage: _Stage, state: JobState, title: str = "Reflow Doc"
) -> Generator[str]:
    """
    Park one job owned by the page as the worker's current job, in ``state``.

    Args:
        stage: The page and server to stage on.
        state: The state to write on the row.
        title: The row's title.

    Yields:
        The job's id.

    """
    job_store: JobStore = stage.server.app.state.job_store
    worker = stage.server.app.state.worker
    owner = _as_owner(stage.page, stage.server.url)
    job = job_store.create_job(profile="default", title=title, owner_token=owner)
    if state is not JobState.PENDING:
        job_store.update_state(job.id, state)
    worker._current_job_id = job.id
    try:
        yield job.id
    finally:
        worker._current_job_id = None
        job_store.delete_job(job.id)


def _open_on(stage: _Stage, line: str) -> None:
    """
    Load the page and wait for the status area to say ``line``.

    Args:
        stage: The page and server.
        line: Text the area must contain before anything is measured.

    """
    stage.page.goto(stage.server.url)
    expect(stage.page.locator("#status-area")).to_contain_text(line)


@contextmanager
def _reflow_queued(stage: _Stage) -> Generator[None]:
    """Queue the page's job behind two others and a running max-length title."""
    job_store: JobStore = stage.server.app.state.job_store
    title = "W" * TITLE_MAX_LENGTH
    owner = _as_owner(stage.page, stage.server.url)
    with _parked(stage, JobState.SCANNING, title):
        queued = [
            job_store.create_job(
                profile="default", title=f"Queued {index}", owner_token=owner
            ).id
            for index in range(_QUEUE_AHEAD + 1)
        ]
        try:
            # The page follows this browser's own newest job, the last queued.
            _open_on(
                stage,
                busy_line(
                    JobState.PENDING, queue_title=title, queue_ahead=_QUEUE_AHEAD
                ),
            )
            yield
        finally:
            for job_id in queued:
                job_store.delete_job(job_id)


@contextmanager
def _reflow_starting(stage: _Stage) -> Generator[None]:
    """Park the page's job as the one the worker is starting."""
    with _parked(stage, JobState.PENDING):
        _open_on(stage, busy_line(JobState.PENDING))
        yield


@contextmanager
def _reflow_later_pass(stage: _Stage) -> Generator[None]:
    """Answer a between-pass prompt with Scan next, so a later pass is scanning."""
    prompt = PassPrompt(
        number=1,
        wait=PassWait.NEXT_PASS,
        pages_kept=_KEPT_PAGE_COUNT,
        offered=frozenset(
            {PassAnswer.NEXT, PassAnswer.FINISH, PassAnswer.RESCAN, PassAnswer.ABORT}
        ),
        timeout_seconds=600,
        last_pass_pages=1,
        last_pass_kept=1,
    )
    owner = _as_owner(stage.page, stage.server.url)
    with _staged_prompt(stage.server, owner, prompt) as staged:
        assert staged.coordinator.answer(prompt.number, PassAnswer.NEXT)
        staged.asker.join(_JOB_FINISH_TIMEOUT)
        stage.server.app.state.job_store.update_state(staged.job_id, JobState.SCANNING)
        _open_on(stage, busy_line(JobState.SCANNING, pages_kept=_KEPT_PAGE_COUNT))
        yield


@contextmanager
def _reflow_front_count(stage: _Stage) -> Generator[None]:
    """Park a manual-duplex job scanning its backs, after a counted front pass."""
    worker = stage.server.app.state.worker
    with _parked(stage, JobState.SCANNING_REVERSE):
        worker._front_pages = _FRONT_PAGE_COUNT
        try:
            _open_on(
                stage,
                busy_line(JobState.SCANNING_REVERSE, front_pages=_FRONT_PAGE_COUNT),
            )
            yield
        finally:
            worker._front_pages = None


@contextmanager
def _reflow_flip_acknowledgement(stage: _Stage) -> Generator[None]:
    """Answer a waiting flip with Continue, so the acknowledgement shows."""
    worker = stage.server.app.state.worker
    with _parked(stage, JobState.AWAITING_FLIP) as job_id:
        coordinator = WorkerFlipCoordinator(job_id)
        coordinator.arm()
        worker._flip_coordinator = coordinator
        try:
            assert worker.continue_flip(job_id)
            _open_on(stage, flip_answer_label(FlipOutcome.CONTINUED))
            yield
        finally:
            worker._flip_coordinator = None


@contextmanager
def _reflow_pass_acknowledgement(stage: _Stage) -> Generator[None]:
    """Interrupt an open between-pass prompt: the longest acknowledgement."""
    prompt = PassPrompt(
        number=1,
        wait=PassWait.NEXT_PASS,
        pages_kept=1,
        offered=frozenset(
            {PassAnswer.NEXT, PassAnswer.FINISH, PassAnswer.RESCAN, PassAnswer.ABORT}
        ),
        timeout_seconds=600,
        last_pass_pages=1,
        last_pass_kept=1,
    )
    owner = _as_owner(stage.page, stage.server.url)
    with _staged_prompt(stage.server, owner, prompt) as staged:
        assert staged.coordinator.interrupt_for_shutdown()
        _open_on(stage, pass_answer_label(PassAnswer.INTERRUPTED))
        yield


@contextmanager
def _reflow_settling_strip(stage: _Stage) -> Generator[None]:
    """Hold a scan while the cold strip is still asking for its results."""
    with _parked(stage, JobState.SCANNING):
        _open_on(stage, busy_line(JobState.SCANNING))
        expect(stage.page.locator('#checks-body[aria-busy="true"]')).to_have_count(1)
        yield


@contextmanager
def _reflow_last_scan_failed(stage: _Stage) -> Generator[None]:
    """
    Load the idle page over a failed scan whose title has no spaces.

    Both "Last scan" lines render: the outcome naming the title, and the
    category's message and next step beneath it.
    """
    job_store: JobStore = stage.server.app.state.job_store
    with _parked(stage, JobState.ERROR, "W" * TITLE_MAX_LENGTH) as job_id:
        job_store.update_state(
            job_id,
            JobState.ERROR,
            error="disk on fire",
            error_category=ErrorCategory.SCANNER,
        )
        _open_on(stage, "Last scan: ")
        expect(stage.page.locator("#status-area .last-scan")).to_have_count(2)
        yield


@contextmanager
def _reflow_lost_contact(stage: _Stage) -> Generator[None]:
    """Break the store's job reads under a scan, so the poll shows its fallback."""
    job_store: JobStore = stage.server.app.state.job_store
    broken = threading.Event()

    def _guarded(original: Callable[..., object]) -> Callable[..., object]:
        def guarded(*args: object, **kwargs: object) -> object:
            if broken.is_set():
                msg = "disk I/O error"
                raise sqlite3.OperationalError(msg)
            return original(*args, **kwargs)

        return guarded

    for name in ("get_job", "latest_run_job"):
        stage.monkeypatch.setattr(job_store, name, _guarded(getattr(job_store, name)))
    with _parked(stage, JobState.SCANNING):
        _open_on(stage, busy_line(JobState.SCANNING))
        broken.set()
        try:
            expect(stage.page.locator("#status-area")).to_contain_text(
                LOST_CONTACT_LINE, timeout=10_000
            )
            yield
        finally:
            broken.clear()


@contextmanager
def _armed_flip(stage: _Stage, job_id: str) -> Generator[None]:
    """
    Give the worker an armed flip wait for ``job_id``, so its deadline is known.

    Args:
        stage: The page and server.
        job_id: The job waiting at the flip.

    """
    worker = stage.server.app.state.worker
    coordinator = WorkerFlipCoordinator(job_id)
    coordinator.arm()
    worker._flip_coordinator = coordinator
    try:
        assert worker.flip_deadline(job_id) is not None
        yield
    finally:
        worker._flip_coordinator = None


@contextmanager
def _reflow_owner_flip_prompt(stage: _Stage) -> Generator[None]:
    """
    Open the owner's flip prompt for a max-length title with no spaces.

    The job line, the instructions, the joined button group and the deadline
    note are all on the page when it is measured.
    """
    title = "W" * TITLE_MAX_LENGTH
    with (
        _parked(stage, JobState.AWAITING_FLIP, title) as job_id,
        _armed_flip(stage, job_id),
    ):
        _open_on(stage, flip_heading(title))
        expect(stage.page.locator("#status-area .prompt-note")).to_be_visible()
        expect(stage.page.locator("#flip-continue")).to_be_visible()
        yield


@contextmanager
def _reflow_non_owner_flip_wait(stage: _Stage) -> Generator[None]:
    """Show another browser's flip wait, with its deadline, to this page."""
    job_store: JobStore = stage.server.app.state.job_store
    worker = stage.server.app.state.worker
    job = job_store.create_job(
        profile="default",
        title="W" * TITLE_MAX_LENGTH,
        owner_token="a-browser-that-is-not-this-one",
    )
    job_store.update_state(job.id, JobState.AWAITING_FLIP)
    worker._current_job_id = job.id
    try:
        with _armed_flip(stage, job.id):
            deadline = worker.flip_deadline(job.id)
            assert deadline is not None
            _open_on(
                stage, non_owner_wait_line(JobState.AWAITING_FLIP, deadline=deadline)
            )
            yield
    finally:
        worker._current_job_id = None
        job_store.delete_job(job.id)


@contextmanager
def _reflow_lists_loading(stage: _Stage) -> Generator[None]:
    """Hold the lazy list load, so the page shows its placeholders and hold line."""
    held = _hold_the_list_load(stage.page)
    stage.page.goto(stage.server.url)
    load = _await_the_held_load(stage.page, held)
    expect(stage.page.locator("#scan-hold-reason")).to_be_visible()
    expect(stage.page.locator("#tags-list")).to_have_text(TAGS_LOADING)
    try:
        yield
    finally:
        load.abort()


@contextmanager
def _reflow_lists_unavailable(stage: _Stage) -> Generator[None]:
    """Let both lists fail, as they do against the stage's closed paperless-ngx."""
    stage.page.goto(stage.server.url)
    expect(stage.page.locator("#tags-list")).to_contain_text(TAGS_UNAVAILABLE)
    expect(stage.page.locator("#correspondent-help")).to_contain_text(
        CORRESPONDENTS_UNAVAILABLE
    )
    _await_the_lists(stage.page)
    yield


_REFLOW_ROWS: dict[str, Callable[[_Stage], AbstractContextManager[None]]] = {
    "queued-with-max-length-title": _reflow_queued,
    "starting": _reflow_starting,
    "later-multi-page-pass": _reflow_later_pass,
    "manual-duplex-front-count": _reflow_front_count,
    "flip-acknowledgement": _reflow_flip_acknowledgement,
    "pass-acknowledgement": _reflow_pass_acknowledgement,
    "settling-strip": _reflow_settling_strip,
    "lost-contact": _reflow_lost_contact,
    "last-scan-failed": _reflow_last_scan_failed,
    "owner_flip_prompt": _reflow_owner_flip_prompt,
    "non_owner_flip_wait": _reflow_non_owner_flip_wait,
    "lists_loading": _reflow_lists_loading,
    "lists_unavailable": _reflow_lists_unavailable,
}


@pytest.mark.browser
class TestStatusReflow:
    """
    Every busy status line wraps at 320 px, and its spinner obeys reduced motion.

    A busy line carries no ``aria-busy``: Pico keeps any busy element on one
    line, so a queued line naming a long title would push the whole page wider
    than a phone.  Each row here stages one busy state on a private server and
    measures the document at 320 x 640.  Two rows stage the form instead: its
    lists loading, with Scan's hold line, and its lists unavailable.
    """

    @pytest.mark.parametrize("row", list(_REFLOW_ROWS))
    def test_status_reflows_at_320px(
        self,
        page: Page,
        cold_strip_server: _BrowserServer,
        monkeypatch: pytest.MonkeyPatch,
        row: str,
    ) -> None:
        """The page never scrolls sideways at 320 px in this busy state."""
        page.set_viewport_size({"width": 320, "height": 640})
        stage = _Stage(page, cold_strip_server, monkeypatch)
        with _REFLOW_ROWS[row](stage):
            layout = page.evaluate(_MEASURE_REFLOW)

        assert layout["overflow"] <= 0, (
            f"the page scrolls sideways by {layout['overflow']}px at 320px: "
            f"{layout['overflowing']}"
        )
        if row == "settling-strip":
            assert layout["stripBusy"], "the strip had settled before it was measured"

    def test_busy_spinner_is_still_under_reduced_motion(
        self,
        page: Page,
        cold_strip_server: _BrowserServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        With reduced motion the spinner's animated image is replaced by a ring.

        The loading image animates inside its own SVG, which no page rule can
        pause, so the only way to stop it is not to draw it.  Read both ways,
        so the reduced-motion reading cannot pass for want of any image at all.
        """
        stage = _Stage(page, cold_strip_server, monkeypatch)
        with _reflow_starting(stage):
            spinner = page.locator("#status-area .busy-line")
            read = "(line) => getComputedStyle(line, '::before').backgroundImage"
            assert spinner.evaluate(read).startswith("url("), spinner.evaluate(read)

            page.emulate_media(reduced_motion="reduce")
            assert spinner.evaluate(read) == "none"


def _accept_the_confirmation(page: Page) -> None:
    """Answer the next native confirmation on ``page`` with OK."""

    def _on_dialog(dialog: Dialog) -> None:
        dialog.accept()

    page.once("dialog", _on_dialog)


def _end_the_flip(server: _BrowserServer, job_id: str) -> None:
    """
    Bring a flip job a test started to an end before the server shuts down.

    An Abort after an answer has already won is dropped, so this ends a job
    left at the prompt and leaves an answered one to finish on its own.

    Args:
        server: The private flip server.
        job_id: The job to end.

    """
    job_store: JobStore = server.app.state.job_store
    server.app.state.worker.abort_flip(job_id)
    wait_for_state(job_store, job_id, TERMINAL_STATES, timeout=_JOB_FINISH_TIMEOUT)


@pytest.mark.browser
class TestFocusMap:
    """
    Every row of the focus map, asserted on ``document.activeElement``.

    Focus is placed by the server alone: an action's response carries
    ``autofocus`` on the status area, a claimed Abort asks for the Scan button
    through its poll URL, and a prompt's first appearance carries it on its
    primary button, all applied by htmx with no script of the page's own.
    ``to_be_focused`` retries, so each assertion waits for the swap that moves
    focus rather than for a fixed time.  Check again's row lives with the
    strip's tests.
    """

    def test_focus_map_scan(self, page: Page, scan_harness: _ScanHarness) -> None:
        """
        Scan pressed from the keyboard moves focus to the status area.

        The scan is held in SCANNING, so the polls that follow change nothing
        and leave focus where the press put it.
        """
        server = scan_harness.server
        page.goto(server.url)
        # A held button cannot take focus; the lists release it.
        _await_the_lists(page)
        server.scanner.gate.clear()
        try:
            page.locator("#scan-btn").focus()
            page.keyboard.press("Enter")
            area = page.locator("#status-area")
            expect(area).to_be_focused()
            expect(area).to_contain_text(progress_label(JobState.SCANNING))
            _await_status_poll(page)
            expect(area).to_be_focused()
        finally:
            server.scanner.gate.set()
        expect(page.locator("#status-area .status-done")).to_be_visible(timeout=15_000)

    def test_focus_map_prompt_appears_for_the_owner(
        self,
        page: Page,
        flip_server: _BrowserServer,
    ) -> None:
        """
        The flip prompt arriving through the poll puts focus on Continue.

        The submit put focus on the status area; the owner's poll rendering of
        the new prompt moves it on to the button the operator needs next, and
        the unchanged polls after it leave it there.
        """
        job_id = _drive_to_flip_prompt(page, flip_server, "Focus Prompt")
        try:
            expect(page.locator("#flip-continue")).to_be_focused(timeout=10_000)
            _await_status_poll(page)
            expect(page.locator("#flip-continue")).to_be_focused()
        finally:
            _end_the_flip(flip_server, job_id)

    def test_focus_map_flip_continue(
        self,
        page: Page,
        flip_server: _BrowserServer,
    ) -> None:
        """Continue, pressed with Enter, moves focus to the status area."""
        job_id = _drive_to_flip_prompt(page, flip_server, "Focus Continue")
        try:
            expect(page.locator("#flip-continue")).to_be_focused(timeout=10_000)
            page.keyboard.press("Enter")
            expect(page.locator("#status-area")).to_be_focused()
        finally:
            _end_the_flip(flip_server, job_id)

    def test_focus_map_flip_abort(
        self,
        page: Page,
        flip_server: _BrowserServer,
    ) -> None:
        """
        A confirmed Abort ends with focus on the Scan button.

        The response focuses the status area while the scan winds down, and
        the first poll whose Scan button is enabled moves focus onto it.
        """
        job_id = _drive_to_flip_prompt(page, flip_server, "Focus Abort")
        try:
            expect(page.locator("#flip-abort")).to_be_visible(timeout=10_000)
            _accept_the_confirmation(page)
            page.locator("#flip-abort").focus()
            page.keyboard.press("Enter")
            scan = page.locator("#scan-btn")
            expect(scan).to_be_enabled(timeout=10_000)
            expect(scan).to_be_focused()
            expect(page.locator("#status-area .status-cancelled")).to_be_visible()
        finally:
            _end_the_flip(flip_server, job_id)

    def test_focus_map_multi_page_scan_next_then_next_question(
        self,
        page: Page,
        multi_page_scan_server: _BrowserServer,
    ) -> None:
        """
        Scan next focuses the status area, then the next question's Scan next.

        The next pass is held at the scanner, so the status area's focus is
        seen before the next question arrives and takes it.
        """
        server = multi_page_scan_server
        job_id = _start_multi_page_scan(page, server, "Focus Next")
        try:
            expect(page.locator("#mp-next")).to_be_focused(timeout=10_000)
            server.scanner.gate.clear()
            page.keyboard.press("Enter")
            area = page.locator("#status-area")
            expect(area).to_be_focused()
            expect(area.locator(".pages-prompt")).to_have_count(0)
            server.scanner.gate.set()
            expect(area.locator(".pages-prompt")).to_contain_text(
                "2 pages kept so far.", timeout=10_000
            )
            expect(page.locator("#mp-next")).to_be_focused()
        finally:
            server.scanner.gate.set()
            _abort_the_document(server, job_id)

    def test_focus_map_multi_page_finish(
        self,
        page: Page,
        multi_page_scan_server: _BrowserServer,
    ) -> None:
        """Finish moves focus to the status area, and it stays through Done."""
        server = multi_page_scan_server
        job_id = _start_multi_page_scan(page, server, "Focus Finish")
        try:
            page.locator("#mp-finish").focus()
            page.keyboard.press("Enter")
            area = page.locator("#status-area")
            expect(area).to_be_focused()
            expect(area.locator(".status-done")).to_be_visible(timeout=15_000)
            expect(area).to_be_focused()
        finally:
            _abort_the_document(server, job_id)

    def test_focus_map_multi_page_abort(
        self,
        page: Page,
        multi_page_scan_server: _BrowserServer,
    ) -> None:
        """A confirmed Abort of the document ends with focus on the Scan button."""
        server = multi_page_scan_server
        job_id = _start_multi_page_scan(page, server, "Focus Drop")
        try:
            _accept_the_confirmation(page)
            page.locator("#mp-abort").focus()
            page.keyboard.press("Enter")
            scan = page.locator("#scan-btn")
            expect(scan).to_be_enabled(timeout=10_000)
            expect(scan).to_be_focused()
            expect(page.locator("#status-area .status-cancelled")).to_be_visible()
        finally:
            _abort_the_document(server, job_id)

    def test_focus_map_non_owner_is_not_autofocused(
        self,
        page: Page,
        browser: Browser,
        flip_server: _BrowserServer,
        egress_allowlist: list[str],
    ) -> None:
        """
        A prompt appearing for somebody else's scan moves no focus of mine.

        The viewer is typing a title while the owner's scan runs.  When the
        scan reaches the flip, the viewer's poll brings the waiting line and
        focus stays in the Title box; the owner, polling at the same moment,
        is moved to Continue.  The viewer's context is built by hand, installs
        the egress gate and the policy recorder itself, and is checked after
        it closes.
        """
        server = flip_server
        job_store: JobStore = server.app.state.job_store
        blocked: list[str] = []
        seen: list[str] = []
        violations: list[str] = []
        page.goto(server.url)
        page.select_option("#profile-select", "duplex")
        page.fill("#title-input", "Not Yours To Flip")
        server.scanner.gate.clear()
        job_id = ""
        viewer_ctx = browser.new_context()
        try:
            with page.expect_response(lambda r: r.url.endswith("/api/scan")):
                page.click("#scan-btn")
            job_id = job_store.list_recent(1)[0].id
            viewer_ctx.route("**/*", _make_gate(blocked, egress_allowlist, seen))
            _make_csp_gate(viewer_ctx, violations)
            viewer = viewer_ctx.new_page()
            viewer.goto(server.url)
            viewer_status = viewer.locator("#status-area")
            expect(viewer_status).to_contain_text(progress_label(JobState.SCANNING))
            title = viewer.locator("#title-input")
            title.focus()
            server.scanner.gate.set()
            wait_for_state(job_store, job_id, JobState.AWAITING_FLIP, timeout=20.0)
            expect(viewer_status).to_contain_text(_WAITING_LINE)
            _await_status_poll(viewer)
            expect(title).to_be_focused()
            assert viewer.evaluate("document.activeElement.id") == "title-input"
            expect(page.locator("#flip-continue")).to_be_focused(timeout=10_000)
        finally:
            viewer_ctx.close()
            server.scanner.gate.set()
            if job_id:
                _end_the_flip(server, job_id)
            assert seen, "the hand-built context's gate handled no request"
            assert blocked == [], f"a page tried to reach the network: {blocked}"
            assert violations == [], (
                f"a page violated its Content-Security-Policy: {violations}"
            )


# Where a heading's text, its refresh button and the control under it sit, as
# rectangles: the heading element is the text's line box, and the range over
# its one text node is the text itself.
_HEADING_LAYOUT = """
([headingSelector, buttonSelector, controlSelector]) => {
    const box = (node) => {
        const rect = node.getBoundingClientRect();
        return {
            top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right,
        };
    };
    const heading = document.querySelector(headingSelector);
    const text = [...heading.childNodes].find(
        (node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim() !== "",
    );
    const range = document.createRange();
    range.selectNodeContents(text);
    return {
        line: box(heading),
        text: box(range),
        button: box(document.querySelector(buttonSelector)),
        control: box(document.querySelector(controlSelector)),
    };
}
"""

# Each refresh button's heading, the button, and the control under both, as
# selectors.
_REFRESH_ROWS = [
    pytest.param(
        ("fieldset:has(#tags-list) > legend", "#tags-refresh", "#tag-filter"),
        id="tags",
    ),
    pytest.param(
        (
            'label[for="correspondent-select"]',
            "#correspondents-refresh",
            "#correspondent-select",
        ),
        id="correspondents",
    ),
]


def _is_tags_request(request: Request) -> bool:
    """Say whether ``request`` is the tag filter's own fetch."""
    return urlsplit(request.url).path == "/api/tags"


@pytest.mark.browser
class TestFormControls:
    """
    The tag and correspondent controls, as a browser and a screen reader see them.

    Each control is announced by its own name and nothing else, the filter
    follows every edit and its own clear button, and each refresh button
    keeps focus and the current choice while it sits beside its heading
    rather than inside it.
    """

    def test_accessible_name_of_tags_and_correspondent(
        self, page: Page, defaults_server: _BrowserServer
    ) -> None:
        """A heading names its control only; the filter has a label of its own."""
        page.goto(defaults_server.url)
        _await_the_lists(page)

        expect(page.locator("fieldset:has(#tags-list)")).to_have_accessible_name("Tags")
        expect(page.locator("#correspondent-select")).to_have_accessible_name(
            "Correspondent"
        )
        expect(page.locator('label[for="tag-filter"]')).to_have_count(1)
        expect(page.locator("#tag-filter")).to_have_accessible_name(TAG_FILTER_LABEL)

    def test_tag_filter_fill_and_search_clear(
        self, page: Page, defaults_server: _BrowserServer
    ) -> None:
        """
        One request per edit, and the box's own clear brings the list back.

        ``fill`` sets the value and fires ``input`` with no key at all, as a
        paste or a dictation does.  The clear is the ``search`` event alone,
        which is what the box's clear button and Escape fire, so the list is
        restored by that trigger and not by an ``input`` riding along.
        """
        requested: list[str] = []

        def _record(request: Request) -> None:
            if _is_tags_request(request):
                requested.append(request.url)

        page.goto(defaults_server.url)
        _await_the_lists(page)
        page.on("request", _record)
        tags_list = page.locator("#tags-list")
        filter_box = page.locator("#tag-filter")

        with page.expect_response(
            lambda r: _is_tags_request(r.request), timeout=5_000
        ) as narrowed:
            filter_box.fill("gar")
        assert narrowed.value.status == 200
        expect(tags_list).to_contain_text("garden")
        expect(tags_list).not_to_contain_text("school")
        assert len(requested) == 1, requested

        with page.expect_response(
            lambda r: _is_tags_request(r.request), timeout=5_000
        ) as cleared:
            filter_box.evaluate(
                "box => { box.value = ''; box.dispatchEvent(new Event('search')); }"
            )
        assert cleared.value.status == 200
        expect(page.locator("label.tag-option")).to_have_count(len(_DEFAULTS_TAGS))
        assert len(requested) == 2, requested

    def test_focus_map_tags_refresh_keeps_focus(
        self, page: Page, defaults_server: _BrowserServer
    ) -> None:
        """Pressing the tags refresh from the keyboard leaves focus on it."""
        page.goto(defaults_server.url)
        _await_the_lists(page)
        button = page.locator("#tags-refresh")
        button.focus()

        with page.expect_response(lambda r: r.url.endswith("resource=tags")) as done:
            page.keyboard.press("Enter")

        assert done.value.status == 200
        expect(page.locator("#tags-list.htmx-added")).to_have_count(0)
        expect(button).to_be_focused()

    def test_focus_map_correspondents_refresh_keeps_focus(
        self, page: Page, defaults_server: _BrowserServer
    ) -> None:
        """Pressing the correspondents refresh from the keyboard leaves focus on it."""
        page.goto(defaults_server.url)
        _await_the_lists(page)
        button = page.locator("#correspondents-refresh")
        button.focus()

        with page.expect_response(
            lambda r: r.url.endswith("resource=correspondents")
        ) as done:
            page.keyboard.press("Enter")

        assert done.value.status == 200
        expect(page.locator("#correspondent-select.htmx-settling")).to_have_count(0)
        expect(button).to_be_focused()

    def test_correspondent_refresh_keeps_the_choice(
        self, page: Page, defaults_server: _BrowserServer
    ) -> None:
        """
        A refresh changes the list, never what is chosen in it.

        The profile opens on 41, so 42 is a choice the operator made.  The
        options are marked before the press and the wait is for the marks to
        be gone, so the value is read from the swapped-in options.  The button
        is found by its accessible name.
        """
        page.goto(defaults_server.url)
        _await_the_lists(page)
        select = page.locator("#correspondent-select")
        select.select_option("42")
        select.locator("option").evaluate_all(
            "options => options.forEach(o => o.setAttribute('data-stale', ''))"
        )

        with page.expect_response(
            lambda r: r.url.endswith("resource=correspondents")
        ) as done:
            page.get_by_role("button", name="Refresh correspondents").click()

        assert done.value.status == 200
        expect(select.locator("option[data-stale]")).to_have_count(0)
        expect(select).to_have_value("42")

    @pytest.mark.parametrize("width", [1280, 320])
    @pytest.mark.parametrize("row", _REFRESH_ROWS)
    def test_refresh_buttons_keep_their_place(
        self,
        page: Page,
        defaults_server: _BrowserServer,
        width: int,
        row: tuple[str, str, str],
    ) -> None:
        """
        Each button sits on its heading's line, right of the text, above the control.

        The buttons sit outside the legend and the label in the markup, yet on
        screen they keep that place, at a desktop width and at the narrowest
        phone.
        """
        page.set_viewport_size({"width": width, "height": 800})
        page.goto(defaults_server.url)
        _await_the_lists(page)

        layout = page.evaluate(_HEADING_LAYOUT, list(row))

        line, text, refresh, below = (
            layout["line"],
            layout["text"],
            layout["button"],
            layout["control"],
        )
        middle = (refresh["top"] + refresh["bottom"]) / 2
        assert line["top"] <= middle <= line["bottom"], layout
        assert refresh["top"] < text["bottom"], layout
        assert refresh["bottom"] > text["top"], layout
        assert refresh["left"] >= text["right"], layout
        assert below["top"] >= max(line["bottom"], refresh["bottom"]), layout


# A title the refusal must keep out of the URL and off the page.
_NO_SCRIPT_TITLE = "Kept-Out-Of-The-URL-5c1d"


def _listless_settings(tmp_dir: Path) -> Settings:
    """
    Build the browser test settings with neither list on the form.

    With JavaScript off the lists never load, so Scan stays held on a form
    that shows one.  A form showing neither has nothing to wait for, so
    Scan can be pressed and the browser posts the form by itself.
    """
    return _browser_test_settings(tmp_dir).model_copy(
        update={"web": WebConfig(show_tags=False, show_correspondent=False)}
    )


@pytest.mark.browser
class TestJavaScriptOff:
    """
    A browser with JavaScript off is told why Scan cannot work, and leaks nothing.

    Each context is built by hand with JavaScript off, installs the egress
    gate and the policy recorder, and is checked after it closes.  With
    scripting off the recorder's init script never runs, so the egress gate
    is the check that means something here.
    """

    def test_javascript_off_scan_lands_on_the_refusal_page(
        self, browser: Browser, tmp_path: Path, egress_allowlist: list[str]
    ) -> None:
        """Scan posts the form, the server refuses it, and no row is written."""
        blocked: list[str] = []
        seen: list[str] = []
        violations: list[str] = []
        with _serve(_listless_settings(tmp_path), _BrowserTestScanner()) as server:
            egress_allowlist.append(server.url)
            job_store: JobStore = server.app.state.job_store
            before = len(job_store.list_recent(limit=1_000))
            ctx = browser.new_context(java_script_enabled=False)
            try:
                ctx.route("**/*", _make_gate(blocked, egress_allowlist, seen))
                _make_csp_gate(ctx, violations)
                page = ctx.new_page()
                page.goto(server.url)
                expect(page.locator("#scan-btn")).to_be_enabled()
                page.fill("#title-input", _NO_SCRIPT_TITLE)

                page.click("#scan-btn")

                expect(page).to_have_title(NO_SCRIPT_PAGE_TITLE)
                expect(
                    page.get_by_role("heading", name=NO_SCRIPT_HEADING)
                ).to_be_visible()
                assert urlsplit(page.url).path == "/api/scan", page.url
                assert _NO_SCRIPT_TITLE not in page.url
                assert _NO_SCRIPT_TITLE not in page.content()
                assert len(job_store.list_recent(limit=1_000)) == before
            finally:
                ctx.close()
        assert seen, "the hand-built context's gate handled no request"
        assert blocked == [], f"a page tried to reach the network: {blocked}"
        assert violations == [], (
            f"a page violated its Content-Security-Policy: {violations}"
        )

    def test_javascript_off_page_explains_itself(
        self,
        browser: Browser,
        browser_server: _BrowserServer,
        egress_allowlist: list[str],
    ) -> None:
        """The line under the heading shows, and Scan is held: the lists never load."""
        blocked: list[str] = []
        seen: list[str] = []
        violations: list[str] = []
        ctx = browser.new_context(java_script_enabled=False)
        try:
            ctx.route("**/*", _make_gate(blocked, egress_allowlist, seen))
            _make_csp_gate(ctx, violations)
            page = ctx.new_page()
            page.goto(browser_server.url)

            # Read by locator and inner text: Playwright's text matching skips
            # the inside of a noscript element, whatever the browser renders.
            line = page.locator("noscript > p.status-fallback")
            expect(line).to_be_visible()
            assert line.inner_text() == f"\u26a0 {NO_SCRIPT_LINE}"
            expect(page.locator("#scan-btn")).to_be_disabled()
        finally:
            ctx.close()
        assert seen, "the hand-built context's gate handled no request"
        assert blocked == [], f"a page tried to reach the network: {blocked}"
        assert violations == [], (
            f"a page violated its Content-Security-Policy: {violations}"
        )
