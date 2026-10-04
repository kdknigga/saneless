"""
Every response carries the security headers, each exactly once.

A page on another site can frame saneless and steer a real click onto Scan or
a flip answer; the click is the user's own, so the cross-site check cannot see
anything wrong.  The Content-Security-Policy refuses the frame, and with it any
inline script, inline style or foreign resource a future injection could use.
``X-Frame-Options`` says the same to browsers too old for ``frame-ancestors``,
and ``nosniff`` stops a browser guessing a content type the server did not
send.

The headers must be on every response, not only on pages that render: an error
page is a page too, and a 500 is sent by Starlette's outermost middleware,
past every middleware the application adds.  So the statuses are enumerated
here, the 500 included.

The htmx configuration is part of the same policy.  htmx would otherwise inject
a ``<style>`` for its request indicator, which the policy blocks, and its eval
and script-tag paths are exactly what an injection would reach for.
"""

from __future__ import annotations

import html
import json
import re
from typing import TYPE_CHECKING, NamedTuple

import pytest
from fastapi.testclient import TestClient

from saneless.config import ProfileConfig, Settings
from saneless.vocabulary import TITLE_MAX_LENGTH
from saneless.web.app import create_app
from tests.conftest import StubScannerBackend, services_of, stand_in

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi import FastAPI


