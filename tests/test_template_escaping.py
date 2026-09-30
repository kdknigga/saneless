"""
Tag and correspondent names from paperless-ngx reach the page only escaped.

The names are third-party text: anyone who can create a tag in paperless-ngx
chooses them, and every LAN client that opens saneless renders them. Jinja's
autoescape is what keeps them inert, so these tests pin it from both sides: a
hostile name on every route that renders one, and a static walk of the
templates for anything that would switch escaping off.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saneless.config import ProfileConfig
from saneless.web.app import TEMPLATE_DIR, create_app
from tests.conftest import StubScannerBackend

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from saneless.config import Settings

pytestmark = pytest.mark.usefixtures("offline_paperless")

_HOSTILE_NAME = "<img src=x onerror=alert(1)>"
_ESCAPED_NAME = "&lt;img src=x onerror=alert(1)&gt;"
_RAW_MARKER = "<img src=x onerror"

_UNSAFE_TEMPLATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\|\s*safe\b"),
    re.compile(r"\bMarkup\b"),
    re.compile(r"autoescape\s+false"),
)
"""
What would let a template print a value unescaped.

The ``safe`` filter, the ``Markup`` class and a block that turns autoescape off.
"""


@pytest.fixture
def app(make_settings: Callable[..., Settings]) -> FastAPI:
    """
    Build the app with a paperless stub whose every name is hostile markup.

    Both lists are shown: ``show_tags`` and ``show_correspondent`` default on.

    Returns:
        The app, its paperless client answering from the stub lists.

    """
    settings = make_settings(profiles={"default": ProfileConfig()})
    assert settings.web.show_tags
    assert settings.web.show_correspondent
    application = create_app(settings, StubScannerBackend())
    application.state.paperless.get_tags = lambda *, timeout=None: [
        {"id": 1, "name": _HOSTILE_NAME}
    ]
    application.state.paperless.get_correspondents = lambda *, timeout=None: [
        {"id": 2, "name": _HOSTILE_NAME}
    ]
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """
    Run the app's lifespan around a TestClient.

    Yields:
        The client.

    """
    with TestClient(app) as tc:
        yield tc


@pytest.mark.parametrize(
    ("method", "path", "renders"),
    [
        pytest.param("GET", "/", 2, id="index-renders-both-lists"),
        pytest.param("GET", "/api/tags", 1, id="tags-partial"),
        pytest.param("GET", "/api/correspondents", 1, id="correspondents-partial"),
        pytest.param(
            "POST",
            "/api/cache/invalidate?resource=tags",
            1,
            id="invalidate-tags",
        ),
        pytest.param(
            "POST",
            "/api/cache/invalidate?resource=correspondents",
            1,
            id="invalidate-correspondents",
        ),
    ],
)
def test_paperless_names_onerror_markup_is_escaped(
    client: TestClient, method: str, path: str, renders: int
) -> None:
    """A tag or correspondent named as an ``<img onerror>`` renders as text."""
    response = client.request(method, path)

    assert response.status_code == 200, response.text
    assert _RAW_MARKER not in response.text
    assert response.text.count(_ESCAPED_NAME) >= renders


def test_no_template_switches_escaping_off(client: TestClient) -> None:
    """
    No template marks a value safe, and autoescape is on for HTML.

    The app is taken through ``client`` rather than built bare: only the
    lifespan closes the job store ``create_app`` opens, and a connection left
    to the garbage collector fails whichever later test it is finalized in.
    """
    app = client.app
    assert isinstance(app, FastAPI)
    templates = sorted(TEMPLATE_DIR.rglob("*.html"))
    assert templates, f"no templates found under {TEMPLATE_DIR}"

    offenders = [
        f"{path.relative_to(TEMPLATE_DIR)}: {pattern.pattern}"
        for path in templates
        for pattern in _UNSAFE_TEMPLATE_PATTERNS
        if pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []
    assert app.state.templates.env.autoescape("x.html") is True
