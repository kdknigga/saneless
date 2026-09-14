"""
Web endpoint tests for the saneless FastAPI application.

Covers requirements: UI-01 through UI-08, PROF-03, PLSS-04,
HLTH-01, HLTH-02, LOG-03.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

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
from saneless.job import JobState, JobStore
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScanBatch,
    ScannerBackend,
    ScanSettings,
)
from saneless.vocabulary import FlipOutcome
from saneless.web.app import create_app
from saneless.worker import WorkerFlipCoordinator


def _app(client: TestClient) -> FastAPI:
    """Extract the FastAPI app from a TestClient, helping the type checker."""
    app: Any = client.app
    if not isinstance(app, FastAPI):
        msg = "Expected FastAPI app"
        raise TypeError(msg)
    return app


class StubScanner(ScannerBackend):
    """Minimal scanner backend for web tests that avoids ABC mock issues."""

    def get_devices(self) -> list[DeviceInfo]:
        """Return an empty device list."""
        return []

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """Return default capabilities."""
        return DeviceCapabilities(
            sources=["Flatbed"],
            resolutions=[300],
            modes=["color"],
        )

    def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
        """Return a batch holding a single white test image."""
        return ScanBatch(
            pages=[Image.new("RGB", (100, 100), "white")],
            actual_resolution=settings.resolution,
            pages_rejected=0,
        )


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    """Create Settings with test-safe defaults and tmp_path for output."""
    auth = "test-token"
    return Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(
            url="http://localhost:8000",
            token=auth,
        ),
        output=OutputConfig(
            tmp_dir=str(tmp_path),
            data_dir=str(tmp_path),
        ),
        profiles={
            "default": ProfileConfig(),
            "duplex": ProfileConfig(source="ADF Duplex"),
        },
    )


@pytest.fixture
def web_scanner() -> StubScanner:
    """Return a concrete StubScanner for web tests."""
    return StubScanner()


@pytest.fixture
def app(test_settings: Settings, web_scanner: StubScanner) -> FastAPI:
    """Create the FastAPI app with test settings and stub scanner."""
    return create_app(test_settings, web_scanner)


@pytest.fixture
def mock_paperless(app: FastAPI) -> object:
    """Patch paperless client methods to return test data without network calls."""
    app.state.paperless.get_tags = lambda: [
        {"id": 1, "name": "receipt"},
        {"id": 2, "name": "invoice"},
    ]
    app.state.paperless.get_correspondents = lambda: [
        {"id": 1, "name": "ACME Corp"},
    ]
    return app.state.paperless


@pytest.fixture
def client(app: FastAPI, mock_paperless: object) -> Iterator[TestClient]:
    """TestClient that handles lifespan enter/exit automatically."""
    _ = mock_paperless  # Ensure paperless is patched before requests
    with TestClient(app) as tc:
        yield tc


def test_page_loads(client: TestClient) -> None:
    """GET / returns 200 with form elements (UI-01)."""
    response = client.get("/")
    assert response.status_code == 200
    assert "saneless" in response.text
    assert 'name="profile"' in response.text
    assert 'name="title"' in response.text
    assert 'name="tags"' in response.text
    assert 'id="scan-btn"' in response.text


def test_health_endpoint_ok(client: TestClient) -> None:
    """GET /health returns 200 with status ok when worker alive (HLTH-01)."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_endpoint_no_auth(client: TestClient) -> None:
    """GET /health requires no authentication (HLTH-02)."""
    response = client.get("/health")
    assert response.status_code == 200


def test_profile_dropdown(client: TestClient, test_settings: Settings) -> None:
    """Profile names from settings appear in dropdown (PROF-03)."""
    response = client.get("/")
    for profile_name in test_settings.profiles:
        assert profile_name in response.text


def test_scan_form_submit(client: TestClient) -> None:
    """POST /api/scan creates job and returns status partial (PLSS-04, UI-07)."""
    response = client.post(
        "/api/scan", data={"profile": "default", "title": "Test Scan"}
    )
    assert response.status_code == 200
    assert 'id="status-area"' in response.text


def test_status_polling(client: TestClient) -> None:
    """GET /api/jobs/current/status returns status partial (UI-02)."""
    response = client.get("/api/jobs/current/status")
    assert response.status_code == 200
    assert 'id="status-area"' in response.text


