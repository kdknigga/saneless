"""
Web endpoint tests for the saneless FastAPI application.

Covers requirements: UI-01 through UI-08, PROF-03, PLSS-04,
HLTH-01, HLTH-02, LOG-03.
"""

from __future__ import annotations

import ast
import html
import inspect
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.parse import parse_qs, urlsplit

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from saneless.job import Job

import httpx2
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from saneless.auto_profiles import generate_profiles
from saneless.checks import (
    check_name,
    check_row_class,
    check_row_glyph,
    check_row_label,
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
from saneless.scanner.base import DeviceCapabilities
from saneless.vocabulary import (
    QUEUE_FULL_JOB_ERROR,
    TOKEN_UNSET_JOB_ERROR,
    ErrorCategory,
    FlipOutcome,
    PassAnswer,
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
from saneless.web import routes as routes_module
from saneless.web.app import create_app
from saneless.web.checks_cache import CheckCache
from saneless.web.refresher import CheckRefresher
from saneless.web.routes import _profile_options, _ProfileOption
from saneless.worker import ScanWorker, WorkerFlipCoordinator
from tests.conftest import StubScannerBackend, leaf_routes

# Every app built in this module, fixture or helper, talks to a Paperless client
# whose requests fail inside the process: nothing reaches localhost:8000.
pytestmark = pytest.mark.usefixtures("offline_paperless")


def _app(client: TestClient) -> FastAPI:
    """Extract the FastAPI app from a TestClient, helping the type checker."""
    app: Any = client.app
    if not isinstance(app, FastAPI):
        msg = "Expected FastAPI app"
        raise TypeError(msg)
    return app


@pytest.fixture
def web_settings(make_settings: Callable[..., Settings]) -> Settings:
    """Build the web app's settings: the suite defaults plus a ``duplex`` profile."""
    return make_settings(
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
def app(web_settings: Settings, web_scanner: StubScannerBackend) -> FastAPI:
    """Create the FastAPI app with test settings and stub scanner."""
    return create_app(web_settings, web_scanner)


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


# The owner token a test hands its client when the rows it stages should render
# as their owner sees them.  A job's title, preview and detail text reach only
# the browser that started it; every other browser, and every browser for a row
# that recorded no owner, sees the generic title instead.
_VIEWER_TOKEN = "tok-owner"


def _as_owner(client: TestClient) -> str:
    """
    Make the client present ``_VIEWER_TOKEN``, and return it to record on rows.

    Args:
        client: The browser that should see its rows in full.

    Returns:
        The token, for ``create_job(owner_token=...)``.

    """
    client.cookies.set(routes_module.OWNER_COOKIE, _VIEWER_TOKEN)
    return _VIEWER_TOKEN


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
    """GET /health returns 200 with status ok, and needs no authentication."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_no_route_handler_is_a_coroutine(client: TestClient) -> None:
    """
    Every route handler is a plain ``def`` (ROBU-05, M-01).

    Each handler calls blocking code, and FastAPI only moves ``def`` handlers
    onto its threadpool; an ``async def`` one would block the event loop.  The
    client fixture is used so the lifespan closes the job store afterwards.
    """
    routes = [
        route for route in leaf_routes(_app(client)) if isinstance(route, APIRoute)
    ]
    # The exact count, not just a non-empty one: a filter that found a single
    # route would satisfy `assert routes` while leaving the other eighteen
    # handlers unchecked.
    assert len(routes) == 19, (
        f"the app serves {len(routes)} API routes, not the 19 this test pins; "
        f"a route was added or removed, so update this literal"
    )
    for route in routes:
        assert not inspect.iscoroutinefunction(route.endpoint), route.path


def test_health_answers_while_a_request_blocks(client: TestClient) -> None:
    """/health answers promptly while another request is blocked in I/O (ROBU-05)."""
    gate = threading.Event()
    entered = threading.Event()
    app = _app(client)

    def blocking_get_tags() -> list[dict[str, object]]:
        entered.set()
        gate.wait(5)
        return []

    app.state.paperless.get_tags = blocking_get_tags
    app.state.cache.invalidate("tags")
    slow = threading.Thread(target=lambda: client.get("/api/tags"))
    slow.start()
    try:
        assert entered.wait(5)
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
    """
    A failure with no list fetched before renders empty options and logs why.

    Nothing has fetched the tags in this app yet, so there is no previous list
    to fall back on (``tests/test_metadata_fallback.py`` covers the case where
    there is).  The failure here is not a Paperless error, so the warning
    names its class alone: neither its text nor a traceback is logged.
    """
    app = _app(client)

    def failing_get_tags() -> list[dict[str, object]]:
        msg = "paperless unreachable"
        raise ConnectionError(msg)

    app.state.paperless.get_tags = failing_get_tags
    with caplog.at_level(logging.WARNING, logger="saneless.web.routes"):
        response = client.get("/api/tags")

    assert response.status_code == 200
    assert "receipt" not in response.text
    assert app.state.cache.get("tags") is None
    records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "using empty list" in r.getMessage()
    ]
    assert len(records) == 1
    assert records[0].getMessage().endswith("using empty list: ConnectionError")
    assert "paperless unreachable" not in caplog.text
    assert records[0].exc_info is None


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


def test_profile_dropdown(client: TestClient, web_settings: Settings) -> None:
    """Profile names from settings appear in dropdown (PROF-03)."""
    response = client.get("/")
    for profile_name in web_settings.profiles:
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
    monkeypatch.setattr(
        app.state.worker, "submit", lambda _job, _options: SubmitResult.ACCEPTED
    )
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
    """The owner's job with a thumbnail shows the base64 img tag (UI-04)."""
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(
        profile="default", title="Thumb Test", owner_token=_as_owner(client)
    )
    job_store.update_thumbnail(job.id, "dGVzdA==")
    job_store.update_state(job.id, JobState.SCANNING)
    _app(client).state.worker._current_job_id = job.id

    response = client.get("/api/jobs/current/status")
    assert "data:image/jpeg;base64,dGVzdA==" in response.text


def test_job_history(client: TestClient) -> None:
    """GET /api/jobs/history returns the owner's jobs by title (UI-05)."""
    job_store: JobStore = _app(client).state.job_store
    titles = ["Job Alpha", "Job Beta", "Job Gamma"]
    owner = _as_owner(client)
    for title in titles:
        job_store.create_job(profile="default", title=title, owner_token=owner)

    response = client.get("/api/jobs/history")
    assert response.status_code == 200
    for title in titles:
        assert title in response.text


def test_error_display(client: TestClient) -> None:
    """Error state shows the owner its error message in status area (LOG-03)."""
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(
        profile="default", title="Error Test", owner_token=_as_owner(client)
    )
    job_store.update_state(job.id, JobState.ERROR, error="Scanner disconnected")
    _app(client).state.worker._current_job_id = job.id

    response = client.get("/api/jobs/current/status")
    assert "Scanner disconnected" in response.text


def test_cache_invalidate(client: TestClient) -> None:
    """POST /api/cache/invalidate refetches the resource and renders it."""
    app = _app(client)
    app.state.cache.set("tags", [{"id": 1, "name": "receipt"}])
    fetches: list[int] = []

    def fresh_tags() -> list[dict[str, object]]:
        fetches.append(1)
        return [{"id": 7, "name": "tax-return"}]

    app.state.paperless.get_tags = fresh_tags

    response = client.post("/api/cache/invalidate?resource=tags")

    assert response.status_code == 200
    assert fetches == [1]
    assert "tax-return" in response.text
    assert "receipt" not in response.text
    assert app.state.cache.get("tags") == [{"id": 7, "name": "tax-return"}]


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
    job = job_store.create_job(
        profile="default", title="Running Job", owner_token=_as_owner(client)
    )
    job_store.update_state(job.id, JobState.SCANNING)
    worker._current_job_id = job.id
    monkeypatch.setattr(
        worker, "submit", lambda _job, _options: SubmitResult.QUEUE_FULL
    )

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
    # History still lists the rejected attempt (D-05).  A submit refused after
    # its row was written keeps the token it was written with, so the browser
    # that sent it still sees its title.
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
    job = job_store.create_job(
        profile="duplex", title="Flip Just Finished", owner_token=_as_owner(client)
    )
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
    job = job_store.create_job(
        profile="duplex", title="Flip Timed Out", owner_token=_as_owner(client)
    )
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
    assert response.status_code == 500
    data = response.json()
    assert data["status"] == "error"
    assert data["detail"] == "RuntimeError"
    assert "boom" not in response.text


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


def test_paperless_test_500_sanitizes_exception(client: TestClient) -> None:
    """500 response returns exception class name, not raw message with secrets (RH-04)."""
    sensitive_msg = "http://192.168.1.100:8000 token=abc123"
    _app(client).state.paperless.test_connection = _raise_factory(
        ConnectionError, sensitive_msg
    )
    response = client.get("/api/paperless/test")
    assert response.status_code == 500
    data = response.json()
    assert data["detail"] == "ConnectionError"
    assert "192.168.1.100" not in response.text
    assert "abc123" not in response.text


def _raise_factory(exc_type: type[Exception], msg: str) -> Callable[[], NoReturn]:
    """Create a callable that raises the given exception with the given message."""

    def _raise() -> NoReturn:
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
        self, web_settings: Settings, web_scanner: StubScannerBackend
    ) -> Iterator[FastAPI]:
        """Build the app and never enter its lifespan, closing what it opened."""
        built = create_app(web_settings, web_scanner)
        try:
            yield built
        finally:
            built.state.paperless.close()
            built.state.job_store.close()

    def test_registers_the_filters_the_templates_reach_for(
        self, unstarted_app: FastAPI
    ) -> None:
        """Every name this phase's templates reach for is registered (APPL-03)."""
        filters = unstarted_app.state.templates.env.filters
        expected = {
            "check_row_class",
            "check_row_glyph",
            "check_row_label",
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
        assert filters["check_row_class"] is check_row_class
        assert filters["check_row_glyph"] is check_row_glyph
        assert filters["check_row_label"] is check_row_label
        assert filters["check_name"] is check_name
        assert filters["error_message"] is error_message
        assert filters["error_next_step"] is error_next_step
        assert filters["page_counts"] is page_counts
        assert filters["local_time"] is local_time

    def test_the_state_lookups_are_not_filters(self, unstarted_app: FastAPI) -> None:
        """
        R4-IN-03: a filter no template may correctly use is not registered.

        ``check_state_class`` and its two siblings draw a marker from the
        state alone, which is what rendered a skipped row as a green tick
        (R3-WR-03).  The strip reaches for the ``check_row_*`` filters, and
        leaving the state lookups in the template namespace handed the next
        row's author two plausible names of which only one is right.  The
        Python functions stay; it is the filter names that are gone.
        """
        filters = unstarted_app.state.templates.env.filters
        state_lookups = {"check_state_class", "check_state_glyph", "check_state_label"}
        assert not state_lookups & set(filters)

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
        self, unstarted_app: FastAPI, web_settings: Settings
    ) -> None:
        """
        The five earlier injections hold, and the label filters are the right ones.

        ``state_label`` is deliberately absent: it reads the state alone, so it
        would call a warned upload "Complete".  Every label goes through
        ``job_label``, which also reads the warning.
        """
        state = unstarted_app.state
        assert isinstance(state.worker, ScanWorker)
        assert isinstance(state.job_store, JobStore)
        assert state.settings is web_settings
        assert state.paperless is not None
        assert state.cache is not None
        filters = state.templates.env.filters
        assert {"job_label", "progress_label", "flip_answer_label"} <= set(filters)
        assert "state_label" not in filters

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

# The owner cookie's lifetime, spelled out for the same reason as its name: a
# year in seconds, so a browser keeps its ownership across restarts.
_OWNER_COOKIE_MAX_AGE_ATTRIBUTE = "max-age=31536000"


def _owner_set_cookie(response: httpx2.Response) -> str | None:
    """
    Return the raw ``Set-Cookie`` header carrying the owner token, if any.

    The raw header is parsed instead of the client's cookie jar because the jar
    normalises away exactly what D-06 pins: the ``Max-Age`` value and an absent
    ``Secure`` are both invisible once httpx2 has turned the header into a jar
    entry, so a jar assertion could not tell a year-long cookie from a session
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


def _owner_cookie_value(header: str) -> str:
    """
    Return the token a raw owner ``Set-Cookie`` header carries.

    Args:
        header: A header line returned by ``_owner_set_cookie``.

    Returns:
        The value between the cookie's name and its first attribute.

    """
    return header.split(";", 1)[0].removeprefix(f"{_OWNER_COOKIE}=")


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
        _app(client).state.worker,
        "submit",
        lambda _job, _options: SubmitResult.ACCEPTED,
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

    Covers APPL-09 and decisions D-23 (one token per browser), D-24 (the token
    gates the two flip buttons) and D-06 (the cookie lives a year, so ownership
    survives a browser restart).
    """

    def test_owner_cookie_is_httponly_lax_and_lives_a_year(
        self, accepting_client: TestClient
    ) -> None:
        """The first submit mints D-06's exact attribute set, and nothing else."""
        response = accepting_client.post(
            "/api/scan", data={"profile": "duplex", "title": "First Scan"}
        )

        header = _owner_set_cookie(response)
        assert header is not None
        attributes = header.lower()
        assert "httponly" in attributes
        assert "samesite=lax" in attributes
        assert "path=/" in attributes
        # A year-long lifetime, so a browser still sees its own scans after it
        # restarts.  Secure is deliberately absent: the appliance is served
        # over plain HTTP on a LAN, and Secure would silently disable the
        # cookie rather than harden it.
        assert _OWNER_COOKIE_MAX_AGE_ATTRIBUTE in attributes
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

    def test_owner_cookie_is_minted_once_and_re_set_on_every_submit(
        self, accepting_client: TestClient
    ) -> None:
        """
        A second submit re-sets the same token with the year-long lifetime.

        The mint rule still holds -- one value per browser (D-23) -- but every
        accepted submit sends it back, so the lifetime is renewed each time.
        """
        first = accepting_client.post(
            "/api/scan", data={"profile": "duplex", "title": "One"}
        )
        second = accepting_client.post(
            "/api/scan", data={"profile": "duplex", "title": "Two"}
        )

        first_header = _owner_set_cookie(first)
        second_header = _owner_set_cookie(second)
        assert first_header is not None
        assert second_header is not None
        assert _owner_cookie_value(second_header) == _owner_cookie_value(first_header)
        assert _OWNER_COOKIE_MAX_AGE_ATTRIBUTE in second_header.lower()

    def test_owner_cookie_from_an_older_release_gains_the_lifetime(
        self, accepting_client: TestClient
    ) -> None:
        """
        A token the browser already holds comes back unchanged, now with Max-Age.

        A browser upgraded from a release that set a session cookie presents
        that token on its next submit; it keeps its ownership and the cookie
        becomes persistent.
        """
        held = "a-token-set-by-an-older-release"
        accepting_client.cookies.set(_OWNER_COOKIE, held)

        response = accepting_client.post(
            "/api/scan", data={"profile": "duplex", "title": "Upgraded"}
        )

        header = _owner_set_cookie(response)
        assert header is not None
        assert _owner_cookie_value(header) == held
        assert _OWNER_COOKIE_MAX_AGE_ATTRIBUTE in header.lower()
        assert _newest_job(accepting_client).owner_token == held

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
        minted = _owner_cookie_value(header)
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


# Text a forced render failure carries, so a test can prove none of it reaches
# the browser.
_RENDER_FAILURE_MARKER = "render-failure-7d3a91"


def _fail_render(*_args: object, **_kwargs: object) -> NoReturn:
    """Stand in for a render step and raise, as a broken context or template would."""
    raise RuntimeError(_RENDER_FAILURE_MARKER)


def _lenient_browser(client: TestClient) -> TestClient:
    """
    Return a client over the same app that receives a server error as a response.

    The shared client re-raises an unhandled exception in the test, which would
    hide the response a browser is actually sent: the status, the cookie and
    the body are what the scan's owner sees, so those are what is asserted.

    Args:
        client: The client whose running app to share.

    Returns:
        A client holding no cookies, answering errors as responses.

    """
    return TestClient(_app(client), raise_server_exceptions=False)


def _job_count(client: TestClient) -> int:
    """
    Return how many job rows the app's store holds.

    Args:
        client: The client whose app owns the job store.

    Returns:
        The number of rows written so far.

    """
    job_store: JobStore = _app(client).state.job_store
    return len(job_store.list_recent(limit=10_000))


class TestPostSubmitRender:
    """
    Once the worker has accepted a job, the response reports that job as queued.

    The submit is the commit point: from there the scan will run whatever the
    browser is told.  A failure while the response is rendered must therefore
    not read as a failed submit, and must not lose the cookie that lets this
    browser answer the job's flip prompt.
    """

    def _assert_still_queued(
        self, browser: TestClient, response: httpx2.Response
    ) -> None:
        """
        Assert the response reports the accepted job as queued, with its cookie.

        Args:
            browser: The client that submitted the scan.
            response: The response to the submit.

        """
        assert response.status_code == 200
        header = _owner_set_cookie(response)
        assert header is not None
        attributes = header.lower()
        assert "httponly" in attributes
        assert "samesite=lax" in attributes
        assert "path=/" in attributes
        assert _OWNER_COOKIE_MAX_AGE_ATTRIBUTE in attributes
        assert "secure" not in attributes

        job = _newest_job(browser)
        assert job.owner_token == _owner_cookie_value(header)
        assert job.state is JobState.PENDING
        assert job.error_category is None

        body = response.text
        assert 'id="status-area" tabindex="-1"' in body
        assert _polls(body, job.id)
        # The first poll carries the empty token, so it is always answered
        # with the full status, the Scan button included.
        assert f'hx-get="/api/jobs/{job.id}/status?seen="' in body
        line = html.escape(progress_label(JobState.PENDING))
        assert f'<p class="busy-line">{line}</p>' in body
        assert 'hx-trigger="every 1s"' in body
        assert 'hx-swap="outerHTML"' in body
        assert html.escape(progress_label(JobState.PENDING)) in body
        assert '<div id="status-message" hx-swap-oob="innerHTML"></div>' in body
        assert "try again" not in body.lower()
        assert _RENDER_FAILURE_MARKER not in body
        assert "RuntimeError" not in body

    def test_post_submit_render_survives_a_failed_status_context(
        self, accepting_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A status context that raises after the submit still reports the job."""
        browser = _lenient_browser(accepting_client)
        monkeypatch.setattr(routes_module, "_status_context", _fail_render)

        response = browser.post(
            "/api/scan", data={"profile": "duplex", "title": "Context Fails"}
        )

        self._assert_still_queued(browser, response)

    def test_post_submit_render_survives_a_failed_template(
        self, accepting_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A template that raises while rendering still reports the job."""
        browser = _lenient_browser(accepting_client)
        templates = _app(accepting_client).state.templates
        render = templates.TemplateResponse

        def _fail_the_scan_response(
            request: object, name: str, *args: object, **kwargs: object
        ) -> object:
            if name == "partials/status_response.html":
                _fail_render()
            return render(request, name, *args, **kwargs)

        monkeypatch.setattr(templates, "TemplateResponse", _fail_the_scan_response)

        response = browser.post(
            "/api/scan", data={"profile": "duplex", "title": "Template Fails"}
        )

        self._assert_still_queued(browser, response)

    def test_post_submit_render_context_failure_before_the_submit_writes_no_job(
        self, accepting_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The status-strip context is built before the row, so its failure writes none."""
        browser = _lenient_browser(accepting_client)
        submitted: list[object] = []

        def _record_submit(job: object, _options: object) -> SubmitResult:
            submitted.append(job)
            return SubmitResult.ACCEPTED

        monkeypatch.setattr(
            _app(accepting_client).state.worker, "submit", _record_submit
        )
        monkeypatch.setattr(routes_module, "_checks_context", _fail_render)
        before = _job_count(accepting_client)

        response = browser.post(
            "/api/scan", data={"profile": "duplex", "title": "Checks Fail"}
        )

        assert response.status_code == 500
        assert _job_count(accepting_client) == before
        assert submitted == []

    def test_post_submit_render_normal_path_is_the_full_status_response(
        self, accepting_client: TestClient
    ) -> None:
        """Without a failure the submit answers today's whole status response."""
        response = accepting_client.post(
            "/api/scan", data={"profile": "duplex", "title": "Normal Path"}
        )

        assert response.status_code == 200
        assert _owner_set_cookie(response) is not None
        job = _newest_job(accepting_client)
        body = response.text
        assert _polls(body, job.id)
        assert '<div id="status-message" hx-swap-oob="innerHTML"></div>' in body
        # The out-of-band Scan button and status strip ride along only in the
        # full response, which is how it differs from the fallback.
        assert 'id="scan-btn"' in body
        assert 'id="checks-body"' in body


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


def _running_job(
    client: TestClient, title: str, *, owner_token: str | None = None
) -> str:
    """
    Create a SCANNING job and make it the worker's current job.

    Args:
        client: The client whose app owns the store and worker.
        title: The document title to give it.
        owner_token: The token to record, or None for a row nobody owns.

    Returns:
        The job's id.

    """
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title=title, owner_token=owner_token)
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


def _polls(markup: str, job_id: str) -> bool:
    """
    Report whether the markup's status area polls ``job_id``'s status URL.

    A prefix match: the URL may carry a query string after the path, so the
    path must be followed by either that query or the attribute's closing
    quote, which is what stops one id matching as the prefix of another.

    Args:
        markup: A rendered page or status response.
        job_id: The job id in the path, or ``current`` for the current-job URL.

    Returns:
        True when an ``hx-get`` names that job's status URL.

    """
    pattern = rf'hx-get="/api/jobs/{re.escape(job_id)}/status[?"]'
    return re.search(pattern, markup) is not None


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
        owner = _as_owner(client)
        _running_job(client, "Someone Elses Scan", owner_token=owner)
        job_store: JobStore = _app(client).state.job_store
        mine = job_store.create_job(
            profile="default", title="My Scan", owner_token=owner
        )
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

        assert _polls(response.text, job_id)

    def test_followed_job_unknown_id_polls_the_current_url(
        self, client: TestClient
    ) -> None:
        """The fallback rendering polls the current-job URL, as it always did."""
        _running_job(client, "Still Running")

        response = client.get("/api/jobs/no-such-job-at-all/status")

        assert _polls(response.text, "current")

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
        assert _polls(response.text, newest)

    def test_followed_job_current_status_route_is_unchanged(
        self, client: TestClient
    ) -> None:
        """A browser that submitted nothing keeps today's route and inference."""
        _running_job(client, "Inferred")

        response = client.get("/api/jobs/current/status")

        assert response.status_code == 200
        assert _polls(response.text, "current")
        assert "/api/jobs/current/status" in response.text

    def test_continue_follows_the_posted_job(
        self,
        accepting_client: TestClient,
        owned_flip: tuple[str, WorkerFlipCoordinator],
    ) -> None:
        """
        The Continue response keeps polling the job it answered.

        Otherwise the answer hands the area to the current-job URL, and the next
        poll reports whatever the worker runs, not the scan this person flipped.
        """
        job_id, _ = owned_flip

        response = accepting_client.post("/api/flip/continue", data={"job_id": job_id})

        assert response.status_code == 200
        assert _polls(response.text, job_id)

    def test_abort_follows_the_posted_job(
        self,
        accepting_client: TestClient,
        owned_flip: tuple[str, WorkerFlipCoordinator],
    ) -> None:
        """The Abort response keeps polling the job it answered."""
        job_id, _ = owned_flip

        response = accepting_client.post("/api/flip/abort", data={"job_id": job_id})

        assert response.status_code == 200
        assert _polls(response.text, job_id)

    def test_multi_page_answer_follows_the_posted_job(self, client: TestClient) -> None:
        """A multi-page answer's response keeps polling the job it answered."""
        job_store: JobStore = _app(client).state.job_store
        worker = _app(client).state.worker
        job = job_store.create_job(
            profile="default", title="Pages", owner_token=_as_owner(client)
        )
        job_store.update_state(job.id, JobState.AWAITING_NEXT_PASS)
        worker._current_job_id = job.id
        try:
            response = client.post(
                "/api/multi-page/answer",
                data={"job_id": job.id, "prompt": "1", "answer": str(PassAnswer.NEXT)},
            )
        finally:
            worker._current_job_id = None

        assert response.status_code == 200
        assert _polls(response.text, job.id)

    def test_index_follows_the_owned_queued_job(self, client: TestClient) -> None:
        """
        A reload while someone else's scan runs follows this browser's queued job.

        The page used to poll the current-job URL, so a queued submitter who
        reloaded was shown the running job instead of their own.
        """
        owner = _as_owner(client)
        _running_job(client, "Someone Elses Scan", owner_token="another-browser")
        job_store: JobStore = _app(client).state.job_store
        mine = job_store.create_job(profile="default", title="Mine", owner_token=owner)

        page = client.get("/").text

        assert _polls(page, mine.id)

    def test_index_follows_the_newest_owned_active_job(
        self, client: TestClient
    ) -> None:
        """Of two active jobs this browser owns, the page follows the newer one."""
        owner = _as_owner(client)
        _running_job(client, "Older And Running", owner_token=owner)
        job_store: JobStore = _app(client).state.job_store
        newer = job_store.create_job(
            profile="default", title="Newer And Queued", owner_token=owner
        )

        page = client.get("/").text

        assert _polls(page, newer.id)

    def test_index_never_follows_an_unowned_job(self, client: TestClient) -> None:
        """A queued job nobody owns, or someone else owns, is never followed."""
        _as_owner(client)
        job_store: JobStore = _app(client).state.job_store
        nobodys = job_store.create_job(profile="default", title="Nobody's")
        theirs = job_store.create_job(
            profile="default", title="Theirs", owner_token="another-browser"
        )

        page = client.get("/").text

        assert _polls(page, "current")
        assert not _polls(page, nobodys.id)
        assert not _polls(page, theirs.id)

    def test_index_without_an_owned_job_polls_current(self, client: TestClient) -> None:
        """With nothing of its own active, the page still follows the current job."""
        _running_job(client, "Inferred")

        page = client.get("/").text

        assert _polls(page, "current")


_STATUS_POLL_URL = re.compile(r'hx-get="(?P<url>/api/jobs/[^"/]+/status[^"]*)"')
_TOKEN = re.compile(r"[0-9a-f]{24}")
# A tiny JPEG's worth of base64: the thumbnail is rendered as a data URL, and
# only whether it is present matters here.
_THUMBNAIL = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP"


def _poll_url_in(markup: str) -> str:
    """
    Return the URL the markup's status area polls, as the browser would request it.

    Args:
        markup: A rendered page or status response.

    Returns:
        The ``hx-get`` of the status area, with its HTML entities resolved.

    """
    match = _STATUS_POLL_URL.search(markup)
    assert match is not None, markup
    return html.unescape(match.group("url"))


def _seen_in(markup: str) -> str:
    """
    Return the token the markup's status poll carries.

    Args:
        markup: A rendered page or status response.

    Returns:
        The ``seen`` query value of the status area's poll URL.

    """
    query = parse_qs(urlsplit(_poll_url_in(markup)).query)
    assert "seen" in query, markup
    return query["seen"][0]


class TestStatusPollAnswersWhenChanged:
    """
    A status poll answers 204 when nothing its viewer would see has changed.

    A poll that re-rendered every second would replace the focused button under
    the operator's finger, and a live region over it would speak every second.
    Every status rendering bakes a token of what it shows into its poll URL;
    a poll presenting the current token is answered with no content, which
    htmx leaves unswapped.
    """

    def test_unchanged_poll_answers_204(self, client: TestClient) -> None:
        """The same state polled again with its own token is answered empty."""
        job_id = _running_job(client, "Unchanged")

        first = client.get(f"/api/jobs/{job_id}/status")
        seen = _seen_in(first.text)
        second = client.get(f"/api/jobs/{job_id}/status", params={"seen": seen})

        assert first.status_code == 200
        assert _TOKEN.fullmatch(seen)
        assert second.status_code == 204
        assert second.content == b""

    def test_state_change_answers_200(self, client: TestClient) -> None:
        """A state the viewer has not seen yet is rendered in full."""
        job_id = _running_job(client, "Changing")
        seen = _seen_in(client.get(f"/api/jobs/{job_id}/status").text)
        job_store: JobStore = _app(client).state.job_store
        job_store.update_state(job_id, JobState.UPLOADING)

        response = client.get(f"/api/jobs/{job_id}/status", params={"seen": seen})

        assert response.status_code == 200
        assert _seen_in(response.text) != seen

    def test_thumbnail_arrival_answers_200(self, client: TestClient) -> None:
        """A preview stored mid-scan is news, though the busy line is unchanged."""
        job_id = _running_job(client, "Previewed", owner_token=_as_owner(client))
        seen = _seen_in(client.get(f"/api/jobs/{job_id}/status").text)
        job_store: JobStore = _app(client).state.job_store
        job_store.update_thumbnail(job_id, _THUMBNAIL)

        response = client.get(f"/api/jobs/{job_id}/status", params={"seen": seen})

        assert response.status_code == 200
        assert _THUMBNAIL in response.text

    def test_seen_from_another_viewer_answers_200(self, client: TestClient) -> None:
        """
        The owner's token proves nothing about what another browser was shown.

        The owner sees the preview; anyone else sees none, so the same job
        renders differently and the stranger's poll is answered.  (Without a
        preview a scanning job reads the same to everyone, and a 204 would be
        right.)
        """
        job_id = _running_job(client, "Owners Scan", owner_token=_as_owner(client))
        job_store: JobStore = _app(client).state.job_store
        job_store.update_thumbnail(job_id, _THUMBNAIL)
        owners = client.get(f"/api/jobs/{job_id}/status").text
        seen = _seen_in(owners)

        stranger = _other_browser(client).get(
            f"/api/jobs/{job_id}/status", params={"seen": seen}
        )

        assert _THUMBNAIL in owners
        assert stranger.status_code == 200
        assert _THUMBNAIL not in stranger.text

    @pytest.mark.parametrize("seen", ["f" * 10_000, "not a token at all"])
    def test_oversized_seen_answers_200(self, client: TestClient, seen: str) -> None:
        """A token too long or malformed is ignored, never a 422."""
        job_id = _running_job(client, "Odd Token")

        response = client.get(f"/api/jobs/{job_id}/status", params={"seen": seen})

        assert response.status_code == 200
        assert seen not in response.text

    def test_first_poll_after_the_page_is_unchanged(self, client: TestClient) -> None:
        """The page's own token is the first poll's, so nothing is swapped."""
        job_id = _running_job(client, "Reloaded", owner_token=_as_owner(client))

        url = _poll_url_in(client.get("/").text)
        response = client.get(url)

        assert _polls(f'hx-get="{url}"', job_id)
        assert response.status_code == 204

    def test_first_poll_after_scan_is_unchanged(
        self, accepting_client: TestClient
    ) -> None:
        """
        The scan submit's token is the poll's, although the submit carries more.

        The submit also clears the message slot and refreshes the strip; the
        token is computed from the rendering a poll would produce, so those
        extras cannot make the first poll swap.
        """
        submitted = accepting_client.post(
            "/api/scan", data={"profile": "duplex", "title": "Submitted"}
        )

        response = accepting_client.get(_poll_url_in(submitted.text))

        assert submitted.status_code == 200
        assert response.status_code == 204

    def test_first_poll_after_continue_is_unchanged(
        self,
        accepting_client: TestClient,
        owned_flip: tuple[str, WorkerFlipCoordinator],
    ) -> None:
        """While the worker still holds the answer, the first poll changes nothing."""
        job_id, _ = owned_flip

        answered = accepting_client.post("/api/flip/continue", data={"job_id": job_id})
        response = accepting_client.get(_poll_url_in(answered.text))

        assert answered.status_code == 200
        assert response.status_code == 204

    def test_current_route_answers_204_when_unchanged(self, client: TestClient) -> None:
        """The current-job URL follows the same rule."""
        _running_job(client, "Current")

        url = _poll_url_in(client.get("/api/jobs/current/status").text)
        response = client.get(url)

        assert url.startswith("/api/jobs/current/status?")
        assert response.status_code == 204


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
        """One job ahead reads as APPL-08 writes it, to the running job's owner."""
        _running_job(client, "Tax return", owner_token=_as_owner(client))
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
        _running_job(client, "Tax return", owner_token=_as_owner(client))
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
        _running_job(client, "<script>alert(1)</script>", owner_token=_as_owner(client))
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
    """
    Replace the worker's profile set, under its own lock.

    The worker can refuse a set, so the answer is asserted: a refused set must
    fail the test that built it, never leave the test passing against
    whatever profiles happened to be there before.
    """
    assert _app(client).state.worker._set_profiles(profiles)


class TestProfileDescriptionRoute:
    """GET /api/profiles/description -- the sentence under the select (APPL-05)."""

    def test_a_known_profile_returns_its_description_text_alone(
        self, client: TestClient
    ) -> None:
        """The body is the sentence with no wrapper element around it."""
        _configure_profiles(
            client,
            {
                "glass": ProfileConfig(description="Scans one page from the glass."),
                "default": ProfileConfig(),
            },
        )

        response = client.get("/api/profiles/description", params={"profile": "glass"})

        assert response.status_code == 200
        assert response.text == "Scans one page from the glass."

    def test_an_unknown_profile_name_gets_a_422_from_the_description_route(
        self, client: TestClient
    ) -> None:
        """The name is validated against the profile set, never used as a path."""
        _configure_profiles(
            client, {"glass": ProfileConfig(), "default": ProfileConfig()}
        )

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
        _configure_profiles(
            client, {"bare": ProfileConfig(), "default": ProfileConfig()}
        )

        response = client.get("/api/profiles/description", params={"profile": "bare"})

        assert response.status_code == 200
        assert response.text == ""

    def test_a_description_containing_markup_is_escaped_not_rendered(
        self, client: TestClient
    ) -> None:
        """Config free text is autoescaped and never marked safe (T-30-70)."""
        _configure_profiles(
            client,
            {
                "evil": ProfileConfig(description="<script>alert(1)</script>"),
                "default": ProfileConfig(),
            },
        )

        response = client.get("/api/profiles/description", params={"profile": "evil"})

        assert "<script>" not in response.text
        assert "&lt;script&gt;" in response.text

    def test_the_description_route_is_a_plain_def(self, client: TestClient) -> None:
        """Every handler runs on the threadpool, this one included (ROBU-05)."""
        routes = [
            route
            for route in leaf_routes(_app(client))
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
                ),
                "default": ProfileConfig(source="ADF"),
            },
        )

        options = _profile_options(_app(client).state.worker).options

        assert options == (
            _ProfileOption(
                name="adf",
                label="Feeder, single-sided",
                description="Feeds a stack of sheets.",
            ),
            _ProfileOption(name="default", label="default", description=""),
        )

    def test_a_blank_label_falls_back_to_the_profile_name(
        self, client: TestClient
    ) -> None:
        """A config written before this phase never shows a blank option (A-3)."""
        _configure_profiles(
            client,
            {
                "adf-duplex": ProfileConfig(source="ADF Duplex"),
                "default": ProfileConfig(source="ADF Duplex"),
            },
        )

        option, _ = _profile_options(_app(client).state.worker).options

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
                "default": ProfileConfig(source="Auto"),
            },
        )

        options = _profile_options(_app(client).state.worker).options

        assert [option.name for option in options] == ["stack", "pick", "default"]

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
                "default": ProfileConfig(source="Whatever"),
            },
        )

        options = _profile_options(_app(client).state.worker).options

        assert [option.name for option in options] == ["feeder", "mystery", "default"]

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
                "default": ProfileConfig(source="Auto"),
            },
        )

        options = _profile_options(_app(client).state.worker).options

        assert [option.name for option in options] == [
            "stack-1",
            "stack-2",
            "pick-1",
            "pick-2",
            "default",
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
                "default": ProfileConfig(),
            },
        )

        options = _profile_options(_app(client).state.worker).options

        assert [option.name for option in options] == ["glass", "stack", "default"]

    def test_a_profile_that_disappears_mid_build_is_dropped_from_the_ordering(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A profile rewritten between the two locked calls is skipped, not None."""
        worker = _app(client).state.worker
        _configure_profiles(
            client,
            {
                "stays": ProfileConfig(),
                "goes": ProfileConfig(),
                "default": ProfileConfig(),
            },
        )
        real_lookup = worker.get_profile
        monkeypatch.setattr(
            worker,
            "get_profile",
            lambda name: None if name == "goes" else real_lookup(name),
        )

        options = _profile_options(worker).options

        assert [option.name for option in options] == ["stays", "default"]

    def test_the_rendered_option_ordering_matches_the_rule(
        self, client: TestClient
    ) -> None:
        """The page consumes the ordered list, not the bare name list."""
        _configure_profiles(
            client,
            {
                "pick": ProfileConfig(source="Auto"),
                "stack": ProfileConfig(source="ADF"),
                "default": ProfileConfig(source="Auto"),
            },
        )

        response = client.get("/")

        rendered = re.findall(r'<option value="([^"]+)"', response.text)
        assert rendered[:2] == ["stack", "pick"]


def _generated(*sources: str) -> dict[str, ProfileConfig]:
    """
    Return the profile set ``generate_profiles`` builds for a device's sources.

    Built by the generator itself rather than by hand, so the ``default`` these
    tests hide is the one a real installation carries, twin and all.
    """
    return generate_profiles(
        DeviceCapabilities(sources=list(sources), resolutions=[300], modes=["Color"])
    )


def _rendered_options(page: str) -> dict[str, str]:
    """Map each rendered profile option's value to its text."""
    select = page.split('id="profile-select"', 1)[1].split("</select>", 1)[0]
    return dict(re.findall(r'<option value="([^"]+)"[^>]*>([^<]*)</option>', select))


class TestProfileChoices:
    """
    Each distinct profile is offered once, with its own text.

    A generated ``default`` that scans exactly as another profile does would
    be a second option doing the same thing under the same words, so the
    first profile it equals stands in for it.  Only a generated twin is
    hidden, and any label two rendered options still share gets the profile
    name, so a household member never meets two options reading the same.
    """

    def test_a_generated_default_twin_is_hidden_behind_its_stand_in(
        self, client: TestClient
    ) -> None:
        """The first profile equal to the generated default opens the page."""
        profiles = _generated("Flatbed", "ADF")
        assert profiles["default"] == profiles["flatbed"]
        _configure_profiles(client, profiles)

        choices = _profile_options(_app(client).state.worker)

        assert [option.name for option in choices.options] == ["flatbed", "adf"]
        assert choices.opening == "flatbed"

    def test_the_page_offers_no_hidden_twin_and_selects_its_stand_in(
        self, client: TestClient
    ) -> None:
        """The rendered select has no ``default`` option and opens on the glass."""
        _configure_profiles(client, _generated("Flatbed", "ADF"))

        page = client.get("/").text

        assert 'value="default"' not in page.split("</select>", 1)[0]
        assert '<option value="flatbed" selected>' in page
        assert _rendered_options(page) == {
            "flatbed": "Glass (flatbed)",
            "adf": "Feeder, single-sided",
        }

    def test_an_edited_default_is_shown_opens_the_page_and_reads_distinctly(
        self, client: TestClient
    ) -> None:
        """Default tags of its own make the default more than a twin."""
        profiles = _generated("Flatbed", "ADF")
        profiles["default"] = profiles["default"].model_copy(
            update={"default_tags": [1]}
        )
        _configure_profiles(client, profiles)

        choices = _profile_options(_app(client).state.worker)

        assert [option.name for option in choices.options] == [
            "flatbed",
            "adf",
            "default",
        ]
        assert choices.opening == "default"
        labels = {option.name: option.label for option in choices.options}
        assert labels == {
            "flatbed": "Glass (flatbed) (flatbed)",
            "adf": "Feeder, single-sided",
            "default": "Glass (flatbed) (default)",
        }

    def test_hand_written_duplicate_labels_each_carry_their_profile_name(
        self, client: TestClient
    ) -> None:
        """Every member of a shared label is suffixed; a unique label is not."""
        _configure_profiles(
            client,
            {
                "a": ProfileConfig(label="Scan"),
                "b": ProfileConfig(label="Scan"),
                "default": ProfileConfig(label="Other"),
            },
        )

        choices = _profile_options(_app(client).state.worker)
        page = client.get("/").text

        assert [option.label for option in choices.options] == [
            "Scan (a)",
            "Scan (b)",
            "Other",
        ]
        assert choices.opening == "default"
        assert _rendered_options(page) == {
            "a": "Scan (a)",
            "b": "Scan (b)",
            "default": "Other",
        }
        assert '<option value="default" selected>Other</option>' in page

    def test_a_hand_written_default_equal_to_another_profile_is_shown(
        self, client: TestClient
    ) -> None:
        """Only a generated twin is hidden; an operator's own default stays."""
        _configure_profiles(
            client,
            {
                "glass": ProfileConfig(label="Glass"),
                "default": ProfileConfig(label="Glass"),
            },
        )

        choices = _profile_options(_app(client).state.worker)

        assert [option.name for option in choices.options] == ["glass", "default"]
        assert [option.label for option in choices.options] == [
            "Glass (glass)",
            "Glass (default)",
        ]
        assert choices.opening == "default"

    def test_a_sheet_fed_twin_is_hidden_behind_its_feeder_stand_in(
        self, client: TestClient
    ) -> None:
        """The feeder-first ordering is untouched by hiding the twin."""
        profiles = _generated("ADF", "ADF Duplex")
        assert profiles["default"] == profiles["adf"]
        _configure_profiles(client, profiles)

        choices = _profile_options(_app(client).state.worker)

        assert [option.name for option in choices.options] == ["adf", "adf-duplex"]
        assert choices.opening == "adf"

    def test_no_profiles_gives_no_options_and_no_opening(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing to list is an empty select, and the page still renders."""
        worker = _app(client).state.worker
        monkeypatch.setattr(worker, "profile_names", list)

        choices = _profile_options(worker)
        response = client.get("/")

        assert choices.options == ()
        assert choices.opening == ""
        assert response.status_code == 200
        assert _rendered_options(response.text) == {}


class TestPageOpensOnDefault:
    """
    The page opens on the profile ``saneless scan`` uses with no ``--profile``.

    That is ``default``, wherever it sits in the list, and everything the page
    shows for the opening profile comes from it rather than from whichever
    option happens to be listed first.
    """

    @pytest.fixture
    def default_last(self, client: TestClient) -> TestClient:
        """Configure a first-listed profile and a ``default`` with defaults."""
        _configure_profiles(
            client,
            {
                "receipts": ProfileConfig(
                    description="Scans receipts.", default_tags=[2]
                ),
                "default": ProfileConfig(
                    description="Scans everyday post.",
                    default_tags=[1],
                    default_correspondent=1,
                ),
            },
        )
        return client

    def test_the_default_option_is_the_selected_one(
        self, default_last: TestClient
    ) -> None:
        """The select opens on ``default`` though it is listed last."""
        page = default_last.get("/").text

        assert '<option value="default" selected>' in page
        assert '<option value="receipts" selected>' not in page

    def test_the_description_beneath_the_select_is_the_defaults(
        self, default_last: TestClient
    ) -> None:
        """The sentence on first paint belongs to the option the page opens on."""
        page = default_last.get("/").text

        assert (
            '<small id="profile-description" aria-live="polite">'
            "Scans everyday post.</small>" in page
        )

    def test_the_defaults_tags_are_the_pre_ticked_ones(
        self, default_last: TestClient
    ) -> None:
        """The opening profile's tags are ticked, and the first option's are not."""
        page = default_last.get("/").text

        assert "checked" in _checkbox(page, 1)
        assert "checked" not in _checkbox(page, 2)

    def test_the_defaults_correspondent_is_pre_selected(
        self, default_last: TestClient
    ) -> None:
        """The opening profile's correspondent is chosen on first paint."""
        page = default_last.get("/").text

        assert "selected" in _option(page, 1)

    def test_the_multiple_pages_field_follows_the_defaults_manual_duplex(
        self, client: TestClient
    ) -> None:
        """A manual-duplex default listed last disables the box on first paint."""
        _configure_profiles(
            client,
            {
                "glass": ProfileConfig(),
                "default": ProfileConfig(source="ADF Front", duplex="manual"),
            },
        )

        page = client.get("/").text

        box = re.search(r'<input[^>]*\bid="multi-page"[^>]*>', page)
        assert box is not None, page
        assert re.search(r"\sdisabled(?=[\s>])", box.group(0)), box.group(0)

    def test_a_hidden_twin_default_still_scans_when_posted(
        self, client: TestClient
    ) -> None:
        """Hiding is the page's business; the name stays a valid submit."""
        _configure_profiles(client, _generated("Flatbed", "ADF"))

        response = client.post(
            "/api/scan", data={"profile": "default", "title": "Hidden Twin"}
        )

        assert response.status_code == 200
        job_store: JobStore = _app(client).state.job_store
        assert job_store.list_recent(limit=1)[0].profile == "default"


# The scan form element as it stood before plan 30-15, byte for byte.  The
# profile select lives inside it and must add nothing to it: the form already
# carries hx-disinherit="hx-disabled-elt", because an inherited hx-disabled-elt
# would put the form's own child requests in charge of the Scan button's
# disabled attribute (C-10).
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
                "default": ProfileConfig(),
            },
        )

        response = client.get("/")

        assert '<option value="glass">Glass (flatbed)</option>' in response.text
        assert '<option value="bare">bare</option>' in response.text
        assert '<option value="default" selected>default</option>' in response.text

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
                ),
                "default": ProfileConfig(description="Scans the everyday way."),
            },
        )

        response = client.get("/")

        assert (
            '<small id="profile-description" aria-live="polite">'
            "Scans the everyday way.</small>" in response.text
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
        """
        The description doubles as the control's help text (APPL-10).

        The Multiple pages field sits between the profile and the title, and
        its help line belongs to its own checkbox, so the count stops where
        that field starts.
        """
        response = client.get("/")

        under_profile = response.text.split('<label for="profile-select">', 1)[1].split(
            '<div id="multi-page-field"', 1
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
    """An httpx2 handler that records every request and answers with no tags."""

    def __init__(self) -> None:
        """Start with an empty record."""
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """
        Record the request and answer with an empty paperless-ngx page.

        Args:
            request: The request the client issued.

        Returns:
            A 200 carrying an empty collection page, so the client's
            pagination loop terminates on the first page.

        """
        self.requests.append(request)
        return httpx2.Response(200, json={"count": 0, "results": []})


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
        transport=httpx2.MockTransport(handler),
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


# A tag list paperless-ngx is known to hold, for the stale-default tests: 3 is
# in it, 99 never is, and 7 is the row nobody ticks.
_KNOWN_TAG_ROWS: list[dict[str, object]] = [
    {"id": 3, "name": "Recipes"},
    {"id": 7, "name": "taxes"},
]
_KNOWN_CORRESPONDENT_ROWS: list[dict[str, object]] = [
    {"id": 12, "name": "Acme Water"},
]
_STALE_NOTE = "no longer in paperless-ngx"


def _fail_fetch() -> NoReturn:
    """Stand in for a paperless-ngx that cannot be reached."""
    msg = "paperless unreachable"
    raise ConnectionError(msg)


def _serve_known_lists(client: TestClient) -> FastAPI:
    """
    Make the two known lists the ones the app sees, caches cleared.

    Args:
        client: The client whose app is being wired.

    Returns:
        The app, for tests that reach into its state.

    """
    app = _app(client)
    app.state.paperless.get_tags = lambda: list(_KNOWN_TAG_ROWS)
    app.state.paperless.get_correspondents = lambda: list(_KNOWN_CORRESPONDENT_ROWS)
    app.state.cache.invalidate("tags")
    app.state.cache.invalidate("correspondents")
    return app


def _serve_nothing(client: TestClient) -> FastAPI:
    """
    Make both lists unavailable: every fetch fails and nothing is cached.

    Args:
        client: The client whose app is being wired.

    Returns:
        The app, for tests that reach into its state.

    """
    app = _app(client)
    app.state.paperless.get_tags = _fail_fetch
    app.state.paperless.get_correspondents = _fail_fetch
    app.state.cache.invalidate("tags")
    app.state.cache.invalidate("correspondents")
    return app


def _tag_row(markup: str, tag_id: int) -> str:
    """
    Return one tag's whole label row, checkbox and text, or an empty string.

    Args:
        markup: The rendered tag list.
        tag_id: The paperless-ngx tag id to look for.

    Returns:
        The matching ``<label>`` element, or ``""``.

    """
    match = re.search(
        rf'<label class="tag-option"><input type="checkbox" name="tags" '
        rf'value="{tag_id}"[^>]*>[^<]*</label>',
        markup,
    )
    return match.group(0) if match else ""


def _option(markup: str, value: int) -> str:
    """
    Return one rendered ``<option>``, tag and text, or an empty string.

    Args:
        markup: The rendered options.
        value: The option value to look for.

    Returns:
        The matching ``<option>`` element, or ``""``.

    """
    match = re.search(rf'<option value="{value}"[^>]*>[^<]*</option>', markup)
    return match.group(0) if match else ""


def _render_correspondent_options(client: TestClient, selected: int | None) -> str:
    """
    Render the correspondent options partial from the route's own context.

    Args:
        client: The client whose app renders.
        selected: The correspondent id the control should show chosen.

    Returns:
        The rendered options.

    """
    app = _app(client)
    context = routes_module._correspondent_options_context(app.state, selected)
    template = app.state.templates.get_template("partials/correspondents.html")
    return template.render(context)


class TestStaleDefaultAndUnlistedRows:
    """
    A ticked id is never dropped from the form, and a stale one says why.

    The form's untouched submit has to carry every id it shows ticked, so an
    id the list does not name still renders as a ticked row.  Only a list
    that was actually read can prove the id gone, so the note appears then
    and only then; without the list the row is just the id.
    """

    def test_stale_default_tag_renders_checked_pinned_and_labelled(
        self, client: TestClient
    ) -> None:
        """99 is not in a list that was read: ticked, first, with the note."""
        _serve_known_lists(client)

        response = client.get("/api/tags", params={"tags": [3, 99]})

        assert response.status_code == 200
        assert "checked" in _checkbox(response.text, 3)
        assert "Recipes" in _tag_row(response.text, 3)
        stale = _tag_row(response.text, 99)
        assert "checked" in _checkbox(response.text, 99)
        assert "tag 99 (no longer in paperless-ngx; will be skipped)" in stale
        assert response.text.index('value="99"') < response.text.index('value="3"')
        assert "checked" not in _checkbox(response.text, 7)

    def test_stale_default_tag_stays_pinned_under_a_filter(
        self, client: TestClient
    ) -> None:
        """A filter that matches nothing still leaves the stale row ticked."""
        _serve_known_lists(client)

        response = client.get("/api/tags", params={"q": "zzz", "tags": [99]})

        assert "checked" in _checkbox(response.text, 99)
        assert "No tags match that filter." not in response.text

    def test_unlisted_tags_render_checked_by_number_without_a_note(
        self, client: TestClient
    ) -> None:
        """With no list, 3 and 99 are both just ids, and both stay ticked."""
        _serve_nothing(client)

        response = client.get("/api/tags", params={"tags": [3, 99]})

        assert response.status_code == 200
        assert "checked" in _checkbox(response.text, 3)
        assert "checked" in _checkbox(response.text, 99)
        assert "> tag 3</label>" in _tag_row(response.text, 3)
        assert "> tag 99</label>" in _tag_row(response.text, 99)
        assert _STALE_NOTE not in response.text
        assert "No tags" not in response.text

    def test_unlisted_nothing_ticked_keeps_today_s_empty_line(
        self, client: TestClient
    ) -> None:
        """With nothing ticked and no list, the empty line is what it was."""
        _serve_nothing(client)

        response = client.get("/api/tags")

        assert "No tags in paperless-ngx yet." in response.text
        assert 'type="checkbox"' not in response.text

    def test_every_tag_checkbox_opts_out_of_form_state_restore_stale_default(
        self, client: TestClient
    ) -> None:
        """Pinned, filtered, stale and unlisted rows all carry the attribute."""
        # Unreachable first: once a list has been read, the cache keeps it as
        # the last good copy and a failed fetch would render that instead.
        _serve_nothing(client)
        unlisted = client.get("/api/tags", params={"tags": [5]}).text
        _serve_known_lists(client)
        known = client.get("/api/tags", params={"q": "tax", "tags": [3, 99]}).text

        boxes = re.findall(r'<input type="checkbox"[^>]*>', known + unlisted)
        assert len(boxes) == 4
        for box in boxes:
            assert 'autocomplete="off"' in box, box

    def test_stale_default_template_marks_nothing_safe(self) -> None:
        """Names and notes are autoescaped text; the partials mark none safe."""
        for name in ("tags.html", "correspondents.html"):
            source = _web_asset("templates", "partials", name)
            assert "|safe" not in source.replace(" ", ""), name

    def test_stale_default_correspondent_known_selection_is_selected(
        self, client: TestClient
    ) -> None:
        """12 is in the list: its own option is the selected one."""
        _serve_known_lists(client)

        markup = _render_correspondent_options(client, 12)

        assert "selected" in _option(markup, 12)
        assert markup.count("selected") == 1
        assert _STALE_NOTE not in markup

    def test_stale_default_correspondent_is_a_selected_option_with_the_note(
        self, client: TestClient
    ) -> None:
        """99 is not in a list that was read: one extra option, selected."""
        _serve_known_lists(client)

        markup = _render_correspondent_options(client, 99)

        stale = _option(markup, 99)
        assert "selected" in stale
        assert "correspondent 99 (no longer in paperless-ngx; will be skipped)" in stale
        assert "selected" not in _option(markup, 12)
        assert markup.count('value="99"') == 1

    def test_unlisted_correspondent_is_selected_by_number(
        self, client: TestClient
    ) -> None:
        """Without the list, 99 is selected and called what it is."""
        _serve_nothing(client)

        markup = _render_correspondent_options(client, 99)

        assert "selected" in _option(markup, 99)
        assert ">correspondent 99</option>" in _option(markup, 99)
        assert _STALE_NOTE not in markup

    def test_unlisted_no_selection_selects_no_correspondent(
        self, client: TestClient
    ) -> None:
        """Nothing chosen adds no option and marks none selected."""
        _serve_known_lists(client)

        markup = _render_correspondent_options(client, None)

        assert "selected" not in markup
        assert markup.count("<option") == 2


# The profile defaults the D-29 regression tests drive, chosen so neither can
# be produced by accident: no fixture tag or correspondent uses these ids.
_PROFILE_DEFAULT_TAGS = [41, 42]
_PROFILE_DEFAULT_CORRESPONDENT = 43

# The two rules UI-SPEC S6 adds to app.css, property by property. Written out
# here rather than matched loosely, because "the tap target is 44 px" is the
# whole of D-30 and a rule that lost one declaration would still look right.
# The Multiple pages checkbox shares the tag rows' rule rather than a copy.
_TAG_OPTION_RULE = """label.tag-option,
label.multi-page-option {
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


@pytest.mark.usefixtures("offline_paperless")
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
        """
        What is left is the shortest form the appliance has.

        Multiple pages is not behind either key: it is on every form.
        """
        with TestClient(
            _simple_form_app(tmp_path, show_tags=False, show_correspondent=False)
        ) as client:
            page = client.get("/").text

        assert 'name="profile"' in page
        assert 'name="multi_page"' in page
        assert 'name="title"' in page
        assert 'id="scan-btn"' in page
        # Profile, Multiple pages and Title keep their help lines; the two that
        # went with the hidden controls are the only ones that leave.
        assert re.findall(r'<small id="([^"]+)"', page) == [
            "profile-description",
            "multi-page-help",
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
        assert css.count("label.tag-option,\nlabel.multi-page-option {") == 1

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


@pytest.mark.usefixtures("offline_paperless")
class TestProfileDefaultsFollowTheFormShape:
    """
    A shown control is answered by the submit; a hidden one takes the default.

    The form offers a profile's default tags and correspondent already chosen,
    so a submit from a shown control is the operator's whole answer: the
    defaults when nobody touched it, none when every box was unticked.  A
    control ``[web]`` hides was never answered, and the one metadata policy
    the command line also uses fills it with the profile's default.  An empty
    tag list and an absent correspondent arrive identically either way, so
    the config key that decided whether to render the control, never the
    submitted value, is what tells the two apart.
    """

    def test_the_scan_route_resolves_metadata_through_the_shared_policy(
        self,
    ) -> None:
        """The route asks the same function the command line asks, once."""
        source = inspect.getsource(routes_module.start_scan)
        calls = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "resolve_scan_metadata"
        ]

        assert len(calls) == 1

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


# The scenario the pre-ticking tests drive: the page opens on ``default``,
# listed last, whose defaults are all in paperless-ngx; ``plain``, listed
# first, has none, so a page that opened on the first option would tick
# nothing; ``other`` has different ones, and ``gone`` names a tag and a
# correspondent paperless-ngx no longer has.
_OPENING_TAGS = [3, 7]
_OPENING_CORRESPONDENT = 12
_OTHER_TAGS = [7]
_OTHER_CORRESPONDENT = 14
_PRE_TICK_CORRESPONDENT_ROWS: list[dict[str, object]] = [
    {"id": 12, "name": "Acme Water"},
    {"id": 14, "name": "Globex Power"},
]

# The five attributes that make a control its own profile-change swap target.
_PROFILE_CHANGE_ATTRIBUTES = (
    'hx-trigger="change from:#profile-select"',
    'hx-include="#profile-select"',
    'hx-target="this"',
    'hx-swap="outerHTML"',
)
_TAGS_WRAPPER = re.compile(r'<div id="tags-list"[^>]*>')
_CORRESPONDENT_SELECT_TAG = re.compile(
    r'<select name="correspondent" id="correspondent-select"[^>]*>'
)


def _pre_ticked_app(
    tmp_path: Path, *, show_tags: bool = True, show_correspondent: bool = True
) -> FastAPI:
    """
    Build an app whose opening profile carries default tags and a correspondent.

    Args:
        tmp_path: Where the app writes its database and files.
        show_tags: Whether the Tags fieldset is rendered at all.
        show_correspondent: Whether the Correspondent control is rendered.

    Returns:
        The app, whose lifespan starts with its TestClient.

    """
    settings = Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(
            url="http://localhost:8000", token="a-real-looking-token"
        ),
        output=OutputConfig(tmp_dir=str(tmp_path), data_dir=str(tmp_path)),
        web=WebConfig(show_tags=show_tags, show_correspondent=show_correspondent),
        profiles={
            "plain": ProfileConfig(),
            "other": ProfileConfig(
                default_tags=_OTHER_TAGS,
                default_correspondent=_OTHER_CORRESPONDENT,
            ),
            "gone": ProfileConfig(default_tags=[3, 99], default_correspondent=98),
            "default": ProfileConfig(
                default_tags=_OPENING_TAGS,
                default_correspondent=_OPENING_CORRESPONDENT,
            ),
        },
    )
    app = create_app(settings, StubScannerBackend())
    # Keyword-only ``timeout``, as the real client takes it: the page asks
    # without one, and a profile change and the check before a scan ask with
    # one.
    app.state.paperless.get_tags = _TimedList(_KNOWN_TAG_ROWS)
    app.state.paperless.get_correspondents = _TimedList(_PRE_TICK_CORRESPONDENT_ROWS)
    return app


class _TimedList:
    """A stand-in for ``get_tags`` or ``get_correspondents`` that notes timeouts."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        """
        Answer with ``rows`` and note nothing yet.

        Args:
            rows: The list every call answers with a copy of.

        """
        self._rows = rows
        self.timeouts: list[float | None] = []

    def __call__(self, *, timeout: float | None = None) -> list[dict[str, object]]:
        """
        Note the budget the caller asked for and answer a copy of the list.

        Args:
            timeout: The per-request budget, or None for the client default.

        Returns:
            A copy of the rows.

        """
        self.timeouts.append(timeout)
        return list(self._rows)


class TestProfileChangeFetchesAreShort:
    """
    A profile change never waits the client's 30 s on a cold cache.

    Until the swap lands the form still shows the previous profile's
    defaults, so both profile-change routes fetch with the short budget; the
    full page keeps the client default.
    """

    @pytest.mark.parametrize(
        ("route", "getter"),
        [
            ("/api/profiles/tags", "get_tags"),
            ("/api/profiles/correspondent", "get_correspondents"),
        ],
    )
    def test_a_profile_change_fetches_with_the_short_budget(
        self, tmp_path: Path, route: str, getter: str
    ) -> None:
        """The cold-cache fetch behind the swap carries the short timeout."""
        app = _pre_ticked_app(tmp_path)
        with TestClient(app) as client:
            app.state.cache.invalidate("tags")
            app.state.cache.invalidate("correspondents")
            fetch: _TimedList = getattr(app.state.paperless, getter)
            fetch.timeouts.clear()
            response = client.get(route, params={"profile": "other"})

        assert response.status_code == 200
        assert fetch.timeouts == [routes_module.PROFILE_CHANGE_FETCH_TIMEOUT_SECONDS]

    def test_the_full_page_keeps_the_client_default(self, tmp_path: Path) -> None:
        """Only the swap is shortened; the page load is unchanged."""
        app = _pre_ticked_app(tmp_path)
        with TestClient(app) as client:
            app.state.cache.invalidate("tags")
            app.state.cache.invalidate("correspondents")
            tags: _TimedList = app.state.paperless.get_tags
            correspondents: _TimedList = app.state.paperless.get_correspondents
            tags.timeouts.clear()
            correspondents.timeouts.clear()
            response = client.get("/")

        assert response.status_code == 200
        assert tags.timeouts == [None]
        assert correspondents.timeouts == [None]


class TestProfileDefaultsArePreTicked:
    """
    The form opens on the profile's defaults and follows every profile change.

    An untouched submit then carries exactly what ``saneless scan --profile``
    carries, and a cleared one carries none.  A profile change replaces the
    ticks rather than keeping them, because the new profile's defaults are
    what the untouched form now has to mean; a refresh keeps them, because a
    refresh changes the list and not the choice.
    """

    def test_profile_defaults_are_ticked_and_selected_on_first_paint(
        self, tmp_path: Path
    ) -> None:
        """The opening profile's tags are ticked and its correspondent chosen."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            page = client.get("/").text

        assert "checked" in _checkbox(page, 3)
        assert "checked" in _checkbox(page, 7)
        assert "selected" in _option(page, _OPENING_CORRESPONDENT)
        assert "selected" not in _option(page, _OTHER_CORRESPONDENT)

    def test_profile_defaults_controls_opt_out_of_form_state_restore(
        self, tmp_path: Path
    ) -> None:
        """A browser restoring form state must not override the defaults."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            page = client.get("/").text

        select = _CORRESPONDENT_SELECT_TAG.search(page)
        assert select is not None, page
        assert 'autocomplete="off"' in select.group(0)
        assert 'aria-describedby="correspondent-help"' in select.group(0)
        for box in re.findall(r'<input type="checkbox" name="tags"[^>]*>', page):
            assert 'autocomplete="off"' in box, box

    def test_profile_change_wrappers_are_on_the_first_paint(
        self, tmp_path: Path
    ) -> None:
        """Both controls follow the profile select from the page's first load."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            page = client.get("/").text

        wrapper = _TAGS_WRAPPER.search(page)
        select = _CORRESPONDENT_SELECT_TAG.search(page)
        assert wrapper is not None
        assert select is not None
        assert 'hx-get="/api/profiles/tags"' in wrapper.group(0)
        assert 'hx-get="/api/profiles/correspondent"' in select.group(0)
        for attribute in _PROFILE_CHANGE_ATTRIBUTES:
            assert attribute in wrapper.group(0), attribute
            assert attribute in select.group(0), attribute
        assert page.count('id="correspondent-select"') == 1

    def test_profile_change_tags_replace_the_earlier_ticks(
        self, tmp_path: Path
    ) -> None:
        """The new profile's defaults are ticked; what was ticked before is not."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            response = client.get(
                "/api/profiles/tags", params={"profile": "other", "tags": [3]}
            )

        assert response.status_code == 200
        assert "checked" in _checkbox(response.text, 7)
        assert "checked" not in _checkbox(response.text, 3)
        wrapper = _TAGS_WRAPPER.search(response.text)
        assert wrapper is not None, response.text
        assert 'hx-get="/api/profiles/tags"' in wrapper.group(0)
        for attribute in _PROFILE_CHANGE_ATTRIBUTES:
            assert attribute in wrapper.group(0), attribute

    def test_profile_change_to_a_profile_without_defaults_clears_every_tick(
        self, tmp_path: Path
    ) -> None:
        """A profile with no defaults means none, so the swap ticks nothing."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            tags = client.get(
                "/api/profiles/tags", params={"profile": "plain", "tags": [3]}
            )
            select = client.get(
                "/api/profiles/correspondent", params={"profile": "plain"}
            )

        assert tags.status_code == 200
        assert select.status_code == 200
        assert _checkbox(tags.text, 3)
        assert "checked" not in tags.text
        assert _option(select.text, _OPENING_CORRESPONDENT)
        assert "selected" not in select.text

    def test_profile_change_correspondent_renders_the_whole_select(
        self, tmp_path: Path
    ) -> None:
        """The select comes back whole, with the new default chosen."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            response = client.get(
                "/api/profiles/correspondent", params={"profile": "other"}
            )

        assert response.status_code == 200
        select = _CORRESPONDENT_SELECT_TAG.search(response.text)
        assert select is not None, response.text
        assert 'hx-get="/api/profiles/correspondent"' in select.group(0)
        assert 'autocomplete="off"' in select.group(0)
        for attribute in _PROFILE_CHANGE_ATTRIBUTES:
            assert attribute in select.group(0), attribute
        assert "selected" in _option(response.text, _OTHER_CORRESPONDENT)
        assert "selected" not in _option(response.text, _OPENING_CORRESPONDENT)
        assert response.text.rstrip().endswith("</select>")

    def test_profile_change_shows_a_stale_default_ticked_with_the_note(
        self, tmp_path: Path
    ) -> None:
        """A default paperless-ngx no longer has is ticked, and says so."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            tags = client.get("/api/profiles/tags", params={"profile": "gone"}).text
            select = client.get(
                "/api/profiles/correspondent", params={"profile": "gone"}
            ).text

        assert "checked" in _checkbox(tags, 99)
        assert "tag 99 (no longer in paperless-ngx; will be skipped)" in tags
        assert "checked" in _checkbox(tags, 3)
        assert "selected" in _option(select, 98)
        assert "correspondent 98 (no longer in paperless-ngx; will be skipped)" in (
            select
        )

    @pytest.mark.parametrize(
        "route", ["/api/profiles/tags", "/api/profiles/correspondent"]
    )
    def test_profile_change_to_an_unknown_profile_is_refused(
        self, tmp_path: Path, route: str
    ) -> None:
        """The same 422 the other profile routes give, before any fetch."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            response = client.get(route, params={"profile": "../../etc/passwd"})

        assert response.status_code == 422
        assert response.json() == {
            "status": "error",
            "detail": rejection_message(RequestRejection.UNKNOWN_PROFILE),
        }

    def test_profile_defaults_survive_a_correspondent_refresh(
        self, tmp_path: Path
    ) -> None:
        """The refresh carries the choice and renders it chosen again."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            page = client.get("/").text
            response = client.post(
                "/api/cache/invalidate?resource=correspondents",
                data={"correspondent": str(_OPENING_CORRESPONDENT)},
            )

        assert response.status_code == 200
        assert "selected" in _option(response.text, _OPENING_CORRESPONDENT)
        button = re.search(
            r'<button[^>]*hx-post="/api/cache/invalidate\?resource=correspondents"'
            r"[^>]*>",
            page,
        )
        assert button is not None, page
        assert 'hx-include="#correspondent-select"' in button.group(0)
        assert 'hx-target="#correspondent-select"' in button.group(0)
        assert 'hx-swap="innerHTML"' in button.group(0)

    def test_profile_defaults_refresh_refuses_a_malformed_correspondent(
        self, tmp_path: Path
    ) -> None:
        """The refresh's correspondent is bounded like the scan's."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            response = client.post(
                "/api/cache/invalidate?resource=correspondents",
                data={"correspondent": "0"},
            )

        assert response.status_code == 422

    def test_profile_defaults_refresh_with_no_correspondent_chosen(
        self, tmp_path: Path
    ) -> None:
        """The "No correspondent" option sends an empty value, which means none."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            response = client.post(
                "/api/cache/invalidate?resource=correspondents",
                data={"correspondent": ""},
            )

        assert response.status_code == 200
        assert _option(response.text, _OPENING_CORRESPONDENT)
        assert "selected" not in response.text

    def test_profile_defaults_route_answers_with_the_tag_control_hidden(
        self, tmp_path: Path
    ) -> None:
        """No list on the page, and the route still renders an empty one."""
        with TestClient(_pre_ticked_app(tmp_path, show_tags=False)) as client:
            page = client.get("/").text
            response = client.get("/api/profiles/tags", params={"profile": "other"})

        assert 'id="tags-list"' not in page
        assert response.status_code == 200
        assert 'id="tags-list"' in response.text
        assert 'type="checkbox"' not in response.text

    def test_profile_defaults_route_answers_with_the_correspondent_hidden(
        self, tmp_path: Path
    ) -> None:
        """No select on the page, and the route still renders one."""
        with TestClient(_pre_ticked_app(tmp_path, show_correspondent=False)) as client:
            page = client.get("/").text
            response = client.get(
                "/api/profiles/correspondent", params={"profile": "other"}
            )

        assert 'id="correspondent-select"' not in page
        assert response.status_code == 200
        assert "selected" in _option(response.text, _OTHER_CORRESPONDENT)


