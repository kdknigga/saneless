"""
The paperless-ngx lists one page shows: the tag list and the correspondent select.

Both lists come from paperless-ngx through the metadata cache, and every
route that renders one -- the full page, the filter, both refreshes, the
profile-change swaps, the lazy list load and its retry -- must fetch it with
the same budget and show the same ticks the same way.  A ticked id the list
no longer names, and a list that could not be loaded at all, are facts the
page states rather than hides.  The contexts are built here, so the handlers
only choose which one to render.
"""

from __future__ import annotations

import logging
import math
from functools import partial
from typing import TYPE_CHECKING, Final, Literal

import httpx2

from saneless.exceptions import ConfigError, PaperlessError, describe
from saneless.scan_metadata import metadata_ids
from saneless.scanner.saned_probe import PROBE_CONNECT_SECONDS, PROBE_READ_SECONDS
from saneless.vocabulary import (
    stale_default_correspondent_label,
    stale_default_tag_label,
    unlisted_correspondent_label,
    unlisted_tag_label,
)
from saneless.web import cache as web_cache
from saneless.web.cache import MetadataUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable

    from saneless.paperless import PaperlessClient
    from saneless.web.cache import CachedList, MetadataCache
    from saneless.web.services import Services

__all__ = [
    "METADATA_RETRY_FLOOR_SECONDS",
    "REQUEST_FETCH_TIMEOUT",
    "MetadataResource",
    "cached_list_or_none",
    "correspondent_options_context",
    "metadata_retry_seconds",
    "no_correspondent_options",
    "no_tag_list",
    "tag_list_context",
]

logger = logging.getLogger(__name__)

REQUEST_FETCH_TIMEOUT: Final = httpx2.Timeout(
    PROBE_READ_SECONDS, connect=PROBE_CONNECT_SECONDS
)
"""
The budget for a list fetched to answer a request: 2 s to connect, 5 s to read.

A page or a click must never wait on the client's own 30 s per page.  On a cold
cache with paperless-ngx slow or down that would hold the tag list, the
correspondent select and a profile change open for half a minute or more, and
the page is only told what the lists hold once they answer.  The status strip's
probe already decides whether paperless-ngx is answering with these two
numbers, so they are the codebase's one measure of "answering", and every list
route fetches with them: the tag list and its filter, the correspondent
options, both refreshes, both profile-change swaps, the lazy list load and
the retry of a list that could not be loaded.  A list that does not answer
within them is reported as not loaded, and is asked again once the cache's
short memory of the failure has run out.
"""

METADATA_RETRY_FLOOR_SECONDS: Final = 5
"""
The shortest interval at which a list that could not be loaded asks again.

The retry follows the cache's memory of a failure, which is never longer than
``paperless_cache_ttl_seconds`` and is nothing at all with the cache disabled.
Without a floor, a short or zero TTL would have every open page ask
paperless-ngx for both lists about once a second for as long as it is down or
refusing one of them: unauthenticated, unbounded upstream traffic, each request
holding a worker thread for up to the connect budget.  Five seconds still lets
a page left open recover soon after paperless-ngx is back.  Read at call time,
not bound where it is used.
"""

# The only metadata resources the cache holds.  A runtime alias, not a
# TYPE_CHECKING import, because FastAPI reads it to validate the ``resource``
# query parameter: anything else is a 422 instead of reaching the cache.
MetadataResource = Literal["tags", "correspondents"]


def _lock_wait(timeout: httpx2.Timeout) -> float:
    """
    Say how long a request waits its turn to fetch, given its fetch budget.

    A request queued behind another request's fetch of the same list waits at
    most as long as that fetch may take to connect and read, then answers the
    list unavailable.  Every list fetch made to answer a request has a budget,
    so no request waits as long as it takes.

    Args:
        timeout: The request's fetch budget.

    Returns:
        The seconds to wait for the cache's per-key lock.

    """
    return (timeout.connect or 0.0) + (timeout.read or 0.0)


