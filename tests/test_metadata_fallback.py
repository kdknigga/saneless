"""
What the tag and correspondent lists show while Paperless cannot be reached.

The app is built with its real, offline Paperless client, so a fetch fails the
way a refused connection does, and with a metadata cache whose clock the test
moves.  Two cases matter to the operator:

* the lists were fetched once and Paperless then went away: the page keeps
  showing the last good lists, and the log says why once per TTL;
* the lists were never fetched: the page renders them empty, and the log
  now says why.

Nothing in this file sleeps.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saneless.web import app as app_module
from saneless.web.app import create_app
from saneless.web.cache import MetadataCache
from tests.conftest import StubScannerBackend

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from saneless.config import Settings

pytestmark = pytest.mark.usefixtures("offline_paperless")

_TTL_SECONDS = 60
# What the offline client's fetch failure starts with; the rest names the URL
# and the refused connection.
_CAUSE = "Could not fetch tags from Paperless at"


class _FakeClock:
    """A monotonic clock the test moves by hand instead of waiting for."""

    def __init__(self, start: float = 100.0) -> None:
        """Start the clock at ``start`` seconds."""
        self.now = start

    def __call__(self) -> float:
        """Return the current fake monotonic reading."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward, the way a real one would move on its own."""
        self.now += seconds


type _Clocked = tuple[TestClient, _FakeClock]


def _app(client: TestClient) -> FastAPI:
    """Return the client's app, checked so the type checkers know it."""
    app = client.app
    assert isinstance(app, FastAPI)
    return app


@pytest.fixture
def clocked(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> Iterator[_Clocked]:
    """
    Build a started client whose metadata cache runs on a movable clock.

    The cache is substituted where the app builds it, so the routes use the
    one instance the app made, with the TTL the settings give it.
    """
    clock = _FakeClock()
    monkeypatch.setattr(
        app_module, "MetadataCache", functools.partial(MetadataCache, clock=clock)
    )
    settings = make_settings()
    assert settings.output.paperless_cache_ttl_seconds == _TTL_SECONDS
    with TestClient(create_app(settings, StubScannerBackend())) as tc:
        yield tc, clock


def _warnings(caplog: pytest.LogCaptureFixture, logger: str) -> list[logging.LogRecord]:
    """Return the WARNING records ``logger`` wrote."""
    return [
        r for r in caplog.records if r.name == logger and r.levelno == logging.WARNING
    ]


def test_an_outage_keeps_the_last_good_tag_list(
    clocked: _Clocked, caplog: pytest.LogCaptureFixture
) -> None:
    """Tags fetched once are still rendered after Paperless stops answering."""
    client, clock = clocked
    paperless = _app(client).state.paperless
    offline_get_tags = paperless.get_tags
    paperless.get_tags = lambda: [{"id": 1, "name": "receipt"}]
    assert "receipt" in client.get("/api/tags").text

    paperless.get_tags = offline_get_tags
    clock.advance(_TTL_SECONDS + 1)
    with caplog.at_level(logging.WARNING):
        first = client.get("/api/tags")
        second = client.get("/api/tags")

    assert first.status_code == 200
    assert "receipt" in first.text
    assert "receipt" in second.text
    records = _warnings(caplog, "saneless.web.cache")
    assert len(records) == 1
    assert _CAUSE in records[0].getMessage()
    assert "Token" not in records[0].getMessage()
    assert _warnings(caplog, "saneless.web.routes") == []


def test_tags_never_fetched_render_empty_and_log_the_cause(
    clocked: _Clocked, caplog: pytest.LogCaptureFixture
) -> None:
    """With no previous list the tags render empty, and the warning says why."""
    client, _clock = clocked

    with caplog.at_level(logging.WARNING):
        response = client.get("/api/tags")

    assert response.status_code == 200
    assert "receipt" not in response.text
    records = _warnings(caplog, "saneless.web.routes")
    assert len(records) == 1
    message = records[0].getMessage()
    assert "using empty list" in message
    assert _CAUSE in message
    assert "Token" not in message
    assert records[0].exc_info is None