_PROFILE_SELECT_TAG = re.compile(r'<select name="profile" id="profile-select"[^>]*>')


def _marker(html: str, name: str) -> str:
    """
    Return the one hidden profile marker named ``name`` in ``html``.

    Args:
        html: The page or partial to search.
        name: The marker's form name.

    Returns:
        The marker's whole tag.

    """
    found = re.findall(rf'<input type="hidden"[^>]*name="{name}"[^>]*>', html)
    assert len(found) == 1, html
    return found[0]


@pytest.mark.usefixtures("offline_paperless")
class TestMetadataFollowsTheSubmittedProfile:
    """
    The metadata a scan files belongs to the profile it names.

    The tag list and the correspondent select each follow the Profile select
    by their own request, so a submit can name one profile while a control
    still shows another's defaults: a swap still in flight or failed, or a
    browser that restored the select on reload.  The Profile select opts out
    of form-state restore like the controls it drives, and each control
    carries a marker naming whose defaults it shows, so a control that is out
    of step is read as unanswered and the submitted profile's defaults apply.
    """

    def test_the_profile_select_opts_out_of_form_state_restore(
        self, tmp_path: Path
    ) -> None:
        """Restored alone, the select would name a profile nothing followed."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            page = client.get("/").text

        select = _PROFILE_SELECT_TAG.search(page)
        assert select is not None, page
        assert 'autocomplete="off"' in select.group(0)

    def test_the_page_marks_both_controls_with_the_opening_profile(
        self, tmp_path: Path
    ) -> None:
        """Each marker names the profile the page ticked the defaults of."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            page = client.get("/").text

        for name in ("tags_profile", "correspondent_profile"):
            marker = _marker(page, name)
            assert 'value="default"' in marker, marker
            assert 'autocomplete="off"' in marker, marker
            assert "hx-swap-oob" not in marker, marker

    def test_a_hidden_control_carries_no_marker(self, tmp_path: Path) -> None:
        """No control on the page, nothing to mark."""
        app = _pre_ticked_app(tmp_path, show_tags=False, show_correspondent=False)
        with TestClient(app) as client:
            page = client.get("/").text

        assert "tags_profile" not in page
        assert "correspondent_profile" not in page

    def test_the_marker_is_after_the_correspondent_help_line(
        self, tmp_path: Path
    ) -> None:
        """The select stays the element directly before its help line."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            page = client.get("/").text

        assert re.search(r"</select>\s*<small id=\"correspondent-help\">", page), page

    @pytest.mark.parametrize(
        ("route", "name"),
        [
            ("/api/profiles/tags", "tags_profile"),
            ("/api/profiles/correspondent", "correspondent_profile"),
        ],
    )
    def test_a_profile_change_moves_its_marker_out_of_band(
        self, tmp_path: Path, route: str, name: str
    ) -> None:
        """The marker changes in the same swap that shows the new defaults."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            response = client.get(route, params={"profile": "other"})

        assert response.status_code == 200
        marker = _marker(response.text, name)
        assert 'value="other"' in marker, marker
        assert 'hx-swap-oob="true"' in marker, marker

    def test_a_filter_or_refresh_leaves_the_marker_alone(self, tmp_path: Path) -> None:
        """They keep the ticks, so the profile they belong to has not changed."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            filtered = client.get("/api/tags", params={"q": "a", "tags": [3]})
            refreshed_tags = client.post("/api/cache/invalidate?resource=tags")
            refreshed_correspondents = client.post(
                "/api/cache/invalidate?resource=correspondents"
            )

        for response in (filtered, refreshed_tags, refreshed_correspondents):
            assert response.status_code == 200
            assert "_profile" not in response.text, response.text

    def test_ticks_marked_for_another_profile_give_way_to_its_defaults(
        self, tmp_path: Path
    ) -> None:
        """
        The regression: ``other`` submitted with ``default``'s metadata.

        This is what a restored Profile select or an unfinished swap sends,
        and before the marker it filed ``default``'s tags and correspondent
        under ``other`` with no warning.
        """
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            response = client.post(
                "/api/scan",
                data={
                    "profile": "other",
                    "title": "Out of step",
                    "tags": [str(tag) for tag in _OPENING_TAGS],
                    "correspondent": str(_OPENING_CORRESPONDENT),
                    "tags_profile": "default",
                    "correspondent_profile": "default",
                },
            )

            assert response.status_code == 200
            job = _newest_job(client)
            assert job.tags == _OTHER_TAGS
            assert job.correspondent == _OTHER_CORRESPONDENT

    def test_each_control_is_judged_by_its_own_marker(self, tmp_path: Path) -> None:
        """One swap landed and the other did not: only the stale one gives way."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            response = client.post(
                "/api/scan",
                data={
                    "profile": "other",
                    "title": "Half swapped",
                    "tags": [str(tag) for tag in _OPENING_TAGS],
                    "correspondent": str(_OPENING_CORRESPONDENT),
                    "tags_profile": "default",
                    "correspondent_profile": "other",
                },
            )

            assert response.status_code == 200
            job = _newest_job(client)
            assert job.tags == _OTHER_TAGS
            assert job.correspondent == _OPENING_CORRESPONDENT

    @pytest.mark.parametrize("marked", [True, False])
    def test_values_marked_for_this_profile_or_unmarked_are_taken_as_given(
        self, tmp_path: Path, *, marked: bool
    ) -> None:
        """The page's own answer, or a script's, is the operator's choice."""
        data: dict[str, str | list[str]] = {
            "profile": "other",
            "title": "In step",
            "tags": [str(tag) for tag in _OPENING_TAGS],
            "correspondent": str(_OPENING_CORRESPONDENT),
        }
        if marked:
            data |= {"tags_profile": "other", "correspondent_profile": "other"}
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            response = client.post("/api/scan", data=data)

            assert response.status_code == 200
            job = _newest_job(client)
            assert job.tags == _OPENING_TAGS
            assert job.correspondent == _OPENING_CORRESPONDENT


