"""
Tests for the cross-site request guard.

A third-party page open in the operator's browser can submit a form or call
``fetch`` against saneless and start a scan or answer a flip prompt (N-22).
ROBU-10 rejects such requests with a check modelled on Go 1.25's
``net/http.CrossOriginProtection`` (D-20):

1. ``Sec-Fetch-Site`` present: allow ``same-origin`` and ``none``, reject
   everything else, ``same-site`` included.
2. ``Sec-Fetch-Site`` absent, ``Origin`` present: allow only when Origin's
   host[:port] equals ``Host`` or an ``X-Forwarded-Host`` entry (D-21).
3. Both absent: allow, because curl and scripts are not CSRF vectors.

Branch 2 exists because browsers send ``Sec-Fetch-Site`` only to potentially
trustworthy URLs.  saneless's documented deployment is plain HTTP to a LAN IP,
where the header is never sent, so a rule built on it alone would protect
nothing there.  ``TestClient`` sends neither header by default, which makes the
plain-HTTP branch directly testable.

Covers requirement ROBU-10 (decisions D-20, D-21, D-22, D-23).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from markupsafe import escape
from PIL import Image
from starlette.datastructures import Headers

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
from saneless.vocabulary import RequestRejection, rejection_message
from saneless.web.app import create_app
from saneless.web.cross_origin import CrossOriginGuard, is_cross_origin_request

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from fastapi import FastAPI
    from starlette.types import Message, Receive, Scope, Send

LAN_HOST = "192.168.1.5:8080"
LAN_ORIGIN = f"http://{LAN_HOST}"
EVIL_ORIGIN = "http://evil.example"

CROSS_SITE_HEADERS = {"Sec-Fetch-Site": "cross-site"}
HTMX_HEADERS = {"HX-Request": "true"}
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
GUARD_LOGGER = "saneless.web.cross_origin"
KNOWN_UNSAFE_PATHS = frozenset(
    {
        "/api/scan",
        "/api/cache/invalidate",
        "/api/flip/continue",
        "/api/flip/abort",
    }
)
_PATH_PARAM = re.compile(r"\{[^}]+\}")


@pytest.mark.parametrize(
    ("method", "headers", "rejected"),
    [
        pytest.param(
            "GET",
            {"sec-fetch-site": "cross-site", "origin": EVIL_ORIGIN, "host": LAN_HOST},
            False,
            id="safe-get-cross-site-allowed",
        ),
        pytest.param(
            "HEAD",
            {"sec-fetch-site": "cross-site", "origin": EVIL_ORIGIN, "host": LAN_HOST},
            False,
            id="safe-head-cross-site-allowed",
        ),
        pytest.param(
            "OPTIONS",
            {"sec-fetch-site": "cross-site", "origin": EVIL_ORIGIN, "host": LAN_HOST},
            False,
            id="safe-options-cross-site-allowed",
        ),
        pytest.param(
            "POST",
            {"sec-fetch-site": "same-origin"},
            False,
            id="branch1-same-origin-allowed",
        ),
        pytest.param(
            "POST",
            {"sec-fetch-site": "none"},
            False,
            id="branch1-none-allowed",
        ),
        pytest.param(
            "POST",
            {"sec-fetch-site": "same-site"},
            True,
            id="branch1-same-site-rejected",
        ),
        pytest.param(
            "POST",
            {"sec-fetch-site": "cross-site"},
            True,
            id="branch1-cross-site-rejected",
        ),
        pytest.param(
            "POST",
            {"sec-fetch-site": "SAME-ORIGIN"},
            False,
            id="branch1-value-case-insensitive",
        ),
        pytest.param(
            "POST",
            {"sec-fetch-site": "same-origin", "origin": EVIL_ORIGIN, "host": LAN_HOST},
            False,
            id="branch1-decides-over-foreign-origin",
        ),
        pytest.param(
            "POST",
            {"origin": LAN_ORIGIN, "host": LAN_HOST},
            False,
            id="branch2-origin-matches-host-allowed",
        ),
        pytest.param(
            "POST",
            {"origin": EVIL_ORIGIN, "host": LAN_HOST},
            True,
            id="branch2-foreign-origin-rejected",
        ),
        pytest.param(
            "POST",
            {"origin": "http://192.168.1.5:8000", "host": LAN_HOST},
            True,
            id="branch2-same-host-other-port-rejected",
        ),
        pytest.param(
            "POST",
            {"origin": "null", "host": LAN_HOST},
            True,
            id="branch2-null-origin-rejected",
        ),
        pytest.param(
            "POST",
            {
                "origin": "https://scan.example",
                "host": "127.0.0.1:8080",
                "x-forwarded-host": "scan.example",
            },
            False,
            id="branch2-x-forwarded-host-allowed",
        ),
        pytest.param(
            "POST",
            {
                "origin": "https://scan.example",
                "host": "127.0.0.1:8080",
                "x-forwarded-host": "other.example, scan.example",
            },
            False,
            id="branch2-x-forwarded-host-list-allowed",
        ),
        pytest.param(
            "POST",
            {
                "origin": "https://scan.example",
                "host": "127.0.0.1:8080",
                "x-forwarded-host": "other.example",
            },
            True,
            id="branch2-x-forwarded-host-mismatch-rejected",
        ),
        pytest.param(
            "POST",
            {"origin": "http://SCAN.example:8080", "host": "scan.EXAMPLE:8080"},
            False,
            id="branch2-host-case-insensitive",
        ),
        pytest.param(
            "POST",
            {"host": LAN_HOST},
            False,
            id="branch3-no-headers-allowed",
        ),
        pytest.param(
            "PUT",
            {"sec-fetch-site": "cross-site"},
            True,
            id="put-branch1-cross-site-rejected",
        ),
        pytest.param(
            "DELETE",
            {"origin": EVIL_ORIGIN, "host": LAN_HOST},
            True,
            id="delete-branch2-foreign-origin-rejected",
        ),
        pytest.param(
            "PATCH",
            {"origin": LAN_ORIGIN, "host": LAN_HOST},
            False,
            id="patch-branch2-origin-matches-host-allowed",
        ),
        pytest.param(
            "PATCH",
            {"sec-fetch-site": "same-site"},
            True,
            id="patch-branch1-same-site-rejected",
        ),
        pytest.param(
            "DELETE",
            {},
            False,
            id="delete-branch3-no-headers-allowed",
        ),
    ],
)
def test_is_cross_origin_request(
    method: str, headers: dict[str, str], *, rejected: bool
) -> None:
    """The verdict follows the three D-20 branches and D-21's X-Forwarded-Host."""
    assert is_cross_origin_request(method, Headers(headers)) is rejected


