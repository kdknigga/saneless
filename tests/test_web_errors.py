"""
Tests for the web layer's single error-rendering path.

Every 4xx and 5xx the application produces is rendered by one function (D-01).
An htmx request's error is retargeted into the ``#status-message`` slot so it
never lands in its original target (D-02, D-03); any other request gets the
``{"status": "error", "detail": ...}`` JSON shape with the same status, and a
429 carries ``Retry-After`` on both branches (D-04).  The error bodies and the
log lines never carry request input or exception text.

The routes these tests drive are test-only: they are added to the fixture app
before the client starts, so the renderer is proven independently of which
production route raises.

Covers requirements: ROBU-02, ROBU-08.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import pytest
from fastapi import FastAPI, Form, HTTPException
from fastapi.testclient import TestClient
from markupsafe import escape
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
    QUEUE_FULL_JOB_ERROR,
    TITLE_MAX_LENGTH,
    WORKER_DEGRADED_JOB_ERROR,
    WORKER_DOWN_JOB_ERROR,
    ErrorCategory,
    JobState,
    RequestRejection,
    SubmitResult,
    WorkerHealth,
    rejection_message,
    rejection_status_code,
)
from saneless.web import errors
from saneless.web.app import create_app
from saneless.worker import ScanWorker

if TYPE_CHECKING:
    from collections.abc import Iterator

    import httpx

    from saneless.job import Job, JobStore

HTMX_HEADERS = {"HX-Request": "true"}

HISTORY_LOADER = (
    '<div hx-get="/api/jobs/history" hx-target="#history-body" '
    'hx-swap="outerHTML" hx-trigger="load" class="htmx-hidden"></div>'
)

INPUT_MARKER = "zz-marker"
BAD_INT = "abc"
SECRET_MARKER = "zz-secret"


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


# --- Test-only routes --------------------------------------------------------


def _raise_rejection(name: str) -> None:
    """Raise the RequestRejected named in the path."""
    raise errors.RequestRejected(RequestRejection(name))


def _raise_rejection_with_history(name: str) -> None:
    """Raise the RequestRejected named in the path, asking for a history refresh."""
    raise errors.RequestRejected(RequestRejection(name), refresh_history=True)


def _raise_plain_http_exception(status: int) -> None:
    """Raise a bare HTTPException with the status in the path."""
    raise HTTPException(status_code=status)


def _validate_form(
    title: Annotated[str, Form(max_length=TITLE_MAX_LENGTH)],
    n: Annotated[int, Form()],
) -> dict[str, str]:
    """Accept a capped title and an integer, so bad input reaches validation."""
    return {"title": title, "n": str(n)}


def _raise_runtime_error() -> None:
    """Fail with an exception whose text must never reach the client."""
    raise RuntimeError(SECRET_MARKER)


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
    """Create the FastAPI app with the test-only error routes added."""
    application = create_app(test_settings, web_scanner)
    application.add_api_route("/_test/reject/{name}", _raise_rejection)
    application.add_api_route(
        "/_test/reject-history/{name}", _raise_rejection_with_history
    )
    application.add_api_route("/_test/http/{status}", _raise_plain_http_exception)
    application.add_api_route("/_test/validate", _validate_form, methods=["POST"])
    application.add_api_route("/_test/boom", _raise_runtime_error)
    return application


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


@pytest.fixture
def lenient_client(app: FastAPI, mock_paperless: object) -> Iterator[TestClient]:
    """
    TestClient that returns 500 responses instead of re-raising.

    ServerErrorMiddleware re-raises after sending the catch-all handler's
    response, and the default TestClient would re-raise that into the test.
    """
    _ = mock_paperless
    with TestClient(app, raise_server_exceptions=False) as tc:
        yield tc


def _error_paragraph(rejection: RequestRejection) -> str:
    """Return the exact error paragraph markup, escaped as Jinja autoescapes it."""
    return (
        f'<p class="status-error">&#10007; {escape(rejection_message(rejection))}</p>'
    )


def _assert_htmx_error(
    response: httpx.Response, rejection: RequestRejection, status: int
) -> None:
    """Assert an htmx error response is retargeted and carries only the paragraph."""
    assert response.status_code == status
    assert response.headers["HX-Retarget"] == "#status-message"
    assert response.headers["HX-Reswap"] == "innerHTML"
    assert response.text.strip() == _error_paragraph(rejection)


def _assert_json_error(
    response: httpx.Response, rejection: RequestRejection, status: int
) -> None:
    """Assert a non-htmx error response is the JSON shape, not retargeted."""
    assert response.status_code == status
    assert "HX-Retarget" not in response.headers
    assert "HX-Reswap" not in response.headers
    assert response.json() == {
        "status": "error",
        "detail": rejection_message(rejection),
    }


# --- T4 / T5: every rejection, both branches ---------------------------------


@pytest.mark.parametrize("rejection", list(RequestRejection))
def test_htmx_rejection_is_retargeted_into_the_message_slot(
    client: TestClient, rejection: RequestRejection
) -> None:
    """An htmx error lands in #status-message and nowhere else (D-02, T4)."""
    response = client.get(f"/_test/reject/{rejection.value}", headers=HTMX_HEADERS)
    _assert_htmx_error(response, rejection, rejection_status_code(rejection))
    for forbidden in ("scan-btn", "hx-swap-oob", "status-area", 'role="alert"'):
        assert forbidden not in response.text