def test_status_polling_active_job(client: TestClient) -> None:
    """Active job triggers hx-trigger polling attributes (UI-02)."""
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title="Polling Test")
    job_store.update_state(job.id, JobState.SCANNING)
    _app(client).state.worker._current_job_id = job.id

    response = client.get("/api/jobs/current/status")
    assert response.status_code == 200
    # Active job states include polling attributes
    has_polling = "hx-trigger" in response.text or "hx-get" in response.text
    assert has_polling


def test_flip_prompt(client: TestClient) -> None:
    """AWAITING_FLIP state shows flip prompt with PRD wording (UI-03)."""
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title="Flip Test")
    job_store.update_state(job.id, JobState.AWAITING_FLIP)
    _app(client).state.worker._current_job_id = job.id

    response = client.get("/api/jobs/current/status")
    text_lower = response.text.lower()
    assert "flip the stack over the long edge" in text_lower
    assert 'hx-post="/api/flip/continue"' in response.text
    assert 'hx-post="/api/flip/abort"' in response.text

    # Both buttons name the job they were rendered for, so an answer can only
    # ever land on that job (CR-01).
    buttons = re.findall(
        r"<button[^>]*hx-post=\"/api/flip/(continue|abort)\"[^>]*>", response.text
    )
    assert sorted(buttons) == ["abort", "continue"]
    hx_vals = re.findall(
        r"<button[^>]*hx-post=\"/api/flip/(?:continue|abort)\"[^>]*"
        r"hx-vals='([^']*)'[^>]*>",
        response.text,
    )
    assert len(hx_vals) == 2
    for raw in hx_vals:
        assert json.loads(html.unescape(raw)) == {"job_id": job.id}


def test_thumbnail_display(client: TestClient) -> None:
    """Job with thumbnail shows base64 img tag (UI-04)."""
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title="Thumb Test")
    job_store.update_thumbnail(job.id, "dGVzdA==")
    job_store.update_state(job.id, JobState.SCANNING)
    _app(client).state.worker._current_job_id = job.id

    response = client.get("/api/jobs/current/status")
    assert "data:image/jpeg;base64,dGVzdA==" in response.text


def test_job_history(client: TestClient) -> None:
    """GET /api/jobs/history returns job list (UI-05)."""
    job_store: JobStore = _app(client).state.job_store
    titles = ["Job Alpha", "Job Beta", "Job Gamma"]
    for title in titles:
        job_store.create_job(profile="default", title=title)

    response = client.get("/api/jobs/history")
    assert response.status_code == 200
    for title in titles:
        assert title in response.text


def test_error_display(client: TestClient) -> None:
    """Error state shows error message in status area (LOG-03)."""
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title="Error Test")
    job_store.update_state(job.id, JobState.ERROR, error="Scanner disconnected")
    _app(client).state.worker._current_job_id = job.id

    response = client.get("/api/jobs/current/status")
    assert "Scanner disconnected" in response.text


def test_cache_invalidate(client: TestClient) -> None:
    """POST /api/cache/invalidate refreshes resource (UI-08)."""
    response = client.post("/api/cache/invalidate?resource=tags")
    assert response.status_code == 200


def test_flip_continue(client: TestClient) -> None:
    """
    A Continue arriving after the job ended reports that job (UI-03, M-02).

    The worker clears its current job id in ``_process_job``'s ``finally``, so
    a click landing as the job ends finds no current job.  The route must fall
    back to the most recent job, as the status poll does, rather than render
    the idle copy for a job that plainly exists.
    """
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="duplex", title="Flip Just Finished")
    job_store.finish_job(job.id, JobState.DONE)
    assert _app(client).state.worker.current_job_id is None

    response = client.post("/api/flip/continue", data={"job_id": job.id})

    assert response.status_code == 200
    assert "Done: Flip Just Finished" in response.text
    assert "Ready to scan." not in response.text


