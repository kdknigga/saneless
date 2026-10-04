"""
One rendering rule for every error the web layer produces.

Route-raised ``HTTPException``s, the router's and ``StaticFiles``' 404 and 405,
request validation failures and any unhandled exception all end in
``render_error``.  An htmx request gets the error partial, retargeted into the
page's ``#status-message`` slot, except the status strip fetching itself
(``CHECKS_POLL_TARGET_ID``); any other request gets ``{"status": "error",
"detail": <message>}``.  A scan form a browser posted by itself, with
JavaScript off, gets a whole page instead (``BrowserNavigationRefused``).

Every message is a ``RequestRejection`` vocabulary constant, and the only
request input a body carries is the neutralised, bounded ``Host`` of a 421.
Log lines carry only the method and path, formatted with ``%r`` so a control
character cannot forge a log line.

Starlette re-raises after the catch-all handler's response is sent, so uvicorn
logs its traceback a second time; that duplicate is accepted, because avoiding
it would need a middleware that swallows exceptions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from saneless.vocabulary import (
    NO_SCRIPT_BACK_LINK,
    NO_SCRIPT_BODY,
    NO_SCRIPT_HEADING,
    NO_SCRIPT_PAGE_TITLE,
    RequestRejection,
    rejection_message,
    rejection_status_code,
)

from .security_headers import NO_STORE, SECURITY_HEADERS
from .services import services

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import FastAPI, Request
    from starlette.responses import Response

__all__ = [
    "CHECKS_POLL_TARGET_ID",
    "RETRY_AFTER_SECONDS",
    "TITLE_CONTROL_TYPE",
    "BrowserNavigationRefused",
    "RequestRejected",
    "TechnicalDetails",
    "install_error_handlers",
    "rejection_for_status",
    "render_error",
]

logger = logging.getLogger(__name__)

# A scan takes tens of seconds, so a shorter hint would only invite a retry
# that is rejected again.
RETRY_AFTER_SECONDS: Final = 30

# The id of the one element whose error must land on itself, compared with the
# ``HX-Target`` header verbatim; the routes of every other polling element catch
# their own failures.  An armed htmx poll ends only when its element leaves the
# DOM or on an HTTP 286, so a retargeted strip failure would leave the strip
# polling for the life of the tab.
CHECKS_POLL_TARGET_ID: Final = "checks-body"

_SCAN_SUBMIT: Final = ("POST", "/api/scan")

# Refusals that do not hand focus back to the Scan button: the button is
# disabled by design on an appliance that cannot upload, and re-rendering it
# enabled would offer a press the route is certain to refuse.
_BLOCKED_SCAN_REJECTIONS: Final = frozenset(
    {RequestRejection.TOKEN_UNSET, RequestRejection.URL_UNSET}
)

_BAD_REQUEST = 400
_TOO_MANY_REQUESTS = 429
_NOT_FOUND = 404
_METHOD_NOT_ALLOWED = 405
_UNPROCESSABLE = 422
_SERVER_ERROR_FLOOR = 500

_TITLE_LOC = ("body", "title")
_TOO_LONG_TYPE = "string_too_long"
# The error type the scan route's title validator raises for a control
# character; the route imports it from here.
TITLE_CONTROL_TYPE: Final = "title_control_character"


@dataclass(frozen=True, slots=True)
class TechnicalDetails:
    """
    The facts an error's "Technical details" disclosure may show, beyond status.

    This is the slot's whole permitted vocabulary in one place.  Each field is
    either a value saneless made or a value its caller has already made safe;
    exception text, other request input and the log path have no field here
    (ASVS 4.0.3 V7.4.1).
    """

    job_id: str | None = None
    """The job row the refused attempt wrote, or None when it wrote none."""

    echoed_host: str | None = None
    """
    The refused Host of a 421, or None.

    The caller neutralises and bounds it.  It is shown in Technical details
    and as the JSON ``"host"`` field, never inside the sentence.
    """


_NO_DETAILS: Final = TechnicalDetails()


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
                when it wrote none.  Job History reloads exactly when it is set.

        """
        super().__init__(
            status_code=rejection_status_code(rejection),
            detail=rejection_message(rejection),
        )
        self.rejection = rejection
        self.job_id = job_id


class BrowserNavigationRefused(Exception):
    """
    A browser posted the scan form as a navigation, so no scan is started.

    That happens only when JavaScript is off or htmx failed to load.  A browser
    shows a navigation's answer as the page, so this is answered with
    ``no_script.html`` and never through ``render_error``, which would send JSON.
    """


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


def _is_the_strip_fetching_itself(request: Request) -> bool:
    """
    Say whether this request is the status strip asking for its own body.

    True for a GET whose ``HX-Target`` is ``CHECKS_POLL_TARGET_ID``.  The
    method is part of the key because the ``Check again`` button's POST targets
    the same id, and a click's failure must go to the slot, not over the strip.
    """
    return (
        request.method == "GET"
        and request.headers.get("HX-Target") == CHECKS_POLL_TARGET_ID
    )