@pytest.mark.parametrize("rejection", list(RequestRejection))
def test_non_htmx_rejection_is_json(
    client: TestClient, rejection: RequestRejection
) -> None:
    """A request without HX-Request gets the JSON error shape (D-04, T4)."""
    response = client.get(f"/_test/reject/{rejection.value}")
    _assert_json_error(response, rejection, rejection_status_code(rejection))


@pytest.mark.parametrize("rejection", list(RequestRejection))
@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "json"])
def test_retry_after_only_on_queue_full(
    client: TestClient, rejection: RequestRejection, *, htmx: bool
) -> None:
    """429 carries Retry-After on both branches; nothing else does (D-04, T5)."""
    headers = HTMX_HEADERS if htmx else {}
    response = client.get(f"/_test/reject/{rejection.value}", headers=headers)
    if rejection is RequestRejection.QUEUE_FULL:
        assert response.status_code == 429
        assert response.headers["Retry-After"] == str(errors.RETRY_AFTER_SECONDS)
        assert errors.RETRY_AFTER_SECONDS == 30
    else:
        assert "Retry-After" not in response.headers


def test_refresh_history_appends_the_hidden_history_loader(
    client: TestClient,
) -> None:
    """A rejection that wrote a job row reloads Job History (D-05, UI-SPEC S2)."""
    rejection = RequestRejection.QUEUE_FULL
    response = client.get(
        f"/_test/reject-history/{rejection.value}", headers=HTMX_HEADERS
    )
    assert response.status_code == 429
    assert response.headers["HX-Retarget"] == "#status-message"
    assert response.text.strip() == f"{_error_paragraph(rejection)}\n{HISTORY_LOADER}"


def test_no_history_loader_without_refresh_history(client: TestClient) -> None:
    """The history loader is absent unless refresh_history is set."""
    response = client.get("/_test/reject/QUEUE_FULL", headers=HTMX_HEADERS)
    assert "/api/jobs/history" not in response.text


# --- Framework-raised errors -------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/does-not-exist", "/static/does-not-exist.js"], ids=["router", "static"]
)
def test_not_found_renders_on_both_branches(client: TestClient, path: str) -> None:
    """Router and StaticFiles 404s go through the one renderer (D-01)."""
    _assert_htmx_error(
        client.get(path, headers=HTMX_HEADERS), RequestRejection.NOT_FOUND, 404
    )
    _assert_json_error(client.get(path), RequestRejection.NOT_FOUND, 404)


def test_method_not_allowed_renders_on_both_branches(client: TestClient) -> None:
    """A router 405 goes through the one renderer (D-01)."""
    rejection = RequestRejection.METHOD_NOT_ALLOWED
    _assert_htmx_error(client.post("/health", headers=HTMX_HEADERS), rejection, 405)
    _assert_json_error(client.post("/health"), rejection, 405)


@pytest.mark.parametrize(
    ("status", "rejection"),
    [(409, RequestRejection.CLIENT_ERROR), (502, RequestRejection.INTERNAL)],
)
def test_plain_http_exception_maps_by_status(
    client: TestClient, status: int, rejection: RequestRejection
) -> None:
    """A bare HTTPException keeps its status and gets the generic message."""
    path = f"/_test/http/{status}"
    _assert_htmx_error(client.get(path, headers=HTMX_HEADERS), rejection, status)
    _assert_json_error(client.get(path), rejection, status)


