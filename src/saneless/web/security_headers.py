"""
Send the same three security headers on every response, and forbid caching.

``Content-Security-Policy`` allows nothing that is not served by saneless
itself: no inline script, no inline style, no foreign script, stylesheet,
font or connection.  Images may also be ``data:`` URIs, because Pico's form
icons and the scan preview are.  ``frame-ancestors 'none'`` refuses every
frame, so a hostile page cannot put saneless under a disguised button and
steer a real click onto Scan or a flip answer; the click would be the user's
own, and the cross-site check could not tell.  ``base-uri 'none'`` and
``form-action 'self'`` stop injected markup from moving where relative links
and forms go.

``X-Frame-Options: DENY`` says what ``frame-ancestors`` says to browsers too
old to read it.  ``X-Content-Type-Options: nosniff`` stops a browser guessing
a content type the server did not send.

``Cache-Control: no-store`` goes on every response except the static files.
The page, the status poll and the history differ by browser: the one that
started a job sees its real title and thumbnail, every other one a generic
title.  A caching reverse proxy or shared cache that kept the owner's copy
could hand it to anyone, so no dynamic response may be stored at all.  The
files under ``/static/`` are the same for everyone and stay cacheable.

The pages keep to the policy by construction: no template carries a ``style``
attribute, a ``<style>`` element, an inline script, an event-handler attribute
or an htmx expression that htmx would evaluate, and the only scripts and
stylesheets are the vendored files under ``/static/``.  ``base.html`` turns
htmx's own ``<style>`` injection, eval and script-tag processing off.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from starlette.datastructures import MutableHeaders

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["NO_STORE", "SECURITY_HEADERS", "STATIC_PATH", "SecurityHeaders"]

SECURITY_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    (
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
)
"""The headers every response carries, as ``(name, value)`` pairs."""

NO_STORE: Final[tuple[str, str]] = ("Cache-Control", "no-store")
"""The header every response but a static file carries, as ``(name, value)``."""

STATIC_PATH: Final = "/static"
"""Where the static files are mounted; the only responses a cache may keep."""


class SecurityHeaders:
    """
    Pure ASGI middleware that sets ``SECURITY_HEADERS`` on every HTTP response.

    It also sets ``NO_STORE`` on every response whose path is not under
    ``STATIC_PATH``.

    It is installed as the outermost of the application's own middleware, so
    every route, every static file, the router's 404 and 405 and the refusals
    of the Host and cross-site checks pass through it.

    Each header is set, not appended, so a response that already carries it,
    as every ``render_error`` response does, still carries it exactly once.

    One response never reaches it: the 500 for an unhandled exception.
    Starlette's ``ServerErrorMiddleware`` sits outside all of the
    application's middleware and sends that response through its own outer
    ``send``.  That is why ``render_error`` sets the same headers itself,
    ``NO_STORE`` included.

    It is a plain ASGI class rather than ``BaseHTTPMiddleware``, which does
    not propagate ``contextvars`` changes and wraps streaming responses.
    """

    def __init__(self, app: ASGIApp) -> None:
        """
        Wrap the next application in the middleware stack.

        Args:
            app: The ASGI application whose responses get the headers.

        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Pass the request on, adding the headers to the response's start.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.

        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope["path"]
        cacheable = path.startswith(f"{STATIC_PATH}/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    headers[name] = value
                if not cacheable:
                    name, value = NO_STORE
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
