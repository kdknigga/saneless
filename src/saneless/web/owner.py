"""
Who may answer for a job: the owner token a browser carries in a cookie.

The first scan a browser submits mints a random token, records it on the job
row and hands it back as a cookie; later submits record the same token.  The
status view shows a job's title, thumbnail and error text only to the owning
browser, and the prompt handlers pass a flip or multi-page answer to the
worker only from it, so the cookie is written, read and compared here alone.
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

# ``HttpOnly`` so no script reads it, ``SameSite=Lax`` so a cross-site POST goes
# without it, and no ``Secure``, because over plain HTTP on a LAN that flag
# would stop the cookie being sent rather than harden it.
#
# The owner token is a per-browser cookie that scopes what a viewer sees and
# may answer, not a login: it stops a second person at the appliance answering
# a prompt for paper they did not load.  A scripted client sending a guessed
# cookie of its own can still answer one, which is accepted on a trusted LAN.
# See docs/explanation/decisions/0014-owner-token-not-a-login.md.
OWNER_COOKIE: Final = "saneless_owner"

# Renewed on every accepted submit, so a browser keeps seeing its own scans'
# titles and previews after it restarts.
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

    A NULL recorded token means the job is unowned and anyone may answer it.
    Nothing creates one now, but a row written before owner tokens has one,
    and a strict rule would strand its flip prompt until the timeout.

    ``secrets.compare_digest`` keeps the comparison constant-time.  It
    refuses a non-ASCII ``str``, so both sides are encoded first.

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

    One rule for the flip prompt and every multi-page prompt.  An unknown job
    id answers False.  The outcome is logged as a match or a mismatch, never
    the token, which no log line or markup may carry.

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

    Minted on a browser's first submit and reused for every later job, so two
    tabs on one device do not disown each other.

    Args:
        presented: The token the request carries, or None when it carries none.

    Returns:
        The presented token, or a fresh one when there was none.

    """
    return presented or secrets.token_urlsafe(32)