# Written out here rather than imported, so a change to the policy in the
# source has to be made twice and cannot pass unnoticed.
_POLICY = (
    "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)
_EXPECTED_HEADERS = (
    ("content-security-policy", _POLICY),
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
)

_BOOM_PATH = "/_test/boom"
_CROSS_SITE = {"Sec-Fetch-Site": "cross-site"}
_HTMX = {"HX-Request": "true"}

# The responseHandling array base.html carries.  htmx 2.0.10 merges the meta
# configuration shallowly, so the security switches sit beside this array,
# restated in full, rather than replace it.
_RESPONSE_HANDLING = [
    {"code": "204", "swap": False},
    {"code": "[23]..", "swap": True},
    {"code": "[45]..", "swap": True, "error": True},
]
_HTMX_CONFIG_META = re.compile(
    r"<meta\s+name=\"htmx-config\"\s+content='(?P<content>[^']*)'\s*>"
)


def _boom() -> None:
    """Stand in for a route that fails with an exception nobody handles."""
    msg = "boom"
    raise RuntimeError(msg)


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
def app(web_settings: Settings) -> FastAPI:
    """
    Build the production app, plus one route that raises, added after the build.

    The route stands in for any future handler that fails unexpectedly: the
    500 it causes is the response most likely to miss a header.
    """
    application = create_app(web_settings, StubScannerBackend())
    stand_in(
        services_of(application).paperless,
        "get_tags",
        lambda *, timeout=None: [{"id": 1, "name": "receipt"}],
    )
    stand_in(
        services_of(application).paperless,
        "get_correspondents",
        lambda *, timeout=None: [{"id": 1, "name": "ACME"}],
    )
    application.add_api_route(_BOOM_PATH, _boom)
    return application


@pytest.fixture
def lenient_client(app: FastAPI) -> Iterator[TestClient]:
    """
    TestClient that returns 500 responses instead of re-raising.

    ServerErrorMiddleware re-raises after sending the catch-all handler's
    response, and the default TestClient would re-raise that into the test.
    """
    with TestClient(app, raise_server_exceptions=False) as tc:
        yield tc


class _Probe(NamedTuple):
    """One request, and the status it must be answered with."""

    method: str
    path: str
    status: int
    headers: dict[str, str] | None = None
    data: dict[str, str] | None = None


@pytest.mark.parametrize(
    "probe",
    [
        pytest.param(_Probe("GET", "/", 200), id="index-200"),
        pytest.param(_Probe("GET", "/static/app.css", 200), id="static-file-200"),
        pytest.param(
            _Probe("GET", "/health", 400, {"Host": "<b>x</b>.example"}),
            id="bad-host-400",
        ),
        pytest.param(
            _Probe("POST", "/api/scan", 403, _CROSS_SITE), id="cross-site-403"
        ),
        pytest.param(_Probe("GET", "/nope", 404), id="not-found-404"),
        pytest.param(_Probe("GET", "/nope", 404, _HTMX), id="not-found-htmx-404"),
        pytest.param(_Probe("DELETE", "/", 405), id="method-405"),
        pytest.param(
            _Probe("GET", "/health", 421, {"Host": "evil.example"}),
            id="foreign-host-421",
        ),
        pytest.param(
            _Probe(
                "POST", "/api/scan", 422, data={"title": "x" * (TITLE_MAX_LENGTH + 1)}
            ),
            id="invalid-form-422",
        ),
        pytest.param(_Probe("GET", _BOOM_PATH, 500), id="unhandled-500"),
        pytest.param(_Probe("GET", _BOOM_PATH, 500, _HTMX), id="unhandled-htmx-500"),
    ],
)
def test_every_response_carries_the_security_headers_once(
    lenient_client: TestClient, probe: _Probe
) -> None:
    """Each header is present with its exact value, and only once, on every status."""
    response = lenient_client.request(
        probe.method, probe.path, headers=probe.headers, data=probe.data
    )
    assert response.status_code == probe.status
    for name, value in _EXPECTED_HEADERS:
        assert response.headers.get_list(name) == [value], name


@pytest.mark.parametrize(
    "probe",
    [
        pytest.param(_Probe("GET", "/", 200), id="index"),
        pytest.param(
            _Probe("GET", "/api/jobs/current/status", 200), id="current-status"
        ),
        pytest.param(_Probe("GET", "/api/jobs/current/status", 200, _HTMX), id="poll"),
        pytest.param(_Probe("GET", "/api/jobs/history", 200, _HTMX), id="history"),
        pytest.param(_Probe("GET", "/health", 200), id="health"),
        pytest.param(
            _Probe("GET", "/health", 421, {"Host": "evil.example"}),
            id="foreign-host-421",
        ),
        pytest.param(_Probe("GET", "/nope", 404), id="not-found-404"),
        pytest.param(_Probe("GET", _BOOM_PATH, 500), id="unhandled-500"),
    ],
)
def test_every_dynamic_response_is_not_stored(
    lenient_client: TestClient, probe: _Probe
) -> None:
    """
    No dynamic response may be kept by a cache, the owner-only views included.

    The page, the status poll and the history show the browser that started
    a job its real title and thumbnail, and everyone else a generic title.
    A caching proxy that stored the owner's copy could hand it to anyone.
    """
    response = lenient_client.request(
        probe.method, probe.path, headers=probe.headers, data=probe.data
    )
    assert response.status_code == probe.status
    assert response.headers.get_list("cache-control") == ["no-store"]


def test_a_job_status_response_is_not_stored(
    lenient_client: TestClient, app: FastAPI
) -> None:
    """The status of one job, the owner-gated view a poll fetches, is no-store."""
    job = services_of(app).job_store.create_job("default", "Private Title")
    response = lenient_client.get(f"/api/jobs/{job.id}/status", headers=_HTMX)
    assert response.status_code == 200
    assert response.headers.get_list("cache-control") == ["no-store"]


def test_static_files_stay_cacheable(lenient_client: TestClient) -> None:
    """The vendored scripts and stylesheets hold nothing private, so no no-store."""
    response = lenient_client.get("/static/app.css")
    assert response.status_code == 200
    assert "no-store" not in response.headers.get("cache-control", "")


def test_the_htmx_config_turns_off_eval_script_tags_and_indicator_styles(
    lenient_client: TestClient,
) -> None:
    """The meta keeps the response handling and adds the three switches, all off."""
    response = lenient_client.get("/")
    match = _HTMX_CONFIG_META.search(response.text)
    assert match is not None, "base.html has no htmx-config meta"
    config = json.loads(html.unescape(match.group("content")))
    assert config.get("includeIndicatorStyles") is False
    assert config.get("allowEval") is False
    assert config.get("allowScriptTags") is False
    assert config.get("responseHandling") == _RESPONSE_HANDLING
