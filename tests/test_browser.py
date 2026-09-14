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

import threading
import time
from typing import TYPE_CHECKING, Literal, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi import FastAPI
    from playwright.sync_api import BrowserContext, Page, Route

    from saneless.job import Job, JobStore

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
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScanBatch,
    ScannerBackend,
    ScanSettings,
)
from saneless.vocabulary import TERMINAL_STATES, FlipOutcome, JobState, ScanOutcome
from saneless.web.app import create_app
from saneless.worker import WorkerFlipCoordinator

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


_SCAN_GATE_TIMEOUT = 30.0
"""Longest a closed gate holds ``scan_pages``, so a test that forgets it cannot hang."""


class _BrowserTestScanner(ScannerBackend):
    """
    Concrete scanner stub for browser tests, with a gate on ``scan_pages``.

    The gate is open by default, so a scan returns at once. A test closes it
    (``gate.clear()``) to hold a job in SCANNING while it looks at the page, and
    opens it again (``gate.set()``) to let the job finish. The wait is bounded,
    so a gate left closed by a failing test ends the scan rather than the run.
    """

    def __init__(self) -> None:
        """Create the stub with its gate open."""
        self.gate = threading.Event()
        self.gate.set()

    def get_devices(self) -> list[DeviceInfo]:
        """Return a single fake device."""
        return [
            DeviceInfo(
                name="test:browser:001",
                vendor="Test",
                model="Browser Scanner",
                device_type="virtual",
            ),
        ]

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """Return default capabilities."""
        return DeviceCapabilities(
            sources=["Flatbed"],
            resolutions=[300],
            modes=["color"],
        )

    def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
        """
        Wait for the gate, then return one page with content on it.

        The page is half black rather than blank: the default profile's empty
        page detection would drop an all-white page and end the job in ERROR,
        and the browser tests need a scan that can reach DONE.
        """
        self.gate.wait(timeout=_SCAN_GATE_TIMEOUT)
        page = Image.new("RGB", (100, 100), "white")
        page.paste((0, 0, 0), (0, 0, 50, 100))
        return ScanBatch(
            pages=[page],
            actual_resolution=settings.resolution,
            pages_rejected=0,
        )


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


@pytest.fixture(scope="session")
def browser_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_BrowserServer]:
    """Start a real uvicorn server for browser tests."""
    tmp_dir = tmp_path_factory.mktemp("browser")
    test_token = "fake-token"
    settings = Settings(
        scanner=ScannerConfig(device="test:browser:001"),
        paperless=PaperlessConfig(
            url="http://localhost:9999",
            token=test_token,
        ),
        output=OutputConfig(
            tmp_dir=str(tmp_dir),
            data_dir=str(tmp_dir),
            log_file=str(tmp_dir / "saneless.log"),
        ),
        profiles={
            "default": ProfileConfig(),
            "duplex": ProfileConfig(source="ADF Manual Duplex"),
        },
    )
    scanner = _BrowserTestScanner()
    app = create_app(settings, scanner)

    # Note: uvicorn.Server.capture_signals already skips signal handling
    # when running in a non-main thread, so no special config is needed.
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
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
    yield _BrowserServer(url=f"http://127.0.0.1:{port}", app=app, scanner=scanner)

    server.should_exit = True
    thread.join(timeout=5)
    # join() reports nothing on timeout. A uvicorn thread that fails to stop
    # leaves a bound port and a live app behind for the rest of the session,
    # so the outcome is asserted rather than discarded.
    assert not thread.is_alive(), "uvicorn test server did not shut down"


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
    paperless = browser_server.app.state.paperless

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
# resolved, and removes it again. Reading all three from the same live page is
# the only way to compare them: only one status renders at a time, so there is
# never a moment when all three exist in the document on their own.
_PROBE_STATUS_COLOURS = """
() => {
    const out = {};
    for (const cls of ["status-done", "status-error", "status-fallback"]) {
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
_PROBE_CONTEXT_CONTRAST = """
({cls, context}) => {
    const probe = document.createElement(context === "status-area" ? "p" : "td");
    probe.className = cls;
    probe.textContent = "probe";
    let added = probe;
    if (context === "status-area") {
        document.getElementById("status-area").appendChild(probe);
    } else {
        const row = document.createElement("tr");
        row.appendChild(probe);
        document.getElementById("history-body").appendChild(row);
        added = row;
    }
    const colour = getComputedStyle(probe).color;
    const alphaOf = (value) => {
        if (value === "transparent") return 0;
        if (!value.startsWith("rgba(")) return 1;
        return parseFloat(value.slice(value.lastIndexOf(",") + 1));
    };
    // Fully transparent layers paint nothing and are skipped; translucent ones
    // are collected and the walk continues, because what is beneath them still
    // shows through. Only a fully opaque layer ends the walk.
    const backgroundStack = [];
    for (let el = probe; el !== null; el = el.parentElement) {
        const value = getComputedStyle(el).backgroundColor;
        const alpha = alphaOf(value);
        if (alpha === 0) continue;
        backgroundStack.push(value);
        if (alpha === 1) break;
    }
    const last = backgroundStack[backgroundStack.length - 1];
    if (backgroundStack.length === 0 || alphaOf(last) < 1) {
        // Nothing out to <html> painted an opaque layer, so what shows through
        // is the browser canvas, which is white.
        backgroundStack.push("rgb(255, 255, 255)");
    }
    added.remove();
    return {colour, backgroundStack};
}
"""


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
        assert len(set(colours.values())) == 3, colours

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
    @pytest.mark.parametrize("placement", ["status-area", "history-cell"])
    @pytest.mark.parametrize("cls", ["status-done", "status-error", "status-fallback"])
    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_status_colour_meets_aa_contrast(
        self,
        page: Page,
        browser_server_url: str,
        scheme: Literal["light", "dark"],
        cls: Literal["status-done", "status-error", "status-fallback"],
        placement: Literal["status-area", "history-cell"],
    ) -> None:
        """
        Every status colour reaches WCAG AA where it is really shown (T2).

        The fallback amber is also checked by value, so a palette drift is
        reported by name rather than only as a ratio that happens to pass.
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
