"""
Tests for the web layer's single error-rendering path.

Every 4xx and 5xx the application produces is rendered by one function.  An
htmx request's error is retargeted into the ``#status-message`` slot so it
never lands in its original target; any other request gets the
``{"status": "error", "detail": ...}`` JSON shape with the same status, and a
429 carries ``Retry-After`` on both branches.  The error bodies and the log
lines never carry request input or exception text.

The routes these tests drive are test-only: they are added to the fixture app
before the client starts, so the renderer is proven independently of which
production route raises.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import pytest
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from markupsafe import escape

from saneless.config import (
    PaperlessConfig,
    ProfileConfig,
    Settings,
)
from saneless.job import REJECTED_HISTORY_ROWS
from saneless.vocabulary import (
    HIDDEN_JOB_TITLE,
    LOST_CONTACT_LINE,
    QUEUE_FULL_JOB_ERROR,
    TITLE_MAX_LENGTH,
    TOKEN_UNSET_JOB_ERROR,
    URL_UNSET_JOB_ERROR,
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
from saneless.web.owner import OWNER_COOKIE
from saneless.worker import ScanOptions, ScanWorker
from tests.conftest import StubScannerBackend, poll_until, services_of, stand_in
from tests.template_support import markup_start_tags, template_start_tags

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

    import httpx2
    from starlette.responses import Response

    from saneless.job import Job, JobStore


HTMX_HEADERS = {"HX-Request": "true"}

HISTORY_LOADER = (
    '<div hx-get="/api/jobs/history" hx-target="#history-body" '
    'hx-swap="outerHTML" hx-trigger="load" class="htmx-hidden"></div>'
)

INPUT_MARKER = "zz-marker"
BAD_INT = "abc"
SECRET_MARKER = "zz-secret"

# The log file every app in this module writes to.  A host filesystem path must
# never reach a LAN-visible page, so this name is deliberately unlike anything
# the slot could produce by accident.
LOG_FILE_NAME = "errors-test-do-not-render-me.log"

# The row the test-only history route pretends its refusal wrote.  The renderer
# reloads Job History exactly when there is a row to reload it for, so this
# route has to name one to ask for the reload at all.
REJECTED_ROW_ID = "zz-rejected-row"


# --- Test-only routes --------------------------------------------------------


def _raise_rejection(name: str) -> None:
    """Raise the RequestRejected named in the path."""
    raise errors.RequestRejected(RequestRejection(name))


def _raise_rejection_with_history(name: str) -> None:
    """Raise the RequestRejected named in the path, as if it had written a row."""
    raise errors.RequestRejected(RequestRejection(name), job_id=REJECTED_ROW_ID)


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
def web_settings(make_settings: Callable[..., Settings], tmp_path: Path) -> Settings:
    """Build the web app's settings: the suite defaults plus a ``duplex`` profile."""
    settings = make_settings(
        profiles={
            "default": ProfileConfig(),
            "duplex": ProfileConfig(source="ADF Duplex"),
        },
    )
    # Named distinctively so the slot can be searched for it and mean
    # something: the default is a path fragment that could collide with
    # tmp_path itself and pass on nothing.
    settings.output.log_file = tmp_path / LOG_FILE_NAME
    return settings


@pytest.fixture
def web_scanner() -> StubScannerBackend:
    """Return the shared concrete stub backend for web tests."""
    return StubScannerBackend()


@pytest.fixture
def app(web_settings: Settings, web_scanner: StubScannerBackend) -> FastAPI:
    """Create the FastAPI app with the test-only error routes added."""
    application = create_app(web_settings, web_scanner)
    # Served on POST as well as GET so the retarget exemption, which is keyed
    # on the method, can be pinned for every rejection in both directions.
    application.add_api_route(
        "/_test/reject/{name}", _raise_rejection, methods=["GET", "POST"]
    )
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
    stand_in(
        services_of(app).paperless,
        "get_tags",
        lambda *, timeout=None: [
            {"id": 1, "name": "receipt"},
            {"id": 2, "name": "invoice"},
        ],
    )
    stand_in(
        services_of(app).paperless,
        "get_correspondents",
        lambda *, timeout=None: [
            {"id": 1, "name": "ACME Corp"},
        ],
    )
    return services_of(app).paperless


@pytest.fixture
def client(app: FastAPI, mock_paperless: object) -> Iterator[TestClient]:
    """TestClient that handles lifespan enter/exit automatically."""
    _ = mock_paperless  # Ensure paperless is patched before requests
    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def fast_tick_client(
    app: FastAPI, mock_paperless: object, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """
    TestClient whose worker takes an idle tick every 20 ms instead of every 5 s.

    The tick is patched before the lifespan starts the worker: patched later,
    the worker would already sit in its first five-second queue wait.
    """
    _ = mock_paperless
    monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", 0.02)
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


def _error_body(
    rejection: RequestRejection, status: int, job_id: str | None = None
) -> str:
    """
    Return the exact slot body: the message, then the short disclosure.

    The disclosure carries the HTTP status code and, when the refused attempt
    wrote a job row, that row's id -- and nothing else.  Exception text,
    request input and the log path never reach this surface (ASVS V7), so this
    helper is the whole permitted vocabulary of the slot.

    Args:
        rejection: The vocabulary member whose message is shown.
        status: The response's HTTP status, which is not always the
            rejection's own -- a framework 405 or 422 maps onto a generic
            rejection while keeping its own code.
        job_id: The id of the row a refused submit wrote, if it wrote one.

    Returns:
        The body the slot must equal once stripped.

    """
    lines = [
        _error_paragraph(rejection),
        '<details class="tech-details">',
        "  <summary>Technical details</summary>",
        f"  <p>Status: {status}</p>",
    ]
    if job_id is not None:
        lines.append(f"  <p>Job: {job_id}</p>")
    lines.append("</details>")
    return "\n".join(lines)


def _assert_htmx_error(
    response: httpx2.Response, rejection: RequestRejection, status: int
) -> None:
    """Assert an htmx error response is retargeted and carries only the slot body."""
    assert response.status_code == status
    assert response.headers["HX-Retarget"] == "#status-message"
    assert response.headers["HX-Reswap"] == "innerHTML"
    assert response.text.strip() == _error_body(rejection, status)


def _assert_json_error(
    response: httpx2.Response, rejection: RequestRejection, status: int
) -> None:
    """Assert a non-htmx error response is the JSON shape, not retargeted."""
    assert response.status_code == status
    assert "HX-Retarget" not in response.headers
    assert "HX-Reswap" not in response.headers
    assert response.json() == {
        "status": "error",
        "detail": rejection_message(rejection),
    }


# The Scan button a refused htmx Scan press carries out-of-band: enabled and
# with ``autofocus``, so focus goes back to the button the press came from.
# Compared as attributes, so their order in the markup does not matter.
SCAN_REFOCUS = {
    "type": "submit",
    "id": "scan-btn",
    "hx-swap-oob": "true",
    "autofocus": None,
}

# The rendered Scan button, found by its id in any attribute order.
_SCAN_BUTTON_ELEMENT = re.compile(
    r'<button(?=[^>]*\sid="scan-btn")\s[^>]*>(?P<label>.*?)</button>', re.DOTALL
)


def _slot_of_scan_refusal(text: str) -> str:
    """
    Return a refused Scan press's slot body, after checking the button it ends with.

    Args:
        text: The response body.

    Returns:
        Everything before the out-of-band Scan button, stripped.

    """
    button = _SCAN_BUTTON_ELEMENT.search(text)
    assert button is not None, text
    assert markup_start_tags(button.group(0)) == [("button", SCAN_REFOCUS)]
    assert button.group("label").strip() == "Scan"
    assert not text[button.end() :].strip(), text
    return text[: button.start()].strip()


def _assert_scan_refusal(
    response: httpx2.Response, rejection: RequestRejection, status: int
) -> None:
    """Assert a refused htmx Scan press is the slot body plus the Scan button."""
    assert response.status_code == status
    assert response.headers["HX-Retarget"] == "#status-message"
    assert response.headers["HX-Reswap"] == "innerHTML"
    assert _slot_of_scan_refusal(response.text) == _error_body(rejection, status)


# --- Every rejection, both branches ------------------------------------------


@pytest.mark.parametrize("rejection", list(RequestRejection))
def test_htmx_rejection_is_retargeted_into_the_message_slot(
    client: TestClient, rejection: RequestRejection
) -> None:
    """An htmx error lands in #status-message and nowhere else."""
    response = client.get(f"/_test/reject/{rejection.value}", headers=HTMX_HEADERS)
    _assert_htmx_error(response, rejection, rejection_status_code(rejection))
    for forbidden in ("scan-btn", "hx-swap-oob", "status-area", 'role="alert"'):
        assert forbidden not in response.text


@pytest.mark.parametrize("rejection", list(RequestRejection))
def test_non_htmx_rejection_is_json(
    client: TestClient, rejection: RequestRejection
) -> None:
    """A request without HX-Request gets the JSON error shape."""
    response = client.get(f"/_test/reject/{rejection.value}")
    _assert_json_error(response, rejection, rejection_status_code(rejection))


@pytest.mark.parametrize("rejection", list(RequestRejection))
@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "json"])
def test_retry_after_only_on_queue_full(
    client: TestClient, rejection: RequestRejection, *, htmx: bool
) -> None:
    """429 carries Retry-After on both branches; nothing else does."""
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
    """A rejection that wrote a job row reloads Job History."""
    rejection = RequestRejection.QUEUE_FULL
    response = client.get(
        f"/_test/reject-history/{rejection.value}", headers=HTMX_HEADERS
    )
    assert response.status_code == 429
    assert response.headers["HX-Retarget"] == "#status-message"
    # History reloads because a row was written, and the disclosure names that
    # same row -- one fact, not two that could disagree.
    body = _error_body(rejection, 429, REJECTED_ROW_ID)
    assert response.text.strip() == f"{body}\n{HISTORY_LOADER}"


