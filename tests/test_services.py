"""
The web app's collaborators are one typed object, read through ``services``.

A handler that reads a collaborator goes through one function that narrows the
type, so a misspelt name is a type error rather than an attribute that quietly
does not exist.
"""

from __future__ import annotations

import dataclasses
import operator
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from saneless.web.app import create_app
from saneless.web.services import Services, services
from tests.conftest import StubScannerBackend

if TYPE_CHECKING:
    from saneless.config import Settings


def _request_for(app: FastAPI) -> Request:
    """
    Build a bare request whose app is ``app``, as a handler would receive it.

    Args:
        app: The application the request belongs to.

    Returns:
        A request carrying only what ``services`` reads.

    """
    return Request({"type": "http", "app": app, "headers": []})


def test_services_returns_the_object_create_app_stored(
    default_settings: Settings,
) -> None:
    """``services(request)`` answers with the app's own ``Services``, not a copy."""
    app = create_app(default_settings, StubScannerBackend())
    try:
        found = services(_request_for(app))
        assert isinstance(found, Services)
        assert found is app.state.services
    finally:
        app.state.services.job_store.close()
        app.state.services.paperless.close()


def test_services_refuses_an_app_holding_something_else() -> None:
    """Anything but a ``Services`` on the app is a TypeError, never a silent read."""
    app = FastAPI()
    app.state.services = object()
    with pytest.raises(TypeError):
        services(_request_for(app))


def test_services_fields_are_fixed_but_the_lifecycle_flag_moves(
    default_settings: Settings,
) -> None:
    """
    A field cannot be reassigned; the lifecycle's started flag can.

    The collaborators are decided once, at start-up.  The one value the
    lifespan changes afterwards lives in a mutable holder, which starts false.
    """
    app = create_app(default_settings, StubScannerBackend())
    try:
        found = services(_request_for(app))
        field = "scan_blocked"
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(found, field, not found.scan_blocked)
        assert found.lifecycle.started is False
        found.lifecycle.started = True
        assert services(_request_for(app)).lifecycle.started is True
    finally:
        app.state.services.job_store.close()
        app.state.services.paperless.close()


# Every collaborator, and the lifecycle flag, by the name the app state could
# hold a stray copy under.
_FORMER_STATE_NAMES = (
    "worker",
    "job_store",
    "settings",
    "paperless",
    "cache",
    "checks",
    "refresher",
    "templates",
    "invalidate_floors",
    "paperless_test_result",
    "status_token_key",
    "scan_blocked",
    "lifespan_started",
)


@pytest.mark.parametrize("name", _FORMER_STATE_NAMES)
def test_collaborators_are_not_also_kept_under_their_own_names(
    default_settings: Settings, name: str
) -> None:
    """
    The app state holds no collaborator beside ``services``.

    A second copy under its own name would let a reader that missed the
    typed path keep working against an object a test had swapped out of
    ``services``; with no copy, such a reader fails at once.
    """
    app = create_app(default_settings, StubScannerBackend())
    try:
        with pytest.raises(AttributeError):
            operator.attrgetter(f"state.{name}")(app)
    finally:
        app.state.services.job_store.close()
        app.state.services.paperless.close()


@pytest.mark.parametrize("name", _FORMER_STATE_NAMES)
def test_a_running_app_keeps_no_collaborator_under_its_own_name(
    default_settings: Settings, name: str
) -> None:
    """
    The app state still holds no collaborator beside ``services`` once started.

    The lifespan writes state of its own, so the check is repeated with the
    lifespan running, not only on the app ``create_app`` returned.
    """
    app = create_app(default_settings, StubScannerBackend())
    with TestClient(app), pytest.raises(AttributeError):
        operator.attrgetter(f"state.{name}")(app)
