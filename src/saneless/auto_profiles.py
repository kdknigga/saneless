"""
Automatic scanner profile generation.

Converts scanner device capabilities into ready-to-use scan profiles,
with comment-preserving TOML config file writing via tomlkit. All
generation logic uses pure functions for easy testing.
"""

from __future__ import annotations

import logging
import re
import tomllib
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, cast

import tomlkit
from tomlkit.items import InlineTable

from saneless.atomic_write import replace_file_atomically
from saneless.config import DEFAULT_RESOLUTION, ProfileConfig, Settings
from saneless.exceptions import ConfigError
from saneless.scanner.base import SourceKind, classify_source

if TYPE_CHECKING:
    from pathlib import Path

    from tomlkit import TOMLDocument

    from saneless.scanner.base import DeviceCapabilities

__all__ = [
    "ProfileWriteResult",
    "generate_profiles",
    "is_bare_default",
    "pick_closest_resolution",
    "pick_preferred_mode",
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

    Returns True only when there is exactly one profile, named "default",
    equal to ``ProfileConfig()`` in every field.

    The whole profile is compared rather than a hand-picked subset, so a field
    added later cannot be silently ignored -- as ``duplex`` was, which let a
    hand-written manual-duplex default be replaced in memory by a generated
    flatbed profile (WR-04). Pydantic model equality compares field values,
    not which fields were set, so a config that spells out default values
    explicitly still counts as bare. ``auto_generated=True`` is covered by the
    same equality, because the bare profile has ``auto_generated=False``.

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


def _duplex(source: str) -> Literal["none", "hardware"]:
    """
    Decide the ``duplex`` value a generated profile should carry.

    A source the classifier calls ``FEEDER_DUPLEX`` is one the device duplexes
    itself, so its profile says ``"hardware"``; every other source says
    ``"none"``. ``"manual"`` is deliberately outside this function's range:
    manual duplex is not a device source at all, so auto-profiles has no
    evidence for it and those profiles are always written by hand.

    Nothing reads ``"hardware"`` (D-05). It records operator intent and makes a
    generated profile self-describing, and Phase 30's APPL-05 -- generated
    ``label`` / ``description`` such as "Feeder, double-sided" -- is its
    eventual reader. ``config.py`` states the same fact; it is repeated here
    because this is where the value is produced, and a reader here will ask
    what consumes it.

    The value is always passed explicitly, never left to the field default:
    the config loader reads a source name containing both "manual" and
    "duplex" as ``duplex = "manual"`` when no duplex is given. Such a name
    classifies as ``FEEDER_DUPLEX`` here, so passing the value is what keeps
    a generated profile from turning into a manual-duplex one.

    Args:
        source: The SANE source name the profile will carry.

    Returns:
        The ``duplex`` the profile should carry.

    """
    if classify_source(source) is SourceKind.FEEDER_DUPLEX:
        return "hardware"
    return "none"


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
    # A flatbed backs the default when the device has one; otherwise its first
    # reported source does. The fallback is what keeps a sheet-fed scanner from
    # producing a config that Settings refuses to load (see the docstring).
    # Decided before the loop so that the slug it occupies can be reserved.
    flatbed_sources = [
        s for s in capabilities.sources if classify_source(s) is SourceKind.FLATBED
    ]
    default_source = next(iter(flatbed_sources), None) or next(
        iter(capabilities.sources), None
    )

    # "default" is claimed up front because the default profile is assigned
    # after the loop with a bare ``profiles["default"] = ...``: a source whose
    # name slugs to "default" would otherwise claim the slug, be written, and
    # then be silently overwritten by that assignment -- N sources in, N-1
    # represented, which is the exact loss _claim_slug exists to prevent.
    # Reserving it sends such a source to "default-2" and makes the collision
    # WARNING name both.
    claimed: dict[str, str] = {}
    if default_source is not None:
        claimed["default"] = default_source

    for source in capabilities.sources:
        slug = _claim_slug(source, claimed)
        profiles[slug] = ProfileConfig(
            source=source,
            resolution=resolution,
            mode=mode,
            auto_generated=True,
            auto_source_mode=_auto_source_mode(source, has_flatbed=has_flatbed),
            duplex=_duplex(source),
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
            # Mirrors the loop for the same reason: the default duplicates a
            # source profile and must not claim a different duplex strategy.
            duplex=_duplex(default_source),
        )

    return profiles


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
# back from that inside the tool: every command loads and validates settings
# before it runs (only ``--help`` skips that), so not even ``auto-profiles``
# could regenerate the key it just deleted, and the user has to hand-edit TOML. A previous run stamps every
# profile it writes with ``auto_generated = true``, ``default`` included, so
# without this guard a single run against a scanner with no flatbed -- an
# ordinary sheet-fed document scanner -- destroys a working installation.
_UNPRUNABLE = frozenset({"default"})


# The keys a generation writes, and so the keys the tool owns in a profile that
# carries ``auto_generated = true`` (D-02). ``--force`` overwrites exactly these
# on the existing table and deletes any of them the fresh generation omits
# (D-03); every other key -- default_tags, title, thresholds -- is the user's.
# A hand edit to an owned key is overwritten while the flag is set: to keep it,
# remove ``auto_generated`` and the profile is never touched again (D-01).
_OWNED_KEYS: Final = (
    "source",
    "resolution",
    "mode",
    "auto_source_mode",
    "duplex",
    "auto_generated",
)

# Why an unflagged same-name profile was left alone, and how to hand it back to
# the tool (D-01). There is deliberately no flag that overrides this.
_NOT_GENERATED_REASON: Final = (
    "not created by auto-profiles (no auto_generated = true); "
    "rename or delete it to regenerate"
)


@dataclass(frozen=True, slots=True)
class ProfileWriteResult:
    """
    What one ``write_profiles_to_config`` call did, grouped by action (D-04).

    The CLI and the worker's startup log both print it through ``describe``,
    so the two front ends share one vocabulary rather than two spellings.

    Attributes:
        path: The config file the result describes.
        added: Generated names the file did not have, now written.
        refreshed: Flagged profiles whose owned keys ``force`` rewrote.
        skipped_not_generated: Same-name profiles without a truthy
            ``auto_generated``, never touched, under ``force`` too (D-01).
        skipped_existing: Flagged profiles left alone because ``force`` was
            not passed.
        removed: Flagged profiles the scanner no longer produces, pruned
            (Phase 24 D-16).

    """

    path: Path
    added: tuple[str, ...] = ()
    refreshed: tuple[str, ...] = ()
    skipped_not_generated: tuple[str, ...] = ()
    skipped_existing: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    @property
    def persisted(self) -> frozenset[str]:
        """Names whose file table now matches the generated profile."""
        return frozenset(self.added + self.refreshed)

    def groups(self) -> list[tuple[str, tuple[str, ...]]]:
        """
        Pair each non-empty group's line with the names it wrote.

        The second element is the group's names for Added and Refreshed, whose
        profiles a front end may detail, and empty for every other group.

        Returns:
            ``(line, written_names)`` in the fixed order Added, Refreshed,
            Skipped (not auto-generated), Skipped (already exists), Removed.

        """
        labelled = (
            ("Added", self.added, "", True),
            ("Refreshed", self.refreshed, "", True),
            (
                "Skipped (not auto-generated)",
                self.skipped_not_generated,
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
        return lines

    def describe(self) -> list[str]:
        """
        Render the result as one line per non-empty group.

        Returns:
            The group lines, empty when nothing was added, refreshed, skipped
            or removed.

        """
        return [line for line, _ in self.groups()]


def _generated_values(profile: ProfileConfig) -> dict[str, str | int | bool]:
    """
    Build the owned key values a fresh generation writes for ``profile``.

    Insertion order is the file's key order for a new table. Only non-default
    values of ``auto_source_mode`` and ``duplex`` are included (Phase 25 D-06),
    so a refreshed table reads the way a freshly generated one does.

    Args:
        profile: A generated profile.

    Returns:
        The owned keys to write, in file order; always a subset of
        ``_OWNED_KEYS``.

    """
    values: dict[str, str | int | bool] = {
        "source": profile.source,
        "resolution": profile.resolution,
        "mode": profile.mode,
    }
    if profile.auto_source_mode != "flatbed":
        values["auto_source_mode"] = profile.auto_source_mode
    # Written only when non-default, like auto_source_mode. In a generated set
    # that means "hardware" on a FEEDER_DUPLEX source; see _duplex for why
    # nothing reads it yet. Omitting "none" cannot let the loader's legacy
    # translation turn a profile manual on reload: a "none" source classified
    # as something other than FEEDER_DUPLEX, so its name does not contain
    # "duplex".
    if profile.duplex != "none":
        values["duplex"] = profile.duplex
    values["auto_generated"] = True
    return values


def _read_config(config_path: Path) -> tuple[TOMLDocument, str]:
    """
    Parse the config file for a merge, or start an empty document.

    Args:
        config_path: The config file; it need not exist.

    Returns:
        The parsed document and the exact text it was parsed from (empty for
        a file that does not exist yet).

    Raises:
        ConfigError: The file's bytes are not valid UTF-8.

    """
    if not config_path.exists():
        return tomlkit.document(), ""
    # D-05 / Pitfall 4: bytes decoded as UTF-8 here. Path's text-mode reader
    # uses the locale encoding and translates CRLF to LF, silently re-encoding
    # the file and rewriting its line endings on the way back out.
    try:
        text = config_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        msg = f"{config_path} is not valid UTF-8; refusing to rewrite it"
        raise ConfigError(msg) from None
    return tomlkit.parse(text), text


def _render_checked(config_path: Path, doc: TOMLDocument, original_text: str) -> str:
    """
    Dump the merged document, keep its line endings, and prove it parses.

    Args:
        config_path: The config file, for the error message.
        doc: The merged document.
        original_text: The text the document was parsed from.

    Returns:
        The text to write.

    Raises:
        ConfigError: The dumped text is not valid TOML; nothing was written.

    """
    new_text = tomlkit.dumps(doc)
    if "\r\n" in original_text:
        # tomlkit keeps the CRLF of the lines it parsed but ends the lines it
        # adds with a bare LF; normalise those so the file stays CRLF (D-05).
        new_text = re.sub(r"(?<!\r)\n", "\r\n", new_text)
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        # The guard: whatever shape the operator's file has, and whatever
        # tomlkit makes of it, text that does not parse never replaces a
        # working config.
        msg = (
            f"Cannot update {config_path}: the merged profiles do not form "
            "valid TOML; refusing to rewrite it, so the file is left intact"
        )
        raise ConfigError(msg) from None
    return new_text


_MergeOutcome = Literal[
    "added", "refreshed", "skipped_not_generated", "skipped_existing"
]


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
        # D-01: not created by the tool (a stray scalar included), so not the
        # tool's to change. Reported, never overwritten, under force too.
        return "skipped_not_generated"
    if not force:
        return "skipped_existing"
    # D-02: keys are set on the EXISTING table -- never a fresh table assigned
    # over it, which would drop default_tags, title and the comments. D-03: an
    # owned key this generation omits is deleted, so a stale
    # ``duplex = "hardware"`` does not outlive the source that produced it.
    owned = cast("MutableMapping[str, object]", existing)
    for key in _OWNED_KEYS:
        if key in values:
            owned[key] = values[key]
        elif key in owned:
            del owned[key]
    return "refreshed"


def write_profiles_to_config(
    config_path: Path,
    profiles: dict[str, ProfileConfig],
    *,
    force: bool = False,
) -> ProfileWriteResult:
    """
    Merge generated profiles into a TOML config file, preserving what is there.

    Uses tomlkit for comment-preserving TOML round-tripping. For each
    generated name:

    * absent from the file: a new table is added;
    * present without a truthy ``auto_generated``: left byte for byte alone and
      reported, whether or not ``force`` is passed (D-01);
    * present and flagged, without ``force``: skipped as already existing;
    * present and flagged, with ``force``: the owned keys (``_OWNED_KEYS``) are
      written onto the existing table and any the generation omits are
      deleted, so every other key and every comment survives (D-02, D-03).

    Auto-generated profiles that the freshly generated set no longer names are
    pruned first, so renaming does not strand the profiles it replaced. The
    prune runs whether or not ``force`` is passed, and that is deliberate:
    ``force`` governs refreshing profiles that are *present* in the generated
    set, while an orphan is by definition absent from it, so ``force`` has
    nothing to say about it.

    ``default`` is never pruned either, whatever it is flagged with: it is not
    an ordinary profile but a schema requirement (``_UNPRUNABLE``), and a
    config missing it is one saneless refuses to load.

    Args:
        config_path: Path to the TOML config file.
        profiles: Dictionary of profile name to ProfileConfig.
        force: If True, refresh the owned keys of flagged profiles.

    The rewrite is durable (CFG-08): the file is read as UTF-8 bytes, CRLF
    line endings are kept, the new text is re-parsed before anything is
    replaced, and ``replace_file_atomically`` swaps it in through any symlink
    (D-05, D-07). A merge that changes nothing does not rewrite the file.

    Args:
        config_path: Path to the TOML config file.
        profiles: Dictionary of profile name to ProfileConfig.
        force: If True, refresh the owned keys of flagged profiles.

    Returns:
        What was added, refreshed, skipped and removed, with ``path`` the real
        file (the symlink's target when ``config_path`` is a link).

    Raises:
        ConfigError: ``[profiles]`` in the file is not a table, the file is not
            valid UTF-8, the merged text does not parse as TOML, or the file is
            bind-mounted as a single file (EBUSY) and cannot be replaced.
        OSError: Any other failure to read or replace the file, including
            ``PermissionError`` for a file this process may not write.

    """
    doc, original_text = _read_config(config_path)

    if "profiles" not in doc:
        doc.add("profiles", tomlkit.table(is_super_table=True))

    # ``cast`` is a promise to the type checker, not a check. A config whose
    # ``profiles`` key is a scalar -- ``profiles = "oops"`` -- reaches
    # ``.items()`` on a tomlkit String, and the user gets a raw AttributeError
    # traceback out of ``auto-profiles`` instead of a configuration error.
    # ``_is_auto_generated`` already guards exactly this risk one level down,
    # for each entry; the container itself was not given the same treatment.
    section = doc["profiles"]
    if not isinstance(section, Mapping):
        msg = f"[profiles] in {config_path} is not a table; refusing to overwrite it"
        raise ConfigError(msg)
    profiles_section = cast("dict[str, object]", section)

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

    outcomes: dict[_MergeOutcome, list[str]] = {
        "added": [],
        "refreshed": [],
        "skipped_not_generated": [],
        "skipped_existing": [],
    }
    for name, profile in profiles.items():
        outcome = _merge_profile(profiles_section, name, profile, force=force)
        outcomes[outcome].append(name)

    if not (outcomes["added"] or outcomes["refreshed"] or orphans):
        # Nothing changed, so nothing is replaced -- and a legacy single-file
        # mount gets no EBUSY error for a run that had nothing to write.
        target = config_path.resolve()
    else:
        new_text = _render_checked(config_path, doc, original_text)
        target = replace_file_atomically(config_path, new_text)
        if target != config_path.absolute():
            # D-07: the operator edits the link, the write lands on the target;
            # naming both explains which file changed.
            logger.info("Wrote profiles to %s (symlink to %s)", config_path, target)

    return ProfileWriteResult(
        path=target,
        added=tuple(outcomes["added"]),
        refreshed=tuple(outcomes["refreshed"]),
        skipped_not_generated=tuple(outcomes["skipped_not_generated"]),
        skipped_existing=tuple(outcomes["skipped_existing"]),
        removed=tuple(orphans),
    )
