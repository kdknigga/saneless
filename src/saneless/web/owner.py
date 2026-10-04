"""
Who may answer for a job: the owner token a browser carries in a cookie.

The first scan a browser submits mints a random token, records it on the job
row and hands it back as a cookie, and every later submit from that browser
records the same token again.  Two things read it back: the status view,
which shows a job's title, thumbnail and error text only to the browser that
owns it, and the prompt handlers, which pass a flip or multi-page answer to
the worker only from that browser.  Both ask the same question, so the cookie
is written, read and compared in this one module, beside the handlers and the
view rather than inside either.
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from saneless.job import Job

__all__ = [
    "OWNER_COOKIE",
    "OWNER_COOKIE_MAX_AGE",
    "is_owner",
    "owner_answers",
    "presented_owner",
    "set_owner_cookie",
    "token_for",
]

logger = logging.getLogger(__name__)

# The cookie naming the browser that started a scan.  Every attribute it
# is set with is deliberate: ``HttpOnly`` so no script can read it -- there is
# no script file in this application at all; ``SameSite=Lax`` so the browser
# withholds it on any cross-site POST; ``Max-Age`` of one year, so a browser
# keeps seeing its own scans' titles and previews after it restarts; and
# deliberately no ``Secure``, because the appliance is served over plain HTTP
# on a LAN and that flag would silently stop the cookie being sent rather than
# harden it.
#
# The cookie is re-set on every accepted submit, with the value the browser
# presented or a fresh one when it presented none.  That renews the year each
# time, and it is how a session cookie set by an older release gains the
# lifetime: its token comes back unchanged, now persistent.
#
# The token's position, recorded here so a later reader does not mistake this
# for something it is not: the token is a footgun guard for the flip prompt,
# not an authentication mechanism.  ``CrossOriginGuard`` allows a POST that
# carries neither ``Sec-Fetch-Site`` nor ``Origin``, so a scripted client that
# sends a guessed cookie of its own can answer a flip.  That is accepted on a
# trusted LAN, not overlooked.  What the token stops is the household member
# standing at the same appliance pressing Continue on a stack they did not
# load, which is the failure the token exists to close.
OWNER_COOKIE: Final = "saneless_owner"

# One year in seconds: the owner cookie's lifetime, renewed on every accepted
# submit.
OWNER_COOKIE_MAX_AGE: Final = 365 * 24 * 60 * 60


def set_owner_cookie(response: Response, owner: str) -> None:
    """
    Set the owner cookie on a response, with the attributes documented above.

    The one place the cookie is written, so the response a scan submit sends
    carries the same cookie whichever of its responses goes out.

    Args:
        response: The response to set the cookie on.
        owner: The owner token the accepted job recorded.

    """
    response.set_cookie(
        OWNER_COOKIE,
        owner,
        max_age=OWNER_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


def presented_owner(request: Request) -> str | None:
    """
    Return the owner token this request carries, or None when it carries none.

    A blank or whitespace-only value counts as none.  A browser holding one has
    no usable identity, and treating it as a token would make every such
    browser the same owner as every other.

    Args:
        request: The incoming request.

    Returns:
        The token, or None.

    """
    presented = request.cookies.get(OWNER_COOKIE, "").strip()
    return presented or None


def is_owner(presented: str | None, recorded: str | None) -> bool:
    """
    Report whether a presented token speaks for the job that recorded one.

    A NULL recorded token means the job is unowned and everyone may answer it.
    Every row written before owner tokens existed has one, including a
    manual-duplex job that was in flight across an upgrade, and a strict rule
    would leave such a job un-continuable until the manual-duplex flip timeout
    fired it away.  Nothing creates a NULL-token job any more, so the exception
    has a closed lifetime.

    The comparison goes through ``secrets.compare_digest`` so no timing
    difference can be read off it.  Both sides are encoded first:
    the presented value arrives as text out of a header and ``compare_digest``
    refuses a non-ASCII ``str``, while it compares bytes of any two lengths
    safely.

    Args:
        presented: The token this request carries, or None.
        recorded: The token stored on the job row, or None when unowned.

    Returns:
        Whether this request may answer for the job.

    """
    if recorded is None:
        return True
    if presented is None:
        return False
    return secrets.compare_digest(presented.encode(), recorded.encode())


def owner_answers(presented: str | None, job: Job | None) -> bool:
    """
    Report whether this request may answer the named job's prompt.

    One rule for both kinds of wait: the manual-duplex flip prompt and every
    multi-page prompt.  An unknown job id answers False: there is nothing to
    own, and the worker would have dropped the answer anyway.  The outcome is logged as a match or
    a mismatch and never as a value -- the token is not allowed into a log
    line any more than into the markup.

    Args:
        presented: The token this request carries, or None.
        job: The job the answer names, or None when no such row exists.

    Returns:
        Whether the answer should be passed to the worker.

    """
    if job is None:
        return False
    matched = is_owner(presented, job.owner_token)
    logger.debug(
        "Answer for job %s: owner %s",
        job.id,
        "matched" if matched else "did not match",
    )
    return matched


def token_for(presented: str | None) -> str:
    """
    Return the owner token a scan submit records and sends back.

    The mint rule: a token is minted on the first submit from a browser
    and reused for every later job from it, so two tabs on one device do not
    disown each other.  It is recorded on the row either way, and an
    accepted submit sends it back as a cookie either way, so the lifetime is
    renewed.

    Args:
        presented: The token the request carries, or None when it carries none.

    Returns:
        The presented token, or a fresh one when there was none.

    """
    return presented or secrets.token_urlsafe(32)