# --- T7: validation and the catch-all never leak -----------------------------


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "json"])
def test_title_too_long_is_422_without_echoing_input(
    client: TestClient, caplog: pytest.LogCaptureFixture, *, htmx: bool
) -> None:
    """A title over the cap is TITLE_TOO_LONG and its text is never echoed (ROBU-08)."""
    title = (INPUT_MARKER * 29)[: TITLE_MAX_LENGTH + 1]
    assert len(title) == TITLE_MAX_LENGTH + 1
    headers = HTMX_HEADERS if htmx else {}
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/_test/validate", data={"title": title, "n": "1"}, headers=headers
        )
    rejection = RequestRejection.TITLE_TOO_LONG
    if htmx:
        _assert_htmx_error(response, rejection, 422)
    else:
        _assert_json_error(response, rejection, 422)
    assert INPUT_MARKER not in response.text
    assert INPUT_MARKER not in caplog.text
    assert all(INPUT_MARKER not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "json"])
def test_invalid_field_is_422_without_echoing_input(
    client: TestClient, caplog: pytest.LogCaptureFixture, *, htmx: bool
) -> None:
    """Any other validation failure is INVALID_REQUEST and echoes nothing (D-01)."""
    headers = HTMX_HEADERS if htmx else {}
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/_test/validate", data={"title": "ok", "n": BAD_INT}, headers=headers
        )
    rejection = RequestRejection.INVALID_REQUEST
    if htmx:
        _assert_htmx_error(response, rejection, 422)
    else:
        _assert_json_error(response, rejection, 422)
    assert BAD_INT not in response.text
    assert BAD_INT not in caplog.text
    assert all(BAD_INT not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "json"])
def test_unhandled_exception_is_500_and_logged_not_leaked(
    lenient_client: TestClient, caplog: pytest.LogCaptureFixture, *, htmx: bool
) -> None:
    """The catch-all renders INTERNAL and logs the traceback server-side (D-01)."""
    headers = HTMX_HEADERS if htmx else {}
    with caplog.at_level(logging.ERROR, logger="saneless.web.errors"):
        response = lenient_client.get("/_test/boom", headers=headers)
    rejection = RequestRejection.INTERNAL
    if htmx:
        _assert_htmx_error(response, rejection, 500)
    else:
        _assert_json_error(response, rejection, 500)
    assert SECRET_MARKER not in response.text
    assert "RuntimeError" not in response.text
    records = [
        r
        for r in caplog.records
        if r.name == "saneless.web.errors" and r.levelno == logging.ERROR
    ]
    assert records
    assert all(r.exc_info is not None for r in records)


# --- The production scan route: 422 before any row, 429 and 503 with one ------


def _job_store(client: TestClient) -> JobStore:
    """Return the served app's job store."""
    app = client.app
    assert isinstance(app, FastAPI)
    return app.state.job_store


def _worker(client: TestClient) -> ScanWorker:
    """Return the served app's scan worker."""
    app = client.app
    assert isinstance(app, FastAPI)
    return app.state.worker


def _force_health(monkeypatch: pytest.MonkeyPatch, health: WorkerHealth) -> None:
    """
    Make every worker report ``health`` for the rest of the test.

    Patching the property rather than setting the degraded Event keeps the
    worker thread's idle recovery probe from clearing it mid-request.
    """
    monkeypatch.setattr(ScanWorker, "health", property(lambda _self: health))


def _refuse_submit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, result: SubmitResult
) -> list[Job]:
    """Make the worker refuse every submit with ``result``; return the offers."""
    offered: list[Job] = []

    def submit(job: Job) -> SubmitResult:
        offered.append(job)
        return result

    monkeypatch.setattr(_worker(client), "submit", submit)
    return offered


