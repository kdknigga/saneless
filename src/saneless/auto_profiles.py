"""
Automatic scanner profile generation.

Converts scanner device capabilities into ready-to-use scan profiles,
with comment-preserving TOML config file writing via tomlkit. All
generation logic uses pure functions for easy testing.
"""

from __future__ import annotations

import logging
import math
import re
import tomllib
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, assert_never, cast

import tomlkit
from pydantic import TypeAdapter, ValidationError
from tomlkit.exceptions import ParseError, TOMLKitError
from tomlkit.items import InlineTable, Item

from saneless.atomic_write import replace_file_atomically
from saneless.config import DEFAULT_RESOLUTION, ProfileConfig, Settings
from saneless.exceptions import ConfigError, describe
from saneless.scanner.base import SourceKind, classify_source
from saneless.text_safety import has_control_characters

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from tomlkit import TOMLDocument

    from saneless.scanner.base import DeviceCapabilities, DeviceInfo

__all__ = [
    "ProfileWriteResult",
    "device_type_of",
    "generate_profiles",
    "is_bare_default",
    "pick_closest_resolution",
    "pick_preferred_mode",
    "source_to_slug",
    "write_profiles_to_config",
]

logger = logging.getLogger(__name__)


# The slug for a source name of which _slugify keeps nothing.  A second such
# name becomes "source-2" through the collision tie-break rather than being
# dropped.
_EMPTY_SLUG_FALLBACK = "source"


# Matched as substrings, case-insensitively, against DeviceInfo.device_type:
# backends decorate these ("flatbed scanner", hpaio's "all-in-one").
_PLATEN_DEVICE_TYPES: Final = ("flatbed", "all-in-one", "multi-function")

# The mode a generated profile asks for when the device reported none.
_FALLBACK_MODE: Final = "Color"

# Matched as whole names first and then as substrings, because backends
# decorate them ("24bit Color", "True Gray").
_COLOUR_WORDS: Final = ("color", "colour")
_GRAY_WORDS: Final = ("gray", "grey")

# A mode naming one of these is chosen only when nothing cleaner in the same
# family exists.
_DEGRADED_MODE_WORDS: Final = (
    "lineart",
    "halftone",
    "negative",
    "dither",
    "binary",
    "diffusion",
)


def _slugify(source: str) -> str:
    """
    Reduce a source name to a strict ``[a-z0-9-]`` slug.

    The character set is the contract: no leading, trailing or doubled
    hyphen, and ``_EMPTY_SLUG_FALLBACK`` when nothing survives.  This is not
    the PDF filename sanitiser and must not be merged with it or reused for
    filesystem paths.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
    return slug or _EMPTY_SLUG_FALLBACK


def source_to_slug(source: str) -> str:
    """
    Convert a SANE source name to a profile slug.

    Named from the source itself, never from its ``SourceKind``: "ADF Front"
    and "ADF Back" are both feeders, and naming by kind would collapse them
    onto one profile.  Two different names can still normalise alike; that is
    resolved where profiles are assembled, so this function stays pure.

    Args:
        source: SANE source name string (e.g., "Flatbed", "ADF Duplex").

    Returns:
        A lowercase slug matching ``^[a-z0-9][a-z0-9-]*$``.

    """
    return _slugify(source)


def _snap_into_range(target: int, resolution_range: tuple[float, float, float]) -> int:
    """
    Clamp a target into a reported range and snap it onto the range's step.

    An off-grid value is one the device never offered, and SANE would
    silently substitute another.  The maximum need not lie on the grid, so the
    ceiling is the largest on-grid value not above it, and a tie goes to the
    higher value.  A step of zero means a continuous range in SANE, so it
    clamps without snapping.
    """
    low, high, step = resolution_range
    if step > 0:
        top = low + math.floor((high - low) / step) * step
        clamped = min(max(float(target), low), top)
        below = low + math.floor((clamped - low) / step) * step
        above = below + step
        value = above if clamped - below >= above - clamped else below
        value = min(value, top)
    else:
        value = min(max(float(target), low), high)
    # Half-up coercion to whole dpi. Python's built-in rounding takes a .5 to
    # the nearest even number, a rule about accumulated error, not scanners.
    return math.floor(value + 0.5)


def pick_closest_resolution(
    resolutions: list[int],
    target: int = DEFAULT_RESOLUTION,
    resolution_range: tuple[float, float, float] | None = None,
) -> int:
    """
    Pick the resolution closest to target from what the device actually offers.

    A device constrains its resolution option with either a word list or a
    ``(min, max, step)`` range, so the two arguments are alternatives; a word
    list wins when present.  A range-reporting device sends an empty list, so
    the range branch is what keeps it from being asked for a resolution it
    never advertised.

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


