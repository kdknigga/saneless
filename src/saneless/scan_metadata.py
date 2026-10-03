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

It is also where those ids are checked against paperless-ngx before a scan
starts (:func:`check_scan_metadata`), so an id that no longer exists is
dropped with a warning rather than discovered when the upload is refused.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol

from saneless.exceptions import ConfigError, PaperlessError, describe
from saneless.vocabulary import dropped_ids_warning

if TYPE_CHECKING:
    from collections.abc import Sequence

    from saneless.config import ProfileConfig

__all__ = [
    "ClientMetadataLookup",
    "MetadataKind",
    "MetadataLookup",
    "MetadataSource",
    "ScanMetadata",
    "check_scan_metadata",
    "fetch_metadata",
    "metadata_ids",
    "resolve_scan_metadata",
]

logger = logging.getLogger(__name__)

# The budget of each fetch made to check ids before a scan.  Shorter than the
# client's own timeout, because it is paid before any paper moves; when it
# runs out the ids go unchecked.
_VALIDATION_TIMEOUT_SECONDS: Final = 5.0

type MetadataKind = Literal["tags", "correspondents"]


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


class MetadataSource(Protocol):
    """The two paperless-ngx client calls the id check reads."""

    def get_tags(self, *, timeout: float | None = None) -> list[dict[str, object]]:
        """Return paperless-ngx's tags, each a dict with at least an ``id``."""
        ...

    def get_correspondents(
        self, *, timeout: float | None = None
    ) -> list[dict[str, object]]:
        """Return paperless-ngx's correspondents, each a dict with an ``id``."""
        ...


class MetadataLookup(Protocol):
    """
    Where the id check learns which ids paperless-ngx has.

    Each method returns the ids of one list, or None when the list cannot be
    read -- paperless-ngx unreachable, the request refused, or an answer that
    is not a list.  None means "cannot tell", never "none exist", so only a
    list actually read can show an id to be stale.  ``fresh`` asks for the list
    as paperless-ngx has it now, not as a cache last saw it.
    """

    def tag_ids(self, *, fresh: bool) -> frozenset[int] | None:
        """Return the ids of paperless-ngx's tags, or None when unknown."""
        ...

    def correspondent_ids(self, *, fresh: bool) -> frozenset[int] | None:
        """Return the ids of paperless-ngx's correspondents, or None when unknown."""
        ...


def fetch_metadata(
    source: MetadataSource, kind: MetadataKind
) -> list[dict[str, object]] | None:
    """
    Fetch one metadata list for the id check, with the check's short timeout.

    Args:
        source: The paperless-ngx client.
        kind: Which list to fetch.

    Returns:
        The list as paperless-ngx sent it, or None when the fetch raised
        (logged) or the answer was not a list.

    """
    fetch = source.get_tags if kind == "tags" else source.get_correspondents
    try:
        items: object = fetch(timeout=_VALIDATION_TIMEOUT_SECONDS)
    except (PaperlessError, ConfigError) as exc:
        logger.warning(
            "Could not read %s from paperless-ngx to check this scan's ids; "
            "they are sent unchecked: %s",
            kind,
            describe(exc),
        )
        return None
    except Exception as exc:
        # Anything else from the client, such as a RecursionError from a deeply
        # nested hostile body, still only means the ids cannot be checked.
        # Logged by class name alone: third-party text can carry a URL, a
        # header or the token.  ScanInterrupted is a BaseException, so a
        # signal still gets through.
        logger.warning(
            "Could not read %s from paperless-ngx to check this scan's ids; "
            "they are sent unchecked: %s",
            kind,
            type(exc).__name__,
        )
        return None
    if not isinstance(items, list):
        return None
    rows: list[dict[str, object]] = items
    return rows


def metadata_ids(items: object) -> frozenset[int] | None:
    """
    Return the ids a metadata list holds, or None when it is not a list.

    Only an ``int`` ``id`` on a dict entry counts; a bool, a string or an entry
    that is not a dict is ignored, so nothing but data can vouch for an id.

    Args:
        items: A list as paperless-ngx or a cache holds it.

    Returns:
        The ids, empty for an empty list, or None for anything but a list.

    """
    if not isinstance(items, list):
        return None
    ids: set[int] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        value: object = item.get("id")
        if isinstance(value, int) and not isinstance(value, bool):
            ids.add(value)
    return frozenset(ids)


