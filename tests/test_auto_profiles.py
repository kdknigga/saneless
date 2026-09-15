"""Tests for automatic scanner profile generation."""

from __future__ import annotations

import logging
import re
import tomllib
from typing import TYPE_CHECKING

import pytest

from saneless import auto_profiles
from saneless.auto_profiles import (
    ProfileWriteResult,
    generate_profiles,
    is_bare_default,
    pick_closest_resolution,
    pick_preferred_mode,
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

    def test_manual_duplex_default_not_bare(self) -> None:
        """
        A hand-written manual-duplex default is not bare (WR-04).

        Comparing only source, resolution and mode treated it as untouched, so
        the first web job's auto-generation replaced it in memory with a
        generated flatbed profile: the operator got one flatbed snapshot and a
        green DONE instead of a flip prompt.
        """
        settings = Settings(profiles={"default": ProfileConfig(duplex="manual")})
        assert is_bare_default(settings) is False

    def test_feeder_manual_duplex_default_not_bare(self) -> None:
        """A manual-duplex default naming a feeder is not bare either."""
        settings = Settings(
            profiles={"default": ProfileConfig(source="ADF", duplex="manual")},
        )
        assert is_bare_default(settings) is False

    def test_default_customised_in_another_field_not_bare(self) -> None:
        """Any customised field, not a hand-picked subset, makes it not bare."""
        settings = Settings(profiles={"default": ProfileConfig(paper_size="a4")})
        assert is_bare_default(settings) is False

    def test_default_values_spelled_out_in_toml_are_bare(self, tmp_path: Path) -> None:
        """Writing the default values explicitly still counts as untouched."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[profiles.default]\n"
            'source = "Flatbed"\n'
            f"resolution = {DEFAULT_RESOLUTION}\n"
            'mode = "color"\n'
        )

        settings = load_settings(str(config_file))

        assert is_bare_default(settings) is True

    @pytest.mark.parametrize(
        ("toml_text", "env", "expected"),
        [
            pytest.param(
                '[scanner]\nhost = "scanner.local"\n',
                {},
                True,
                id="toml-without-profiles-section",
            ),
            pytest.param(
                "[profiles.default]\n",
                {},
                True,
                id="empty-default-table",
            ),
            pytest.param(None, {}, True, id="env-only-no-file"),
            pytest.param(
                None,
                {"SANELESS_PROFILES__DEFAULT__RESOLUTION": str(DEFAULT_RESOLUTION)},
                True,
                id="env-var-set-to-default-value",
            ),
            pytest.param(
                '[profiles.default]\ntitle = ""\n',
                {},
                True,
                id="title-alias-default-value",
            ),
            pytest.param(
                '[profiles.default]\nsource = "Manual Duplex"\n',
                {},
                False,
                id="legacy-manual-duplex-source",
            ),
        ],
    )
    def test_untouched_default_shapes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        toml_text: str | None,
        env: dict[str, str],
        *,
        expected: bool,
    ) -> None:
        """
        Every untouched shape of the default profile is bare (ROBU-07, WR-04).

        Each shape goes through the real ``load_settings`` path, so the profile
        is whatever TOML parsing, the nested-env merge and the ``title`` alias
        actually produce. The legacy ``source = "Manual Duplex"`` default is
        translated to ``duplex = "manual"`` at load, so it is customised and
        must not be replaced by generated profiles.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        for name, value in env.items():
            monkeypatch.setenv(name, value)

        if toml_text is None:
            settings = load_settings()
            assert settings.config_path is None
        else:
            config_file = tmp_path / "shape.toml"
            config_file.write_text(toml_text)
            settings = load_settings(str(config_file))

        assert is_bare_default(settings) is expected


class TestNoRederivedConfigPath:
    """Generated profiles are written only to the loaded config file (D-16)."""

    def test_the_rederived_write_path_is_gone(self) -> None:
        """
        M-04: the re-derived write path no longer exists.

        It ignored ``--config`` and could drop ``./saneless.toml`` into the
        daemon's working directory; ``Settings.config_path`` replaced it.
        """
        assert not hasattr(auto_profiles, "resolve_config_path")


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

    @pytest.mark.parametrize(
        ("sources", "expected_default_source"),
        [
            pytest.param(["Flatbed"], "Flatbed", id="flatbed-only"),
            pytest.param(
                ["Automatic Document Feeder", "ADF Duplex"],
                "Automatic Document Feeder",
                id="feeder-only",
            ),
            pytest.param(
                ["Flatbed", "Automatic Document Feeder", "ADF Duplex"],
                "Flatbed",
                id="mixed",
            ),
        ],
    )
    def test_generated_config_round_trips_with_a_default_profile(
        self,
        tmp_path: Path,
        sources: list[str],
        expected_default_source: str,
    ) -> None:
        """
        The written file loads back, which is the guarantee that matters.

        "default is present" is a proxy; a config saneless can actually load
        after auto-profiles has run is the user-visible promise. The generated
        set is therefore written to disk and read back through load_settings,
        whose ``validate_default_profile`` refuses any config without the key,
        for each of the three device shapes a scanner can have (DPLX-07).
        """
        caps = DeviceCapabilities(
            sources=sources,
            resolutions=[300],
            modes=["Color"],
        )
        config_file = tmp_path / "config.toml"
        write_profiles_to_config(config_file, generate_profiles(caps))

        settings = load_settings(str(config_file))
        assert settings.profiles["default"].source == expected_default_source


class TestGenerateProfilesDuplex:
    """Generated profiles carry duplex from the source classifier (D-06)."""

    @staticmethod
    def _written_profiles(config_file: Path) -> dict[str, dict[str, object]]:
        """Parse the written config file and return its profile tables."""
        data = tomllib.loads(config_file.read_text())
        return data["profiles"]

    def test_hardware_duplex_source_is_generated_with_duplex_hardware(
        self, tmp_path: Path
    ) -> None:
        """
        A FEEDER_DUPLEX source yields duplex = "hardware", in memory and on disk.

        D-06 is a statement about what is written to disk, so the file is
        asserted as well as the model: an in-memory assertion alone would not
        catch a writer that drops the key.
        """
        caps = DeviceCapabilities(
            sources=["Flatbed", "ADF Duplex"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert profiles["adf-duplex"].duplex == "hardware"

        config_file = tmp_path / "config.toml"
        write_profiles_to_config(config_file, profiles)
        assert self._written_profiles(config_file)["adf-duplex"]["duplex"] == (
            "hardware"
        )

    def test_default_backed_by_a_duplex_feeder_mirrors_its_source(self) -> None:
        """
        The default profile's duplex agrees with the source it was copied from.

        On a device whose first reported source is a duplex feeder, the default
        is that feeder; it must not claim a different duplex strategy from the
        profile it duplicates.
        """
        caps = DeviceCapabilities(
            sources=["ADF Duplex", "ADF"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert profiles["default"].source == "ADF Duplex"
        assert profiles["default"].duplex == "hardware"

    @pytest.mark.parametrize(
        "source",
        ["Flatbed", "ADF", "Automatic Document Feeder", "Auto"],
    )
    def test_other_source_kinds_are_generated_with_duplex_none(
        self, source: str
    ) -> None:
        """Flatbed, plain feeder and Auto sources all carry the default "none"."""
        caps = DeviceCapabilities(
            sources=[source],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert {profile.duplex for profile in profiles.values()} == {"none"}

    def test_non_duplex_profiles_are_written_without_a_duplex_key(
        self, tmp_path: Path
    ) -> None:
        """
        A duplex of "none" is never written, keeping the config clean.

        Follows the auto_source_mode precedent: only non-default values reach
        the file. The mixed device proves the omission is per profile, not a
        blanket rule -- its duplex feeder still gets the key.
        """
        caps = DeviceCapabilities(
            sources=["Flatbed", "Automatic Document Feeder", "Auto", "ADF Duplex"],
            resolutions=[300],
            modes=["Color"],
        )
        config_file = tmp_path / "config.toml"
        write_profiles_to_config(config_file, generate_profiles(caps))

        written = self._written_profiles(config_file)
        for name in ("flatbed", "automatic-document-feeder", "auto", "default"):
            assert "duplex" not in written[name], name
        assert "duplex" in written["adf-duplex"]

    def test_flatbed_only_config_file_has_no_duplex_key_anywhere(
        self, tmp_path: Path
    ) -> None:
        """The text written for a flatbed-only device never mentions duplex."""
        caps = DeviceCapabilities(
            sources=["Flatbed"],
            resolutions=[300],
            modes=["Color"],
        )
        config_file = tmp_path / "config.toml"
        write_profiles_to_config(config_file, generate_profiles(caps))

        assert "duplex" not in config_file.read_text()

    @pytest.mark.parametrize(
        "sources",
        [
            pytest.param(["Flatbed"], id="flatbed-only"),
            pytest.param(["ADF", "ADF Duplex"], id="feeder-only"),
            pytest.param(["Flatbed", "ADF", "ADF Duplex", "Auto"], id="mixed"),
            pytest.param(["Manual Duplex", "ADF Manual Duplex"], id="legacy-names"),
        ],
    )
    def test_auto_profiles_never_emits_manual_duplex(
        self, tmp_path: Path, sources: list[str]
    ) -> None:
        """
        No generated profile is ever manual duplex, before or after a reload.

        Manual duplex is not a device source at all -- those profiles are always
        hand-written -- so auto-profiles has no evidence for it. Pinning this
        stops a later change from inventing a heuristic.

        The legacy-names case is the sharp edge: a source name containing both
        "manual" and "duplex" is read as ``duplex = "manual"`` by the config
        loader when no duplex is given. A generated profile must therefore
        state its duplex explicitly, so that neither construction nor the
        reload of the written file translates it.
        """
        caps = DeviceCapabilities(
            sources=sources,
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert all(profile.duplex != "manual" for profile in profiles.values())

        config_file = tmp_path / "config.toml"
        write_profiles_to_config(config_file, profiles)
        settings = load_settings(str(config_file))
        assert all(profile.duplex != "manual" for profile in settings.profiles.values())


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

    def test_result_names_added_and_removed_profiles_apart(
        self, tmp_path: Path
    ) -> None:
        """The result reports what was added and what was pruned, separately."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(self._EXISTING)
        result = write_profiles_to_config(config_file, self._generated())
        assert set(result.added) == {"adf", "flatbed"}
        assert result.removed == ("legacy-feeder",)


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

    def test_default_is_not_reported_removed(self, tmp_path: Path) -> None:
        """Only the ordinary orphan is named under Removed, never ``default``."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(self._PREVIOUS_RUN)
        result = write_profiles_to_config(config_file, self._feeder_only())
        assert result.removed == ("flatbed",)

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
        result = write_profiles_to_config(config_file, profiles)

        content = config_file.read_text()
        assert "# SANE network host" in content
        assert result.added == ("default",)

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
        result = write_profiles_to_config(config_file, profiles)

        assert result.added == ()
        assert result.refreshed == ()
        assert result.skipped_not_generated == ("default",)
        content = config_file.read_text()
        assert 'source = "ADF"' in content

    def test_overwrite_with_force(self, tmp_path: Path) -> None:
        """
        Force does not overwrite a profile saneless did not generate (D-01).

        ``auto_generated = false`` marks a hand-written profile, and there is
        no flag that hands it to the tool: it is skipped, reported, and left
        byte for byte as the user wrote it.
        """
        config_file = tmp_path / "config.toml"
        original = (
            b'[profiles.default]\nsource = "ADF"\nresolution = 600\n'
            b'mode = "Gray"\nauto_generated = false\n'
        )
        config_file.write_bytes(original)

        profiles = {
            "default": ProfileConfig(
                source="Flatbed",
                resolution=300,
                mode="Color",
                auto_generated=True,
            ),
        }
        result = write_profiles_to_config(config_file, profiles, force=True)

        assert result.skipped_not_generated == ("default",)
        assert result.refreshed == ()
        assert config_file.read_bytes() == original

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
        result = write_profiles_to_config(config_file, profiles)

        assert result.added == ("default",)
        assert config_file.exists()
        content = config_file.read_text()
        assert 'source = "Flatbed"' in content


class TestForceMerge:
    """
    ``--force`` merges generated keys instead of replacing profiles (M-09).

    D-01: a profile without a truthy ``auto_generated`` is never touched, under
    force too, and is reported. D-02: in a flagged profile the owned keys are
    written onto the existing table, so every other key and comment survives
    and a hand edit to an owned key is overwritten. D-03: an owned key the fresh
    generation does not write is deleted. D-04: the outcome comes back grouped.
    """

    _DEFAULT_BLOCK = """\
[profiles.default]
# hand written: never regenerate this one
source = "Flatbed"  # the one I actually use
resolution = 600
mode = "Gray"

"""

    _EXISTING = (
        "# saneless configuration -- keep this comment\n"
        + _DEFAULT_BLOCK
        + """\
[profiles.scan]
# generated, then customised by hand
source = "Old Source"
resolution = 600  # hand edit
mode = "Gray"
duplex = "hardware"
default_tags = [3, 7]
title = "Scanned"
auto_generated = true

[profiles.stale]
source = "Gone"
resolution = 300
mode = "Color"
auto_generated = true
"""
    )

    def _generated(self, *, extra: bool = False) -> dict[str, ProfileConfig]:
        """Build the regenerated set: ``scan`` and ``default``, plus ``fresh``."""
        profiles = {
            "scan": ProfileConfig(
                source="ADF", resolution=300, mode="Color", auto_generated=True
            ),
            "default": ProfileConfig(
                source="Flatbed", resolution=300, mode="Color", auto_generated=True
            ),
        }
        if extra:
            profiles["fresh"] = ProfileConfig(
                source="ADF Duplex",
                resolution=300,
                mode="Color",
                auto_source_mode="adf",
                duplex="hardware",
                auto_generated=True,
            )
        return profiles

    def _write(
        self, tmp_path: Path, *, force: bool, extra: bool = False
    ) -> tuple[Path, ProfileWriteResult]:
        """Write the regenerated set over the fixture and return path and result."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(self._EXISTING)
        result = write_profiles_to_config(
            config_file, self._generated(extra=extra), force=force
        )
        return config_file, result

    def test_force_refreshes_owned_keys_in_place(self, tmp_path: Path) -> None:
        """D-02: owned keys take the generated values, a hand edit included."""
        config_file, _ = self._write(tmp_path, force=True)
        scan = tomllib.loads(config_file.read_text())["profiles"]["scan"]
        assert scan["source"] == "ADF"
        assert scan["resolution"] == 300
        assert scan["mode"] == "Color"
        assert scan["auto_generated"] is True

    def test_force_deletes_a_stale_owned_key(self, tmp_path: Path) -> None:
        """D-03: ``duplex`` is not generated for this source, so it goes."""
        config_file, _ = self._write(tmp_path, force=True)
        scan = tomllib.loads(config_file.read_text())["profiles"]["scan"]
        assert "duplex" not in scan

    def test_force_merge_keeps_unowned_keys_and_comments(self, tmp_path: Path) -> None:
        """D-02: ``default_tags``, ``title`` and the table comment survive."""
        config_file, _ = self._write(tmp_path, force=True)
        text = config_file.read_text()
        scan = tomllib.loads(text)["profiles"]["scan"]
        assert scan["default_tags"] == [3, 7]
        assert scan["title"] == "Scanned"
        assert "# generated, then customised by hand" in text
        assert "# saneless configuration -- keep this comment" in text

    @pytest.mark.parametrize("force", [False, True])
    def test_force_never_touches_an_unflagged_profile(
        self, tmp_path: Path, *, force: bool
    ) -> None:
        """D-01: the hand-written ``default`` block is byte-for-byte unchanged."""
        config_file, _ = self._write(tmp_path, force=force)
        assert self._DEFAULT_BLOCK.encode() in config_file.read_bytes()

    def test_force_merge_result_groups(self, tmp_path: Path) -> None:
        """D-04: refreshed, skipped and removed names are reported apart."""
        _, result = self._write(tmp_path, force=True)
        assert result.added == ()
        assert result.refreshed == ("scan",)
        assert result.skipped_not_generated == ("default",)
        assert result.skipped_existing == ()
        assert result.removed == ("stale",)

    def test_without_force_merge_leaves_a_flagged_profile_alone(
        self, tmp_path: Path
    ) -> None:
        """Without force a flagged profile is skipped as already existing."""
        config_file, result = self._write(tmp_path, force=False)
        scan = tomllib.loads(config_file.read_text())["profiles"]["scan"]
        assert scan["resolution"] == 600
        assert scan["duplex"] == "hardware"
        assert result.skipped_existing == ("scan",)
        assert "default" in result.skipped_not_generated
        assert result.refreshed == ()

    @pytest.mark.parametrize("force", [False, True])
    def test_merge_adds_a_missing_name_in_generated_key_order(
        self, tmp_path: Path, *, force: bool
    ) -> None:
        """A name the file lacks is added with today's key order and flag."""
        config_file, result = self._write(tmp_path, force=force, extra=True)
        assert result.added == ("fresh",)
        assert (
            "[profiles.fresh]\n"
            'source = "ADF Duplex"\n'
            "resolution = 300\n"
            'mode = "Color"\n'
            'auto_source_mode = "adf"\n'
            'duplex = "hardware"\n'
            "auto_generated = true\n"
        ) in config_file.read_text()

    def test_merge_persisted_is_added_plus_refreshed(self, tmp_path: Path) -> None:
        """``persisted`` names exactly the tables that now match the generation."""
        _, result = self._write(tmp_path, force=True, extra=True)
        assert result.persisted == frozenset({"fresh", "scan"})

    def test_merge_result_describe_lists_groups_in_order(self, tmp_path: Path) -> None:
        """One line per non-empty group, in a fixed order, names shown with repr."""
        result = ProfileWriteResult(
            path=tmp_path / "config.toml",
            added=("a", "b"),
            refreshed=("c",),
            skipped_not_generated=("default",),
            skipped_existing=("e",),
            removed=("f",),
        )
        assert result.describe() == [
            "Added: 'a', 'b'",
            "Refreshed: 'c'",
            "Skipped (not auto-generated): 'default' -- not created by "
            "auto-profiles (no auto_generated = true); rename or delete it to "
            "regenerate",
            "Skipped (already exists; use --force to refresh): 'e'",
            "Removed (scanner no longer offers it): 'f'",
        ]

    def test_merge_result_describe_omits_empty_groups(self, tmp_path: Path) -> None:
        """Empty groups print nothing; an empty result describes nothing."""
        path = tmp_path / "config.toml"
        assert ProfileWriteResult(path=path, refreshed=("x",)).describe() == [
            "Refreshed: 'x'"
        ]
        assert ProfileWriteResult(path=path).describe() == []


class TestOwnershipReadsTheFlagLikeTheLoader:
    """
    The writer decides ownership the way ``load_settings`` parses the flag.

    CR-01: pydantic's lax ``bool`` reads ``"false"``, ``"no"``, ``"off"`` and
    ``"0"`` as False, so the loader calls such a profile hand-written. Plain
    truthiness called the same non-empty string True, and the tool refreshed
    or pruned a profile it did not own (D-01).
    """

    _FALSY_STRINGS = ("false", "no", "off", "0", "f", "n")

    @staticmethod
    def _config(tmp_path: Path, name: str, flag: str) -> Path:
        """Write a hand-written ``name`` profile whose flag is the string ``flag``."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[profiles.default]\nsource = "Flatbed"\n\n'
            f'[profiles.{name}]\nsource = "mine"\nauto_generated = "{flag}"\n'
        )
        return config_file

    @pytest.mark.parametrize("flag", _FALSY_STRINGS)
    def test_loader_and_writer_agree_the_profile_is_hand_written(
        self, tmp_path: Path, flag: str
    ) -> None:
        """The loader's reading is the one the writer must follow."""
        config_file = self._config(tmp_path, "adf", flag)
        settings = load_settings(str(config_file))
        assert settings.profiles["adf"].auto_generated is False

    @pytest.mark.parametrize("flag", _FALSY_STRINGS)
    def test_force_does_not_refresh_a_string_false_profile(
        self, tmp_path: Path, flag: str
    ) -> None:
        """A same-name profile flagged ``"false"`` is skipped, byte for byte."""
        config_file = self._config(tmp_path, "adf", flag)
        before = config_file.read_bytes()
        generated = {
            "adf": ProfileConfig(
                source="ADF", resolution=300, mode="Color", auto_generated=True
            ),
            "default": ProfileConfig(
                source="Flatbed", resolution=300, mode="Color", auto_generated=True
            ),
        }

        result = write_profiles_to_config(config_file, generated, force=True)

        assert result.refreshed == ()
        assert "adf" in result.skipped_not_generated
        assert config_file.read_bytes() == before

    @pytest.mark.parametrize("force", [False, True])
    @pytest.mark.parametrize("flag", _FALSY_STRINGS)
    def test_prune_does_not_remove_a_string_false_profile(
        self, tmp_path: Path, flag: str, *, force: bool
    ) -> None:
        """An orphan flagged ``"no"`` is not the tool's, so it is not removed."""
        config_file = self._config(tmp_path, "zzz", flag)
        generated = {
            "default": ProfileConfig(
                source="Flatbed", resolution=300, mode="Color", auto_generated=True
            ),
        }

        result = write_profiles_to_config(config_file, generated, force=force)

        assert result.removed == ()
        profiles = tomllib.loads(config_file.read_text())["profiles"]
        assert profiles["zzz"] == {"source": "mine", "auto_generated": flag}

    def test_a_string_true_flag_is_still_owned(self, tmp_path: Path) -> None:
        """``"yes"`` loads as True, so the writer owns and prunes it too."""
        config_file = self._config(tmp_path, "zzz", "yes")
        generated = {
            "default": ProfileConfig(
                source="Flatbed", resolution=300, mode="Color", auto_generated=True
            ),
        }

        result = write_profiles_to_config(config_file, generated)

        assert result.removed == ("zzz",)

    def test_an_unparseable_flag_is_not_owned(self, tmp_path: Path) -> None:
        """A flag pydantic rejects is never read as the tool's (fail safe)."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[profiles.default]\nsource = "Flatbed"\n\n'
            '[profiles.zzz]\nsource = "mine"\nauto_generated = "maybe"\n'
        )
        generated = {
            "default": ProfileConfig(
                source="Flatbed", resolution=300, mode="Color", auto_generated=True
            ),
        }

        result = write_profiles_to_config(config_file, generated)

        assert result.removed == ()
        assert "zzz" in tomllib.loads(config_file.read_text())["profiles"]


class TestDurableConfigWrite:
    """
    Config rewrites are UTF-8, line-ending preserving, guarded and atomic.

    CFG-08 / M-10: the old write truncated the file in place in the locale
    encoding and translated CRLF to LF. D-05: bytes are decoded as UTF-8 and
    the dumped text is re-parsed before ``replace_file_atomically`` swaps it
    in. D-07: a symlinked config is written through and both paths are logged.
    """

    @staticmethod
    def _generated() -> dict[str, ProfileConfig]:
        """Build a one-profile generated set that the fixtures do not name."""
        return {
            "flatbed": ProfileConfig(
                source="Flatbed", resolution=300, mode="Color", auto_generated=True
            )
        }

    def test_crlf_utf8_config_keeps_crlf_and_comment(self, tmp_path: Path) -> None:
        """Every line ending stays CRLF, lines tomlkit adds included (D-05)."""
        config_file = tmp_path / "config.toml"
        config_file.write_bytes(
            '# café\r\n[profiles.default]\r\nsource = "Flatbed"\r\n'.encode()
        )

        write_profiles_to_config(config_file, self._generated())

        data = config_file.read_bytes()
        assert "# café\r\n".encode() in data
        assert re.search(rb"(?<!\r)\n", data) is None
        profiles = tomllib.loads(data.decode("utf-8"))["profiles"]
        assert set(profiles) == {"default", "flatbed"}

    def test_lf_utf8_config_stays_lf(self, tmp_path: Path) -> None:
        """An LF file gains no carriage returns."""
        config_file = tmp_path / "config.toml"
        config_file.write_bytes(b'[profiles.default]\nsource = "Flatbed"\n')

        write_profiles_to_config(config_file, self._generated())

        assert b"\r" not in config_file.read_bytes()

    def test_non_utf8_config_is_refused_utf8(self, tmp_path: Path) -> None:
        """Bytes that are not UTF-8 raise ConfigError and leave the file alone."""
        config_file = tmp_path / "config.toml"
        original = b"# caf\xe9\n"
        config_file.write_bytes(original)

        with pytest.raises(ConfigError, match="UTF-8") as caught:
            write_profiles_to_config(config_file, self._generated())

        assert str(config_file) in str(caught.value)
        assert config_file.read_bytes() == original

    def test_inline_profiles_section_stays_valid_toml(self, tmp_path: Path) -> None:
        """A profile added to an inline ``profiles = {...}`` section re-parses."""
        config_file = tmp_path / "config.toml"
        config_file.write_text('profiles = { default = { source = "Flatbed" } }\n')

        result = write_profiles_to_config(config_file, self._generated())

        profiles = tomllib.loads(config_file.read_text())["profiles"]
        assert profiles["default"] == {"source": "Flatbed"}
        assert profiles["flatbed"]["resolution"] == 300
        assert result.added == ("flatbed",)

    def test_guard_refuses_output_that_does_not_parse_inline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid dumped TOML raises ConfigError; file and directory unchanged."""
        config_file = tmp_path / "config.toml"
        original = b'[profiles.default]\nsource = "Flatbed"\n'
        config_file.write_bytes(original)
        monkeypatch.setattr(auto_profiles.tomlkit, "dumps", lambda _doc: "profiles = {")

        with pytest.raises(ConfigError, match="refus") as caught:
            write_profiles_to_config(config_file, self._generated())

        assert str(config_file) in str(caught.value)
        assert config_file.read_bytes() == original
        assert list(tmp_path.iterdir()) == [config_file]

    @pytest.mark.parametrize("force", [False, True])
    def test_guard_refuses_dotted_keys_that_change_meaning(
        self, tmp_path: Path, *, force: bool
    ) -> None:
        """
        Output that parses but means something else is refused (CR-02).

        With top-level dotted profile keys, tomlkit moves the second dotted
        line under the table it adds, so the dumped text is valid TOML that
        nests ``profiles.default.auto_generated`` inside the new profile. The
        file must be left as it was, still loadable, with no temp file behind.
        """
        config_file = tmp_path / "config.toml"
        original = (
            b'profiles.default.source = "Flatbed"\n'
            b"profiles.default.auto_generated = true\n"
        )
        config_file.write_bytes(original)
        generated = {
            "default": ProfileConfig(
                source="Flatbed", resolution=300, mode="Color", auto_generated=True
            ),
            "adf": ProfileConfig(
                source="ADF", resolution=300, mode="Color", auto_generated=True
            ),
        }

        with pytest.raises(ConfigError, match="refusing") as caught:
            write_profiles_to_config(config_file, generated, force=force)

        assert str(config_file) in str(caught.value)
        assert config_file.read_bytes() == original
        assert list(tmp_path.iterdir()) == [config_file]
        assert load_settings(str(config_file)).profiles["default"].source == "Flatbed"

    def test_guard_accepts_a_crlf_multiline_string_and_nan(
        self, tmp_path: Path
    ) -> None:
        """
        Parser differences that do not change meaning are not refused.

        tomllib reads CRLF inside a multi-line string as LF while tomlkit keeps
        it, and NaN is unequal to itself; neither may block every rewrite.
        """
        config_file = tmp_path / "config.toml"
        config_file.write_bytes(
            b'[profiles.default]\r\nsource = "Flatbed"\r\n'
            b'title = """two\r\nlines"""\r\nnote = nan\r\n'
        )

        result = write_profiles_to_config(config_file, self._generated())

        assert result.added == ("flatbed",)
        profiles = tomllib.loads(config_file.read_bytes().decode("utf-8"))["profiles"]
        assert profiles["default"]["title"] == "two\nlines"

    def test_guard_refuses_output_that_parses_to_a_different_document(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valid TOML whose data is not the merged document is never written."""
        config_file = tmp_path / "config.toml"
        original = b'[profiles.default]\nsource = "Flatbed"\n'
        config_file.write_bytes(original)
        monkeypatch.setattr(
            auto_profiles.tomlkit,
            "dumps",
            lambda _doc: '[profiles.default]\nsource = "Other"\n',
        )

        with pytest.raises(ConfigError, match="refusing"):
            write_profiles_to_config(config_file, self._generated())

        assert config_file.read_bytes() == original

    def test_guard_skips_the_replace_when_nothing_changed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only skipped names means no rewrite, so no EBUSY noise for a no-op."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[profiles.flatbed]\nsource = "Flatbed"\nauto_generated = true\n'
        )
        calls: list[object] = []

        def recorder(path: Path, text: str) -> Path:
            """Record the call; a no-op merge must never reach here."""
            calls.append((path, text))
            return path

        monkeypatch.setattr(auto_profiles, "replace_file_atomically", recorder)

        result = write_profiles_to_config(config_file, self._generated())

        assert result.skipped_existing == ("flatbed",)
        assert calls == []

    def test_symlink_config_is_written_through_and_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D-07: the link survives, the real file changes, both are named."""
        real = tmp_path / "dotfiles" / "config.toml"
        real.parent.mkdir()
        real.write_text('[profiles.default]\nsource = "Flatbed"\n')
        link = tmp_path / "config.toml"
        link.symlink_to(real)

        with caplog.at_level(logging.INFO, logger="saneless.auto_profiles"):
            result = write_profiles_to_config(link, self._generated())

        assert link.is_symlink()
        assert "flatbed" in tomllib.loads(real.read_text())["profiles"]
        assert result.path == real.resolve()
        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "saneless.auto_profiles"
            and str(link) in record.getMessage()
            and str(real.resolve()) in record.getMessage()
        ]
        assert len(messages) == 1
