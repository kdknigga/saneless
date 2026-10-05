"""
Every request is refused unless its Host header names saneless.

A page on any website can rebind its own name to saneless's LAN address.  The
browser then treats saneless as same-origin with that page, so the page can read
previews, titles and tag names and start scans.  What the browser cannot change
is the ``Host`` header: it still carries the hostile page's name.  So every
request, of every method, is refused unless ``Host`` names saneless.

The zero-configuration trusted set is IP literals, ``localhost``, single-label
names and names under ``.local``, ``.home.arpa``, ``.internal`` and ``.lan``.
``[web] allowed_hosts`` adds to it and never replaces it.  A well-formed
untrusted Host gets 421 with a sentence naming the key to set; a missing,
empty, duplicated or malformed Host gets the generic 400.
"""

from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from saneless.config import ProfileConfig, Settings, WebConfig
from saneless.text_safety import has_control_characters
from saneless.vocabulary import RequestRejection, rejection_message
from saneless.web import host_guard as host_guard_module
from saneless.web.app import create_app
from saneless.web.errors import TechnicalDetails, render_error
from saneless.web.host_guard import (
    DEFAULT_TRUSTED_SUFFIXES,
    HostGuard,
    HostVerdict,
    host_verdict,
)
from tests.conftest import StubScannerBackend, services_of, stand_in

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi import FastAPI
    from starlette.types import Message, Receive, Scope, Send


EVIL_HOST = "evil.example"
LAN_HOST = "192.168.1.5:8080"
GUARD_LOGGER = "saneless.web.host_guard"
HTMX_HEADERS = {"HX-Request": "true"}
ECHO_LIMIT = 255

_HOST_SENTENCE = rejection_message(RequestRejection.HOST_NOT_ALLOWED)
_CLIENT_ERROR_JSON = {
    "status": "error",
    "detail": rejection_message(RequestRejection.CLIENT_ERROR),
}


# --- The verdict, as a pure function -------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(["192.168.1.5:8080"], id="ipv4-with-port"),
        pytest.param(["[::1]:8080"], id="bracketed-ipv6-with-port"),
        pytest.param(["[fe80::1]"], id="bracketed-ipv6"),
        pytest.param(["LOCALHOST"], id="localhost-upper-case"),
        pytest.param(["localhost:8080"], id="localhost-with-port"),
        pytest.param(["localhost:"], id="localhost-with-empty-port"),
        pytest.param(["[::1]:"], id="bracketed-ipv6-with-empty-port"),
        pytest.param(["x:"], id="single-label-with-empty-port"),
        pytest.param(["scanner"], id="single-label"),
        pytest.param(["scanner."], id="single-label-trailing-dot"),
        pytest.param(["saneless_web:8080"], id="compose-service-name"),
        pytest.param(["SANELESS.local.:8080"], id="dot-local-mixed-case"),
        pytest.param(["a.home.arpa"], id="home-arpa"),
        pytest.param(["n.internal"], id="internal"),
        pytest.param(["p.lan"], id="lan"),
        pytest.param(["box.local"], id="local"),
        # TestClient's default Host.  Every TestClient suite in this repository
        # relies on it being trusted, so the single-label rule must stay.
        pytest.param(["testserver"], id="testclient-default-host"),
    ],
)
def test_the_zero_configuration_set_is_trusted(values: list[str]) -> None:
    """IP literals, localhost, single labels and the private suffixes pass."""
    assert host_verdict(values, ()) is HostVerdict.TRUSTED


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(["evil.example"], id="foreign-name"),
        pytest.param(["evil.example:8080"], id="foreign-name-with-port"),
        pytest.param(["evil.example:"], id="foreign-name-with-empty-port"),
        pytest.param(["localhost.evil.example"], id="localhost-prefix"),
        pytest.param(["127.0.0.1.nip.io"], id="ip-shaped-prefix"),
        pytest.param(["scan.example.com"], id="not-yet-allowed"),
        pytest.param(["local.evil.example"], id="suffix-word-as-prefix"),
        pytest.param(["box.xlan"], id="suffix-without-its-dot"),
    ],
)
def test_a_foreign_name_is_untrusted(values: list[str]) -> None:
    """A dotted name outside the defaults and the allow-list is refused."""
    assert host_verdict(values, ()) is HostVerdict.UNTRUSTED