def _first_mode_containing(modes: Sequence[str], words: tuple[str, ...]) -> str | None:
    """
    Pick the cleanest mode whose name contains one of the given words.

    Args:
        modes: Available scan modes, in the order the device reported them.
        words: Lowercase words marking the family wanted (colour or gray).

    Returns:
        The first mode in the family that names no degraded rendering, else the
        first mode in the family at all, else None when the family is absent.

    """
    family = [m for m in modes if any(w in m.casefold() for w in words)]
    for mode in family:
        if not any(w in mode.casefold() for w in _DEGRADED_MODE_WORDS):
            return mode
    return family[0] if family else None


def pick_preferred_mode(modes: list[str]) -> str:
    """
    Pick the scan mode a person would choose, ranked by what each mode means.

    SANE backends do not agree on how to spell colour (brother4 offers
    ``24bit Color``), and a backend's first entry is usually its
    black-and-white mode, so the first entry is only the last resort.

    The tiers, each scanning the modes in the order the device reported them
    and matching case-insensitively:

    1. A mode whose whole name is a colour word (``Color``, ``Colour``).
    2. A mode containing a colour word, preferring one that names no degraded
       rendering (lineart, halftone, negative, dither, binary, diffusion).
    3. The same rule for gray (``Gray``, ``Grey``), so a device without colour
       still scans in gray rather than in dithered black-and-white.
    4. The first mode the device reported.

    Args:
        modes: Available scan modes from the scanner.

    Returns:
        The chosen mode, or ``_FALLBACK_MODE`` when the device reported none.

    """
    for mode in modes:
        if mode.casefold() in _COLOUR_WORDS:
            return mode
    for words in (_COLOUR_WORDS, _GRAY_WORDS):
        chosen = _first_mode_containing(modes, words)
        if chosen is not None:
            return chosen
    return modes[0] if modes else _FALLBACK_MODE


def is_bare_default(settings: Settings) -> bool:
    """
    Check if settings have only the uncustomized default profile.

    Returns True only when there is exactly one profile, named "default",
    equal to ``ProfileConfig()`` in every field.

    The whole profile is compared rather than a hand-picked subset, so a field
    added later cannot be silently ignored.  Pydantic equality compares field
    values, not which fields were set, so a config that spells out the
    defaults still counts as bare.

    Args:
        settings: Application settings to inspect.

    Returns:
        True if the profile set is the bare uncustomized default.

    """
    if len(settings.profiles) != 1:
        return False
    if "default" not in settings.profiles:
        return False
    return settings.profiles["default"] == ProfileConfig()


