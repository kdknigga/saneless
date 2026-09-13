"""Tests for automatic scanner profile generation."""

from __future__ import annotations

import logging
import re
import tomllib
from typing import TYPE_CHECKING

import pytest

from saneless.auto_profiles import (
    generate_profiles,
    is_bare_default,
    pick_closest_resolution,
    pick_preferred_mode,
    resolve_config_path,
    source_to_slug,
    write_profiles_to_config,
)
from saneless.config import (
    DEFAULT_RESOLUTION,
    ProfileConfig,
    Settings,
    load_settings,
)
from saneless.exceptions import ConfigError
from saneless.scanner.base import DeviceCapabilities

if TYPE_CHECKING:
    from pathlib import Path


_SLUG_INPUTS = [
    "Flatbed",
    "ADF",
    "Automatic Document Feeder",
    "ADF Duplex",
    "Adf-duplex",
    "ADF Front",
    "ADF Back",
    "Auto",
    "auto",
    "Flatbed Duplex",
    "ADF (left aligned)",
    "Flachbett/Einzug",
    "ADF  Duplex",
    "---ADF---",
    "  ",
]


class TestSourceToSlug:
    """Source name to profile slug conversion."""

    def test_flatbed(self) -> None:
        """Flatbed slugs from its own name (D-14)."""
        assert source_to_slug("Flatbed") == "flatbed"

    def test_adf(self) -> None:
        """ADF slugs from its own name (D-14)."""
        assert source_to_slug("ADF") == "adf"

    def test_automatic_document_feeder(self) -> None:
        """Automatic Document Feeder slugs from its own name (D-14)."""
        assert (
            source_to_slug("Automatic Document Feeder") == "automatic-document-feeder"
        )

    def test_adf_duplex(self) -> None:
        """ADF Duplex slugs from its own name (D-14)."""
        assert source_to_slug("ADF Duplex") == "adf-duplex"

    def test_case_insensitive_adf_duplex(self) -> None:
        """Slugging lowercases whatever case the device reports."""
        assert source_to_slug("Adf-duplex") == "adf-duplex"

    def test_adf_front(self) -> None:
        """ADF Front slugs from its own name (D-14)."""
        assert source_to_slug("ADF Front") == "adf-front"

    def test_auto_source(self) -> None:
        """Auto slugs from its own name (D-14)."""
        assert source_to_slug("Auto") == "auto"

    def test_flatbed_duplex_slugs_from_its_own_name(self) -> None:
        """
        A name carrying both "flatbed" and "duplex" slugs verbatim (CTR-04).

        This case used to assert classify_source's branch order *through*
        source_to_slug: the slug was picked from the returned SourceKind, and
        because duplex is tested before flatbed (scanner/base.py) this name
        took the duplex kind's hard-coded name rather than the flatbed kind's.
        D-14 severed that coupling -- the slug is now the device's own wording,
        so this case can no longer witness the precedence. It is re-pointed
        rather than deleted
        so the change of meaning is recorded; the classifier's branch order is
        asserted directly against classify_source in the scanner tests.
        """
        assert source_to_slug("Flatbed Duplex") == "flatbed-duplex"

    def test_auto_is_matched_exactly_not_as_a_substring(self) -> None:
        """
        "Automatic Document Feeder" is a feeder, not an Auto source (CTR-04).

        The name begins with the letters "auto"; a substring rule would have
        collapsed it onto the Auto source's slug and routed a stack of pages
        down the single-page path. D-14 makes that collapse impossible by
        construction -- each source keeps its own wording -- so the guarantee
        survives here as an assertion on the two distinct new values.
        """
        assert (
            source_to_slug("Automatic Document Feeder") == "automatic-document-feeder"
        )
        assert source_to_slug("Auto") == "auto"

    def test_auto_source_case_insensitive(self) -> None:
        """A lowercase "auto" slugs the same as "Auto"."""
        assert source_to_slug("auto") == "auto"

    def test_adf_back_slugs_from_its_own_name(self) -> None:
        """ADF Back slugs verbatim rather than onto a shared feeder name."""
        assert source_to_slug("ADF Back") == "adf-back"

    def test_adf_front_and_back_never_collide(self) -> None:
        """
        Two distinct feeder sources never collapse onto one slug (N-09).

        "ADF Front" and "ADF Back" both classify as FEEDER -- they ARE feeders
        for routing purposes -- so any rule that named the profile from the
        SourceKind gave them the same slug and silently lost one. D-14 names
        from the source string itself, so distinctness holds by construction.
        """
        assert source_to_slug("ADF Front") != source_to_slug("ADF Back")

    def test_parentheses_do_not_survive(self) -> None:
        """Canon's "ADF (left aligned)" loses its parentheses (D-15)."""
        assert source_to_slug("ADF (left aligned)") == "adf-left-aligned"

    def test_slash_does_not_survive(self) -> None:
        """
        A path separator never reaches the slug (D-15).

        Measured before the hardening: "Flachbett/Einzug" slugged
        "flachbett/einzug", passing "/" straight through. Slugs are TOML keys
        rather than filesystem paths today, so this was not exploitable -- but
        it is one careless reuse away, which is exactly why Phase 23's D-19
        refused to reuse this sanitiser for PDF filenames.
        """
        assert source_to_slug("Flachbett/Einzug") == "flachbett-einzug"

    def test_hyphen_runs_collapse(self) -> None:
        """A doubled space collapses to a single hyphen (D-15)."""
        assert source_to_slug("ADF  Duplex") == "adf-duplex"

    def test_leading_and_trailing_hyphens_are_stripped(self) -> None:
        """Leading and trailing hyphens are stripped (D-15)."""
        assert source_to_slug("---ADF---") == "adf"

    def test_degenerate_name_still_yields_a_usable_slug(self) -> None:
        """
        A whitespace-only source name still yields a non-empty slug (D-15).

        Measured before the hardening: "  " degenerated to "--", which is not
        addressable as a --profile value. The guarded fallback keeps the
        profile reachable.
        """
        slug = source_to_slug("  ")
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug), slug


