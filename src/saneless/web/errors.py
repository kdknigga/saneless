"""
One rendering rule for every error the web layer produces.

Route-raised ``HTTPException``s, the router's and ``StaticFiles``' 404 and 405,
request validation failures and any unhandled exception all end in
``render_error`` (D-01).  An htmx request gets the error partial, retargeted
into the page's ``#status-message`` slot so an error never lands in the element
the request was aimed at (D-02, D-03); any other request gets
``{"status": "error", "detail": <message>}`` with the same status code, and a
429 carries ``Retry-After`` on both branches (D-04).

Every message is a ``RequestRejection`` vocabulary constant.  No request input
and no exception text reaches a response body from here.  The only request
input a log line carries is the method and path, formatted with ``%r`` so a
control character in them is escaped and cannot forge a log line, as in
``cross_origin``.

The catch-all handler logs the traceback with ``exc_info``.  Starlette's
``ServerErrorMiddleware`` re-raises after the handler's response is sent, so
uvicorn logs the same traceback a second time as "Exception in ASGI
application".  That duplicate is accepted: the alternative is a middleware that
swallows exceptions, which would leave the application without a real
catch-all handler.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from saneless.vocabulary import (
    RequestRejection,
    rejection_message,
    rejection_status_code,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import FastAPI, Request
    from starlette.responses import Response

__all__ = [
    "RETRY_AFTER_SECONDS",
    "RequestRejected",
    "install_error_handlers",
    "rejection_for_status",
    "render_error",
]

logger = logging.getLogger(__name__)

# How long a client is told to wait after a 429.  A scan takes tens of seconds,
# so a shorter hint would only invite a retry that is rejected again.
RETRY_AFTER_SECONDS: Final = 30

_TOO_MANY_REQUESTS = 429
_NOT_FOUND = 404
_METHOD_NOT_ALLOWED = 405
_UNPROCESSABLE = 422
_SERVER_ERROR_FLOOR = 500

_TITLE_LOC = ("body", "title")
_TOO_LONG_TYPE = "string_too_long"


class RequestRejected(HTTPException):
    """
    An ``HTTPException`` that names the ``RequestRejection`` it renders as.

    Routes and middleware raise this instead of building error HTML; the status
    code and detail both come from the vocabulary, so they cannot disagree with
    the message the page shows.
    """

    def __init__(
        self, rejection: RequestRejection, *, job_id: str | None = None
    ) -> None:
        """
        Build the exception from a rejection.

        Args:
            rejection: The vocabulary member to render.
            job_id: The id of the job row the refused attempt wrote, or None
                when it wrote none.  This one argument carries what used to be
                two: the rendered error reloads Job History exactly when a row
                was written, so a separate ``refresh_history`` flag was a
                second degree of freedom that production never used
                independently and that a reader had to check could not
                disagree with the id (D-05).  The id is also one of only two
                technical facts the error slot may carry (D-10): it is the row
                the user will find in Job History a moment later, never an
                arbitrary request value.

        """
        super().__init__(
            status_code=rejection_status_code(rejection),
            detail=rejection_message(rejection),
        )
        self.rejection = rejection
        self.job_id = job_id


def rejection_for_status(status_code: int) -> RequestRejection:
    """
    Return the generic rejection for a bare HTTP status code.

    Used for ``HTTPException``s that carry no ``RequestRejection`` of their
    own: the router's 404 and 405, ``StaticFiles``' 404, and any plain
    ``HTTPException`` a route raises.

    Args:
        status_code: The HTTP status code the exception carries.

    Returns:
        The rejection whose message fits that status.

    """
    if status_code == _NOT_FOUND:
        rejection = RequestRejection.NOT_FOUND
    elif status_code == _METHOD_NOT_ALLOWED:
        rejection = RequestRejection.METHOD_NOT_ALLOWED
    elif status_code == _UNPROCESSABLE:
        rejection = RequestRejection.INVALID_REQUEST
    elif status_code >= _SERVER_ERROR_FLOOR:
        rejection = RequestRejection.INTERNAL
    else:
        rejection = RequestRejection.CLIENT_ERROR
    return rejection


def render_error(
    request: Request,
    rejection: RequestRejection,
    *,
    status_code: int,
    job_id: str | None = None,
    extra_headers: Mapping[str, str] | None = None,
) -> Response:
    """
    Render an error response for either an htmx or a plain request.

    The htmx body offers a short "Technical details" disclosure carrying the
    status code and, when the refused attempt wrote a job row, that row's id.
    Those two are the whole permitted vocabulary of the slot: Phase 26 D-10 and
    ASVS V7 forbid exception text, request input and the log path from ever
    reaching it, and a uniform affordance that sometimes lied about having
    detail would be worse than one that says what it has (APPL-04, UI-SPEC S2).

    Whether Job History reloads is decided here rather than in the template, so
    the partial keeps no rule of its own: it reloads exactly when a row was
    written, which is exactly when there is an id to name (D-05).

    Args:
        request: The request being answered.
        rejection: The vocabulary member whose message is shown.
        status_code: The HTTP status code to send.
        job_id: The row the refused attempt wrote, or None when it wrote none.
        extra_headers: Headers the exception carries and the response must
            keep, such as a 405's ``Allow`` (WR-08, RFC 9110 section 15.5.6).

    Returns:
        The error partial retargeted to ``#status-message`` for an htmx
        request, otherwise the JSON error shape.

    """
    headers: dict[str, str] = dict(extra_headers or {})
    if status_code == _TOO_MANY_REQUESTS:
        headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
    message = rejection_message(rejection)
    if request.headers.get("HX-Request") == "true":
        headers["HX-Retarget"] = "#status-message"
        headers["HX-Reswap"] = "innerHTML"
        return request.app.state.templates.TemplateResponse(
            request,
            "partials/error.html",
            {
                "message": message,
                "status_code": status_code,
                "job_id": job_id,
                "refresh_history": job_id is not None,
            },
            status_code=status_code,
            headers=headers,
        )
    return JSONResponse(
        {"status": "error", "detail": message},
        status_code=status_code,
        headers=headers,
    )


async def _http_exception(request: Request, exc: Exception) -> Response:
    """Render an ``HTTPException``, raised by a route or by the framework."""
    if not isinstance(exc, StarletteHTTPException):
        return await _unhandled_exception(request, exc)
    if isinstance(exc, RequestRejected):
        return render_error(
            request,
            exc.rejection,
            status_code=exc.status_code,
            job_id=exc.job_id,
        )
    # The exception's own headers are kept: the router's 405 carries the
    # ``Allow`` header RFC 9110 requires on a 405 (WR-08).
    return render_error(
        request,
        rejection_for_status(exc.status_code),
        status_code=exc.status_code,
        extra_headers=exc.headers,
    )


async def _validation_error(request: Request, exc: Exception) -> Response:
    """
    Render a request validation failure as a 422.

    Only each error's location and type are read or logged.  An error's
    ``input``, ``msg`` and ``ctx`` can carry what the client sent, so none of
    them is touched.
    """
    if not isinstance(exc, RequestValidationError):
        return await _unhandled_exception(request, exc)
    failures = [
        (tuple(error.get("loc", ())), str(error.get("type", "")))
        for error in exc.errors()
    ]
    logger.info(
        "Rejected invalid request to %r %r: %s",
        request.method,
        request.url.path,
        failures,
    )
    title_too_long = any(
        loc == _TITLE_LOC and error_type == _TOO_LONG_TYPE
        for loc, error_type in failures
    )
    rejection = (
        RequestRejection.TITLE_TOO_LONG
        if title_too_long
        else RequestRejection.INVALID_REQUEST
    )
    return render_error(
        request, rejection, status_code=rejection_status_code(rejection)
    )


async def _unhandled_exception(request: Request, exc: Exception) -> Response:
    """Log an unhandled exception's traceback and render a generic 500."""
    logger.error(
        "Unhandled exception while handling %r %r",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    rejection = RequestRejection.INTERNAL
    return render_error(
        request, rejection, status_code=rejection_status_code(rejection)
    )


def install_error_handlers(app: FastAPI) -> None:
    """
    Register the three handlers that send every error through ``render_error``.

    Args:
        app: The application to register the handlers on.

    """
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled_exception)