def _unreachable(*, timeout: float | None = None) -> NoReturn:
    """
    Stand in for a paperless-ngx that cannot be reached, whatever the timeout.

    Args:
        timeout: The budget the caller asked for, unused.

    Raises:
        ConnectionError: Always.

    """
    del timeout
    msg = "paperless unreachable"
    raise ConnectionError(msg)


class TestTheLastGoodCopyProvesNothing:
    """
    A list served from the last good copy cannot show an id is gone.

    While paperless-ngx cannot be reached the page still renders the list it
    last read, but the scan will send the ids unchecked, and a tag created
    since is missing from that copy without being gone.  So an id the copy
    lacks is labelled by number alone, never "will be skipped".
    """

    def _outage(self, client: TestClient) -> None:
        """Fill the cache while paperless-ngx answers, then take it away."""
        app = _app(client)
        assert client.get("/").status_code == 200
        app.state.paperless.get_tags = _unreachable
        app.state.paperless.get_correspondents = _unreachable
        app.state.cache.invalidate("tags")
        app.state.cache.invalidate("correspondents")

    def test_a_default_missing_from_the_copy_is_unlisted_not_stale(
        self, tmp_path: Path
    ) -> None:
        """Tag 99 and correspondent 98 are not claimed gone during an outage."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            self._outage(client)
            tags = client.get("/api/profiles/tags", params={"profile": "gone"}).text
            select = client.get(
                "/api/profiles/correspondent", params={"profile": "gone"}
            ).text

        assert "checked" in _checkbox(tags, 99)
        assert "no longer in paperless-ngx" not in tags
        assert re.search(r'value="99"[^>]*> tag 99</label>', tags), tags
        assert "selected" in _option(select, 98)
        assert "no longer in paperless-ngx" not in select
        # A ticked id the copy does list is rendered once, from the list.
        assert tags.count('value="3"') == 1
        assert "checked" in _checkbox(tags, 3)

    def test_the_second_render_in_the_outage_is_not_proof_either(
        self, tmp_path: Path
    ) -> None:
        """The re-armed copy stays unproven for its whole extra TTL."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            self._outage(client)
            client.get("/api/profiles/tags", params={"profile": "gone"})
            again = client.get("/api/profiles/tags", params={"profile": "gone"}).text

        assert "no longer in paperless-ngx" not in again
        assert "checked" in _checkbox(again, 99)

    def test_a_current_list_still_proves_a_default_gone(self, tmp_path: Path) -> None:
        """With paperless-ngx answering, the stale note is unchanged."""
        with TestClient(_pre_ticked_app(tmp_path)) as client:
            tags = client.get("/api/profiles/tags", params={"profile": "gone"}).text

        assert "tag 99 (no longer in paperless-ngx; will be skipped)" in tags


