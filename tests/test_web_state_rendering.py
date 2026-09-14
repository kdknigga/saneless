"""
Per-state rendering contract for the web templates.

Covers requirements: UI-03, UI-07, CTR-01, ROBU-04.

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
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
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
    ScanBatch,
    ScannerBackend,
    ScanSettings,
)
from saneless.vocabulary import (
    ACTIVE_STATES,
    BUSY_STATES,
    TERMINAL_STATES,
    JobState,
    progress_label,
    state_label,
)
from saneless.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from saneless.job import JobStore


class _StubScanner(ScannerBackend):
    """Concrete scanner stub; these tests never run a scan."""

    def get_devices(self) -> list[DeviceInfo]:
        """Return a single fake device."""
        return [
            DeviceInfo(
                name="test:device:001",
                vendor="Test",
                model="Stub",
                device_type="virtual",
            ),
        ]

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """Return default capabilities."""
        return DeviceCapabilities(
            sources=["Flatbed"], resolutions=[300], modes=["color"]
        )

    def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
        """Return a batch holding a single white test image."""
        return ScanBatch(
            pages=[Image.new("RGB", (100, 100), "white")],
            actual_resolution=settings.resolution,
            pages_rejected=0,
        )


# The scan button, captured whole so attribute and text assertions cannot be
# satisfied by markup somewhere else on the page.
_SCAN_BUTTON = re.compile(
    r'<button type="submit" id="scan-btn"(?P<attrs>[^>]*)>(?P<text>.*?)</button>',
    re.DOTALL,
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """TestClient over a real app with a stub scanner and no network calls."""
    settings = Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(url="http://localhost:8000", token="test-token"),
        output=OutputConfig(tmp_dir=str(tmp_path), data_dir=str(tmp_path)),
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
    with TestClient(app) as tc:
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
    client: TestClient, state: JobState, warning: str | None = None
) -> None:
    """Create a job, drive it to `state`, and make it the worker's current job."""
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title="Render Test")
    job_store.update_state(job.id, state, error="disk on fire")
    if warning is not None:
        _set_warning(job_store, job.id, warning)
    _adopt_as_current_job(client, job.id)


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
    """Only the three terminal history cells carry a status CSS class (CTR-01)."""
    _job_in_state(client, state)
    text = client.get("/api/jobs/history").text
    assert ('<td class="status-done">' in text) is (state is JobState.DONE)
    assert ('<td class="status-error">' in text) is (state is JobState.ERROR)
    assert ('<td class="status-fallback">' in text) is (state is JobState.FALLBACK)


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
        assert (
            '<p role="alert" class="status-error">&#10007; Error: disk on fire</p>'
            in text
        )
    if state is JobState.FALLBACK:
        assert (
            '<p class="status-fallback">&#8594; Saved to folder: Render Test</p>'
            in text
        )
        # A fallback is a degradation, not a failure: no role="alert" here.
        assert 'role="alert"' not in text
    # The history-refresh hook belongs to the terminal states only -- all three
    # of them, FALLBACK included, or the table goes stale after a fallback.
    assert ('hx-get="/api/jobs/history"' in text) is (state in TERMINAL_STATES)


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
