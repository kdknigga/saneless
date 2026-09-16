"""
Playwright browser tests for web UI rendering verification.

These tests verify that PicoCSS styling, HTMX interactions, and UI
components render correctly in a real browser. They use a session-scoped
uvicorn server with a stub scanner for isolation from real hardware.

PicoCSS and htmx are vendored under ``/static/vendor/`` and ``base.html`` loads
them with an SRI ``integrity`` pin; ``tests/test_vendor_assets.py`` pins those
bytes to the hashes. Every browser test here runs behind an egress gate (the
overridden ``context`` fixture) that aborts and records any request not
addressed to the test server and fails the test if anything was recorded, so
the UI is proven to work with no internet, and the CI ``browser`` job runs this
whole module offline.

A stylesheet whose bytes no longer match its ``integrity`` is refused by the
browser without announcing itself: the contrast checks then report unstyled
black-on-white ratios and the amber checks report a colour mismatch, both of
which read like a palette regression. If a run fails that way in bulk, read
``test_pico_css_applied`` first -- it is the load canary, and it is the one that
says so in plain words.

Requires: pytest-playwright, chromium browser (uv run playwright install chromium)
"""

from __future__ import annotations

import re
import socket
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal, NamedTuple
from urllib.parse import parse_qs

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from fastapi import FastAPI
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Dialog,
        Locator,
        Page,
        Request,
        Response,
        Route,
    )
    from starlette.types import ASGIApp, Receive, Scope, Send

    from saneless.job import Job, JobStore
    from saneless.scanner.base import PageSink, ScanSettings

import httpx
import pytest
import uvicorn
from PIL import Image
from playwright.sync_api import expect

from saneless.checks import CHECKING_MESSAGE, CheckKey
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
    WebConfig,
)
from saneless.job import JobResult
from saneless.paperless import UploadResult
from saneless.scanner.base import DeviceInfo, ScanBatch
from saneless.vocabulary import (
    TERMINAL_STATES,
    ErrorCategory,
    FlipOutcome,
    JobState,
    ScanOutcome,
    WorkerHealth,
    error_message,
    error_next_step,
)
from saneless.web.app import create_app
from saneless.worker import WorkerFlipCoordinator
from tests.conftest import StubScannerBackend, scan_batch

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
"""Pico's ``--pico-del-color`` behind ``.status-error``, per 26-UI-SPEC, as computed."""

_MUTED = {"light": "rgb(100, 107, 121)", "dark": "rgb(123, 132, 149)"}
"""Pico's ``--pico-muted-color`` (#646b79 / #7b8495) behind ``.status-cancelled``."""

_SUCCESS_GREEN = {"light": "rgb(29, 106, 84)", "dark": "rgb(98, 175, 154)"}
"""Pico's ``--pico-ins-color`` behind ``.check-ok``, per 30-UI-SPEC, as computed."""

# The status strip's three verdict colours, each pinned to the token 30-UI-SPEC
# § Color says it reads. The amber is the existing _AMBER rather than a second
# pair of literals: .check-warn and .status-fallback share one custom property
# on purpose, so a drift in either must be reported by both.
_CHECK_STATE_COLOURS = {
    "check-ok": _SUCCESS_GREEN,
    "check-warn": _AMBER,
    "check-fail": _ERROR_RED,
}


_SCAN_GATE_TIMEOUT = 30.0
"""Longest a closed gate holds ``scan_pages``, so a test that forgets it cannot hang."""


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
        [sink.add(page) for page in records],
        resolution=resolution,
    )


class _BrowserTestScanner(StubScannerBackend):
    """
    Concrete scanner stub for browser tests, with a gate on ``scan_pages``.

    The gate is open by default, so a scan returns at once. A test closes it
    (``gate.clear()``) to hold a job in SCANNING while it looks at the page, and
    opens it again (``gate.set()``) to let the job finish. The wait is bounded,
    so a gate left closed by a failing test ends the scan rather than the run.

    Capabilities come from ``StubScannerBackend``, which reports the same
    flatbed at 300 dpi in colour the local copy did. Only ``get_devices``
    differs, because these tests want a device with a recognisable name.
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
            settings: Only ``resolution`` is used, and only to report it back.
            sink: The pipeline's own sink, which receives the page.

        Returns:
            A batch of the single record the sink returned.

        """
        self.gate.wait(timeout=_SCAN_GATE_TIMEOUT)
        return _spool_pages(sink, 1, settings.resolution)