def test_flip_abort(client: TestClient) -> None:
    """
    An Abort arriving after the job ended reports that job (UI-03, M-02).

    Otherwise an abort that aborted nothing reports nothing either: the partial
    would say "Ready to scan." while the job that timed out sits in history.
    """
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="duplex", title="Flip Timed Out")
    job_store.finish_job(
        job.id,
        JobState.ERROR,
        error="Manual duplex flip wait timed out after 600 seconds",
    )
    assert _app(client).state.worker.current_job_id is None

    response = client.post("/api/flip/abort", data={"job_id": job.id})

    assert response.status_code == 200
    assert "Manual duplex flip wait timed out after 600 seconds" in response.text
    assert "Ready to scan." not in response.text


_FLIP_BUTTONS = ('hx-post="/api/flip/continue"', 'hx-post="/api/flip/abort"')


@pytest.fixture
def waiting_flip(client: TestClient) -> Iterator[tuple[str, WorkerFlipCoordinator]]:
    """
    Stage a job waiting at an armed flip prompt as the worker's current job.

    This reaches into the served worker the same deliberate way
    ``test_flip_prompt`` does, and adds the armed coordinator the worker would
    hold while the job is ``AWAITING_FLIP``.  Both attributes are reset on
    teardown so the lifespan's worker thread is not left pointing at a fake.
    """
    worker = _app(client).state.worker
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="duplex", title="Flip Waiting")
    job_store.update_state(job.id, JobState.AWAITING_FLIP)
    coordinator = WorkerFlipCoordinator(job.id)
    coordinator.arm()
    worker._current_job_id = job.id
    worker._flip_coordinator = coordinator
    try:
        yield job.id, coordinator
    finally:
        worker._flip_coordinator = None
        worker._current_job_id = None


def _assert_acknowledged(text: str, label: str) -> None:
    """Assert the status area acknowledges ``label`` and shows no flip buttons."""
    assert f'<p aria-busy="true">{label}</p>' in text
    for button in _FLIP_BUTTONS:
        assert button not in text
    assert "flip the stack over the long edge" not in text.lower()


def test_claimed_abort_acknowledges_instead_of_the_prompt(
    client: TestClient, waiting_flip: tuple[str, WorkerFlipCoordinator]
) -> None:
    """
    A claimed Abort renders "Aborting scan..." without the buttons (CR-01).

    The worker has not persisted ERROR yet, so the row still reads
    AWAITING_FLIP.  Re-rendering the prompt there would look as though the
    click did nothing and invite a second one.
    """
    job_id, coordinator = waiting_flip

    response = client.post("/api/flip/abort", data={"job_id": job_id})

    assert response.status_code == 200
    _assert_acknowledged(response.text, "Aborting scan...")
    assert coordinator.answer is FlipOutcome.ABORTED
    # AWAITING_FLIP is still active, so the status area keeps polling.
    assert 'hx-trigger="every 1s"' in response.text


def test_claimed_continue_acknowledges_instead_of_the_prompt(
    client: TestClient, waiting_flip: tuple[str, WorkerFlipCoordinator]
) -> None:
    """A claimed Continue renders the flip-confirmed copy without buttons (CR-01)."""
    job_id, coordinator = waiting_flip

    response = client.post("/api/flip/continue", data={"job_id": job_id})

    assert response.status_code == 200
    _assert_acknowledged(
        response.text, "Flip confirmed. Scanning reverse sides next..."
    )
    assert coordinator.answer is FlipOutcome.CONTINUED


def test_double_clicked_abort_still_acknowledges(
    client: TestClient, waiting_flip: tuple[str, WorkerFlipCoordinator]
) -> None:
    """
    A repeat click on an answered job renders the same acknowledgment (CR-01).

    The second Abort is dropped by the coordinator, so the route has no claim
    of its own; the acknowledgment must come from the answer the worker holds.
    """
    job_id, coordinator = waiting_flip

    first = client.post("/api/flip/abort", data={"job_id": job_id})
    second = client.post("/api/flip/abort", data={"job_id": job_id})

    assert first.status_code == 200
    assert second.status_code == 200
    _assert_acknowledged(second.text, "Aborting scan...")
    assert coordinator.answer is FlipOutcome.ABORTED


def test_continue_after_abort_reports_the_abort(
    client: TestClient, waiting_flip: tuple[str, WorkerFlipCoordinator]
) -> None:
    """A late Continue on an aborted job acknowledges the Abort that won (D-16)."""
    job_id, coordinator = waiting_flip

    client.post("/api/flip/abort", data={"job_id": job_id})
    response = client.post("/api/flip/continue", data={"job_id": job_id})

    assert response.status_code == 200
    _assert_acknowledged(response.text, "Aborting scan...")
    assert coordinator.answer is FlipOutcome.ABORTED