# --- The guard wired into the application (D-22, D-23) -----------------------


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


def _new_route() -> dict[str, str]:
    """Stand in for a POST route added after the guard was written."""
    return {"status": "ok"}


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
    """Create the production FastAPI app."""
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


def _unsafe_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Return every (method, path) pair the app serves outside the safe methods."""
    return sorted(
        (method, _PATH_PARAM.sub("x", route.path))
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method not in SAFE_METHODS
    )


def test_every_unsafe_route_rejects_a_cross_site_request(
    app: FastAPI, client: TestClient
) -> None:
    """Every state-changing route is behind the guard, not a per-route check (D-23)."""
    routes = _unsafe_routes(app)
    assert len(routes) >= len(KNOWN_UNSAFE_PATHS)
    assert {path for _, path in routes} >= KNOWN_UNSAFE_PATHS
    for method, path in routes:
        response = client.request(
            method, f"{path}?resource=tags", headers=CROSS_SITE_HEADERS
        )
        assert response.status_code == 403, (method, path)


def test_a_route_added_later_is_also_guarded(app: FastAPI, client: TestClient) -> None:
    """A POST route registered after the app is built cannot forget the check."""
    app.add_api_route("/api/zz-new", _new_route, methods=["POST"])
    assert client.post("/api/zz-new").status_code == 200
    response = client.post("/api/zz-new", headers=CROSS_SITE_HEADERS)
    assert response.status_code == 403


def test_plain_http_branch_through_the_app(client: TestClient) -> None:
    """With no Sec-Fetch-Site, Origin must match Host (D-20 branch 2)."""
    url = "/api/cache/invalidate?resource=tags"
    rejected = client.post(url, headers={"Origin": EVIL_ORIGIN})
    assert rejected.status_code == 403
    allowed = client.post(url, headers={"Origin": "http://testserver"})
    assert allowed.status_code == 200


def test_safe_method_is_never_rejected(client: TestClient) -> None:
    """GET is allowed even when the browser says it is cross-site."""
    assert client.get("/", headers=CROSS_SITE_HEADERS).status_code == 200


def test_htmx_rejection_is_the_cross_site_partial(client: TestClient) -> None:
    """An htmx rejection lands in #status-message with the CROSS_SITE text (D-22)."""
    response = client.post(
        "/api/cache/invalidate?resource=tags",
        headers=CROSS_SITE_HEADERS | HTMX_HEADERS,
    )
    assert response.status_code == 403
    assert response.headers["HX-Retarget"] == "#status-message"
    message = escape(rejection_message(RequestRejection.CROSS_SITE))
    assert response.text.strip() == f'<p class="status-error">&#10007; {message}</p>'


