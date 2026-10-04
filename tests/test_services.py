"""
The web app's collaborators are one typed object, read through ``services``.

A handler that reads a collaborator goes through one function that narrows the
type, so a misspelt name is a type error rather than an attribute that quietly
does not exist.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from saneless.web.services import Services, services
from starlette.requests import Request

from saneless.web.app import create_app
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
