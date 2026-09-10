"""
Automatic scanner profile generation.

Converts scanner device capabilities into ready-to-use scan profiles,
with comment-preserving TOML config file writing via tomlkit. All
generation logic uses pure functions for easy testing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal, assert_never, cast

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


def _slugify(lower: str) -> str:
    """
    Slugify an already-lowercased source name.

    Args:
        lower: Lowercased SANE source name.

    Returns:
        The name with spaces and underscores replaced by hyphens.

    """
    return lower.replace(" ", "-").replace("_", "-")


def source_to_slug(source: str) -> str:
    """
    Convert a SANE source name to a profile slug.

    Dispatches on ``classify_source`` -- the codebase's single
    source-classification rule -- and turns the resulting SourceKind into a
    descriptive, URL-safe slug, falling back to a basic slugification for
    source names that match no rule.

    Args:
        source: SANE source name string (e.g., "Flatbed", "ADF Duplex").

    Returns:
        A lowercase hyphenated slug string.

    """
    lower = source.lower()
    match classify_source(source):
        case SourceKind.AUTO:
            slug = "auto-scan"
        case SourceKind.FLATBED:
            slug = "flatbed-scan"
        case SourceKind.FEEDER_DUPLEX:
            slug = "adf-duplex"
        case SourceKind.FEEDER:
            # Not a thin passthrough: "ADF Back" classifies as FEEDER because
            # it IS a feeder for routing purposes, but it must not slug to
            # "adf-simplex" or it collides with "ADF Front" (the N-09 defect).
            # "Which scan path do I take?" and "what do I name this profile?"
            # are different questions with different equivalence classes.
            slug = _slugify(lower) if "back" in lower else "adf-simplex"
        case SourceKind.UNKNOWN:
            slug = _slugify(lower)
        case unhandled:
            assert_never(unhandled)
    return slug


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


def generate_profiles(
    capabilities: DeviceCapabilities,
) -> dict[str, ProfileConfig]:
    """
    Generate scan profiles from scanner capabilities.

    Creates one profile per scanner source, plus a "default" profile
    mapped to the flatbed source if available. All generated profiles
    have auto_generated=True.

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

    for source in capabilities.sources:
        slug = source_to_slug(source)
        auto_source_mode: Literal["flatbed", "adf"] = "flatbed"
        if source.lower() == "auto":
            has_flatbed = any("flatbed" in s.lower() for s in capabilities.sources)
            auto_source_mode = "adf" if not has_flatbed else "flatbed"
        profiles[slug] = ProfileConfig(
            source=source,
            resolution=resolution,
            mode=mode,
            auto_generated=True,
            auto_source_mode=auto_source_mode,
        )

    # Set default to flatbed if available
    flatbed_sources = [s for s in capabilities.sources if "flatbed" in s.lower()]
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