# The two collection endpoints a page load can reach, as paperless.py spells
# them.  Counting by path is what separates "no tag request" from "no request
# at all", which are different claims and fail differently.
_TAGS_ENDPOINT = "/api/tags/"
_CORRESPONDENTS_ENDPOINT = "/api/correspondents/"


class _MetadataRequestCounter:
    """Records every paperless-ngx request a request under test issues."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.paths: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """
        Record the request's path and answer with an empty collection.

        Args:
            request: The request the client issued.

        Returns:
            A 200 carrying a paginated response with no results, so a fetch
            that does happen succeeds and the count is the only difference.

        """
        self.paths.append(request.url.path)
        return httpx2.Response(200, json={"count": 0, "results": []})

    def count(self, path: str) -> int:
        """
        Say how many requests reached one endpoint.

        Args:
            path: The endpoint path to count.

        Returns:
            The number of recorded requests for that path.

        """
        return self.paths.count(path)


def _counted_app(
    tmp_path: Path,
    *,
    show_tags: bool = True,
    show_correspondent: bool = True,
) -> tuple[FastAPI, _MetadataRequestCounter]:
    """
    Build a form-shaped app whose Paperless traffic is counted, not stubbed.

    The stub client ``_simple_form_app`` installs answers without going near
    the transport, which is exactly what makes it useless for counting, so the
    client is replaced here by a real one over a mock transport.  The metadata
    cache is untouched and therefore cold: every fetch the route decides to
    make shows up.

    Args:
        tmp_path: Where the app writes its database and files.
        show_tags: Whether the Tags fieldset is rendered at all.
        show_correspondent: Whether the Correspondent control is rendered.

    Returns:
        The app and the counter its Paperless client reports to.

    """
    app = _simple_form_app(
        tmp_path, show_tags=show_tags, show_correspondent=show_correspondent
    )
    counter = _MetadataRequestCounter()
    credential = "a-real-looking-token"
    app.state.paperless = PaperlessClient(
        url="http://localhost:8000",
        token=credential,
        transport=httpx2.MockTransport(counter),
    )
    return app, counter


class TestHiddenControlsCostNoMetadataFetch:
    """
    IN-01: an appliance does not pay for data its markup leaves out.

    On a cold metadata cache a page load costs one paperless-ngx round trip per
    optional control, and the flags the template branches on are the same flags
    that decide whether that data can ever be seen.  With both controls off the
    page is the profile, the title and the Scan button, and it should reach
    paperless-ngx not at all.
    """

    def test_a_hidden_tag_control_costs_no_tag_request(self, tmp_path: Path) -> None:
        """A cold cache and no Tags fieldset means no ``/api/tags/`` fetch."""
        app, counter = _counted_app(tmp_path, show_tags=False)
        with TestClient(app) as client:
            assert client.get("/").status_code == 200

        assert counter.count(_TAGS_ENDPOINT) == 0

    def test_a_hidden_correspondent_control_costs_no_fetch(
        self, tmp_path: Path
    ) -> None:
        """The correspondent half of the same claim."""
        app, counter = _counted_app(tmp_path, show_correspondent=False)
        with TestClient(app) as client:
            assert client.get("/").status_code == 200

        assert counter.count(_CORRESPONDENTS_ENDPOINT) == 0

    def test_the_shortest_form_costs_no_paperless_request_at_all(
        self, tmp_path: Path
    ) -> None:
        """Both controls off: the page load reaches paperless-ngx never."""
        app, counter = _counted_app(tmp_path, show_tags=False, show_correspondent=False)
        with TestClient(app) as client:
            assert client.get("/").status_code == 200

        assert counter.paths == []

    def test_the_full_form_still_fetches_both(self, tmp_path: Path) -> None:
        """No fetch is lost: the default shape costs exactly what it did."""
        app, counter = _counted_app(tmp_path)
        with TestClient(app) as client:
            assert client.get("/").status_code == 200

        assert counter.count(_TAGS_ENDPOINT) == 1
        assert counter.count(_CORRESPONDENTS_ENDPOINT) == 1

    def test_a_hidden_tag_control_still_renders_a_whole_page(
        self, tmp_path: Path
    ) -> None:
        """
        The guarded context is complete enough to render, not just to type.

        ``index`` spreads the tag context into its own, so a key the guard
        forgot would be an ``UndefinedError`` or a silently empty branch on a
        page that has nothing to do with tags.  Rendering is the assertion.
        """
        app, _ = _counted_app(tmp_path, show_tags=False)
        with TestClient(app) as client:
            page = client.get("/").text

        assert 'id="tags-list"' not in page
        assert 'id="scan-btn"' in page
        assert 'id="correspondent-select"' in page

    def test_the_tag_route_answers_an_empty_list_when_the_control_is_off(
        self, tmp_path: Path
    ) -> None:
        """``/api/tags`` is reachable with the control off and costs nothing."""
        app, counter = _counted_app(tmp_path, show_tags=False)
        with TestClient(app) as client:
            response = client.get("/api/tags")

        assert response.status_code == 200
        assert 'name="tags"' not in response.text
        assert counter.count(_TAGS_ENDPOINT) == 0


# A Paperless URL configured behind a reverse proxy may carry Basic-auth
# userinfo, and httpx2 puts the URL it could not reach into the exception's
# string form.  Both halves are asserted absent from the log.
_CREDENTIALLED_URL = "https://user:pass@paperless.example/api/tags/"


class TestRouteLogsNameExceptionsOnly:
    """
    No handler in ``web/routes.py`` hands an exception object to a logger.

    Third-party exception text can quote a URL, a header or a token, so an
    exception interpolated with ``%s`` can put a credential in the log file.
    The routes log a client exception's message only when the client built it
    (``TestWebLogsNoClientSecret`` feeds marker text through every such path);
    the guard below stops a future handler passing ``exc`` itself, which a
    single behavioural test could not do.
    """

    def test_a_failed_connection_test_logs_only_the_class_name(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The userinfo and the host both stay out of the record (ASVS V7)."""

        def _raise_with_the_url() -> str:
            raise httpx2.ConnectError(_CREDENTIALLED_URL)

        app = _simple_form_app(tmp_path)
        app.state.paperless.test_connection = _raise_with_the_url
        with TestClient(app) as client, caplog.at_level(logging.WARNING):
            response = client.get("/api/paperless/test")

        assert response.status_code == 500
        assert response.json() == {"status": "error", "detail": "ConnectError"}
        for record in caplog.records:
            message = record.getMessage()
            assert "user:pass" not in message
            assert "paperless.example" not in message
        assert "ConnectError" in caplog.records[-1].getMessage()

    def test_no_logger_call_in_routes_takes_a_bare_exception(self) -> None:
        """
        Read the source: no ``logger`` call is handed the exception itself.

        Parsed rather than grepped, so a call spread over several lines is
        caught too -- the shape this forbids is easiest to reintroduce when
        the arguments have been wrapped.
        """
        source = Path(routes_module.__file__).read_text(encoding="utf-8")
        offenders = [
            node.lineno
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
            and any(
                isinstance(argument, ast.Name) and argument.id == "exc"
                for argument in [
                    *node.args,
                    *(keyword.value for keyword in node.keywords),
                ]
            )
        ]

        assert offenders == []