class TestSlugCharacterSet:
    """The D-15 character-set invariant, over every name the suite exercises."""

    @pytest.mark.parametrize("source", _SLUG_INPUTS)
    def test_slug_uses_only_lowercase_alphanumerics_and_hyphens(
        self, source: str
    ) -> None:
        """Every slug is [a-z0-9-] with no leading, trailing or doubled hyphen."""
        slug = source_to_slug(source)
        assert re.fullmatch(r"[a-z0-9-]+", slug), slug
        assert not slug.startswith("-"), slug
        assert not slug.endswith("-"), slug
        assert "--" not in slug, slug


class TestPickClosestResolution:
    """Resolution selection logic."""

    def test_exact_match(self) -> None:
        """Returns DEFAULT_RESOLUTION when it is available."""
        assert (
            pick_closest_resolution([150, 300, 600], target=DEFAULT_RESOLUTION)
            == DEFAULT_RESOLUTION
        )

    def test_nearest_when_no_exact(self) -> None:
        """Returns nearest resolution when DEFAULT_RESOLUTION is not available."""
        result = pick_closest_resolution([150, 600], target=DEFAULT_RESOLUTION)
        assert result in (150, 600)

    def test_empty_returns_target(self) -> None:
        """Returns target when no resolutions available."""
        assert (
            pick_closest_resolution([], target=DEFAULT_RESOLUTION) == DEFAULT_RESOLUTION
        )


