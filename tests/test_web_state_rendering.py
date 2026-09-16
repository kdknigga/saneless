"""
Per-state rendering contract for the web templates.

Covers requirements: UI-03, UI-07, CTR-01, ROBU-01, ROBU-04, ROBU-08.

The templates are the one surface neither ``ty`` nor ``pyrefly`` can see. Once
the hand-written state lists moved behind ``Job.is_active`` / ``Job.is_busy``
and the ``state_label`` / ``progress_label`` filters, nothing mechanical pinned
*which* state produces *which* markup any more: the grep gates in the plan only
prove the string literals are gone, and the browser suite only exercises the
idle page. These tests pin the mapping for every ``JobState`` member.

Every case is parametrised over ``list(JobState)`` rather than a hand-written
list of names, so an eighth member cannot be added without forcing a decision
here -- the same discipline ``tests/test_vocabulary.py`` applies to the lookup
functions themselves.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from markupsafe import escape

from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.scanner.base import DeviceInfo
from saneless.vocabulary import (
    ACTIVE_STATES,
    BUSY_STATES,
    TERMINAL_STATES,
    TITLE_MAX_LENGTH,
    ErrorCategory,
    JobState,
    RequestRejection,
    error_message,
    error_next_step,
    progress_label,
    rejection_message,
    state_label,
)
from saneless.web import app as app_module
from saneless.web.app import create_app
from tests.conftest import StubScannerBackend

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from saneless.job import JobStore


class _StubScanner(StubScannerBackend):
    """
    The shared stub backend, but reporting one device instead of none.

    Only ``get_devices`` differs: the profile dropdown and the worker's
    startup profile generation (D-14) both read it, and a device list of one
    is what these tests render against.  Capabilities and ``scan_pages`` come
    straight from ``StubScannerBackend``; these tests never run a scan.
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


# The log file every app in this module writes to. D-13 forbids a host
# filesystem path from reaching a LAN-visible page, so this name is deliberately
# unlike anything a template could produce by accident.
_LOG_FILE_NAME = "render-test-do-not-render-me.log"

# The category `_job_in_state` records on an ERROR row unless a test asks for
# another. Production never writes an ERROR row without one -- the worker always
# classifies (`worker.py`) -- so this, not NULL, is the shape the status area's
# main path renders. REJECTED is avoided because D-06 gives it its own routing.
_DEFAULT_ERROR_CATEGORY = ErrorCategory.SCANNER

# The templates and the stylesheet, located the way the app locates them, so a
# moved package cannot make a source assertion pass on an empty file.
_PACKAGE_DIR = Path(app_module.__file__).parent
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_APP_CSS = _PACKAGE_DIR / "static" / "app.css"

# The scan button, captured whole so attribute and text assertions cannot be
# satisfied by markup somewhere else on the page.
_SCAN_BUTTON = re.compile(
    r'<button type="submit" id="scan-btn"(?P<attrs>[^>]*)>(?P<text>.*?)</button>',
    re.DOTALL,
)


def _make_app(tmp_path: Path) -> FastAPI:
    """
    Build a real app with a stub scanner and no network calls, not yet started.

    Returns:
        The app, whose lifespan (and so its worker) starts with its TestClient.

    """
    settings = Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(url="http://localhost:8000", token="test-token"),
        output=OutputConfig(
            tmp_dir=str(tmp_path),
            data_dir=str(tmp_path),
            # Named distinctively so `test_no_log_path_reaches_the_page` can
            # search the rendered markup for it and mean something: the default
            # would be a path fragment that could collide with tmp_path itself.
            log_file=str(tmp_path / _LOG_FILE_NAME),
        ),
        # Two profiles, so the set is not the bare default.  With only
        # ``default``, the worker's startup generation (D-14) would build
        # profiles from _StubScanner's device and swap them in while these
        # requests read the dropdown -- generation these tests do not intend.
        profiles={
            "default": ProfileConfig(),
            "duplex": ProfileConfig(source="ADF Duplex"),
        },
    )
    app = create_app(settings, _StubScanner())
    app.state.paperless.get_tags = list
    app.state.paperless.get_correspondents = list
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


