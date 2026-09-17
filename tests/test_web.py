"""
Web endpoint tests for the saneless FastAPI application.

Covers requirements: UI-01 through UI-08, PROF-03, PLSS-04,
HLTH-01, HLTH-02, LOG-03.
"""

from __future__ import annotations

import html
import inspect
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx import Response

    from saneless.job import Job

import httpx
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from saneless.checks import (
    check_name,
    check_state_class,
    check_state_glyph,
    check_state_label,
)
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
    WebConfig,
)
from saneless.job import JobState, JobStore
from saneless.paperless import PaperlessClient
from saneless.vocabulary import (
    QUEUE_FULL_JOB_ERROR,
    TOKEN_UNSET_JOB_ERROR,
    ErrorCategory,
    FlipOutcome,
    RequestRejection,
    SubmitResult,
    WorkerHealth,
    error_message,
    error_next_step,
    local_time,
    page_counts,
    progress_label,
    rejection_message,
)
from saneless.web import app as app_module
from saneless.web.app import create_app
from saneless.web.checks_cache import CheckCache
from saneless.web.refresher import CheckRefresher
from saneless.web.routes import _profile_options, _ProfileOption
from saneless.worker import ScanWorker, WorkerFlipCoordinator
from tests.conftest import StubScannerBackend


def _app(client: TestClient) -> FastAPI:
    """Extract the FastAPI app from a TestClient, helping the type checker."""
    app: Any = client.app
    if not isinstance(app, FastAPI):
        msg = "Expected FastAPI app"
        raise TypeError(msg)
    return app


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
def web_scanner() -> StubScannerBackend:
    """Return the shared concrete stub backend for web tests."""
    return StubScannerBackend()


@pytest.fixture
def app(test_settings: Settings, web_scanner: StubScannerBackend) -> FastAPI:
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


def test_no_route_handler_is_a_coroutine(client: TestClient) -> None:
    """
    Every route handler is a plain ``def`` (ROBU-05, M-01).

    Each handler calls blocking code, and FastAPI only moves ``def`` handlers
    onto its threadpool; an ``async def`` one would block the event loop.  The
    client fixture is used so the lifespan closes the job store afterwards.
    """
    routes = [route for route in _app(client).routes if isinstance(route, APIRoute)]
    assert routes
    for route in routes:
        assert not inspect.iscoroutinefunction(route.endpoint), route.path


def test_health_answers_while_a_request_blocks(client: TestClient) -> None:
    """/health answers promptly while another request is blocked in I/O (ROBU-05)."""
    gate = threading.Event()
    app = _app(client)

    def blocking_get_tags() -> list[dict[str, object]]:
        gate.wait(5)
        return []

    app.state.paperless.get_tags = blocking_get_tags
    app.state.cache.invalidate("tags")
    slow = threading.Thread(target=lambda: client.get("/api/tags"))
    slow.start()
    try:
        time.sleep(0.2)
        started = time.monotonic()
        response = client.get("/health")
        elapsed = time.monotonic() - started
        assert response.status_code == 200
        assert elapsed < 1.0
    finally:
        gate.set()
        slow.join(5)


def test_metadata_fetch_failure_falls_back_to_an_empty_list(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A Paperless failure still renders empty options and logs a WARNING (ROBU-05)."""
    app = _app(client)

    def failing_get_tags() -> list[dict[str, object]]:
        msg = "paperless unreachable"
        raise ConnectionError(msg)

    app.state.paperless.get_tags = failing_get_tags
    app.state.cache.invalidate("tags")
    with caplog.at_level(logging.WARNING, logger="saneless.web.routes"):
        response = client.get("/api/tags")

    assert response.status_code == 200
    assert "receipt" not in response.text
    assert app.state.cache.get("tags") is None
    assert any(
        r.levelno == logging.WARNING and "using empty list" in r.getMessage()
        for r in caplog.records
    )


def test_health_reports_degraded_worker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A degraded worker makes /health a 503 naming the store (ROBU-05, D-10).

    The property is patched rather than the degraded Event set, so the worker
    thread's idle recovery probe cannot clear it mid-request.
    """
    monkeypatch.setattr(
        ScanWorker, "health", property(lambda _self: WorkerHealth.DEGRADED)
    )
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "detail": "job store failing"}


def test_health_reports_down_worker(client: TestClient) -> None:
    """A stopped worker makes /health a 503 naming the thread (ROBU-05, D-10)."""
    assert _app(client).state.worker.stop()
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "detail": "worker thread is down"}


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


@pytest.fixture
def titled_client(
    tmp_path: Path, web_scanner: StubScannerBackend, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """
    TestClient whose ``default`` profile has ``title = "Receipt"`` (D-16).

    The worker's ``submit`` is stubbed to accept without running a pipeline,
    so the created job row is what the route resolved and nothing else.

    The token is configured because ``POST /api/scan`` now refuses outright
    when it is a placeholder (APPL-07, D-15), and an unset one here would stop
    these title tests at the guard instead of reaching the resolution they are
    about.  That refusal has its own tests in tests/test_web_errors.py.
    """
    auth = "test-token"
    settings = Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(url="http://localhost:8000", token=auth),
        output=OutputConfig(tmp_dir=str(tmp_path), data_dir=str(tmp_path)),
        profiles={"default": ProfileConfig(title="Receipt")},
    )
    app = create_app(settings, web_scanner)
    app.state.paperless.get_tags = list
    app.state.paperless.get_correspondents = list
    monkeypatch.setattr(app.state.worker, "submit", lambda _job: SubmitResult.ACCEPTED)
    with TestClient(app) as tc:
        yield tc


@pytest.mark.parametrize("typed", ["", "   "], ids=["empty", "whitespace"])
def test_scan_blank_title_uses_profile_title(
    titled_client: TestClient, typed: str
) -> None:
    """A blank typed title is replaced by the profile's title (D-16, M-24)."""
    response = titled_client.post(
        "/api/scan", data={"profile": "default", "title": typed}
    )
    assert response.status_code == 200
    job_store: JobStore = _app(titled_client).state.job_store
    assert job_store.list_recent(limit=1)[0].title == "Receipt"


def test_scan_typed_title_beats_profile_title(titled_client: TestClient) -> None:
    """A typed title is used as given over the profile's title (D-16)."""
    response = titled_client.post(
        "/api/scan", data={"profile": "default", "title": "Typed"}
    )
    assert response.status_code == 200
    job_store: JobStore = _app(titled_client).state.job_store
    assert job_store.list_recent(limit=1)[0].title == "Typed"


def test_scan_title_unknown_profile_still_rejected(titled_client: TestClient) -> None:
    """Resolving the title never masks an unknown profile (D-16, T-27-04)."""
    job_store: JobStore = _app(titled_client).state.job_store
    before = job_store.list_recent(limit=50)
    response = titled_client.post(
        "/api/scan", data={"profile": "no-such-profile", "title": ""}
    )
    assert response.status_code == 422
    assert response.json() == {
        "status": "error",
        "detail": rejection_message(RequestRejection.UNKNOWN_PROFILE),
    }
    assert job_store.list_recent(limit=50) == before


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


def test_cache_invalidate_rejects_an_unknown_resource(client: TestClient) -> None:
    """Only tags and correspondents can be invalidated; nothing else is (N-20, D-19)."""
    cache = _app(client).state.cache
    cache.set("tags", [{"id": 1, "name": "receipt"}])

    response = client.post("/api/cache/invalidate?resource=bogus")

    assert response.status_code == 422
    assert response.json() == {
        "status": "error",
        "detail": rejection_message(RequestRejection.INVALID_REQUEST),
    }
    assert cache.get("tags") == [{"id": 1, "name": "receipt"}]


def _status_area(page: str) -> str:
    """Cut the status area out of the full page, leaving history behind."""
    start = page.index('id="status-area"')
    return page[start : page.index("history-table-wrap", start)]


