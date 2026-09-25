"""
Refuse every request whose ``Host`` does not name saneless.

DNS rebinding lets a page on any website re-point its own name at saneless's
LAN address.  The browser then treats saneless as the same origin as that page,
so the page can read previews, job titles and tag names and start scans, and
the cross-site check cannot see anything wrong.  What the page cannot change
is the ``Host`` header: it still carries the hostile name.  So this check runs
on every request, of every method, ``/health`` and static files included.

A Host is trusted when, ignoring case, the port and one trailing dot, it is:

* an IP literal: dotted IPv4, or IPv6 in brackets;
* ``localhost`` or any other single-label name, such as a Docker service name
  or ``testserver``;
* a name under ``.local``, ``.home.arpa``, ``.internal`` or ``.lan``, which no
  public DNS server answers for; or
* a name ``[web] allowed_hosts`` lists.  An exact entry matches only itself; a
  ``.example.com`` entry matches ``example.com`` and every name under it.

``allowed_hosts`` adds to the defaults and never replaces them, so adding a
name can never lock anyone out of ``http://<lan-ip>:8080``.

Only ``Host`` decides.  ``X-Forwarded-Host`` is never trusted: a rebinding
page cannot set it on a simple request, but a client that can set it could
name anything, so trusting it would let any value through.  It is read for one
thing only.  A reverse proxy that replaces ``Host`` with its upstream's name,
nginx's default, sends a name saneless always answers to, such as
``saneless:8080``, and so turns the check off for every request through it
without a single refusal to show for it.  When a trusted ``Host`` arrives
beside an ``X-Forwarded-Host`` naming a different host, the guard logs one
WARNING saying so, and answers the request as before.

A well-formed Host that is not trusted gets 421 Misdirected Request, with a
sentence naming the key to set and the refused Host beside it.  A missing,
empty, duplicated or malformed Host gets the generic 400: RFC 9110 section 7.2
requires a 400 for an invalid Host, and there would be no name to add.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from starlette.requests import Request

from saneless.text_safety import neutralise_bounded
from saneless.vocabulary import RequestRejection, rejection_status_code

from .errors import TechnicalDetails, render_error
from .throttle import MinimumInterval

if TYPE_CHECKING:
    from collections.abc import Sequence

    from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = ["DEFAULT_TRUSTED_SUFFIXES", "HostGuard", "HostVerdict", "host_verdict"]

logger = logging.getLogger(__name__)

DEFAULT_TRUSTED_SUFFIXES: Final = (".local", ".home.arpa", ".internal", ".lan")
"""Name suffixes trusted with no configuration: none resolves in public DNS."""

ECHOED_HOST_MAX_LENGTH: Final = 255
"""The most characters of a refused Host the error body and log line repeat."""

REPLACED_HOST_REPORT_SECONDS = 3600.0
"""
The shortest gap between two reports of a proxy that seems to replace Host.