def test_no_history_loader_without_refresh_history(client: TestClient) -> None:
    """The history loader is absent unless refresh_history is set."""
    response = client.get("/_test/reject/QUEUE_FULL", headers=HTMX_HEADERS)
    assert "/api/jobs/history" not in response.text


# --- The status strip's swap target is exempt from the retarget --------------

# What htmx puts in the ``HX-Target`` request header: the target element's id
# with no leading ``#``.  The strip's polling body carries ``hx-target="this"``
# on ``<div id="checks-body">``, so its poll requests arrive with this value.
_CHECKS_TARGET_HEADERS = {"HX-Request": "true", "HX-Target": "checks-body"}

# Another element's id, to prove the exemption is one element's and not a
# change to the application-wide error contract.
_OTHER_TARGET_HEADERS = {"HX-Request": "true", "HX-Target": "status-area"}

_TARGETED_HEADER_SETS = [_OTHER_TARGET_HEADERS, HTMX_HEADERS]
_TARGETED_HEADER_IDS = ["other-target", "no-target"]


class TestChecksPollTargetIsExemptFromTheRetarget:
    """
    A failing checks poll's GET is swapped into the strip, not the message slot.

    The strip is the one element that polls, and an armed htmx poll ends only
    when its element leaves the DOM.  Retargeted, a failing poll would leave
    ``#checks-body`` and its ``every 2s`` trigger in place for the life of the
    tab.  The exemption is keyed on the strip's id *and* on GET: ``Check
    again`` posts with the same ``HX-Target``, and its failure belongs in the
    slot, or the error body would overwrite the strip and the button with it.
    Every other target keeps the ordinary retarget.
    """

    @pytest.mark.parametrize("rejection", list(RequestRejection))
    def test_a_checks_poll_error_is_not_retargeted(
        self, client: TestClient, rejection: RequestRejection
    ) -> None:
        """The strip's own target gets neither retarget header."""
        status = rejection_status_code(rejection)
        response = client.get(
            f"/_test/reject/{rejection.value}", headers=_CHECKS_TARGET_HEADERS
        )
        assert response.status_code == status
        assert "hx-retarget" not in response.headers
        assert "hx-reswap" not in response.headers
        assert response.text.strip() == _error_body(rejection, status)

    def test_an_exempt_error_still_carries_retry_after(
        self, client: TestClient
    ) -> None:
        """Dropping the retarget drops nothing else: a 429 keeps Retry-After."""
        response = client.get(
            "/_test/reject/QUEUE_FULL", headers=_CHECKS_TARGET_HEADERS
        )
        assert response.status_code == 429
        assert response.headers["Retry-After"] == str(errors.RETRY_AFTER_SECONDS)
        assert "hx-retarget" not in response.headers

    def test_an_exempt_error_still_carries_the_allow_header(
        self, client: TestClient
    ) -> None:
        """A 405's ``Allow`` survives the exemption (RFC 9110 15.5.6)."""
        response = client.get("/api/scan", headers=_CHECKS_TARGET_HEADERS)
        assert response.status_code == 405
        allowed = {method.strip() for method in response.headers["Allow"].split(",")}
        assert "POST" in allowed
        assert "hx-retarget" not in response.headers
        assert "hx-reswap" not in response.headers

    @pytest.mark.parametrize("headers", _TARGETED_HEADER_SETS, ids=_TARGETED_HEADER_IDS)
    def test_every_other_htmx_error_is_still_retargeted(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        """Every other htmx target is still retargeted into the message slot."""
        rejection = RequestRejection.QUEUE_FULL
        response = client.get(f"/_test/reject/{rejection.value}", headers=headers)
        _assert_htmx_error(response, rejection, rejection_status_code(rejection))

    @pytest.mark.parametrize(
        "target", ["checks-body", "status-area"], ids=["strip", "other"]
    )
    def test_a_plain_request_is_json_for_either_target(
        self, client: TestClient, target: str
    ) -> None:
        """Without ``HX-Request`` the target header changes nothing."""
        rejection = RequestRejection.QUEUE_FULL
        response = client.get(
            f"/_test/reject/{rejection.value}", headers={"HX-Target": target}
        )
        _assert_json_error(response, rejection, rejection_status_code(rejection))

    def test_the_exempt_id_is_the_id_the_strip_template_ships(
        self, client: TestClient
    ) -> None:
        """
        The exempt id is the id the strip template renders.

        Renaming either one fails here rather than silently ending the
        exemption.
        """
        strip = client.get("/api/checks").text
        assert f'id="{errors.CHECKS_POLL_TARGET_ID}"' in strip

    @pytest.mark.parametrize("rejection", list(RequestRejection))
    def test_a_post_aimed_at_the_strip_is_still_retargeted(
        self, client: TestClient, rejection: RequestRejection
    ) -> None:
        """
        The same target header on a POST gets the ordinary contract.

        The strip's poll is a GET.  A POST carrying the strip's ``HX-Target``
        is the ``Check again`` button, and its failure belongs in the message
        slot: retargeted there, the strip and the button are left on the page.
        """
        response = client.post(
            f"/_test/reject/{rejection.value}", headers=_CHECKS_TARGET_HEADERS
        )
        _assert_htmx_error(response, rejection, rejection_status_code(rejection))

    def test_a_refused_check_again_click_lands_in_the_slot(
        self, client: TestClient
    ) -> None:
        """
        The real button route, refused by real middleware, is retargeted.

        ``CrossOriginGuard`` answers a cross-site POST before the route runs,
        which makes it the one failure of ``POST /api/checks/refresh`` that
        needs no patching to reach ``render_error``.  Without the retarget, the
        button's own ``hx-swap=outerHTML`` would write the error over the strip.
        """
        rejection = RequestRejection.CROSS_SITE
        response = client.post(
            "/api/checks/refresh",
            headers={**_CHECKS_TARGET_HEADERS, "Sec-Fetch-Site": "cross-site"},
        )
        _assert_htmx_error(response, rejection, rejection_status_code(rejection))

    def test_a_get_aimed_at_the_strip_is_the_only_exempt_shape(
        self, client: TestClient
    ) -> None:
        """Both halves of the key are needed: same rejection, four requests."""
        rejection = RequestRejection.QUEUE_FULL
        path = f"/_test/reject/{rejection.value}"
        exempt = client.get(path, headers=_CHECKS_TARGET_HEADERS)
        assert "hx-retarget" not in exempt.headers
        for response in (
            client.post(path, headers=_CHECKS_TARGET_HEADERS),
            client.get(path, headers=_OTHER_TARGET_HEADERS),
            client.post(path, headers=_OTHER_TARGET_HEADERS),
        ):
            assert response.headers["HX-Retarget"] == "#status-message"


# The disclosure, captured whole. It nests no <details>, so a non-greedy body is
# exact rather than merely convenient.
_TECH_DETAILS = re.compile(
    r'<details class="tech-details"(?P<attrs>[^>]*)>(?P<body>.*?)</details>',
    re.DOTALL,
)

# How many facts the slot's disclosure may carry: the HTTP status
# code, and the id of the row a refused submit wrote.  Nothing else.
_MAX_DISCLOSED_FACTS = 2


class TestRequestErrorDisclosure:
    """
    The slot's deliberately short "Technical details" disclosure.

    The exact-body assertions above already prove what the slot renders.  These
    pin the properties that make the disclosure safe rather than merely
    correct: it starts shut, it names a job only when one exists, and it can
    never grow a third fact without a test noticing.
    """

    @staticmethod
    def _details(response: httpx2.Response) -> re.Match[str]:
        """
        Return the disclosure in `response`, failing if there is none.

        Returns:
            The match, whose ``attrs`` and ``body`` groups the tests read.

        """
        match = _TECH_DETAILS.search(response.text)
        assert match is not None, "the error slot renders no disclosure"
        return match

    def test_the_disclosure_is_collapsed(self, client: TestClient) -> None:
        """No ``open`` attribute: the detail is offered, never imposed."""
        response = client.get("/_test/reject/QUEUE_FULL", headers=HTMX_HEADERS)
        assert "open" not in self._details(response).group("attrs")
        assert "<summary>Technical details</summary>" in response.text

    @pytest.mark.parametrize("rejection", list(RequestRejection))
    def test_the_disclosure_names_the_status_and_no_job(
        self, client: TestClient, rejection: RequestRejection
    ) -> None:
        """
        With no row written, the status code is the only fact (ASVS V7).

        Parametrised over every rejection so a new member cannot quietly bring
        a second fact with it.
        """
        response = client.get(f"/_test/reject/{rejection.value}", headers=HTMX_HEADERS)
        body = self._details(response).group("body")
        assert f"<p>Status: {rejection_status_code(rejection)}</p>" in body
        assert "Job:" not in body
        assert body.count("<p>") == 1

    def test_the_disclosure_names_the_row_a_refused_submit_wrote(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A refused submit's own row is the one job id the slot may name.

        It is the row the user will find in Job History a moment later, not an
        arbitrary request value.
        """
        _refuse_submit(client, monkeypatch, SubmitResult.QUEUE_FULL)
        response = client.post(
            "/api/scan",
            data={"profile": "default", "title": "Queue Full"},
            headers=HTMX_HEADERS,
        )
        body = self._details(response).group("body")
        assert f"<p>Job: {_newest_job_id(client)}</p>" in body
        assert body.count("<p>") == _MAX_DISCLOSED_FACTS

    @pytest.mark.parametrize(
        ("path", "headers"),
        [
            ("/_test/boom", HTMX_HEADERS),
            (f"/_test/reject/{RequestRejection.CROSS_SITE.value}", HTMX_HEADERS),
        ],
        ids=["unhandled", "rejection"],
    )
    def test_the_slot_leaks_neither_exception_text_nor_a_log_path(
        self, lenient_client: TestClient, path: str, headers: dict[str, str]
    ) -> None:
        """
        Neither an exception's text nor the log file reaches the slot.

        The disclosure is the affordance that would be tempted to offer both.
        """
        response = lenient_client.get(path, headers=headers)
        assert SECRET_MARKER not in response.text
        assert LOG_FILE_NAME not in response.text

    def test_the_partial_carries_no_alert_role(self) -> None:
        """
        ``#status-message`` is the alert region; the partial must not be one.

        Asserted against the template source as well as the responses above,
        so a branch no test happens to drive cannot introduce a second alert.
        """
        tags = template_start_tags(WEB_DIR / "templates" / "partials" / "error.html")
        assert tags, "no markup in the error partial"
        assert [
            tag for tag, attributes in tags if attributes.get("role") == "alert"
        ] == []


# --- Framework-raised errors -------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/does-not-exist", "/static/does-not-exist.js"], ids=["router", "static"]
)
def test_not_found_renders_on_both_branches(client: TestClient, path: str) -> None:
    """Router and StaticFiles 404s go through the one renderer."""
    _assert_htmx_error(
        client.get(path, headers=HTMX_HEADERS), RequestRejection.NOT_FOUND, 404
    )
    _assert_json_error(client.get(path), RequestRejection.NOT_FOUND, 404)


# The paths FastAPI registers for its generated schema and documentation.  The
# app registers none of them: an unauthenticated LAN appliance serving an HTMX
# UI has no stable API contract to advertise; its endpoints are the UI's own.
_SCHEMA_AND_DOCS_PATHS = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")


@pytest.mark.parametrize("path", _SCHEMA_AND_DOCS_PATHS)
def test_schema_and_docs_paths_are_not_found(client: TestClient, path: str) -> None:
    """The schema and docs paths 404 like any unknown path, on both branches."""
    _assert_htmx_error(
        client.get(path, headers=HTMX_HEADERS), RequestRejection.NOT_FOUND, 404
    )
    _assert_json_error(client.get(path), RequestRejection.NOT_FOUND, 404)


def test_method_not_allowed_renders_on_both_branches(client: TestClient) -> None:
    """A router 405 goes through the one renderer."""
    rejection = RequestRejection.METHOD_NOT_ALLOWED
    _assert_htmx_error(client.post("/health", headers=HTMX_HEADERS), rejection, 405)
    _assert_json_error(client.post("/health"), rejection, 405)


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "json"])
def test_method_not_allowed_keeps_the_allow_header(
    client: TestClient, *, htmx: bool
) -> None:
    """A 405 names the methods it does allow (RFC 9110 section 15.5.6)."""
    headers = HTMX_HEADERS if htmx else {}
    response = client.get("/api/scan", headers=headers)
    assert response.status_code == 405
    allowed = {method.strip() for method in response.headers["Allow"].split(",")}
    assert "POST" in allowed
    assert "GET" not in allowed


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


# --- Validation and the catch-all never leak ---------------------------------


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "json"])
def test_title_too_long_is_422_without_echoing_input(
    client: TestClient, caplog: pytest.LogCaptureFixture, *, htmx: bool
) -> None:
    """A title over the cap is TITLE_TOO_LONG and its text is never echoed."""
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
    """Any other validation failure is INVALID_REQUEST and echoes nothing."""
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
    """The catch-all renders INTERNAL and logs the traceback server-side."""
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


# A decoded path carrying a terminal escape and a newline, as uvicorn would
# hand it over for %1B and %0A.
_CONTROL_PATH = "/_test/zz\x1b[2J\nFAKE LOG LINE"


def _control_character_request() -> Request:
    """
    Build a request whose method and path carry control characters.

    httpx2 strips control characters from a URL, so the scope is built directly
    rather than sent through ``TestClient``, as the cross-origin guard's test
    does.

    Returns:
        A plain (non-htmx) request, so rendering needs no application.

    """
    return Request(
        {
            "type": "http",
            "method": "POST\x1b",
            "scheme": "http",
            "path": _CONTROL_PATH,
            "raw_path": _CONTROL_PATH.encode(),
            "root_path": "",
            "query_string": b"",
            "server": ("testserver", 80),
            "headers": [(b"host", b"testserver")],
        }
    )


@pytest.mark.parametrize("handler", ["validation", "unhandled"])
def test_error_logs_escape_control_characters_in_the_request_line(
    caplog: pytest.LogCaptureFixture, handler: str
) -> None:
    """
    The 422 and 500 log lines escape the method and path with ``%r``.

    A percent-encoded newline or terminal escape in the path must not forge a
    log line or rewrite what an operator reads.
    """
    request = _control_character_request()

    async def call_handler() -> Response:
        if handler == "validation":
            return await errors._validation_error(
                request,
                RequestValidationError([{"loc": ("body", "n"), "type": "int_parsing"}]),
            )
        return await errors._unhandled_exception(request, RuntimeError(SECRET_MARKER))

    # On its own thread: a Playwright session earlier in the same run can leave
    # an event loop running on this one, where asyncio.run refuses.
    with (
        caplog.at_level(logging.INFO, logger="saneless.web.errors"),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        response = pool.submit(lambda: asyncio.run(call_handler())).result()
    assert response.status_code == (422 if handler == "validation" else 500)
    records = [r for r in caplog.records if r.name == "saneless.web.errors"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "\\x1b" in message
    assert "\x1b" not in message
    assert "\n" not in message


# --- The production scan route: 422 before any row, 429 and 503 with one ------


def _job_store(client: TestClient) -> JobStore:
    """Return the served app's job store."""
    app = client.app
    assert isinstance(app, FastAPI)
    return services_of(app).job_store


def _worker(client: TestClient) -> ScanWorker:
    """Return the served app's scan worker."""
    app = client.app
    assert isinstance(app, FastAPI)
    return services_of(app).worker


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

    def submit(job: Job, _options: ScanOptions) -> SubmitResult:
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


def _newest_job_id(client: TestClient) -> str:
    """
    Return the id of the newest job row.

    Returns:
        The row the refused submit just wrote, which is the only job id the
        error slot is permitted to name.

    """
    newest = _job_store(client).list_recent(limit=1)
    assert len(newest) == 1
    return newest[0].id


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "json"])
def test_scan_unknown_profile_is_422_without_a_row(
    client: TestClient, *, htmx: bool
) -> None:
    """An unknown profile is refused before any job row exists."""
    before = _job_store(client).list_recent(limit=50)
    headers = HTMX_HEADERS if htmx else {}
    response = client.post(
        "/api/scan",
        data={"profile": f"{INPUT_MARKER}-<script>", "title": "Profile Test"},
        headers=headers,
    )
    rejection = RequestRejection.UNKNOWN_PROFILE
    if htmx:
        _assert_scan_refusal(response, rejection, 422)
    else:
        _assert_json_error(response, rejection, 422)
    assert INPUT_MARKER not in response.text
    assert _job_store(client).list_recent(limit=50) == before


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "json"])
def test_scan_title_too_long_is_422_without_a_row(
    client: TestClient, *, htmx: bool
) -> None:
    """A title over the cap is refused before any job row exists."""
    title = (INPUT_MARKER * 29)[: TITLE_MAX_LENGTH + 1]
    before = _job_store(client).list_recent(limit=50)
    headers = HTMX_HEADERS if htmx else {}
    response = client.post(
        "/api/scan", data={"profile": "default", "title": title}, headers=headers
    )
    rejection = RequestRejection.TITLE_TOO_LONG
    if htmx:
        _assert_scan_refusal(response, rejection, 422)
    else:
        _assert_json_error(response, rejection, 422)
    assert INPUT_MARKER not in response.text
    assert _job_store(client).list_recent(limit=50) == before