class TestPickClosestResolutionHonoursARange:
    """
    A range-reporting device gets a resolution it actually offers (D-13, N-01).

    This function returned the target unchanged whenever the word list was
    empty -- which is precisely what a range-reporting device produces. So the
    SANE ``test`` backend got 300 not because it offered 300 but because nothing
    had been read at all, and a device whose ceiling sat below 300 was asked for
    a resolution it had never advertised.

    A word list is an exhaustive enumeration, so it still wins when present; a
    range is a span, and the value chosen from it has to be both inside the span
    and on the step grid, or it is still a value the device never offered.
    """

    @staticmethod
    def _assert_on_the_step_grid(
        result: int, resolution_range: tuple[float, float, float]
    ) -> None:
        """
        Assert a chosen value is inside the range and reachable by its step.

        Args:
            result: The resolution chosen.
            resolution_range: The ``(min, max, step)`` the device reported.

        """
        low, high, step = resolution_range
        assert low <= result <= high
        remainder = (result - low) % step
        assert min(remainder, step - remainder) == pytest.approx(0.0, abs=1e-6)

    @pytest.mark.parametrize(
        ("resolution_range", "expected"),
        [
            ((1.0, 1200.0, 1.0), 300),
            ((1.0, 200.0, 1.0), 200),
            ((400.0, 1200.0, 100.0), 400),
            ((40.0, 1200.0, 100.0), 340),
        ],
        ids=[
            "target-inside-the-range",
            "clamped-to-the-maximum",
            "clamped-to-the-minimum",
            "snapped-onto-the-step",
        ],
    )
    def test_the_value_returned_is_one_the_device_could_accept(
        self,
        resolution_range: tuple[float, float, float],
        expected: int,
    ) -> None:
        """
        Each case clamps into the range and lands on the step grid.

        The last case is deliberately not a tie: 300 sits 2.6 steps above 40, so
        the nearest reachable value is unambiguously 340. A tie such as 2.5
        steps would silently encode Python's banker's rounding as though it were
        a decision about scanners.
        """
        result = pick_closest_resolution(
            [], target=DEFAULT_RESOLUTION, resolution_range=resolution_range
        )

        assert result == expected
        self._assert_on_the_step_grid(result, resolution_range)

    def test_a_word_list_still_wins_over_a_range(self) -> None:
        """An exhaustive enumeration beats a span; list behaviour is unchanged."""
        result = pick_closest_resolution(
            [150, 600],
            target=DEFAULT_RESOLUTION,
            resolution_range=(1.0, 1200.0, 1.0),
        )

        assert result in (150, 600)

    def test_a_device_constraining_nothing_still_leaves_the_target_alone(self) -> None:
        """Neither shape reported means there is nothing to honour."""
        assert (
            pick_closest_resolution(
                [], target=DEFAULT_RESOLUTION, resolution_range=None
            )
            == DEFAULT_RESOLUTION
        )

    def test_generated_profiles_use_a_resolution_the_range_allows(self) -> None:
        """
        End to end: a range-only device no longer gets a silent 300.

        A device whose ceiling is 200 dpi would previously have had every
        generated profile ask for 300 -- a resolution it cannot deliver, which
        SANE then silently substitutes.
        """
        caps = DeviceCapabilities(
            sources=["Flatbed"],
            resolutions=[],
            modes=["Color"],
            resolution_range=(1.0, 200.0, 1.0),
        )

        profiles = generate_profiles(caps)

        assert profiles["flatbed"].resolution == 200
        assert profiles["default"].resolution == 200


class TestPickPreferredMode:
    """Mode selection logic."""

    def test_preferred_available(self) -> None:
        """Returns Color when available."""
        assert pick_preferred_mode(["Color", "Gray"], preferred="Color") == "Color"

    def test_preferred_not_available(self) -> None:
        """Returns first mode when preferred not available."""
        assert pick_preferred_mode(["Gray", "Lineart"], preferred="Color") == "Gray"

    def test_empty_returns_preferred(self) -> None:
        """Returns preferred when no modes available."""
        assert pick_preferred_mode([], preferred="Color") == "Color"


class TestIsBareDefault:
    """Bare default profile detection."""

    def test_bare_default(self) -> None:
        """Uncustomized single default profile is bare."""
        settings = Settings(profiles={"default": ProfileConfig()})
        assert is_bare_default(settings) is True

    def test_multiple_profiles_not_bare(self) -> None:
        """Multiple profiles is not bare default."""
        settings = Settings(
            profiles={"default": ProfileConfig(), "custom": ProfileConfig()},
        )
        assert is_bare_default(settings) is False

    def test_customized_default_not_bare(self) -> None:
        """Customized default profile is not bare."""
        settings = Settings(
            profiles={"default": ProfileConfig(resolution=600)},
        )
        assert is_bare_default(settings) is False

    def test_auto_generated_not_bare(self) -> None:
        """Already auto-generated default is not bare."""
        settings = Settings(
            profiles={"default": ProfileConfig(auto_generated=True)},
        )
        assert is_bare_default(settings) is False


class TestResolveConfigPath:
    """Config path resolution."""

    def test_explicit_path(self) -> None:
        """Explicit path is returned as-is."""
        result = resolve_config_path(config_path="/explicit/path.toml")
        assert str(result) == "/explicit/path.toml"

    def test_none_returns_default(self, tmp_path: Path) -> None:
        """None config_path returns default saneless.toml path."""
        result = resolve_config_path(config_path=None)
        assert result.name.endswith(".toml")


