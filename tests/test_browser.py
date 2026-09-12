"""
Playwright browser tests for web UI rendering verification.

These tests verify that PicoCSS styling, HTMX interactions, and UI
components render correctly in a real browser. They use a session-scoped
uvicorn server with a stub scanner for isolation from real hardware.

PicoCSS and htmx are loaded from cdn.jsdelivr.net by ``base.html``, so every
colour assertion below quietly depends on that CDN being reachable during the
run. An unreachable CDN does not announce itself: the contrast checks report
unstyled black-on-white ratios and the amber checks report a colour mismatch,
both of which read like a palette regression. If a run fails that way in bulk,
read ``test_pico_css_applied`` first -- it is the load canary, and it is the one
that says so in plain words.

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
from saneless.job import JobResult
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScannerBackend,
    ScanSettings,
)
from saneless.vocabulary import JobState, ScanOutcome
from saneless.web.app import create_app

# Every palette value these tests assert against, in one place. All of them are
# valid only for @picocss/pico@2.1.1, the version pinned in base.html. Pico is
# loaded from a CDN and .github/dependabot.yml watches "github-actions" only, so
# that pin sits outside every automated update path and nothing warns when it
# drifts.
#
# Nor is the drift caught on the enforcing boundary yet: CI deselects the
# `browser` marker, and adding a browser job is held for phase 26, which vendors
# Pico and htmx locally first so CI need not depend on CDN egress. Until then a
# Pico bump is a manual edit whose breakage surfaces only when a human runs
# `pytest -m browser`. Collecting the expectations here does not fix that, but it
# makes the bump a one-line edit with a named reason instead of four scattered
# literals that have to be found by grep.
_PICO_SURFACE = {"light": "rgb(255, 255, 255)", "dark": "rgb(19, 23, 31)"}
"""Pico's page surface under each colour scheme, as the browser computes it."""

_AMBER = {"light": "rgb(161, 98, 7)", "dark": "rgb(202, 138, 4)"}
"""The app's fallback amber (#a16207 / #ca8a04, from app.css), as computed."""


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
    # join() reports nothing on timeout. A uvicorn thread that fails to stop
    # leaves a bound port and a live app behind for the rest of the session,
    # so the outcome is asserted rather than discarded.
    assert not thread.is_alive(), "uvicorn test server did not shut down"


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
# to plus the background it actually sits on. The background is found by
# walking up to the first ancestor that paints one, because a history cell has
# its own surface while a status paragraph shows the page through. Adding,
# reading and removing all happen in this one call, so no htmx swap can land
# in between.
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
    const isTransparent = (value) =>
        value === "transparent" ||
        (value.startsWith("rgba(") &&
            parseFloat(value.slice(value.lastIndexOf(",") + 1)) === 0);
    let background = null;
    for (let el = probe; el !== null; el = el.parentElement) {
        const value = getComputedStyle(el).backgroundColor;
        if (!isTransparent(value)) {
            background = value;
            break;
        }
    }
    if (background === null) {
        background = getComputedStyle(document.documentElement).backgroundColor;
    }
    added.remove();
    return {colour, background};
}
"""


def _relative_luminance(css_colour: str) -> float:
    """Return the WCAG relative luminance of an ``rgb()`` or ``rgba()`` string."""
    inner = css_colour[css_colour.index("(") + 1 : css_colour.index(")")]
    channels = [float(part) / 255 for part in inner.split(",")[:3]]
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
        background = probe["background"]
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