def test_scan_title_at_the_cap_is_not_422(client: TestClient) -> None:
    """A title exactly at the cap is accepted."""
    title = "t" * TITLE_MAX_LENGTH
    response = client.post(
        "/api/scan",
        data={"profile": "default", "title": title},
        headers=HTMX_HEADERS,
    )
    assert response.status_code == 200
    assert 'id="status-area"' in response.text


# What paperless-ngx keeps of a title (127) less the longest suffix a split
# duplex job appends (" (fronts)").  Spelled out rather than imported, so the
# tests pin the number the operator is told.
_TITLE_CAP = 118


def test_scan_title_one_over_what_paperless_keeps_is_422_naming_the_cap(
    client: TestClient,
) -> None:
    """A 119-character title is TITLE_TOO_LONG, and the sentence names 118."""
    before = _job_store(client).list_recent(limit=50)
    response = client.post(
        "/api/scan", data={"profile": "default", "title": "t" * (_TITLE_CAP + 1)}
    )
    _assert_json_error(response, RequestRejection.TITLE_TOO_LONG, 422)
    assert f"{_TITLE_CAP} characters or fewer" in response.json()["detail"]
    assert _job_store(client).list_recent(limit=50) == before


def test_scan_title_of_what_paperless_keeps_is_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 118-character title is offered to the worker exactly as typed."""
    offered = _refuse_submit(client, monkeypatch, SubmitResult.ACCEPTED)
    title = "t" * _TITLE_CAP
    response = client.post(
        "/api/scan",
        data={"profile": "default", "title": title},
        headers=HTMX_HEADERS,
    )
    assert response.status_code == 200
    assert [job.title for job in offered] == [title]


def test_scan_form_title_maxlength_is_what_paperless_keeps(
    client: TestClient,
) -> None:
    """The title input's maxlength is the same 118 the server enforces."""
    response = client.get("/")
    assert response.status_code == 200
    assert f'maxlength="{_TITLE_CAP}"' in response.text


# One control character from each part of the refused set: a tab, which a
# person can type into a text input; ESC, which starts a terminal escape
# sequence; NEL, a C1 control; and DEL, the one control outside both blocks.
_TITLE_CONTROL_CHARACTERS = ["\t", "\x1b", "\x85", "\x7f"]
_TITLE_CONTROL_IDS = ["tab", "esc", "nel", "del"]