class TestGenerateProfiles:
    """Profile generation from scanner capabilities."""

    def test_single_source(self) -> None:
        """Single flatbed source generates flatbed and default profiles."""
        caps = DeviceCapabilities(
            sources=["Flatbed"],
            resolutions=[150, 300, 600],
            modes=["Color", "Gray"],
        )
        profiles = generate_profiles(caps)
        assert "flatbed" in profiles
        assert "default" in profiles
        assert profiles["flatbed"].source == "Flatbed"
        assert profiles["flatbed"].resolution == 300
        assert profiles["flatbed"].mode == "Color"
        assert profiles["flatbed"].auto_generated is True

    def test_multiple_sources(self) -> None:
        """Multiple sources generate correct number of profiles."""
        caps = DeviceCapabilities(
            sources=["Flatbed", "ADF", "ADF Duplex"],
            resolutions=[200, 400],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert "flatbed" in profiles
        assert "adf" in profiles
        assert "adf-duplex" in profiles
        assert "default" in profiles
        assert len(profiles) == 4
        # Closest to 300 from [200, 400] is 200
        assert profiles["flatbed"].resolution == 200

    def test_all_auto_generated(self) -> None:
        """All generated profiles have auto_generated=True."""
        caps = DeviceCapabilities(
            sources=["Flatbed"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        for profile in profiles.values():
            assert profile.auto_generated is True


class TestGenerateProfilesAutoSource:
    """Auto source handling in profile generation."""

    def test_auto_with_adf_no_flatbed_sets_adf_mode(self) -> None:
        """Auto source defaults to adf mode when no Flatbed source exists."""
        caps = DeviceCapabilities(
            sources=["Auto", "ADF"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert "auto" in profiles
        assert profiles["auto"].auto_source_mode == "adf"

    def test_auto_with_flatbed_sets_flatbed_mode(self) -> None:
        """Auto source defaults to flatbed mode when Flatbed source exists."""
        caps = DeviceCapabilities(
            sources=["Auto", "Flatbed"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert "auto" in profiles
        assert profiles["auto"].auto_source_mode == "flatbed"

    def test_auto_only_sets_adf_mode(self) -> None:
        """Auto source alone (no Flatbed, no ADF) defaults to adf mode."""
        caps = DeviceCapabilities(
            sources=["Auto"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert "auto" in profiles
        assert profiles["auto"].auto_source_mode == "adf"

    def test_flatbed_no_regression(self) -> None:
        """Flatbed source slugs to flatbed (no regression)."""
        assert source_to_slug("Flatbed") == "flatbed"


class TestGenerateProfilesUsesClassifier:
    """generate_profiles asks classify_source, not its own string rules."""

    def test_auto_spelled_with_surrounding_whitespace(self) -> None:
        """
        " AUTO " still receives the auto_source_mode treatment (D-02, Q9).

        The rule this replaces was ``source.lower() == "auto"``, which a device
        reporting stray whitespace defeats: " auto " is not "auto", so the
        source fell through to the flatbed default and a single-source Auto
        scanner was told to behave as a flatbed. classify_source strips before
        comparing, so every spelling reaches the same branch.
        """
        caps = DeviceCapabilities(
            sources=[" AUTO "],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert profiles["auto"].auto_source_mode == "adf"

    def test_auto_spelled_lowercase(self) -> None:
        """
        A lowercase "auto" receives the auto_source_mode treatment.

        This spelling was already handled by the equality rule -- it is
        asserted so the swap to classify_source cannot quietly regress it.
        """
        caps = DeviceCapabilities(
            sources=["auto"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert profiles["auto"].auto_source_mode == "adf"

    def test_duplex_feeder_is_not_mistaken_for_a_flatbed(self) -> None:
        """
        "Flatbed Duplex" is a duplex feeder, so it never backs the default.

        The rule this replaces asked ``"flatbed" in s.lower()``, which is true
        of "Flatbed Duplex" -- so a duplex feeder became the flatbed-backed
        default profile, and an Auto source beside it was told to behave as a
        flatbed. classify_source tests duplex before flatbed, deliberately, and
        answers FEEDER_DUPLEX.
        """
        caps = DeviceCapabilities(
            sources=["Auto", "Flatbed Duplex"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        # The default exists (Settings requires it) but is backed by the first
        # reported source, never by the duplex feeder.
        assert profiles["default"].source == "Auto"
        assert profiles["default"].source != "Flatbed Duplex"
        assert profiles["auto"].auto_source_mode == "adf"

    def test_real_flatbed_still_backs_the_default_profile(self) -> None:
        """A genuine Flatbed source still becomes the default profile."""
        caps = DeviceCapabilities(
            sources=["Flatbed", "ADF"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert profiles["default"].source == "Flatbed"

    def test_feeder_only_device_still_gets_a_default_profile(self) -> None:
        """
        A device reporting no flatbed source falls back to its first source.

        This assertion used to read ``"default" not in profiles``, which pinned
        a defect rather than a guarantee: Settings requires the key, so the set
        this function returned for a sheet-fed scanner could be written to disk
        and then never loaded again.
        """
        caps = DeviceCapabilities(
            sources=["Automatic Document Feeder"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert profiles["default"].source == "Automatic Document Feeder"

    def test_a_feeder_only_config_round_trips_through_load_settings(
        self, tmp_path: Path
    ) -> None:
        """
        The written file loads back, which is the guarantee that matters.

        "default is present" is a proxy; a config saneless can actually load
        after auto-profiles has run is the user-visible promise.
        """
        caps = DeviceCapabilities(
            sources=["Automatic Document Feeder", "ADF Duplex"],
            resolutions=[300],
            modes=["Color"],
        )
        config_file = tmp_path / "config.toml"
        write_profiles_to_config(config_file, generate_profiles(caps))

        settings = load_settings(str(config_file))
        assert settings.profiles["default"].source == "Automatic Document Feeder"


class TestGenerateProfilesSlugCollision:
    """Two names that normalise alike both survive, with a tie-break (Q5)."""

    _COLLIDING = ("ADF-Front", "ADF Front")

    def _caps(self) -> DeviceCapabilities:
        """Capabilities whose two source names normalise to the same slug."""
        return DeviceCapabilities(
            sources=list(self._COLLIDING),
            resolutions=[300],
            modes=["Color"],
        )

    def test_both_sources_survive_with_a_suffix(self) -> None:
        """The first claimant keeps the bare slug; the second gains "-2"."""
        profiles = generate_profiles(self._caps())
        # "default" is not a source profile; it is the key Settings requires.
        assert set(profiles) == {"adf-front", "adf-front-2", "default"}
        assert profiles["adf-front"].source == "ADF-Front"
        assert profiles["adf-front-2"].source == "ADF Front"

    def test_no_source_is_silently_lost(self) -> None:
        """
        N distinct source strings yield N source profiles.

        The assignment was unguarded, so the second collider overwrote the
        first and the device lost a source -- which is N-09's actual complaint.
        Counting keys is no longer the way to ask: the required "default" key
        aliases one of the sources, so the question is put to the source
        strings the profiles actually carry.
        """
        profiles = generate_profiles(self._caps())
        sources = [
            profile.source for name, profile in profiles.items() if name != "default"
        ]
        assert len(sources) == len(self._COLLIDING)
        assert set(sources) == set(self._COLLIDING)

    def test_tie_break_follows_source_order_and_is_deterministic(self) -> None:
        """Generating twice from the same capabilities yields identical slugs."""
        assert list(generate_profiles(self._caps())) == list(
            generate_profiles(self._caps())
        )

    def test_collision_warning_names_both_sources(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The WARNING names both colliding source strings and the new slug."""
        with caplog.at_level(logging.WARNING, logger="saneless.auto_profiles"):
            generate_profiles(self._caps())

        message = "\n".join(
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        )
        assert "ADF-Front" in message
        assert "ADF Front" in message
        assert "adf-front-2" in message


class TestAutoGeneratedField:
    """ProfileConfig auto_generated field."""

    def test_default_false(self) -> None:
        """auto_generated defaults to False."""
        profile = ProfileConfig()
        assert profile.auto_generated is False

    def test_set_true(self) -> None:
        """auto_generated can be set to True."""
        profile = ProfileConfig(auto_generated=True)
        assert profile.auto_generated is True


class TestMalformedProfilesSection:
    """
    A ``[profiles]`` key that is not a table is refused, not crashed on (WR-05).

    The parsed document was cast to a shape it is not guaranteed to have, and
    ``cast`` is a promise to the type checker rather than a check, so
    ``profiles = "oops"`` reached ``.items()`` on a tomlkit String. The user
    saw an unhandled AttributeError out of ``auto-profiles``.
    """

    _MALFORMED = 'profiles = "oops"\n'

    def _generated(self) -> dict[str, ProfileConfig]:
        """Build any non-empty generated set; the failure precedes its use."""
        return {
            "flatbed": ProfileConfig(
                source="Flatbed", resolution=300, mode="Color", auto_generated=True
            )
        }

    def test_a_scalar_profiles_key_raises_a_config_error(self, tmp_path: Path) -> None:
        """The domain's own error type, not AttributeError from tomlkit."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(self._MALFORMED)

        with pytest.raises(ConfigError, match="not a table"):
            write_profiles_to_config(config_file, self._generated())

    def test_the_malformed_file_is_left_untouched(self, tmp_path: Path) -> None:
        """Refusing to overwrite means the user's file is still theirs."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(self._MALFORMED)

        with pytest.raises(ConfigError):
            write_profiles_to_config(config_file, self._generated())

        assert config_file.read_text() == self._MALFORMED


class TestReservedDefaultSlug:
    """
    A source whose slug is "default" is not overwritten by the default profile.

    ``_claim_slug`` exists so that N distinct source strings yield N source
    profiles, but "default" was never reserved and the default profile is
    assigned after the loop with a bare ``profiles["default"] = ...``. A
    scanner reporting a source literally named "Default" therefore claimed the
    slug, was written, and was then overwritten: two sources in, one source
    represented -- the precise invariant _claim_slug was added to guarantee,
    broken by the line that runs after it.
    """

    _SOURCES = ("Default", "Flatbed")

    def _caps(self) -> DeviceCapabilities:
        """Build capabilities whose first source slugs to the reserved name."""
        return DeviceCapabilities(
            sources=list(self._SOURCES),
            resolutions=[300],
            modes=["Color"],
        )

    def test_no_source_is_lost_to_the_reserved_name(self) -> None:
        """Both source strings still appear on a source profile."""
        profiles = generate_profiles(self._caps())
        sources = [
            profile.source for name, profile in profiles.items() if name != "default"
        ]
        assert set(sources) == set(self._SOURCES)

    def test_the_colliding_source_gains_a_suffix(self) -> None:
        """The reserved slug pushes the source to the tie-break name."""
        profiles = generate_profiles(self._caps())
        assert profiles["default-2"].source == "Default"

    def test_the_default_profile_is_still_the_flatbed(self) -> None:
        """Reserving the slug must not change which source backs the default."""
        profiles = generate_profiles(self._caps())
        assert profiles["default"].source == "Flatbed"

    def test_the_collision_warning_names_the_reserved_slug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The operator is told the source was renamed, and to what."""
        with caplog.at_level(logging.WARNING, logger="saneless.auto_profiles"):
            generate_profiles(self._caps())

        message = "\n".join(
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        )
        assert "Default" in message
        assert "default-2" in message


class TestOrphanedProfilePrune:
    """Auto-generated profiles absent from the new set are pruned (D-16)."""

    # "legacy-feeder" stands for a profile generated under the pre-D-14 naming
    # scheme, which the rename strands. It is given a neutral name on purpose:
    # the slugs D-14 retired must appear nowhere in the tree, so this fixture
    # cannot spell them even as historic content. The prune keys on the
    # auto_generated flag, never on the name, so the substitution is faithful.
    _EXISTING = """\
# saneless configuration -- hand written, keep this comment
[profiles.default]
source = "Flatbed"  # the one I actually use
resolution = 600
mode = "Gray"

[profiles.legacy-feeder]
source = "ADF"
resolution = 300
mode = "Color"
auto_generated = true

[profiles.handwritten]
source = "ADF"
resolution = 150
mode = "Gray"
auto_generated = false
"""

    def _generated(self) -> dict[str, ProfileConfig]:
        """Build a generated set that names none of the existing profiles."""
        return {
            "adf": ProfileConfig(
                source="ADF", resolution=300, mode="Color", auto_generated=True
            ),
            "flatbed": ProfileConfig(
                source="Flatbed", resolution=300, mode="Color", auto_generated=True
            ),
        }

    def _write(self, tmp_path: Path, *, force: bool) -> Path:
        """Write the generated set over the existing config and return the path."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(self._EXISTING)
        write_profiles_to_config(config_file, self._generated(), force=force)
        return config_file

    @pytest.mark.parametrize("force", [False, True])
    def test_orphaned_auto_generated_profile_is_removed(
        self, tmp_path: Path, *, force: bool
    ) -> None:
        """
        The orphan is pruned whether or not force is passed.

        force governs overwriting keys that are *present* in the generated set;
        an orphan is by definition absent from it, so force has nothing to say
        about it. Without the prune a rename leaves the stale profile behind,
        still functional, which is the quiet duplication D-16 prevents.
        """
        config_file = self._write(tmp_path, force=force)
        parsed = tomllib.loads(config_file.read_text())
        assert "legacy-feeder" not in parsed["profiles"]

    @pytest.mark.parametrize("force", [False, True])
    def test_profiles_without_a_true_flag_are_never_touched(
        self, tmp_path: Path, *, force: bool
    ) -> None:
        """A profile with no flag, or an explicit false, survives intact."""
        config_file = self._write(tmp_path, force=force)
        profiles = tomllib.loads(config_file.read_text())["profiles"]

        assert profiles["default"]["source"] == "Flatbed"
        assert profiles["default"]["resolution"] == 600
        assert profiles["default"]["mode"] == "Gray"
        assert profiles["handwritten"]["resolution"] == 150
        assert profiles["handwritten"]["auto_generated"] is False

    def test_comments_survive_the_prune(self, tmp_path: Path) -> None:
        """Preserve file-level and inline comments through tomlkit's round trip."""
        content = self._write(tmp_path, force=False).read_text()
        assert "# saneless configuration -- hand written, keep this comment" in content
        assert "# the one I actually use" in content

    def test_generated_profiles_are_still_written(self, tmp_path: Path) -> None:
        """The freshly generated profiles land in the file alongside the prune."""
        parsed = tomllib.loads(self._write(tmp_path, force=False).read_text())
        assert "adf" in parsed["profiles"]
        assert "flatbed" in parsed["profiles"]

    def test_written_list_names_only_written_profiles(self, tmp_path: Path) -> None:
        """The returned list names what was written, never what was pruned."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(self._EXISTING)
        written = write_profiles_to_config(config_file, self._generated())
        assert set(written) == {"adf", "flatbed"}


class TestDefaultProfileSurvivesThePrune:
    """
    The prune must never remove ``default``, because Settings requires it.

    ``write_profiles_to_config`` stamps ``auto_generated = true`` on every
    profile it writes, ``default`` included, so a ``default`` that a previous
    ``auto-profiles`` run produced is indistinguishable from any other orphan
    the moment the user points saneless at a scanner with no flatbed. Pruning
    it leaves a config that fails ``Settings`` validation, and ``cli()`` loads
    settings before dispatch, so not even ``auto-profiles`` can regenerate it.
    """

    # Exactly what a previous auto-profiles run against a flatbed+ADF scanner
    # leaves behind: every profile carries the flag the writer always stamps.
    _PREVIOUS_RUN = """\
[profiles.default]
source = "Flatbed"
resolution = 300
mode = "Color"
auto_generated = true

[profiles.flatbed]
source = "Flatbed"
resolution = 300
mode = "Color"
auto_generated = true
"""

    def _feeder_only(self) -> dict[str, ProfileConfig]:
        """Build a generated set from a sheet-fed scanner, naming no flatbed."""
        return {
            "adf-duplex": ProfileConfig(
                source="ADF Duplex",
                resolution=300,
                mode="Color",
                auto_generated=True,
            ),
        }

    def _rerun(self, tmp_path: Path) -> Path:
        """Re-run the writer against a feeder-only device and return the path."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(self._PREVIOUS_RUN)
        write_profiles_to_config(config_file, self._feeder_only())
        return config_file

    def test_an_auto_generated_default_survives(self, tmp_path: Path) -> None:
        """``default`` stays even though the new set does not name it."""
        parsed = tomllib.loads(self._rerun(tmp_path).read_text())
        assert "default" in parsed["profiles"]

    def test_other_orphans_are_still_pruned(self, tmp_path: Path) -> None:
        """The guard is one name wide, not a disabling of the prune."""
        parsed = tomllib.loads(self._rerun(tmp_path).read_text())
        assert "flatbed" not in parsed["profiles"]
        assert "adf-duplex" in parsed["profiles"]

    def test_the_written_config_still_loads(self, tmp_path: Path) -> None:
        """
        The file the writer leaves behind round-trips through load_settings.

        This is the assertion that matters: "default is present" is a proxy,
        while loading the config is the thing the user actually needs to work
        after running auto-profiles.
        """
        settings = load_settings(str(self._rerun(tmp_path)))
        assert "default" in settings.profiles
        assert "adf-duplex" in settings.profiles


class TestTomlWriting:
    """TOML config file writing with comment preservation."""

    def test_preserves_comments(self, tmp_path: Path) -> None:
        """Writing profiles preserves existing TOML comments."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[scanner]\nhost = ""  # SANE network host\n',
        )

        profiles = {
            "default": ProfileConfig(
                source="Flatbed",
                resolution=300,
                mode="Color",
                auto_generated=True,
            ),
        }
        written = write_profiles_to_config(config_file, profiles)

        content = config_file.read_text()
        assert "# SANE network host" in content
        assert "flatbed" not in written or "default" in written
        assert "default" in written

    def test_skip_existing_without_force(self, tmp_path: Path) -> None:
        """Existing profiles are not overwritten without force."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[profiles.default]\nsource = "ADF"\nresolution = 600\n'
            'mode = "Gray"\nauto_generated = false\n',
        )

        profiles = {
            "default": ProfileConfig(
                source="Flatbed",
                resolution=300,
                mode="Color",
                auto_generated=True,
            ),
        }
        written = write_profiles_to_config(config_file, profiles)

        assert written == []
        content = config_file.read_text()
        assert 'source = "ADF"' in content

    def test_overwrite_with_force(self, tmp_path: Path) -> None:
        """Existing profiles are overwritten with force=True."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[profiles.default]\nsource = "ADF"\nresolution = 600\n'
            'mode = "Gray"\nauto_generated = false\n',
        )

        profiles = {
            "default": ProfileConfig(
                source="Flatbed",
                resolution=300,
                mode="Color",
                auto_generated=True,
            ),
        }
        written = write_profiles_to_config(config_file, profiles, force=True)

        assert "default" in written
        content = config_file.read_text()
        assert 'source = "Flatbed"' in content

    def test_writes_auto_source_mode_adf(self, tmp_path: Path) -> None:
        """Writes auto_source_mode when value is adf (non-default)."""
        config_file = tmp_path / "config.toml"
        profiles = {
            "auto": ProfileConfig(
                source="Auto",
                resolution=300,
                mode="Color",
                auto_source_mode="adf",
                auto_generated=True,
            ),
        }
        write_profiles_to_config(config_file, profiles)
        content = config_file.read_text()
        assert 'auto_source_mode = "adf"' in content

    def test_omits_auto_source_mode_flatbed(self, tmp_path: Path) -> None:
        """Does NOT write auto_source_mode when value is flatbed (default)."""
        config_file = tmp_path / "config.toml"
        profiles = {
            "auto": ProfileConfig(
                source="Auto",
                resolution=300,
                mode="Color",
                auto_source_mode="flatbed",
                auto_generated=True,
            ),
        }
        write_profiles_to_config(config_file, profiles)
        content = config_file.read_text()
        assert "auto_source_mode" not in content

    def test_auto_generated_profiles_omit_paper_size(self, tmp_path: Path) -> None:
        """Auto-generated profiles do not write paper_size to TOML (default omitted)."""
        caps = DeviceCapabilities(
            sources=["Flatbed", "ADF"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        config_file = tmp_path / "auto_config.toml"
        write_profiles_to_config(config_file, profiles)
        content = config_file.read_text()
        assert "paper_size" not in content

    def test_creates_new_file(self, tmp_path: Path) -> None:
        """Creates config file if it does not exist."""
        config_file = tmp_path / "new_config.toml"

        profiles = {
            "default": ProfileConfig(
                source="Flatbed",
                resolution=300,
                mode="Color",
                auto_generated=True,
            ),
        }
        written = write_profiles_to_config(config_file, profiles)

        assert "default" in written
        assert config_file.exists()
        content = config_file.read_text()
        assert 'source = "Flatbed"' in content