# How the web tier logs an exception from the Paperless client, here, in
# routes.paperless_test and in web/cache.py: the client-exception rule.  A
# PaperlessError or ConfigError is logged by its message, which the client
# builds from fixed words, the credential-free display URL and a token-redacted
# reason.  Anything else on those paths is logged by class name only and
# without a traceback, because third-party exception text can carry a URL, a
# header or a token.  Tracebacks remain only for failures of saneless's own
# store and templates, which never receive a client exception.
def cached_list_or_none(
    cache: MetadataCache,
    paperless: PaperlessClient,
    resource: MetadataResource,
    *,
    timeout: httpx2.Timeout,
) -> CachedList | None:
    """
    Retrieve metadata from cache or paperless-ngx, or None when it is unknown.

    The fetch goes through the cache's single-flight ``get_or_fetch_list``, so
    concurrent requests for the same resource make one Paperless call.  When
    a refresh fails, the cache serves the last list fetched successfully,
    marked as not current; only when there has never been one does the error
    reach this function, which logs its cause and answers None.  None and an
    empty list are different facts: paperless-ngx that has no tags can prove a
    ticked id gone, and one that could not be asked cannot.  Nor can the last
    good copy, which predates anything created or deleted since.  The page
    says which it is: an empty list is "no tags yet", None is "could not be
    loaded".

    The cache remembers a failure with no last good copy for a short while.
    A request inside that window, or one that waited longer than its budget
    for another request's fetch, gets ``MetadataUnavailableError`` without a
    fetch, and answers None without logging: the failure was logged once,
    when it happened.

    Args:
        cache: Metadata cache instance.
        paperless: Paperless-ngx API client.
        resource: Resource name ('tags' or 'correspondents').
        timeout: The fetch budget on a miss, which also bounds the wait for
            another request's fetch.

    Returns:
        The list, fresh or the last good one, with whether it is current, or
        None when neither exists.

    """
    getter = paperless.get_tags if resource == "tags" else paperless.get_correspondents
    fetch = partial(getter, timeout=timeout)
    try:
        return cache.get_or_fetch_list(
            resource, fetch, lock_timeout=_lock_wait(timeout)
        )
    except MetadataUnavailableError:
        return None
    except (PaperlessError, ConfigError) as exc:
        logger.warning(
            "Failed to fetch %s from paperless-ngx, answering unavailable: %s",
            resource,
            describe(exc),
        )
    except Exception as exc:
        logger.warning(
            "Failed to fetch %s from paperless-ngx, answering unavailable: %s",
            resource,
            type(exc).__name__,
        )
    return None


