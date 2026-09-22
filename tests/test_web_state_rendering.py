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
from saneless.job import JobResult
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
    page_counts,
    progress_label,
    rejection_message,
    state_label,
)
from saneless.web import app as app_module
from saneless.web.app import create_app
from tests.conftest import StubScannerBackend, poll_until

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


# The paperless-ngx credential a configured appliance has. Anything outside
# `config.PLACEHOLDER_TOKENS` counts as real: the predicate is a fixed literal
# set and never a shape heuristic (D-14).
_REAL_CREDENTIAL = "test-token"

# The shipped stand-in the predicate refuses -- docker-compose.yml and the
# docker reference both carry it, so it is the placeholder a real installation
# is most likely to be left with (D-14, APPL-07).
_SHIPPED_PLACEHOLDER = "changeme"


def _make_app(tmp_path: Path, *, credential: str = _REAL_CREDENTIAL) -> FastAPI:
    """
    Build a real app with a stub scanner and no network calls, not yet started.

    Args:
        tmp_path: The directory the app writes its database and files under.
        credential: The paperless-ngx token this appliance is configured with.
            The default is a real one; pass `_SHIPPED_PLACEHOLDER` for the
            blocked-Scan-button case (UI-SPEC S8).

    Returns:
        The app, whose lifespan (and so its worker) starts with its TestClient.

    """
    settings = Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(url="http://localhost:8000", token=credential),
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


@pytest.fixture
def blocked_client(tmp_path: Path) -> Iterator[TestClient]:
    """
    TestClient over an appliance whose paperless-ngx token is a placeholder.

    The verdict is derived from `Settings`, which is read once at process
    start, so a second app is the only honest way to drive the blocked case:
    there is no runtime setter to reach for (UI-SPEC S8).
    """
    with TestClient(_make_app(tmp_path, credential=_SHIPPED_PLACEHOLDER)) as tc:
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
    is mandatory: the selects and refresh buttons inside the form would
    otherwise inherit it and disable the button for the length of their own
    requests (C-10).
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


# A scan that produced twelve pages, discarded two as blank and sent ten on.
_COUNTS = JobResult(
    outcome=None,
    warning=None,
    pages_scanned=12,
    pages_removed=2,
    pages_uploaded=10,
)

# The one-page scan that measured no blanks. Its zero is a true measurement and
# must render as 0; only a NULL suppresses the sentence (D-32, Pitfall 4).
_ONE_PAGE = JobResult(
    outcome=None,
    warning=None,
    pages_scanned=1,
    pages_removed=0,
    pages_uploaded=1,
)

# The six terminal cases UI-SPEC S3 enumerates, and what each records. Only the
# first two record counts: `worker.py` leaves them NULL on error and on cancel,
# `job.py` leaves them NULL on a refused submit, and no pre-Phase-23 row was
# ever backfilled. Four of six is the common path, not an edge.
_TERMINAL_CASES = [
    pytest.param(JobState.DONE, None, _COUNTS, id="done"),
    pytest.param(JobState.FALLBACK, None, _COUNTS, id="fallback"),
    pytest.param(JobState.ERROR, _DEFAULT_ERROR_CATEGORY, None, id="error"),
    pytest.param(JobState.CANCELLED, None, None, id="cancelled"),
    pytest.param(JobState.ERROR, ErrorCategory.REJECTED, None, id="rejected"),
    pytest.param(JobState.DONE, None, None, id="pre-phase-23"),
]

# The two branches that record counts, so the parametrised expectation is one
# membership test rather than a second copy of the table above.
_CASES_WITH_COUNTS = {"done", "fallback"}


def _finished_job(
    client: TestClient,
    state: JobState,
    result: JobResult | None,
    error_category: ErrorCategory | None = None,
) -> str:
    """
    Finish a job in `state`, recording `result`'s counts or leaving them NULL.

    ``finish_job`` is the only writer of the count columns, and omitting the
    result leaves all three NULL rather than zero -- which is exactly how the
    four countless terminal cases arise in production.

    Returns:
        The job's id.

    """
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title="Render Test")
    job_store.finish_job(
        job.id,
        state,
        result=result,
        error="disk on fire",
        error_category=error_category,
    )
    _adopt_as_current_job(client, job.id)
    return job.id