class _BrowserServer(NamedTuple):
    """
    The live test server: its base URL and the app object behind it.

    Yielding only the URL made the server a black box -- a test could look at
    the idle page and nothing else, because there was no way to put a job into
    a given state. Carrying the app alongside the URL is what lets a test drive
    a job to FALLBACK and then look at it through a real browser; carrying the
    scanner is what lets a test hold a real scan in SCANNING through its gate.
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
    startup, and Paperless points at a closed local port so an unstubbed upload
    fails fast instead of reaching the network.
    """
    return Settings(
        scanner=ScannerConfig(device="test:browser:001"),
        paperless=PaperlessConfig(
            url="http://localhost:9999",
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
    """A uvicorn server running on a daemon thread, and the port it bound."""

    server: uvicorn.Server
    thread: threading.Thread
    port: int


def _start_uvicorn(app: ASGIApp, host: str) -> _RunningUvicorn:
    """Run ``app`` under uvicorn on a daemon thread, bound to ``host`` on a free port."""
    # Note: uvicorn.Server.capture_signals already skips signal handling
    # when running in a non-main thread, so no special config is needed.
    config = uvicorn.Config(app, host=host, port=0, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server startup
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    else:
        msg = "Uvicorn server failed to start"
        raise RuntimeError(msg)

    # Get actual assigned port
    port = server.servers[0].sockets[0].getsockname()[1]
    return _RunningUvicorn(server=server, thread=thread, port=port)


def _stop_uvicorn(running: _RunningUvicorn) -> None:
    """Stop a server started by ``_start_uvicorn`` and assert its thread ended."""
    running.server.should_exit = True
    running.thread.join(timeout=5)
    # join() reports nothing on timeout. A uvicorn thread that fails to stop
    # leaves a bound port and a live app behind for the rest of the session,
    # so the outcome is asserted rather than discarded.
    assert not running.thread.is_alive(), "uvicorn test server did not shut down"


@contextmanager
def _serve(
    settings: Settings, scanner: _BrowserTestScanner
) -> Iterator[_BrowserServer]:
    """
    Run a private app on loopback for the length of one test, then shut it down.

    ``blocked_server`` and ``lan_server`` each hand-rolled this; the tests that
    need a *cold* check cache or a scan held mid-flight need it several times
    more, and none of them can use the session server -- its cache is warm
    within a second of the first page load and its scanner is shared with every
    other test in the module.

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


@pytest.fixture(scope="session")
def browser_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_BrowserServer]:
    """Start a real uvicorn server for browser tests."""
    tmp_dir = tmp_path_factory.mktemp("browser")
    scanner = _BrowserTestScanner()
    app = create_app(_browser_test_settings(tmp_dir), scanner)
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


# Pitfall 9, stated in full because this factory exists only to design it out.
#
# The gate used to be a closure written inside the ``context`` fixture below,
# which made it unreachable from anywhere else in the module. A test that needs
# two browser sessions at once has to build its own contexts with
# ``browser.new_context()`` -- the owner/non-owner proof of the flip prompt is
# exactly that shape -- and a hand-made context carries none of the overridden
# fixture's routing. It would therefore have had NO gate at all: its page could
# reach the real internet in CI and nothing in the suite would have noticed,
# because the only ``blocked`` list anybody asserted on belonged to a context
# that test never used. The escape is silent, which is what makes it dangerous.
#
# The rule this factory enforces is therefore twofold, and both halves matter:
# every context created in this module must install a gate built here, and
# every one of them must be covered by a ``blocked`` assertion at teardown. One
# list may be shared by several contexts, which is the point -- a single
# assertion then speaks for all of them.
def _make_gate(blocked: list[str], allowlist: list[str]) -> Callable[[Route], None]:
    """
    Build the no-egress route handler every context in this module installs.

    Args:
        blocked: The list each refused URL is appended to. The caller asserts it
            empty at teardown; sharing one list across contexts is supported and
            is how a multi-context test keeps a single assertion honest.
        allowlist: The base URLs a page may reach. Read when each request
            arrives rather than captured, so a base appended after the gate is
            installed still counts.

    Returns:
        A handler suitable for ``context.route("**/*", ...)``.

    """

    def _gate(route: Route) -> None:
        url = route.request.url
        if _is_allowed(url, allowlist):
            route.continue_()
        else:
            blocked.append(url)
            route.abort()

    return _gate


@pytest.fixture
def context(
    context: BrowserContext, egress_allowlist: list[str]
) -> Iterator[BrowserContext]:
    """
    Route every request the page makes through a no-egress gate.

    This overrides pytest-playwright's ``context`` fixture, so every ``page`` in
    this module -- the DARK-01/DARK-02 tests included -- is built from a context
    that continues requests addressed to the test server and aborts and records
    everything else. The test then fails if anything was recorded. That proves
    the UI needs no internet (ROBU-09) rather than assuming it from the network
    the run happens to have, and it is what lets every browser test run offline
    in the CI ``browser`` job (ROBU-11).

    The gate itself comes from ``_make_gate`` rather than being written here, so
    a hand-made context can install the identical one; see that factory's note.
    """
    blocked: list[str] = []
    context.route("**/*", _make_gate(blocked, egress_allowlist))
    yield context
    assert blocked == [], f"the page tried to reach the network: {blocked}"


@pytest.fixture
def delivering_paperless(
    browser_server: _BrowserServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Make the live app's Paperless client deliver every upload at once.

    The worker holds the same client instance as ``app.state.paperless``, so
    patching its methods affects real scans. Without this the upload goes to
    ``localhost:9999``, spends about 3 s in retry backoff and ends ERROR, which is
    neither the outcome under test nor fast enough to wait for. ``poll_task``
    returning None is what success means since plan 23-04 (it raises on
    failure). ``monkeypatch`` restores both methods at teardown.
    """
    _make_paperless_deliver(browser_server.app, monkeypatch)


def _make_paperless_deliver(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``app``'s started Paperless client so every upload is delivered at once."""
    paperless = app.state.paperless

    def _upload_document(*_args: object, **_kwargs: object) -> UploadResult:
        return UploadResult(delivered_to_api=True, task_uuid="browser-test-task")

    def _poll_task(*_args: object, **_kwargs: object) -> None:
        return None

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
    wait_for_state: Callable[..., Job],
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


@pytest.mark.browser
class TestBrowserRendering:
    """PicoCSS and semantic HTML rendering tests."""

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
        # so its existence proves nothing. Neither does "x > 0": measured with
        # the CDN blocked, <main> is full-bleed inside <body>'s default 8px
        # margin -- x=8, width=1264 at a 1280px viewport -- so a bare x > 0
        # passes unstyled as well. Pico's .container caps the width and centres
        # what is left (x=40, width=1200), so it is the *cap* that tells the two
        # states apart. Both numbers were measured against a blocked CDN.
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
class TestHTMXPolling:
    """HTMX live status polling tests."""

    def test_status_area_exists(self, page: Page, browser_server_url: str) -> None:
        """Status area element exists on the page."""
        page.goto(browser_server_url)
        # Status area should be present (div#status-area)
        status = page.locator(
            "[hx-get*='status'], [data-hx-get*='status'], #status-area"
        )
        assert status.count() >= 1

    def test_htmx_loaded(self, page: Page, browser_server_url: str) -> None:
        """HTMX library is loaded and active on the page."""
        page.goto(browser_server_url)
        # Check htmx is available in the global scope
        htmx_loaded = page.evaluate("typeof htmx !== 'undefined'")
        assert htmx_loaded is True


@pytest.mark.browser
class TestOfflinePage:
    """
    The page structure phase 26 promises, read from the live DOM.

    Each of these is a property of what the browser actually loaded and built:
    which htmx ran and with which config, whether a deleted script is still
    fetched, and how an empty alert region lays out. A template string test sees
    the markup, not the result.
    """

    def test_vendored_htmx_is_the_pinned_version_with_error_swapping(
        self, page: Page, browser_server_url: str
    ) -> None:
        """
        The vendored htmx 2.0.8 runs with all three response rules (B2, ROBU-09).

        htmx merges the meta config shallowly, so a config holding only the
        ``[45]..`` entry would replace the whole array and stop every 2xx swap;
        the count of three and the error entry's ``swap`` are both asserted. No
        ``data-theme`` and no ``pico.colors`` stylesheet keep the 23.1 dark-mode
        coupling intact.
        """
        page.goto(browser_server_url)
        assert page.evaluate("htmx.version") == "2.0.8"
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
        The deleted app script is neither referenced nor served (B3, ROBU-04).

        A stale ``<script>`` tag pointing at a 404 would still load nothing, and
        a stale file still served would let a cached page run the old button
        logic, so both halves are checked.
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
        The request-error slot is one empty alert region taking no space (B4, D-03).

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

    def test_title_input_caps_at_256(self, page: Page, browser_server_url: str) -> None:
        """
        The title input stops at the server's 256-character cap (B14, ROBU-08).

        The server's 422 stays authoritative; this is what keeps a browser user
        from ever meeting it.
        """
        page.goto(browser_server_url)
        title_input = page.locator("#title-input")
        assert title_input.get_attribute("maxlength") == "256"
        title_input.fill("x" * 300)
        assert len(title_input.input_value()) == 256


@pytest.mark.browser
class TestFlipPromptUI:
    """Flip prompt rendering tests."""

    def test_flip_prompt_not_visible_on_idle(
        self, page: Page, browser_server_url: str
    ) -> None:
        """Flip prompt is not visible when no job is in AWAITING_FLIP state."""
        page.goto(browser_server_url)
        # On idle, the flip prompt area should not show Continue/Cancel
        flip_continue = page.locator(
            "button:has-text('Continue'), [hx-post*='flip/continue']"
        )
        # Either not present or not visible
        if flip_continue.count() > 0:
            assert not flip_continue.first.is_visible()

    def test_scan_button_present(self, page: Page, browser_server_url: str) -> None:
        """Scan button exists and is visible in idle state."""
        page.goto(browser_server_url)
        scan_btn = page.locator("button[type='submit'], input[type='submit']").first
        assert scan_btn.is_visible()

    def test_flip_continue_click_answers_the_waiting_job(
        self, page: Page, browser_server: _BrowserServer
    ) -> None:
        """
        A real Continue click answers its own job and is acknowledged (CR-01).

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

# Counts the two in-form controls that load themselves on page load. htmx 2.0.8
# strips hx-disabled-elt's `disabled` inside the request's onload handler, after
# the swap and before htmx:afterSettle, so once both events have fired the
# inheritance trap has either sprung or it has not. Registered as an init script
# so the listener exists before htmx issues the load requests.
#
# afterSettle, not afterRequest: the tag list is swapped outerHTML, and htmx
# fires afterRequest on the element it requested *after* that swap has already
# detached it, so the event never reaches this document-level listener. The
# settle pass runs on the elements that are now in the document, on htmx's
# default 20 ms settle delay -- which is later still, so it remains a sound
# "the trap has had its chance" signal. The correspondent select is swapped
# innerHTML and settles as itself; the new tag list settles under the same id
# the old one carried, because the partial renders its own wrapper.
_RECORD_LOAD_REQUESTS = """
window.__selectLoadsFinished = 0;
document.addEventListener("htmx:afterSettle", (event) => {
    const id = event.detail.elt && event.detail.elt.id;
    if (id === "tags-list" || id === "correspondent-select") {
        window.__selectLoadsFinished += 1;
    }
});
"""


@pytest.mark.browser
class TestServerOwnedScanButton:
    """
    The Scan button through a real scan, in Chromium (ROBU-04, ROBU-11).

    C-10 shipped because its fix was proven by reading: the button's state had
    two owners, the server's template and a client script, and they disagreed.
    The script is gone and the server re-renders the button out of band with
    every status response. These tests drive real scans through the live worker
    and watch the button the user sees.
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
        Click Scan, reach DONE, and the button is usable again (B5, ROBU-11, C-10).

        This is roadmap success criterion 5 as a test: clicking Scan in a real
        browser, waiting for the terminal status, and finding the button enabled
        again -- with no app script on the page, and exactly one button, so the
        release is the server's out-of-band render and not a duplicate.
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
        While a scan is in flight the button is disabled and busy (B6, ROBU-04).

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

    def test_page_loaded_during_an_active_job_keeps_the_button_disabled(
        self,
        page: Page,
        scan_harness: _ScanHarness,
        wait_for_state: Callable[..., Job],
    ) -> None:
        """
        A page opened mid-scan keeps Scan disabled after its selects load (B7).

        The form carries ``hx-disabled-elt="#scan-btn"``. Without
        ``hx-disinherit`` the tags and correspondents selects inherit it, and on
        htmx 2.0.8 finishing their load requests strips ``disabled`` from the
        button the server rendered disabled -- C-10 again, on every page load
        during a scan (T-26-48).
        """
        server = scan_harness.server
        server.scanner.gate.clear()
        try:
            response = httpx.post(server.url + "/api/scan", data={"profile": "default"})
            assert response.status_code == 200, response.text
            created = scan_harness.created_job_ids()
            assert len(created) == 1, created
            wait_for_state(scan_harness.job_store, created[0], JobState.SCANNING)

            page.add_init_script(_RECORD_LOAD_REQUESTS)
            with (
                page.expect_response(lambda r: "/api/tags" in r.url),
                page.expect_response(lambda r: "/api/correspondents" in r.url),
            ):
                page.goto(server.url)
            page.wait_for_function("window.__selectLoadsFinished >= 2")

            # Read once, without retrying. expect(...).to_be_disabled() polls for
            # up to 5 s, and the status poll re-renders the button disabled
            # every second, so a retrying check waits out the trap and passes
            # with it sprung -- observed with hx-disinherit removed.
            assert page.locator("#scan-btn").is_disabled(), (
                "a page load during an active scan re-enabled the Scan button"
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
        // card are the same colour, so the first one serves for both. (It used
        // to be the Scan card; #checks-card now precedes it.)
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
    The contrast helper, pinned to the numbers the UI-SPEC measured.

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
        """Light amber, dark amber, and the dark-surface failure this phase fixes."""
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
        # This is Pico's striped-row colour. It is unreachable today --
        # history.html uses role="grid", which Pico 2.1.1 does not stripe -- but
        # adding class="striped" is a one-attribute change, and reading it as
        # opaque would report a passing ratio as ~1.1:1.
        stripe = "rgba(111, 120, 135, 0.0375)"
        flattened = _flatten([stripe, _PICO_SURFACE["light"]])
        assert _relative_luminance(flattened) > 0.9
        # The same colour read as opaque, which is what the bug did.
        assert _relative_luminance(stripe) < 0.3

    def test_a_colour_the_parser_cannot_read_is_named(self) -> None:
        """An unreadable colour asserts with the string, not a slice ValueError."""
        with pytest.raises(AssertionError, match=r"color\(srgb"):
            _relative_luminance("color(srgb 1 1 1)")


@pytest.mark.browser
class TestFallbackStatusRendering:
    """
    The FALLBACK status render, proven in a browser rather than by grep.

    Three of this phase's claims are only checkable here: that the amber is a
    different colour from the success green and the failure red once the
    cascade has resolved (D-05), that the Scan button recovers after a fallback
    swap (T-23-23), and that the history table refreshes (T-23-24). A template
    assertion cannot see any of them -- the first is a cascade outcome, and the
    other two depend on JavaScript and htmx actually running.
    """

    @pytest.fixture
    def fallback_page(
        self, page: Page, browser_server: _BrowserServer
    ) -> Iterator[Page]:
        """Drive the live app's current job to FALLBACK, then clear it again."""
        app = browser_server.app
        job_store: JobStore = app.state.job_store
        job = job_store.create_job(profile="default", title="Fallback Doc")
        # finish_job is the public writer for the warning column, and the worker
        # already reaches FALLBACK through it. This used to UPDATE the column
        # through job_store._conn, on a comment saying no warning writer landed
        # until plan 23-07 -- which it since has.
        #
        # The workaround was also unsafe by the store's own rules: every public
        # writer is wrapped in @_locked because the web and worker threads share
        # one connection opened with check_same_thread=False, and sqlite3
        # connection context managers do not nest -- an inner `with conn:`
        # commits the outer transaction. Opening one from the test thread while
        # holding no lock could commit another thread's in-flight work. Benign
        # only because no scan runs during a browser test.
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
            # has to undo both halves. Clearing the pointer alone did not:
            # index() and current_job_status() each fall back to
            # list_recent(limit=1) when there is no current job, and the most
            # recent job was the one this fixture had just created. The FALLBACK
            # page therefore followed every later test onto what was supposed to
            # be the idle page, and the jobs accumulated across the session.
            # Deleting the row is what actually restores the idle state.
            app.state.worker._current_job_id = None
            job_store.delete_job(job.id)

    def _goto(self, page: Page, url: str, scheme: Literal["light", "dark"]) -> None:
        """Load the page under an emulated OS colour-scheme preference."""
        page.emulate_media(color_scheme=scheme)
        page.goto(url)
        page.wait_for_selector("#status-area .status-fallback")

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

        This is the mechanical proof of D-05's "visually distinct from both",
        and it is run under both colour schemes because a token that is legible
        in one and invisible in the other is not distinct at all.
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
        A fallback swap releases a stale disabled Scan button (T-23-23, ROBU-04).

        A FALLBACK is a finished scan, so a button still disabled when it lands
        would look like a locked-up application. What releases it now is the
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
        The hidden reload div in the FALLBACK branch actually fires (T-23-24).

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


_POLL_OBSERVATION_MS = 2500
"""How long a terminal page is watched for status polls: two and a half 1 s ticks."""


@pytest.mark.browser
class TestCancelledStatusRendering:
    """
    The CANCELLED status render, proven in a browser (D-01, EXC-04).

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
        job = job_store.create_job(profile="default", title="Cancelled Doc")
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
        page.wait_for_selector("#status-area .status-cancelled")

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
        A CANCELLED current job leaves the page idle-ready (D-01: terminal).

        The Scan button is enabled with no busy marker, the status area carries
        no polling trigger, and -- the behaviour the attribute stands for -- no
        status request goes out across more than two poll intervals.
        """
        polls: list[str] = []
        cancelled_page.on(
            "request",
            lambda request: (
                polls.append(request.url)
                if "/api/jobs/current/status" in request.url
                else None
            ),
        )
        self._goto(cancelled_page, browser_server.url, "light")

        scan_btn = cancelled_page.locator("#scan-btn")
        assert scan_btn.is_enabled()
        assert scan_btn.get_attribute("aria-busy") is None
        assert (scan_btn.text_content() or "").strip() == "Scan"
        assert (
            cancelled_page.locator("#status-area").get_attribute("hx-trigger") is None
        )

        cancelled_page.wait_for_timeout(_POLL_OBSERVATION_MS)
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
        # instead. This is true only because fallback_page deletes its job on
        # teardown; while that row survived, every test in this class was in
        # fact looking at a FALLBACK page.
        page.wait_for_selector("#history-body")

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_root_surface_follows_the_os_scheme(
        self,
        page: Page,
        browser_server_url: str,
        scheme: Literal["light", "dark"],
    ) -> None:
        """The root element paints Pico's surface for the OS scheme (T1)."""
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
            # Phase 30's counts line renders in the status area and in the
            # history Title cell alike (UI-SPEC S3), so it belongs in exactly
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
        Every status colour reaches WCAG AA where it is really shown (T2).

        The card placement is the margin UI-SPEC records for a status line
        inside an ``<article>``; the dark card is lighter than the dark page,
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

    def test_forced_dark_theme_gives_cancelled_the_dark_muted_colour(
        self, page: Page, browser_server_url: str
    ) -> None:
        """
        A forced dark theme gets Pico's dark muted grey under a light OS (D-01).

        ``.status-cancelled`` reads Pico's own token rather than an app-owned
        pair, so this is the proof that Pico's ``[data-theme="dark"]`` block
        reaches it -- and that it still clears AA on the dark card there.
        """
        self._goto(page, browser_server_url, "light")
        page.evaluate("() => { document.documentElement.dataset.theme = 'dark'; }")
        colours = page.evaluate(_PROBE_STATUS_COLOURS)
        assert colours["status-cancelled"] == _MUTED["dark"], colours
        probe = page.evaluate(
            _PROBE_CONTEXT_CONTRAST, {"cls": "status-cancelled", "context": "card"}
        )
        background = _flatten(probe["backgroundStack"])
        ratio = _contrast_ratio(probe["colour"], background)
        assert ratio >= 4.5, (probe, background, ratio)

    def test_forced_dark_theme_keeps_the_dark_amber(
        self, page: Page, browser_server_url: str
    ) -> None:
        """
        A forced dark theme gets the dark amber even under a light OS (T3).

        The automatic-dark tests cannot reach the forced-dark rule, because the
        OS preference alone never sets data-theme. This sets it directly, the
        way a future theme toggle would, and checks the amber follows.
        """
        self._goto(page, browser_server_url, "light")
        page.evaluate("() => { document.documentElement.dataset.theme = 'dark'; }")
        colours = page.evaluate(_PROBE_STATUS_COLOURS)
        assert colours["status-fallback"] == _AMBER["dark"], colours


_QUEUE_FULL_TEXT = (
    "✗ The scan queue is full. Wait for a scan to finish, then try again."
)
"""The slot's exact text for a 429: the error partial's cross plus the S3 copy."""

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

Phase 30 put a collapsed "Technical details" disclosure beside the message
(APPL-04, UI-SPEC S2), so the slot's own text is no longer only the message and
its paragraphs are no longer only one.  Everything asserting what the user reads
-- the exact text, the red, the left edge, the wrapped line count -- names this
paragraph, so a later change to the disclosure cannot be measured by mistake.
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

# The status poll names the job this browser submitted once it has one, and
# the current-job path only until then (D-25), so a poll is recognised by the
# shape of its URL rather than by one literal path.
_POLL_URL = re.compile(r"/api/jobs/[^/]+/status$")


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
        response = httpx.post(url + "/api/scan", data={"profile": "default"})
        statuses.append(response.status_code)
        if response.status_code == _TOO_MANY_REQUESTS:
            return
    pytest.fail(f"the queue never refused a submit: {statuses}")


@pytest.mark.browser
class TestRequestErrorSlot:
    """
    Request errors are seen where 26-UI-SPEC S2 puts them (ROBU-02, D-02..D-06).

    A 429 that is returned but never shown is the failure this class exists for:
    the TestClient tests prove the response, and only a browser proves that htmx
    retargets it into ``#status-message``, that the text is legible and aligned,
    that polling does not erase it, and that a successful scan does. The Scan
    button is disabled while a job is active, so the queue is filled over HTTP
    after an idle page has loaded (B8).
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
        A 429 lands in the slot, as announced text, and in Job History (B8).

        Covers ROBU-02's visible message, D-02 (errors go to the slot, not the
        status area), D-05 (the rejected attempt is recorded) and the UI-SPEC
        accessibility contract: the slot itself is the alert, so the message
        must not carry a second ``role="alert"``, and focus stays where the user
        left it.
        """
        page = queue_full_page()
        slot = page.locator("#status-message")
        expect(slot.locator("p.status-error")).to_have_count(1)
        expect(slot.locator('[role="alert"]')).to_have_count(0)
        expect(page.locator("#status-area")).to_have_count(1)
        expect(page.locator("#scan-btn")).to_be_enabled()
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
        The slot message is the error red at WCAG AA in both schemes (B9).

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
        The slot's text starts at the same x as the status text (B12).

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
        The disclosure starts at the same x as the message it belongs to (B12).

        ``app.css`` insets the slot's children by the status area's own border
        and padding so the slot and the status area share a left edge. Phase 30
        made the message a sibling rather than an only child, so the inset has
        to cover the disclosure too, or "Technical details" hangs a rem to the
        left of the sentence it explains.
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
        The longest slot message wraps at 375 px with no horizontal overflow (B13).

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
        An error shown mid-scan outlives at least two status polls (B10, D-03).

        The poll re-renders ``#status-area`` every second while a job is active.
        If a poll response carried the slot clear that a successful scan
        carries, the message would vanish within a second -- before a user
        could read it. The submitted profile is also checked not to be echoed.
        """
        server = scan_harness.server
        polls: list[str] = []

        def _record_poll(response: Response) -> None:
            if _POLL_URL.search(response.url):
                polls.append(response.url)

        page.on("response", _record_poll)
        server.scanner.gate.clear()
        page.goto(server.url)
        page.locator("#scan-btn").click()
        expect(page.locator('#status-area p[aria-busy="true"]')).to_be_visible()

        page.evaluate(_SUBMIT_UNKNOWN_PROFILE)

        slot = page.locator("#status-message")
        expect(slot).to_contain_text(_UNKNOWN_PROFILE_TEXT)
        assert "zz-nonexistent-profile" not in slot.inner_text()
        polls_before = len(polls)
        page.wait_for_timeout(2500)
        polls_during = len(polls) - polls_before
        assert polls_during >= 2, f"only {polls_during} polls arrived in 2.5 s"
        # Read once, without retrying: the claim is that the message is there
        # now, after the polls, not that it can be found again within a timeout.
        assert _UNKNOWN_PROFILE_TEXT in slot.inner_text(), "a poll erased the error"
        assert page.locator("#status-area").count() == 1

    def test_successful_scan_clears_the_error(
        self, page: Page, scan_harness: _ScanHarness
    ) -> None:
        """
        A successful scan empties the slot back to zero height (B11, D-03).

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

        page.wait_for_function(
            "document.getElementById('status-message').childNodes.length === 0"
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
    server receives them (measured: a routed 127.0.0.1 page reports none while
    the server gets ``same-origin``). So the headers are read where the guard
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
    this one test, with a stub scanner and a stubbed Paperless (T-26-56).
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
    The cross-site guard lets the app's own page scan from a LAN address (D-20).

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
        A same-origin Scan click from ``http://<lan-ip>`` is accepted (ROBU-10).

        Research assumption A6: the Fetch Metadata spec adds ``Sec-Fetch-*``
        only for potentially trustworthy URLs, so Chromium omits it on a plain
        HTTP LAN origin and the guard decides on ``Origin`` against ``Host``
        (D-20 branch 2). ``localhost`` and ``127.0.0.1`` cannot prove this: they
        are secure contexts, always get ``Sec-Fetch-Site``, and exercise branch
        1 instead. The TestClient branch-2 tests in ``tests/test_cross_origin.py``
        prove the rule; this proves the browser really takes that branch.

        The headers are checked as the server received them, not through
        Playwright's request object, which hides ``Sec-Fetch-*`` under routing
        (see ``_ScanHeaderRecorder``).
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
            f"{lan_url}, so this test no longer exercises D-20 branch 2: the "
            "runner's address is being treated as potentially trustworthy"
        )
        assert headers.get("origin") == lan_url, headers
        expect(page.locator("#status-message")).to_be_empty()
        expect(page.locator("#status-area .status-done")).to_be_visible(timeout=15_000)


# The shipped stand-in `config.is_placeholder_token` refuses (D-14). A second
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
    below, and the private one plan 30-19 uses for the claims that write a job
    row. A second copy of this ``model_copy`` would be a second place for the
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
    a real browser in front of the blocked page (UI-SPEC S8).
    """
    tmp_dir = tmp_path_factory.mktemp("browser-blocked")
    scanner = _BrowserTestScanner()
    app = create_app(_blocked_settings(tmp_dir), scanner)
    running = _start_uvicorn(app, host="127.0.0.1")
    yield _BrowserServer(
        url=f"http://127.0.0.1:{running.port}", app=app, scanner=scanner
    )
    _stop_uvicorn(running)


@pytest.mark.browser
class TestBlockedScanButtonInABrowser:
    """
    The blocked Scan button and its reason line, in Chromium (APPL-07, D-15).

    Two of these claims cannot be made from rendered markup. Whether the
    ``disabled`` attribute survives the page's own load requests is the C-10
    inheritance trap, which only a browser running htmx can spring; and whether
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

    def test_the_blocked_button_survives_its_own_page_load_requests(
        self,
        page: Page,
        blocked_server: _BrowserServer,
        egress_allowlist: list[str],
    ) -> None:
        """
        The load requests inside the form do not re-enable it (C-10, T-30-66).

        On htmx 2.0.8 an inherited ``hx-disabled-elt`` strips ``disabled`` from
        a server-disabled button the moment a child request finishes. With no
        job active there is no one-second status poll to put it back, so a
        blocked button that lost the attribute here would stay clickable --
        which is exactly why the flag lives in the one button partial.
        """
        egress_allowlist.append(blocked_server.url)
        page.add_init_script(_RECORD_LOAD_REQUESTS)
        with (
            page.expect_response(lambda r: "/api/tags" in r.url),
            page.expect_response(lambda r: "/api/correspondents" in r.url),
        ):
            page.goto(blocked_server.url)
        page.wait_for_function("window.__selectLoadsFinished >= 2")

        # Read once, without retrying: nothing re-renders this button while no
        # job is active, so a retrying check would only hide a sprung trap
        # behind a timeout.
        assert page.locator("#scan-btn").is_disabled(), (
            "the page's own load requests re-enabled a blocked Scan button"
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
        """The courtesy is absent when there is nothing to be courteous about."""
        page.goto(browser_server.url)

        assert page.locator(_BLOCKED_REASON_SELECTOR).count() == 0
        assert page.locator("#scan-btn").get_attribute("aria-describedby") is None
        # The other half of the same flag, asserted here so the blocked case
        # below is a difference and not just a presence (plan 30-19, P16).
        expect(page.locator("#scan-btn")).to_be_enabled()


_OWNER_COOKIE_NAME = "saneless_owner"

# Playwright reports a session cookie -- one with neither Max-Age nor Expires --
# with this expiry. It is the only way to tell a session cookie from a
# persistent one through the cookie jar, and D-23 turns on the difference.
_SESSION_COOKIE_EXPIRY = -1


@pytest.mark.browser
class TestOwnerCookieInABrowser:
    """
    The owner cookie's behaviour in Chromium, measured rather than assumed.

    RESEARCH carried assumption A6: that a browser processes ``Set-Cookie`` on
    an htmx XHR response exactly as it does on a navigation. If that were
    false the token would never persist and the whole gate would be decorative,
    so it is asserted here instead of reasoned about (APPL-09, D-23).
    """

    def test_scan_submit_sets_a_session_owner_cookie(
        self, page: Page, scan_harness: _ScanHarness
    ) -> None:
        """A real htmx submit leaves an HttpOnly, Lax, session cookie behind."""
        server = scan_harness.server
        server.scanner.gate.clear()
        page.goto(server.url)

        page.locator("#scan-btn").click()
        expect(page.locator('#status-area p[aria-busy="true"]')).to_be_visible()

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
        assert cookie["expires"] == _SESSION_COOKIE_EXPIRY
        assert cookie["secure"] is False
        # HttpOnly proved from inside the page, not from the header: this is
        # the claim that the token cannot reach a script (T-30-59), and there
        # is no script file that could read it in the first place.
        assert _OWNER_COOKIE_NAME not in page.evaluate("() => document.cookie")

    def test_a_non_owning_browser_gets_no_flip_buttons_in_the_dom(
        self, page: Page, browser_server: _BrowserServer
    ) -> None:
        """
        The gate leaves the buttons out of the DOM; it does not hide them.

        A CSS-hidden control is still in the page and still reachable from the
        console, which would be an ASVS V4 failure. This asserts absence in a
        real browser rather than absence from a string (D-24).
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
            expect(status).to_contain_text("Waiting for the stack to be flipped")
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
    """The sentence under the select follows the selection, live (D-20, S4)."""

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
        One help line under Profile, and it is the live one (APPL-10).

        The adjacent-sibling selector is Pico's own help-text rule, so this
        asserts the styling hook and the "no second help line" contract at
        once: if anything were inserted between the control and the slot, the
        muted styling would go with it.
        """
        page.goto(browser_server_url)

        helps = page.locator("#profile-select + small")

        expect(helps).to_have_count(1)
        expect(helps).to_have_id("profile-description")


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
    paperless.get_tags = lambda: list(_BROWSER_TAGS)
    browser_server.app.state.cache.invalidate("tags")
    try:
        yield browser_server
    finally:
        paperless.get_tags = original
        browser_server.app.state.cache.invalidate("tags")


@pytest.mark.browser
class TestTagFilterInChromium:
    """
    The tag picker's two designed-out hazards, proven in a real browser.

    Neither is reachable from a server-side test. A-5 is about what the DOM
    holds after an htmx swap, and A-6 is about which form a browser considers
    an input to belong to -- the HTML form-owner association, which only a
    browser implements.
    """

    def _load_tags(self, page: Page, url: str) -> None:
        """
        Open the page and wait for the tag list to have loaded itself.

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
        A tick the filter excludes stays in the DOM, above the list (A-5).

        This is the hazard the research named. A swap that re-rendered only the
        matches would take an already-chosen tag out of the document, and a tag
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
        The filtered-out tick rides along and the filter text does not (A-5, A-6).

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
        Enter performs implicit submission of the filter's own form (A-6).

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
        Every row is at least 44 px tall and spans the list (D-30, WCAG 2.5.5).

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
# The status strip, in Chromium.
#
# Per CLAUDE.md nothing in this phase's browser contract is deferred to a
# person. Every row of 30-UI-SPEC's Verification Contract that names a browser
# is automated -- here and in the classes below -- and none of them is marked
# for a human to look at. The one out-of-suite item is scanner reachability
# against physical hardware, which cannot be stubbed and which 30-VALIDATION.md
# already records as the hardware-only check it is.
# ---------------------------------------------------------------------------

_PAUSED_PREFIX = "Paused during scan \N{EM DASH} "
"""The freshness line's prefix while a scan holds the scanner (D-08, UI-SPEC S1)."""

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


@pytest.fixture
def cold_strip_server(
    tmp_path: Path, egress_allowlist: list[str]
) -> Iterator[_BrowserServer]:
    """
    Serve a private app whose check cache is cold and stays cold until asked.

    The background refresher is stopped rather than raced. Left running it
    fills the cache on its first tick, so the cold window would be "however
    long a tick takes" and the cold-start assertions would be racing a
    stopwatch -- the flake T-30-84 exists to design out. Stopped, the strip
    stays cold until the test itself asks for a probe through
    ``POST /api/checks/refresh``, which is the app's own probe-and-store path
    and not a reimplementation of it.
    """
    with _serve(_browser_test_settings(tmp_path), _BrowserTestScanner()) as server:
        assert server.app.state.refresher.stop(), "the refresher thread did not stop"
        egress_allowlist.append(server.url)
        yield server


def _probe_now(server: _BrowserServer) -> None:
    """
    Make the appliance probe and store its checks, from outside the browser.

    ``POST /api/checks/refresh`` is the "Check again" route: it takes the
    scanner gate without blocking, sets ``skip_scanner`` from the outcome, runs
    the probes and stores them. Calling it over HTTP rather than clicking the
    button leaves the *page* untouched, so what discovers the new results is
    the strip's own poll -- which is the half of the cold-start contract under
    test. Neither ``Origin`` nor ``Sec-Fetch-Site`` is sent, which is the
    non-browser branch ``CrossOriginGuard`` allows by design.

    Args:
        server: The private server to probe.

    """
    response = httpx.post(f"{server.url}/api/checks/refresh", timeout=30.0)
    assert response.status_code == 200, response.status_code


@pytest.mark.browser
class TestStatusStripInChromium:
    """
    The health strip, read from a real page (APPL-02, D-06, D-08).

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
        ``#checks-card`` is the first card, and the alert slot is untouched (P1).

        "At a glance" is the phase goal and at a glance means first, so the
        ordinal is asserted rather than merely the presence. The second half is
        phase 26's invariant: ``#status-message`` is the immediate element
        sibling above ``#status-area``, and inserting a card above the form must
        not have moved either of them.
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
            == "status-area"
        )

    def test_a_cold_strip_polls_itself_until_results_land_and_then_stops(
        self, page: Page, cold_strip_server: _BrowserServer
    ) -> None:
        """
        The cold body carries the 2 s poll; the body that replaces it does not (P2).

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
        The three verdict colours are legible where the strip really is (P3).

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

    @pytest.mark.parametrize("cls", ["check-ok", "check-warn", "check-fail"])
    def test_forced_dark_theme_gives_the_check_states_their_dark_colours(
        self,
        page: Page,
        browser_server_url: str,
        cls: Literal["check-ok", "check-warn", "check-fail"],
    ) -> None:
        """
        A forced ``data-theme`` reaches all three, under a light OS (P3).

        The OS preference alone never sets ``data-theme``, so the emulated case
        above cannot exercise the forced-dark rule at all. One of the three
        reads an app-owned property and two read Pico's own tokens, and this is
        what proves the forced-dark block reaches every one of them -- and that
        the dark colour still clears AA on the dark card.
        """
        page.emulate_media(color_scheme="light")
        page.goto(browser_server_url)
        page.evaluate("() => { document.documentElement.dataset.theme = 'dark'; }")

        probe = page.evaluate(_PROBE_CONTEXT_CONTRAST, {"cls": cls, "context": "card"})
        colour = probe["colour"]
        background = _flatten(probe["backgroundStack"])
        ratio = _contrast_ratio(colour, background)
        assert colour == _CHECK_STATE_COLOURS[cls]["dark"], (colour, background)
        assert ratio >= 4.5, (colour, background, ratio)

    def test_a_scan_in_flight_pauses_the_scanner_row_and_says_so(
        self, page: Page, cold_strip_server: _BrowserServer
    ) -> None:
        """
        The Scanner row is skipped and the freshness line says why (P4, D-08).

        The two inputs are set to exactly what a live scan produces -- a current
        job on the worker, and the scanner gate held -- rather than by running
        one. What a gate-holding worker does is ``tests/test_worker.py``'s
        subject and the skip decision is ``tests/test_refresher.py``'s; what is
        only provable here is that the pair of them renders as a paused strip
        rather than a stale or a blank one, which is the whole of D-08.
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

        Plan 30-17 drove this same route over HTTP on purpose, so that the swap
        its cold-start test observed was the *poll's*; nobody had yet pressed
        the button in a browser. The claim here is the opposite of P8's: the
        description slot must survive its swap, and this body must not -- the
        button replaces the element it targets, trigger attribute and all, and
        the witness set from the test is what tells replacement from update.

        The checks are probed before the page opens so the body arrives with no
        poll trigger on it. That is what makes the click the only thing in the
        run that can swap this element, and it is why the assertion below needs
        no interval arithmetic.
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

    def test_the_strip_fits_a_320px_phone_without_a_sideways_scrollbar(
        self, page: Page, cold_strip_server: _BrowserServer
    ) -> None:
        """
        At 320 px the rows wrap instead of widening the page (UI-SPEC S1).

        320 px is the narrowest viewport the spec names and the narrowest this
        module has ever measured -- every other responsive assertion here stops
        at 375. The design is a wrapping flex row with a fixed 1 rem glyph
        gutter and a 7 rem name column, so what has to be proved is that those
        two fixed columns plus a message do not push the document wider than
        the viewport, and that the message wrapped rather than overflowed.

        The document width is the load-bearing assertion: a household member
        who has to scroll sideways to read a health verdict has not been told
        anything at a glance.
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

        # The fixed gutter and name column still hold at 320 px, so the five
        # messages still start at one x. A layout that "fitted" by letting the
        # name column collapse per row would clear the overflow check above and
        # be unreadable.
        assert len(set(name_columns)) == 1, name_columns

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


# The counts UI-SPEC S3 pins, for the three cases that behave differently: a
# measured set, a measured zero in the middle clause, and never-recorded.
_MEASURED_COUNTS = "12 pages scanned, 2 blank removed, 10 uploaded"
_ZERO_BLANK_COUNTS = "12 pages scanned, 0 blank removed, 12 uploaded"

# The category this phase's error rendering is read through. UPLOAD is chosen
# over UNKNOWN on purpose: UNKNOWN's next step ends "check the saneless log",
# and a test whose own fixture data contains the word the D-13 assertion is
# hunting for would be arguing with itself.
_ERROR_CATEGORY = ErrorCategory.UPLOAD

_ERROR_DETAIL = "connect to paperless-ngx failed: [Errno 111] Connection refused"
"""The specific message the disclosure must still carry, relocated but not removed."""


@pytest.fixture
def empty_history_server(
    tmp_path: Path, egress_allowlist: list[str]
) -> Iterator[_BrowserServer]:
    """
    Serve a private app whose job history starts empty.

    Two of the assertions below are about what is *absent* from the whole page:
    a job with NULL counts renders no ``.page-counts`` element anywhere, and an
    ERROR page carries no log-file path anywhere. Neither claim means anything
    against the session server, whose store and whose configured log path are
    shared with every other test in this module.
    """
    with _serve(_browser_test_settings(tmp_path), _BrowserTestScanner()) as server:
        egress_allowlist.append(server.url)
        yield server


@pytest.mark.browser
class TestErrorRenderingInChromium:
    """
    The plain-language failure, read from a rendered page (APPL-04, D-13).

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
        One alert, the details outside it and shut, and no path anywhere (P5).

        The count of one is the point of the wrapping ``<div role="alert">``:
        the sentence and the next step are announced together, once, and the
        disclosure is not announced with them. The closed state matters because
        an ``open`` disclosure would put the raw exception in front of a
        household member as though it were the message.
        """
        server = empty_history_server
        job_store: JobStore = server.app.state.job_store
        worker = server.app.state.worker
        job = job_store.create_job(profile="default", title="Broken Doc")
        job_store.finish_job(
            job.id,
            JobState.ERROR,
            error=_ERROR_DETAIL,
            error_category=_ERROR_CATEGORY,
        )
        worker._current_job_id = job.id
        try:
            page.goto(server.url)

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
            log_file = server.app.state.settings.output.log_file
            assert log_file not in source, log_file
            # Not just this deployment's path: any filename that looks like a
            # log would be a host filesystem detail on a page the whole LAN can
            # read, so the substring is what is refused (D-13).
            assert ".log" not in source
        finally:
            worker._current_job_id = None
            job_store.delete_job(job.id)


@pytest.mark.browser
class TestPageCountsInChromium:
    """
    The counts footnote, in both places it renders (APPL-03, D-32).

    One class and one rule serve the status area and the Title cell alike, and
    the guard for both is the filter returning None rather than a count's own
    truthiness. The failure mode that matters -- a measured zero rendered as
    nothing, or a never-recorded count rendered as zero -- is a rendering
    outcome, so it is read off the page rather than off the filter.
    """

    @pytest.mark.parametrize(
        ("counts", "expected"),
        [
            ((12, 2, 10), _MEASURED_COUNTS),
            ((12, 0, 12), _ZERO_BLANK_COUNTS),
            ((None, None, None), None),
        ],
        ids=["measured", "measured-zero", "never-recorded"],
    )
    def test_a_done_job_renders_its_counts_in_both_places_or_not_at_all(
        self,
        page: Page,
        empty_history_server: _BrowserServer,
        counts: tuple[int | None, int | None, int | None],
        expected: str | None,
    ) -> None:
        """
        Measured counts render twice; never-recorded ones render nowhere (P6).

        The three cases are separate page loads on a server with an empty
        history precisely so the negative case can be stated at full strength:
        not "this row has no counts" but "this page has no ``.page-counts``
        element at all". Sharing one page between the cases would have made the
        other two rows answer for the third.
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
            expect(page.locator("#status-area .status-done")).to_be_visible()

            if expected is None:
                expect(page.locator(".page-counts")).to_have_count(0)
                return

            expect(page.locator("#status-area .page-counts")).to_have_text(expected)
            title_cell = page.locator("#history-body tr").first.locator("td").nth(2)
            expect(title_cell.locator(".page-counts")).to_have_text(expected)
        finally:
            worker._current_job_id = None
            job_store.delete_job(job.id)


_FRONT_PAGES = 12
"""How many sheets pass A produces, so the busy line has UI-SPEC P7's number."""

_FRONT_COUNT_PREFIX = f"Front: {_FRONT_PAGES} pages \N{MIDDLE DOT} "
"""The opening UI-SPEC P7 pins, separator included."""


class _ManualDuplexScanner(_BrowserTestScanner):
    """
    A stub whose passes produce a known number of sheets, gated on pass B.

    ``_BrowserTestScanner`` spools one page per pass, which would pin the front
    count at 1 and leave the singular branch of the busy line as the only thing
    a browser could ever be shown. Twelve is the number UI-SPEC P7 writes, and
    it arrives by the real route: pass A returns twelve records, the pipeline
    counts them and hands the count to the worker through the pass-count
    callback, before it announces AWAITING_FLIP (D-33).

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
            settings: Only ``resolution`` is used, and only to report it back.
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
    The front count leads the busy line at SCANNING_REVERSE (APPL-03, D-33).

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
        wait_for_state: Callable[..., Job],
    ) -> None:
        """
        At ``SCANNING_REVERSE`` the busy line opens ``Front: 12 pages ·`` (P7).

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

            busy = page.locator("#status-area p[aria-busy='true']")
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
# nothing to say. Amendment A-3's fallback is only observable when some profile
# carries a human name and another does not, so both cases are configured.
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
    ``unnamed`` carries none, which is the pair Amendment A-3's fallback needs
    to be visible at all; ``silent`` carries no description, which is what the
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
    The dropdown explains itself live, without losing the slot (APPL-05, S4).

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
        The text changes and the element does not (P8, S4).

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
        ``label or name``, both halves, in the rendered options (P8, A-3).

        A config written before this phase carries no label until
        ``auto-profiles --force`` has been run, and a dropdown that rendered it
        as a blank row would be unusable. The labelled profile is asserted
        alongside so the fallback cannot pass by never being reached.
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
        An emptied slot is hidden outright, not left as a stray margin (P8).

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
"""UI-SPEC P15's Time cell shape, with the zone token captured for reuse."""


@pytest.mark.browser
class TestTimestampZonesInChromium:
    """
    Every timestamp on the page names its zone (APPL-12, D-34, D-35).

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
        The Time cell matches the pinned shape and the strip echoes its zone (P15).

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
# jars that decision exists to tell apart. Phase 30's fifth success criterion
# is about two people at one appliance, so it is answered here with two of them.
# ---------------------------------------------------------------------------

_FLIP_CONTROL_SELECTOR = "[hx-post^='/api/flip/']"
"""Every control that could answer a flip prompt, matched by where it posts."""

_WAITING_LINE = "Waiting for the stack to be flipped"
"""The non-owner's locked copy at AWAITING_FLIP (UI-SPEC S5). No ellipsis."""

_ABORT_CONFIRMATION = "Abort this scan? It will stop and cannot be resumed."
"""The question ``hx-confirm`` puts in the native dialog (D-27, UI-SPEC S5)."""

# D-26 forbids a third way out of the flip prompt: no override, no take-over,
# no force-continue. The absence IS the rendering, so it is asserted rather
# than left to a reviewer's memory -- and it is asserted on the words as well
# as on the controls, because a page offering one in prose would have smuggled
# the same affordance past a control count.
_NO_THIRD_WAY_OUT = re.compile(r"override|take over|force", re.IGNORECASE)

_FLIP_JOB_TITLE = "Two Browsers One Stack"
"""The title both pages must agree on, so the comparison is not vacuous."""


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
    wait_for_state: Callable[..., Job],
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
        wait_for_state: The conftest waiter, polling the job store.
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


@pytest.mark.browser
class TestTwoBrowsersOneStack:
    """
    One appliance, two browsers, one owner (APPL-09, D-24, D-26, success 5).

    The owner gate is a statement about two cookie jars, and a cookie jar is
    something only a browser has. Staging the non-owner by writing a foreign
    token onto a job row -- how the rest of this module does it -- proves the
    server branches correctly but leaves research assumption A6 untested: that
    a browser really keeps the ``Set-Cookie`` an htmx XHR returned, really
    sends it back on the next poll, and that a second browser really has none.
    Two contexts is the honest sample, so this is where success criterion 5 is
    answered.
    """

    def test_the_owner_is_offered_the_flip_and_the_second_browser_is_not(
        self,
        browser: Browser,
        flip_server: _BrowserServer,
        egress_allowlist: list[str],
        wait_for_state: Callable[..., Job],
    ) -> None:
        """
        Two cookie jars, one prompt, and nothing else differs (P9, D-24, D-26).

        Both contexts are built by hand, so neither inherits the overridden
        ``context`` fixture's routing: each installs ``_make_gate`` explicitly
        and both share one ``blocked`` list, which is the arrangement that
        factory exists for (Pitfall 9). Without it these two pages would be the
        only ones in the module able to reach the real internet, and nothing in
        the suite would have said so.
        """
        server = flip_server
        job_store: JobStore = server.app.state.job_store

        blocked: list[str] = []
        owner_ctx = browser.new_context()
        viewer_ctx = browser.new_context()
        try:
            owner_ctx.route("**/*", _make_gate(blocked, egress_allowlist))
            viewer_ctx.route("**/*", _make_gate(blocked, egress_allowlist))
            owner_page = owner_ctx.new_page()
            viewer_page = viewer_ctx.new_page()

            job_id = _drive_to_flip_prompt(
                owner_page, server, wait_for_state, _FLIP_JOB_TITLE
            )
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

                # A6, measured rather than assumed: the owner's jar holds the
                # cookie the scan response minted, and the second jar does not.
                owner_cookies = [
                    cookie
                    for cookie in owner_ctx.cookies()
                    if cookie["name"] == _OWNER_COOKIE_NAME
                ]
                assert len(owner_cookies) == 1, owner_ctx.cookies()
                cookie = owner_cookies[0]
                assert cookie["httpOnly"] is True
                assert cookie["sameSite"] == "Lax"
                # -1 is how this Playwright reports a cookie carrying neither
                # Max-Age nor Expires, and that is the only way a session
                # cookie is distinguishable through a cookie jar -- which is
                # what D-23 turns on: ownership ends when the browser does.
                assert cookie["expires"] == _SESSION_COOKIE_EXPIRY
                assert [
                    c for c in viewer_ctx.cookies() if c["name"] == _OWNER_COOKIE_NAME
                ] == []

                # Only the controls differ. The two status areas cannot be
                # compared directly -- the owner's holds the prompt and the
                # viewer's the waiting line, which is the difference under test
                # -- so the comparison is made where both pages report the same
                # job: the newest Job History row, title and state alike.
                assert _history_row_text(owner_page) == _history_row_text(viewer_page)
                assert _history_row_text(owner_page)[0].startswith(_FLIP_JOB_TITLE)

                # D-26's absence guard, on both pages.
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
            # which is the contract _make_gate's note states.
            assert blocked == [], f"a page tried to reach the network: {blocked}"

    def test_abort_asks_first_and_does_nothing_when_the_answer_is_no(
        self,
        page: Page,
        flip_server: _BrowserServer,
        wait_for_state: Callable[..., Job],
    ) -> None:
        """
        The confirm is really raised, and dismissing it aborts nothing (P10).

        ``hx-confirm`` is one attribute in a template, and a template test can
        only see that it is there. Whether a browser raises a dialog, whether
        the question it shows is the locked copy, and above all whether a "no"
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

        job_id = _drive_to_flip_prompt(page, server, wait_for_state, "Abort Me")
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


# ---------------------------------------------------------------------------
# The simpler form: [web] show_tags = false (D-28, D-29).
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

    The default profile's ``default_tags`` is what makes D-29 observable at
    all: with no tag picker on the page the submit carries no ``tags`` field,
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
    ``show_tags = false`` changes the form and never the scan (D-28, D-29).

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
        wait_for_state: Callable[..., Job],
    ) -> None:
        """
        No tag markup anywhere, and a scan still files under the profile's tags (P14).

        Absent, never hidden: what is not in the markup cannot be re-shown from
        devtools, cannot be read out by a screen reader and cannot be tabbed
        into, which is the whole of D-28's claim. The tags on the created job
        are then the proof of D-29 -- the submit carried no ``tags`` field at
        all, so the two ids on the row can only have come from the profile.
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
# The courtesy and the enforcement (D-15, APPL-07).
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
"""The slot's exact text for the refusal: the error partial's cross plus S8's copy."""

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
    A run of real status responses does not give the blocked button back (C-10).

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
        Three poll responses later the button is still disabled (P16).

        And once the job retires, the flag is the only thing still holding it
        shut, so the last swap is the tightest of the four readings.

        The job is parked directly rather than scanned, for the reason plan
        30-17's P4 gives: what the page needs is the two pieces of state a live
        scan produces, and running one would make the assertion wait on a
        worker thread it is not testing. Here it could not run one anyway --
        the guard refuses every submit on this server.

        The waits are completed responses, never a sleep: "several ticks" is a
        count of round trips, and a stopwatch would be measuring the host.
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
    The button is the courtesy; this is the proof the guard is the refusal (D-15).

    ``tests/test_web_errors.py`` already refuses this submit against a client
    that has no button at all, which is the stronger statement about the route.
    What it cannot say is that the two halves of D-15 really are separable in a
    browser: that the greyed-out button is a convenience anyone with devtools
    can take away, and that taking it away buys nothing. That is what this
    does, by removing the attribute and clicking.
    """

    def test_a_tampered_button_still_cannot_start_a_scan(
        self, page: Page, private_blocked_server: _BrowserServer
    ) -> None:
        """
        ``disabled`` removed, clicked, refused, and recorded as Failed (P17).

        Three separate claims, because a refusal that left any one of them
        unmet would be a different bug: the person is told why in the slot, no
        scan started, and the attempt is in Job History rather than silently
        swallowed (D-05). The row's error is asserted through the job store
        because Job History renders a state label and not the sentence -- the
        sentence is what ``saneless jobs`` and the API surface.
        """
        server = private_blocked_server
        job_store: JobStore = server.app.state.job_store
        page.goto(server.url)
        button = page.locator("#scan-btn")
        expect(button).to_be_disabled()

        page.evaluate(
            "() => document.getElementById('scan-btn').removeAttribute('disabled')"
        )
        expect(button).to_be_enabled()

        with page.expect_response(lambda r: r.url.endswith("/api/scan")) as caught:
            button.click()
        assert caught.value.status == _SERVICE_UNAVAILABLE, caught.value.status

        # Told why, in the slot phase 26 reserved for request errors (D-02).
        expect(page.locator(_SLOT_MESSAGE)).to_have_text(_TOKEN_UNSET_SLOT_TEXT)
        # And nothing started: the status area was never the target of this
        # response, and it still says what an idle appliance says.
        expect(page.locator("#status-area")).to_have_text("Ready to scan.")
        expect(page.locator("#status-area [aria-busy]")).to_have_count(0)

        status_cell = page.locator("#history-body tr").first.locator("td").nth(3)
        expect(status_cell).to_have_text("Failed", timeout=5_000)
        expect(status_cell).to_have_class(re.compile(r"\bstatus-error\b"))

        recent = job_store.list_recent(1)
        assert recent, "the refused submit recorded no job row"
        written = recent[0]
        assert written.error == _TOKEN_UNSET_ROW_ERROR, written.error
        assert written.error_category is ErrorCategory.REJECTED