def _adopt_as_current_job(client: TestClient, job_id: str) -> None:
    """
    Point the live worker at `job_id` without submitting real work.

    This writes ScanWorker._current_job_id directly, which is private. It is the
    single place in this module that does so, deliberately: the routes read the
    current job through the worker, and driving it through the real submit path
    would run a pipeline these rendering tests do not want.

    Safe because no job is ever submitted here, so _process_job's `finally`
    cannot race the assignment. If the worker's job tracking changes, this one
    helper is the only thing that needs updating.
    """
    _app(client).state.worker._current_job_id = job_id


def _set_warning(job_store: JobStore, job_id: str, warning: str) -> None:
    """
    Write the `warning` column directly.

    `JobStore` grows no warning writer until plan 23-07, but the FALLBACK status
    markup interpolates `job.warning` now, so the escaping contract (T-23-21)
    needs a value to escape today. A single parameterised UPDATE is narrower
    than adding a production setter that nothing else would call yet, and it is
    the same kind of deliberate reach-through as `_adopt_as_current_job` above.
    """
    with job_store._conn:
        job_store._conn.execute(
            "UPDATE jobs SET warning = ? WHERE id = ?",
            (warning, job_id),
        )


def _job_in_state(
    client: TestClient,
    state: JobState,
    warning: str | None = None,
    error_category: ErrorCategory | None = _DEFAULT_ERROR_CATEGORY,
) -> str:
    """
    Create a job, drive it to `state`, and make it the worker's current job.

    The category is written for every state, exactly as `error` already was:
    only the ERROR branch reads either, so the other states are unaffected, and
    keeping one write means the helper has one shape. Pass
    ``error_category=None`` to get a pre-Phase-21 row, whose status area falls
    back to the specific message alone.

    Returns:
        The job's id, which the technical-details assertions need.

    """
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title="Render Test")
    job_store.update_state(
        job.id, state, error="disk on fire", error_category=error_category
    )
    if warning is not None:
        _set_warning(job_store, job.id, warning)
    _adopt_as_current_job(client, job.id)
    return job.id


@pytest.mark.parametrize("state", list(JobState))
def test_history_cell_shows_the_shared_label(
    client: TestClient, state: JobState
) -> None:
    """
    Each state reaches `state_label` in the history table (CTR-01).

    This pins the wiring -- which state is routed through which filter -- not
    the label text, because the expectation is built from the same function the
    template calls. The literal strings are pinned separately in
    tests/test_vocabulary.py; the one below stands alone as a spot check.
    """
    _job_in_state(client, state)
    response = client.get("/api/jobs/history")
    assert response.status_code == 200
    assert f">\n    {state_label(state)}\n  </td>" in response.text


@pytest.mark.parametrize("state", list(JobState))
def test_history_cell_css_class(client: TestClient, state: JobState) -> None:
    """Only the four terminal history cells carry a status CSS class (CTR-01, D-01)."""
    _job_in_state(client, state)
    text = client.get("/api/jobs/history").text
    assert ('<td class="status-done">' in text) is (state is JobState.DONE)
    assert ('<td class="status-error">' in text) is (state is JobState.ERROR)
    assert ('<td class="status-fallback">' in text) is (state is JobState.FALLBACK)
    assert ('<td class="status-cancelled">' in text) is (state is JobState.CANCELLED)


@pytest.mark.parametrize("state", list(JobState))
def test_status_area_polls_only_while_active(
    client: TestClient, state: JobState
) -> None:
    """The 1s status poll is attached for exactly the active states (UI-07)."""
    _job_in_state(client, state)
    text = client.get("/api/jobs/current/status").text
    assert ('hx-trigger="every 1s"' in text) is (state in ACTIVE_STATES)