class ClientMetadataLookup:
    """
    The id check's lookup straight from the client, as ``saneless scan`` uses it.

    Each list is fetched at most once: the first fetch is already as fresh as
    paperless-ngx can make it, so the check's refetch reuses it rather than
    asking again a moment later.  A failed fetch is not remembered.

    Args:
        client: The paperless-ngx client.

    """

    def __init__(self, client: MetadataSource) -> None:
        """Keep the client; nothing is fetched until a list is asked for."""
        self._client = client
        self._read: dict[MetadataKind, frozenset[int]] = {}

    def tag_ids(self, *, fresh: bool) -> frozenset[int] | None:
        """Return the ids of paperless-ngx's tags, or None when unknown."""
        del fresh  # Every fetch this lookup makes is fresh.
        return self._ids("tags")

    def correspondent_ids(self, *, fresh: bool) -> frozenset[int] | None:
        """Return the ids of paperless-ngx's correspondents, or None when unknown."""
        del fresh  # Every fetch this lookup makes is fresh.
        return self._ids("correspondents")

    def _ids(self, kind: MetadataKind) -> frozenset[int] | None:
        """Return one list's ids, fetching it the first time it is read."""
        known = self._read.get(kind)
        if known is not None:
            return known
        ids = metadata_ids(fetch_metadata(self._client, kind))
        if ids is not None:
            self._read[kind] = ids
        return ids


class _IdReader(Protocol):
    """One of a lookup's two methods, bound."""

    def __call__(self, *, fresh: bool) -> frozenset[int] | None:
        """Return one list's ids, or None when unknown."""
        ...


def _missing(ids: Sequence[int], read: _IdReader) -> frozenset[int]:
    """
    Return the ids a list does not hold, looking once more before saying so.

    Args:
        ids: The ids to look for.
        read: The lookup's method for the list they belong to.

    Returns:
        The ids missing from the list even after a fresh read, or none when
        either read could not tell.

    """
    wanted = frozenset(ids)
    known = read(fresh=False)
    if known is None or wanted <= known:
        return frozenset()
    # A miss may only mean the first answer was old: one fresh look decides.
    known = read(fresh=True)
    if known is None:
        return frozenset()
    return wanted - known


def check_scan_metadata(
    metadata: ScanMetadata, lookup: MetadataLookup
) -> tuple[ScanMetadata, str | None]:
    """
    Drop the ids paperless-ngx no longer has, before anything is scanned.

    A scan with no tags and no correspondent is returned as it is, and nothing
    is asked of paperless-ngx.  Otherwise each list is read once, and when an
    id is missing from it, once more, fresh.  An id still missing after that
    is dropped: the scan goes ahead without it, and the warning names it.

    Only a list actually read can show an id to be stale.  When either read
    cannot tell -- paperless-ngx unreachable, the request refused, an answer
    that is not a list -- the ids pass unchecked and the upload decides.

    Known limitation: paperless-ngx lists only the tags and correspondents
    the token's user may see, so an id that exists but is hidden from that
    user is judged stale and dropped, although the upload would have accepted
    it.  The same user cannot pick it on the web form either.

    Args:
        metadata: The metadata the scan was asked for.
        lookup: Where paperless-ngx's ids are read from.

    Returns:
        The metadata to file the document with, and the warning naming what
        was dropped, or None when nothing was.

    """
    if not metadata.tags and metadata.correspondent is None:
        return metadata, None
    stale_tags = (
        _missing(metadata.tags, lookup.tag_ids) if metadata.tags else frozenset()
    )
    correspondent = metadata.correspondent
    stale_correspondent = False
    if correspondent is not None:
        missing = _missing((correspondent,), lookup.correspondent_ids)
        stale_correspondent = correspondent in missing
    dropped_tags = tuple(tag for tag in metadata.tags if tag in stale_tags)
    warning = dropped_ids_warning(
        dropped_tags, correspondent if stale_correspondent else None
    )
    if warning is None:
        return metadata, None
    checked = ScanMetadata(
        tags=tuple(tag for tag in metadata.tags if tag not in stale_tags),
        correspondent=None if stale_correspondent else correspondent,
    )
    return checked, warning
