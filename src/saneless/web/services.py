"""
The web app's collaborators, as one typed object read through ``services``.

``create_app`` builds a single ``Services`` and stores it on the application.
Every handler, error handler and the server's stop hook reads it through
``services(request)``, which narrows the type, so a misspelt collaborator or a
wrong keyword passed to one is an error in both type checkers rather than an
attribute that quietly does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates
    from starlette.requests import Request

    from saneless.config import Settings
    from saneless.job import JobStore
    from saneless.paperless import PaperlessClient
    from saneless.worker import ScanWorker

    from .cache import MetadataCache
    from .checks_cache import CheckCache
    from .refresher import CheckRefresher
    from .throttle import MinimumInterval, SingleFlightResult

__all__ = ["AppLifecycle", "PaperlessTestAnswer", "Services", "services"]


@dataclass(slots=True)
class AppLifecycle:
    """The one per-app value that changes after start-up."""

    started: bool = False
    """
    Whether the lifespan owns the scanner.

    The lifespan sets it once start-up finishes, and also when a failed
    start-up left a thread running, since that thread may still be inside
    SANE.  ``serve`` reads it to decide whether the scanner is still its own
    to close.
    """


@dataclass(frozen=True, slots=True)
class PaperlessTestAnswer:
    """One connection-test response, as the shared result stores it."""

    status_code: int
    body: dict[str, str]


# One object, fixed at start-up: nothing reassigns a field, the one value that
# moves lives in ``lifecycle``, and a test swaps a collaborator by building a
# new object with ``dataclasses.replace``.
# See docs/explanation/decisions/0010-typed-services-accessor.md.
@dataclass(frozen=True, slots=True, kw_only=True)
class Services:
    """Everything a handler reaches beyond its own request."""

    worker: ScanWorker
    job_store: JobStore
    settings: Settings
    paperless: PaperlessClient
    cache: MetadataCache
    checks: CheckCache
    refresher: CheckRefresher
    templates: Jinja2Templates
    invalidate_floors: dict[str, MinimumInterval]
    paperless_test_result: SingleFlightResult[PaperlessTestAnswer]
    status_token_key: bytes
    scan_blocked: bool
    lifecycle: AppLifecycle


def services(request: Request) -> Services:
    """
    Return the services of the app ``request`` arrived at.

    Args:
        request: The request being answered.

    Returns:
        The ``Services`` object ``create_app`` stored.

    Raises:
        TypeError: If the app holds anything other than a ``Services``.

    """
    found = request.app.state.services
    if not isinstance(found, Services):
        msg = f"the app's services are a {type(found).__name__}, not Services"
        raise TypeError(msg)
    return found