@pytest.mark.parametrize(
    "values",
    [
        pytest.param([], id="missing"),
        pytest.param([""], id="empty"),
        pytest.param(["a b@c"], id="space-and-at"),
        pytest.param(["::1"], id="bare-ipv6"),
        pytest.param(["x", "y"], id="two-host-headers"),
        pytest.param(["[::1"], id="unclosed-bracket"),
        pytest.param(["[1.2.3.4]"], id="bracketed-ipv4"),
        pytest.param(["[zz::1]"], id="bracketed-non-address"),
        pytest.param(["."], id="only-a-dot"),
        pytest.param(["a..b"], id="empty-label"),
        pytest.param([".example"], id="leading-dot"),
        pytest.param(["<b>x</b>.example"], id="markup"),
        pytest.param(["evil.example\x85"], id="c1-control"),
        pytest.param(["user@evil.example"], id="userinfo"),
    ],
)
def test_a_missing_or_malformed_host_is_malformed(values: list[str]) -> None:
    """Anything that is not exactly one well-formed Host is malformed."""
    assert host_verdict(values, ()) is HostVerdict.MALFORMED


_ALLOWED = ("scan.example.com", ".home.example")


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        pytest.param("scan.example.com", HostVerdict.TRUSTED, id="exact"),
        pytest.param("SCAN.example.com.:443", HostVerdict.TRUSTED, id="exact-normal"),
        pytest.param("www.scan.example.com", HostVerdict.UNTRUSTED, id="exact-no-sub"),
        pytest.param("example.com", HostVerdict.UNTRUSTED, id="exact-no-parent"),
        pytest.param("home.example", HostVerdict.TRUSTED, id="suffix-bare-domain"),
        pytest.param("a.b.home.example", HostVerdict.TRUSTED, id="suffix-deep"),
        pytest.param("xhome.example", HostVerdict.UNTRUSTED, id="suffix-not-a-label"),
        pytest.param("192.168.1.5", HostVerdict.TRUSTED, id="defaults-kept-ip"),
        pytest.param("box.local", HostVerdict.TRUSTED, id="defaults-kept-suffix"),
        pytest.param(EVIL_HOST, HostVerdict.UNTRUSTED, id="still-foreign"),
    ],
)
def test_allowed_hosts_adds_names_and_suffixes(
    host: str, expected: HostVerdict
) -> None:
    """Exact names match only themselves; a .suffix also matches its bare domain."""
    assert host_verdict([host], _ALLOWED) is expected


def test_the_default_suffixes_are_the_four_private_ones() -> None:
    """The trusted suffixes are exactly the four names no public DNS answers."""
    assert set(DEFAULT_TRUSTED_SUFFIXES) == {
        ".local",
        ".home.arpa",
        ".internal",
        ".lan",
    }


# --- The guard wired into the application --------------------------------------


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
def client(web_settings: Settings) -> Iterator[TestClient]:
    """TestClient over the production app, with its lifespan running."""
    app = create_app(web_settings, StubScannerBackend())
    stand_in(
        services_of(app).paperless,
        "get_tags",
        lambda *, timeout=None: [{"id": 1, "name": "receipt"}],
    )
    stand_in(
        services_of(app).paperless,
        "get_correspondents",
        lambda *, timeout=None: [{"id": 1, "name": "ACME"}],
    )
    with TestClient(app) as tc:
        yield tc