def _unnamed_rows(
    ticked: list[int],
    known_ids: frozenset[int] | None,
    *,
    proven: bool,
    stale_label: Callable[[int], str],
    unlisted_label: Callable[[int], str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """
    Split the ticked ids the list does not name into stale and unlisted rows.

    A list read from paperless-ngx just now that lacks an id proves it gone,
    so that id is stale and its label says it will be skipped.  With no list
    at all nothing is proved, so every ticked id is unlisted and labelled by
    number alone.  Nor does the last good copy prove anything, served while
    paperless-ngx cannot be reached: the scan will send the id unchecked, and
    the id may be newer than the copy, so an id it lacks is unlisted too.
    Either way the row stays ticked: the untouched submit has to carry what
    the form shows, and the scan decides what to drop.

    Args:
        ticked: The ticked ids, in order and without repeats.
        known_ids: The ids the list holds, or None when it is unknown.
        proven: Whether the list is current, and so can prove an id gone.
        stale_label: Labels an id a current list lacks.
        unlisted_label: Labels an id nothing can prove gone.

    Returns:
        The stale rows and the unlisted rows, each ``{"id", "label"}``.

    """
    if known_ids is None:
        return [], [{"id": item, "label": unlisted_label(item)} for item in ticked]
    missing = [item for item in ticked if item not in known_ids]
    if not proven:
        return [], [{"id": item, "label": unlisted_label(item)} for item in missing]
    return [{"id": item, "label": stale_label(item)} for item in missing], []


def no_tag_list() -> dict[str, object]:
    """
    Return the tag list's context with no list in it and nothing ticked.

    The key set is ``tag_list_context``'s, emptied.  A hidden list renders
    nothing from it, and the page, which loads its lists after it renders,
    shows a loading line in their place.  Both spread it with ``**``, so a
    missing key would leave an undefined name in a template.

    Returns:
        The emptied context: no rows, nothing ticked, and no verdict on
        whether the list could be loaded, because nobody asked.

    """
    return {
        "stale": [],
        "unlisted": [],
        "pinned": [],
        "tags": [],
        "selected_tags": set(),
        "any_tags": False,
        "tags_unavailable": False,
        "tags_retry_seconds": None,
    }


def no_correspondent_options(*, shown: bool) -> dict[str, object]:
    """
    Return the correspondent options' context with no list and no choice.

    The key set is ``correspondent_options_context``'s, emptied, for the
    same two callers as ``no_tag_list``: a hidden control, and the page,
    whose select holds only the option that means none until the list
    arrives.

    Args:
        shown: Whether the control is on the page.  It rides along so the
            partials leave out the help line a hidden control does not have.

    Returns:
        The emptied context.

    """
    return {
        "correspondents": [],
        "selected_correspondent": None,
        "extra_option": None,
        "correspondents_unavailable": False,
        "correspondents_retry_seconds": None,
        "show_correspondent": shown,
    }


def tag_list_context(
    svc: Services,
    *,
    q: str,
    selected: list[int],
    timeout: httpx2.Timeout,
) -> dict[str, object]:
    """
    Build the tag checkbox list's context: the filtered list and pinned ticks.

    Two lists, not one, and that is the whole design.  The filter
    request carries the currently ticked ids with it -- ``hx-include`` over a
    checkbox list gathers only the boxes that are checked -- so this function
    can re-render every one of them ticked, and pin the ones the filter
    excludes *above* the filtered list.  A tick therefore cannot leave the DOM,
    and a tick that cannot leave the DOM cannot be silently dropped from the
    next submit.  A tag that is both ticked and matched is rendered by the
    filtered loop alone, so it appears once rather than twice.

    A ticked id the list does not name is kept the same way, as a stale or an
    unlisted row pinned first (see ``_unnamed_rows``).  That is what lets the
    page open on a profile's default tags even when one of them has since
    been deleted, or paperless-ngx cannot be reached.

    ``q`` is a Python-side substring test over the already-cached list and
    nothing else (ASVS 4.0.3 V5.1.1).  It is never interpolated into a paperless-ngx
    query URL -- the cache holds the whole list, so there is nothing to ask
    upstream and the filter costs no request at all -- and it is
    deliberately absent from the context this returns, so it cannot be echoed
    back into the page.  Its length is already bounded by the route's
    ``max_length`` before this runs.

    Args:
        svc: The app's services, for the metadata cache and Paperless client.
        q: The filter text, matched case-insensitively against tag names.
        selected: The tag ids the request reports as currently ticked.
        timeout: The fetch budget on a cache miss.

    Returns:
        The context ``partials/tags.html`` renders: the stale and unlisted
        ticks, the pinned ticks, the filtered list, the ticked ids, whether
        any tag exists at all, whether the list could not be loaded, and how
        often it then asks again.

    """
    # With ``[web] show_tags`` off the tag markup is never emitted, so
    # a fetch here buys nothing and costs a paperless-ngx round trip on every
    # cold-cache page load -- and the flag that would hide the list is the same
    # flag that decides whether the data can ever be seen.  The guard sits in
    # this function rather than in ``index`` so it covers every call site,
    # including the filter, refresh and profile-change routes, which have the
    # same reason to skip.  The key set is the normal path's, emptied (see
    # ``no_tag_list``).
    if not svc.settings.web.show_tags:
        return no_tag_list()
    cached = cached_list_or_none(svc.cache, svc.paperless, "tags", timeout=timeout)
    listed = cached.rows if cached is not None else None
    # The same rule the pre-scan check applies, so the page and the scan
    # agree on which ids paperless-ngx still has.
    known_ids = metadata_ids(listed)
    everything = listed if listed is not None and known_ids is not None else []
    ticked = list(dict.fromkeys(selected))
    stale, unlisted = _unnamed_rows(
        ticked,
        known_ids,
        proven=cached is not None and cached.current,
        stale_label=stale_default_tag_label,
        unlisted_label=unlisted_tag_label,
    )
    needle = q.casefold()
    matched = [
        tag for tag in everything if needle in str(tag.get("name", "")).casefold()
    ]
    matched_ids = {tag.get("id") for tag in matched}
    pinned = [
        tag
        for tag in everything
        if tag.get("id") in ticked and tag.get("id") not in matched_ids
    ]
    return {
        "stale": stale,
        "unlisted": unlisted,
        "pinned": pinned,
        "tags": matched,
        # Not ``selected``: the index context already uses that name for the
        # profile the page opens on, and an include shares its parent's
        # context, so the two would collide on the full-page render.
        "selected_tags": set(ticked),
        # Which empty state to render when both lists are empty: "paperless-ngx
        # has no tags" and "your filter matched none of them" are different
        # facts and only one of them is the reader's to fix.
        "any_tags": bool(everything),
        # A third fact, and not an empty state at all: paperless-ngx could not
        # be asked and there is no last good copy.  The list says so instead
        # of claiming there are no tags, and still shows the ticked ids.
        "tags_unavailable": known_ids is None,
        # How often the list, rendered unavailable, asks whether it can be
        # loaded now (see ``probe_metadata``).  Named for the list, because
        # the lazy list load spreads both lists' contexts into one.
        "tags_retry_seconds": metadata_retry_seconds(
            svc.settings.output.paperless_cache_ttl_seconds
        ),
    }


def correspondent_options_context(
    svc: Services, selected: int | None, *, timeout: httpx2.Timeout
) -> dict[str, object]:
    """
    Build the correspondent options' context, with one of them chosen.

    The full page, the profile-change swap and the refresh all render the
    options through this, so each shows the same choice the same way.  A
    chosen id the list does not name becomes one extra option, selected,
    labelled stale or unlisted by the rule the tag list uses: the untouched
    submit carries it, and the scan decides whether it is dropped.

    Args:
        svc: The app's services, for the metadata cache and Paperless client.
        selected: The correspondent id to show chosen, or None for none.
        timeout: The fetch budget on a cache miss.

    Returns:
        The context ``partials/correspondents.html`` renders: the list, the
        chosen id, the extra option or None, whether the list could not be
        loaded and how often it then asks again, and whether the control is
        shown at all.

    """
    # With ``[web] show_correspondent`` off the select is never emitted, so a
    # fetch buys nothing, and it is a token-bearing request for data nobody
    # can be shown.  The guard sits here, as the tag list's does, so it covers
    # every caller: the options, the refresh, the profile change and the
    # lazy list load.  The keys are the normal path's, emptied (see
    # ``no_correspondent_options``).
    if not svc.settings.web.show_correspondent:
        return no_correspondent_options(shown=False)
    cached = cached_list_or_none(
        svc.cache, svc.paperless, "correspondents", timeout=timeout
    )
    listed = cached.rows if cached is not None else None
    known_ids = metadata_ids(listed)
    correspondents = listed if listed is not None and known_ids is not None else []
    extra_option: dict[str, object] | None = None
    if selected is not None:
        stale, unlisted = _unnamed_rows(
            [selected],
            known_ids,
            proven=cached is not None and cached.current,
            stale_label=stale_default_correspondent_label,
            unlisted_label=unlisted_correspondent_label,
        )
        extra_option = next(iter(stale + unlisted), None)
    return {
        "correspondents": correspondents,
        "selected_correspondent": selected,
        "extra_option": extra_option,
        # Read by the help line under the select, which says the list could
        # not be loaded; a select cannot hold a sentence.
        "correspondents_unavailable": known_ids is None,
        "correspondents_retry_seconds": metadata_retry_seconds(
            svc.settings.output.paperless_cache_ttl_seconds
        ),
        "show_correspondent": True,
    }


def metadata_retry_seconds(ttl: float) -> int:
    """
    Say how often a list that could not be loaded asks again, in seconds.

    No sooner than the cache forgets the failure: the negative TTL, or the
    cache's own TTL when that is shorter.  The retry replaces itself with
    every answer, so it asks again this long after the last answer landed
    (see ``partials/list_retry.html``).  The cache remembers a failure from
    when the failed fetch ended, before that answer, so the next ask is past
    the memory its own last fetch left, and asks paperless-ngx unless
    another request asked meanwhile.  Rounded up to whole seconds for htmx's
    trigger, and never under ``METADATA_RETRY_FLOOR_SECONDS``, so a short or
    disabled cache cannot make every open page ask paperless-ngx once a
    second.  A floor longer than the memory only leaves the next ask further
    past it.  Both constants are read here, at call time, not bound when the
    module loads.

    Args:
        ttl: The configured ``paperless_cache_ttl_seconds``.

    Returns:
        The interval at which a list rendered unavailable asks again.

    """
    return max(
        METADATA_RETRY_FLOOR_SECONDS,
        math.ceil(min(ttl, web_cache.NEGATIVE_TTL_SECONDS)),
    )
