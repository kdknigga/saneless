"""
Automatic scanner profile generation.

Converts scanner device capabilities into ready-to-use scan profiles,
with comment-preserving TOML config file writing via tomlkit. All
generation logic uses pure functions for easy testing.
"""

from __future__ import annotations

import logging
import re
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


def pick_closest_resolution(
    resolutions: list[int],
    target: int = DEFAULT_RESOLUTION,
) -> int:
    """
    Pick the resolution closest to target from available options.

    Args:
        resolutions: Available resolution values from scanner.
        target: Preferred resolution (defaults to DEFAULT_RESOLUTION DPI).

    Returns:
        The closest available resolution, or target if list is empty.

    """
    if not resolutions:
        return target
    return min(resolutions, key=lambda r: abs(r - target))


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


def generate_profiles(
    capabilities: DeviceCapabilities,
) -> dict[str, ProfileConfig]:
    """
    Generate scan profiles from scanner capabilities.

    Creates one profile per scanner source, plus a "default" profile
    mapped to the flatbed source if available. All generated profiles
    have auto_generated=True.

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
        capabilities.resolutions, target=DEFAULT_RESOLUTION
    )
    mode = pick_preferred_mode(capabilities.modes, preferred="Color")
    has_flatbed = any(
        classify_source(s) is SourceKind.FLATBED for s in capabilities.sources
    )
    claimed: dict[str, str] = {}

    for source in capabilities.sources:
        slug = _claim_slug(source, claimed)
        auto_source_mode: Literal["flatbed", "adf"] = "flatbed"
        if classify_source(source) is SourceKind.AUTO and not has_flatbed:
            auto_source_mode = "adf"
        profiles[slug] = ProfileConfig(
            source=source,
            resolution=resolution,
            mode=mode,
            auto_generated=True,
            auto_source_mode=auto_source_mode,
        )

    # Set default to flatbed if available
    flatbed_sources = [
        s for s in capabilities.sources if classify_source(s) is SourceKind.FLATBED
    ]
    if flatbed_sources:
        profiles["default"] = ProfileConfig(
            source=flatbed_sources[0],
            resolution=resolution,
            mode=mode,
            auto_generated=True,
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

    Args:
        config_path: Path to the TOML config file.
        profiles: Dictionary of profile name to ProfileConfig.
        force: If True, overwrite existing profiles.

    Returns:
        List of profile names that were actually written.

    """
    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text())
    else:
        doc = tomlkit.document()

    if "profiles" not in doc:
        doc.add("profiles", tomlkit.table(is_super_table=True))

    profiles_section = cast("dict[str, object]", doc["profiles"])

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
