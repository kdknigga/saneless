"""
Whether this appliance can start a scan at all, and why not.

A placeholder paperless-ngx token or an empty ``paperless.url`` means every
scan would be fed and then have nowhere to go.  The verdict is read from the
settings the process started with, so it cannot change while the process
runs.  The scan submit refuses on it, the status area disables the Scan
button and says why, and the error rendering keeps that button disabled, so
all three ask this one module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from saneless.config import is_placeholder_token
from saneless.vocabulary import (
    SCAN_BLOCKED_REASON,
    SCAN_BLOCKED_URL_REASON,
    TOKEN_UNSET_JOB_ERROR,
    URL_UNSET_JOB_ERROR,
    RequestRejection,
)

if TYPE_CHECKING:
    from saneless.config import Settings

__all__ = ["ScanBlock", "block_for", "scan_is_blocked"]


@dataclass(frozen=True, slots=True)
class ScanBlock:
    """
    Why no scan can start on this appliance, in each form a surface shows it.

    Attributes:
        rejection: What ``POST /api/scan`` is refused with.
        job_error: The refused attempt's job-row error.
        reason: The line beneath the disabled Scan button.

    """

    rejection: RequestRejection
    job_error: str
    reason: str


_TOKEN_UNSET_BLOCK: Final = ScanBlock(
    RequestRejection.TOKEN_UNSET, TOKEN_UNSET_JOB_ERROR, SCAN_BLOCKED_REASON
)
_URL_UNSET_BLOCK: Final = ScanBlock(
    RequestRejection.URL_UNSET, URL_UNSET_JOB_ERROR, SCAN_BLOCKED_URL_REASON
)


def block_for(settings: Settings) -> ScanBlock | None:
    """
    Decide whether paperless-ngx is configured well enough for a scan to start.

    The one place the web layer decides it, so the Scan button, its reason line
    and the route guard cannot disagree.  A placeholder token is named before an
    empty ``paperless.url``, as the status strip's Paperless row orders them.
    The unwrapped token goes to the predicate only: it is never logged,
    rendered, echoed or put in the job row (ASVS 4.0.3 V7.1.1).

    Args:
        settings: The settings the process started with.

    Returns:
        The block, or None when a scan may start.

    """
    if is_placeholder_token(settings.paperless.token.get_secret_value()):
        return _TOKEN_UNSET_BLOCK
    if not settings.paperless.url:
        return _URL_UNSET_BLOCK
    return None


def scan_is_blocked(settings: Settings) -> bool:
    """
    Say whether this appliance refuses every scan, from ``block_for``'s rule.

    ``create_app`` records it once on the app's services, because the settings
    cannot change while the process runs, and the error rendering reads it
    there, so a refused Scan press on a blocked appliance never re-enables the
    button.

    Args:
        settings: The settings the process started with.

    Returns:
        True when no scan can start.

    """
    return block_for(settings) is not None