@pytest.mark.parametrize("character", _TITLE_CONTROL_CHARACTERS, ids=_TITLE_CONTROL_IDS)
@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "json"])
def test_scan_title_control_character_is_422_without_a_row(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    character: str,
    *,
    htmx: bool,
) -> None:
    """A title holding a control character is refused, not repaired, and not echoed."""
    title = f"{INPUT_MARKER}a{character}b"
    before = _job_store(client).list_recent(limit=50)
    headers = HTMX_HEADERS if htmx else {}
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/api/scan", data={"profile": "default", "title": title}, headers=headers
        )
    rejection = RequestRejection.TITLE_HAS_CONTROL
    if htmx:
        _assert_scan_refusal(response, rejection, 422)
    else:
        _assert_json_error(response, rejection, 422)
    assert INPUT_MARKER not in response.text
    assert all(INPUT_MARKER not in r.getMessage() for r in caplog.records)
    assert _job_store(client).list_recent(limit=50) == before


def test_scan_title_control_check_accepts_accents_and_no_break_space(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An accented letter and a no-break space are text, not control characters."""
    offered = _refuse_submit(client, monkeypatch, SubmitResult.ACCEPTED)
    title = "Caf\u00e9\u00a0receipt"
    response = client.post(
        "/api/scan",
        data={"profile": "default", "title": title},
        headers=HTMX_HEADERS,
    )
    assert response.status_code == 200
    assert [job.title for job in offered] == [title]


# The tag cap the scan form, the tag list and the tag refresh all enforce.
# Spelled out rather than imported, so the test pins the documented number.
_TAGS_CAP = 100


def _tag_values(count: int) -> list[str]:
    """Return ``count`` distinct tag ids as the form strings a browser sends."""
    return [str(tag_id) for tag_id in range(1, count + 1)]


def test_scan_tags_cap_accepts_the_cap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submit ticking exactly the capped number of tags is accepted whole."""
    offered = _refuse_submit(client, monkeypatch, SubmitResult.ACCEPTED)
    response = client.post(
        "/api/scan",
        data={
            "profile": "default",
            "title": "Many Tags",
            "tags": _tag_values(_TAGS_CAP),
        },
        headers=HTMX_HEADERS,
    )
    assert response.status_code == 200
    assert len(offered) == 1
    assert offered[0].tags == list(range(1, _TAGS_CAP + 1))


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "json"])
def test_scan_tags_cap_refuses_one_over_without_a_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, *, htmx: bool
) -> None:
    """One tag over the cap is a 422 before any job row exists or is offered."""
    offered = _refuse_submit(client, monkeypatch, SubmitResult.ACCEPTED)
    before = _job_store(client).list_recent(limit=50)
    headers = HTMX_HEADERS if htmx else {}
    response = client.post(
        "/api/scan",
        data={
            "profile": "default",
            "title": "Too Many Tags",
            "tags": _tag_values(_TAGS_CAP + 1),
        },
        headers=headers,
    )
    rejection = RequestRejection.INVALID_REQUEST
    if htmx:
        _assert_scan_refusal(response, rejection, 422)
    else:
        _assert_json_error(response, rejection, 422)
    assert offered == []
    assert _job_store(client).list_recent(limit=50) == before


def test_tag_list_tags_cap_accepts_the_cap(client: TestClient) -> None:
    """The tag list re-renders with exactly the capped number ticked."""
    response = client.get(
        "/api/tags", params={"tags": _tag_values(_TAGS_CAP)}, headers=HTMX_HEADERS
    )
    assert response.status_code == 200


def test_tag_list_tags_cap_refuses_one_over(client: TestClient) -> None:
    """The tag list refuses one ticked tag over the cap before any work."""
    response = client.get(
        "/api/tags",
        params={"tags": _tag_values(_TAGS_CAP + 1)},
        headers=HTMX_HEADERS,
    )
    _assert_htmx_error(response, RequestRejection.INVALID_REQUEST, 422)


def test_tag_refresh_tags_cap_refuses_one_over(client: TestClient) -> None:
    """The tag refresh refuses one ticked tag over the cap before the cache."""
    response = client.post(
        "/api/cache/invalidate",
        params={"resource": "tags"},
        data={"tags": _tag_values(_TAGS_CAP + 1)},
        headers=HTMX_HEADERS,
    )
    _assert_htmx_error(response, RequestRejection.INVALID_REQUEST, 422)


# A paperless-ngx id is a 32-bit auto-increment key: 1 to 2147483647.  Spelled
# out rather than imported, so the tests pin the documented range.
_MAX_ID = 2_147_483_647
_MALFORMED_IDS = ["0", "-5", str(10**22), str(_MAX_ID + 1)]
_MALFORMED_ID_NAMES = ["zero", "negative", "22-digit", "one-over"]


@pytest.mark.parametrize("field", ["tags", "correspondent"])
@pytest.mark.parametrize("value", _MALFORMED_IDS, ids=_MALFORMED_ID_NAMES)
@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "json"])
def test_scan_malformed_id_is_422_without_a_row(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    *,
    htmx: bool,
) -> None:
    """An id no paperless-ngx can have is INVALID_REQUEST before any job row."""
    offered = _refuse_submit(client, monkeypatch, SubmitResult.ACCEPTED)
    before = _job_store(client).list_recent(limit=50)
    headers = HTMX_HEADERS if htmx else {}
    response = client.post(
        "/api/scan",
        data={"profile": "default", "title": "Malformed Id", field: value},
        headers=headers,
    )
    rejection = RequestRejection.INVALID_REQUEST
    if htmx:
        _assert_scan_refusal(response, rejection, 422)
    else:
        _assert_json_error(response, rejection, 422)
    assert offered == []
    assert _job_store(client).list_recent(limit=50) == before


def test_scan_malformed_id_is_neither_echoed_nor_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The refused id stays out of the response and the log; only loc and type."""
    value = "9876543210987654321012"
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/api/scan",
            data={"profile": "default", "title": "Refused", "tags": [value]},
        )
    _assert_json_error(response, RequestRejection.INVALID_REQUEST, 422)
    assert value not in response.text
    assert value not in caplog.text
    assert all(value not in r.getMessage() for r in caplog.records)


def test_scan_malformed_tag_among_good_ones_is_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One malformed id refuses the whole submit; nothing is dropped quietly."""
    offered = _refuse_submit(client, monkeypatch, SubmitResult.ACCEPTED)
    response = client.post(
        "/api/scan",
        data={"profile": "default", "title": "Mixed", "tags": ["3", "0", "7"]},
        headers=HTMX_HEADERS,
    )
    _assert_scan_refusal(response, RequestRejection.INVALID_REQUEST, 422)
    assert offered == []


def test_scan_largest_paperless_id_is_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The largest id a paperless-ngx key can hold is offered unchanged."""
    offered = _refuse_submit(client, monkeypatch, SubmitResult.ACCEPTED)
    response = client.post(
        "/api/scan",
        data={
            "profile": "default",
            "title": "Largest Id",
            "tags": [str(_MAX_ID)],
            "correspondent": str(_MAX_ID),
        },
        headers=HTMX_HEADERS,
    )
    assert response.status_code == 200
    assert len(offered) == 1
    assert offered[0].tags == [_MAX_ID]
    assert offered[0].correspondent == _MAX_ID


@pytest.mark.parametrize("value", _MALFORMED_IDS, ids=_MALFORMED_ID_NAMES)
def test_tag_list_malformed_tag_is_422(client: TestClient, value: str) -> None:
    """The tag list refuses a ticked id no paperless-ngx can have."""
    response = client.get("/api/tags", params={"tags": [value]}, headers=HTMX_HEADERS)
    _assert_htmx_error(response, RequestRejection.INVALID_REQUEST, 422)


@pytest.mark.parametrize("value", _MALFORMED_IDS, ids=_MALFORMED_ID_NAMES)
def test_tag_refresh_malformed_tag_is_422(client: TestClient, value: str) -> None:
    """The tag refresh refuses a ticked id no paperless-ngx can have."""
    response = client.post(
        "/api/cache/invalidate",
        params={"resource": "tags"},
        data={"tags": [value]},
        headers=HTMX_HEADERS,
    )
    _assert_htmx_error(response, RequestRejection.INVALID_REQUEST, 422)


def test_scan_queue_full_is_429_with_a_rejected_row_htmx(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full queue is a visible 429 that reloads history."""
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
    body = _error_body(rejection, 429, _newest_job_id(client))
    assert _slot_of_scan_refusal(response.text) == f"{body}\n{HISTORY_LOADER}"
    _assert_rejected_row(client, QUEUE_FULL_JOB_ERROR)


