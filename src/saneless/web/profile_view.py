"""
The scan form's profile choices and the Job History rows, as one browser sees them.

The Profile select's options, their order, their labels and the option the
page opens on are decided here, from the worker's profile set, so the full
page and every later render agree on them.  The Multiple pages field's context
and the history rows are built here too: the full page and the routes that
re-render each one read the same builder, so the two cannot drift.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from saneless.job import WEB_HISTORY_LIMIT
from saneless.scanner.base import SourceKind, classify_source
from saneless.vocabulary import (
    MULTI_PAGE_DISABLED_REASON,
    MULTI_PAGE_HELP,
    MULTI_PAGE_LABEL,
)
from saneless.web import owner
from saneless.web.job_view import build_job_view
from saneless.web.services import services

if TYPE_CHECKING:
    from starlette.requests import Request

    from saneless.config import ProfileConfig
    from saneless.web.job_view import JobView
    from saneless.worker import ScanWorker

__all__ = [
    "ProfileChoices",
    "ProfileOption",
    "history_views",
    "is_manual_duplex",
    "multi_page_field",
    "profile_options",
]


@dataclass(frozen=True, slots=True)
class ProfileOption:
    """
    One entry of the Profile select: what it submits and what it reads as.

    Attributes:
        name: The profile name, which is the ``value`` the option submits.
            It is the wire contract ``POST /api/scan`` already takes, so only
            the text a household member reads is new here.
        label: The human name shown in the dropdown.
        description: The sentence shown beneath the select for this profile.
        manual_duplex: Whether the profile scans both sides by the manual
            flip, which is what disables the Multiple pages checkbox when the
            page opens on it.  Read here, from the same locked lookup as the
            text, so the page never looks the profile up a second time.

    """

    name: str
    label: str
    description: str
    manual_duplex: bool = False


@dataclass(frozen=True, slots=True)
class ProfileChoices:
    """
    What the Profile select offers, and which option it opens on.

    Attributes:
        options: The options to render, in the order they are rendered.
        opening: The name of the option the page opens on: ``default`` when
            it is offered, else the profile standing in for a hidden
            ``default``, else the first option; ``""`` when there are none.

    """

    options: tuple[ProfileOption, ...]
    opening: str


# The profile ``saneless scan`` uses when no ``--profile`` is named, which is
# the one the page has to open on for an untouched form to mean the same.
_DEFAULT_PROFILE: Final = "default"


def profile_options(worker: ScanWorker) -> ProfileChoices:
    """
    Build the ordered option list the Profile select renders, and its opening.

    Feeder profiles come first on a sheet-fed device, and this is
    where ``has_flatbed`` is read: sheet-fed means the device reports no
    flatbed source, and at render time the server's evidence for that is the
    generated profile set, which mirrors the device's sources.  So the answer
    is derived from the profiles already in hand -- no new device probe and no
    new config key.

    The classification comes from ``classify_source`` and from nowhere else:
    its docstring states it is the only source-classification rule in the
    codebase and forbids re-deriving the answer from the string.  That matters
    most for the commonest real feeder name of all, whose first four letters
    are the whole of the automatic rule -- an exact match is what keeps it out
    of the single-page group, and this function inherits that answer rather
    than asking again.

    A generated ``default`` that equals another profile is not offered: it
    would be a second option scanning the same way under the same words, and
    the first profile it equals stands in for it, so the page still opens on
    what ``saneless scan`` with no ``--profile`` does.  The name stays valid
    everywhere else -- the CLI, the config and ``POST /api/scan`` all still
    take it.  Any label two offered options still share then gets the profile
    name after it, for every member of the group.

    Args:
        worker: The worker whose profile set is being rendered.

    Returns:
        One option per offered profile, feeder-first when the device is
        sheet-fed and in configuration order otherwise, and the name of the
        option the page opens on.

    """
    read: dict[str, ProfileConfig] = {}
    for name in worker.profile_names():
        profile = worker.get_profile(name)
        if profile is None:
            # Listing and looking up are two locked calls, so a profile
            # rewritten between them can be gone by the time it is read.  One
            # option fewer for one render is honest; a placeholder would not
            # be, and there is nothing to show under a name that no longer
            # names anything.
            continue
        read[name] = profile
    # Over every profile read, the hidden twin included: a twin shares its
    # stand-in's source, so leaving it out could never change the answer.
    sheet_fed = not any(
        classify_source(profile.source) is SourceKind.FLATBED
        for profile in read.values()
    )
    stand_in = _default_stand_in(read)
    if stand_in is not None:
        del read[_DEFAULT_PROFILE]
    entries = [
        (
            ProfileOption(
                name=name,
                # A deployed config whose profiles predate the ``label`` key
                # carries an empty human name: startup generation only runs on
                # a bare default config, so it is skipped there, and a blank
                # option is worse than a raw profile name.  ``saneless
                # auto-profiles --force`` is what backfills the text, and the
                # how-to says so.
                label=profile.label or name,
                description=profile.description,
                manual_duplex=is_manual_duplex(profile),
            ),
            classify_source(profile.source),
        )
        for name, profile in read.items()
    ]
    if sheet_fed:
        # A stable sort, so configuration order survives inside each group and
        # the only thing this changes is which group comes first.
        entries.sort(key=lambda entry: not entry[1].uses_feeder)
    options = _distinct_labels(tuple(option for option, _ in entries))
    names = [option.name for option in options]
    if _DEFAULT_PROFILE in names:
        opening = _DEFAULT_PROFILE
    elif stand_in is not None:
        opening = stand_in
    else:
        opening = names[0] if names else ""
    return ProfileChoices(options=options, opening=opening)


def _default_stand_in(profiles: dict[str, ProfileConfig]) -> str | None:
    """
    Name the profile a generated ``default`` twin is hidden behind, if any.

    The whole profile is compared rather than a hand-picked subset of its
    fields, the rule ``is_bare_default`` follows, so a field added to the
    model later cannot be silently left out of the comparison, and any edit
    an operator makes to ``default`` -- default tags, a title, a mode --
    shows it again.  Only a generated ``default`` is hidden: one an operator
    wrote is theirs to show, even when it happens to match another profile.

    Args:
        profiles: The profiles read for this render, in configuration order.

    Returns:
        The first profile in configuration order that equals a generated
        ``default``, or None when ``default`` is absent, hand-written or
        equal to no other profile.

    """
    default = profiles.get(_DEFAULT_PROFILE)
    if default is None or not default.auto_generated:
        return None
    return next(
        (
            name
            for name, profile in profiles.items()
            if name != _DEFAULT_PROFILE and profile == default
        ),
        None,
    )


def _distinct_labels(
    options: tuple[ProfileOption, ...],
) -> tuple[ProfileOption, ...]:
    """
    Append the profile name to every label two or more options share.

    A household member must never see two options with the same text and no
    way to tell which is which.  Generated labels are already unique, but a
    hand-written config can still collide, and so can an edited ``default``
    beside the profile it was copied from.  Every member of a shared group is
    suffixed, not only the later ones, so none of them reads as the plain one.

    Args:
        options: The options in render order.

    Returns:
        The same options in the same order, with shared labels made distinct.

    """
    counts = Counter(option.label for option in options)
    return tuple(
        replace(option, label=f"{option.label} ({option.name})")
        if counts[option.label] > 1
        else option
        for option in options
    )


def is_manual_duplex(profile: ProfileConfig) -> bool:
    """
    Say whether ``profile`` scans both sides by the manual flip.

    The one place the web layer reads this, so the checkbox on the page, its
    refresh and the refusal of a submit all agree on which profiles it means.

    Args:
        profile: The chosen profile.

    Returns:
        True only for a manual-duplex profile.

    """
    return profile.duplex == "manual"


def multi_page_field(*, manual_duplex: bool, ticked: bool) -> dict[str, object]:
    """
    Build the context the Multiple pages field renders from.

    The full page and ``GET /api/profiles/multi-page`` both render the field
    through this, so the two cannot disagree about when it is disabled or what
    the line beneath it says.  The template composes no prose: the label and
    the help line or the reason come from the vocabulary.

    Args:
        manual_duplex: Whether the chosen profile is manual duplex, which
            disables the box and puts the reason in place of the help line.
        ticked: Whether the box was ticked before this render.  A full page
            load passes False, so the box is unticked on every load.

    Returns:
        The field's template context.

    """
    return {
        "multi_page_label": MULTI_PAGE_LABEL,
        "multi_page_disabled": manual_duplex,
        # A disabled box is never also ticked: it would not be submitted, so a
        # tick on it would show a choice the scan would not make.
        "multi_page_checked": ticked and not manual_duplex,
        "multi_page_help": (
            MULTI_PAGE_DISABLED_REASON if manual_duplex else MULTI_PAGE_HELP
        ),
    }


def history_views(request: Request) -> list[JobView]:
    """
    Return the Job History rows as the browser making this request may see them.

    One view per row, each decided by ``build_job_view`` from the token this
    request presents: a row this browser started keeps its real title, and
    every other row -- another browser's, or one that recorded no owner --
    shows the generic title.  Time, profile, outcome and page counts are the
    same for every viewer, so the table still shows the appliance is in use.
    The full page and ``GET /api/jobs/history`` both read the rows here, so
    the two cannot disagree about what a browser is shown.

    Args:
        request: The incoming request, for its owner token and the settings.

    Returns:
        The most recent jobs, newest first, as this browser's views.

    """
    svc = services(request)
    presented = owner.presented_owner(request)
    return [
        build_job_view(job, presented=presented, settings=svc.settings)
        for job in svc.job_store.list_recent(limit=WEB_HISTORY_LIMIT)
    ]
