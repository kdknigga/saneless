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
from typing import TYPE_CHECKING, Literal, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from fastapi import FastAPI
    from playwright.sync_api import BrowserContext, Page, Response, Route
    from starlette.types import ASGIApp, Receive, Scope, Send

    from saneless.job import Job, JobStore
    from saneless.scanner.base import PageSink, ScanSettings

import httpx
import pytest
import uvicorn
from PIL import Image
from playwright.sync_api import expect

from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.job import JobResult
from saneless.paperless import UploadResult
from saneless.scanner.base import DeviceInfo, ScanBatch
from saneless.vocabulary import (
    TERMINAL_STATES,
    FlipOutcome,
    JobState,
    ScanOutcome,
    WorkerHealth,
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


_SCAN_GATE_TIMEOUT = 30.0
"""Longest a closed gate holds ``scan_pages``, so a test that forgets it cannot hang."""


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

        The page is half black rather than blank: the default profile's empty
        page detection would drop an all-white page and end the job in ERROR,
        and the browser tests need a scan that can reach DONE.

        Args:
            device_id: Ignored; this stub scans nothing real.
            settings: Only ``resolution`` is used, and only to report it back.
            sink: The pipeline's own sink, which receives the page.

        Returns:
            A batch of the single record the sink returned.

        """
        self.gate.wait(timeout=_SCAN_GATE_TIMEOUT)
        page = Image.new("RGB", (100, 100), "white")
        page.paste((0, 0, 0), (0, 0, 50, 100))
        record = sink.add(page)
        return scan_batch([record], resolution=settings.resolution)


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
    """
    blocked: list[str] = []

    def _gate(route: Route) -> None:
        url = route.request.url
        if _is_allowed(url, egress_allowlist):
            route.continue_()
        else:
            blocked.append(url)
            route.abort()

    context.route("**/*", _gate)
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

# Counts htmx:afterRequest for the two selects that load on page load. htmx
# 2.0.8 strips hx-disabled-elt's `disabled` inside the request's onload handler,
# before it fires afterRequest, so once both events have fired the inheritance
# trap has either sprung or it has not. Registered as an init script so the
# listener exists before htmx issues the load requests.
_RECORD_LOAD_REQUESTS = """
window.__selectLoadsFinished = 0;
document.addEventListener("htmx:afterRequest", (event) => {
    const id = event.detail.elt && event.detail.elt.id;
    if (id === "tags-select" || id === "correspondent-select") {
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
        // The first <article> is the Scan card: the secondary surface, which
        // is the one a status line placed inside a card would sit on.
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
        ["status-done", "status-error", "status-fallback", "status-cancelled"],
    )
    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_status_colour_meets_aa_contrast(
        self,
        page: Page,
        browser_server_url: str,
        scheme: Literal["light", "dark"],
        cls: Literal[
            "status-done", "status-error", "status-fallback", "status-cancelled"
        ],
        placement: Literal["status-area", "history-cell", "card"],
    ) -> None:
        """
        Every status colour reaches WCAG AA where it is really shown (T2).

        The card placement is the margin UI-SPEC records for a status line
        inside an ``<article>``; the dark card is lighter than the dark page,
        so it is the tighter of the two for the muted cancelled grey.

        The fallback amber and the cancelled grey are also checked by value, so
        a palette drift is reported by name rather than only as a ratio that
        happens to pass.
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
        if cls == "status-cancelled":
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
    configured = _browser_test_settings(tmp_dir)
    settings = configured.model_copy(
        update={
            "paperless": PaperlessConfig(
                url=configured.paperless.url,
                token=_SHIPPED_PLACEHOLDER,
            )
        }
    )
    app = create_app(settings, scanner)
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
