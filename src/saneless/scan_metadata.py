"""
What metadata a scan carries, decided in one place.

A scan's tags and correspondent come either from the operator or from the
profile.  This module is the one place that says which: the web form and the
command line both ask it, so the same profile and the same answers give the
same document metadata on every surface.

The rule is about whether a control was answered, never about its value.  An
unanswered control -- one the web form did not show, or one the command line
has no option for -- takes the profile's default.  An answered control is the
whole answer, even when it is empty: a cleared tag list means no tags, and
"No correspondent" means none, whatever the profile says.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from saneless.config import ProfileConfig

__all__ = ["ScanMetadata", "resolve_scan_metadata"]


@dataclass(frozen=True, slots=True)
class ScanMetadata:
    """
    The paperless-ngx metadata one scan is filed with.

    Attributes:
        tags: Tag ids, each once, in the order they were chosen.  Empty means
            the document is filed with no tags.
        correspondent: The correspondent id, or None for none.

    """

    tags: tuple[int, ...]
    correspondent: int | None


def resolve_scan_metadata(
    profile: ProfileConfig,
    *,
    tags: Sequence[int] | None,
    correspondent: int | None,
    correspondent_given: bool,
) -> ScanMetadata:
    """
    Decide a scan's metadata from what was answered and the profile's defaults.

    The tag answer carries its own "unanswered" (``None``), because an empty
    list is a real answer.  The correspondent's cannot: ``None`` already means
    "No correspondent", so whether the control was answered at all travels
    separately in ``correspondent_given``.

    Args:
        profile: The profile being scanned with, whose ``default_tags`` and
            ``default_correspondent`` fill in for an unanswered control.
        tags: The chosen tag ids, or None when nobody was asked.
        correspondent: The chosen correspondent id, or None for none.  Read
            only when ``correspondent_given`` is true.
        correspondent_given: Whether the correspondent control was answered.

    Returns:
        The metadata to file the document with, repeated tag ids collapsed to
        their first place.

    """
    chosen_tags = profile.default_tags if tags is None else tags
    chosen_correspondent = (
        correspondent if correspondent_given else profile.default_correspondent
    )
    return ScanMetadata(
        tags=tuple(dict.fromkeys(chosen_tags)), correspondent=chosen_correspondent
    )