@pytest.mark.parametrize("state", list(JobState))
def test_status_area_prose(client: TestClient, state: JobState) -> None:
    """
    Each state renders its own status markup (UI-03, CTR-01).

    As above, the busy line is built from `progress_label`, so it pins routing
    rather than text; the literal spot check below guards the text itself.
    """
    _job_in_state(client, state)
    text = client.get("/api/jobs/current/status").text

    busy_line = f'<p aria-busy="true">{progress_label(state)}</p>'
    assert (busy_line in text) is (state in BUSY_STATES)

    # AWAITING_FLIP is active but NOT busy: it shows the flip prompt instead of
    # a progress line, and never renders aria-busy.
    assert ("flip-prompt" in text) is (state is JobState.AWAITING_FLIP)
    if state not in BUSY_STATES:
        assert 'aria-busy="true"' not in text

    if state is JobState.DONE:
        assert '<p class="status-done">&#10003; Done: Render Test</p>' in text
    if state is JobState.ERROR:
        # APPL-04, UI-SPEC S2: the paragraph now carries the category sentence
        # and has lost the literal `Error: ` prefix, because the sentence names
        # the problem itself. `role="alert"` moved to a wrapping <div> so the
        # next step is announced too; it is asserted in TestStatusAreaError.
        sentence = escape(error_message(_DEFAULT_ERROR_CATEGORY))
        assert f'<p class="status-error">&#10007; {sentence}</p>' in text
        assert "&#10007; Error: disk on fire" not in text
    if state is JobState.FALLBACK:
        assert (
            '<p class="status-fallback">&#8594; Saved to folder: Render Test</p>'
            in text
        )
        # A fallback is a degradation, not a failure: no role="alert" here.
        assert 'role="alert"' not in text
    if state is JobState.CANCELLED:
        assert '<p class="status-cancelled">&#8856; Cancelled: Render Test</p>' in text
        # A cancel is a deliberate stop, not a failure: no role="alert" here (D-01).
        assert 'role="alert"' not in text
    # The history-refresh hook belongs to the terminal states only -- all four
    # of them, FALLBACK and CANCELLED included, or the table goes stale.
    assert ('hx-get="/api/jobs/history"' in text) is (state in TERMINAL_STATES)


# The alert region and the disclosure, captured whole. Neither nests a <div> or
# a <details>, so a non-greedy body is exact rather than merely convenient.
_ALERT_DIV = re.compile(r'<div role="alert">(?P<body>.*?)</div>', re.DOTALL)
_TECH_DETAILS = re.compile(
    r'<details class="tech-details"(?P<attrs>[^>]*)>(?P<body>.*?)</details>',
    re.DOTALL,
)

# The legacy ERROR line, byte for byte. A row written before Phase 21 carries no
# category, and UI-SPEC S2 requires today's shape verbatim for it: substituting
# UNKNOWN would print "Something went wrong." over a row that still holds a
# truthful specific message.
_LEGACY_ERROR_LINE = (
    '<p role="alert" class="status-error">&#10007; Error: disk on fire</p>'
)