def _foreign(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return request headers naming the hostile Host, plus ``extra``."""
    return {"Host": EVIL_HOST, **(extra or {})}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("GET", "/api/jobs/current/status", id="status-poll"),
        pytest.param("GET", "/health", id="health"),
        pytest.param("GET", "/static/app.css", id="static-file"),
        pytest.param("GET", "/", id="index"),
        pytest.param("HEAD", "/", id="head"),
        pytest.param("POST", "/api/scan", id="post"),
    ],
)
def test_every_method_and_path_is_refused_for_a_foreign_host(
    client: TestClient, method: str, path: str
) -> None:
    """Reads are refused as well as writes: rebinding reads as easily as it scans."""
    response = client.request(method, path, headers=_foreign())
    assert response.status_code == 421


def test_the_host_check_runs_before_the_cross_site_check(client: TestClient) -> None:
    """A cross-site POST with a foreign Host is answered by the Host check."""
    response = client.post(
        "/api/scan", headers=_foreign({"Sec-Fetch-Site": "cross-site"})
    )
    assert response.status_code == 421


def test_x_forwarded_host_never_rescues_a_foreign_host(client: TestClient) -> None:
    """Only Host is read; a trusted X-Forwarded-Host changes nothing."""
    response = client.get(
        "/health", headers=_foreign({"X-Forwarded-Host": "127.0.0.1"})
    )
    assert response.status_code == 421


PROXY_UPSTREAM_HOST = "saneless:8080"
PUBLIC_NAME = "scan.example.com"


def _rewrite_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the guard's records about a proxy that replaced the Host."""
    return [
        r
        for r in caplog.records
        if r.name == GUARD_LOGGER and "X-Forwarded-Host" in r.getMessage()
    ]


def test_a_proxy_that_rewrites_host_is_logged_once(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A trusted Host beside an X-Forwarded-Host naming another host warns once.

    Two requests in a row, inside one report interval, log one line between
    them.  nginx without ``proxy_set_header Host`` sends its upstream's name, a
    single label saneless always answers to, and puts the browser's name in
    X-Forwarded-Host.  Every request through it passes the Host check, so the
    check is off for them.  The request is still answered: the header is
    never trusted, only reported.
    """
    headers = {"Host": PROXY_UPSTREAM_HOST, "X-Forwarded-Host": PUBLIC_NAME}
    with caplog.at_level(logging.WARNING, logger=GUARD_LOGGER):
        first = client.get("/health", headers=headers)
        second = client.get("/health", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    records = _rewrite_warnings(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    message = records[0].getMessage()
    assert f"{PROXY_UPSTREAM_HOST!r}" in message
    assert f"{PUBLIC_NAME!r}" in message
    assert "original Host" in message


def test_the_proxy_report_is_made_again_after_its_interval(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The report is limited per interval, not per process.

    Any client that can reach the port can send a made-up X-Forwarded-Host
    and spend a once-only report, and a real misconfigured proxy added later
    would then never be reported until a restart.  With the interval at
    zero, every such request is reported.
    """
    monkeypatch.setattr(
        host_guard_module, "REPLACED_HOST_REPORT_SECONDS", 0.0, raising=False
    )
    headers = {"Host": PROXY_UPSTREAM_HOST, "X-Forwarded-Host": PUBLIC_NAME}
    with caplog.at_level(logging.WARNING, logger=GUARD_LOGGER):
        client.get("/health", headers=headers)
        client.get("/health", headers=headers)
    assert len(_rewrite_warnings(caplog)) == 2


def test_the_proxy_report_is_worded_as_an_observation(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The report says what arrived and what it means only if there is a proxy."""
    headers = {"Host": PROXY_UPSTREAM_HOST, "X-Forwarded-Host": PUBLIC_NAME}
    with caplog.at_level(logging.WARNING, logger=GUARD_LOGGER):
        client.get("/health", headers=headers)
    [record] = _rewrite_warnings(caplog)
    assert "If saneless is behind a reverse proxy" in record.getMessage()


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"Host": PROXY_UPSTREAM_HOST}, id="no-forwarded-host"),
        pytest.param(
            {"Host": PROXY_UPSTREAM_HOST, "X-Forwarded-Host": "SANELESS"},
            id="forwarded-host-names-the-same-host",
        ),
        pytest.param(
            {"Host": "box.local:8080", "X-Forwarded-Host": "box.local, proxy.lan"},
            id="first-forwarded-entry-matches",
        ),
        pytest.param(
            {"Host": PROXY_UPSTREAM_HOST, "X-Forwarded-Host": "<b>"},
            id="malformed-forwarded-host",
        ),
    ],
)
def test_a_proxy_that_keeps_host_is_not_reported(
    client: TestClient, caplog: pytest.LogCaptureFixture, headers: dict[str, str]
) -> None:
    """Only a forwarded name that differs from Host is reported as a replaced Host."""
    with caplog.at_level(logging.WARNING, logger=GUARD_LOGGER):
        response = client.get("/health", headers=headers)
    assert response.status_code == 200
    assert _rewrite_warnings(caplog) == []


def test_a_lan_address_with_a_port_is_answered(client: TestClient) -> None:
    """The address the documented deployment is reached at keeps working."""
    response = client.get("/health", headers={"Host": LAN_HOST})
    assert response.status_code == 200


def test_an_allowed_host_is_answered(make_settings: Callable[..., Settings]) -> None:
    """A name added to ``[web] allowed_hosts`` is answered; others still are not."""
    settings = make_settings(
        web=WebConfig.model_validate({"allowed_hosts": ["scan.example.com"]})
    )
    with TestClient(create_app(settings, StubScannerBackend())) as tc:
        assert tc.get("/health", headers={"Host": "scan.example.com"}).status_code == (
            200
        )
        assert tc.get("/health", headers={"Host": LAN_HOST}).status_code == 200
        assert tc.get("/health", headers=_foreign()).status_code == 421


def test_the_htmx_refusal_names_the_key_and_the_host(client: TestClient) -> None:
    """The fragment carries the fixed sentence and the Host in Technical details."""
    response = client.get("/api/jobs/current/status", headers=_foreign(HTMX_HEADERS))
    assert response.status_code == 421
    assert _HOST_SENTENCE in response.text
    assert "[web] allowed_hosts" in _HOST_SENTENCE
    details = response.text.split('<details class="tech-details">', 1)[1]
    assert f"<p>Host: {EVIL_HOST}</p>" in details.split("</details>", 1)[0]


def test_the_json_refusal_carries_the_host_beside_the_sentence(
    client: TestClient,
) -> None:
    """The echoed Host is its own field, so the sentence stays fixed."""
    response = client.get("/api/jobs/current/status", headers=_foreign())
    assert response.status_code == 421
    assert response.json() == {
        "status": "error",
        "detail": _HOST_SENTENCE,
        "host": EVIL_HOST,
    }


def test_the_refusal_is_logged_once_with_the_host_quoted(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """One WARNING names the Host with repr quotes and the key that fixes it."""
    with caplog.at_level(logging.WARNING, logger=GUARD_LOGGER):
        client.get("/health", headers=_foreign())
    records = [r for r in caplog.records if r.name == GUARD_LOGGER]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    message = records[0].getMessage()
    assert f"{EVIL_HOST!r}" in message
    assert "allowed_hosts" in message


def test_a_markup_host_is_refused_without_an_echo(client: TestClient) -> None:
    """A Host no DNS name can spell is a 400, and none of it reaches the body."""
    response = client.get(
        "/api/jobs/current/status",
        headers={"Host": "<b>x</b>.example", **HTMX_HEADERS},
    )
    assert response.status_code == 400
    assert "<b>" not in response.text
    assert "x</b>" not in response.text


def test_an_echoed_host_is_escaped_in_the_fragment(client: TestClient) -> None:
    """
    The Technical-details slot autoescapes whatever Host it is handed.

    The guard only ever echoes a well-formed name, so markup cannot reach the
    slot through it; this drives ``render_error`` directly to show the template
    would still escape it.
    """
    app = client.app
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "root_path": "",
            "query_string": b"",
            "server": ("testserver", 80),
            "headers": [(b"host", b"testserver"), (b"hx-request", b"true")],
            "app": app,
        }
    )
    rejection = RequestRejection.HOST_NOT_ALLOWED
    response = render_error(
        request,
        rejection,
        status_code=421,
        details=TechnicalDetails(echoed_host="<b>x</b>.example"),
    )
    body = bytes(response.body).decode()
    assert "<b>x</b>" not in body
    assert "<p>Host: &lt;b&gt;x&lt;/b&gt;.example</p>" in body


def test_a_long_host_is_echoed_bounded(client: TestClient) -> None:
    """A well-formed but huge Host is cut to 255 characters in the body and log."""
    host = "a" * 400 + ".example"
    response = client.get("/health", headers={"Host": host})
    assert response.status_code == 421
    echoed = response.json()["host"]
    assert len(echoed) <= ECHO_LIMIT
    assert echoed.startswith("a" * 200)


# --- Hosts TestClient cannot send ---------------------------------------------


async def _never_called(scope: Scope, receive: Receive, send: Send) -> None:
    """Fail if the guard passes a refused request on to the application."""
    _ = (scope, receive, send)
    pytest.fail("a refused request reached the application")


def _drive(headers: list[tuple[bytes, bytes]]) -> tuple[int, bytes]:
    """
    Send one raw GET through a bare ``HostGuard`` and return status and body.

    httpx2 will not send a missing, duplicated or non-ASCII Host, so the scope is
    built directly, as the cross-origin guard's test does.

    Args:
        headers: The raw ASGI header list.

    Returns:
        The response's status code and body bytes.

    """
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "path": "/health",
        "raw_path": b"/health",
        "root_path": "",
        "query_string": b"",
        "server": ("testserver", 80),
        "headers": headers,
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    guard = HostGuard(_never_called)
    # On its own thread: a Playwright session earlier in the same run can leave
    # an event loop running on this one, where asyncio.run refuses.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(lambda: asyncio.run(guard(scope, receive, send))).result()
    assert sent[0]["type"] == "http.response.start"
    body = b"".join(m.get("body", b"") for m in sent[1:])
    return sent[0]["status"], body


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([], id="no-host-header"),
        pytest.param([(b"host", b"")], id="empty-host"),
        pytest.param([(b"host", b"testserver"), (b"host", b"x")], id="two-hosts"),
    ],
)
def test_a_missing_empty_or_duplicated_host_gets_400(
    headers: list[tuple[bytes, bytes]],
) -> None:
    """The generic 400 answers a request with no single usable Host."""
    status, body = _drive(headers)
    assert status == 400
    assert json.loads(body) == _CLIENT_ERROR_JSON


def test_a_control_byte_host_is_refused_without_a_raw_control_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Host carrying byte 0x85 (a C1 control in latin-1) is refused cleanly."""
    host = b"evil.example\x85" + b"a" * 400
    with caplog.at_level(logging.WARNING, logger=GUARD_LOGGER):
        status, body = _drive([(b"host", host)])
    assert status == 400
    assert b"evil.example" not in body
    records = [r for r in caplog.records if r.name == GUARD_LOGGER]
    assert len(records) == 1
    assert not has_control_characters(records[0].getMessage())


def test_the_guard_passes_a_trusted_host_through() -> None:
    """A trusted Host reaches the wrapped application untouched."""
    reached: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        _ = (receive, send)
        reached.append(scope)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        _ = message

    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"host", b"192.168.1.5:8080")],
    }
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(lambda: asyncio.run(HostGuard(app)(scope, receive, send))).result()
    assert reached == [scope]


def test_the_app_installs_the_guard(web_settings: Settings) -> None:
    """``create_app`` wraps the application in ``HostGuard``."""
    app: FastAPI = create_app(web_settings, StubScannerBackend())
    with TestClient(app):
        assert any(m.cls is HostGuard for m in app.user_middleware)