def test_foreign_job_id_leaves_the_prompt_open(
    client: TestClient, waiting_flip: tuple[str, WorkerFlipCoordinator]
) -> None:
    """
    An Abort naming another job does not acknowledge the waiting job (T-25-49).

    The route's claim is only authoritative for the job it names; the rendered
    job is still unanswered, so its prompt and both buttons stay.
    """
    _, coordinator = waiting_flip

    response = client.post("/api/flip/abort", data={"job_id": "some-other-job"})

    assert response.status_code == 200
    for button in _FLIP_BUTTONS:
        assert button in response.text
    assert "Aborting scan..." not in response.text
    assert coordinator.answer is None


def test_poll_acknowledges_an_answer_the_store_has_not_recorded_yet(
    client: TestClient, waiting_flip: tuple[str, WorkerFlipCoordinator]
) -> None:
    """The status poll keeps the buttons away once the job is answered (CR-01)."""
    job_id, _ = waiting_flip
    client.post("/api/flip/continue", data={"job_id": job_id})

    response = client.get("/api/jobs/current/status")

    assert response.status_code == 200
    _assert_acknowledged(
        response.text, "Flip confirmed. Scanning reverse sides next..."
    )
    assert 'hx-trigger="every 1s"' in response.text


def test_poll_renders_the_prompt_for_an_unanswered_flip(
    client: TestClient, waiting_flip: tuple[str, WorkerFlipCoordinator]
) -> None:
    """An armed, unanswered flip wait still renders the full prompt (UI-03)."""
    _ = waiting_flip

    response = client.get("/api/jobs/current/status")

    assert response.status_code == 200
    assert "flip the stack over the long edge" in response.text.lower()
    for button in _FLIP_BUTTONS:
        assert button in response.text
    assert 'aria-busy="true"' not in response.text


def test_index_acknowledges_an_answered_flip(
    client: TestClient, waiting_flip: tuple[str, WorkerFlipCoordinator]
) -> None:
    """A page reload after the answer shows the acknowledgment too (CR-01, D-17)."""
    job_id, _ = waiting_flip
    client.post("/api/flip/abort", data={"job_id": job_id})

    response = client.get("/")

    assert response.status_code == 200
    _assert_acknowledged(response.text, "Aborting scan...")


@pytest.mark.parametrize("route", ["/api/flip/continue", "/api/flip/abort"])
def test_flip_routes_require_a_job_id(client: TestClient, route: str) -> None:
    """
    A flip answer that names no job is rejected before reaching the worker.

    The job id is what scopes the answer to one job (CR-01), so a request
    without it cannot be interpreted and is refused with 422.
    """
    response = client.post(route)

    assert response.status_code == 422


def test_paperless_test_connected(client: TestClient) -> None:
    """GET /api/paperless/test returns connected status (PLSS-03)."""
    _app(client).state.paperless.test_connection = lambda: "connected"
    response = client.get("/api/paperless/test")
    assert response.status_code == 200
    assert response.json() == {"status": "connected"}


def test_paperless_test_token_rejected(client: TestClient) -> None:
    """GET /api/paperless/test returns token_rejected status (PLSS-03)."""
    _app(client).state.paperless.test_connection = lambda: "token_rejected"
    response = client.get("/api/paperless/test")
    assert response.status_code == 200
    assert response.json() == {"status": "token_rejected"}


def test_paperless_test_unreachable(client: TestClient) -> None:
    """GET /api/paperless/test returns unreachable status (PLSS-03)."""
    _app(client).state.paperless.test_connection = lambda: "unreachable"
    response = client.get("/api/paperless/test")
    assert response.status_code == 200
    assert response.json() == {"status": "unreachable"}


def test_paperless_test_error(client: TestClient) -> None:
    """GET /api/paperless/test returns error on exception (PLSS-03)."""

    def raise_exc() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    _app(client).state.paperless.test_connection = raise_exc
    response = client.get("/api/paperless/test")
    assert response.status_code == 502
    data = response.json()
    assert data["status"] == "error"
    assert data["detail"] == "RuntimeError"


