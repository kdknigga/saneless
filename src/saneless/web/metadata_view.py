"""
The paperless-ngx lists one page shows: the tag list and the correspondent select.

Both lists come from paperless-ngx through the metadata cache, and every route
that renders one must fetch it with the same budget and show the same ticks the
same way.  A ticked id the list no longer names, and a list that could not be
loaded at all, are facts the page states rather than hides.

The lazy list load also re-renders the Scan button, so the job it is rendered
from is read here too, from the status view, in a way that never fails.
"""

from __future__ import annotations

import logging
import math
from functools import partial
from typing import TYPE_CHECKING, Final, Literal

import httpx2

from saneless.exceptions import ConfigError, PaperlessError, describe
from saneless.paperless import PROBE_CONNECT_SECONDS, PROBE_READ_SECONDS
from saneless.scan_metadata import metadata_ids
from saneless.vocabulary import (
    stale_default_correspondent_label,
    stale_default_tag_label,
    unlisted_correspondent_label,
    unlisted_tag_label,
)
from saneless.web import cache as web_cache
from saneless.web import owner, scan_block, status_view
from saneless.web.cache import MetadataUnavailableError
from saneless.web.job_view import JobView
from saneless.web.services import services

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request

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
    "metadata_scan_state",
    "no_correspondent_options",
    "no_tag_list",
    "tag_list_context",
]

logger = logging.getLogger(__name__)

REQUEST_FETCH_TIMEOUT: Final = httpx2.Timeout(
    PROBE_READ_SECONDS, connect=PROBE_CONNECT_SECONDS
)
"""
The budget for a list fetched to answer a request.

A page or a click must never wait on the client's own longer timeout, which
on a cold cache with paperless-ngx down would hold the lists and a profile
change open for half a minute or more.  These are the numbers the status
strip's probe uses to decide whether paperless-ngx is answering, so every list
route fetches with them; a list that does not answer is reported as not loaded.
"""

METADATA_RETRY_FLOOR_SECONDS: Final = 5
"""
The shortest interval at which a list that could not be loaded asks again.

Without a floor, a short or zero cache TTL would have every open page ask
paperless-ngx for both lists about once a second while it is down:
unauthenticated, unbounded upstream traffic, each request holding a worker
thread.  Read at call time, not bound where it is used.
"""

# The only metadata resources the cache holds.  A runtime alias, not a
# TYPE_CHECKING import, because FastAPI reads it to validate the ``resource``
# query parameter: anything else is a 422 instead of reaching the cache.
MetadataResource = Literal["tags", "correspondents"]


def _lock_wait(timeout: httpx2.Timeout) -> float:
    """
    Say how long a request waits its turn to fetch, given its fetch budget.

    A request queued behind another's fetch of the same list waits at most as
    long as that fetch may take, then answers the list unavailable.

    """
    return (timeout.connect or 0.0) + (timeout.read or 0.0)


# How the web tier logs a Paperless client exception, here and in web/cache.py.
# A PaperlessError or ConfigError is logged by its message, which the client
# builds free of credentials; anything else by class name only and without a
# traceback, because third-party exception text can carry a URL, a header or a
# token.
def cached_list_or_none(
    cache: MetadataCache,
    paperless: PaperlessClient,
    resource: MetadataResource,
    *,
    timeout: httpx2.Timeout,
) -> CachedList | None:
    """
    Retrieve metadata from cache or paperless-ngx, or None when it is unknown.

    The cache's single-flight ``get_or_fetch_list`` makes one Paperless call for
    concurrent requests, and serves the last good list, marked not current, when
    a refresh fails.  None and an empty list are different facts: an empty list
    is "no tags yet", None is "could not be loaded".  A failure the cache still
    remembers, or a wait past the budget, answers None without logging, because
    the failure was logged once, when it happened.

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

    Only a current list proves a missing id gone (stale); with no list, or only
    the last good copy, it is unlisted.  Either way the row stays ticked, so the
    untouched submit carries what the form shows and the scan decides.

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

    The filter request carries the ticked ids, so every one is re-rendered
    ticked and the ones the filter excludes are pinned above the list: a tick
    cannot leave the DOM, so it cannot be dropped from the next submit.  A
    ticked id the list does not name is pinned the same way, as a stale or
    unlisted row.

    ``q`` is a Python-side substring test over the cached list (ASVS 4.0.3
    V5): it never reaches a paperless-ngx query URL and is absent from the
    returned context, so it cannot be echoed into the page.

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
    # With ``[web] show_tags`` off the tag markup is never emitted, so a fetch
    # buys nothing.  The guard sits here rather than in ``index`` so it covers
    # every call site.
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
    # fetch would be a token-bearing request for data nobody can be shown.  The
    # guard sits here, as the tag list's does, so it covers every caller.
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
    cache's own TTL when that is shorter.  The retry counts from the last
    answer, which lands after the failure was remembered, so the next ask is
    past that memory.  Rounded up for htmx's trigger, never under
    ``METADATA_RETRY_FLOOR_SECONDS``, and both constants are read at call time.

    Args:
        ttl: The configured ``paperless_cache_ttl_seconds``.

    Returns:
        The interval at which a list rendered unavailable asks again.

    """
    return max(
        METADATA_RETRY_FLOOR_SECONDS,
        math.ceil(min(ttl, web_cache.NEGATIVE_TTL_SECONDS)),
    )


def metadata_scan_state(request: Request) -> tuple[JobView | None, bool]:
    """
    Read what the lazy list load's Scan button is rendered from, never failing.

    The job comes from the status context the page builds, so an active job
    still disables the button.  The loader polls until answered, and an error
    would land in the alert slot on every tick, so a failure is logged and the
    button rendered as though no job ran, keeping the blocked verdict; the
    status poll corrects it once the store reads again.

    Args:
        request: The incoming request.

    Returns:
        The job view, or None, and whether the appliance blocks a scan.

    """
    svc = services(request)
    try:
        live = status_view.status_context(
            svc.worker,
            svc.job_store,
            status_view.status_facts(
                request,
                followed_job_id=status_view.owned_active_job_id(
                    svc.worker, svc.job_store, owner.presented_owner(request)
                ),
            ),
        )
    except Exception:
        logger.exception("Failed to read the job for the lazy list load")
        return None, scan_block.block_for(svc.settings) is not None
    job = live["job"]
    return (job if isinstance(job, JobView) else None), bool(live["scan_blocked"])
