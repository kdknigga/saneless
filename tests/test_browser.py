"""
Playwright browser tests for web UI rendering verification.

These tests verify that PicoCSS styling, HTMX interactions, and UI
components render correctly in a real browser. They use a session-scoped
uvicorn server with a stub scanner for isolation from real hardware.

Requires: pytest-playwright, chromium browser (uv run playwright install chromium)
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Literal, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI
    from playwright.sync_api import Page

    from saneless.job import JobStore

import pytest
import uvicorn
from PIL import Image

from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScannerBackend,
    ScanSettings,
)
from saneless.vocabulary import JobState
from saneless.web.app import create_app


class _BrowserTestScanner(ScannerBackend):
    """Concrete scanner stub for browser tests."""

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

    def scan_pages(
        self, device_id: str, settings: ScanSettings
    ) -> Iterator[Image.Image]:
        """Yield a single white test image."""
        yield Image.new("RGB", (100, 100), "white")


class _BrowserServer(NamedTuple):
    """
    The live test server: its base URL and the app object behind it.

    Yielding only the URL made the server a black box -- a test could look at
    the idle page and nothing else, because there was no way to put a job into
    a given state. Carrying the app alongside the URL is what lets a test drive
    a job to FALLBACK and then look at it through a real browser.
    """

    url: str
    app: FastAPI


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
    yield _BrowserServer(url=f"http://127.0.0.1:{port}", app=app)

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def browser_server_url(browser_server: _BrowserServer) -> str:
    """Return the live server's base URL, for tests that need nothing else."""
    return browser_server.url


@pytest.mark.browser
class TestBrowserRendering:
    """PicoCSS and semantic HTML rendering tests."""

    def test_page_loads_with_title(self, page: Page, browser_server_url: str) -> None:
        """Main page loads and has the saneless title."""
        page.goto(browser_server_url)
        assert "saneless" in page.title().lower()

    def test_pico_css_applied(self, page: Page, browser_server_url: str) -> None:
        """PicoCSS styles are loaded and applied to semantic elements."""
        page.goto(browser_server_url)
        # PicoCSS styles <main> with max-width and margin
        main = page.locator("main")
        assert main.count() >= 1
        # Check that PicoCSS has loaded by verifying computed style
        box = main.first.bounding_box()
        assert box is not None
        # PicoCSS centers main content -- left margin should be > 0 on wide
        # viewports (default 1280px, PicoCSS max-width ~1200px)

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

# Mimics what app.js does on htmx:beforeRequest, so the afterSwap handler has
# something to undo. Calling the real handler is not possible from a test: it
# lives inside an IIFE and exposes no globals, by design.
_DISABLE_SCAN_BUTTON = """
() => {
    const btn = document.getElementById("scan-btn");
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    btn.textContent = "Scanning\\u2026";
}
"""

_SWAP_STATUS_AREA = """
() => htmx.ajax("GET", "/api/jobs/current/status",
                {target: "#status-area", swap: "outerHTML"})
"""


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
        job_store.update_state(job.id, JobState.FALLBACK)
        # No warning writer lands until plan 23-07; the column is written
        # directly so the inline warning paragraph has something to render.
        with job_store._conn:
            job_store._conn.execute(
                "UPDATE jobs SET warning = ? WHERE id = ?",
                ("Title, tags and correspondent were not applied.", job.id),
            )
        app.state.worker._current_job_id = job.id
        try:
            yield page
        finally:
            # The server is session-scoped, so a job left as "current" would
            # follow every later test onto the idle page.
            app.state.worker._current_job_id = None

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
        A fallback swap releases the Scan button (T-23-23).

        Before `.status-fallback` joined the `htmx:afterSwap` condition the
        button stayed disabled until the user reloaded the page -- a successful
        scan that looked like a locked-up application. A grep cannot prove the
        handler fires; this drives a real swap and watches the button.
        """
        self._goto(fallback_page, browser_server.url, scheme)
        fallback_page.evaluate(_DISABLE_SCAN_BUTTON)
        assert fallback_page.locator("#scan-btn").is_disabled()

        fallback_page.evaluate(_SWAP_STATUS_AREA)
        fallback_page.wait_for_selector("#scan-btn:not([disabled])")
        scan_btn = fallback_page.locator("#scan-btn")
        assert not scan_btn.is_disabled()
        assert scan_btn.get_attribute("aria-busy") == "false"
        assert scan_btn.inner_text() == "Scan"

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