class TestPageCounts:
    """
    Where the page counts render, and where they deliberately do not (APPL-03).

    UI-SPEC S3, D-32. Success criterion 3 means "every terminal job that
    recorded counts": ERROR and CANCELLED rows have none by construction, and
    D-32 accepts that, so a later reader need not chase a phantom.
    """

    @pytest.mark.parametrize(("state", "category", "result"), _TERMINAL_CASES)
    def test_pages_render_for_exactly_the_cases_that_recorded_them(
        self,
        client: TestClient,
        request: pytest.FixtureRequest,
        state: JobState,
        category: ErrorCategory | None,
        result: JobResult | None,
    ) -> None:
        """
        Two of the six terminal cases show counts; four show no element at all.

        Not an empty paragraph and not a blank line -- nothing. A sentence
        naming two of three counts would invite the reader to wonder about the
        third, so one NULL suppresses the whole line.
        """
        case = request.node.callspec.id
        _finished_job(client, state, result, category)
        text = client.get("/api/jobs/current/status").text

        expected = case in _CASES_WITH_COUNTS
        assert ('<p class="page-counts">' in text) is expected
        assert ("page-counts" in text) is expected

    def test_pages_sentence_is_the_shared_filter_verbatim(
        self, client: TestClient
    ) -> None:
        """The status area shows what `page_counts` built, not its own wording."""
        _finished_job(client, JobState.DONE, _COUNTS)
        sentence = page_counts(_COUNTS)
        assert sentence is not None
        text = client.get("/api/jobs/current/status").text
        assert f'<p class="page-counts">{sentence}</p>' in text
        assert "12 pages scanned, 2 blank removed, 10 uploaded" in text

    def test_a_measured_zero_pages_renders_as_zero(self, client: TestClient) -> None:
        """
        A scan where nothing was blank really did remove 0 pages (D-32).

        The guard is the filter returning None, never truthiness; a truthiness
        guard anywhere on this path would delete this line.
        """
        _finished_job(client, JobState.DONE, _ONE_PAGE)
        text = client.get("/api/jobs/current/status").text
        assert (
            '<p class="page-counts">1 page scanned, 0 blank removed, 1 uploaded</p>'
            in text
        )

    def test_the_pages_line_follows_the_outcome_and_precedes_the_thumbnail(
        self, client: TestClient
    ) -> None:
        """
        The counts are the last paragraph of the terminal branch (UI-SPEC S3).

        They are a footnote to the outcome, so they read after it -- and before
        the preview, which is the end of the status area.
        """
        job_id = _finished_job(client, JobState.DONE, _COUNTS)
        _app(client).state.job_store.update_thumbnail(job_id, "dGVzdA==")
        text = client.get("/api/jobs/current/status").text

        assert text.index('class="status-done"') < text.index('class="page-counts"')
        assert text.index('class="page-counts"') < text.index('class="thumbnail"')

    def test_the_fallback_pages_line_follows_the_warning(
        self, client: TestClient
    ) -> None:
        """Under FALLBACK the counts read after the warning, not between it and the outcome."""
        job_store: JobStore = _app(client).state.job_store
        job = job_store.create_job(profile="default", title="Render Test")
        job_store.finish_job(
            job.id,
            JobState.FALLBACK,
            result=JobResult(
                outcome=None,
                warning="Metadata was not applied.",
                pages_scanned=12,
                pages_removed=2,
                pages_uploaded=10,
            ),
        )
        _adopt_as_current_job(client, job.id)
        text = client.get("/api/jobs/current/status").text

        assert text.index("Metadata was not applied.") < text.index(
            'class="page-counts"'
        )

    def test_the_history_title_cell_carries_the_pages_as_a_second_line(
        self, client: TestClient
    ) -> None:
        """
        The counts are a second line in the Title cell, not a fifth column.

        A span with `display: block`, so it forms its own line inside the cell
        the mobile rule already wraps (UI-SPEC S3).
        """
        _finished_job(client, JobState.DONE, _COUNTS)
        sentence = page_counts(_COUNTS)
        assert sentence is not None
        text = client.get("/api/jobs/history").text
        assert f'<span class="page-counts">{sentence}</span>' in text

    def test_the_history_cell_omits_the_pages_element_entirely(
        self, client: TestClient
    ) -> None:
        """A row with no counts renders no span, not an empty one."""
        _finished_job(client, JobState.CANCELLED, None)
        text = client.get("/api/jobs/history").text
        assert "page-counts" not in text

    def test_the_history_table_still_has_four_columns(self) -> None:
        """
        Pitfall 11 is designed out, not guarded against (UI-SPEC S3).

        A fifth column would drift against `colspan`, and would compete with
        the Time column S7 simultaneously widens.
        """
        index = (_TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
        history = (_TEMPLATES_DIR / "partials" / "history.html").read_text(
            encoding="utf-8"
        )
        assert index.count("<th>") == 4
        assert history.count('colspan="4"') == 1

    def test_no_template_guards_a_page_count_with_truthiness(self) -> None:
        """
        The guard is the filter returning None, never a count's truthiness.

        `{% if job.pages_removed %}` would silently delete a true zero, so no
        template is allowed to write one (D-32, Pitfall 4).
        """
        offenders = sorted(
            path.name
            for path in _TEMPLATES_DIR.rglob("*.html")
            if "if job.pages_" in path.read_text(encoding="utf-8")
        )
        assert offenders == []

    def test_the_stylesheet_gains_one_muted_pages_rule(self) -> None:
        """
        One class, one rule, no new colour literal (UI-SPEC S3).

        Muted rather than a status colour, because a count is a measurement and
        not an outcome.
        """
        css = _APP_CSS.read_text(encoding="utf-8")
        assert css.count(".page-counts") == 1
        assert "display: block;" in css
        assert "color: var(--pico-muted-color);" in css


class TestHistoryTimeCell:
    """The history Time cell names its zone (APPL-12, D-34, D-35, UI-SPEC S7)."""

    def test_the_time_cell_names_the_zone(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A copy-pasted line is self-describing, so the zone is on every row.

        `local_time` renders whatever zone the C library reports, so pinning
        `TZ` and calling `time.tzset()` is the only way to assert a shape on a
        host in an unknown zone; the trailing `tzset` makes the library notice
        monkeypatch's teardown.
        """
        monkeypatch.setenv("TZ", "America/Chicago")
        time.tzset()
        try:
            _finished_job(client, JobState.DONE, _COUNTS)
            text = client.get("/api/jobs/history").text
            cell = re.search(r"<tr>\s*<td>([^<]*)</td>", text)
            assert cell is not None, "no history row rendered"
            assert re.fullmatch(
                r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+", cell.group(1).strip()
            )
        finally:
            monkeypatch.undo()
            time.tzset()

    def test_the_time_cell_renders_through_the_shared_filter(self) -> None:
        """
        No `strftime` survives in the template: one function serves both surfaces.

        `cli.py` imports the same object, so the web table and the CLI table
        cannot disagree about the zone or the format without editing the one
        implementation (UI-SPEC S7).
        """
        source = (_TEMPLATES_DIR / "partials" / "history.html").read_text(
            encoding="utf-8"
        )
        assert "strftime" not in source
        assert source.count("local_time") == 1


# How long the owed-write test waits for the worker to act.  Idle ticks run at
# 20 ms there, so this is many ticks' worth of slack.
_OWED_WRITE_BUDGET = 2.0

# The error every simulated job store failure in the owed-write test raises.
_DISK_ERROR = "disk I/O error"


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
        assert poll_until(lambda: finishes.calls >= 2, _OWED_WRITE_BUDGET)
        stuck = _only_scan_button(tc.get("/api/jobs/current/status").text)
        assert "disabled" in stuck.group("attrs")
        assert tc.get("/health").status_code == 200

        broken.clear()

        def button_enabled() -> bool:
            text = tc.get("/api/jobs/current/status").text
            return "disabled" not in _only_scan_button(text).group("attrs")

        assert poll_until(button_enabled, _OWED_WRITE_BUDGET)
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

        assert poll_until(
            lambda: tc.get("/health").status_code == 503, _OWED_WRITE_BUDGET
        )
        assert tc.get("/health").json() == {
            "status": "error",
            "detail": "job store failing",
        }
        refused = tc.post("/api/scan", data={"profile": "default"})
        assert refused.status_code == 503

        broken.clear()

        assert poll_until(
            lambda: tc.get("/health").status_code == 200, _OWED_WRITE_BUDGET
        )

        def button_enabled() -> bool:
            text = tc.get("/api/jobs/current/status").text
            return "disabled" not in _only_scan_button(text).group("attrs")

        assert poll_until(button_enabled, _OWED_WRITE_BUDGET)
        finished = job_store.get_job(job_id)
        assert finished is not None
        assert finished.state is JobState.ERROR


# --- The owner-gated flip prompt (APPL-09, D-24, D-26, D-27) ----------------

# The cookie's wire name, spelled out rather than imported: a test that imported
# the constant would still pass if the name changed under every browser that
# already holds one.
_OWNER_COOKIE = "saneless_owner"

# The token these tests write straight onto the row.  The mint itself is pinned
# in tests/test_web.py; what is under test here is the rendering, so the row is
# staged directly and the value only has to be distinctive.
_OWNING_BROWSER = "the-browser-that-submitted-this-stack"

# A base64 payload that is not a real JPEG.  Nothing decodes it: it exists so
# both renderings carry an identical `<img>` for the identical-content
# assertion to have something after the flip block to compare.
_THUMBNAIL = "c3RhbmQtaW4="

# The exact confirmation D-27 locks: one question, one consequence, and no
# claim about the pages already scanned, which is a promise this contract
# cannot verify.
_ABORT_CONFIRMATION = "Abort this scan? It will stop and cannot be resumed."

# The copy a viewer who did not submit the job sees in place of the buttons.
_NON_OWNER_LINE = "Waiting for the stack to be flipped"

_STATUS_OPEN = re.compile(r'<div id="status-area"[^>]*>', re.DOTALL)
# The real element, not the prose reference to it in the comment above the
# status card: the element carries at least one further attribute, so it is
# the only one of the two whose open tag has whitespace after the URL.
_SCAN_FORM = re.compile(r'<form hx-post="/api/scan"\s+[^>]*>', re.DOTALL)
_THUMBNAIL_START = '<img src="data:image/jpeg;base64,'

# Any control that would let a second browser seize an answered-for job.  D-26
# says the absence of one IS the rendering, so it is asserted like any other
# contract rather than left to a reviewer's memory.
_SEIZE_CONTROL = re.compile(r"override|take[ -]over|force", re.IGNORECASE)


def _flip_job(client: TestClient, owner: str | None) -> str:
    """
    Stage an AWAITING_FLIP job with a thumbnail and the given owner token.

    Args:
        client: The client whose app owns the store and worker.
        owner: The token to record, or None for a pre-upgrade unowned row.

    Returns:
        The job's id.

    """
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(
        profile="default", title="Flip Render", owner_token=owner
    )
    job_store.update_state(job.id, JobState.AWAITING_FLIP)
    job_store.update_thumbnail(job.id, _THUMBNAIL)
    _adopt_as_current_job(client, job.id)
    return job.id


def _as_browser(client: TestClient, token: str | None) -> str:
    """
    Fetch the status area as a browser carrying `token`, and return the markup.

    The cookie is set as a header rather than through the client's jar so one
    client can stand in for two browsers without the jar carrying state from
    one request into the next.

    Args:
        client: The client to request through.
        token: The owner token to present, or None to present none.

    Returns:
        The rendered response body.

    """
    headers = {} if token is None else {"Cookie": f"{_OWNER_COOKIE}={token}"}
    response = client.get("/api/jobs/current/status", headers=headers)
    assert response.status_code == 200
    return response.text


def _around_the_flip_block(markup: str) -> tuple[str, str]:
    """
    Split the markup into what precedes and what follows the flip branch.

    The branch's output is the only thing the owner gate may change, so
    everything on either side of it -- the status area's opening tag with its
    poll attributes, the thumbnail, and the out-of-band Scan button -- must be
    identical for both viewers.

    Args:
        markup: The rendered response body.

    Returns:
        The status area's opening tag, and everything from the thumbnail on.

    """
    opening = _STATUS_OPEN.search(markup)
    assert opening is not None
    thumbnail_at = markup.index(_THUMBNAIL_START)
    return opening.group(0), markup[thumbnail_at:]


class TestOwnerGatedFlipPrompt:
    """
    Who sees the Continue and Abort buttons, and what everyone else sees.

    APPL-09 and D-24: the token gates those two buttons and nothing else.  The
    gate is server-side -- the buttons are not rendered for a non-owner, never
    hidden with CSS, which would be an ASVS V4 failure.
    """

    def test_owner_sees_the_flip_prompt_with_both_buttons(
        self, client: TestClient
    ) -> None:
        """The browser that submitted the stack gets the prompt (APPL-09)."""
        _flip_job(client, _OWNING_BROWSER)

        markup = _as_browser(client, _OWNING_BROWSER)

        assert "flip the stack over the long edge" in markup.lower()
        assert markup.count('hx-post="/api/flip/') == 2
        assert ">Continue<" in markup
        assert ">Abort scan<" in markup
        assert _NON_OWNER_LINE not in markup

    def test_non_owner_sees_the_waiting_line_and_no_flip_controls(
        self, client: TestClient
    ) -> None:
        """
        A second viewer gets the waiting copy and zero controls (D-24).

        Zero controls, not hidden ones: the gate is decided on the server and
        the markup never carries a button the viewer is not allowed to press.
        """
        _flip_job(client, _OWNING_BROWSER)

        markup = _as_browser(client, None)

        assert f'<p aria-busy="true">{_NON_OWNER_LINE}</p>' in markup
        assert markup.count('hx-post="/api/flip/') == 0
        assert "Continue" not in markup
        assert "Abort scan" not in markup

    def test_a_different_owner_token_is_also_a_non_owner(
        self, client: TestClient
    ) -> None:
        """A wrong token is no better than no token at all."""
        _flip_job(client, _OWNING_BROWSER)

        markup = _as_browser(client, "some-other-browsers-token")

        assert f'<p aria-busy="true">{_NON_OWNER_LINE}</p>' in markup
        assert markup.count('hx-post="/api/flip/') == 0

    def test_owner_and_non_owner_see_identical_markup_around_the_flip_block(
        self, client: TestClient
    ) -> None:
        """Only the flip block differs: state, poll, thumbnail and button match."""
        _flip_job(client, _OWNING_BROWSER)

        owner = _as_browser(client, _OWNING_BROWSER)
        other = _as_browser(client, None)

        assert _around_the_flip_block(owner) == _around_the_flip_block(other)

    def test_unowned_flip_job_renders_the_prompt_for_everyone(
        self, client: TestClient
    ) -> None:
        """
        A NULL owner token means unowned, so the prompt renders for all.

        This is the manual-duplex job that was already in flight when the
        appliance was upgraded; a strict rule would leave it un-continuable.
        """
        _flip_job(client, None)

        markup = _as_browser(client, None)

        assert markup.count('hx-post="/api/flip/') == 2
        assert _NON_OWNER_LINE not in markup

    def test_owner_flip_prompt_offers_no_way_to_seize_another_job(
        self, client: TestClient
    ) -> None:
        """
        There are exactly two controls and no third one (D-26).

        A human who closed the tab is the same case as a human who walked
        away, and the bounded Phase 25 flip timeout already resolves both.  The
        absence of a third control is the decision, so it is asserted.
        """
        _flip_job(client, _OWNING_BROWSER)

        markup = _as_browser(client, _OWNING_BROWSER)

        assert markup.count("<button") == markup.count('hx-post="/api/flip/') + 1
        assert _SEIZE_CONTROL.search(markup) is None

    def test_non_owner_flip_rendering_offers_nothing_to_seize_with_either(
        self, client: TestClient
    ) -> None:
        """The waiting viewer is offered no control of any kind (D-26)."""
        _flip_job(client, _OWNING_BROWSER)

        markup = _as_browser(client, None)

        assert _SEIZE_CONTROL.search(markup) is None

    def test_non_owner_flip_line_carries_no_trailing_ellipsis(
        self, client: TestClient
    ) -> None:
        """
        The locked copy ends without one, against the usual in-progress style.

        The user wrote this string twice; D-24 treats it as locked copy and
        this test records the style exception rather than letting a later
        tidy-up "fix" it.
        """
        _flip_job(client, _OWNING_BROWSER)

        markup = _as_browser(client, None)

        assert f"{_NON_OWNER_LINE}</p>" in markup
        assert f"{_NON_OWNER_LINE}..." not in markup
        assert f"{_NON_OWNER_LINE}&#8230;" not in markup

    def test_flip_abort_button_carries_the_exact_confirmation(
        self, client: TestClient
    ) -> None:
        """Abort asks first, in D-27's exact words (APPL-09)."""
        _flip_job(client, _OWNING_BROWSER)

        markup = _as_browser(client, _OWNING_BROWSER)

        assert f'hx-confirm="{_ABORT_CONFIRMATION}"' in markup
        assert markup.count("hx-confirm") == 1

    def test_flip_confirmation_is_on_the_button_not_the_scan_form(self) -> None:
        """
        The scan form is untouched, so the C-10 inheritance fix still holds.

        Putting the confirmation on the form would make every child request
        confirm, and would need the form's inheritance list extended -- the
        exact landmine Phase 26 spent a regression test on.
        """
        index = (_TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
        flip = (_TEMPLATES_DIR / "partials" / "flip.html").read_text(encoding="utf-8")

        form = _SCAN_FORM.search(index)
        assert form is not None
        assert "hx-confirm" not in index
        assert 'hx-disinherit="hx-disabled-elt"' in form.group(0)
        assert flip.count("hx-confirm") == 1


# --- S8: the blocked Scan button and its reason line (APPL-07, D-14, D-15) ---

# The reason line's copy, verbatim from UI-SPEC S8. Pinned here as a literal so
# a change to the constant is caught by a test and not only by a reviewer; that
# the constant lives in Python and not in a template is asserted below.
_BLOCKED_REASON = (
    "The paperless-ngx API token has not been set \N{EM DASH} see System status above."
)

# The reason element, whole. `<small>` nests nothing here, so a non-greedy body
# is exact rather than merely convenient.
_REASON_LINE = re.compile(
    r'<small id="scan-blocked-reason" class="status-error">(?P<text>.*?)</small>',
    re.DOTALL,
)

# The scan form's opening tag. The whitespace after the URL is required because
# the comment above the status card mentions `<form hx-post="/api/scan">` in
# prose, and a looser pattern matches that instead of the element.
_SCAN_FORM_TAG = re.compile(r'<form hx-post="/api/scan"\s+(?P<attrs>[^>]*)>')

# The four attributes the scan form carries, in order. Phase 30 adds none:
# `hx-disinherit` is the C-10 fix and `hx-disabled-elt` is what it protects, so
# the list is asserted whole rather than by membership.
_SCAN_FORM_ATTRS = [
    'hx-target="#status-area"',
    'hx-swap="outerHTML"',
    'hx-disabled-elt="#scan-btn"',
    'hx-disinherit="hx-disabled-elt"',
]

_DESCRIBED_BY = 'aria-describedby="scan-blocked-reason"'

# The button's opening literal, which `_SCAN_BUTTON` above depends on.
_PINNED_BUTTON_OPENING = '<button type="submit" id="scan-btn"'

# The package root, for the assertions that follow the copy rather than markup.
_SRC_DIR = _PACKAGE_DIR.parent


def _button_partial() -> str:
    """
    Return the one copy of the Scan button markup.

    Returns:
        The partial's source, header comment included.

    """
    return (_TEMPLATES_DIR / "partials" / "scan_button.html").read_text(
        encoding="utf-8"
    )


def _css_rule(selector: str) -> str:
    """
    Return the body of the one CSS rule with `selector`.

    Args:
        selector: The selector, without its opening brace.

    Returns:
        Everything between that rule's braces.

    """
    css = _APP_CSS.read_text(encoding="utf-8")
    opening = f"{selector} {{"
    assert css.count(opening) == 1, f"expected exactly one {selector} rule"
    body = css[css.index(opening) + len(opening) :]
    return body[: body.index("}")]


def _done_job_that_is_not_current(client: TestClient, title: str) -> str:
    """
    Create a job already DONE, leaving the worker's current job alone.

    `_job_in_state` adopts the row it makes as the worker's current job, which
    is exactly what the two-browser cases below must not do: they need a second
    row that ran and ended while something else is still running. The module's
    own `_finished_job` above records page counts and is about the history
    table, so it is not this.

    Args:
        client: The client whose app owns the store.
        title: The title the status area will report for it.

    Returns:
        The job's id.

    """
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title=title)
    job_store.update_state(job.id, JobState.DONE)
    return job.id


class TestScanBlocked:
    """
    The placeholder token disables the Scan button and says why (UI-SPEC S8).

    The button is the courtesy, never the enforcement: every test here is about
    what a viewer is shown, and the refusal that actually holds is proved
    against the route in tests/test_web_errors.py. Exactly one condition blocks
    the button in this phase -- a scanner that is unreachable or a
    paperless-ngx that is down does not, because the user is allowed to try and
    will get a plain-language error with a next step.
    """

    def test_blocked_idle_button_is_disabled_and_still_says_scan(
        self, blocked_client: TestClient
    ) -> None:
        """A blocked appliance with nothing running still offers one word."""
        match = _only_scan_button(blocked_client.get("/").text)
        attrs = match.group("attrs")

        assert "disabled" in attrs
        assert _DESCRIBED_BY in attrs
        assert "aria-busy" not in attrs
        assert match.group("text").strip() == "Scan"

    @pytest.mark.parametrize("state", list(JobState))
    def test_blocked_button_leaves_the_job_label_and_aria_busy_alone(
        self, blocked_client: TestClient, state: JobState
    ) -> None:
        """
        The flag is a second `disabled` source and touches nothing else.

        Phase 26's table is re-asserted underneath it: the label and
        `aria-busy` still come from the job, for every state.
        """
        _job_in_state(blocked_client, state)
        match = _only_scan_button(blocked_client.get("/").text)
        attrs = match.group("attrs")

        assert "disabled" in attrs
        assert _DESCRIBED_BY in attrs
        assert ('aria-busy="true"' in attrs) is (state in BUSY_STATES)

        if state is JobState.AWAITING_FLIP:
            expected = "Waiting for flip&#8230;"
        elif state in BUSY_STATES:
            expected = "Scanning&#8230;"
        else:
            expected = "Scan"
        assert match.group("text").strip() == expected

    def test_an_unblocked_page_carries_no_describedby_and_no_reason(
        self, client: TestClient
    ) -> None:
        """A configured appliance is exactly as it was before this plan."""
        page = client.get("/").text
        match = _only_scan_button(page)

        assert _DESCRIBED_BY not in match.group("attrs")
        assert "scan-blocked-reason" not in page
        assert _REASON_LINE.search(page) is None

    def test_blocked_reason_line_renders_the_exact_copy(
        self, blocked_client: TestClient
    ) -> None:
        """The reason is S8's sentence, once, as visible text."""
        page = blocked_client.get("/").text
        rendered = _REASON_LINE.findall(page)

        assert len(rendered) == 1
        assert rendered[0].strip() == _BLOCKED_REASON

    def test_blocked_reason_line_follows_the_button_inside_the_form(
        self, blocked_client: TestClient
    ) -> None:
        """It reads directly after the control it explains, inside the form."""
        page = blocked_client.get("/").text
        form = page[page.index("<form hx-post=") : page.index("</form>")]

        assert 'id="scan-btn"' in form
        assert 'id="scan-blocked-reason"' in form
        assert form.index('id="scan-btn"') < form.index('id="scan-blocked-reason"')

    def test_blocked_button_stays_disabled_across_repeated_status_polls(
        self, blocked_client: TestClient
    ) -> None:
        """
        No status response can hand back an enabled button (C-10, T-30-66).

        This is the regression guard for the new flag specifically: the button
        is re-rendered from server state every second, so a flag expressed
        anywhere but the one partial would be dropped by exactly these polls.
        """
        for _ in range(5):
            match = _only_scan_button(
                blocked_client.get("/api/jobs/current/status").text
            )
            assert "disabled" in match.group("attrs")
            assert _DESCRIBED_BY in match.group("attrs")

    def test_blocked_followed_job_poll_carries_the_blocked_button(
        self, blocked_client: TestClient
    ) -> None:
        """The per-browser poll route carries it too, not just the shared one."""
        job_id = _job_in_state(blocked_client, JobState.DONE)
        match = _only_scan_button(blocked_client.get(f"/api/jobs/{job_id}/status").text)

        assert "disabled" in match.group("attrs")
        assert _DESCRIBED_BY in match.group("attrs")

    @pytest.mark.parametrize("route", ["continue", "abort"], ids=["continue", "abort"])
    def test_blocked_flip_responses_carry_the_blocked_button(
        self, blocked_client: TestClient, route: str
    ) -> None:
        """Both flip answers re-render the button, and both keep it blocked."""
        job_id = _job_in_state(blocked_client, JobState.AWAITING_FLIP)
        response = blocked_client.post(f"/api/flip/{route}", data={"job_id": job_id})
        match = _only_scan_button(response.text)

        assert "disabled" in match.group("attrs")
        assert _DESCRIBED_BY in match.group("attrs")

    def test_checks_refresh_carries_no_button_and_no_blocked_reason(
        self, blocked_client: TestClient
    ) -> None:
        """
        Re-running the checks cannot change a verdict read from Settings (S8).

        So the refresh response carries neither the button nor the reason:
        re-rendering them there would be theatre, and it is why
        `#scan-blocked-reason` is never an out-of-band swap target and never
        has to exist as an empty placeholder.
        """
        text = blocked_client.post("/api/checks/refresh").text

        assert 'id="scan-btn"' not in text
        assert 'id="scan-blocked-reason"' not in text

    def test_blocked_reason_is_never_an_out_of_band_target(
        self, blocked_client: TestClient
    ) -> None:
        """
        The reason element exists only on the full page and never swaps itself.

        The *id* still appears in every status response, as the button's
        `aria-describedby` value: that is the point of the attribute. What must
        never appear is a second copy of the element, out-of-band or otherwise.
        """
        page = blocked_client.get("/").text
        reason = _REASON_LINE.search(page)
        assert reason is not None
        assert "hx-swap-oob" not in reason.group(0)
        assert page.count('id="scan-blocked-reason"') == 1

        for path in ("/api/jobs/current/status", "/api/jobs/history", "/api/checks"):
            text = blocked_client.get(path).text
            assert 'id="scan-blocked-reason"' not in text
            assert _REASON_LINE.search(text) is None

    def test_the_blocked_button_keeps_its_pinned_first_two_attributes(
        self, blocked_client: TestClient
    ) -> None:
        """
        `type="submit" id="scan-btn"` stay first, in that order (ROBU-04).

        The `_SCAN_BUTTON` regex in this module depends on it, and so does the
        partial's own header comment.

        Asserted against the partial's first line of markup rather than a fixed
        number of leading lines, because the header comment above it is
        deliberately long and is meant to grow.
        """
        markup = [
            line for line in _button_partial().splitlines() if line.startswith("<")
        ]
        assert markup, "no markup in the button partial"
        assert markup[0].startswith(_PINNED_BUTTON_OPENING)

        page = blocked_client.get("/").text
        assert page.count(_PINNED_BUTTON_OPENING) == 1
        assert _only_scan_button(page).group("text").strip() == "Scan"

    def test_the_scan_form_element_gains_no_attribute(self) -> None:
        """
        The form is byte-identical to before this plan (C-10, UI-SPEC S8).

        The blocked state lives in the button partial and nowhere else, so the
        inheritance fix and its comment are untouched.
        """
        index = (_TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
        match = _SCAN_FORM_TAG.search(index)

        assert match is not None
        assert match.group("attrs").split() == _SCAN_FORM_ATTRS

    def test_no_template_reaches_for_aria_disabled_or_a_tooltip(self) -> None:
        """
        The reason is visible text, not a tooltip and not `aria-disabled` (S8).

        A `disabled` button is not focusable and cannot be reliably hovered, so
        a `title` is unreachable by keyboard and unreliable on touch. Switching
        to `aria-disabled="true"` to make it focusable is forbidden: it would
        break Phase 26's `disabled` contract and `hx-disabled-elt`.
        """
        for path in sorted(_TEMPLATES_DIR.rglob("*.html")):
            assert "aria-disabled" not in path.read_text(encoding="utf-8"), path
        assert "title=" not in _button_partial()

    def test_the_blocked_reason_copy_lives_in_python_and_in_no_template(self) -> None:
        """
        The sentence is a developer constant, never composed in a template.

        It names the problem and nothing else: not the token value, not the
        paperless-ngx URL, which may carry credentials, and no exception text
        (ASVS V7, T-30-65).
        """
        carrying = sorted(
            path
            for path in _SRC_DIR.rglob("*")
            if path.is_file()
            and path.suffix in {".py", ".html", ".css"}
            and _BLOCKED_REASON in path.read_text(encoding="utf-8")
        )

        assert len(carrying) == 1, carrying
        assert carrying[0].suffix == ".py"

    def test_the_blocked_reason_rule_adds_no_colour(self) -> None:
        """
        The line is a block with a top margin and borrows `.status-error`.

        `--pico-del-color` on the card measures 7.83:1 light and 5.60:1 dark,
        already asserted in tests/test_browser.py. No new token, no new value.
        """
        rule = _css_rule("#scan-blocked-reason")

        assert "display: block;" in rule
        assert "margin-top: 0.5rem;" in rule
        assert "color" not in rule
        assert "#" not in rule


class TestScanButtonFollowsTheRenderedJob:
    """
    The button's job-derived state describes the job the page is reporting.

    D-25 made the status area follow the job *this* browser submitted, and the
    out-of-band Scan button is rendered from the same context. A browser whose
    own job has finished therefore sees an enabled button while somebody else's
    job is still running, where before this phase every viewer's button was
    disabled whenever any job was active.

    That is kept deliberately, and pinned here. Keying `disabled` on a
    different job from the one the status area reports would let the button
    read "Scanning..." directly above "Done: ...", which is the page
    contradicting itself -- the untruthfulness this milestone exists to remove.
    The appliance queues, which is the premise APPL-08's queue line rests on,
    so a browser whose scan has finished is allowed to start another and will
    be told where it lands. The guard against over-submission is unchanged: the
    worker's QUEUE_FULL refusal and its REJECTED row.
    """

    def test_a_finished_followed_job_leaves_the_button_enabled(
        self, client: TestClient
    ) -> None:
        """A browser whose own scan is done may start another one."""
        _job_in_state(client, JobState.SCANNING)
        finished = _done_job_that_is_not_current(client, "Mine, Already Done")

        text = client.get(f"/api/jobs/{finished}/status").text
        match = _only_scan_button(text)

        assert "Mine, Already Done" in text
        assert "disabled" not in match.group("attrs")
        assert match.group("text").strip() == "Scan"

    def test_a_browser_that_submitted_nothing_still_sees_the_appliance_busy(
        self, client: TestClient
    ) -> None:
        """The shared poll route is unchanged: it reports the running job."""
        _job_in_state(client, JobState.SCANNING)
        _done_job_that_is_not_current(client, "Somebody Else's, Already Done")

        match = _only_scan_button(client.get("/api/jobs/current/status").text)

        assert "disabled" in match.group("attrs")
        assert match.group("text").strip() == "Scanning&#8230;"

    def test_the_button_never_describes_a_job_the_status_area_is_not_showing(
        self, client: TestClient
    ) -> None:
        """One response, one job: the label and the prose cannot disagree."""
        _job_in_state(client, JobState.SCANNING)
        finished = _done_job_that_is_not_current(client, "Mine, Already Done")

        text = client.get(f"/api/jobs/{finished}/status").text

        assert "Render Test" not in text
        assert "Scanning&#8230;" not in text

    def test_a_blocked_appliance_disables_even_a_finished_followed_job(
        self, blocked_client: TestClient
    ) -> None:
        """
        The placeholder flag is independent of which job is rendered.

        It is the OR that makes the courtesy hold for every viewer, whatever
        their own job did.
        """
        _job_in_state(blocked_client, JobState.SCANNING)
        finished = _done_job_that_is_not_current(blocked_client, "Mine, Already Done")

        match = _only_scan_button(
            blocked_client.get(f"/api/jobs/{finished}/status").text
        )

        assert "disabled" in match.group("attrs")
        assert _DESCRIBED_BY in match.group("attrs")


# Every help line on the scan form, keyed by the id the control points at
# (UI-SPEC S6). Profile is deliberately not here: its help line is the live
# description plan 30-15 built, and a second one would be a second line under
# one control.
_HELP_TEXT = {
    "title-help": "What this document should be called in paperless-ngx.",
    "tag-filter-help": "Type to narrow the list. Ticked tags stay ticked.",
    "tags-help": "Labels to file this under in paperless-ngx. Optional.",
    "correspondent-help": "Who sent this document? Optional.",
}

# The identified help slots the idle page renders, in document order. Profile's
# leads because its control does; `#scan-blocked-reason` is absent because this
# appliance's token is real, and the tag list's empty-state line carries no id
# because it is a state of the list and not a help line for a control.
_HELP_SLOT_IDS = [
    "profile-description",
    "title-help",
    "tag-filter-help",
    "tags-help",
    "correspondent-help",
]

# Pico styles a help line through `:where(input,select,textarea,fieldset)+small`,
# so each slot has to be its control's *adjacent* sibling. The markup that must
# sit immediately before each one, whitespace aside.
_HELP_ADJACENCY = [
    (r'id="title-input"[^>]*>', "title-help"),
    (r'id="tag-filter"[^>]*>', "tag-filter-help"),
    (r"</fieldset>", "tags-help"),
    (r"</select>", "correspondent-help"),
]

# The filter box, whole, as the page renders it.
_TAG_FILTER_INPUT = re.compile(r'<input[^>]*id="tag-filter"[^>]*>')

# The filter's own form: empty, and with no submit button of its own.
_TAG_FILTER_FORM = re.compile(
    r'<form id="tag-filter-form"(?P<attrs>[^>]*)>(?P<body>.*?)</form>', re.DOTALL
)

# The tag list's wrapper, which the partial renders and every swap replaces.
_TAGS_LIST = re.compile(r'<div id="tags-list"[^>]*>')
_CORRESPONDENT_SELECT = re.compile(
    r'<select name="correspondent" id="correspondent-select"(?P<attrs>[^>]*)>'
)


class TestFormHelpTextAndTagPicker:
    """
    UI-SPEC S6: one plain-words line per control, and a filter that cannot scan.

    Two of these assertions exist because of hazards the design removes rather
    than guards against. The filter input's HTML form owner is a separate empty
    form, so Enter in it filters instead of starting a scan and a scan can never
    carry the filter text (A-6); and nothing at all is added to the scan form
    element, so the C-10 inheritance fix stays byte-identical (Pitfall 8).
    """

    def test_every_control_has_one_help_line_wired_with_aria_describedby(
        self, client: TestClient
    ) -> None:
        """Four sentences, four slots, four references -- one each (APPL-10)."""
        page = client.get("/").text

        for slot, sentence in _HELP_TEXT.items():
            assert page.count(f'<small id="{slot}">{sentence}</small>') == 1, slot
            assert page.count(f'aria-describedby="{slot}"') == 1, slot

    def test_the_help_lines_are_the_only_identified_small_elements(
        self, client: TestClient
    ) -> None:
        """
        Exactly five slots, in order, and Profile's is the live description.

        Asserted as the whole list rather than by membership, so a second help
        line under any one control fails here -- Profile's included, where the
        description plan 30-15 built already is the line.
        """
        page = client.get("/").text

        assert re.findall(r'<small id="([^"]+)"', page) == _HELP_SLOT_IDS

    def test_each_help_line_is_its_control_s_adjacent_sibling(
        self, client: TestClient
    ) -> None:
        """Pico's `+small` rule is what makes a help line look like one."""
        page = client.get("/").text

        for before, slot in _HELP_ADJACENCY:
            assert re.search(before + r'\s*<small id="' + slot + '"', page), slot

    def test_the_title_placeholder_survives_its_new_help_line(
        self, client: TestClient
    ) -> None:
        """The two say different things, so neither replaces the other."""
        match = _TITLE_INPUT.search(client.get("/").text)

        assert match is not None
        assert 'placeholder="Document title (auto-generated if empty)"' in match.group(
            0
        )
        assert 'aria-describedby="title-help"' in match.group(0)

    def test_the_tag_filter_input_carries_its_form_owner_and_htmx_wiring(
        self, client: TestClient
    ) -> None:
        """The seven attributes S6 specifies, the form owner first (A-6, D-31)."""
        match = _TAG_FILTER_INPUT.search(client.get("/").text)

        assert match is not None, "tag filter input not rendered"
        for attribute in (
            'type="search"',
            'name="q"',
            'form="tag-filter-form"',
            'hx-get="/api/tags"',
            'hx-target="#tags-list"',
            'hx-swap="outerHTML"',
            'hx-trigger="keyup changed delay:300ms"',
            'hx-include="#tags-list"',
        ):
            assert attribute in match.group(0), attribute

    def test_the_tag_filter_form_is_an_empty_sibling_after_the_scan_form(
        self, client: TestClient
    ) -> None:
        """
        Enter in the filter filters, because the input belongs to this form.

        It sits after the scan form's closing tag and before the Scan article
        ends, so it is a sibling and not a nested form -- which HTML forbids.
        """
        page = client.get("/").text
        scan_form_close = page.index("</form>")
        filter_form_open = page.index('<form id="tag-filter-form"')

        assert filter_form_open > scan_form_close
        assert "</article>" not in page[scan_form_close:filter_form_open]

        match = _TAG_FILTER_FORM.search(page)
        assert match is not None
        assert match.group("body").strip() == ""
        assert "<button" not in match.group("body")
        for attribute in (
            'hx-get="/api/tags"',
            'hx-target="#tags-list"',
            'hx-swap="outerHTML"',
            'hx-include="#tags-list"',
        ):
            assert attribute in match.group("attrs"), attribute

    def test_the_tag_list_wrapper_asks_for_nothing_on_the_full_page(
        self, client: TestClient
    ) -> None:
        """
        The page renders the list itself, so the wrapper carries no request.

        The list comes from the same cache ``/api/tags`` reads, so a load
        trigger here would only fetch what the page already holds.  The filter
        box, the refresh button and the filter form each keep their own.
        """
        page = client.get("/").text
        match = _TAGS_LIST.search(page)

        assert match is not None, "tag list wrapper not rendered"
        assert match.group(0) == '<div id="tags-list" class="tag-list">'
        assert 'hx-post="/api/cache/invalidate?resource=tags"' in page
        assert 'hx-get="/api/tags" hx-target="#tags-list"' in page

    def test_the_correspondent_select_asks_for_nothing_on_the_full_page(
        self, client: TestClient
    ) -> None:
        """
        The options are server-rendered; only the refresh button fetches them.

        The page reads the same cache ``/api/correspondents`` does, so asking
        for the options again once the select is parsed would repeat the page's
        own work.
        """
        _app(client).state.cache.set("correspondents", [{"id": 7, "name": "Acme"}])
        page = client.get("/").text
        match = _CORRESPONDENT_SELECT.search(page)

        assert match is not None, "correspondent select not rendered"
        assert "hx-" not in match.group("attrs")
        assert '<option value="7">Acme</option>' in page
        assert 'hx-post="/api/cache/invalidate?resource=correspondents"' in page

    def test_the_tag_block_adds_no_attribute_to_the_scan_form(self) -> None:
        """
        The form is byte-identical to before this plan (C-10, Pitfall 8).

        This is the decisive advantage of the form-owner attribute over
        `hx-params="not q"` on the form, which would have needed the
        `hx-disinherit` list extended.
        """
        index = (_TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
        match = _SCAN_FORM_TAG.search(index)

        assert match is not None
        assert match.group("attrs").split() == _SCAN_FORM_ATTRS
        assert "hx-confirm" not in index

    def test_no_template_keeps_the_tag_multi_select(self) -> None:
        """D-30: one control, one partial -- the multi-select is deleted."""
        for path in sorted(_TEMPLATES_DIR.rglob("*.html")):
            source = path.read_text(encoding="utf-8")
            assert 'name="tags" multiple' not in source, path
            assert '<select name="tags"' not in source, path

    def test_the_tag_and_correspondent_refresh_buttons_survive_the_rewrite(
        self, client: TestClient
    ) -> None:
        """Both maintenance controls stay; only the tag one's target moves."""
        page = client.get("/").text

        assert page.count('aria-label="Refresh tags"') == 1
        assert page.count('aria-label="Refresh correspondents"') == 1

        refresh = re.search(
            r'<button type="button"[^>]*resource=tags[^>]*>', page, re.DOTALL
        )
        assert refresh is not None
        for attribute in (
            'hx-target="#tags-list"',
            'hx-swap="outerHTML"',
            'hx-include="#tag-filter, #tags-list"',
        ):
            assert attribute in refresh.group(0), attribute