def test_scan_button_disabled_during_active_job(client: TestClient) -> None:
    """Scan button disabled during active job (UI-07)."""
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title="Active Job")
    job_store.update_state(job.id, JobState.SCANNING)
    _app(client).state.worker._current_job_id = job.id

    response = client.get("/")
    assert "disabled" in response.text
    assert 'id="scan-btn"' in response.text


def test_history_humanized_labels(client: TestClient) -> None:
    """Job history shows human-readable state labels instead of raw enum values (P12-01)."""
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title="Label Test")
    job_store.update_state(job.id, JobState.DONE)

    response = client.get("/api/jobs/history")
    assert response.status_code == 200
    assert "Complete" in response.text


def test_status_no_inline_scripts(client: TestClient) -> None:
    """Status partial contains no inline script tags for DONE or ERROR states (P12-04)."""
    job_store: JobStore = _app(client).state.job_store

    # DONE state
    job_done = job_store.create_job(profile="default", title="Done Script Test")
    job_store.update_state(job_done.id, JobState.DONE)
    _app(client).state.worker._current_job_id = job_done.id
    resp_done = client.get("/api/jobs/current/status")
    assert "<script>" not in resp_done.text

    # ERROR state
    job_err = job_store.create_job(profile="default", title="Err Script Test")
    job_store.update_state(job_err.id, JobState.ERROR, error="test error")
    _app(client).state.worker._current_job_id = job_err.id
    resp_err = client.get("/api/jobs/current/status")
    assert "<script>" not in resp_err.text


def test_scan_form_no_hx_on(client: TestClient) -> None:
    """Scan form does not use hx-on:: inline event attributes (P12-04)."""
    response = client.get("/")
    assert "hx-on::before-request" not in response.text


def test_refresh_buttons_accessible(client: TestClient) -> None:
    """Refresh buttons have aria-label attributes and sr-only text (P12-02)."""
    response = client.get("/")
    assert 'aria-label="Refresh tags"' in response.text
    assert 'aria-label="Refresh correspondents"' in response.text
    assert "sr-only" in response.text


def test_scan_form_has_heading(client: TestClient) -> None:
    """Scan form article has an h2 heading for accessibility (P12-02)."""
    response = client.get("/")
    assert "<h2>Scan</h2>" in response.text


def test_flip_abort_label(client: TestClient) -> None:
    """Flip prompt cancel button reads Abort scan (P12-05)."""
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title="Flip Abort Test")
    job_store.update_state(job.id, JobState.AWAITING_FLIP)
    _app(client).state.worker._current_job_id = job.id

    response = client.get("/api/jobs/current/status")
    assert "Abort scan" in response.text
    assert ">Cancel<" not in response.text


def test_correspondent_placeholder(client: TestClient) -> None:
    """Correspondent dropdown placeholder reads No correspondent (P12-05)."""
    response = client.get("/")
    assert "No correspondent" in response.text
    assert "-- None --" not in response.text


def test_css_spacing_normalized() -> None:
    """CSS uses PicoCSS grid-aligned spacing with no !important overrides (P12-05)."""
    css_path = (
        Path(__file__).parent.parent / "src" / "saneless" / "web" / "static" / "app.css"
    )
    css_content = css_path.read_text()
    assert "!important" not in css_content
    assert "padding: 0.25rem" in css_content
    assert "var(--pico-border-width)" in css_content


def test_paperless_test_502_sanitizes_exception(client: TestClient) -> None:
    """502 response returns exception class name, not raw message with secrets (RH-04)."""
    sensitive_msg = "http://192.168.1.100:8000 token=abc123"
    _app(client).state.paperless.test_connection = _raise_factory(
        ConnectionError, sensitive_msg
    )
    response = client.get("/api/paperless/test")
    assert response.status_code == 502
    data = response.json()
    assert data["detail"] == "ConnectionError"
    assert "192.168.1.100" not in response.text
    assert "abc123" not in response.text


def _raise_factory(exc_type: type[Exception], msg: str):  # noqa: ANN202 -- return type is dynamic callable
    """Create a callable that raises the given exception with the given message."""

    def _raise() -> None:
        raise exc_type(msg)

    return _raise