# Text no log record may carry: a stand-in for a configured token, and the
# userinfo of a URL that third-party exception text might quote.
_MARKER = "tok-MARKER-9b1e5c"
_MARKED_TEXT = f"refused: Token {_MARKER} user:pass@paperless.invalid"


def _marked_client(error: Exception, token: str = _MARKER) -> PaperlessClient:
    """
    Build a real Paperless client whose token is the marker and whose requests fail.

    Args:
        error: What every request raises, from inside the transport, so the
            client's own handling decides what reaches the log.
        token: The configured token: the marker, or the marker with the
            trailing space h11 refuses to send.

    Returns:
        The client, ready to replace ``app.state.paperless``.

    """

    def refuse(request: httpx2.Request) -> NoReturn:
        _ = request
        raise error

    return PaperlessClient(
        url="http://paperless.invalid:8000",
        token=token,
        transport=httpx2.MockTransport(refuse),
    )


def _client_refuses_the_connection(app: FastAPI) -> None:
    """Make the tag fetch fail inside a real client, quoting its token."""
    app.state.paperless = _marked_client(httpx2.ConnectError(f"refused: {_MARKER}"))


def _client_cannot_send_the_request(app: FastAPI) -> None:
    """Make the tag fetch fail the way h11 refuses a token it cannot send."""
    app.state.paperless = _marked_client(
        httpx2.LocalProtocolError(f"Illegal header value b'Token {_MARKER} '"),
        token=f"{_MARKER} ",
    )