class TestStatusAreaError:
    """
    The ERROR branch after APPL-04: a sentence, a next step, and a disclosure.

    UI-SPEC S2. The three things worth breaking a test over are that the alert
    covers the next step and not just the sentence, that the specific message
    was relocated rather than deleted, and that no host filesystem path ever
    reaches the markup (D-13).
    """

    @staticmethod
    def _status(client: TestClient) -> str:
        """
        Render the status area for the current job.

        Returns:
            The status poll's body.

        """
        return client.get("/api/jobs/current/status").text

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_one_alert_covers_the_sentence_and_the_next_step(
        self, client: TestClient, category: ErrorCategory
    ) -> None:
        """
        The alert announces the message *and* what to do about it (APPL-04).

        The next step is the most actionable content on the page; leaving it
        outside the alert would mean a screen-reader user never hears it.
        Parametrised over ``list(ErrorCategory)`` so an eighth member cannot be
        added without forcing a decision here.
        """
        _job_in_state(client, JobState.ERROR, error_category=category)
        text = self._status(client)

        assert text.count('role="alert"') == 1
        match = _ALERT_DIV.search(text)
        assert match is not None, "the ERROR branch renders no alert div"
        # Escaped, because ASSEMBLY's next step contains an apostrophe and
        # Jinja renders it as `&#39;`. Comparing against the raw constant would
        # quietly exempt exactly the copy most likely to carry punctuation.
        sentence = escape(error_message(category))
        next_step = escape(error_next_step(category))
        body = match.group("body")
        assert sentence in body
        assert next_step in body
        assert f'<p class="status-error">&#10007; {sentence}</p>' in body

    def test_the_disclosure_is_collapsed_and_sits_outside_the_alert(
        self, client: TestClient
    ) -> None:
        """
        "Technical details" is not announced with the failure, and starts shut.

        No ``open`` attribute, so the detail is one tap away rather than in the
        way; and outside the alert, so a screen reader reads the sentence and
        the next step without the debugging aid.
        """
        _job_in_state(client, JobState.ERROR)
        text = self._status(client)

        details = _TECH_DETAILS.search(text)
        assert details is not None, "the ERROR branch renders no disclosure"
        assert "open" not in details.group("attrs")
        assert "<summary>Technical details</summary>" in details.group("body")

        alert = _ALERT_DIV.search(text)
        assert alert is not None
        assert "<details" not in alert.group("body")

    def test_the_disclosure_holds_the_specific_message_category_and_job_id(
        self, client: TestClient
    ) -> None:
        """
        The specific message is relocated, not removed (UI-SPEC S2, D-13).

        ``vocabulary.error_message``'s own docstring warns that swapping the
        specific message for a category sentence would be a regression, so
        ``job.error`` is still rendered -- inside the disclosure, with the two
        other facts D-13 permits and nothing else.
        """
        job_id = _job_in_state(client, JobState.ERROR)
        details = _TECH_DETAILS.search(self._status(client))
        assert details is not None
        body = details.group("body")

        assert "disk on fire" in body
        assert f"Category: {_DEFAULT_ERROR_CATEGORY.value}" in body
        assert f"Job: {job_id}" in body

    def test_a_row_without_a_category_renders_the_legacy_line_verbatim(
        self, client: TestClient
    ) -> None:
        """
        A pre-Phase-21 row keeps today's shape, with no next step and no detail.

        Substituting UNKNOWN would print "Something went wrong." over a row
        that still holds a truthful specific message (UI-SPEC S2).
        """
        _job_in_state(client, JobState.ERROR, error_category=None)
        text = self._status(client)

        assert _LEGACY_ERROR_LINE in text
        assert text.count('role="alert"') == 1
        assert "tech-details" not in text
        assert escape(error_next_step(ErrorCategory.UNKNOWN)) not in text

    @pytest.mark.parametrize("category", [_DEFAULT_ERROR_CATEGORY, None])
    def test_no_log_path_reaches_the_rendered_page(
        self, client: TestClient, category: ErrorCategory | None
    ) -> None:
        """
        The configured log file never appears in the markup (D-13, T-30-52).

        It is a host filesystem path on a LAN-visible page. Both the
        categorised and the legacy branch are checked, because the disclosure
        is the surface that would be tempted to offer one.
        """
        _job_in_state(client, JobState.ERROR, error_category=category)
        for text in (self._status(client), client.get("/").text):
            assert _LOG_FILE_NAME not in text

    def test_no_template_names_a_log_path(self) -> None:
        """
        No template under ``web/templates/`` references a log path at all.

        The rendered-page assertion above proves this app does not leak one;
        this proves no template *could*, which is the durable half (T-30-52).
        """
        offenders = sorted(
            path.name
            for path in _TEMPLATES_DIR.rglob("*.html")
            if re.search(
                r"log_file|log_path|logfile",
                path.read_text(encoding="utf-8"),
                re.IGNORECASE,
            )
        )
        assert offenders == []

    def test_the_disclosure_escapes_markup_rather_than_interpolating_it(
        self, client: TestClient
    ) -> None:
        """
        ``job.error`` is exception-derived text and is escaped (T-30-54).

        Moving it into a disclosure moved an untrusted string to a new place in
        the document; autoescaping is a setting, and a setting can be changed,
        so the property is pinned here as it already is for ``job.warning``.
        """
        job_store: JobStore = _app(client).state.job_store
        job = job_store.create_job(profile="default", title="Render Test")
        job_store.update_state(
            job.id,
            JobState.ERROR,
            error="<script>alert(1)</script>",
            error_category=_DEFAULT_ERROR_CATEGORY,
        )
        _adopt_as_current_job(client, job.id)

        text = self._status(client)
        assert "<script>alert(1)</script>" not in text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text

    def test_the_summary_clears_the_touch_target_floor(self) -> None:
        """
        The summary is a finger-sized row (UI-SPEC S2, WCAG 2.5.5).

        Rare control or not, it is still one someone taps standing at the
        scanner.
        """
        css = _APP_CSS.read_text(encoding="utf-8")
        assert "details.tech-details > summary {" in css
        assert "min-height: 2.75rem;" in css


@pytest.mark.parametrize("state", list(JobState))
def test_scan_button_disabled_and_busy_split(
    client: TestClient, state: JobState
) -> None:
    """`disabled` follows is_active; `aria-busy` follows the narrower is_busy (UI-07)."""
    _job_in_state(client, state)
    match = _SCAN_BUTTON.search(client.get("/").text)
    assert match is not None, "scan button markup not found"
    attrs = match.group("attrs")

    assert ("disabled" in attrs) is (state in ACTIVE_STATES)
    assert ('aria-busy="true"' in attrs) is (state in BUSY_STATES)