def test_json_rejection_is_the_error_shape(client: TestClient) -> None:
    """A non-htmx rejection is the JSON error shape with the CROSS_SITE text."""
    response = client.post(
        "/api/cache/invalidate?resource=tags", headers=CROSS_SITE_HEADERS
    )
    assert response.status_code == 403
    assert "HX-Retarget" not in response.headers
    assert response.json() == {
        "status": "error",
        "detail": rejection_message(RequestRejection.CROSS_SITE),
    }


def test_rejected_scan_never_reaches_the_route(
    app: FastAPI, client: TestClient
) -> None:
    """A cross-site scan submission writes no job row."""
    response = client.post(
        "/api/scan",
        data={"profile": "default", "title": "cross-site"},
        headers=CROSS_SITE_HEADERS,
    )
    assert response.status_code == 403
    assert app.state.job_store.list_recent(limit=50) == []


def test_rejection_logs_one_warning_naming_every_header(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """One WARNING names method, path and all four headers with %r (D-22)."""
    origin = "http://evil.example\nFAKE LOG LINE"
    forwarded = "proxy.example"
    with caplog.at_level(logging.WARNING, logger=GUARD_LOGGER):
        response = client.post(
            "/api/cache/invalidate?resource=tags",
            headers={
                "Origin": origin,
                "X-Forwarded-Host": forwarded,
                "Sec-Fetch-Site": "cross-site",
            },
        )
    assert response.status_code == 403
    records = [
        r
        for r in caplog.records
        if r.name == GUARD_LOGGER and r.levelno == logging.WARNING
    ]
    assert len(records) == 1
    message = records[0].getMessage()
    for expected in (
        "POST",
        "/api/cache/invalidate",
        f"Origin={origin!r}",
        f"Host={'testserver'!r}",
        f"X-Forwarded-Host={forwarded!r}",
        f"Sec-Fetch-Site={'cross-site'!r}",
    ):
        assert expected in message
    assert "\n" not in message


async def _never_called(scope: Scope, receive: Receive, send: Send) -> None:
    """Fail if the guard passes a rejected request on to the application."""
    _ = (scope, receive, send)
    pytest.fail("a rejected request reached the application")


def test_rejection_log_escapes_control_characters_in_the_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Control characters in the decoded path reach the log line escaped.

    uvicorn percent-decodes ``%0A`` and ``%1B`` into ``scope["path"]``, but
    httpx strips control characters from a URL, so the scope is driven into
    the guard directly rather than through ``TestClient``.  The standard
    library's URL parser drops the newline; ``%r`` escapes the rest, so a
    terminal escape sequence cannot rewrite what an operator reads.
    """
    path = "/api/zz\x1b[2J\nFAKE LOG LINE"
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "server": ("testserver", 80),
        "headers": [(b"host", b"testserver"), (b"sec-fetch-site", b"cross-site")],
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    guard = CrossOriginGuard(_never_called)
    with caplog.at_level(logging.WARNING, logger=GUARD_LOGGER):
        asyncio.run(guard(scope, receive, send))
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 403
    records = [r for r in caplog.records if r.name == GUARD_LOGGER]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "\\x1b[2J" in message
    assert "\x1b" not in message
    assert "\n" not in message