def _fetch_raises_a_foreign_exception(app: FastAPI) -> None:
    """Make the tag fetch raise something no saneless code wrote."""
    app.state.paperless.get_tags = _raise_factory(RuntimeError, _MARKED_TEXT)


def _connection_test_raises(app: FastAPI) -> None:
    """Make the connection test raise httpx2's error, quoting the marker."""
    app.state.paperless.test_connection = _raise_factory(
        httpx2.ConnectError, _MARKED_TEXT
    )


class TestWebLogsNoClientSecret:
    """
    Marker text fed through every web logging path never reaches the log.

    Each case makes a Paperless call fail with text carrying the marker, then
    reads ``caplog.text``, which includes any formatted traceback.  Where the
    exception comes from the client (a ``PaperlessError`` or ``ConfigError``),
    it is raised by a real client configured with the marker as its token, so
    the test proves the client's redaction rather than a hand-made message.
    ``tests/test_cache.py`` covers the cache's own stale-copy warnings.
    """

    @pytest.mark.parametrize(
        ("install", "path", "logged"),
        [
            pytest.param(
                _client_refuses_the_connection,
                "/api/tags",
                "Could not fetch tags from Paperless at http://paperless.invalid:8000",
                id="fetch-paperless-error",
            ),
            pytest.param(
                _client_cannot_send_the_request,
                "/api/tags",
                "the request could not be sent with the configured paperless.url",
                id="fetch-config-error",
            ),
            pytest.param(
                _fetch_raises_a_foreign_exception,
                "/api/tags",
                "using empty list: RuntimeError",
                id="fetch-other",
            ),
            pytest.param(
                _connection_test_raises,
                "/api/paperless/test",
                "Paperless connection test failed: ConnectError",
                id="connection-test",
            ),
        ],
    )
    def test_marker_text_never_reaches_the_log(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        install: Callable[[FastAPI], None],
        path: str,
        logged: str,
    ) -> None:
        """The request still answers, and the warning says what failed without it."""
        app = _simple_form_app(tmp_path)
        install(app)
        caplog.set_level(logging.DEBUG)
        with TestClient(app) as client:
            response = client.get(path)

        assert response.status_code in {200, 500}
        assert _MARKER not in caplog.text
        assert "user:pass" not in caplog.text
        web_records = [
            record
            for record in caplog.records
            if record.name.startswith("saneless.web")
            and record.levelno >= logging.WARNING
        ]
        assert [record.exc_info for record in web_records] == [None]
        assert logged in web_records[0].getMessage()
