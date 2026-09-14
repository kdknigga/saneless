"""
Reject cross-site state-changing requests (ROBU-10).

The check follows Go 1.25's ``net/http.CrossOriginProtection`` (D-20).
``GET``, ``HEAD`` and ``OPTIONS`` are always allowed; every other method goes
through three branches:

1. When ``Sec-Fetch-Site`` is present, ``same-origin`` and ``none`` are
   allowed and every other value is rejected.  A Paperless UI on the same host
   at another port is ``same-site``, and is rejected.
2. When it is absent and ``Origin`` is present, the request is allowed only
   when Origin's host[:port] equals ``Host`` or one of the comma-separated
   ``X-Forwarded-Host`` entries.  ``Origin: null`` has no host and is rejected.
3. When both are absent the request is allowed: curl, scripts and other
   non-browser clients are not CSRF vectors.

Branch 2 is what protects saneless's documented deployment.  The W3C Fetch
Metadata spec appends ``Sec-Fetch-*`` headers only when the request URL is a
"potentially trustworthy URL", so a browser talking plain HTTP to
``http://<lan-ip>:8080`` never sends ``Sec-Fetch-Site``, and a rule built on it
alone would protect nothing there.

``X-Forwarded-Host`` is accepted as a match (D-21) because a cross-site form
cannot set it, and a cross-site ``fetch`` that sets it triggers a CORS
preflight saneless never approves, so a browser cannot forge it.  Accepting it
makes reverse proxies that add it work without configuration.

As in Go, the scheme is not compared and default ports are not normalised;
browsers omit ``:80`` and ``:443`` from both ``Origin`` and ``Host``.  Unlike
Go, hosts are compared case-insensitively, because a non-browser client may
send a mixed-case ``Host``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from starlette.datastructures import Headers

__all__ = ["is_cross_origin_request"]

_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_ALLOWED_FETCH_SITES: Final = frozenset({"same-origin", "none"})


def _origin_matches_host(origin: str, headers: Headers) -> bool:
    """
    Return whether an ``Origin`` names the host the request was sent to.

    Args:
        origin: The raw ``Origin`` header value.
        headers: The request headers, read for ``Host`` and
            ``X-Forwarded-Host``.

    Returns:
        True when Origin's host[:port] is non-empty and equals ``Host`` or an
        ``X-Forwarded-Host`` entry, ignoring case.

    """
    origin_host = urlsplit(origin).netloc.lower()
    candidates = {headers.get("host", "").lower()}
    candidates |= {
        entry.strip().lower()
        for entry in headers.get("x-forwarded-host", "").split(",")
        if entry.strip()
    }
    return origin_host != "" and origin_host in candidates


def is_cross_origin_request(method: str, headers: Headers) -> bool:
    """
    Return whether a request must be rejected as cross-site (D-20, D-21).

    Args:
        method: The request's HTTP method, upper case.
        headers: The request headers.

    Returns:
        True when the request is a state-changing request that did not come
        from a saneless page.

    """
    fetch_site = headers.get("sec-fetch-site")
    origin = headers.get("origin")
    if method in _SAFE_METHODS:
        rejected = False
    elif fetch_site is not None:
        rejected = fetch_site.lower() not in _ALLOWED_FETCH_SITES
    elif origin is not None:
        rejected = not _origin_matches_host(origin, headers)
    else:
        rejected = False
    return rejected