def test_rejected_submit_does_not_replace_the_job_that_ran(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A rejection during a run never becomes the status area's job (D-06, T8).

    The rejected row is newer than the running job, so once that job ends the
    status area must still report it -- not the queue-full rejection.
    """
    app = _app(client)
    job_store: JobStore = app.state.job_store
    worker = app.state.worker
    job = job_store.create_job(profile="default", title="Running Job")
    job_store.update_state(job.id, JobState.SCANNING)
    worker._current_job_id = job.id
    monkeypatch.setattr(worker, "submit", lambda _job: SubmitResult.QUEUE_FULL)

    rejected = client.post(
        "/api/scan", data={"profile": "default", "title": "Refused Scan"}
    )
    assert rejected.status_code == 429
    newest = job_store.list_recent(limit=1)[0]
    assert newest.title == "Refused Scan"
    assert newest.error_category is ErrorCategory.REJECTED

    job_store.finish_job(job.id, JobState.DONE)
    worker._current_job_id = None

    status = client.get("/api/jobs/current/status")
    assert "Done: Running Job" in status.text
    assert QUEUE_FULL_JOB_ERROR not in status.text

    page = client.get("/").text
    assert "Done: Running Job" in _status_area(page)
    assert QUEUE_FULL_JOB_ERROR not in _status_area(page)
    # History still lists the rejected attempt (D-05).
    assert "Refused Scan" in page


def test_rejected_rows_alone_leave_the_status_area_ready(client: TestClient) -> None:
    """With only rejected rows and no current job, nothing has run (D-06)."""
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title="Never Ran")
    job_store.finish_job(
        job.id,
        JobState.ERROR,
        error=QUEUE_FULL_JOB_ERROR,
        error_category=ErrorCategory.REJECTED,
    )

    response = client.get("/api/jobs/current/status")

    assert response.status_code == 200
    assert "Ready to scan." in response.text
    assert QUEUE_FULL_JOB_ERROR not in response.text


def test_index_lists_the_worker_profiles(client: TestClient) -> None:
    """The profile dropdown reads the worker's locked profile set (D-19)."""
    _app(client).state.worker._set_profiles(
        {"default": ProfileConfig(), "zz-new-profile": ProfileConfig()}
    )

    response = client.get("/")

    assert response.status_code == 200
    assert "zz-new-profile" in response.text
    assert ">duplex<" not in response.text


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


class TestAppComposition:
    """
    What ``create_app`` must have assembled before a single request arrives.

    The phase's rendering and its background refresh both depend on wiring that
    no route can compensate for: a template cannot invent a filter, and a route
    cannot probe on its own without undoing D-04.  Every assertion here is made
    on an app that has **not** been entered, because construction is exactly the
    moment that must stay free of threads and probes.

    Covers requirements: APPL-02, APPL-03, APPL-04, APPL-12.
    """

    @pytest.fixture
    def unstarted_app(
        self, test_settings: Settings, web_scanner: StubScannerBackend
    ) -> Iterator[FastAPI]:
        """Build the app and never enter its lifespan, closing what it opened."""
        built = create_app(test_settings, web_scanner)
        try:
            yield built
        finally:
            built.state.paperless.close()
            built.state.job_store.close()

    def test_registers_the_eight_new_filters(self, unstarted_app: FastAPI) -> None:
        """Every name this phase's templates reach for is registered (APPL-03)."""
        filters = unstarted_app.state.templates.env.filters
        expected = {
            "check_state_class",
            "check_state_glyph",
            "check_state_label",
            "check_name",
            "error_message",
            "error_next_step",
            "page_counts",
            "local_time",
        }
        assert expected <= set(filters)

    def test_each_filter_is_the_shared_implementation(
        self, unstarted_app: FastAPI
    ) -> None:
        """
        Identity, not equivalence: one implementation serves both surfaces.

        A lambda wrapper here would pass a "renders the same string" test today
        and drift from ``saneless doctor`` the first time either side is
        edited.  ``is`` is what makes APPL-12's "one shared place" checkable.
        """
        filters = unstarted_app.state.templates.env.filters
        assert filters["check_state_class"] is check_state_class
        assert filters["check_state_glyph"] is check_state_glyph
        assert filters["check_state_label"] is check_state_label
        assert filters["check_name"] is check_name
        assert filters["error_message"] is error_message
        assert filters["error_next_step"] is error_next_step
        assert filters["page_counts"] is page_counts
        assert filters["local_time"] is local_time

    def test_exposes_the_cache_and_the_refresher_on_app_state(
        self, unstarted_app: FastAPI
    ) -> None:
        """Routes reach both through the same channel the worker uses."""
        assert isinstance(unstarted_app.state.checks, CheckCache)
        assert isinstance(unstarted_app.state.refresher, CheckRefresher)

    def test_the_cache_is_cold_before_the_app_is_entered(
        self, unstarted_app: FastAPI
    ) -> None:
        """Construction issues no probe, so the first render says Checking (D-06)."""
        cached = unstarted_app.state.checks.current()
        assert cached.results is None
        assert cached.checked_at is None

    def test_the_existing_state_entries_and_filters_are_unchanged(
        self, unstarted_app: FastAPI, test_settings: Settings
    ) -> None:
        """The five earlier injections and three earlier filters still hold."""
        state = unstarted_app.state
        assert isinstance(state.worker, ScanWorker)
        assert isinstance(state.job_store, JobStore)
        assert state.settings is test_settings
        assert state.paperless is not None
        assert state.cache is not None
        filters = state.templates.env.filters
        assert {"state_label", "progress_label", "flip_answer_label"} <= set(filters)

    def test_building_the_app_does_not_start_the_refresher_thread(
        self, unstarted_app: FastAPI
    ) -> None:
        """
        Starting a thread belongs to the lifespan, not to the factory.

        ``create_app`` is called by tests, by ``saneless doctor``'s neighbours
        and by anything that wants to inspect routes; none of them should get a
        probing daemon as a side effect.
        """
        assert unstarted_app.state.refresher._thread.is_alive() is False


# --- The owner cookie (APPL-09, D-23, D-24) ---------------------------------

# The cookie's name, spelled out here rather than imported from the route
# module: a test that imported the constant would still pass if the wire name
# changed underneath every browser that already holds one.
_OWNER_COOKIE = "saneless_owner"

# ``secrets.token_urlsafe(32)`` is 32 random bytes in unpadded URL-safe base64,
# which is always 43 characters.  Asserting the length is the only way this
# suite can see the entropy behind the value it is handed.
_MINIMUM_OWNER_COOKIE_LENGTH = 43


def _owner_set_cookie(response: Response) -> str | None:
    """
    Return the raw ``Set-Cookie`` header carrying the owner token, if any.

    The raw header is parsed instead of the client's cookie jar because the jar
    normalises away exactly what D-23 pins: an absent ``Max-Age`` and an absent
    ``Secure`` are both invisible once httpx has turned the header into a jar
    entry, so a jar assertion could not tell a session cookie from a persistent
    one.

    Args:
        response: The response to read the headers of.

    Returns:
        The whole header line, or None when this response mints nothing.

    """
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{_OWNER_COOKIE}="):
            return header
    return None


def _newest_job(client: TestClient) -> Job:
    """
    Return the most recently written job row.

    Args:
        client: The client whose app owns the job store.

    Returns:
        The newest row, which is the one the last submit created.

    """
    job_store: JobStore = _app(client).state.job_store
    return job_store.list_recent(limit=1)[0]


def _submit_scan(client: TestClient, title: str) -> str:
    """
    Submit one scan through the real route and return the job it created.

    Args:
        client: The browser submitting the scan.
        title: The document title to submit.

    Returns:
        The id of the created job row.

    """
    response = client.post("/api/scan", data={"profile": "duplex", "title": title})
    assert response.status_code == 200
    return _newest_job(client).id


def _other_browser(client: TestClient) -> TestClient:
    """
    Return a second client over the same running app, with its own cookie jar.

    Two clients rather than one client with its jar emptied: the jar is the
    thing under test, and two jars are what D-23's "one token per browser"
    actually means.  The app is already started by the first client's fixture,
    so this one is used without entering its lifespan.

    Args:
        client: The client whose app to share.

    Returns:
        A second client holding no cookies.

    """
    return TestClient(_app(client))


@pytest.fixture
def accepting_client(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Return the shared client, with submits accepted but no pipeline run."""
    monkeypatch.setattr(
        _app(client).state.worker, "submit", lambda _job: SubmitResult.ACCEPTED
    )
    return client


@pytest.fixture
def owned_flip(
    accepting_client: TestClient,
) -> Iterator[tuple[str, WorkerFlipCoordinator]]:
    """
    Stage a job waiting at a flip prompt, owned by ``accepting_client``.

    The token is minted by a real ``POST /api/scan`` rather than written
    straight into the row, so the fixture exercises the mint a browser gets
    instead of a hand-made value that only resembles one.
    """
    job_id = _submit_scan(accepting_client, "Owned Flip")
    worker = _app(accepting_client).state.worker
    job_store: JobStore = _app(accepting_client).state.job_store
    job_store.update_state(job_id, JobState.AWAITING_FLIP)
    coordinator = WorkerFlipCoordinator(job_id)
    coordinator.arm()
    worker._current_job_id = job_id
    worker._flip_coordinator = coordinator
    try:
        yield job_id, coordinator
    finally:
        worker._flip_coordinator = None
        worker._current_job_id = None


class TestOwnerCookie:
    """
    The owner token's mint, its reuse, and the gate it puts on a flip answer.

    Covers APPL-09 and decisions D-23 (a session cookie, one per browser) and
    D-24 (the token gates the two flip buttons and nothing else).
    """

    def test_owner_cookie_is_httponly_lax_and_session_only(
        self, accepting_client: TestClient
    ) -> None:
        """The first submit mints D-23's exact attribute set, and nothing else."""
        response = accepting_client.post(
            "/api/scan", data={"profile": "duplex", "title": "First Scan"}
        )

        header = _owner_set_cookie(response)
        assert header is not None
        attributes = header.lower()
        assert "httponly" in attributes
        assert "samesite=lax" in attributes
        assert "path=/" in attributes
        # A session cookie dies with the browser, so neither lifetime attribute
        # may appear.  Secure is deliberately absent too: the appliance is
        # served over plain HTTP on a LAN, and Secure would silently disable
        # the cookie rather than harden it.
        assert "max-age" not in attributes
        assert "expires" not in attributes
        assert "secure" not in attributes

    def test_owner_cookie_value_is_recorded_on_the_job(
        self, accepting_client: TestClient
    ) -> None:
        """The job row records exactly the token the browser was handed."""
        accepting_client.post(
            "/api/scan", data={"profile": "duplex", "title": "Recorded"}
        )

        assert (
            _newest_job(accepting_client).owner_token
            == accepting_client.cookies[_OWNER_COOKIE]
        )

    def test_owner_cookie_is_minted_once_per_browser(
        self, accepting_client: TestClient
    ) -> None:
        """A second submit from the same browser mints nothing (D-23)."""
        first = accepting_client.post(
            "/api/scan", data={"profile": "duplex", "title": "One"}
        )
        second = accepting_client.post(
            "/api/scan", data={"profile": "duplex", "title": "Two"}
        )

        assert _owner_set_cookie(first) is not None
        assert _owner_set_cookie(second) is None

    def test_owner_cookie_reuse_records_the_same_value_on_a_second_job(
        self, accepting_client: TestClient
    ) -> None:
        """Two tabs on one device do not disown each other (D-23)."""
        accepting_client.post("/api/scan", data={"profile": "duplex", "title": "One"})
        accepting_client.post("/api/scan", data={"profile": "duplex", "title": "Two"})

        job_store: JobStore = _app(accepting_client).state.job_store
        recorded = {job.owner_token for job in job_store.list_recent(limit=2)}
        assert recorded == {accepting_client.cookies[_OWNER_COOKIE]}

    def test_owner_cookie_carries_at_least_32_bytes_of_entropy(
        self, accepting_client: TestClient
    ) -> None:
        """The mint is token_urlsafe(32), which is 43 characters (T-30-57)."""
        accepting_client.post(
            "/api/scan", data={"profile": "duplex", "title": "Entropy"}
        )

        minted = accepting_client.cookies[_OWNER_COOKIE]
        assert len(minted) >= _MINIMUM_OWNER_COOKIE_LENGTH

    @pytest.mark.parametrize("presented", ["", "   "], ids=["empty", "whitespace"])
    def test_owner_cookie_that_is_blank_is_replaced(
        self, accepting_client: TestClient, presented: str
    ) -> None:
        """A blank cookie is treated as no cookie and a fresh one is minted."""
        accepting_client.cookies.set(_OWNER_COOKIE, presented)

        response = accepting_client.post(
            "/api/scan", data={"profile": "duplex", "title": "Blank"}
        )

        header = _owner_set_cookie(response)
        assert header is not None
        # The value is read out of the header rather than the jar: the jar now
        # holds the blank cookie this test planted as well as the minted one.
        minted = header.split(";", 1)[0].removeprefix(f"{_OWNER_COOKIE}=")
        assert minted.strip() != ""
        assert _newest_job(accepting_client).owner_token == minted

    def test_owner_cookie_holder_can_continue_the_flip(
        self,
        accepting_client: TestClient,
        owned_flip: tuple[str, WorkerFlipCoordinator],
    ) -> None:
        """The browser that submitted the job answers Continue (APPL-09)."""
        job_id, coordinator = owned_flip

        response = accepting_client.post("/api/flip/continue", data={"job_id": job_id})

        assert response.status_code == 200
        assert coordinator.answer is FlipOutcome.CONTINUED

    def test_owner_cookie_holder_can_abort_the_flip(
        self,
        accepting_client: TestClient,
        owned_flip: tuple[str, WorkerFlipCoordinator],
    ) -> None:
        """The browser that submitted the job answers Abort (APPL-09)."""
        job_id, coordinator = owned_flip

        response = accepting_client.post("/api/flip/abort", data={"job_id": job_id})

        assert response.status_code == 200
        assert coordinator.answer is FlipOutcome.ABORTED

    def test_owner_cookie_absent_cannot_continue_the_flip(
        self,
        accepting_client: TestClient,
        owned_flip: tuple[str, WorkerFlipCoordinator],
    ) -> None:
        """
        A second browser's Continue is dropped, not errored (D-16, D-24).

        The household member who did not load the paper must not be able to
        start pass B, and must not be shown a failure for trying either: the
        answer is simply not taken and the current status comes back.
        """
        job_id, coordinator = owned_flip

        response = _other_browser(accepting_client).post(
            "/api/flip/continue", data={"job_id": job_id}
        )

        assert response.status_code == 200
        assert 'id="status-area"' in response.text
        assert coordinator.answer is None

    def test_owner_cookie_absent_cannot_abort_the_flip(
        self,
        accepting_client: TestClient,
        owned_flip: tuple[str, WorkerFlipCoordinator],
    ) -> None:
        """A second browser's Abort is dropped the same way (D-16, D-24)."""
        job_id, coordinator = owned_flip

        response = _other_browser(accepting_client).post(
            "/api/flip/abort", data={"job_id": job_id}
        )

        assert response.status_code == 200
        assert 'id="status-area"' in response.text
        assert coordinator.answer is None

    def test_owner_cookie_is_not_required_when_the_job_has_none(
        self, client: TestClient, waiting_flip: tuple[str, WorkerFlipCoordinator]
    ) -> None:
        """
        A NULL owner token means unowned, so anyone may answer (UI-SPEC S5).

        This is the manual-duplex job that was already in flight when the
        appliance was upgraded.  A strict rule would make it un-continuable and
        force it to time out.
        """
        job_id, coordinator = waiting_flip
        job_store: JobStore = _app(client).state.job_store
        staged = job_store.get_job(job_id)
        assert staged is not None
        assert staged.owner_token is None

        response = client.post("/api/flip/continue", data={"job_id": job_id})

        assert response.status_code == 200
        assert coordinator.answer is FlipOutcome.CONTINUED

    def test_owner_cookie_never_reaches_the_body_or_the_log(
        self,
        accepting_client: TestClient,
        owned_flip: tuple[str, WorkerFlipCoordinator],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The token appears in no rendered markup and no log record (T-30-59)."""
        job_id, _ = owned_flip
        minted = accepting_client.cookies[_OWNER_COOKIE]

        with caplog.at_level(logging.DEBUG):
            page = accepting_client.get("/")
            polled = accepting_client.get("/api/jobs/current/status")
            answered = accepting_client.post(
                "/api/flip/continue", data={"job_id": job_id}
            )

        for body in (page.text, polled.text, answered.text):
            assert minted not in body
        assert minted not in caplog.text


# --- The followed job and the queue line (APPL-08, D-25) ---------------------


def _displayed(markup: str) -> str:
    """
    Return the markup with its HTML entities resolved, as a reader sees it.

    Jinja autoescapes the whole busy line, and APPL-08's copy quotes the title
    with apostrophes, so the sentence is spelled with entities in the markup
    and only reads back as written once they are resolved.  Copy assertions use
    this; escaping assertions deliberately do not, because unescaping first
    would make them vacuous.

    Args:
        markup: The rendered response body.

    Returns:
        The same text with entities resolved.

    """
    return html.unescape(markup)


def _running_job(client: TestClient, title: str) -> str:
    """
    Create a SCANNING job and make it the worker's current job.

    Args:
        client: The client whose app owns the store and worker.
        title: The document title to give it.

    Returns:
        The job's id.

    """
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title=title)
    job_store.update_state(job.id, JobState.SCANNING)
    _app(client).state.worker._current_job_id = job.id
    return job.id


def _queued_job(client: TestClient, title: str) -> str:
    """
    Create a job and leave it PENDING, waiting in the queue.

    Args:
        client: The client whose app owns the store.
        title: The document title to give it.

    Returns:
        The job's id.

    """
    job_store: JobStore = _app(client).state.job_store
    return job_store.create_job(profile="default", title=title).id


class TestFollowedJob:
    """
    The status area follows the job this browser submitted (D-25).

    A submitter who sees another household member's scan reported back at them
    learns nothing about their own, which is the whole of APPL-08's complaint.
    """

    def test_followed_job_status_reports_that_job_not_the_running_one(
        self, client: TestClient
    ) -> None:
        """The named job is rendered whatever the worker happens to be running."""
        _running_job(client, "Someone Elses Scan")
        job_store: JobStore = _app(client).state.job_store
        mine = job_store.create_job(profile="default", title="My Scan")
        job_store.finish_job(mine.id, JobState.DONE)

        response = client.get(f"/api/jobs/{mine.id}/status")

        assert response.status_code == 200
        assert "Done: My Scan" in response.text
        assert "Someone Elses Scan" not in response.text

    def test_followed_job_status_falls_back_when_the_id_is_unknown(
        self, client: TestClient
    ) -> None:
        """
        A pruned job degrades to today's inference, not to a 404.

        A 404 would also confirm to a caller which ids exist, which is a fact
        the appliance has no reason to hand out (T-30-61).
        """
        _running_job(client, "Still Running")

        followed = client.get("/api/jobs/no-such-job-at-all/status")
        current = client.get("/api/jobs/current/status")

        assert followed.status_code == 200
        assert followed.text == current.text

    def test_followed_job_poll_url_names_the_followed_job(
        self, client: TestClient
    ) -> None:
        """An active followed job keeps being polled by id."""
        job_id = _running_job(client, "Polled By Id")

        response = client.get(f"/api/jobs/{job_id}/status")

        assert f'hx-get="/api/jobs/{job_id}/status"' in response.text

    def test_followed_job_unknown_id_polls_the_current_url(
        self, client: TestClient
    ) -> None:
        """The fallback rendering polls the current-job URL, as it always did."""
        _running_job(client, "Still Running")

        response = client.get("/api/jobs/no-such-job-at-all/status")

        assert 'hx-get="/api/jobs/current/status"' in response.text

    def test_followed_job_id_is_baked_into_the_scan_response(
        self, accepting_client: TestClient
    ) -> None:
        """POST /api/scan hands back a poll URL naming the job it created."""
        job_id = _submit_scan(accepting_client, "Freshly Submitted")

        response = accepting_client.post(
            "/api/scan", data={"profile": "duplex", "title": "Second Submit"}
        )

        newest = _newest_job(accepting_client).id
        assert newest != job_id
        assert f'hx-get="/api/jobs/{newest}/status"' in response.text

    def test_followed_job_current_status_route_is_unchanged(
        self, client: TestClient
    ) -> None:
        """A browser that submitted nothing keeps today's route and inference."""
        _running_job(client, "Inferred")

        response = client.get("/api/jobs/current/status")

        assert response.status_code == 200
        assert 'hx-get="/api/jobs/current/status"' in response.text
        assert "/api/jobs/current/status" in response.text


class TestQueueLine:
    """
    What a queued submitter is told while they wait (APPL-08, UI-SPEC S5).

    Every case renders through the existing busy branch; no new state branch is
    added, which is what keeps the flip controls disappearing the moment a job
    leaves AWAITING_FLIP.
    """

    def test_queue_line_names_the_running_job_and_the_count(
        self, client: TestClient
    ) -> None:
        """One job ahead reads as APPL-08 writes it."""
        _running_job(client, "Tax return")
        _queued_job(client, "Ahead Of Me")
        mine = _queued_job(client, "Mine")

        response = client.get(f"/api/jobs/{mine}/status")

        assert "Waiting for 'Tax return' to finish (1 ahead of you)" in _displayed(
            response.text
        )

    def test_queue_line_says_next_in_line_for_zero_ahead(
        self, client: TestClient
    ) -> None:
        """The last job in the queue is told it is next, not that zero wait."""
        _running_job(client, "Tax return")
        mine = _queued_job(client, "Mine")

        response = client.get(f"/api/jobs/{mine}/status")

        assert "Waiting for 'Tax return' to finish (next in line)" in _displayed(
            response.text
        )

    def test_queue_line_never_says_zero_ahead_of_you(self, client: TestClient) -> None:
        """
        ``(0 ahead of you)`` is never rendered.

        It is technically true and reads like a bug, which is the one thing
        this milestone is spending itself on removing.
        """
        _running_job(client, "Tax return")
        mine = _queued_job(client, "Mine")

        response = client.get(f"/api/jobs/{mine}/status")

        assert "0 ahead of you" not in _displayed(response.text)

    def test_queue_line_is_absent_when_nothing_is_running(
        self, client: TestClient
    ) -> None:
        """A PENDING job with an idle worker keeps the unchanged progress copy."""
        mine = _queued_job(client, "Mine")

        response = client.get(f"/api/jobs/{mine}/status")

        assert progress_label(JobState.PENDING) in _displayed(response.text)
        assert "Waiting for" not in _displayed(response.text)

    def test_queue_line_escapes_the_running_title(self, client: TestClient) -> None:
        """The title is user data and is autoescaped, never injected (T-30-62)."""
        _running_job(client, "<script>alert(1)</script>")
        mine = _queued_job(client, "Mine")

        response = client.get(f"/api/jobs/{mine}/status")

        assert "<script>alert(1)</script>" not in response.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text

    def test_queue_line_shows_the_front_count_on_pass_b(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pass B leads with the pages already counted on pass A (APPL-03, D-33)."""
        monkeypatch.setattr(ScanWorker, "front_pages", property(lambda _self: 12))
        job_id = _running_job(client, "Duplex Stack")
        job_store: JobStore = _app(client).state.job_store
        job_store.update_state(job_id, JobState.SCANNING_REVERSE)

        response = client.get(f"/api/jobs/{job_id}/status")

        displayed = _displayed(response.text)
        assert "Front: 12 pages · " in displayed
        assert progress_label(JobState.SCANNING_REVERSE) in displayed

    def test_queue_line_is_built_in_the_route_and_not_the_template(self) -> None:
        """
        The template renders one server-built string and composes nothing.

        Templates own no vocabulary (Pattern C); a page that assembled its own
        sentence would be a second place for the copy to drift.
        """
        status = (
            Path(app_module.__file__).parent / "templates" / "partials" / "status.html"
        ).read_text(encoding="utf-8")
        assert "busy_line(" not in status
        assert "progress_label" not in status


def _configure_profiles(client: TestClient, profiles: dict[str, ProfileConfig]) -> None:
    """Replace the worker's profile set, under its own lock (D-19)."""
    _app(client).state.worker._set_profiles(profiles)


class TestProfileDescriptionRoute:
    """GET /api/profiles/description -- the sentence under the select (APPL-05)."""

    def test_a_known_profile_returns_its_description_text_alone(
        self, client: TestClient
    ) -> None:
        """The body is the sentence with no wrapper element around it."""
        _configure_profiles(
            client,
            {"glass": ProfileConfig(description="Scans one page from the glass.")},
        )

        response = client.get("/api/profiles/description", params={"profile": "glass"})

        assert response.status_code == 200
        assert response.text == "Scans one page from the glass."

    def test_an_unknown_profile_name_gets_a_422_from_the_description_route(
        self, client: TestClient
    ) -> None:
        """The name is validated against the profile set, never used as a path."""
        _configure_profiles(client, {"glass": ProfileConfig()})

        response = client.get(
            "/api/profiles/description", params={"profile": "../../etc/passwd"}
        )

        assert response.status_code == 422

    def test_a_missing_profile_parameter_gets_a_422_from_the_description_route(
        self, client: TestClient
    ) -> None:
        """The parameter is required, so an absent one never reaches the worker."""
        response = client.get("/api/profiles/description")

        assert response.status_code == 422

    def test_an_empty_description_returns_an_empty_body(
        self, client: TestClient
    ) -> None:
        """A byte-empty body is what lets the :empty CSS rule hide the slot."""
        _configure_profiles(client, {"bare": ProfileConfig()})

        response = client.get("/api/profiles/description", params={"profile": "bare"})

        assert response.status_code == 200
        assert response.text == ""

    def test_a_description_containing_markup_is_escaped_not_rendered(
        self, client: TestClient
    ) -> None:
        """Config free text is autoescaped and never marked safe (T-30-70)."""
        _configure_profiles(
            client,
            {"evil": ProfileConfig(description="<script>alert(1)</script>")},
        )

        response = client.get("/api/profiles/description", params={"profile": "evil"})

        assert "<script>" not in response.text
        assert "&lt;script&gt;" in response.text

    def test_the_description_route_is_a_plain_def(self, client: TestClient) -> None:
        """Every handler runs on the threadpool, this one included (ROBU-05)."""
        routes = [
            route
            for route in _app(client).routes
            if isinstance(route, APIRoute) and route.path == "/api/profiles/description"
        ]

        assert len(routes) == 1
        assert not inspect.iscoroutinefunction(routes[0].endpoint)


class TestProfileOrdering:
    """The option list the select renders, and the D-21 feeder-first rule."""

    def test_options_carry_the_name_label_and_description_of_each_profile(
        self, client: TestClient
    ) -> None:
        """One option object per profile, carrying all three fields."""
        _configure_profiles(
            client,
            {
                "adf": ProfileConfig(
                    source="ADF",
                    label="Feeder, single-sided",
                    description="Feeds a stack of sheets.",
                )
            },
        )

        options = _profile_options(_app(client).state.worker)

        assert options == (
            _ProfileOption(
                name="adf",
                label="Feeder, single-sided",
                description="Feeds a stack of sheets.",
            ),
        )

    def test_a_blank_label_falls_back_to_the_profile_name(
        self, client: TestClient
    ) -> None:
        """A config written before this phase never shows a blank option (A-3)."""
        _configure_profiles(client, {"adf-duplex": ProfileConfig(source="ADF Duplex")})

        (option,) = _profile_options(_app(client).state.worker)

        assert option.label == "adf-duplex"

    def test_feeder_profiles_lead_the_ordering_when_no_flatbed_source_exists(
        self, client: TestClient
    ) -> None:
        """No flatbed source is the literal definition of sheet-fed (D-21)."""
        _configure_profiles(
            client,
            {
                "pick": ProfileConfig(source="Auto"),
                "stack": ProfileConfig(source="ADF"),
            },
        )

        options = _profile_options(_app(client).state.worker)

        assert [option.name for option in options] == ["stack", "pick"]

    def test_an_automatic_document_feeder_source_takes_the_feeder_ordering(
        self, client: TestClient
    ) -> None:
        """
        The commonest real feeder name starts with the letters of the auto rule.

        ``classify_source`` matches the automatic source by exact equality for
        exactly this reason, and the ordering has to inherit that answer rather
        than ask the source string again.
        """
        _configure_profiles(
            client,
            {
                "mystery": ProfileConfig(source="Whatever"),
                "feeder": ProfileConfig(source="Automatic Document Feeder"),
            },
        )

        options = _profile_options(_app(client).state.worker)

        assert [option.name for option in options] == ["feeder", "mystery"]

    def test_config_order_survives_inside_each_group_of_the_sheet_fed_ordering(
        self, client: TestClient
    ) -> None:
        """The regrouping is stable, so neither group is internally reshuffled."""
        _configure_profiles(
            client,
            {
                "pick-1": ProfileConfig(source="Auto"),
                "stack-1": ProfileConfig(source="ADF"),
                "pick-2": ProfileConfig(source="Auto"),
                "stack-2": ProfileConfig(source="ADF Duplex"),
            },
        )

        options = _profile_options(_app(client).state.worker)

        assert [option.name for option in options] == [
            "stack-1",
            "stack-2",
            "pick-1",
            "pick-2",
        ]

    def test_a_flatbed_source_anywhere_leaves_the_ordering_alone(
        self, client: TestClient
    ) -> None:
        """A device with glass is not sheet-fed, so config order is kept."""
        _configure_profiles(
            client,
            {
                "glass": ProfileConfig(source="Flatbed"),
                "stack": ProfileConfig(source="ADF"),
            },
        )

        options = _profile_options(_app(client).state.worker)

        assert [option.name for option in options] == ["glass", "stack"]

    def test_a_profile_that_disappears_mid_build_is_dropped_from_the_ordering(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A profile rewritten between the two locked calls is skipped, not None."""
        worker = _app(client).state.worker
        _configure_profiles(client, {"stays": ProfileConfig(), "goes": ProfileConfig()})
        real_lookup = worker.get_profile
        monkeypatch.setattr(
            worker,
            "get_profile",
            lambda name: None if name == "goes" else real_lookup(name),
        )

        options = _profile_options(worker)

        assert [option.name for option in options] == ["stays"]

    def test_the_rendered_option_ordering_matches_the_rule(
        self, client: TestClient
    ) -> None:
        """The page consumes the ordered list, not the bare name list."""
        _configure_profiles(
            client,
            {
                "pick": ProfileConfig(source="Auto"),
                "stack": ProfileConfig(source="ADF"),
            },
        )

        response = client.get("/")

        rendered = re.findall(r'<option value="([^"]+)"', response.text)
        assert rendered[:2] == ["stack", "pick"]


# The scan form element as it stood before plan 30-15, byte for byte.  The
# profile select lives inside it and must add nothing to it: the form already
# carries hx-disinherit="hx-disabled-elt", and on htmx 2.0.8 an inherited
# hx-disabled-elt strips disabled from a server-disabled button (C-10).
_SCAN_FORM_ELEMENT = """    <form hx-post="/api/scan"
          hx-target="#status-area"
          hx-swap="outerHTML"
          hx-disabled-elt="#scan-btn"
          hx-disinherit="hx-disabled-elt">
"""

# How many six-digit colour literals app.css held before plan 30-15.  The
# :empty rule hides an element; it introduces no colour of its own.
_APP_CSS_COLOUR_LITERALS = 3


def _web_asset(*parts: str) -> str:
    """Read one shipped template or static file, for the markup assertions."""
    return Path(app_module.__file__).parent.joinpath(*parts).read_text(encoding="utf-8")


def _profile_select_tag(page: str) -> str:
    """Return the rendered opening tag of the profile select, or an empty string."""
    match = re.search(r'<select[^>]*id="profile-select"[^>]*>', page, re.DOTALL)
    return match.group(0) if match else ""


class TestProfileSelectMarkup:
    """UI-SPEC S4: the select's wiring and its live description slot."""

    def test_the_profile_select_carries_its_htmx_and_aria_wiring(
        self, client: TestClient
    ) -> None:
        """The seven attributes S4 specifies, and the swap is never outerHTML."""
        response = client.get("/")

        select = _profile_select_tag(response.text)
        for attribute in (
            'name="profile"',
            'id="profile-select"',
            'aria-describedby="profile-description"',
            'hx-get="/api/profiles/description"',
            'hx-trigger="change"',
            'hx-target="#profile-description"',
            'hx-swap="innerHTML"',
        ):
            assert attribute in select
        assert "outerHTML" not in select

    def test_the_profile_select_option_text_is_the_label_and_the_value_the_name(
        self, client: TestClient
    ) -> None:
        """The value is the wire contract; only the text a human reads changes."""
        _configure_profiles(
            client,
            {
                "glass": ProfileConfig(source="Flatbed", label="Glass (flatbed)"),
                "bare": ProfileConfig(source="Flatbed"),
            },
        )

        response = client.get("/")

        assert (
            '<option value="glass" selected>Glass (flatbed)</option>' in response.text
        )
        assert '<option value="bare">bare</option>' in response.text

    def test_the_profile_select_is_followed_by_the_live_description_slot(
        self, client: TestClient
    ) -> None:
        """Pico's help-text styling needs the slot adjacent to the control."""
        response = client.get("/")

        after_select = response.text.split("</select>", 1)[1]
        assert after_select.lstrip().startswith(
            '<small id="profile-description" aria-live="polite">'
        )

    def test_the_profile_select_slot_holds_the_selected_description_at_first_paint(
        self, client: TestClient
    ) -> None:
        """The page opens with the sentence for the option it opens on."""
        _configure_profiles(
            client,
            {
                "glass": ProfileConfig(
                    source="Flatbed", description="Scans one page from the glass."
                )
            },
        )

        response = client.get("/")

        assert (
            '<small id="profile-description" aria-live="polite">'
            "Scans one page from the glass.</small>" in response.text
        )
        assert response.text.count('id="profile-description"') == 1

    def test_the_profile_select_adds_no_attribute_to_the_scan_form(
        self, client: TestClient
    ) -> None:
        """The C-10 fix is untouched and the select inherits nothing harmful."""
        source = _web_asset("templates", "index.html")

        assert _SCAN_FORM_ELEMENT in source
        assert source.count('hx-disabled-elt="') == 1
        assert "hx-disabled-elt" not in _profile_select_tag(client.get("/").text)

    def test_the_profile_select_has_exactly_one_help_line(
        self, client: TestClient
    ) -> None:
        """The description doubles as the control's help text (APPL-10)."""
        response = client.get("/")

        under_profile = response.text.split('<label for="profile-select">', 1)[1].split(
            '<label for="title-input">', 1
        )[0]
        assert under_profile.count("<small") == 1

    def test_the_profile_select_empty_slot_rule_is_the_only_new_css(self) -> None:
        """One rule, hiding the slot; no new colour value (UI-SPEC S4)."""
        css = _web_asset("static", "app.css")

        assert "#profile-description:empty {\n    display: none;\n}" in css
        assert css.count("#profile-description") == 1
        assert len(re.findall(r"#[0-9a-fA-F]{6}", css)) == _APP_CSS_COLOUR_LITERALS

    def test_the_profile_select_still_submits_the_chosen_profile(
        self, client: TestClient
    ) -> None:
        """Changing the option text did not change what the form posts."""
        response = client.post(
            "/api/scan", data={"profile": "duplex", "title": "Select Submit"}
        )

        assert response.status_code == 200
        job_store: JobStore = _app(client).state.job_store
        assert job_store.list_recent(limit=1)[0].profile == "duplex"


# The tag rows every filter test runs against.  Three, not two: the cases need a
# match, a non-match, and a second match whose capitalisation differs from the
# query's -- "Recipes" against a lower-case "rec" is what makes the
# case-insensitivity assertion mean anything at all.
_TAG_ROWS: list[dict[str, object]] = [
    {"id": 1, "name": "receipt"},
    {"id": 2, "name": "invoice"},
    {"id": 3, "name": "Recipes"},
]

# The documented cap on the tag filter is 100 characters.  The number is
# written out here rather than imported so the two boundary tests pin it from
# both sides: a value at the cap must be accepted and a value over it must be
# refused, and a route that quietly moved the cap would fail one of them.
_FILTER_AT_THE_CAP = "b" * 100
_OVER_LONG_FILTER = "a" * 500

# A filter value that would be an injected script if it were ever echoed.  It is
# well under the cap, so it reaches the filter rather than the 422 path, which is
# the case worth proving (T-30-74).
_SCRIPT_FILTER = "<script>alert(1)</script>"


class _RecordingTransport:
    """An httpx handler that records every request and answers with no tags."""

    def __init__(self) -> None:
        """Start with an empty record."""
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """
        Record the request and answer with an empty paperless-ngx page.

        Args:
            request: The request the client issued.

        Returns:
            A 200 carrying an empty collection page, so the client's
            pagination loop terminates on the first page.

        """
        self.requests.append(request)
        return httpx.Response(200, json={"count": 0, "results": []})


def _serve_tag_rows(client: TestClient) -> FastAPI:
    """
    Make `_TAG_ROWS` the tag list the app sees, cache included.

    Args:
        client: The client whose app is being wired.

    Returns:
        The app, for tests that need to reach further into its state.

    """
    app = _app(client)
    app.state.paperless.get_tags = lambda: list(_TAG_ROWS)
    app.state.cache.invalidate("tags")
    return app


def _count_upstream(app: FastAPI) -> _RecordingTransport:
    """
    Swap in a Paperless client whose every request is recorded, not sent.

    Args:
        app: The app whose client is replaced.

    Returns:
        The recorder the new client's transport writes to.

    """
    handler = _RecordingTransport()
    app.state.paperless = PaperlessClient(
        url="http://paperless.invalid:8000",
        token="a-real-looking-token",
        _transport=httpx.MockTransport(handler),
    )
    return handler


def _checkbox(markup: str, tag_id: int) -> str:
    """
    Return the rendered checkbox input for one tag id, or an empty string.

    Args:
        markup: The rendered tag list.
        tag_id: The paperless-ngx tag id to look for.

    Returns:
        The matching `<input>` tag, or `""` when the tag is not rendered.

    """
    match = re.search(
        rf'<input type="checkbox" name="tags" value="{tag_id}"[^>]*>', markup
    )
    return match.group(0) if match else ""


class TestTagFilter:
    """
    UI-SPEC S6: the server-side tag filter that can never drop a tick.

    Two hazards are designed out rather than guarded against, and these tests
    are what hold the design in place: a filter swap must not lose a ticked tag
    (A-5), and the filter text must never reach paperless-ngx or the page
    (T-30-74, T-30-75).
    """

    def test_tag_filter_absent_renders_every_tag_as_an_unchecked_checkbox(
        self, client: TestClient
    ) -> None:
        """No query renders the whole list, in the wrapper the swap replaces."""
        _serve_tag_rows(client)

        response = client.get("/api/tags")

        assert response.status_code == 200
        assert 'id="tags-list"' in response.text
        assert response.text.count('type="checkbox"') == 3
        assert "checked" not in response.text
        for name in ("receipt", "invoice", "Recipes"):
            assert name in response.text

    def test_tag_filter_narrows_the_list_case_insensitively(
        self, client: TestClient
    ) -> None:
        """``rec`` matches ``receipt`` and ``Recipes`` but never ``invoice``."""
        _serve_tag_rows(client)

        response = client.get("/api/tags", params={"q": "rec"})

        assert response.status_code == 200
        assert "receipt" in response.text
        assert "Recipes" in response.text
        assert "invoice" not in response.text

    def test_tag_filter_renders_the_carried_selection_checked(
        self, client: TestClient
    ) -> None:
        """A tag id the request carries comes back ticked (A-5)."""
        _serve_tag_rows(client)

        response = client.get("/api/tags", params={"q": "rec", "tags": [3]})

        assert "checked" in _checkbox(response.text, 3)
        assert "checked" not in _checkbox(response.text, 1)

    def test_tag_filter_pins_a_selected_tag_the_filter_excludes(
        self, client: TestClient
    ) -> None:
        """A tick outside the filter stays in the DOM, above the list (A-5)."""
        _serve_tag_rows(client)

        response = client.get("/api/tags", params={"q": "rec", "tags": [2, 3]})

        pinned = _checkbox(response.text, 2)
        assert "checked" in pinned
        assert response.text.index('value="2"') < response.text.index('value="3"')

    def test_tag_filter_renders_a_selected_and_matched_tag_exactly_once(
        self, client: TestClient
    ) -> None:
        """A tag both ticked and matched is not rendered twice."""
        _serve_tag_rows(client)

        response = client.get("/api/tags", params={"q": "rec", "tags": [3]})

        assert response.text.count('value="3"') == 1

    def test_tag_filter_never_echoes_the_query_into_the_response(
        self, client: TestClient
    ) -> None:
        """The filter is a filter, never a label: it is not rendered (T-30-74)."""
        _serve_tag_rows(client)

        response = client.get("/api/tags", params={"q": _SCRIPT_FILTER})

        assert response.status_code == 200
        assert _SCRIPT_FILTER not in response.text
        assert html.escape(_SCRIPT_FILTER) not in response.text
        assert "alert(1)" not in response.text

    def test_tag_filter_accepts_a_query_at_the_documented_cap(
        self, client: TestClient
    ) -> None:
        """A value exactly at the cap is filtered, not refused."""
        _serve_tag_rows(client)

        response = client.get("/api/tags", params={"q": _FILTER_AT_THE_CAP})

        assert response.status_code == 200

    def test_tag_filter_rejects_an_over_long_query_with_422(
        self, client: TestClient
    ) -> None:
        """An unbounded filter is refused at the boundary (T-30-76)."""
        _serve_tag_rows(client)

        response = client.get("/api/tags", params={"q": _OVER_LONG_FILTER})

        assert response.status_code == 422

    def test_tag_filter_issues_no_upstream_request_when_the_cache_is_warm(
        self, client: TestClient
    ) -> None:
        """Filtering reads the cache; it is not a new fetch (T-30-75)."""
        app = _app(client)
        handler = _count_upstream(app)
        app.state.cache.set("tags", list(_TAG_ROWS))

        response = client.get("/api/tags", params={"q": "rec"})

        assert response.status_code == 200
        assert "receipt" in response.text
        assert handler.requests == []

    def test_tag_filter_never_forwards_the_query_to_paperless(
        self, client: TestClient
    ) -> None:
        """A cold cache fetches the whole list and carries no ``q`` (T-30-75)."""
        app = _app(client)
        handler = _count_upstream(app)
        app.state.cache.invalidate("tags")

        response = client.get("/api/tags", params={"q": "receipt"})

        assert response.status_code == 200
        assert handler.requests
        for request in handler.requests:
            assert "q" not in request.url.params
            assert "receipt" not in str(request.url)

    def test_tag_filter_empty_paperless_renders_the_empty_state(
        self, client: TestClient
    ) -> None:
        """No tags at all is its own sentence, not a blank box."""
        app = _app(client)
        app.state.paperless.get_tags = list
        app.state.cache.invalidate("tags")

        response = client.get("/api/tags")

        assert "No tags in paperless-ngx yet." in response.text
        assert "No tags match that filter." not in response.text

    def test_tag_filter_matching_nothing_renders_the_no_match_state(
        self, client: TestClient
    ) -> None:
        """A filter that matches nothing says so, and says something else."""
        _serve_tag_rows(client)

        response = client.get("/api/tags", params={"q": "zzz"})

        assert "No tags match that filter." in response.text
        assert "No tags in paperless-ngx yet." not in response.text

    def test_tag_filter_survives_a_cache_invalidate_with_the_selection(
        self, client: TestClient
    ) -> None:
        """The refresh button re-renders the same wrapper, filter and ticks intact."""
        _serve_tag_rows(client)

        response = client.post(
            "/api/cache/invalidate?resource=tags",
            data={"q": "rec", "tags": ["3"]},
        )

        assert response.status_code == 200
        assert 'id="tags-list"' in response.text
        assert "checked" in _checkbox(response.text, 3)
        assert "invoice" not in response.text

    def test_tag_filter_text_on_a_scan_submit_does_not_refuse_the_scan(
        self, client: TestClient
    ) -> None:
        """
        The belt to the form-owner attribute's braces (A-6).

        The filter input's HTML form owner is ``#tag-filter-form``, so a scan
        cannot carry ``q`` at all.  This covers the residual case -- a scripted
        client, or a browser that lost the attribute -- by proving the route
        ignores the field rather than refusing the submit.
        """
        response = client.post(
            "/api/scan",
            data={"profile": "default", "title": "Stray Filter", "q": "rec"},
        )

        assert response.status_code == 200
        job_store: JobStore = _app(client).state.job_store
        assert job_store.list_recent(limit=1)[0].title == "Stray Filter"


# The profile defaults the D-29 regression tests drive, chosen so neither can
# be produced by accident: no fixture tag or correspondent uses these ids.
_PROFILE_DEFAULT_TAGS = [41, 42]
_PROFILE_DEFAULT_CORRESPONDENT = 43

# The two rules UI-SPEC S6 adds to app.css, property by property. Written out
# here rather than matched loosely, because "the tap target is 44 px" is the
# whole of D-30 and a rule that lost one declaration would still look right.
_TAG_OPTION_RULE = """label.tag-option {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
    min-height: 2.75rem;
    margin-bottom: 0;
    cursor: pointer;
}"""

_TAG_LIST_RULE = """.tag-list {
    max-height: 17.5rem;
    overflow-y: auto;
    margin-bottom: var(--pico-spacing);
}"""

# How many six-digit colour literals app.css held before this plan. A touch
# target is a size, not a colour, so the count may not move.
_APP_CSS_COLOURS_BEFORE_30_16 = 3


def _simple_form_app(
    tmp_path: Path,
    *,
    show_tags: bool = True,
    show_correspondent: bool = True,
    credential: str = "a-real-looking-token",
) -> FastAPI:
    """
    Build an app whose scan form has the given shape, with two stub profiles.

    Args:
        tmp_path: Where the app writes its database and files.
        show_tags: Whether the Tags fieldset is rendered at all.
        show_correspondent: Whether the Correspondent control is rendered.
        credential: The configured paperless-ngx token; a placeholder here is
            how a caller reaches ``start_scan``'s refusal without a second
            fixture.

    Returns:
        The app, whose lifespan starts with its TestClient.

    """
    settings = Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(url="http://localhost:8000", token=credential),
        output=OutputConfig(tmp_dir=str(tmp_path), data_dir=str(tmp_path)),
        web=WebConfig(show_tags=show_tags, show_correspondent=show_correspondent),
        profiles={
            "default": ProfileConfig(
                default_tags=_PROFILE_DEFAULT_TAGS,
                default_correspondent=_PROFILE_DEFAULT_CORRESPONDENT,
            ),
            "duplex": ProfileConfig(source="ADF Duplex"),
        },
    )
    app = create_app(settings, StubScannerBackend())
    app.state.paperless.get_tags = lambda: list(_TAG_ROWS)
    app.state.paperless.get_correspondents = list
    return app


class TestSimpleForm:
    """
    D-28 and D-29: the owner can shrink the form without changing the scan.

    Two separate claims, and the second is the one that could go wrong quietly.
    Hiding a control changes what a household member is asked; it must not
    change what the appliance does, so the profile's defaults still apply when
    the control that would have overridden them is not on the page.
    """

    def test_simple_form_without_tags_renders_no_part_of_the_tag_block(
        self, tmp_path: Path
    ) -> None:
        """The fieldset, the filter, its form and both help lines all go."""
        with TestClient(_simple_form_app(tmp_path, show_tags=False)) as client:
            page = client.get("/").text

        for fragment in (
            'id="tags-list"',
            'id="tag-filter"',
            'id="tag-filter-form"',
            'id="tags-help"',
            'aria-label="Refresh tags"',
            'name="tags"',
        ):
            assert fragment not in page, fragment

    def test_simple_form_without_correspondent_renders_no_part_of_it(
        self, tmp_path: Path
    ) -> None:
        """The select, its refresh button and its help line all go."""
        with TestClient(_simple_form_app(tmp_path, show_correspondent=False)) as client:
            page = client.get("/").text

        for fragment in (
            'id="correspondent-select"',
            'id="correspondent-help"',
            'aria-label="Refresh correspondents"',
            'name="correspondent"',
        ):
            assert fragment not in page, fragment

    def test_simple_form_with_both_off_is_profile_title_and_scan(
        self, tmp_path: Path
    ) -> None:
        """What is left is the shortest form the appliance has."""
        with TestClient(
            _simple_form_app(tmp_path, show_tags=False, show_correspondent=False)
        ) as client:
            page = client.get("/").text

        assert 'name="profile"' in page
        assert 'name="title"' in page
        assert 'id="scan-btn"' in page
        # Profile and Title keep their help lines; the two that went with the
        # hidden controls are the only ones that leave.
        assert re.findall(r'<small id="([^"]+)"', page) == [
            "profile-description",
            "title-help",
        ]

    def test_simple_form_keeps_every_control_when_both_are_on(
        self, tmp_path: Path
    ) -> None:
        """The default shape is the full form, so an upgrade changes nothing."""
        with TestClient(_simple_form_app(tmp_path)) as client:
            page = client.get("/").text

        for fragment in (
            'id="tags-list"',
            'id="tag-filter"',
            'id="tag-filter-form"',
            'id="tags-help"',
            'id="correspondent-select"',
            'id="correspondent-help"',
        ):
            assert fragment in page, fragment

    def test_simple_form_hides_by_absence_and_never_with_css(
        self, tmp_path: Path
    ) -> None:
        """D-28: the controls are not rendered, not rendered-then-hidden."""
        with TestClient(
            _simple_form_app(tmp_path, show_tags=False, show_correspondent=False)
        ) as client:
            page = client.get("/").text

        assert "display: none" not in page
        assert "display:none" not in page
        assert " hidden" not in page

    def test_simple_form_without_tags_still_applies_the_profile_default_tags(
        self, tmp_path: Path
    ) -> None:
        """D-29: hiding the control changes the form, never the scan."""
        with TestClient(_simple_form_app(tmp_path, show_tags=False)) as client:
            response = client.post(
                "/api/scan", data={"profile": "default", "title": "Defaults Apply"}
            )
            assert response.status_code == 200
            job_store: JobStore = _app(client).state.job_store
            assert job_store.list_recent(limit=1)[0].tags == _PROFILE_DEFAULT_TAGS

    def test_simple_form_without_correspondent_still_applies_its_default(
        self, tmp_path: Path
    ) -> None:
        """The correspondent half of the same claim (D-29)."""
        with TestClient(_simple_form_app(tmp_path, show_correspondent=False)) as client:
            response = client.post(
                "/api/scan", data={"profile": "default", "title": "Defaults Apply"}
            )
            assert response.status_code == 200
            job_store: JobStore = _app(client).state.job_store
            job = job_store.list_recent(limit=1)[0]
            assert job.correspondent == _PROFILE_DEFAULT_CORRESPONDENT

    def test_simple_form_lets_a_visible_control_override_the_default(
        self, tmp_path: Path
    ) -> None:
        """
        The fallback is a fallback, not an override.

        A profile default that won over what the user ticked would be a much
        worse bug than no default at all, so the full form is asserted too.
        """
        with TestClient(_simple_form_app(tmp_path)) as client:
            response = client.post(
                "/api/scan",
                data={"profile": "default", "title": "Chosen", "tags": ["7"]},
            )
            assert response.status_code == 200
            job_store: JobStore = _app(client).state.job_store
            assert job_store.list_recent(limit=1)[0].tags == [7]

    def test_simple_form_tag_rows_are_a_thumb_sized_tap_target(self) -> None:
        """D-30: the label is the target and it clears 44 px at a 16 px root."""
        css = _web_asset("static", "app.css")

        assert _TAG_OPTION_RULE in css
        assert _TAG_LIST_RULE in css

    def test_simple_form_tap_target_rule_outweighs_pico_by_load_order(self) -> None:
        """
        The selector is `label.tag-option`, never the bare class.

        Pico's own `label:has([type=checkbox])` rule carries the same weight, so
        the tie is what makes this win -- and a tie only wins because app.css
        loads second.
        """
        css = _web_asset("static", "app.css")

        assert "\n.tag-option {" not in css
        assert css.count("label.tag-option {") == 1

    def test_simple_form_touch_targets_add_no_colour_to_the_stylesheet(self) -> None:
        """A tap target is a size; the palette may not move (UI-SPEC S6)."""
        css = _web_asset("static", "app.css")

        assert len(re.findall(r"#[0-9a-fA-F]{6}", css)) == _APP_CSS_COLOURS_BEFORE_30_16


def _newest_job(client: TestClient) -> Job:
    """
    Return the job row the submit under test just created.

    The fact these tests are about is what was *stored*, not what the response
    said, so every assertion reads the row back rather than the body.

    Args:
        client: The client whose app holds the job store.

    Returns:
        The most recent job row.

    """
    job_store: JobStore = _app(client).state.job_store
    rows = job_store.list_recent(limit=1)
    assert len(rows) == 1
    return rows[0]


class TestProfileDefaultsFollowTheFormShape:
    """
    WR-06: a profile default fills in for a control nobody was shown.

    An empty tag list and an absent correspondent reach ``start_scan``
    identically whether the control was never rendered or was rendered and
    then cleared, so the submit on its own cannot tell the two apart.  The
    config key that decided whether to render the control is the only fact
    that can, and gating on it is what lets a household member untick every
    box and get a job with no tags -- something the appliance could do before
    this phase and lost to an ungated fallback.
    """

    def test_a_cleared_tag_list_submits_no_tags(self, tmp_path: Path) -> None:
        """The review's named regression test: unticking every box means none."""
        with TestClient(_simple_form_app(tmp_path)) as client:
            response = client.post(
                "/api/scan", data={"profile": "default", "title": "Cleared"}
            )

            assert response.status_code == 200
            assert _newest_job(client).tags == []

    def test_a_hidden_tag_control_still_applies_the_profile_default(
        self, tmp_path: Path
    ) -> None:
        """D-29's half: with no control on the page the profile answers."""
        with TestClient(_simple_form_app(tmp_path, show_tags=False)) as client:
            response = client.post(
                "/api/scan", data={"profile": "default", "title": "Hidden"}
            )

            assert response.status_code == 200
            assert _newest_job(client).tags == _PROFILE_DEFAULT_TAGS

    def test_a_ticked_tag_beats_the_profile_default(self, tmp_path: Path) -> None:
        """A submit that names tags keeps them, with the control on the page."""
        with TestClient(_simple_form_app(tmp_path)) as client:
            response = client.post(
                "/api/scan",
                data={"profile": "default", "title": "Ticked", "tags": ["7"]},
            )

            assert response.status_code == 200
            assert _newest_job(client).tags == [7]

    def test_a_cleared_correspondent_submits_no_correspondent(
        self, tmp_path: Path
    ) -> None:
        """The correspondent half of the same regression."""
        with TestClient(_simple_form_app(tmp_path)) as client:
            response = client.post(
                "/api/scan", data={"profile": "default", "title": "Cleared"}
            )

            assert response.status_code == 200
            assert _newest_job(client).correspondent is None

    def test_a_hidden_correspondent_control_still_applies_the_default(
        self, tmp_path: Path
    ) -> None:
        """The correspondent half of D-29."""
        with TestClient(_simple_form_app(tmp_path, show_correspondent=False)) as client:
            response = client.post(
                "/api/scan", data={"profile": "default", "title": "Hidden"}
            )

            assert response.status_code == 200
            assert _newest_job(client).correspondent == _PROFILE_DEFAULT_CORRESPONDENT

    def test_the_two_flags_are_read_independently(self, tmp_path: Path) -> None:
        """One control off and the other on resolves one default and not both."""
        app = _simple_form_app(tmp_path, show_tags=False, show_correspondent=True)
        with TestClient(app) as client:
            response = client.post(
                "/api/scan", data={"profile": "default", "title": "Mixed"}
            )

            assert response.status_code == 200
            job = _newest_job(client)
            assert job.tags == _PROFILE_DEFAULT_TAGS
            assert job.correspondent is None

    def test_the_placeholder_token_refusal_still_runs_after_the_defaults(
        self, tmp_path: Path
    ) -> None:
        """The gate moves nothing else: one REJECTED row, exactly as before."""
        with TestClient(_simple_form_app(tmp_path, credential="changeme")) as client:
            response = client.post(
                "/api/scan", data={"profile": "default", "title": "Unset Token"}
            )

            assert response.status_code == 503
            job = _newest_job(client)
            assert job.state is JobState.ERROR
            assert job.error == TOKEN_UNSET_JOB_ERROR
            assert job.error_category is ErrorCategory.REJECTED

    def test_a_blank_title_still_falls_back_the_way_it_did(
        self, tmp_path: Path
    ) -> None:
        """``resolve_job_title`` runs on the line above and is untouched."""
        with TestClient(_simple_form_app(tmp_path, show_tags=False)) as client:
            response = client.post(
                "/api/scan", data={"profile": "default", "title": "   "}
            )

            assert response.status_code == 200
            assert _newest_job(client).title.startswith("Scan ")