def render_error(
    request: Request,
    rejection: RequestRejection,
    *,
    status_code: int,
    details: TechnicalDetails = _NO_DETAILS,
    extra_headers: Mapping[str, str] | None = None,
) -> Response:
    """
    Render an error response for either an htmx or a plain request.

    The htmx body's "Technical details" carry only the status code, the job
    row the refused attempt wrote, and a 421's refused Host; exception text,
    other request input and the log path never reach it (ASVS 4.0.3 V7.4.1).
    Every response carries ``SECURITY_HEADERS`` and ``NO_STORE``, because the
    catch-all 500 is sent outside the ``SecurityHeaders`` middleware.

    The strip fetching itself gets no ``HX-Retarget``: htmx 2.0.10 applies that
    header before choosing what to swap, so a retargeted failure left the strip
    polling.  ``base.html``'s ``{"code":"[45]..","swap":true,"error":true}``
    rule is what makes the exempt error body swap at all.

    A refused Scan press carries the Scan button out-of-band, enabled and with
    ``autofocus``, because the press disabled it and dropped focus; focus never
    moves into ``#status-message``.  A blocked appliance never gets the button
    back enabled.

    Args:
        request: The request being answered.
        rejection: The vocabulary member whose message is shown.
        status_code: The HTTP status code to send.
        details: The facts beyond the status code that Technical details
            may show; none by default.
        extra_headers: Headers the exception carries and the response must
            keep, such as a 405's ``Allow`` (RFC 9110 section 15.5.6).

    Returns:
        The error partial for an htmx request, retargeted to
        ``#status-message`` unless the request is the strip fetching itself
        (a GET targeting ``CHECKS_POLL_TARGET_ID``), in which case it is left
        to be swapped by the target's own rule; otherwise the JSON error shape.

    """
    # The middleware sets rather than appends, so an error it also passes
    # through still carries each header once.
    headers: dict[str, str] = dict((*SECURITY_HEADERS, NO_STORE))
    headers.update(extra_headers or {})
    if status_code == _TOO_MANY_REQUESTS:
        headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
    message = rejection_message(rejection)
    if request.headers.get("HX-Request") == "true":
        if not _is_the_strip_fetching_itself(request):
            headers["HX-Retarget"] = "#status-message"
            headers["HX-Reswap"] = "innerHTML"
        svc = services(request)
        refocus_scan = (
            (request.method, request.url.path) == _SCAN_SUBMIT
            and rejection not in _BLOCKED_SCAN_REJECTIONS
            and not svc.scan_blocked
        )
        return svc.templates.TemplateResponse(
            request,
            "partials/error.html",
            {
                "message": message,
                "status_code": status_code,
                "job_id": details.job_id,
                "refresh_history": details.job_id is not None,
                "host": details.echoed_host,
                "refocus_scan": refocus_scan,
            },
            status_code=status_code,
            headers=headers,
        )
    body = {"status": "error", "detail": message}
    if details.echoed_host is not None:
        body["host"] = details.echoed_host
    return JSONResponse(body, status_code=status_code, headers=headers)


async def _http_exception(request: Request, exc: Exception) -> Response:
    """Render an ``HTTPException``, raised by a route or by the framework."""
    if not isinstance(exc, StarletteHTTPException):
        return await _unhandled_exception(request, exc)
    if isinstance(exc, RequestRejected):
        return render_error(
            request,
            exc.rejection,
            status_code=exc.status_code,
            details=TechnicalDetails(job_id=exc.job_id),
        )
    # The exception's own headers are kept: the router's 405 carries the
    # ``Allow`` header RFC 9110 requires on a 405.
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
    title_has_control = any(
        loc == _TITLE_LOC and error_type == TITLE_CONTROL_TYPE
        for loc, error_type in failures
    )
    title_too_long = any(
        loc == _TITLE_LOC and error_type == _TOO_LONG_TYPE
        for loc, error_type in failures
    )
    if title_has_control:
        rejection = RequestRejection.TITLE_HAS_CONTROL
    elif title_too_long:
        rejection = RequestRejection.TITLE_TOO_LONG
    else:
        rejection = RequestRejection.INVALID_REQUEST
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


async def _browser_navigation_refused(request: Request, exc: Exception) -> Response:
    """
    Answer a scan form a browser posted by itself with the refusal page.

    Every word comes from the vocabulary and none from the request
    (ASVS 4.0.3 V7.4.1), and the response carries ``SECURITY_HEADERS`` and
    ``NO_STORE`` itself.  Nothing was started or recorded.
    """
    if not isinstance(exc, BrowserNavigationRefused):
        return await _unhandled_exception(request, exc)
    return services(request).templates.TemplateResponse(
        request,
        "no_script.html",
        {
            "page_title": NO_SCRIPT_PAGE_TITLE,
            "heading": NO_SCRIPT_HEADING,
            "body": NO_SCRIPT_BODY,
            "back_link": NO_SCRIPT_BACK_LINK,
        },
        status_code=_BAD_REQUEST,
        headers=dict((*SECURITY_HEADERS, NO_STORE)),
    )


def install_error_handlers(app: FastAPI) -> None:
    """
    Register the handlers that answer every error the web layer produces.

    All but ``BrowserNavigationRefused`` render through ``render_error``.

    Args:
        app: The application to register the handlers on.

    """
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(BrowserNavigationRefused, _browser_navigation_refused)
    app.add_exception_handler(Exception, _unhandled_exception)