def _claim_slug(source: str, claimed: dict[str, str]) -> str:
    """
    Resolve a source's slug against the slugs already claimed.

    "ADF-Front" and "ADF Front" normalise to the same slug.  The first source
    to claim a slug, in the device's own source order, keeps it bare; each
    later collider gains a "-2", "-3", ... suffix, so no reported source is
    dropped.

    Args:
        source: The SANE source name claiming a slug.
        claimed: Slugs already taken, mapped to the source that took each.
            Mutated to record the returned slug.

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


def device_type_of(devices: Sequence[DeviceInfo], device_id: str) -> str:
    """
    Find the type a discovered device declared, by its SANE name.

    Read from an enumeration the caller already holds, so there is no second
    ``get_devices()`` round trip.  A miss returns the empty string rather
    than raising: ``scanner.device`` may be spelled differently from the
    enumeration, and ``_declares_platen`` reads ``""`` as "no evidence".

    Args:
        devices: The devices the backend enumerated.
        device_id: The SANE device name profiles are being generated for.

    Returns:
        The matching device's declared type, or ``""`` if none matches.

    """
    return next((d.device_type for d in devices if d.name == device_id), "")


def _declares_platen(device_type: str) -> bool:
    """
    Read the device's own declared type for evidence that it has a platen.

    ``"sheetfed scanner"`` matches no token, and an unrecognised or empty type
    is read as no evidence, never as denial.  A feeder-only MFP matches, so
    its ``Auto`` profile scans one page: the cheap, visible failure, preferred
    over probing a platen for a second sheet.
    See docs/explanation/decisions/0001-unknown-sources-scan-one-page.md.
    """
    lower = device_type.strip().lower()
    return any(token in lower for token in _PLATEN_DEVICE_TYPES)


def _auto_source_mode(source: str, *, has_platen: bool) -> Literal["flatbed", "adf"]:
    """
    Decide how an ``Auto`` source should be routed on this device.

    An ``Auto`` source says nothing about what is loaded, so the only
    evidence is whether the device has a platen: one without is sheet-fed,
    where single-page routing returns one page from a stack.  ``has_platen``
    must not be reduced to "reports a Flatbed source": hpaio names an HP
    LaserJet 3030's sources ``Auto`` and ``ADF``, and probing its glass for a
    second sheet fails with a device I/O error.
    """
    if classify_source(source) is SourceKind.AUTO and not has_platen:
        return "adf"
    return "flatbed"


def _duplex(source: str) -> Literal["none", "hardware"]:
    """
    Decide the ``duplex`` value a generated profile should carry.

    ``"hardware"`` for a ``FEEDER_DUPLEX`` source, else ``"none"``; manual
    duplex is never generated.  An ADF-mode option is not evidence: epson2
    reports it inactive until its feeder is selected, and on hardware that
    cannot duplex.  The value is always passed explicitly, because the loader
    reads a source naming "manual" and "duplex" as manual duplex otherwise.
    """
    if classify_source(source) is SourceKind.FEEDER_DUPLEX:
        return "hardware"
    return "none"


# Wording only, never consulted for routing: ``classify_source`` remains the
# only rule that says what a source is.  Real backends spell these "ADF Front"
# and "ADF Back"; none spells "Rear".
_FEEDER_SIDE_WORDS: Final[dict[str, Literal["front", "back"]]] = {
    "front": "front",
    "back": "back",
}


def _feeder_side(source: str) -> Literal["front", "back"] | None:
    """
    Name the one side a single-sided feeder source scans, for its wording only.

    Reached only after ``classify_source`` has called the source a feeder, so
    it never decides which path a scan takes.  A side counts only as a whole
    word ("Backlit" names none); a name with both sides or neither gets None.
    """
    words = set(re.findall(r"[a-z]+", source.lower()))
    sides = {side for word, side in _FEEDER_SIDE_WORDS.items() if word in words}
    if len(sides) != 1:
        return None
    return sides.pop()


def _profile_label(source: str) -> str:
    """
    Return the short human name a generated profile carries.

    Derived from the ``SourceKind``, so no profile is offered under a blank
    name.  Every returned string is a developer-authored constant: the SANE
    source name is never interpolated, so vendor text cannot ride this path
    onto the scan page.  ``generate_profiles`` appends an ordinal when two
    sources share a label.

    Raises:
        AssertionError: The classifier returned a non-``SourceKind`` value.

    """
    kind = classify_source(source)
    match kind:
        case SourceKind.FEEDER:
            match _feeder_side(source):
                case "front":
                    label = "Feeder, front side only"
                case "back":
                    label = "Feeder, back side only"
                case None:
                    label = "Feeder, single-sided"
        case SourceKind.FEEDER_DUPLEX:
            label = "Feeder, double-sided"
        case SourceKind.FLATBED:
            label = "Glass (flatbed)"
        case SourceKind.AUTO:
            label = "Automatic"
        case SourceKind.UNKNOWN:
            label = "Scanner source"
        case _:
            assert_never(kind)
    return label


def _profile_description(
    source: str, *, auto_source_mode: Literal["flatbed", "adf"] = "flatbed"
) -> str:
    """
    Return the one-sentence explanation a generated profile carries.

    Like ``_profile_label``, every string is a developer-authored constant
    derived from the ``SourceKind``; the SANE source name is never
    interpolated.  An ``Auto`` source's sentence follows ``auto_source_mode``,
    because what it does depends on how it is routed.

    Raises:
        AssertionError: The classifier returned a non-``SourceKind`` value.

    """
    kind = classify_source(source)
    match kind:
        case SourceKind.FEEDER:
            match _feeder_side(source):
                case "front":
                    description = (
                        "Scans only the front of every page using the document feeder."
                    )
                case "back":
                    description = (
                        "Scans only the back of every page using the document feeder."
                    )
                case None:
                    description = (
                        "Scans one side of every page using the document feeder."
                    )
        case SourceKind.FEEDER_DUPLEX:
            description = "Scans both sides of every page using the document feeder."
        case SourceKind.FLATBED:
            description = "Scans one page at a time from the glass."
        case SourceKind.AUTO if auto_source_mode == "adf":
            description = (
                "Scans every page in the document feeder; "
                "the scanner decides how it feeds them."
            )
        case SourceKind.AUTO:
            description = "Scans one page; the scanner picks the glass or the feeder."
        case SourceKind.UNKNOWN:
            description = "Uses the scanner source this profile names."
        case _:
            assert_never(kind)
    return description


# For a device with no ``source`` option.  It says "one page" because with no
# source to classify, the scan routes as the model default's flatbed, even on a
# sheet-fed device.
_NO_SOURCE_LABEL: Final = "Standard scan"
_NO_SOURCE_DESCRIPTION: Final = (
    "Scans one page from the scanner, which offers no choice of where the page "
    "comes from."
)


def generate_profiles(
    capabilities: DeviceCapabilities,
    device_type: str = "",
) -> dict[str, ProfileConfig]:
    """
    Generate scan profiles from scanner capabilities.

    Creates one profile per scanner source, plus a "default" profile. All
    generated profiles have auto_generated=True.

    The "default" profile is always emitted, because
    ``Settings.validate_default_profile`` refuses a config without it.  It is
    a copy of the profile for the device's flatbed, else its first reported
    source, so the two compare equal as whole models; a device with no SANE
    ``source`` option gets a ``default`` that names no source.

    Labels are unique within the set: a source whose label another source
    already holds gets an ordinal, so "Feeder, single-sided" is followed by
    "Feeder, single-sided 2".  Every question about a source name is answered
    by ``classify_source`` and by no rule of this function's own.

    Args:
        capabilities: Scanner device capabilities with sources,
            resolutions, and modes.
        device_type: The type string SANE reported for the device, read only
            as extra evidence of a platen.  Empty means the device is judged
            on its source names alone.

    Returns:
        Dictionary mapping profile slug names to ProfileConfig instances.

    """
    profiles: dict[str, ProfileConfig] = {}
    resolution = pick_closest_resolution(
        capabilities.resolutions,
        target=DEFAULT_RESOLUTION,
        resolution_range=capabilities.resolution_range,
    )
    mode = pick_preferred_mode(capabilities.modes)
    # Either witness alone is enough: hpaio declares "all-in-one" while naming
    # no Flatbed source, and a backend can name one while declaring an unknown
    # type.
    has_platen = any(
        classify_source(s) is SourceKind.FLATBED for s in capabilities.sources
    ) or _declares_platen(device_type)
    # Decided before the loop so that the slug it occupies can be reserved.
    flatbed_sources = [
        s for s in capabilities.sources if classify_source(s) is SourceKind.FLATBED
    ]
    default_source = next(iter(flatbed_sources), None) or next(
        iter(capabilities.sources), None
    )

    # Reserved up front: a source that slugs to "default" would otherwise be
    # silently overwritten by the assignment after the loop.  It goes to
    # "default-2" instead.
    claimed: dict[str, str] = {}
    if default_source is not None:
        claimed["default"] = default_source

    # The n-th holder of a base label reads "<label> n", so no two dropdown
    # options share a name (brother4 reports two feeders that name no side).
    # Only the label takes the ordinal; the description is true of every holder.
    holders: dict[str, int] = {}
    default_slug: str | None = None
    for source in capabilities.sources:
        slug = _claim_slug(source, claimed)
        if default_slug is None and source == default_source:
            default_slug = slug
        auto_source_mode = _auto_source_mode(source, has_platen=has_platen)
        base_label = _profile_label(source)
        holders[base_label] = holders.get(base_label, 0) + 1
        ordinal = holders[base_label]
        profiles[slug] = ProfileConfig(
            source=source,
            resolution=resolution,
            mode=mode,
            auto_generated=True,
            auto_source_mode=auto_source_mode,
            duplex=_duplex(source),
            label=base_label if ordinal == 1 else f"{base_label} {ordinal}",
            description=_profile_description(source, auto_source_mode=auto_source_mode),
        )

    if default_slug is None:
        # ``source`` is left unset on purpose: "Flatbed" would claim a platen
        # the scanner never reported.
        profiles["default"] = ProfileConfig(
            resolution=resolution,
            mode=mode,
            auto_generated=True,
            label=_NO_SOURCE_LABEL,
            description=_NO_SOURCE_DESCRIPTION,
        )
    else:
        # A copy, not a second build, so every field agrees by construction,
        # ordinal included; the scan page relies on the two comparing equal.
        # Deep, so that no mutable field is shared between the two entries.
        profiles["default"] = profiles[default_slug].model_copy(deep=True)

    return profiles


# The loader's reading of ``auto_generated``: pydantic's lax ``bool``, the type
# ``ProfileConfig.auto_generated`` declares.
_FLAG: Final = TypeAdapter(bool)


def _is_auto_generated(table: object) -> bool:
    """
    Report whether a parsed profile table carries a truthy auto_generated flag.

    Read with the loader's pydantic ``bool``, not Python truthiness: the
    loader reads ``auto_generated = "false"`` as False, and the writer must
    not refresh or prune a profile the loader says the operator owns.  A
    non-mapping, or a value the loader would reject, answers False.
    """
    if not isinstance(table, Mapping):
        return False
    try:
        return _FLAG.validate_python(table.get("auto_generated", False))
    except ValidationError:
        return False


# Profile names the orphan prune must never remove, however they are flagged.
# Pruning ``default`` leaves a config saneless refuses to load, and every
# command, ``auto-profiles`` included, validates settings before it runs.
_UNPRUNABLE = frozenset({"default"})


# The keys the tool owns in a profile flagged ``auto_generated = true``:
# ``--force`` overwrites these and deletes any the fresh generation omits; every
# other key is the user's.  The order is the key order of a newly written
# table, so a human reads the profile's name before its SANE source.
_OWNED_KEYS: Final = (
    "label",
    "description",
    "source",
    "resolution",
    "mode",
    "auto_source_mode",
    "duplex",
    "auto_generated",
)

# Why an unflagged same-name profile was left alone, and how to hand it back to
# the tool. There is deliberately no flag that overrides this.
_NOT_GENERATED_REASON: Final = (
    "not created by auto-profiles (no auto_generated = true); "
    "rename or delete it to regenerate"
)

# ``default`` cannot be renamed or deleted, so the flag is the one way to hand
# it back to the tool.
_DEFAULT_NOT_GENERATED_REASON: Final = (
    "add auto_generated = true to its table to let auto-profiles --force refresh it"
)


@dataclass(frozen=True, slots=True)
class ProfileWriteResult:
    """
    What one ``write_profiles_to_config`` call did, grouped by action.

    The CLI and the worker's startup log both print it through ``describe``,
    so the two front ends share one vocabulary rather than two spellings.

    Attributes:
        path: The config file the result describes.
        added: Generated names the file did not have, now written.
        refreshed: Flagged profiles whose owned keys ``force`` rewrote.
        unchanged: Flagged profiles ``force`` found already matching the
            generation, key for key; neither rewritten nor reported, since
            nothing about them changed.
        skipped_not_generated: Same-name profiles without a truthy
            ``auto_generated``, never touched, under ``force`` too.
        skipped_existing: Flagged profiles left alone because ``force`` was
            not passed.
        removed: Flagged profiles the scanner no longer produces, pruned.
        pinned_device: The device id written into an unset ``[scanner]
            device``, or None when the call wrote none.

    """

    path: Path
    added: tuple[str, ...] = ()
    refreshed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    skipped_not_generated: tuple[str, ...] = ()
    skipped_existing: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    pinned_device: str | None = None

    @property
    def persisted(self) -> frozenset[str]:
        """Names whose file table now matches the generated profile."""
        return frozenset(self.added + self.refreshed + self.unchanged)

    def groups(self) -> list[tuple[str, tuple[str, ...]]]:
        """
        Pair each non-empty group's line with the names it wrote.

        The second element is the group's names for Added and Refreshed, whose
        profiles a front end may detail, and empty for every other group.

        Returns:
            ``(line, written_names)`` in the fixed order Added, Refreshed,
            Skipped (not auto-generated) -- ``default`` on a line of its own
            first, since the advice differs, then the rest -- Skipped (already
            exists), Removed, then the pinned device, if any, whose line names
            no profiles. Unchanged tables have no line: nothing happened to
            them.

        """
        skipped_default = tuple(
            name for name in self.skipped_not_generated if name in _UNPRUNABLE
        )
        skipped_others = tuple(
            name for name in self.skipped_not_generated if name not in _UNPRUNABLE
        )
        labelled = (
            ("Added", self.added, "", True),
            ("Refreshed", self.refreshed, "", True),
            (
                "Skipped (not auto-generated)",
                skipped_default,
                f" -- {_DEFAULT_NOT_GENERATED_REASON}",
                False,
            ),
            (
                "Skipped (not auto-generated)",
                skipped_others,
                f" -- {_NOT_GENERATED_REASON}",
                False,
            ),
            (
                "Skipped (already exists; use --force to refresh)",
                self.skipped_existing,
                "",
                False,
            ),
            ("Removed (scanner no longer offers it)", self.removed, "", False),
        )
        lines: list[tuple[str, tuple[str, ...]]] = []
        for label, names, suffix, written in labelled:
            if not names:
                continue
            shown = ", ".join(repr(name) for name in names)
            lines.append((f"{label}: {shown}{suffix}", names if written else ()))
        if self.pinned_device is not None:
            # repr, as for the names above: the id came from device discovery
            # and may carry control characters.
            lines.append((f"Pinned [scanner] device: {self.pinned_device!r}", ()))
        return lines

    def describe(self) -> list[str]:
        """
        Render the result as one line per non-empty group.

        Returns:
            The group lines, empty when nothing was added, refreshed, skipped,
            removed or pinned.

        """
        return [line for line, _ in self.groups()]


def _generated_values(profile: ProfileConfig) -> dict[str, str | int | bool]:
    """
    Build the owned key values a fresh generation writes for ``profile``.

    Insertion order is the file's key order for a new table. Only non-default
    values of ``auto_source_mode`` and ``duplex`` are included, so a refreshed
    table reads the way a freshly generated one does. ``label`` and
    ``description`` are the exception: they are always written. ``source`` is
    written only when the profile was given one.

    Args:
        profile: A generated profile.

    Returns:
        The owned keys to write, in file order; always a subset of
        ``_OWNED_KEYS``.

    """
    values: dict[str, str | int | bool] = {
        # Unconditional, so the prune of omitted owned keys never reaches
        # free text.  Copied from the model, not re-derived from the source: a
        # label's ordinal depends on the whole source set.
        "label": profile.label,
        "description": profile.description,
    }
    # A no-source device's default carries none, and a --force refresh deletes
    # a stale one.
    if "source" in profile.model_fields_set:
        values["source"] = profile.source
    values["resolution"] = profile.resolution
    values["mode"] = profile.mode
    if profile.auto_source_mode != "flatbed":
        values["auto_source_mode"] = profile.auto_source_mode
    # Omitting "none" cannot let the loader's legacy translation turn a profile
    # manual on reload: a "none" source is not FEEDER_DUPLEX, so its name does
    # not contain "duplex".
    if profile.duplex != "none":
        values["duplex"] = profile.duplex
    values["auto_generated"] = True
    return values


_TOMLKIT_POSITION: Final = re.compile(r" at line \d+ col \d+$")
"""The position suffix tomlkit appends to every ``ParseError`` message."""


def _read_config(config_path: Path) -> tuple[TOMLDocument, str]:
    """
    Parse the config file for a merge, or start an empty document.

    Args:
        config_path: The config file; it need not exist.

    Returns:
        The parsed document and the exact text it was parsed from (empty for
        a file that does not exist yet).

    Raises:
        ConfigError: The file's bytes are not valid UTF-8, or the text is not
            valid TOML; the latter names the line and column when tomlkit
            reports one and is chained to tomlkit's error.

    """
    if not config_path.exists():
        return tomlkit.document(), ""
    # Path's text-mode reader uses the locale encoding and translates CRLF to
    # LF, silently rewriting the file on the way back out.
    try:
        text = config_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        msg = f"{config_path} is not valid UTF-8; refusing to rewrite it"
        raise ConfigError(msg) from None
    try:
        document = tomlkit.parse(text)
    except TOMLKitError as exc:
        # Every tomlkit error: a table redefined under a dotted header raises
        # KeyAlreadyPresent, which is not a ParseError.  The document text
        # tomlkit can quote is one unexpected character or a duplicated key's
        # name, never a value, so the chain cannot carry the token.
        reason = describe(exc)
        where = ""
        if isinstance(exc, ParseError):
            reason = _TOMLKIT_POSITION.sub("", reason)
            where = f" at line {exc.line}, column {exc.col}"
        msg = f"Cannot update {config_path}: it is not valid TOML{where} ({reason})"
        raise ConfigError(msg) from exc
    return document, text


# Stands in for a NaN float in ``_comparable``: NaN never equals itself, so a
# config holding ``nan`` would otherwise fail the round-trip guard forever.
_NAN: Final = object()


def _comparable(value: object) -> object:
    """
    Normalise parsed TOML data so two parsers' readings compare equal.

    tomllib reads a CRLF inside a multi-line string as LF, while tomlkit keeps
    the CRLF, so a CRLF config holding one would never compare equal; the
    loader sees LF either way, so the meaning is the same. NaN is mapped to a
    sentinel because it is unequal to itself.
    """
    if isinstance(value, str):
        return value.replace("\r\n", "\n")
    if isinstance(value, float) and math.isnan(value):
        return _NAN
    if isinstance(value, Mapping):
        items = cast("Mapping[object, object]", value).items()
        return {key: _comparable(item) for key, item in items}
    if isinstance(value, list):
        return [_comparable(item) for item in cast("list[object]", value)]
    return value


def _render_checked(config_path: Path, doc: TOMLDocument, original_text: str) -> str:
    """
    Dump the merged document, keep its line endings, and prove it round-trips.

    Parsing is not enough: tomlkit can emit valid TOML that means something
    else. With top-level dotted keys (``profiles.default.source = ...``) a
    table it adds captures the dotted lines after it, so the file would parse
    and then fail to load. The dumped text is therefore re-parsed and
    compared with the merged document's data.

    Raises:
        ConfigError: The dumped text is not valid TOML, or it parses to data
            other than the merged document; nothing was written.

    """
    new_text = tomlkit.dumps(doc)
    crlf_count = original_text.count("\r\n")
    if crlf_count and crlf_count == original_text.count("\n"):
        # tomlkit ends the lines it adds with a bare LF.  Only an all-CRLF
        # file is normalised, so no LF the user wrote, such as one inside a
        # multi-line string, becomes CRLF.
        new_text = re.sub(r"(?<!\r)\n", "\r\n", new_text)
    try:
        reparsed: object = _comparable(tomllib.loads(new_text))
    except tomllib.TOMLDecodeError:
        reparsed = None
    if reparsed is None or reparsed != _comparable(doc.unwrap()):
        msg = (
            f"Cannot update {config_path}: the merged profiles do not "
            "round-trip through TOML; refusing to rewrite it, so the file is "
            "left intact"
        )
        raise ConfigError(msg) from None
    return new_text


_MergeOutcome = Literal[
    "added", "refreshed", "unchanged", "skipped_not_generated", "skipped_existing"
]


def _same_value(stored: object, generated: object) -> bool:
    """
    Report whether a stored owned value is exactly the one a generation writes.

    Type and value are both compared, so ``300.0`` against ``300`` and ``1``
    against ``True`` are changes, though Python calls each pair equal: a
    refresh must replace such a value with the generation's own.
    """
    value = stored.unwrap() if isinstance(stored, Item) else stored
    return type(value) is type(generated) and value == generated


def _merge_profile(
    section: MutableMapping[str, object],
    name: str,
    profile: ProfileConfig,
    *,
    force: bool,
) -> _MergeOutcome:
    """
    Merge one generated profile into the parsed ``[profiles]`` section.

    Args:
        section: The parsed ``[profiles]`` section, changed in place.
        name: The generated profile's name.
        profile: The generated profile.
        force: Whether a flagged profile's owned keys are refreshed.

    Returns:
        Which ``ProfileWriteResult`` group ``name`` belongs in.

    """
    values = _generated_values(profile)
    existing = section.get(name)
    if existing is None:
        # A standard table inside an inline ``profiles = { ... }`` section
        # dumps as invalid TOML (verified, tomlkit 0.14), so an inline section
        # gets an inline table. The tomllib guard backs this up.
        table = (
            tomlkit.inline_table()
            if isinstance(section, InlineTable)
            else tomlkit.table()
        )
        for key, value in values.items():
            table.add(key, value)
        section[name] = table
        return "added"
    if not _is_auto_generated(existing) or not isinstance(existing, MutableMapping):
        # Not created by the tool (a stray scalar included), so not the tool's
        # to change. Reported, never overwritten, under force too.
        return "skipped_not_generated"
    if not force:
        return "skipped_existing"
    # Keys are set on the existing table, never a fresh table assigned over
    # it, which would drop default_tags, title and the comments.
    owned = cast("MutableMapping[str, object]", existing)
    if all(
        (key in owned) == (key in values)
        and (key not in values or _same_value(owned[key], values[key]))
        for key in _OWNED_KEYS
    ):
        return "unchanged"
    for key in _OWNED_KEYS:
        if key in values:
            owned[key] = values[key]
        elif key in owned:
            del owned[key]
    return "refreshed"


def _pin_device(doc: TOMLDocument, config_path: Path, device: str) -> str | None:
    """
    Write ``device`` into the document's ``[scanner] device`` when it is unset.

    Unset means absent or ``""``.  ``[scanner] device`` carries no
    ``auto_generated`` marker, so a set value is never the tool's to change,
    ``force`` included.

    Args:
        doc: The parsed config document, changed in place.
        config_path: The config file, for the error message.
        device: The device id to write.

    Returns:
        ``device`` when it was written, None when a set value was kept or the
        id could not be stored.

    Raises:
        ConfigError: ``scanner`` in the file is not a table.

    """
    if has_control_characters(device):
        # tomlkit writes ESC as ``\e``, a TOML 1.1 escape the 1.0 reader
        # rejects, so the round-trip guard would refuse the whole run.  No
        # real SANE id holds one; skipping the pin keeps the profiles.
        logger.warning(
            "Not pinning [scanner] device to %r: it contains control characters",
            device,
        )
        return None
    if "scanner" not in doc:
        doc.add("scanner", tomlkit.table())
    section = doc["scanner"]
    if not isinstance(section, MutableMapping):
        msg = f"[scanner] in {config_path} is not a table; refusing to overwrite it"
        raise ConfigError(msg)
    scanner = cast("MutableMapping[str, object]", section)
    existing = scanner.get("device")
    if existing is not None and existing != "":
        return None
    scanner["device"] = device
    return device


def write_profiles_to_config(
    config_path: Path,
    profiles: dict[str, ProfileConfig],
    *,
    force: bool = False,
    device: str | None = None,
) -> ProfileWriteResult:
    """
    Merge generated profiles into a TOML config file, preserving what is there.

    Uses tomlkit for comment-preserving TOML round-tripping. For each
    generated name:

    * absent from the file: a new table is added;
    * present without a truthy ``auto_generated``: left byte for byte alone and
      reported, whether or not ``force`` is passed;
    * present and flagged, without ``force``: skipped as already existing;
    * present and flagged, with ``force``: the owned keys (``_OWNED_KEYS``) are
      written onto the existing table and any the generation omits are
      deleted, so every other key and every comment survives -- unless every
      owned key already matches in type and value, when the table is left
      alone and counted as unchanged rather than refreshed.

    Auto-generated profiles the fresh set no longer names are pruned first,
    whether or not ``force`` is passed: ``force`` governs profiles present in
    the set, and an orphan is absent from it.  ``default`` is never pruned
    (``_UNPRUNABLE``).

    ``device``, when given, is written into ``[scanner] device`` if the file
    leaves that key absent or empty, so later scans go to the scanner this run
    used.  A set value is never overwritten, ``force`` included.

    The new text is re-parsed and must mean exactly the merged document
    before ``replace_file_atomically`` swaps it in, CRLF line endings kept.  A
    merge that changes nothing does not rewrite the file.

    Args:
        config_path: Path to the TOML config file.
        profiles: Dictionary of profile name to ProfileConfig.
        force: If True, refresh the owned keys of flagged profiles.
        device: The device id discovery chose, to pin when the file has none;
            None to leave ``[scanner]`` untouched.  The worker's startup
            generation passes none on purpose: a service start must not
            silently pin whichever scanner answered first.

    Returns:
        What was added, refreshed, skipped, removed and pinned, with ``path``
        the real file (the symlink's target when ``config_path`` is a link).

    Raises:
        ConfigError: ``[profiles]`` or ``[scanner]`` in the file is not a
            table, the file is not valid UTF-8, the merged text does not
            round-trip through TOML, or the file cannot be replaced because it
            is bind-mounted as a single file (EBUSY) or sits on a read-only
            mount, or for any other refusal ``replace_file_atomically``
            documents.
        OSError: Any other failure to read or replace the file, including
            ``PermissionError`` for a file this process may not write.

    """
    doc, original_text = _read_config(config_path)

    # Before the profiles, so a file created from scratch has [scanner]
    # first, as the example config does.
    pinned = _pin_device(doc, config_path, device) if device is not None else None

    if "profiles" not in doc:
        doc.add("profiles", tomlkit.table(is_super_table=True))

    # ``cast`` is not a check: ``profiles = "oops"`` would reach ``.items()``
    # on a tomlkit String and end in a raw AttributeError traceback.
    section = doc["profiles"]
    if not isinstance(section, Mapping):
        msg = f"[profiles] in {config_path} is not a table; refusing to overwrite it"
        raise ConfigError(msg)
    profiles_section = cast("dict[str, object]", section)

    # Generation always emits ``default``, so "only default was generated"
    # means the scanner reported no sources, and then nothing can be judged
    # orphaned.  A guard on an empty set would never fire.
    orphans = (
        []
        if set(profiles) <= _UNPRUNABLE
        else [
            name
            for name, table in profiles_section.items()
            if name not in profiles
            and name not in _UNPRUNABLE
            and _is_auto_generated(table)
        ]
    )
    for name in orphans:
        logger.info(
            "Removing auto-generated profile %r: the scanner's sources no "
            "longer produce that name.",
            name,
        )
        del profiles_section[name]

    outcomes: dict[_MergeOutcome, list[str]] = {
        "added": [],
        "refreshed": [],
        "unchanged": [],
        "skipped_not_generated": [],
        "skipped_existing": [],
    }
    for name, profile in profiles.items():
        outcome = _merge_profile(profiles_section, name, profile, force=force)
        outcomes[outcome].append(name)

    changed = bool(
        outcomes["added"] or outcomes["refreshed"] or orphans or pinned is not None
    )
    new_text = _render_checked(config_path, doc, original_text) if changed else ""
    if not changed or new_text == original_text:
        # Identical bytes are never swapped in, so a single-file mount gets no
        # EBUSY and the file keeps its inode, ACL and ownership.
        target = config_path.resolve()
    else:
        target = replace_file_atomically(config_path, new_text)
        if config_path.is_symlink():
            # Asked of the path itself: a regular file reached through a
            # linked directory is not a symlink.
            logger.info("Wrote profiles to %s (symlink to %s)", config_path, target)
        if pinned is not None:
            # Logged once the file holds it; %r because the id is untrusted.
            logger.info("Pinned [scanner] device to %r", pinned)

    return ProfileWriteResult(
        path=target,
        added=tuple(outcomes["added"]),
        refreshed=tuple(outcomes["refreshed"]),
        unchanged=tuple(outcomes["unchanged"]),
        skipped_not_generated=tuple(outcomes["skipped_not_generated"]),
        skipped_existing=tuple(outcomes["skipped_existing"]),
        removed=tuple(orphans),
        pinned_device=pinned,
    )