@pytest.mark.parametrize("state", list(JobState))
def test_scan_button_text(client: TestClient, state: JobState) -> None:
    """The button keeps its three exact captions, HTML entity included (UI-07)."""
    _job_in_state(client, state)
    match = _SCAN_BUTTON.search(client.get("/").text)
    assert match is not None, "scan button markup not found"
    text = match.group("text").strip()

    if state is JobState.AWAITING_FLIP:
        expected = "Waiting for flip&#8230;"
    elif state in BUSY_STATES:
        expected = "Scanning&#8230;"
    else:
        expected = "Scan"
    assert text == expected


_OOB_ATTR = ' hx-swap-oob="true"'
_OOB_MESSAGE_CLEAR = '<div id="status-message" hx-swap-oob="innerHTML"></div>'


def _only_scan_button(text: str) -> re.Match[str]:
    """Return the single scan button in `text`, failing on none or several."""
    assert text.count('id="scan-btn"') == 1, "expected exactly one scan button"
    match = _SCAN_BUTTON.search(text)
    assert match is not None, "scan button markup not found"
    return match


def test_page_renders_one_inline_scan_button(client: TestClient) -> None:
    """
    The full page carries exactly one Scan button, and it is not OOB (T1).

    The button markup lives in one partial (ROBU-04, C-10).  If the OOB include
    ever moved into ``partials/status.html``, which the page also includes, the
    page would carry two ``id="scan-btn"`` and this count would catch it.
    """
    match = _only_scan_button(client.get("/").text)
    assert "hx-swap-oob" not in match.group("attrs")


@pytest.mark.parametrize("state", list(JobState))
def test_poll_scan_button_matches_the_page_button(
    client: TestClient, state: JobState
) -> None:
    """
    The poll's OOB button is the page's button plus the OOB flag (T2, ROBU-04).

    One template renders both, so for the same job the two copies must be
    byte-identical once ``hx-swap-oob`` is removed.  A second source of truth
    for the button's state is exactly what C-10 was.
    """
    _job_in_state(client, state)
    page = _only_scan_button(client.get("/").text)
    poll = _only_scan_button(client.get("/api/jobs/current/status").text)

    assert _OOB_ATTR in poll.group("attrs")
    assert poll.group(0).replace(_OOB_ATTR, "", 1) == page.group(0)


@pytest.mark.parametrize("state", list(JobState))
def test_poll_scan_button_follows_the_state_table(
    client: TestClient, state: JobState
) -> None:
    """
    The OOB button on every poll carries the S4 state table (T2, ROBU-04).

    ``aria-busy`` is omitted, never ``"false"``, when the job is not busy.
    """
    _job_in_state(client, state)
    text = client.get("/api/jobs/current/status").text
    match = _only_scan_button(text)
    attrs = match.group("attrs")

    assert ("disabled" in attrs) is (state in ACTIVE_STATES)
    assert ('aria-busy="true"' in attrs) is (state in BUSY_STATES)
    assert 'aria-busy="false"' not in text

    if state is JobState.AWAITING_FLIP:
        expected = "Waiting for flip&#8230;"
    elif state in BUSY_STATES:
        expected = "Scanning&#8230;"
    else:
        expected = "Scan"
    assert match.group("text").strip() == expected


def test_scan_success_carries_button_status_and_message_clear(
    client: TestClient,
) -> None:
    """
    A successful scan re-renders the button and clears the slot OOB (T3, D-03).

    The clear is for this response only: it empties an error left in
    ``#status-message`` by an earlier rejected submit.
    """
    response = client.post(
        "/api/scan", data={"profile": "default", "title": "Button Test"}
    )
    assert response.status_code == 200
    text = response.text

    match = _only_scan_button(text)
    assert _OOB_ATTR in match.group("attrs")
    assert text.count('id="status-area"') == 1
    assert text.count(_OOB_MESSAGE_CLEAR) == 1
    assert text.count("status-message") == 1