def _assert_rejected_row(client: TestClient, error: str) -> None:
    """Assert the newest job row records a rejected submit with ``error``."""
    newest = _job_store(client).list_recent(limit=1)
    assert len(newest) == 1
    assert newest[0].state is JobState.ERROR
    assert newest[0].error == error
    assert newest[0].error_category is ErrorCategory.REJECTED


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "json"])
def test_scan_unknown_profile_is_422_without_a_row(
    client: TestClient, *, htmx: bool
) -> None:
    """An unknown profile is refused before any job row exists (ROBU-08, D-19)."""
    before = _job_store(client).list_recent(limit=50)
    headers = HTMX_HEADERS if htmx else {}
    response = client.post(
        "/api/scan",
        data={"profile": f"{INPUT_MARKER}-<script>", "title": "Profile Test"},
        headers=headers,
    )
    rejection = RequestRejection.UNKNOWN_PROFILE
    if htmx:
        _assert_htmx_error(response, rejection, 422)
    else:
        _assert_json_error(response, rejection, 422)
    assert INPUT_MARKER not in response.text
    assert _job_store(client).list_recent(limit=50) == before


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "json"])
def test_scan_title_too_long_is_422_without_a_row(
    client: TestClient, *, htmx: bool
) -> None:
    """A title over the cap is refused before any job row exists (ROBU-08)."""
    title = (INPUT_MARKER * 29)[: TITLE_MAX_LENGTH + 1]
    before = _job_store(client).list_recent(limit=50)
    headers = HTMX_HEADERS if htmx else {}
    response = client.post(
        "/api/scan", data={"profile": "default", "title": title}, headers=headers
    )
    rejection = RequestRejection.TITLE_TOO_LONG
    if htmx:
        _assert_htmx_error(response, rejection, 422)
    else:
        _assert_json_error(response, rejection, 422)
    assert INPUT_MARKER not in response.text
    assert _job_store(client).list_recent(limit=50) == before


def test_scan_title_at_the_cap_is_not_422(client: TestClient) -> None:
    """A title exactly at the cap is accepted (ROBU-08)."""
    title = "t" * TITLE_MAX_LENGTH
    response = client.post(
        "/api/scan",
        data={"profile": "default", "title": title},
        headers=HTMX_HEADERS,
    )
    assert response.status_code == 200
    assert 'id="status-area"' in response.text


def test_scan_queue_full_is_429_with_a_rejected_row_htmx(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full queue is a visible 429 that reloads history (ROBU-02, D-05)."""
    _refuse_submit(client, monkeypatch, SubmitResult.QUEUE_FULL)
    response = client.post(
        "/api/scan",
        data={"profile": "default", "title": "Queue Full"},
        headers=HTMX_HEADERS,
    )
    rejection = RequestRejection.QUEUE_FULL
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    assert response.headers["HX-Retarget"] == "#status-message"
    assert response.text.strip() == f"{_error_paragraph(rejection)}\n{HISTORY_LOADER}"
    _assert_rejected_row(client, QUEUE_FULL_JOB_ERROR)


def test_scan_queue_full_is_429_json(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-htmx submit refused by a full queue is JSON with Retry-After (D-04)."""
    _refuse_submit(client, monkeypatch, SubmitResult.QUEUE_FULL)
    response = client.post(
        "/api/scan", data={"profile": "default", "title": "Queue Full"}
    )
    _assert_json_error(response, RequestRejection.QUEUE_FULL, 429)
    assert response.headers["Retry-After"] == "30"
    _assert_rejected_row(client, QUEUE_FULL_JOB_ERROR)


@pytest.mark.parametrize(
    ("result", "rejection", "error"),
    [
        (SubmitResult.DOWN, RequestRejection.WORKER_DOWN, WORKER_DOWN_JOB_ERROR),
        (
            SubmitResult.DEGRADED,
            RequestRejection.WORKER_DEGRADED,
            WORKER_DEGRADED_JOB_ERROR,
        ),
    ],
    ids=["down", "degraded"],
)
def test_scan_refused_submit_is_503_with_a_rejected_row(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    result: SubmitResult,
    rejection: RequestRejection,
    error: str,
) -> None:
    """A submit refused as down or degraded is a 503 with a row (D-05, D-11)."""
    _refuse_submit(client, monkeypatch, result)
    response = client.post(
        "/api/scan",
        data={"profile": "default", "title": "Refused"},
        headers=HTMX_HEADERS,
    )
    assert response.status_code == 503
    assert "Retry-After" not in response.headers
    assert response.text.strip() == f"{_error_paragraph(rejection)}\n{HISTORY_LOADER}"
    _assert_rejected_row(client, error)


@pytest.mark.parametrize(
    ("health", "rejection", "error"),
    [
        (WorkerHealth.DOWN, RequestRejection.WORKER_DOWN, WORKER_DOWN_JOB_ERROR),
        (
            WorkerHealth.DEGRADED,
            RequestRejection.WORKER_DEGRADED,
            WORKER_DEGRADED_JOB_ERROR,
        ),
    ],
    ids=["down", "degraded"],
)
def test_scan_unhealthy_worker_is_503_before_submit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    health: WorkerHealth,
    rejection: RequestRejection,
    error: str,
) -> None:
    """An unhealthy worker refuses the scan without offering it the job (D-11)."""
    offered = _refuse_submit(client, monkeypatch, SubmitResult.ACCEPTED)
    _force_health(monkeypatch, health)
    response = client.post(
        "/api/scan",
        data={"profile": "default", "title": "Unhealthy"},
        headers=HTMX_HEADERS,
    )
    assert response.status_code == 503
    assert response.text.strip() == f"{_error_paragraph(rejection)}\n{HISTORY_LOADER}"
    assert offered == []
    _assert_rejected_row(client, error)


