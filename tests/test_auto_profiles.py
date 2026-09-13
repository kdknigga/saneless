"""Tests for automatic scanner profile generation."""

from __future__ import annotations

import logging
import re
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
from saneless.config import DEFAULT_RESOLUTION, ProfileConfig, Settings
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
        assert "default" not in profiles
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

    def test_feeder_only_device_has_no_default_profile(self) -> None:
        """A device reporting no flatbed source gets no default profile."""
        caps = DeviceCapabilities(
            sources=["Automatic Document Feeder"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert "default" not in profiles


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
        assert set(profiles) == {"adf-front", "adf-front-2"}
        assert profiles["adf-front"].source == "ADF-Front"
        assert profiles["adf-front-2"].source == "ADF Front"

    def test_no_source_is_silently_lost(self) -> None:
        """
        N distinct source strings yield N profiles.

        The assignment was unguarded, so the second collider overwrote the
        first and the device lost a source -- which is N-09's actual complaint.
        """
        profiles = generate_profiles(self._caps())
        assert len(profiles) == len(self._COLLIDING)

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