def test_poll_never_clears_the_status_message(client: TestClient) -> None:
    """
    A poll re-renders the button OOB but never touches the slot (T3, D-03).

    Carrying the clear here would erase a 429 shown mid-scan within a second.
    """
    _job_in_state(client, JobState.SCANNING)
    text = client.get("/api/jobs/current/status").text
    assert _OOB_ATTR in _only_scan_button(text).group("attrs")
    assert "status-message" not in text


@pytest.mark.parametrize("answer", ["continue", "abort"])
def test_flip_responses_never_clear_the_status_message(
    client: TestClient, answer: str
) -> None:
    """Flip Continue and Abort re-render the button OOB, not the slot (T3, D-03)."""
    _job_in_state(client, JobState.AWAITING_FLIP)
    job_id = _app(client).state.worker._current_job_id
    response = client.post(f"/api/flip/{answer}", data={"job_id": job_id})
    assert response.status_code == 200
    assert _OOB_ATTR in _only_scan_button(response.text).group("attrs")
    assert "status-message" not in response.text


def test_scan_error_response_carries_no_button(client: TestClient) -> None:
    """A rejected scan renders only the error, never the button (ROBU-04, S4)."""
    response = client.post(
        "/api/scan",
        data={"profile": "no-such-profile", "title": "Rejected"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    assert "scan-btn" not in response.text


_SCAN_FORM = re.compile(r'<form hx-post="/api/scan"[^>]*>')
_TITLE_INPUT = re.compile(r'<input[^>]*id="title-input"[^>]*>')


def test_scan_form_disables_the_button_without_inheritance(
    client: TestClient,
) -> None:
    """
    The form disables the button for its own round-trip only (ROBU-04, S4).

    ``hx-disabled-elt`` replaces the deleted app.js handler.  ``hx-disinherit``
    is mandatory: on htmx 2.0.8 the selects and refresh buttons inside the form
    would otherwise inherit it and strip ``disabled`` from a server-disabled
    button when their own requests finish (C-10).
    """
    match = _SCAN_FORM.search(client.get("/").text)
    assert match is not None, "scan form markup not found"
    form = match.group(0)
    assert 'hx-disabled-elt="#scan-btn"' in form
    assert 'hx-disinherit="hx-disabled-elt"' in form


def test_title_input_is_capped_at_the_server_limit(client: TestClient) -> None:
    """``#title-input`` carries the server's title cap as maxlength (ROBU-08)."""
    match = _TITLE_INPUT.search(client.get("/").text)
    assert match is not None, "title input markup not found"
    assert f'maxlength="{TITLE_MAX_LENGTH}"' in match.group(0)
    assert 'maxlength="256"' in match.group(0)


def test_title_input_cap_comes_from_the_route_context(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The maxlength number is the route's constant, not a template literal."""
    monkeypatch.setattr("saneless.web.routes.TITLE_MAX_LENGTH", 99)
    match = _TITLE_INPUT.search(client.get("/").text)
    assert match is not None, "title input markup not found"
    assert 'maxlength="99"' in match.group(0)


def test_page_loads_no_app_script_and_no_remote_url(client: TestClient) -> None:
    """No application JavaScript and no off-box URL remain on the page (S4)."""
    text = client.get("/").text
    assert "app.js" not in text
    assert "http://" not in text
    assert "https://" not in text
    assert "hx-on" not in text


def test_app_script_is_gone(client: TestClient) -> None:
    """``/static/app.js`` no longer exists; its 404 uses the one renderer."""
    response = client.get("/static/app.js")
    assert response.status_code == 404
    assert response.json() == {
        "status": "error",
        "detail": rejection_message(RequestRejection.NOT_FOUND),
    }


def test_idle_page_button_and_status(client: TestClient) -> None:
    """With no job at all the button reads Scan and the status area is idle."""
    match = _SCAN_BUTTON.search(client.get("/").text)
    assert match is not None, "scan button markup not found"
    assert match.group("text").strip() == "Scan"
    assert "disabled" not in match.group("attrs")
    assert "aria-busy" not in match.group("attrs")

    status = client.get("/api/jobs/current/status").text
    assert "<p>Ready to scan.</p>" in status
    assert 'hx-trigger="every 1s"' not in status


def test_uploading_renders_its_literal_strings(client: TestClient) -> None:
    """
    UPLOADING renders its exact label and prose, independent of the filters.

    The parametrised tests above build their expectations from `state_label` /
    `progress_label`, so they would pass for any label text. This one hard-codes
    the strings, so a change to either is caught here as well as in
    tests/test_vocabulary.py.
    """
    _job_in_state(client, JobState.UPLOADING)
    assert "Uploading" in client.get("/api/jobs/history").text
    status = client.get("/api/jobs/current/status").text
    assert '<p aria-busy="true">Uploading to paperless-ngx...</p>' in status


_HISTORY_RELOAD = (
    '<div hx-get="/api/jobs/history" hx-target="#history-body" '
    'hx-swap="outerHTML" hx-trigger="load" class="htmx-hidden"></div>'
)


def test_fallback_reloads_the_history_table(client: TestClient) -> None:
    """
    FALLBACK carries the hidden history-reload div; a live state does not.

    FALLBACK is terminal, so the history table must refresh the moment the
    status area swaps to it -- exactly as it does for DONE and ERROR. Without
    the div the table keeps showing the job as Uploading until the user
    reloads the page (T-23-24).
    """
    _job_in_state(client, JobState.FALLBACK)
    assert _HISTORY_RELOAD in client.get("/api/jobs/current/status").text

    _job_in_state(client, JobState.SCANNING)
    assert _HISTORY_RELOAD not in client.get("/api/jobs/current/status").text


def test_fallback_warning_renders_inline(client: TestClient) -> None:
    """
    The warning is a second visible paragraph, not a tooltip or a hidden detail.

    A user whose title, tags and correspondent were dropped has to be told so
    without hovering anything (D-05).
    """
    _job_in_state(client, JobState.FALLBACK, warning="Metadata was not applied.")
    text = client.get("/api/jobs/current/status").text
    assert '<p class="status-fallback">Metadata was not applied.</p>' in text


def test_fallback_without_a_warning_renders_no_empty_paragraph(
    client: TestClient,
) -> None:
    """With no warning recorded, the second paragraph is omitted entirely."""
    _job_in_state(client, JobState.FALLBACK)
    text = client.get("/api/jobs/current/status").text
    assert '<p class="status-fallback">None</p>' not in text
    assert '<p class="status-fallback"></p>' not in text
    assert text.count('<p class="status-fallback">') == 1


def test_fallback_warning_is_escaped_not_injected(client: TestClient) -> None:
    """
    A warning carrying markup is escaped, never interpolated as HTML (T-23-21).

    `job.warning` is a new interpolation of a field whose text can originate
    upstream of saneless -- a paperless-ngx response body reaches it via plan
    23-04. Jinja2 autoescaping is on by default under `Jinja2Templates`, but
    "by default" is a setting, and a setting can be changed; this pins the
    property itself. If this test ever fails the fix is to re-enable
    autoescaping, never to sanitise at the call site.
    """
    _job_in_state(client, JobState.FALLBACK, warning="<script>alert(1)</script>")
    text = client.get("/api/jobs/current/status").text
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text


# How long the owed-write test waits for the worker to act.  Idle ticks run at
# 20 ms there, so this is many ticks' worth of slack.
_OWED_WRITE_BUDGET = 2.0

# The error every simulated job store failure in the owed-write test raises.
_DISK_ERROR = "disk I/O error"


def _poll_until(predicate: Callable[[], bool], budget: float) -> bool:
    """
    Poll ``predicate`` until it holds or ``budget`` seconds pass.

    Returns:
        Whether the predicate held before the budget ran out.

    """
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _BreakableWrite:
    """
    A job store write that raises ``sqlite3.OperationalError`` while broken.

    Every call is counted; while ``broken`` is set the call raises, otherwise
    it delegates to the real method, so the row the worker writes is the row
    the status poll reads back.

    Args:
        original: The bound store method being replaced.
        broken: Set while the store should refuse writes.

    """

    def __init__(
        self, original: Callable[..., object], broken: threading.Event
    ) -> None:
        """Wrap ``original``, failing while ``broken`` is set."""
        self._original = original
        self._broken = broken
        self._lock = threading.Lock()
        self._calls = 0

    @property
    def calls(self) -> int:
        """How many times the write has been called so far."""
        with self._lock:
            return self._calls

    def __call__(self, *args: object, **kwargs: object) -> object:
        """Count the call, then raise or delegate."""
        with self._lock:
            self._calls += 1
        if self._broken.is_set():
            raise sqlite3.OperationalError(_DISK_ERROR)
        return self._original(*args, **kwargs)


def test_status_poll_reenables_the_scan_button_once_an_owed_failure_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A job the guard could not end stops disabling Scan once the store heals.

    ROBU-01 success criterion 1, CR-01, D-12: one loop-level failure whose
    best-effort ERROR write also failed leaves the row active and the button
    disabled while ``/health`` stays 200.  The streak limit is raised here so
    the worker never degrades and no probe runs; the owed write must still
    land on an idle tick, and the next status poll must render ``#scan-btn``
    enabled with no restart and no scan.
    """
    # Before the lifespan starts the worker: patched later, it would sit in a
    # five-second queue wait before the first fast tick.
    monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", 0.02)
    # This test watches /health stay 200 through a failing window; the streak
    # degrade is proven by
    # test_health_reports_the_job_store_failing_while_an_owed_failure_cannot_be_written.
    monkeypatch.setattr("saneless.worker._OWED_RETRY_DEGRADED_AFTER", 1_000_000)
    app = _make_app(tmp_path)
    broken = threading.Event()
    broken.set()

    with TestClient(app) as tc:
        job_store: JobStore = app.state.job_store
        updates = _BreakableWrite(job_store.update_state, broken)
        finishes = _BreakableWrite(job_store.finish_job, broken)
        monkeypatch.setattr(job_store, "update_state", updates)
        monkeypatch.setattr(job_store, "finish_job", finishes)

        response = tc.post("/api/scan", data={"profile": "default"})
        assert response.status_code == 200
        job_id = job_store.list_recent(limit=1)[0].id

        # The guard's write, then at least one idle-tick retry.
        assert _poll_until(lambda: finishes.calls >= 2, _OWED_WRITE_BUDGET)
        stuck = _only_scan_button(tc.get("/api/jobs/current/status").text)
        assert "disabled" in stuck.group("attrs")
        assert tc.get("/health").status_code == 200

        broken.clear()

        def button_enabled() -> bool:
            text = tc.get("/api/jobs/current/status").text
            return "disabled" not in _only_scan_button(text).group("attrs")

        assert _poll_until(button_enabled, _OWED_WRITE_BUDGET)
        finished = job_store.get_job(job_id)
        assert finished is not None
        assert finished.state is JobState.ERROR
        assert tc.get("/health").status_code == 200


def test_health_reports_the_job_store_failing_while_an_owed_failure_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A job store that keeps refusing an owed write shows on ``/health``.

    ROBU-01 success criterion 1, WR-10, D-10, D-12: the job's SCANNING write
    and the guard's ERROR write fail, and every idle retry of the owed write
    fails too.  After a short streak of failed retries the worker degrades, so
    ``/health`` answers 503 "job store failing" and a new scan is refused with
    503.  Once the store heals, the recovery probe and the owed write land:
    ``/health`` is 200 again and the status poll renders ``#scan-btn`` enabled.
    """
    # Before the lifespan starts the worker: patched later, it would sit in a
    # five-second queue wait before the first fast tick.
    monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", 0.02)
    app = _make_app(tmp_path)
    broken = threading.Event()
    broken.set()

    with TestClient(app) as tc:
        job_store: JobStore = app.state.job_store
        updates = _BreakableWrite(job_store.update_state, broken)
        finishes = _BreakableWrite(job_store.finish_job, broken)
        monkeypatch.setattr(job_store, "update_state", updates)
        monkeypatch.setattr(job_store, "finish_job", finishes)

        response = tc.post("/api/scan", data={"profile": "default"})
        assert response.status_code == 200
        job_id = job_store.list_recent(limit=1)[0].id

        assert _poll_until(
            lambda: tc.get("/health").status_code == 503, _OWED_WRITE_BUDGET
        )
        assert tc.get("/health").json() == {
            "status": "error",
            "detail": "job store failing",
        }
        refused = tc.post("/api/scan", data={"profile": "default"})
        assert refused.status_code == 503

        broken.clear()

        assert _poll_until(
            lambda: tc.get("/health").status_code == 200, _OWED_WRITE_BUDGET
        )

        def button_enabled() -> bool:
            text = tc.get("/api/jobs/current/status").text
            return "disabled" not in _only_scan_button(text).group("attrs")

        assert _poll_until(button_enabled, _OWED_WRITE_BUDGET)
        finished = job_store.get_job(job_id)
        assert finished is not None
        assert finished.state is JobState.ERROR
