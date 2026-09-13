"""
Automatic scanner profile generation.

Converts scanner device capabilities into ready-to-use scan profiles,
with comment-preserving TOML config file writing via tomlkit. All
generation logic uses pure functions for easy testing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import tomlkit

from saneless.config import DEFAULT_RESOLUTION, ProfileConfig, Settings
from saneless.scanner.base import SourceKind, classify_source

if TYPE_CHECKING:
    from saneless.scanner.base import DeviceCapabilities

__all__ = [
    "generate_profiles",
    "is_bare_default",
    "pick_closest_resolution",
    "pick_preferred_mode",
    "resolve_config_path",
    "source_to_slug",
    "write_profiles_to_config",
]

logger = logging.getLogger(__name__)


# The slug for a name of which _slugify's character set keeps nothing -- a
# whitespace-only or wholly punctuation source name. "source" is the domain's
# own word, so it cannot be mistaken for a device's wording, and it leaves the
# collision tie-break free to turn a second degenerate name into "source-2"
# rather than dropping it.
_EMPTY_SLUG_FALLBACK = "source"


def _slugify(source: str) -> str:
    """
    Reduce a source name to a strict ``[a-z0-9-]`` slug.

    The character set is the contract: the result holds only lowercase ASCII
    letters, digits and hyphens, with no leading, trailing or doubled hyphen.
    The rule, in order -- lowercase the name; replace every run of characters
    outside ``[a-z0-9]`` with a single hyphen; strip leading and trailing
    hyphens; fall back to ``_EMPTY_SLUG_FALLBACK`` when nothing survives.

    Lowercasing happens here rather than in the caller so no caller can pass a
    half-normalised string and get a slug that silently breaks the contract.

    This is deliberately NOT the PDF filename sanitiser, and the two must not
    be merged: Phase 23's D-19 separated them because this one passed "/" and
    ".." straight through. D-15 fixes that character-set weakness here; it does
    not make this function safe to reuse for filesystem paths.

    Args:
        source: SANE source name, in whatever case the device reported it.

    Returns:
        A slug matching ``^[a-z0-9][a-z0-9-]*$``.

    """
    slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
    return slug or _EMPTY_SLUG_FALLBACK


def source_to_slug(source: str) -> str:
    """
    Convert a SANE source name to a profile slug.

    Every source is named from the device's own wording (D-14). This function
    used to map each SourceKind onto one of four hard-coded friendly names,
    which forced a special case for "ADF Back". The reason is worth keeping:
    "which scan path do I take?" and "what do I name this profile?" are
    different questions with different equivalence classes. "ADF Front" and "ADF Back" are both feeders
    for routing, so naming them from the kind collapsed two distinct sources
    onto one profile and silently lost one (the N-09 defect). Naming from the
    source itself removes the naming question's need for a rule at all, so no
    source can be named after another source's kind.

    Two *different* names can still normalise alike ("ADF-Front" and
    "ADF Front"). That residue is resolved where profiles are assembled, with a
    deterministic tie-break -- not here, so this function stays pure.

    Args:
        source: SANE source name string (e.g., "Flatbed", "ADF Duplex").

    Returns:
        A lowercase slug matching ``^[a-z0-9][a-z0-9-]*$``.

    """
    return _slugify(source)


def _snap_into_range(target: int, resolution_range: tuple[float, float, float]) -> int:
    """
    Clamp a target into a reported range and snap it onto the range's step.

    Clamping alone is not enough. A range says the device accepts values from
    its minimum to its maximum *in increments of its step*, so a value inside
    the span but off the grid is still one the device never offered, and SANE
    would silently substitute something else for it.

    A step of zero is not a defect to guard against but a documented SANE
    meaning -- the range is continuous and any value within it is acceptable --
    so it clamps without snapping rather than dividing by zero.

    Args:
        target: The preferred resolution, in dpi.
        resolution_range: The ``(min, max, step)`` the device reported.

    Returns:
        A whole-dpi resolution the device could actually accept. The device
        reports its bounds as floats; the coercion to int happens here, at the
        point of use, rather than when the constraint was read.

    """
    low, high, step = resolution_range
    clamped = min(max(float(target), low), high)
    if step > 0:
        # Snap relative to the minimum, which is where the grid starts.
        clamped = min(max(low + round((clamped - low) / step) * step, low), high)
    # round() on a float already yields an int, which is the coercion this
    # function exists to perform.
    return round(clamped)


def pick_closest_resolution(
    resolutions: list[int],
    target: int = DEFAULT_RESOLUTION,
    resolution_range: tuple[float, float, float] | None = None,
) -> int:
    """
    Pick the resolution closest to target from what the device actually offers.

    A device constrains its resolution option with *either* a word list *or* a
    ``(min, max, step)`` range, so the two arguments are alternatives rather
    than two spellings of one fact. A word list is an exhaustive enumeration and
    wins when present: ``min(..., key=absolute difference)`` is already exactly
    right for it.

    The range branch is what closes N-01. This function used to return the
    target unchanged whenever the list was empty -- which is precisely what a
    range-reporting device produces -- so such a device was asked for 300 dpi
    regardless of what it supported, and a device whose ceiling sat below 300
    got a resolution it had never advertised.

    Args:
        resolutions: The exact resolutions the device offers, when it reported
            a word list.
        target: Preferred resolution (defaults to DEFAULT_RESOLUTION DPI).
        resolution_range: The ``(min, max, step)`` the device reported, when it
            constrained the option with a range instead.

    Returns:
        A resolution the device could accept, or target if the device
        constrained the option in neither way.

    """
    if resolutions:
        return min(resolutions, key=lambda r: abs(r - target))
    if resolution_range is not None:
        return _snap_into_range(target, resolution_range)
    return target


def pick_preferred_mode(
    modes: list[str],
    preferred: str = "Color",
) -> str:
    """
    Pick preferred scan mode, falling back to first available.

    Args:
        modes: Available scan modes from scanner.
        preferred: Preferred mode name (defaults to "Color").

    Returns:
        The matched mode string, or first available, or preferred if empty.

    """
    for mode in modes:
        if mode.lower() == preferred.lower():
            return mode
    return modes[0] if modes else preferred


def is_bare_default(settings: Settings) -> bool:
    """
    Check if settings have only the uncustomized default profile.

    Returns True only when there is exactly one profile named "default"
    with all default field values and auto_generated is False.

    Args:
        settings: Application settings to inspect.

    Returns:
        True if the profile set is the bare uncustomized default.

    """
    if len(settings.profiles) != 1:
        return False
    if "default" not in settings.profiles:
        return False
    default = settings.profiles["default"]
    bare = ProfileConfig()
    return (
        default.source == bare.source
        and default.resolution == bare.resolution
        and default.mode == bare.mode
        and not default.auto_generated
    )


def _claim_slug(source: str, claimed: dict[str, str]) -> str:
    """
    Resolve a source's slug against the slugs already claimed.

    Slugging is not injective -- "ADF-Front" and "ADF Front" are different
    source names that normalise to the same slug -- so the assignment needs a
    tie-break. The first source to claim a slug keeps it bare; each later
    collider gains a "-2", "-3", ... suffix. "Last wins" is rejected: it
    silently drops a source the device reported, which is N-09's actual
    complaint.

    The walk follows ``capabilities.sources`` order, so the result is
    deterministic given the device's own stable ordering of its sources.

    Args:
        source: The SANE source name claiming a slug.
        claimed: Slugs already taken, mapped to the source that took each.
            Mutated to record the returned slug.

    Returns:
        The slug this source may use.

    """
    slug = source_to_slug(source)
    candidate = slug
    suffix = 1
    while candidate in claimed:
        suffix += 1
        candidate = f"{slug}-{suffix}"

    if candidate != slug:
        logger.warning(
            "Scanner sources %r and %r both normalise to the profile slug %r; "
            "naming the second %r so neither source is lost.",
            claimed[slug],
            source,
            slug,
            candidate,
        )

    claimed[candidate] = source
    return candidate


def _auto_source_mode(source: str, *, has_flatbed: bool) -> Literal["flatbed", "adf"]:
    """
    Decide how an ``Auto`` source should be routed on this device.

    A source that is not ``Auto`` routes on its own name and never consults
    this. An ``Auto`` source says nothing about what is loaded, so the only
    evidence available is whether the device has a platen at all: one that does
    not is a sheet-fed machine, where treating ``Auto`` as single-page returns
    one page from a whole stack.

    Args:
        source: The SANE source name the profile will carry.
        has_flatbed: Whether the device reports any flatbed source. Keyword-only,
            because a positional boolean is not allowed by this project's lint
            rules.

    Returns:
        The ``auto_source_mode`` the profile should carry.

    """
    if classify_source(source) is SourceKind.AUTO and not has_flatbed:
        return "adf"
    return "flatbed"


def generate_profiles(
    capabilities: DeviceCapabilities,
) -> dict[str, ProfileConfig]:
    """
    Generate scan profiles from scanner capabilities.

    Creates one profile per scanner source, plus a "default" profile. All
    generated profiles have auto_generated=True.

    The "default" profile is emitted whenever the device reports any source at
    all, and that is not a preference: ``Settings.validate_default_profile``
    makes the key mandatory, so a generated set without it is written to disk
    and then refused by saneless on the next load, with ``auto-profiles``
    reporting success and exiting 0 over an unusable installation. A flatbed
    backs it when the device has one; on a sheet-fed scanner the device's own
    first reported source does, which is the only honest candidate available.

    Every question this function asks about a source name is answered by
    ``classify_source`` (D-02, Q9). It previously carried three rules of its
    own -- an equality test for "auto" and two ``"flatbed" in s.lower()``
    substring tests -- which disagreed with the classifier at the edges: stray
    whitespace defeated the equality test, and the substring test called
    "Flatbed Duplex" a flatbed, making a duplex feeder back the default
    profile.

    Args:
        capabilities: Scanner device capabilities with sources,
            resolutions, and modes.

    Returns:
        Dictionary mapping profile slug names to ProfileConfig instances.

    """
    profiles: dict[str, ProfileConfig] = {}
    resolution = pick_closest_resolution(
        capabilities.resolutions,
        target=DEFAULT_RESOLUTION,
        resolution_range=capabilities.resolution_range,
    )
    mode = pick_preferred_mode(capabilities.modes, preferred="Color")
    has_flatbed = any(
        classify_source(s) is SourceKind.FLATBED for s in capabilities.sources
    )
    claimed: dict[str, str] = {}

    for source in capabilities.sources:
        slug = _claim_slug(source, claimed)
        profiles[slug] = ProfileConfig(
            source=source,
            resolution=resolution,
            mode=mode,
            auto_generated=True,
            auto_source_mode=_auto_source_mode(source, has_flatbed=has_flatbed),
        )

    # A flatbed backs the default when the device has one; otherwise its first
    # reported source does. The fallback is what keeps a sheet-fed scanner from
    # producing a config that Settings refuses to load (see the docstring).
    flatbed_sources = [
        s for s in capabilities.sources if classify_source(s) is SourceKind.FLATBED
    ]
    default_source = next(iter(flatbed_sources), None) or next(
        iter(capabilities.sources), None
    )
    if default_source is not None:
        profiles["default"] = ProfileConfig(
            source=default_source,
            resolution=resolution,
            mode=mode,
            auto_generated=True,
            # Mirrors the loop rather than defaulting to "flatbed": a default
            # backed by an Auto source on a platen-less device would otherwise
            # route a whole stack as a single page, disagreeing with the very
            # profile it was copied from.
            auto_source_mode=_auto_source_mode(default_source, has_flatbed=has_flatbed),
        )

    return profiles


def resolve_config_path(config_path: str | None = None) -> Path:
    """
    Resolve the TOML config file path for writing.

    Searches standard config locations when no explicit path is given.

    Args:
        config_path: Explicit path string, or None to search defaults.

    Returns:
        Resolved Path to the config file.

    """
    if config_path:
        return Path(config_path)
    search_paths = [
        Path("./saneless.toml"),
        Path.home() / ".config" / "saneless" / "config.toml",
        Path("/etc/saneless/config.toml"),
    ]
    for path in search_paths:
        if path.exists():
            return path
    return Path("./saneless.toml")


def _is_auto_generated(table: object) -> bool:
    """
    Report whether a parsed profile table carries a truthy auto_generated flag.

    The parsed profiles section holds values typed ``object``, so the flag is
    read behind an isinstance narrowing rather than an annotation the type
    checkers cannot verify. Anything that is not a mapping -- a stray scalar
    under ``[profiles]`` -- answers False and is therefore never pruned.

    Args:
        table: A value from the parsed ``[profiles]`` section.

    Returns:
        True only for a mapping whose ``auto_generated`` value is truthy.

    """
    if not isinstance(table, Mapping):
        return False
    return bool(table.get("auto_generated", False))


# Profile names the orphan prune must never remove, however they are flagged.
#
# ``default`` is required by ``Settings.validate_default_profile``, so pruning
# it leaves behind a config saneless itself refuses to load. There is no way
# back from that inside the tool: ``cli()`` loads settings before dispatching to
# any subcommand, so not even ``auto-profiles`` could regenerate the key it just
# deleted, and the user has to hand-edit TOML. A previous run stamps every
# profile it writes with ``auto_generated = true``, ``default`` included, so
# without this guard a single run against a scanner with no flatbed -- an
# ordinary sheet-fed document scanner -- destroys a working installation.
_UNPRUNABLE = frozenset({"default"})


def write_profiles_to_config(
    config_path: Path,
    profiles: dict[str, ProfileConfig],
    *,
    force: bool = False,
) -> list[str]:
    """
    Write generated profiles to TOML config file, preserving existing content.

    Uses tomlkit for comment-preserving TOML round-tripping. Profiles that
    already exist in the config are skipped unless force=True.

    Auto-generated profiles that the freshly generated set no longer names are
    pruned first, so renaming does not strand the profiles it replaced. The
    prune runs whether or not ``force`` is passed, and that is deliberate:
    ``force`` governs overwriting keys that are *present* in the generated set,
    while an orphan is by definition absent from it, so ``force`` has nothing
    to say about it. A profile without a truthy ``auto_generated`` flag is
    never touched -- CFG-07's literal wording, which keeps this from
    pre-empting the general merge semantics owned by a later phase.

    ``default`` is never pruned either, whatever it is flagged with: it is not
    an ordinary profile but a schema requirement (``_UNPRUNABLE``), and a
    config missing it is one saneless refuses to load.

    Args:
        config_path: Path to the TOML config file.
        profiles: Dictionary of profile name to ProfileConfig.
        force: If True, overwrite existing profiles.

    Returns:
        List of profile names that were actually written. Pruned profiles are
        not named here -- they were removed, not written.

    """
    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text())
    else:
        doc = tomlkit.document()

    if "profiles" not in doc:
        doc.add("profiles", tomlkit.table(is_super_table=True))

    profiles_section = cast("dict[str, object]", doc["profiles"])

    orphans = [
        name
        for name, table in profiles_section.items()
        if name not in profiles
        and name not in _UNPRUNABLE
        and _is_auto_generated(table)
    ]
    for name in orphans:
        logger.info(
            "Removing auto-generated profile %r: the scanner's sources no "
            "longer produce that name.",
            name,
        )
        del profiles_section[name]

    written: list[str] = []
    for name, profile in profiles.items():
        if name in profiles_section and not force:
            continue
        profile_table = tomlkit.table()
        profile_table.add("source", profile.source)
        profile_table.add("resolution", profile.resolution)
        profile_table.add("mode", profile.mode)
        if profile.auto_source_mode != "flatbed":
            profile_table.add("auto_source_mode", profile.auto_source_mode)
        auto_generated_flag = True
        profile_table.add("auto_generated", auto_generated_flag)
        profiles_section[name] = profile_table
        written.append(name)

    config_path.write_text(tomlkit.dumps(doc))
    return written