def test_scan_degraded_store_failing_is_503_without_a_loader(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejection the store cannot record still renders, without a loader (D-05)."""
    offered = _refuse_submit(client, monkeypatch, SubmitResult.ACCEPTED)
    _force_health(monkeypatch, WorkerHealth.DEGRADED)
    store = _job_store(client)

    def failing_create_job(*_args: object, **_kwargs: object) -> Job:
        msg = "disk I/O error"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(store, "create_job", failing_create_job)
    with caplog.at_level(logging.WARNING, logger="saneless.web.routes"):
        response = client.post(
            "/api/scan",
            data={"profile": "default", "title": "Store Failing"},
            headers=HTMX_HEADERS,
        )
    _assert_htmx_error(response, RequestRejection.WORKER_DEGRADED, 503)
    assert "/api/jobs/history" not in response.text
    assert offered == []
    warnings = [
        r
        for r in caplog.records
        if r.name == "saneless.web.routes" and r.levelno == logging.WARNING
    ]
    assert warnings
    assert all(r.exc_info is not None for r in warnings)


# --- The client half: htmx-config meta and the #status-message slot ----------

WEB_DIR = Path(__file__).parent.parent / "src" / "saneless" / "web"
STATUS_MESSAGE_SLOT = '<div id="status-message" role="alert"></div>'
_HTMX_CONFIG_META = re.compile(r"""<meta name="htmx-config"\s+content='([^']*)'>""")
_CSS_RULE = re.compile(r"#status-message > p \{(?P<body>[^}]*)\}")


def test_status_message_slot_is_one_empty_alert_above_the_status_area(
    client: TestClient,
) -> None:
    """
    The page has one empty alert slot, just above #status-area (D-03).

    It is a sibling of the polled status area, so the 1 s outerHTML poll never
    replaces it, and it sits outside the form.
    """
    page = client.get("/").text
    assert page.count(STATUS_MESSAGE_SLOT) == 1
    assert page.count('id="status-message"') == 1
    slot = page.index(STATUS_MESSAGE_SLOT)
    assert slot < page.index('id="status-area"')
    form = page[page.index("<form") : page.index("</form>")]
    assert STATUS_MESSAGE_SLOT not in form
    assert page.index("</form>") < slot


def test_htmx_config_restates_all_three_response_handling_entries(
    client: TestClient,
) -> None:
    """
    The htmx-config meta swaps error bodies without breaking 2xx swaps (D-01).

    htmx 2.0.8 merges meta config shallowly, so a meta holding only the
    ``[45]..`` entry would replace the whole array and stop every 2xx swap.
    All three entries have to be restated.
    """
    page = client.get("/").text
    match = _HTMX_CONFIG_META.search(page)
    assert match is not None
    config = json.loads(match.group(1))
    assert isinstance(config, dict)
    assert config["responseHandling"] == [
        {"code": "204", "swap": False},
        {"code": "[23]..", "swap": True},
        {"code": "[45]..", "swap": True, "error": True},
    ]


def test_htmx_config_meta_sits_between_color_scheme_and_title() -> None:
    """The meta follows the color-scheme meta and precedes <title> (UI-SPEC S1)."""
    base = (WEB_DIR / "templates" / "base.html").read_text()
    assert base.count('name="htmx-config"') == 1
    color_scheme = base.index('<meta name="color-scheme"')
    htmx_config = base.index('<meta name="htmx-config"')
    title = base.index("<title>")
    assert color_scheme < htmx_config < title


def test_status_message_paragraph_is_inset_like_the_status_area() -> None:
    """An error paragraph lines up with the status area's text (D-03)."""
    css = (WEB_DIR / "static" / "app.css").read_text()
    assert "!important" not in css
    rules = _CSS_RULE.findall(css)
    assert len(rules) == 1
    body = rules[0]
    assert "padding-left: 1rem;" in body
    assert "border-left: var(--pico-border-width) solid transparent;" in body