Limited per interval rather than once per process: any client that can reach
the port can send a made-up ``X-Forwarded-Host``, and a once-only report it
spent would hide a real misconfigured proxy added later until a restart.
"""

# A host name (letters, digits, dots, hyphens and the underscore Docker
# Compose service names may carry), or an IPv6 literal in brackets, then an
# optional port.  The port may be empty, as RFC 3986 allows ("localhost:"),
# and it is ignored either way.  Matched against the lower-cased value, so a Host
# that fails it cannot carry markup, whitespace or a control character.
_HOST_PATTERN: Final = re.compile(
    r"(?P<name>[a-z0-9._-]+|\[[a-f0-9]*:[a-f0-9.:]+\])(?::[0-9]*)?"
)
_HOST_HEADER: Final = b"host"
_FORWARDED_HOST_HEADER: Final = b"x-forwarded-host"
_LOCALHOST: Final = "localhost"


class HostVerdict(StrEnum):
    """What the ``Host`` header says about where a request was aimed."""

    TRUSTED = "TRUSTED"
    UNTRUSTED = "UNTRUSTED"
    MALFORMED = "MALFORMED"


def _split_host(value: str) -> str | None:
    """
    Return the host part of a ``Host`` value, normalised, or None if malformed.

    Args:
        value: One raw ``Host`` header value.

    Returns:
        The lower-cased name with its port and one trailing dot removed, or
        the bracketed IPv6 literal; None when the value is not a well-formed
        ``name[:port]`` or ``[v6][:port]``, where the port may be empty.

    """
    # ASCII first: str.lower maps some non-ASCII letters, such as the Kelvin
    # sign, onto ASCII ones, which would let them through the pattern.
    match = _HOST_PATTERN.fullmatch(value.lower()) if value.isascii() else None
    name = match.group("name") if match is not None else ""
    if name.startswith("["):
        try:
            address = ipaddress.ip_address(name[1:-1])
        except ValueError:
            name = ""
        else:
            name = name if isinstance(address, ipaddress.IPv6Address) else ""
    else:
        name = name.removesuffix(".")
        if "" in name.split("."):
            name = ""
    return name or None


def _is_ip_literal(name: str) -> bool:
    """
    Return whether a normalised host is an IP address.

    Args:
        name: A host part from ``_split_host``.

    Returns:
        True for a bracketed IPv6 literal or a dotted IPv4 address.

    """
    if name.startswith("["):
        return True
    try:
        ipaddress.IPv4Address(name)
    except ValueError:
        return False
    return True


def _is_allowed(name: str, allowed: tuple[str, ...]) -> bool:
    """
    Return whether ``allowed_hosts`` lists a normalised host name.

    Args:
        name: A host part from ``_split_host``.
        allowed: The configured entries, already normalised at load.

    Returns:
        True when an exact entry equals ``name``, or a ``.suffix`` entry
        equals ``name`` with a leading dot or ends it.

    """
    return any(
        name == entry
        or (entry.startswith(".") and (f".{name}" == entry or name.endswith(entry)))
        for entry in allowed
    )


def host_verdict(values: Sequence[str], allowed: tuple[str, ...]) -> HostVerdict:
    """
    Classify a request's ``Host`` header values.

    Args:
        values: Every ``Host`` header value the request carried, decoded.
        allowed: The normalised ``[web] allowed_hosts`` entries.

    Returns:
        MALFORMED unless there is exactly one well-formed value; TRUSTED when
        it is in the zero-configuration set or ``allowed``; else UNTRUSTED.

    """
    name = _split_host(values[0]) if len(values) == 1 else None
    if name is None:
        verdict = HostVerdict.MALFORMED
    elif (
        _is_ip_literal(name)
        or name == _LOCALHOST
        or "." not in name
        or name.endswith(DEFAULT_TRUSTED_SUFFIXES)
        or _is_allowed(name, allowed)
    ):
        verdict = HostVerdict.TRUSTED
    else:
        verdict = HostVerdict.UNTRUSTED
    return verdict


def _replaced_host(host: str, forwarded: Sequence[str]) -> str | None:
    """
    Return the name a proxy forwarded when it differs from ``Host``.

    Args:
        host: The request's one ``Host`` value, already found well-formed.
        forwarded: Every ``X-Forwarded-Host`` value the request carried.

    Returns:
        The first ``X-Forwarded-Host`` entry, the one the first proxy saw,
        when it is a well-formed host naming another host than ``host``;
        else None.  Ports are ignored, as the Host check ignores them.

    """
    if not forwarded:
        return None
    first = forwarded[0].split(",", 1)[0].strip()
    name = _split_host(first)
    if name is None or name == _split_host(host):
        return None
    return first


class HostGuard:
    """
    Pure ASGI middleware that refuses a request whose Host does not name saneless.

    It is installed app-wide, outside ``CrossOriginGuard``, and inspects every
    HTTP request of every method, so a route added later is covered without a
    per-route dependency and a cross-site request with a foreign Host is
    answered here first.

    It is a plain ASGI class rather than ``BaseHTTPMiddleware``, which does
    not propagate ``contextvars`` changes and wraps streaming responses.

    A refusal calls ``render_error`` directly instead of raising.  Starlette's
    ``ExceptionMiddleware`` sits *inside* user middleware, so an
    ``HTTPException`` raised here would never reach the error handlers and
    would surface as a 500.
    """

    def __init__(self, app: ASGIApp, *, allowed_hosts: Sequence[str] = ()) -> None:
        """
        Wrap the next application in the middleware stack.

        Args:
            app: The ASGI application to call for trusted requests.
            allowed_hosts: The normalised ``[web] allowed_hosts`` entries.

        """
        self.app = app
        self.allowed_hosts = tuple(allowed_hosts)
        # Held shut for REPLACED_HOST_REPORT_SECONDS after each report.
        self._replaced_host_report = MinimumInterval()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Refuse an HTTP request aimed at a name saneless does not answer to.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.

        """
        if scope["type"] == "http":
            # The raw header list, not Request.headers: a second Host must be
            # seen, and nothing but the header named "host" is ever read.
            values = [
                value.decode("latin-1")
                for key, value in scope["headers"]
                if key.lower() == _HOST_HEADER
            ]
            verdict = host_verdict(values, self.allowed_hosts)
            if verdict is HostVerdict.TRUSTED:
                self._report_replaced_host(scope, values[0])
            if verdict is not HostVerdict.TRUSTED:
                request = Request(scope)
                echoed_host = self._log_refusal(request, verdict, values)
                rejection = (
                    RequestRejection.HOST_NOT_ALLOWED
                    if echoed_host is not None
                    else RequestRejection.CLIENT_ERROR
                )
                response = render_error(
                    request,
                    rejection,
                    status_code=rejection_status_code(rejection),
                    details=TechnicalDetails(echoed_host=echoed_host),
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _report_replaced_host(self, scope: Scope, host: str) -> None:
        """
        Log a WARNING when a request looks as if a proxy replaced ``Host``.

        At most once per ``REPLACED_HOST_REPORT_SECONDS``, so a misconfigured
        proxy costs one log line an hour rather than one per request.  The
        line is worded as an observation, because any client can send the
        header that triggers it.  The request itself is not affected.

        Args:
            scope: The ASGI connection scope of a request with a trusted Host.
            host: That request's ``Host`` value.

        """
        forwarded = [
            value.decode("latin-1")
            for key, value in scope["headers"]
            if key.lower() == _FORWARDED_HOST_HEADER
        ]
        forwarded_host = _replaced_host(host, forwarded)
        if forwarded_host is None:
            return
        if self._replaced_host_report.claim(REPLACED_HOST_REPORT_SECONDS) is None:
            return
        logger.warning(
            "A request arrived with Host %r and X-Forwarded-Host %r. If saneless "
            "is behind a reverse proxy, it is replacing the Host the browser "
            "sent, so the Host check cannot refuse DNS rebinding through it: "
            "have the proxy pass the original Host header, and list its public "
            "name in [web] allowed_hosts. This is logged at most once an hour.",
            neutralise_bounded(host, ECHOED_HOST_MAX_LENGTH),
            neutralise_bounded(forwarded_host, ECHOED_HOST_MAX_LENGTH),
        )

    @staticmethod
    def _log_refusal(
        request: Request, verdict: HostVerdict, values: Sequence[str]
    ) -> str | None:
        """
        Log one WARNING for a refusal and return the Host to echo, if any.

        Args:
            request: The refused request.
            verdict: UNTRUSTED or MALFORMED.
            values: The request's decoded ``Host`` values.

        Returns:
            The neutralised, bounded Host for an UNTRUSTED request, which is
            the only kind whose Host is a name worth repeating; None for a
            MALFORMED one.

        """
        # %r keeps the method, path and Host on one escaped line; a
        # percent-encoded newline in the path is decoded by then.
        if verdict is HostVerdict.UNTRUSTED:
            echoed_host = neutralise_bounded(values[0], ECHOED_HOST_MAX_LENGTH)
            logger.warning(
                "Refused a request for Host %r on %s %r; add the name to "
                "[web] allowed_hosts if saneless should answer to it",
                echoed_host,
                request.method,
                request.url.path,
            )
        else:
            echoed_host = None
            logger.warning(
                "Refused a request with a missing, repeated or malformed Host "
                "header on %s %r",
                request.method,
                request.url.path,
            )
        return echoed_host