def test_scan_queue_full_is_429_json(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-htmx submit refused by a full queue is JSON with Retry-After."""
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
    """A submit refused as down or degraded is a 503 with a row."""
    _refuse_submit(client, monkeypatch, result)
    response = client.post(
        "/api/scan",
        data={"profile": "default", "title": "Refused"},
        headers=HTMX_HEADERS,
    )
    assert response.status_code == 503
    assert "Retry-After" not in response.headers
    body = _error_body(rejection, 503, _newest_job_id(client))
    assert _slot_of_scan_refusal(response.text) == f"{body}\n{HISTORY_LOADER}"
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
    """An unhealthy worker refuses the scan without offering it the job."""
    offered = _refuse_submit(client, monkeypatch, SubmitResult.ACCEPTED)
    _force_health(monkeypatch, health)
    response = client.post(
        "/api/scan",
        data={"profile": "default", "title": "Unhealthy"},
        headers=HTMX_HEADERS,
    )
    assert response.status_code == 503
    body = _error_body(rejection, 503, _newest_job_id(client))
    assert _slot_of_scan_refusal(response.text) == f"{body}\n{HISTORY_LOADER}"
    assert offered == []
    _assert_rejected_row(client, error)


@pytest.mark.parametrize(
    ("health", "error"),
    [
        (WorkerHealth.DOWN, WORKER_DOWN_JOB_ERROR),
        (WorkerHealth.DEGRADED, WORKER_DEGRADED_JOB_ERROR),
    ],
    ids=["down", "degraded"],
)
def test_a_refused_submit_never_leaves_an_active_row_when_the_store_fails(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    health: WorkerHealth,
    error: str,
) -> None:
    """
    A refused-before-row submit cannot strand a PENDING row.

    ``finish_job`` failing is the store failing between two statements.  If the
    refusal were recorded as create-then-finish, the create would already have
    committed a PENDING row with no REJECTED marker: nothing reconciles it, so
    the status area would read "Starting scan..." and the Scan button would stay
    disabled for good.  Recorded in one statement, ``finish_job`` is never on
    this path, so the row is terminal and history still reloads.
    """
    offered = _refuse_submit(client, monkeypatch, SubmitResult.ACCEPTED)
    _force_health(monkeypatch, health)
    store = _job_store(client)

    def failing_finish_job(*_args: object, **_kwargs: object) -> None:
        msg = "disk I/O error"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(store, "finish_job", failing_finish_job)
    response = client.post(
        "/api/scan",
        data={"profile": "default", "title": "Refused Mid-Write"},
        headers=HTMX_HEADERS,
    )
    assert response.status_code == 503
    assert offered == []
    assert [job for job in store.list_recent(limit=50) if job.is_active] == []
    _assert_rejected_row(client, error)
    assert _slot_of_scan_refusal(response.text).endswith(HISTORY_LOADER)


def test_scan_degraded_store_failing_is_503_without_a_loader(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A rejection the store cannot record still renders, without a loader.

    The refused-before-row path records the rejection in one statement, so when
    that statement fails no row exists at all -- never a PENDING row.
    """
    offered = _refuse_submit(client, monkeypatch, SubmitResult.ACCEPTED)
    _force_health(monkeypatch, WorkerHealth.DEGRADED)
    store = _job_store(client)
    before = store.list_recent(limit=50)

    def failing_create_rejected_job(*_args: object, **_kwargs: object) -> Job:
        msg = "disk I/O error"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(store, "create_rejected_job", failing_create_rejected_job)
    with caplog.at_level(logging.WARNING, logger="saneless.web.routes"):
        response = client.post(
            "/api/scan",
            data={"profile": "default", "title": "Store Failing"},
            headers=HTMX_HEADERS,
        )
    _assert_scan_refusal(response, RequestRejection.WORKER_DEGRADED, 503)
    assert "/api/jobs/history" not in response.text
    assert offered == []
    assert store.list_recent(limit=50) == before
    warnings = [
        r
        for r in caplog.records
        if r.name == "saneless.web.routes" and r.levelno == logging.WARNING
    ]
    assert warnings
    assert all(r.exc_info is not None for r in warnings)


# How long a rejection owed to the worker may take to land: many fast ticks.
_OWED_REJECTION_BUDGET = 2.0

# The rendered Scan button's opening tag, whatever order its attributes are in.
_SCAN_BUTTON_TAG = re.compile(r'<button\s(?=[^>]*\bid="scan-btn")(?P<attrs>[^>]*)>')


def _fail_first_call(original: Callable[..., object]) -> Callable[..., object]:
    """
    Wrap a job store write so its first call raises and later calls delegate.

    The parameter widens the bound method's type, so the wrapper may forward
    arbitrary arguments to it.

    Returns:
        The wrapper, raising ``sqlite3.OperationalError`` only on call one.

    """
    calls: list[None] = []

    def wrapper(*args: object, **kwargs: object) -> object:
        calls.append(None)
        if len(calls) == 1:
            msg = "disk I/O error"
            raise sqlite3.OperationalError(msg)
        return original(*args, **kwargs)

    return wrapper


@pytest.mark.parametrize(
    ("result", "status", "error"),
    [
        (SubmitResult.QUEUE_FULL, 429, QUEUE_FULL_JOB_ERROR),
        (SubmitResult.DOWN, 503, WORKER_DOWN_JOB_ERROR),
        (SubmitResult.DEGRADED, 503, WORKER_DEGRADED_JOB_ERROR),
    ],
    ids=["queue_full", "down", "degraded"],
)
def test_a_refused_submit_whose_rejection_write_fails_is_recorded_by_the_worker(
    fast_tick_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    result: SubmitResult,
    status: int,
    error: str,
) -> None:
    """
    A post-submit rejection the request could not write still lands.

    The row has to exist before ``submit()``, or the worker could dequeue an id
    with no row, so a refusal from ``submit()`` needs a second write.  When
    that write fails, the request owes it to the worker, whose next idle tick
    records it: the row reaches ERROR/REJECTED and the Scan button re-enables
    with no restart.

    Only ``submit`` is patched, so the ``down`` case keeps a live worker thread
    and proves the owe path.  A truly dead thread never ticks; the next
    startup's recovery ends that row instead.
    """
    _refuse_submit(fast_tick_client, monkeypatch, result)
    store = _job_store(fast_tick_client)
    monkeypatch.setattr(store, "finish_job", _fail_first_call(store.finish_job))

    response = fast_tick_client.post(
        "/api/scan",
        data={"profile": "default", "title": "Refused Then Owed"},
        headers=HTMX_HEADERS,
    )
    assert response.status_code == status
    assert "/api/jobs/history" not in response.text

    def newest_state() -> JobState:
        """Read the state of the most recent job row."""
        return store.list_recent(limit=1)[0].state

    assert poll_until(
        lambda: newest_state() is JobState.ERROR, _OWED_REJECTION_BUDGET
    ), f"row still {newest_state().value} after {_OWED_REJECTION_BUDGET}s"

    _assert_rejected_row(fast_tick_client, error)
    assert store.latest_run_job() is None

    poll = fast_tick_client.get("/api/jobs/current/status").text
    assert "Ready to scan." in poll
    button = _SCAN_BUTTON_TAG.search(poll)
    assert button is not None
    assert "disabled" not in button.group("attrs")


@pytest.mark.parametrize(
    ("result", "status", "error"),
    [
        (SubmitResult.QUEUE_FULL, 429, QUEUE_FULL_JOB_ERROR),
        (SubmitResult.DOWN, 503, WORKER_DOWN_JOB_ERROR),
        (SubmitResult.DEGRADED, 503, WORKER_DEGRADED_JOB_ERROR),
    ],
    ids=["queue_full", "down", "degraded"],
)
def test_a_refused_attempt_whose_rejection_is_still_owed_is_not_shown_as_the_live_job(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    result: SubmitResult,
    status: int,
    error: str,
) -> None:
    """
    A refused attempt waiting on the worker never renders as the live job.

    When the request cannot write a refused submit's REJECTED marker it owes
    the write to the worker, and until an idle tick lands it the row is
    PENDING with no marker.  A rejected submit never takes over the status
    area, so the status lookup skips owed rejections just as it skips written
    ones.  The ``client`` fixture's 5 s idle tick keeps the worker from
    writing the row inside this test, and ``finish_job`` never heals anyway.
    """
    _refuse_submit(client, monkeypatch, result)
    store = _job_store(client)
    worker = _worker(client)

    def always_fail(*_args: object, **_kwargs: object) -> None:
        msg = "disk I/O error"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(store, "finish_job", always_fail)

    response = client.post(
        "/api/scan",
        data={"profile": "default", "title": "Refused And Still Owed"},
        headers=HTMX_HEADERS,
    )
    assert response.status_code == status

    refused = store.list_recent(limit=1)[0]
    assert refused.state is JobState.PENDING
    latest = store.latest_run_job()
    assert latest is not None
    assert latest.id == refused.id

    poll = client.get("/api/jobs/current/status").text
    assert "Ready to scan." in poll, f"refused attempt rendered as live ({error})"
    assert "Starting scan..." not in poll
    button = _SCAN_BUTTON_TAG.search(poll)
    assert button is not None
    assert "disabled" not in button.group("attrs")
    assert refused.id in worker.owed_rejection_ids()


# --- A status poll that cannot read its job backs off in place ---------------

# A valid job id that names no row.  The broken store never gets to look it up,
# and once healed the lookup finds nothing and falls back to the current job.
_FOLLOWED_ID = "5d3f0c9e-7b1a-4c2e-9f4d-2a6b8e1c0d57"

_AREA_TAG = re.compile(r'<div id="status-area"(?P<attrs>[^>]*)>')
_HX_GET = re.compile(r'\bhx-get="(?P<url>[^"]*)"')
_HX_TRIGGER = re.compile(r'\bhx-trigger="(?P<trigger>[^"]*)"')


class _BrokenStore:
    """
    Make the job store's two status reads raise until ``heal`` is called.

    ``get_job`` serves a followed or in-flight job and ``latest_run_job`` the
    current route's fallback, so breaking both covers every status poll.  The
    wrappers delegate once healed, so a test can watch the same page recover.
    """

    def __init__(self, store: JobStore, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrap the store's reads in place; ``monkeypatch`` restores them."""
        self.broken = True
        for name in ("get_job", "latest_run_job"):
            monkeypatch.setattr(store, name, self._guard(getattr(store, name)))

    def _guard(self, original: Callable[..., object]) -> Callable[..., object]:
        """Return ``original``, raising a store error while broken."""

        def guarded(*args: object, **kwargs: object) -> object:
            if self.broken:
                msg = "disk I/O error"
                raise sqlite3.OperationalError(msg)
            return original(*args, **kwargs)

        return guarded

    def heal(self) -> None:
        """Let every later read through to the real store."""
        self.broken = False


@pytest.fixture
def broken_store(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> _BrokenStore:
    """Break the served app's job store reads for the length of a test."""
    return _BrokenStore(_job_store(client), monkeypatch)


def _area(body: str) -> tuple[str, str]:
    """
    Return the status area's poll URL, unescaped, and its trigger.

    Args:
        body: A status poll's response body.

    Returns:
        The URL the area polls next and its ``hx-trigger`` value.

    """
    tag = _AREA_TAG.search(body)
    assert tag is not None, body
    url = _HX_GET.search(tag.group("attrs"))
    trigger = _HX_TRIGGER.search(tag.group("attrs"))
    assert url is not None, body
    assert trigger is not None, body
    return html.unescape(url.group("url")), trigger.group("trigger")


def _assert_fallback(response: httpx2.Response) -> tuple[str, str]:
    """
    Assert a poll answered the lost-contact line inside the status area.

    Args:
        response: The poll's response.

    Returns:
        The fallback's next poll URL and its trigger.

    """
    assert response.status_code == 200, response.text
    assert "HX-Retarget" not in response.headers
    assert "HX-Reswap" not in response.headers
    body = response.text
    assert 'id="status-area"' in body
    assert 'tabindex="-1"' in body
    assert 'class="status-fallback"' in body
    assert f"&#9888; {html.escape(LOST_CONTACT_LINE)}" in body
    for forbidden in ("status-message", "scan-btn", "<title>", "disk I/O error"):
        assert forbidden not in body, forbidden
    return _area(body)


class TestStatusPollBacksOff:
    """
    A status poll that cannot read its job stays inside the status area.

    It answers 200 with a fixed amber line, never an error for the alert slot:
    a poll that failed into ``#status-message`` would re-write the page's one
    alert every second and leave it standing above "Done" once the store
    healed.  The interval steps out to a cap, where the unchanged fallback is
    answered 204, and the poll never stops, so a healed store is picked up.
    """

    @pytest.mark.usefixtures("broken_store")
    def test_status_poll_failure_renders_the_fallback(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The first failing poll renders the line and polls again in 2 s."""
        with caplog.at_level(logging.ERROR, logger="saneless.web.status_view"):
            response = client.get(
                f"/api/jobs/{_FOLLOWED_ID}/status", headers=HTMX_HEADERS
            )
        url, trigger = _assert_fallback(response)
        assert trigger == "every 2s"
        assert url.startswith(f"/api/jobs/{_FOLLOWED_ID}/status?")
        assert "attempt=1" in url
        assert "Failed to render the job status" in caplog.text

    @pytest.mark.usefixtures("broken_store")
    def test_status_poll_backs_off_2_5_15(self, client: TestClient) -> None:
        """Each fallback's own URL steps the interval to 5 s, then 15 s."""
        url, trigger = _assert_fallback(
            client.get(f"/api/jobs/{_FOLLOWED_ID}/status", headers=HTMX_HEADERS)
        )
        triggers = [trigger]
        for expected_attempt in (2, 3):
            url, trigger = _assert_fallback(client.get(url, headers=HTMX_HEADERS))
            assert f"attempt={expected_attempt}" in url
            assert url.startswith(f"/api/jobs/{_FOLLOWED_ID}/status?")
            triggers.append(trigger)
        assert triggers == ["every 2s", "every 5s", "every 15s"]

    @pytest.mark.usefixtures("broken_store")
    def test_status_poll_at_the_cap_answers_204(self, client: TestClient) -> None:
        """At the cap the fallback is unchanged, so its own poll gets no body."""
        url = f"/api/jobs/{_FOLLOWED_ID}/status"
        for _ in range(3):
            url, _trigger = _assert_fallback(client.get(url, headers=HTMX_HEADERS))
        capped = client.get(url, headers=HTMX_HEADERS)
        assert capped.status_code == 204
        assert capped.content == b""
        assert client.get(url, headers=HTMX_HEADERS).status_code == 204

    @pytest.mark.usefixtures("broken_store")
    def test_status_poll_current_route_backs_off(self, client: TestClient) -> None:
        """The current-job poll backs off the same way and keeps its own route."""
        url = "/api/jobs/current/status"
        triggers = []
        for _ in range(3):
            url, trigger = _assert_fallback(client.get(url, headers=HTMX_HEADERS))
            assert url.startswith("/api/jobs/current/status?")
            triggers.append(trigger)
        assert triggers == ["every 2s", "every 5s", "every 15s"]
        assert client.get(url, headers=HTMX_HEADERS).status_code == 204

    @pytest.mark.usefixtures("broken_store")
    @pytest.mark.parametrize(
        "job_id",
        ["not-a-uuid", '"><img src=x onerror=alert(1)>'],
        ids=["not_a_uuid", "markup"],
    )
    def test_status_poll_non_uuid_id_falls_back_to_current(
        self, client: TestClient, job_id: str
    ) -> None:
        """An id the store could not confirm is echoed only if it is a UUID."""
        response = client.get(f"/api/jobs/{job_id}/status", headers=HTMX_HEADERS)
        url, _trigger = _assert_fallback(response)
        assert url.startswith("/api/jobs/current/status?")
        assert "onerror" not in response.text

    @pytest.mark.usefixtures("broken_store")
    def test_status_poll_echoes_the_canonical_uuid(self, client: TestClient) -> None:
        """A UUID in another spelling is echoed in its canonical form."""
        response = client.get(
            f"/api/jobs/{_FOLLOWED_ID.upper()}/status", headers=HTMX_HEADERS
        )
        url, _trigger = _assert_fallback(response)
        assert url.startswith(f"/api/jobs/{_FOLLOWED_ID}/status?")

    def test_status_poll_heals_to_the_real_rendering(
        self, client: TestClient, broken_store: _BrokenStore
    ) -> None:
        """A healed store's next poll is the real rendering, with no attempt."""
        url = f"/api/jobs/{_FOLLOWED_ID}/status"
        for _ in range(3):
            url, _trigger = _assert_fallback(client.get(url, headers=HTMX_HEADERS))
        broken_store.heal()
        response = client.get(url, headers=HTMX_HEADERS)
        assert response.status_code == 200
        assert "HX-Retarget" not in response.headers
        body = response.text
        assert "Ready to scan." in body
        assert 'id="scan-btn"' in body
        assert _AREA_TAG.search(body) is not None
        assert "status-fallback" not in body
        assert "attempt=" not in body

    def test_status_poll_render_failure_is_caught(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A template that fails to render is caught exactly as a store error is."""
        app = client.app
        assert isinstance(app, FastAPI)
        templates = services_of(app).templates
        original = templates.get_template

        class _Exploding:
            """A template whose every render raises."""

            def render(self, *_args: object, **_kwargs: object) -> str:
                """Raise, as a template bug would."""
                msg = "template exploded"
                raise RuntimeError(msg)

        def get_template(name: str) -> object:
            if name == "partials/status_response.html":
                return _Exploding()
            return original(name)

        monkeypatch.setattr(templates, "get_template", get_template)
        for route in ("current", _FOLLOWED_ID):
            response = client.get(f"/api/jobs/{route}/status", headers=HTMX_HEADERS)
            _url, trigger = _assert_fallback(response)
            assert trigger == "every 2s"
            assert "template exploded" not in response.text

    @pytest.mark.usefixtures("broken_store")
    @pytest.mark.parametrize(
        ("attempt", "trigger", "next_attempt"),
        [(-5, "every 2s", 1), (99, "every 15s", 3)],
        ids=["below", "above"],
    )
    def test_status_poll_attempt_is_clamped(
        self, client: TestClient, attempt: int, trigger: str, next_attempt: int
    ) -> None:
        """An out-of-range attempt is clamped, never refused with a 422."""
        response = client.get(
            "/api/jobs/current/status",
            params={"attempt": str(attempt)},
            headers=HTMX_HEADERS,
        )
        url, answered = _assert_fallback(response)
        assert answered == trigger
        assert f"attempt={next_attempt}" in url


# --- The placeholder-token refusal -------------------------------------------

# The shipped stand-in: docker-compose.yml and the docker reference both carry
# it, so it is the placeholder a real installation is most likely to be left
# with.  Named rather than written inline at each call site so the three ways a
# credential can be a placeholder are listed in one place.
_SHIPPED_PLACEHOLDER = "changeme"

# One credential per way a paperless-ngx token can fail ``is_placeholder_token``:
# never set at all, set to whitespace by an edit that looked finished, and left
# at the shipped literal.  ``config.is_placeholder_token`` owns the rule; these
# are the three shapes the route has to refuse through it.
_REFUSED_CREDENTIALS = ["", "   ", _SHIPPED_PLACEHOLDER]
_REFUSED_CREDENTIAL_IDS = ["blank", "whitespace", "shipped_literal"]

# A credential the predicate accepts.  Deliberately not a real-looking 40-char
# hex string: the predicate is a fixed literal set, never a shape heuristic, so
# anything outside the set is a real token as far as it is concerned.
_ACCEPTED_CREDENTIAL = "a-token-nobody-shipped"

# The phrase WORKER_DEGRADED puts on a job row.  This refusal never reuses that
# member -- "the scan service was unavailable" is untrue when the service is
# fine and nobody set the token -- so this path asserts it absent.
_DEGRADED_PHRASE = "the scan service was unavailable"


@contextmanager
def _appliance_with_credential(
    settings: Settings,
    scanner: StubScannerBackend,
    credential: str,
    *,
    consume_dir: str = "",
    url: str | None = None,
) -> Generator[TestClient]:
    """
    Serve one app whose paperless-ngx credential is exactly ``credential``.

    The module's ``app`` fixture is fixed at a real token, and the refusal
    under test is decided from ``Settings``, which is read once at process
    start: there is no runtime setter to monkeypatch, so a second app is the
    only honest way to drive the blocked case.

    Args:
        settings: The module's test settings, copied rather than mutated.
        scanner: The stub backend the served worker drives.
        credential: The paperless-ngx token this appliance is configured with.
        consume_dir: A configured consume directory, when the test needs one.
        url: The paperless-ngx address, or None to keep the module's own.

    Yields:
        A started TestClient over that app.

    """
    configured = settings.model_copy(
        update={
            "paperless": PaperlessConfig(
                url=settings.paperless.url if url is None else url,
                token=credential,
                consume_dir=consume_dir,
            )
        }
    )
    application = create_app(configured, scanner)
    stand_in(services_of(application).paperless, "get_tags", lambda *, timeout=None: [])
    stand_in(
        services_of(application).paperless,
        "get_correspondents",
        lambda *, timeout=None: [],
    )
    with TestClient(application) as tc:
        yield tc


class TestPlaceholderTokenRefusal:
    """
    ``POST /api/scan`` refuses a scan that could never upload.

    The one failure certain to waste paper is a paperless-ngx token nobody
    set: the pages are pulled through the scanner and then have nowhere to go.
    The route guard is the enforcement and the disabled button only a
    courtesy, so every test here drives the route directly, with no button in
    sight, and one of them sends no htmx header at all.
    """

    @pytest.mark.parametrize(
        "credential", _REFUSED_CREDENTIALS, ids=_REFUSED_CREDENTIAL_IDS
    )
    def test_placeholder_token_submit_is_503_with_the_unset_message(
        self,
        web_settings: Settings,
        web_scanner: StubScannerBackend,
        credential: str,
    ) -> None:
        """Every placeholder shape is one 503 carrying the unset-token sentence."""
        with _appliance_with_credential(
            web_settings, web_scanner, credential
        ) as client:
            response = client.post(
                "/api/scan",
                data={"profile": "default", "title": "Unset Token"},
                headers=HTMX_HEADERS,
            )
            assert response.status_code == 503
            assert "Retry-After" not in response.headers
            assert response.headers["HX-Retarget"] == "#status-message"
            body = _error_body(
                RequestRejection.TOKEN_UNSET, 503, _newest_job_id(client)
            )
            assert response.text.strip() == f"{body}\n{HISTORY_LOADER}"

    @pytest.mark.parametrize(
        "credential", _REFUSED_CREDENTIALS, ids=_REFUSED_CREDENTIAL_IDS
    )
    def test_placeholder_token_writes_the_rejected_row(
        self,
        web_settings: Settings,
        web_scanner: StubScannerBackend,
        credential: str,
    ) -> None:
        """The refused attempt is recorded, not silently dropped."""
        with _appliance_with_credential(
            web_settings, web_scanner, credential
        ) as client:
            client.post(
                "/api/scan",
                data={"profile": "default", "title": "Unset Token"},
                headers=HTMX_HEADERS,
            )
            _assert_rejected_row(client, TOKEN_UNSET_JOB_ERROR)

    def test_placeholder_token_refusal_appears_in_job_history(
        self, web_settings: Settings, web_scanner: StubScannerBackend
    ) -> None:
        """
        The attempt is visible in Job History end to end.

        Asserted through the history route rather than the store alone,
        because "history shows the attempt" is a claim about the page a
        household member actually looks at.  A submit refused before its row
        existed records no owner, so the row is nobody's and even the browser
        that sent it sees the generic title; the refusal itself was shown to
        that browser in the submit response.
        """
        with _appliance_with_credential(
            web_settings, web_scanner, _SHIPPED_PLACEHOLDER
        ) as client:
            client.post(
                "/api/scan",
                data={"profile": "default", "title": "Refused In History"},
                headers=HTMX_HEADERS,
            )
            history = client.get("/api/jobs/history").text
            assert HIDDEN_JOB_TITLE in history
            assert "Refused In History" not in history
            assert '<td class="status-error">' in history
            _assert_rejected_row(client, TOKEN_UNSET_JOB_ERROR)

    def test_placeholder_token_refuses_before_any_job_is_offered_to_the_worker(
        self,
        web_settings: Settings,
        web_scanner: StubScannerBackend,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The guard precedes ``create_job``: one row exists, and it is the refusal.

        A guard placed after ``create_job`` would leave a PENDING row behind
        as well as the REJECTED one, and the status area would report a scan
        that never started.
        """
        with _appliance_with_credential(
            web_settings, web_scanner, _SHIPPED_PLACEHOLDER
        ) as client:
            offered = _refuse_submit(client, monkeypatch, SubmitResult.ACCEPTED)
            response = client.post(
                "/api/scan",
                data={"profile": "default", "title": "Never Offered"},
                headers=HTMX_HEADERS,
            )
            assert response.status_code == 503
            assert offered == []
            rows = _job_store(client).list_recent(limit=50)
            assert len(rows) == 1
            assert rows[0].state is JobState.ERROR
            assert rows[0].error_category is ErrorCategory.REJECTED

    def test_placeholder_token_refuses_even_with_a_consume_dir_configured(
        self, web_settings: Settings, web_scanner: StubScannerBackend, tmp_path: Path
    ) -> None:
        """
        A placeholder token refuses the scan even with a consume directory set.

        A configured consume directory is the one thing that could look like a
        reason to let the scan run anyway.  It buys no exception: the
        predicate is the whole condition.
        """
        with _appliance_with_credential(
            web_settings,
            web_scanner,
            _SHIPPED_PLACEHOLDER,
            consume_dir=str(tmp_path),
        ) as client:
            response = client.post(
                "/api/scan",
                data={"profile": "default", "title": "Consume Dir Set"},
                headers=HTMX_HEADERS,
            )
            assert response.status_code == 503
            _assert_rejected_row(client, TOKEN_UNSET_JOB_ERROR)

    def test_placeholder_token_never_says_the_scan_service_was_unavailable(
        self, web_settings: Settings, web_scanner: StubScannerBackend
    ) -> None:
        """
        The unset-token refusal never reuses WORKER_DEGRADED's wording.

        Saying the scan service was unavailable when the service is fine and
        nobody set the token would send a household member looking for a broken
        server.
        """
        with _appliance_with_credential(
            web_settings, web_scanner, _SHIPPED_PLACEHOLDER
        ) as client:
            response = client.post(
                "/api/scan",
                data={"profile": "default", "title": "Not Degraded"},
                headers=HTMX_HEADERS,
            )
            assert _DEGRADED_PHRASE not in response.text
            assert WORKER_DEGRADED_JOB_ERROR not in response.text
            degraded = rejection_message(RequestRejection.WORKER_DEGRADED)
            assert degraded not in response.text
            assert rejection_message(RequestRejection.TOKEN_UNSET) in response.text
            newest = _job_store(client).list_recent(limit=1)[0]
            assert newest.error == TOKEN_UNSET_JOB_ERROR
            assert newest.error != WORKER_DEGRADED_JOB_ERROR

    def test_placeholder_token_refuses_a_post_that_sends_no_htmx_header(
        self, web_settings: Settings, web_scanner: StubScannerBackend
    ) -> None:
        """
        curl, a script, and a browser with ``disabled`` stripped are all refused.

        This request carries no htmx header and never touched a button, so it
        stands in for every client the courtesy cannot reach.
        """
        with _appliance_with_credential(
            web_settings, web_scanner, _SHIPPED_PLACEHOLDER
        ) as client:
            response = client.post(
                "/api/scan", data={"profile": "default", "title": "Raw Post"}
            )
            _assert_json_error(response, RequestRejection.TOKEN_UNSET, 503)
            _assert_rejected_row(client, TOKEN_UNSET_JOB_ERROR)

    def test_placeholder_token_refusal_mints_no_owner_cookie(
        self, web_settings: Settings, web_scanner: StubScannerBackend
    ) -> None:
        """
        A refused submit owns no job, so it is handed no owner token.

        The guard sits ahead of the mint, which is what keeps a browser that
        never started anything from collecting an owner cookie.
        """
        with _appliance_with_credential(
            web_settings, web_scanner, _SHIPPED_PLACEHOLDER
        ) as client:
            response = client.post(
                "/api/scan",
                data={"profile": "default", "title": "No Cookie"},
                headers=HTMX_HEADERS,
            )
            assert OWNER_COOKIE not in response.headers.get("set-cookie", "")

    def test_a_real_token_is_no_placeholder_token_and_still_starts_the_scan(
        self, web_settings: Settings, web_scanner: StubScannerBackend
    ) -> None:
        """A configured appliance is untouched, owner-cookie mint included."""
        with _appliance_with_credential(
            web_settings, web_scanner, _ACCEPTED_CREDENTIAL
        ) as client:
            response = client.post(
                "/api/scan",
                data={"profile": "default", "title": "Real Token"},
                headers=HTMX_HEADERS,
            )
            assert response.status_code == 200
            assert 'id="status-area"' in response.text
            assert OWNER_COOKIE in response.headers.get("set-cookie", "")


# Ten times the refused-row cap, so a store that kept every refusal would hold
# far more than the cap and one that let refusals push out runs would long since
# have lost the finished scan.
_FLOOD_SUBMITS = 200


def test_a_refused_submit_flood_keeps_history_bounded_and_the_finished_scan(
    web_settings: Settings, web_scanner: StubScannerBackend
) -> None:
    """Refused submits through the route are capped and never evict a real scan."""
    with _appliance_with_credential(
        web_settings, web_scanner, _SHIPPED_PLACEHOLDER
    ) as client:
        store = _job_store(client)
        finished = store.create_job("default", "Finished Scan")
        store.finish_job(finished.id, JobState.DONE)
        for index in range(_FLOOD_SUBMITS):
            response = client.post(
                "/api/scan",
                data={"profile": "default", "title": f"Flood {index:03d}"},
                headers=HTMX_HEADERS,
            )
            assert response.status_code == 503
        rows = store.list_recent(limit=_FLOOD_SUBMITS * 2)
        refused = [job for job in rows if job.error_category is ErrorCategory.REJECTED]
        assert len(refused) <= REJECTED_HISTORY_ROWS
        assert refused[0].title == f"Flood {_FLOOD_SUBMITS - 1:03d}"
        kept = store.get_job(finished.id)
        assert kept is not None
        assert kept.state is JobState.DONE


class TestUnsetUrlRefusal:
    """
    ``POST /api/scan`` refuses a scan when ``paperless.url`` is empty.

    An empty address loads, so ``serve`` can start and say what is missing,
    but the upload is certain to fail as a configuration error.  Starting
    the scan anyway would only pull the stack through the feeder for a PDF
    that ends in ``failed/``.
    """

    def test_unset_url_submit_is_503_with_its_own_message(
        self, web_settings: Settings, web_scanner: StubScannerBackend
    ) -> None:
        """The refusal names the address, not the token or the service."""
        with _appliance_with_credential(
            web_settings, web_scanner, _ACCEPTED_CREDENTIAL, url=""
        ) as client:
            response = client.post(
                "/api/scan",
                data={"profile": "default", "title": "Unset Url"},
                headers=HTMX_HEADERS,
            )
            assert response.status_code == 503
            body = _error_body(RequestRejection.URL_UNSET, 503, _newest_job_id(client))
            assert response.text.strip() == f"{body}\n{HISTORY_LOADER}"
            assert rejection_message(RequestRejection.TOKEN_UNSET) not in response.text
            _assert_rejected_row(client, URL_UNSET_JOB_ERROR)

    def test_unset_url_refuses_even_with_a_consume_dir_configured(
        self, web_settings: Settings, web_scanner: StubScannerBackend, tmp_path: Path
    ) -> None:
        """No copy is made for an unset URL, so the folder buys no exception."""
        with _appliance_with_credential(
            web_settings,
            web_scanner,
            _ACCEPTED_CREDENTIAL,
            consume_dir=str(tmp_path),
            url="",
        ) as client:
            response = client.post(
                "/api/scan", data={"profile": "default", "title": "Raw Post"}
            )
            _assert_json_error(response, RequestRejection.URL_UNSET, 503)
            _assert_rejected_row(client, URL_UNSET_JOB_ERROR)

    def test_unset_url_refuses_before_any_job_is_offered_to_the_worker(
        self,
        web_settings: Settings,
        web_scanner: StubScannerBackend,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One row exists, and it is the refusal."""
        with _appliance_with_credential(
            web_settings, web_scanner, _ACCEPTED_CREDENTIAL, url=""
        ) as client:
            offered = _refuse_submit(client, monkeypatch, SubmitResult.ACCEPTED)
            response = client.post(
                "/api/scan",
                data={"profile": "default", "title": "Never Offered"},
                headers=HTMX_HEADERS,
            )
            assert response.status_code == 503
            assert offered == []
            rows = _job_store(client).list_recent(limit=50)
            assert len(rows) == 1
            assert rows[0].error_category is ErrorCategory.REJECTED


# --- The client half: htmx-config meta and the #status-message slot ----------

WEB_DIR = Path(__file__).parent.parent / "src" / "saneless" / "web"
STATUS_MESSAGE_SLOT = '<div id="status-message" role="alert"></div>'
_HTMX_CONFIG_META = re.compile(r"""<meta name="htmx-config"\s+content='([^']*)'>""")
# The slot's inset rule names both of its children, the sentence and the
# "Technical details" disclosure, on one selector list: a disclosure that hung a
# rem to the left of the sentence it explains would read as another element's.
_INSET_SELECTOR = "#status-message > p, #status-message > details"
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_BLOCK = re.compile(r"(?P<selector>[^{}]*)\{(?P<body>[^{}]*)\}")


def _css_rules(css: str) -> list[tuple[str, str]]:
    """
    Return every innermost rule of a stylesheet, with its comments removed.

    Args:
        css: The stylesheet's text.

    Returns:
        Each rule's selector, whitespace normalised, and its body.

    """
    return [
        (" ".join(block.group("selector").split()), block.group("body"))
        for block in _CSS_BLOCK.finditer(_CSS_COMMENT.sub("", css))
    ]


def _declarations(body: str) -> dict[str, str]:
    """Return a rule body's declarations as property names and their values."""
    pairs = [part.split(":", 1) for part in body.split(";") if ":" in part]
    return {name.strip(): value.strip() for name, value in pairs}


def test_status_message_slot_is_one_empty_alert_above_the_status_area(
    client: TestClient,
) -> None:
    """
    The page has one empty alert slot, just above #status-area.

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
    The htmx-config meta swaps error bodies without breaking 2xx swaps.

    htmx 2.0.10 merges meta config shallowly, so a meta holding only the
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
    """The htmx-config meta follows the color-scheme meta and precedes <title>."""
    tags = template_start_tags(WEB_DIR / "templates" / "base.html")
    order = [
        attributes.get("name") if tag == "meta" else tag
        for tag, attributes in tags
        if tag == "title"
        or (tag == "meta" and attributes.get("name") in {"color-scheme", "htmx-config"})
    ]
    assert order == ["color-scheme", "htmx-config", "title"]


def test_status_message_children_are_inset_like_the_status_area() -> None:
    """The slot's message and disclosure line up with the status area."""
    rules = _css_rules((WEB_DIR / "static" / "app.css").read_text(encoding="utf-8"))
    assert [selector for selector, body in rules if "!important" in body] == []
    bodies = [body for selector, body in rules if selector == _INSET_SELECTOR]
    assert len(bodies) == 1
    declarations = _declarations(bodies[0])
    assert declarations.get("padding-left") == "1rem"
    assert declarations.get("border-left") == (
        "var(--pico-border-width) solid transparent"
    )


# --- Focus after a refused Scan ----------------------------------------------

# The Scan button a refused htmx submit carries out-of-band, captured whole,
# whatever order its attributes are in.
_OOB_SCAN_BUTTON = re.compile(
    r'<button(?=[^>]*\sid="scan-btn")(?=[^>]*\shx-swap-oob="true")(?P<attrs>\s[^>]*)>'
)
_AUTOFOCUS = re.compile(r"\sautofocus\b")

# One refused submit per kind of refusal the button can recover from: the
# worker's own refusal, a form the validator throws out, and a request the
# route refuses before it writes anything.
_RECOVERABLE_REFUSALS = {
    "queue_full": (
        {"profile": "default", "title": "Queue Full"},
        RequestRejection.QUEUE_FULL,
    ),
    "title_too_long": (
        {"profile": "default", "title": "x" * (TITLE_MAX_LENGTH + 1)},
        RequestRejection.TITLE_TOO_LONG,
    ),
    "unknown_profile": (
        {"profile": "no-such-profile", "title": "Unknown"},
        RequestRejection.UNKNOWN_PROFILE,
    ),
}


@pytest.mark.parametrize("refusal", list(_RECOVERABLE_REFUSALS))
def test_rejected_scan_returns_focus_to_the_scan_button(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    """
    A refused Scan hands focus back to an enabled Scan button.

    The press disabled the button while the request was in flight, which
    dropped focus to the page body.  The refusal re-renders the button,
    enabled, with ``autofocus``, so a keyboard user can correct and press
    again.  Focus never moves into ``#status-message``: the slot speaks the
    error from where it is.  A request that is not htmx still gets the JSON
    shape and no markup.
    """
    data, rejection = _RECOVERABLE_REFUSALS[refusal]
    _refuse_submit(client, monkeypatch, SubmitResult.QUEUE_FULL)

    response = client.post("/api/scan", data=data, headers=HTMX_HEADERS)

    status = rejection_status_code(rejection)
    assert response.status_code == status
    assert response.headers["HX-Retarget"] == "#status-message"
    assert _error_paragraph(rejection) in response.text
    buttons = _OOB_SCAN_BUTTON.findall(response.text)
    assert len(buttons) == 1, response.text
    assert _AUTOFOCUS.search(buttons[0]) is not None
    assert "disabled" not in buttons[0]
    assert response.text.count('id="scan-btn"') == 1

    plain = client.post("/api/scan", data=data)
    _assert_json_error(plain, rejection, status)


@pytest.mark.parametrize(
    ("credential", "url", "rejection"),
    [
        (_SHIPPED_PLACEHOLDER, None, RequestRejection.TOKEN_UNSET),
        (_ACCEPTED_CREDENTIAL, "", RequestRejection.URL_UNSET),
    ],
    ids=["token_unset", "url_unset"],
)
def test_blocked_refusal_does_not_offer_the_scan_button(
    web_settings: Settings,
    web_scanner: StubScannerBackend,
    credential: str,
    url: str | None,
    rejection: RequestRejection,
) -> None:
    """
    On an appliance that cannot upload, the refusal carries no Scan button.

    The button there is disabled by design, and a disabled button cannot take
    focus, so there is nothing to hand focus back to.
    """
    with _appliance_with_credential(
        web_settings, web_scanner, credential, url=url
    ) as blocked:
        response = blocked.post(
            "/api/scan",
            data={"profile": "default", "title": "Blocked"},
            headers=HTMX_HEADERS,
        )

    assert response.status_code == rejection_status_code(rejection)
    assert _error_paragraph(rejection) in response.text
    assert "scan-btn" not in response.text
    assert _AUTOFOCUS.search(response.text) is None


@pytest.mark.parametrize(
    "refusal", ["title_too_long", "unknown_profile"], ids=lambda name: name
)
def test_refusals_before_the_block_do_not_offer_the_scan_button(
    web_settings: Settings, web_scanner: StubScannerBackend, refusal: str
) -> None:
    """
    A refusal checked before the block still carries no Scan button there.

    Some refusals are decided before the appliance's block is, so a forced
    press on the disabled button meets them instead.  Their button would be
    enabled and focused, overriding the server's own disabled state, so on a
    blocked appliance none is sent, as for the block's own refusal.
    """
    data, rejection = _RECOVERABLE_REFUSALS[refusal]
    with _appliance_with_credential(
        web_settings, web_scanner, _SHIPPED_PLACEHOLDER
    ) as blocked:
        response = blocked.post("/api/scan", data=data, headers=HTMX_HEADERS)

    assert response.status_code == rejection_status_code(rejection)
    assert _error_paragraph(rejection) in response.text
    assert "scan-btn" not in response.text
    assert _AUTOFOCUS.search(response.text) is None


@pytest.mark.parametrize(
    ("method", "path", "data"),
    [
        ("GET", "/api/scan", None),
        ("POST", "/api/flip/abort", {}),
        ("POST", "/api/multi-page/answer", {"job_id": "x", "prompt": "0"}),
        ("POST", f"/_test/reject/{RequestRejection.QUEUE_FULL.value}", None),
    ],
    ids=["scan_get", "flip_abort", "multi_page_answer", "other_route"],
)
def test_other_htmx_errors_carry_no_scan_button(
    client: TestClient, method: str, path: str, data: dict[str, str] | None
) -> None:
    """Only a refused Scan press returns focus to Scan; no other error moves it."""
    response = client.request(method, path, data=data, headers=HTMX_HEADERS)

    assert response.status_code >= 400
    assert response.headers["HX-Retarget"] == "#status-message"
    assert "scan-btn" not in response.text
    assert _AUTOFOCUS.search(response.text) is None
