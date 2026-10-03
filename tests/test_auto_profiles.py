"""Tests for automatic scanner profile generation."""

from __future__ import annotations

import logging
import re
import tomllib
from pathlib import Path
from typing import ClassVar, Literal

import pytest
from tomlkit.exceptions import ParseError, TOMLKitError

from saneless import auto_profiles
from saneless.auto_profiles import (
    ProfileWriteResult,
    _profile_description,
    _profile_label,
    _snap_into_range,
    device_type_of,
    generate_profiles,
    is_bare_default,
    pick_closest_resolution,
    pick_preferred_mode,
    source_to_slug,
    write_profiles_to_config,
)
from saneless.config import (
    DEFAULT_RESOLUTION,
    PROFILE_DESCRIPTION_MAX_LENGTH,
    PROFILE_LABEL_MAX_LENGTH,
    ProfileConfig,
    Settings,
    load_settings,
)
from saneless.exceptions import ConfigError
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    SourceKind,
    classify_source,
)

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
        """Returns the resolution nearest DEFAULT_RESOLUTION when it is absent."""
        # 150 is 150 away from 300 and 600 is 300 away, so 150 is the answer.
        result = pick_closest_resolution([150, 600], target=DEFAULT_RESOLUTION)
        assert result == 150

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

        # The range alone would have offered 300 exactly; the list's nearest is 150.
        assert result == 150

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


class TestSnapIntoRange:
    """
    The resolution snap only ever offers a value on the device's step grid.

    A range says the device accepts its minimum plus whole multiples of its
    step, up to its maximum. The maximum itself need not be on that grid, so
    clamping to it can produce a value the device never offered. Ties go up, so
    an equidistant pick is never below the target, and the coercion to whole
    dpi rounds half up rather than to the nearest even number.
    """

    @pytest.mark.parametrize(
        ("target", "resolution_range", "expected"),
        [
            (300, (50.0, 200.0, 100.0), 150),
            (300, (100.0, 250.0, 100.0), 200),
            (300, (75.0, 1200.0, 150.0), 375),
            (300, (50.0, 1200.0, 100.0), 350),
            (300, (100.0, 1200.0, 0.0), 300),
            (5000, (100.0, 1200.0, 0.0), 1200),
            (10, (100.0, 1200.0, 0.0), 100),
            (300, (100.0, 1200.0, 0.5), 300),
            (100, (150.5, 600.0, 0.0), 151),
        ],
        ids=[
            "off-grid-maximum-snaps-down-to-the-grid",
            "off-grid-maximum-with-an-aligned-minimum",
            "tie-goes-up-on-an-odd-step-count",
            "tie-goes-up-on-an-even-step-count",
            "continuous-range-keeps-the-target",
            "continuous-range-clamps-to-the-maximum",
            "continuous-range-clamps-to-the-minimum",
            "fractional-step-keeps-an-on-grid-target",
            "whole-dpi-coercion-rounds-half-up",
        ],
    )
    def test_snap_stays_on_grid(
        self,
        target: int,
        resolution_range: tuple[float, float, float],
        expected: int,
    ) -> None:
        """Each case lands in the range, on the grid, with ties going up."""
        result = _snap_into_range(target, resolution_range)

        assert result == expected
        low, high, step = resolution_range
        assert low <= result <= high
        if step > 0:
            assert (result - low) % step == 0


class TestPickPreferredMode:
    """
    Scan modes are ranked by what they mean, not by an exact spelling.

    SANE backends do not agree on how to spell colour: brother4 offers
    ``24bit Color``, some drivers say ``Colour``, and the standard names include
    ``Color Lineart``. Their first entry is usually black-and-white, so falling
    back to it whenever the exact word ``Color`` is missing hands a colour
    scanner a black-and-white profile.
    """

    @pytest.mark.parametrize(
        ("modes", "expected"),
        [
            (
                [
                    "Black & White",
                    "Gray[Error Diffusion]",
                    "True Gray",
                    "24bit Color",
                    "24bit Color[Fast]",
                ],
                "24bit Color",
            ),
            (
                [
                    "Black & White",
                    "Gray[Error Diffusion]",
                    "True Gray",
                    "24bit Color[Fast]",
                ],
                "24bit Color[Fast]",
            ),
            (["Lineart", "Gray", "Color"], "Color"),
            (["Lineart", "Halftone", "Gray", "Color"], "Color"),
            (
                [
                    "Color",
                    "Gray",
                    "Negative color",
                    "Negative gray",
                    "Infrared",
                    "48 bits color",
                    "16 bits gray",
                    "Lineart",
                ],
                "Color",
            ),
            (["Color Lineart", "Color Halftone", "Colour"], "Colour"),
            (["Negative color", "Color Lineart", "24bit Color"], "24bit Color"),
            (["Color Lineart", "Gray"], "Color Lineart"),
            (["Lineart", "Gray"], "Gray"),
            (["Black & White", "Gray[Error Diffusion]", "True Gray"], "True Gray"),
            (["Grey", "Lineart"], "Grey"),
            (["Black & White"], "Black & White"),
        ],
        ids=[
            "brother4-ads",
            "brother4-mfc",
            "epson2-and-escl",
            "fujitsu",
            "pixma",
            "british-spelling-is-an-exact-colour-name",
            "degraded-colour-modes-lose-the-tie-break",
            "any-colour-mode-beats-gray",
            "no-colour-falls-back-to-gray",
            "brother4-no-colour-skips-the-dithered-gray",
            "british-grey",
            "neither-colour-nor-gray-takes-the-first-entry",
        ],
    )
    def test_preferred_mode_by_meaning(self, modes: list[str], expected: str) -> None:
        """Each backend's list yields the mode a person would have chosen."""
        assert pick_preferred_mode(modes) == expected

    def test_preferred_mode_empty_list_is_color(self) -> None:
        """A device reporting no modes gets the conventional colour name."""
        assert pick_preferred_mode([]) == "Color"


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
        config_file = tmp_path / "saneless.toml"
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


class TestGenerateProfilesPlatenFromDeviceType:
    """
    A declared device type is platen evidence beside the source names.

    Regression cover for the HP LaserJet 3030 on the ``hpaio`` backend: it has
    a platen, hpaio names its sources only ``Auto`` and ``ADF``, and every
    generated profile therefore carried ``auto_source_mode = "adf"``. A scan
    from the glass was routed down ``_scan_adf_pages``, which probed for a page
    2 the glass could not supply; the device answered with a device I/O error
    and a "Memory is low" panel message, and a good one-page scan was reported
    as a scanner fault.
    """

    HP_SOURCES: ClassVar[list[str]] = ["Auto", "ADF"]

    def test_all_in_one_type_routes_auto_to_the_platen(self) -> None:
        """The reported device type supplies the platen the sources do not."""
        caps = DeviceCapabilities(
            sources=self.HP_SOURCES,
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps, "all-in-one")
        assert profiles["auto"].auto_source_mode == "flatbed"

    def test_the_default_profile_agrees_with_the_auto_profile(self) -> None:
        """
        The default is backed by ``Auto`` here, and must route the same way.

        The two are generated by separate calls, so a fix applied to only the
        loop would leave the default -- the profile a fresh install actually
        scans with -- still routing to the feeder.
        """
        caps = DeviceCapabilities(
            sources=self.HP_SOURCES,
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps, "all-in-one")
        assert profiles["default"].source == "Auto"
        assert profiles["default"].auto_source_mode == "flatbed"

    @pytest.mark.parametrize(
        "device_type",
        ["all-in-one", "flatbed scanner", "multi-function peripheral", "ALL-IN-ONE"],
    )
    def test_platen_bearing_types(self, device_type: str) -> None:
        """Every recognised platen type routes Auto to the glass."""
        caps = DeviceCapabilities(
            sources=self.HP_SOURCES,
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps, device_type)
        assert profiles["auto"].auto_source_mode == "flatbed"

    @pytest.mark.parametrize("device_type", ["sheetfed scanner", "", "   ", "handheld"])
    def test_types_that_are_not_platen_evidence_change_nothing(
        self, device_type: str
    ) -> None:
        """
        A sheet-fed, absent or unrecognised type leaves Auto on the feeder.

        This is the half of the rule that keeps it a widening rather than a
        replacement: absence of evidence is read as absence, never as a denial
        that the device feeds.
        """
        caps = DeviceCapabilities(
            sources=self.HP_SOURCES,
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps, device_type)
        assert profiles["auto"].auto_source_mode == "adf"

    def test_a_named_flatbed_source_still_wins_on_an_unknown_type(self) -> None:
        """The source names remain sufficient on their own."""
        caps = DeviceCapabilities(
            sources=["Auto", "Flatbed"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps, "sheetfed scanner")
        assert profiles["auto"].auto_source_mode == "flatbed"


class TestDeviceTypeOf:
    """Looking a declared device type up out of the enumeration result."""

    DEVICES: ClassVar[list[DeviceInfo]] = [
        DeviceInfo(
            name="hpaio:/usb/hp_LaserJet_3030?serial=00MXBM121742",
            vendor="Hewlett-Packard",
            model="hp_LaserJet_3030",
            device_type="all-in-one",
        ),
        DeviceInfo(
            name="net:host:other:0",
            vendor="Fujitsu",
            model="fi-7160",
            device_type="sheetfed scanner",
        ),
    ]

    def test_finds_the_named_device(self) -> None:
        """The type comes from the matching record, not the first one."""
        assert device_type_of(self.DEVICES, "net:host:other:0") == "sheetfed scanner"

    def test_a_name_that_matches_nothing_is_no_evidence(self) -> None:
        """
        A configured name the enumeration does not return is not an error.

        ``scanner.device`` is operator-supplied and need not match verbatim.
        The empty string is what ``_declares_platen`` reads as "no evidence",
        so a miss falls back to judging the device on its source names.
        """
        assert device_type_of(self.DEVICES, "net:host:absent:0") == ""

    def test_an_empty_enumeration_is_no_evidence(self) -> None:
        """No devices means no type, not an exception."""
        assert device_type_of([], "anything") == ""


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
        config_file = tmp_path / "saneless.toml"
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

        config_file = tmp_path / "saneless.toml"
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
        config_file = tmp_path / "saneless.toml"
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
        config_file = tmp_path / "saneless.toml"
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

        config_file = tmp_path / "saneless.toml"
        write_profiles_to_config(config_file, profiles)
        settings = load_settings(str(config_file))
        assert all(profile.duplex != "manual" for profile in settings.profiles.values())


class TestProfileLabels:
    """
    Generated profiles carry human text derived from the classifier (D-19).

    The label and the description come from ``classify_source`` and nothing
    else: no new probe, no new config key, and no vendor string. Every
    returned value is a developer-authored constant, which is what keeps a
    SANE source name from reaching the scan page through this path (T-30-17).
    """

    # One representative SANE source name per SourceKind. Parametrising over
    # ``list(SourceKind)`` rather than over this mapping's keys is deliberate:
    # a new member added to the enum fails here with a KeyError instead of
    # being silently skipped.
    _SAMPLE: ClassVar[dict[SourceKind, str]] = {
        SourceKind.FLATBED: "Flatbed",
        SourceKind.FEEDER: "ADF Front",
        SourceKind.FEEDER_DUPLEX: "ADF Duplex",
        SourceKind.AUTO: "Auto",
        SourceKind.UNKNOWN: "Mystery Tray",
    }

    @pytest.mark.parametrize("kind", list(SourceKind))
    def test_sample_source_names_classify_as_the_kind_they_stand_for(
        self, kind: SourceKind
    ) -> None:
        """The mapping this class parametrises over is itself correct."""
        assert classify_source(self._SAMPLE[kind]) is kind

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("ADF", "Feeder, single-sided"),
            ("ADF Front", "Feeder, front side only"),
            ("ADF Back", "Feeder, back side only"),
            ("ADF Duplex", "Feeder, double-sided"),
            ("Flatbed", "Glass (flatbed)"),
            # No feeder word, so these classify as UNKNOWN and never reach the
            # front/back wording: the side refinement is not a second router.
            ("Card Front", "Scanner source"),
            ("Card Back", "Scanner source"),
        ],
    )
    def test_label_uses_the_fixed_feeder_duplex_and_glass_wording(
        self, source: str, expected: str
    ) -> None:
        """
        The feeder, duplex and glass strings are agreed, locked copy.

        A feeder that names exactly one side reads as that side, which is a
        wording refinement of the single-sided feeder form.
        """
        assert _profile_label(source) == expected

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (
                "ADF Front",
                "Scans only the front of every page using the document feeder.",
            ),
            (
                "ADF Back",
                "Scans only the back of every page using the document feeder.",
            ),
            ("ADF", "Scans one side of every page using the document feeder."),
        ],
    )
    def test_feeder_side_description_names_the_side(
        self, source: str, expected: str
    ) -> None:
        """A front or back feeder's sentence matches its label."""
        assert _profile_description(source) == expected

    @pytest.mark.parametrize(
        ("auto_source_mode", "expected"),
        [
            (
                "adf",
                "Scans every page in the document feeder; "
                "the scanner decides how it feeds them.",
            ),
            ("flatbed", "Scans one page; the scanner picks the glass or the feeder."),
        ],
    )
    def test_auto_description_follows_its_routing(
        self, auto_source_mode: Literal["adf", "flatbed"], expected: str
    ) -> None:
        """
        An ``Auto`` profile is described by how it routes, not by its kind.

        On a platen-less device ``Auto`` is routed as a whole stack, on one
        with a glass as a single page; one sentence cannot be true of both.
        """
        description = _profile_description("Auto", auto_source_mode=auto_source_mode)
        assert description == expected
        assert len(description) <= PROFILE_DESCRIPTION_MAX_LENGTH

    @pytest.mark.parametrize("source", ["Auto", "Some Unknown Source"])
    def test_label_is_never_empty_for_an_unclassified_source(self, source: str) -> None:
        """An Auto or unrecognised source still gets a name a human can read."""
        assert _profile_label(source).strip()

    @pytest.mark.parametrize("kind", list(SourceKind))
    def test_label_is_non_empty_and_within_the_config_cap(
        self, kind: SourceKind
    ) -> None:
        """Generation can never produce a label its own schema would reject."""
        label = _profile_label(self._SAMPLE[kind])
        assert label.strip()
        assert len(label) <= PROFILE_LABEL_MAX_LENGTH

    @pytest.mark.parametrize("kind", list(SourceKind))
    def test_description_is_a_non_empty_sentence_within_the_config_cap(
        self, kind: SourceKind
    ) -> None:
        """Every kind gets one plain sentence that fits the field's cap."""
        description = _profile_description(self._SAMPLE[kind])
        assert description.strip()
        assert description.endswith(".")
        assert len(description) <= PROFILE_DESCRIPTION_MAX_LENGTH

    def test_every_source_kind_gets_a_distinct_label_and_description(self) -> None:
        """No two kinds share text; the dropdown can tell them apart."""
        kinds = list(SourceKind)
        labels = {_profile_label(self._SAMPLE[kind]) for kind in kinds}
        descriptions = {_profile_description(self._SAMPLE[kind]) for kind in kinds}
        assert len(labels) == len(kinds)
        assert len(descriptions) == len(kinds)

    def test_duplex_feeder_description_names_both_sides_and_the_feeder(self) -> None:
        """The FEEDER_DUPLEX sentence says both sides and says feeder."""
        description = _profile_description("ADF Duplex").lower()
        assert "both sides" in description
        assert "feeder" in description

    # A marker no developer-authored constant would ever contain, appended to
    # a name of each classified shape. Asserting on a marker rather than on
    # the whole source name is what makes the assertion mean what it says:
    # "Automatic" contains the literal source name "Auto" by coincidence,
    # which proves nothing about interpolation.
    _VENDOR_MARKER = "ZzVendorZz<script>alert(1)</script>"

    @pytest.mark.parametrize(
        "shape",
        ["Flatbed", "ADF Front", "ADF Duplex", "Auto", "Mystery Tray"],
    )
    def test_no_returned_string_carries_anything_from_the_source_name(
        self, shape: str
    ) -> None:
        """
        T-30-17: the vendor-controlled source name is never interpolated.

        A source name is device-controlled input that ends up in a file the
        web UI renders. Both functions select a constant instead of building
        a string, so nothing from the device can ride along.
        """
        source = f"{shape} {self._VENDOR_MARKER}"
        assert self._VENDOR_MARKER not in _profile_label(source)
        assert self._VENDOR_MARKER not in _profile_description(source)
        assert "<" not in _profile_label(source)
        assert "<" not in _profile_description(source)

    def test_the_marker_source_names_still_reach_every_branch(self) -> None:
        """
        The marker does not quietly send every name down the UNKNOWN arm.

        Without this the interpolation test above would pass on a single
        branch and claim to have covered five.
        """
        marked = {
            classify_source(f"{shape} {self._VENDOR_MARKER}")
            for shape in ("Flatbed", "ADF Front", "ADF Duplex", "Mystery Tray")
        }
        assert marked == {
            SourceKind.FLATBED,
            SourceKind.FEEDER,
            SourceKind.FEEDER_DUPLEX,
            SourceKind.UNKNOWN,
        }

    def test_generated_profiles_carry_the_derived_label_and_description(self) -> None:
        """``generate_profiles`` sets both fields on every profile it builds."""
        caps = DeviceCapabilities(
            sources=["Flatbed", "ADF Front", "ADF Duplex"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert profiles["flatbed"].label == "Glass (flatbed)"
        assert profiles["adf-front"].label == "Feeder, front side only"
        assert profiles["adf-duplex"].label == "Feeder, double-sided"
        for slug, profile in profiles.items():
            assert profile.label == _profile_label(profile.source), slug
            assert profile.description == _profile_description(profile.source), slug

    def test_the_default_profile_carries_the_text_of_the_source_it_copies(
        self,
    ) -> None:
        """The default duplicates a source, so it must duplicate its wording."""
        caps = DeviceCapabilities(
            sources=["ADF Duplex", "ADF Front"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert profiles["default"].source == "ADF Duplex"
        assert profiles["default"].label == "Feeder, double-sided"
        assert profiles["default"].description == _profile_description("ADF Duplex")


class TestFeederSide:
    """
    ``_feeder_side`` names the one side a single-sided feeder scans.

    It answers a wording question only. ``classify_source`` stays the one rule
    that decides what a source is; this helper is reached only from the
    ``FEEDER`` arm of the label and description functions, so a table over the
    real SANE spellings is what pins it.
    """

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            # fujitsu, canon_dr, kodak, epjitsu, avision, epsonds
            ("ADF Front", "front"),
            ("ADF Back", "back"),
            ("adf back", "back"),
            # escl, hp5590
            ("ADF", None),
            # epson2
            ("Automatic Document Feeder", None),
            # brother4
            ("Automatic Document Feeder(left aligned)", None),
            ("Automatic Document Feeder(centrally aligned)", None),
            # Naming both sides names neither.
            ("ADF Front Back", None),
            # A side word must be a whole word, not part of one.
            ("ADF Backlit", None),
            ("ADF Frontal", None),
        ],
    )
    def test_feeder_side_reads_one_whole_side_word(
        self, source: str, expected: str | None
    ) -> None:
        """Exactly one whole side word names a side; anything else names none."""
        assert auto_profiles._feeder_side(source) == expected

    @pytest.mark.parametrize(
        "source", ["ADF Front", "ADF Back", "ADF", "Automatic Document Feeder"]
    )
    def test_every_side_spelling_is_a_single_sided_feeder(self, source: str) -> None:
        """The table's rows are the sources the ``FEEDER`` arm actually sees."""
        assert classify_source(source) is SourceKind.FEEDER


# Source sets as real SANE backends report them. The two named by the unique
# label requirement come first: a flatbed-plus-feeder device (epsonds) and a
# sheet-fed one (fujitsu, canon_dr, kodak, epjitsu, avision).
_FLATBED_AND_FEEDER = ["Flatbed", "ADF Front", "ADF Duplex"]
_SHEET_FED = ["ADF Front", "ADF Back", "ADF Duplex"]
# brother4: two feeder sources, neither naming a side.
_BROTHER4 = [
    "FlatBed",
    "Automatic Document Feeder(left aligned)",
    "Automatic Document Feeder(centrally aligned)",
]
# epson2: two sources the classifier does not recognise.
_EPSON2 = ["Flatbed", "Automatic Document Feeder", "Transparency Unit", "TPU8x10"]
_TWO_FLATBEDS = ["Flatbed", "Flatbed 2", "ADF"]


def _labels_by_source(profiles: dict[str, ProfileConfig]) -> dict[str, str]:
    """Map each non-default generated profile's source to its label."""
    return {
        profile.source: profile.label
        for name, profile in profiles.items()
        if name != "default"
    }


def _generate(sources: list[str]) -> dict[str, ProfileConfig]:
    """Generate profiles for ``sources`` with a resolution and a mode."""
    return generate_profiles(
        DeviceCapabilities(
            sources=sources, resolutions=[150, 300, 600], modes=["Gray", "Color"]
        )
    )


class TestGeneratedLabelsAreUnique:
    """
    No two generated options share a label, and ``default`` is an exact twin.

    Labels stay developer constants: a second holder of a label gets an
    integer ordinal appended, never anything from the source name.
    """

    @pytest.mark.parametrize(
        "sources",
        [_FLATBED_AND_FEEDER, _SHEET_FED, _BROTHER4, _EPSON2, _TWO_FLATBEDS],
        ids=["flatbed-and-feeder", "sheet-fed", "brother4", "epson2", "two-flatbeds"],
    )
    def test_generated_labels_are_unique_per_source_set(
        self, sources: list[str]
    ) -> None:
        """Every option the dropdown offers can be told apart by its label."""
        labels = list(_labels_by_source(_generate(sources)).values())
        assert len(labels) == len(sources)
        assert len(set(labels)) == len(labels), labels

    def test_brother4_feeders_get_an_ordinal_label_in_source_order(self) -> None:
        """The second unsided feeder reads as the first, plus 2."""
        labels = _labels_by_source(_generate(_BROTHER4))
        assert labels["Automatic Document Feeder(left aligned)"] == (
            "Feeder, single-sided"
        )
        assert labels["Automatic Document Feeder(centrally aligned)"] == (
            "Feeder, single-sided 2"
        )
        assert labels["FlatBed"] == "Glass (flatbed)"

    def test_brother4_feeder_descriptions_stay_the_base_sentence(self) -> None:
        """Only the dropdown text must differ; the sentence is true of both."""
        descriptions = {
            profile.description
            for profile in _generate(_BROTHER4).values()
            if profile.source.startswith("Automatic Document Feeder")
        }
        assert descriptions == {_profile_description("Automatic Document Feeder")}

    @pytest.mark.parametrize(
        ("sources", "expected"),
        [
            (
                _EPSON2,
                {
                    "Transparency Unit": "Scanner source",
                    "TPU8x10": "Scanner source 2",
                },
            ),
            (
                _TWO_FLATBEDS,
                {"Flatbed": "Glass (flatbed)", "Flatbed 2": "Glass (flatbed) 2"},
            ),
        ],
        ids=["epson2-unrecognised", "two-flatbeds"],
    )
    def test_any_repeated_label_gets_an_ordinal(
        self, sources: list[str], expected: dict[str, str]
    ) -> None:
        """Two sources of one kind follow the same rule, whatever the kind."""
        labels = _labels_by_source(_generate(sources))
        for source, label in expected.items():
            assert labels[source] == label

    @pytest.mark.parametrize(
        "sources",
        [_FLATBED_AND_FEEDER, _SHEET_FED, _BROTHER4, _EPSON2, _TWO_FLATBEDS, ["Auto"]],
        ids=[
            "flatbed-and-feeder",
            "sheet-fed",
            "brother4",
            "epson2",
            "two-flatbeds",
            "auto-only",
        ],
    )
    def test_default_is_a_whole_model_twin_of_the_profile_it_copies(
        self, sources: list[str]
    ) -> None:
        """``default`` equals its backing profile as a whole model, text included."""
        profiles = _generate(sources)
        default = profiles["default"]
        twins = [
            name
            for name, profile in profiles.items()
            if name != "default" and profile == default
        ]
        assert len(twins) == 1, twins
        assert profiles[twins[0]].source == default.source

    def test_refresh_keeps_the_ordinal_labels_in_the_file(self, tmp_path: Path) -> None:
        """A write and a forced refresh both keep each feeder's own label."""
        config_file = tmp_path / "saneless.toml"
        config_file.write_text("")
        write_profiles_to_config(config_file, _generate(_BROTHER4))
        write_profiles_to_config(config_file, _generate(_BROTHER4), force=True)

        tables = tomllib.loads(config_file.read_text())["profiles"]
        labels = {
            table["source"]: table["label"]
            for name, table in tables.items()
            if name != "default"
        }
        assert labels["Automatic Document Feeder(left aligned)"] == (
            "Feeder, single-sided"
        )
        assert labels["Automatic Document Feeder(centrally aligned)"] == (
            "Feeder, single-sided 2"
        )


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
        config_file = tmp_path / "saneless.toml"
        config_file.write_text(self._MALFORMED)

        with pytest.raises(ConfigError, match="not a table"):
            write_profiles_to_config(config_file, self._generated())

    def test_the_malformed_file_is_left_untouched(self, tmp_path: Path) -> None:
        """Refusing to overwrite means the user's file is still theirs."""
        config_file = tmp_path / "saneless.toml"
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
        config_file = tmp_path / "saneless.toml"
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
        config_file = tmp_path / "saneless.toml"
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
        config_file = tmp_path / "saneless.toml"
        config_file.write_text(self._PREVIOUS_RUN)
        write_profiles_to_config(config_file, self._feeder_only())
        return config_file

    def test_an_auto_generated_default_survives(self, tmp_path: Path) -> None:
        """``default`` stays even though the new set does not name it."""
        parsed = tomllib.loads(self._rerun(tmp_path).read_text())
        assert "default" in parsed["profiles"]

    def test_default_is_not_reported_removed(self, tmp_path: Path) -> None:
        """Only the ordinary orphan is named under Removed, never ``default``."""
        config_file = tmp_path / "saneless.toml"
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


class TestNoSourceDevice:
    """
    A scanner with no SANE ``source`` option still gets a working ``default``.

    Such a device feeds without being told where from, and the backend assigns
    no source to it. Generating nothing for it left a config that saneless
    refuses to load, and let the prune delete every flagged profile an earlier
    run had written, together with the operator's ``default_tags``.
    """

    _CAPS = DeviceCapabilities(
        sources=[], resolutions=[150, 300], modes=["Gray", "Color"]
    )

    # What an earlier run against a flatbed scanner leaves behind, with an
    # operator's own key added to the flagged profile.
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
default_tags = [4]
auto_generated = true
"""

    def test_no_source_device_generates_only_default(self) -> None:
        """Generation emits exactly ``default``, flagged, at the chosen settings."""
        profiles = generate_profiles(self._CAPS)

        assert set(profiles) == {"default"}
        default = profiles["default"]
        assert default.resolution == 300
        assert default.mode == "Color"
        assert default.auto_generated is True

    def test_no_source_default_names_no_source(self) -> None:
        """The source is the model default, never one the device did not report."""
        default = generate_profiles(self._CAPS)["default"]
        assert "source" not in default.model_fields_set

    def test_no_source_default_text(self) -> None:
        """
        The human text says the scanner offers no choice, and scans one page.

        With no source to classify, the scan is routed as the model default's
        flatbed, one page per scan, even on a sheet-fed device; the
        description says so rather than promising a stack.
        """
        default = generate_profiles(self._CAPS)["default"]
        assert default.label == "Standard scan"
        assert default.description == (
            "Scans one page from the scanner, which offers no choice of where "
            "the page comes from."
        )

    def test_no_source_default_is_written_without_a_source_key(
        self, tmp_path: Path
    ) -> None:
        """The new file's ``default`` table carries no ``source``, and loads."""
        config_file = tmp_path / "saneless.toml"
        write_profiles_to_config(config_file, generate_profiles(self._CAPS))

        table = tomllib.loads(config_file.read_text())["profiles"]["default"]
        assert "source" not in table
        assert table["auto_generated"] is True
        settings = load_settings(str(config_file))
        assert "default" in settings.profiles

    def test_no_source_device_prunes_nothing(self, tmp_path: Path) -> None:
        """A flagged ``flatbed`` and its ``default_tags`` survive the run."""
        config_file = tmp_path / "saneless.toml"
        config_file.write_text(self._PREVIOUS_RUN)

        result = write_profiles_to_config(config_file, generate_profiles(self._CAPS))

        assert result.removed == ()
        settings = load_settings(str(config_file))
        assert settings.profiles["flatbed"].default_tags == [4]

    def test_no_source_force_drops_a_stale_source(self, tmp_path: Path) -> None:
        """
        ``--force`` deletes the ``source`` an earlier run wrote into ``default``.

        The generation owns ``source`` and no longer writes one, so a refresh
        removes it, as it does any owned key a fresh generation omits.
        """
        config_file = tmp_path / "saneless.toml"
        config_file.write_text(self._PREVIOUS_RUN)

        result = write_profiles_to_config(
            config_file, generate_profiles(self._CAPS), force=True
        )

        assert result.refreshed == ("default",)
        profiles = tomllib.loads(config_file.read_text())["profiles"]
        assert "source" not in profiles["default"]
        assert profiles["flatbed"]["default_tags"] == [4]


class TestTomlWriting:
    """TOML config file writing with comment preservation."""

    def test_preserves_comments(self, tmp_path: Path) -> None:
        """Writing profiles preserves existing TOML comments."""
        config_file = tmp_path / "saneless.toml"
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
        config_file = tmp_path / "saneless.toml"
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
        config_file = tmp_path / "saneless.toml"
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
        config_file = tmp_path / "saneless.toml"
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
        config_file = tmp_path / "saneless.toml"
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
        config_file = tmp_path / "auto_saneless.toml"
        write_profiles_to_config(config_file, profiles)
        content = config_file.read_text()
        assert "paper_size" not in content

    def test_creates_new_file(self, tmp_path: Path) -> None:
        """Creates config file if it does not exist."""
        config_file = tmp_path / "new_saneless.toml"

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
                # The text a generation gives this source: the writer copies
                # a profile's own label and description rather than deriving
                # them again.
                label=_profile_label("ADF Duplex"),
                description=_profile_description("ADF Duplex"),
            )
        return profiles

    def _write(
        self, tmp_path: Path, *, force: bool, extra: bool = False
    ) -> tuple[Path, ProfileWriteResult]:
        """Write the regenerated set over the fixture and return path and result."""
        config_file = tmp_path / "saneless.toml"
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
        """
        A name the file lacks is added with today's key order and flag.

        The human name leads the table, which is why _OWNED_KEYS puts it
        first: this is the order a reader opening the config meets.
        """
        config_file, result = self._write(tmp_path, force=force, extra=True)
        assert result.added == ("fresh",)
        assert (
            "[profiles.fresh]\n"
            'label = "Feeder, double-sided"\n'
            "description = "
            '"Scans both sides of every page using the document feeder."\n'
            'source = "ADF Duplex"\n'
            "resolution = 300\n"
            'mode = "Color"\n'
            'auto_source_mode = "adf"\n'
            'duplex = "hardware"\n'
            "auto_generated = true\n"
        ) in config_file.read_text()

    def test_merge_persisted_is_added_refreshed_and_unchanged(
        self, tmp_path: Path
    ) -> None:
        """
        ``persisted`` names exactly the tables that now match the generation.

        That is every table added or refreshed, and every flagged table
        ``force`` found already matching: a second forced run changes nothing,
        yet the file still holds the generated profiles.
        """
        config_file, first = self._write(tmp_path, force=True, extra=True)
        assert first.persisted == frozenset({"fresh", "scan"})

        second = write_profiles_to_config(
            config_file, self._generated(extra=True), force=True
        )
        assert second.refreshed == ()
        assert set(second.unchanged) == {"fresh", "scan"}
        assert second.persisted == frozenset({"fresh", "scan"})

    def test_force_on_an_unchanged_file_keeps_the_inode(self, tmp_path: Path) -> None:
        """
        A forced run over a file that already matches does not rewrite it.

        The file keeps its inode and its bytes, nothing is reported as
        refreshed, and the tables still count as persisted.
        """
        config_file = tmp_path / "saneless.toml"
        generated = generate_profiles(
            DeviceCapabilities(
                sources=["Flatbed", "ADF", "ADF Duplex"],
                resolutions=[150, 300, 600],
                modes=["Gray", "Color"],
            )
        )
        write_profiles_to_config(config_file, generated)
        inode = config_file.stat().st_ino
        before = config_file.read_bytes()

        result = write_profiles_to_config(config_file, generated, force=True)

        assert config_file.stat().st_ino == inode
        assert config_file.read_bytes() == before
        assert result.refreshed == ()
        assert set(result.unchanged) == set(generated)
        assert result.persisted == frozenset(generated)
        assert result.describe() == []

    @pytest.mark.parametrize(
        ("stored", "key"),
        [
            ("resolution = 300.0", "resolution"),
            ("resolution = 300", "auto_generated"),
        ],
        ids=["float-for-int", "int-for-bool"],
    )
    def test_force_compares_type_as_well_as_value(
        self, tmp_path: Path, stored: str, key: str
    ) -> None:
        """
        An owned key equal in value but not in type is a change, and refreshed.

        ``300 == 300.0`` and ``1 == True`` hold in Python, so a comparison by
        value alone would leave a float resolution, or a numeric flag, in the
        file for good.
        """
        flag = "auto_generated = 1" if key == "auto_generated" else ""
        config_file = tmp_path / "saneless.toml"
        config_file.write_text(
            "[profiles.default]\n"
            'label = "Glass (flatbed)"\n'
            'description = "Scans one page at a time from the glass."\n'
            'source = "Flatbed"\n'
            f"{stored}\n"
            'mode = "Color"\n'
            f"{flag or 'auto_generated = true'}\n"
        )
        generated = {
            "default": ProfileConfig(
                source="Flatbed",
                resolution=300,
                mode="Color",
                auto_generated=True,
                label="Glass (flatbed)",
                description="Scans one page at a time from the glass.",
            ),
        }

        result = write_profiles_to_config(config_file, generated, force=True)

        assert result.refreshed == ("default",)
        table = tomllib.loads(config_file.read_text())["profiles"]["default"]
        assert type(table[key]) is type(getattr(generated["default"], key))

    def test_force_treats_an_extra_owned_key_as_a_change(self, tmp_path: Path) -> None:
        """An owned key only the file holds is a change: the refresh deletes it."""
        config_file = tmp_path / "saneless.toml"
        config_file.write_text(
            "[profiles.default]\n"
            'label = "Glass (flatbed)"\n'
            'description = "Scans one page at a time from the glass."\n'
            'source = "Flatbed"\n'
            "resolution = 300\n"
            'mode = "Color"\n'
            'duplex = "none"\n'
            "auto_generated = true\n"
        )
        generated = {
            "default": ProfileConfig(
                source="Flatbed",
                resolution=300,
                mode="Color",
                auto_generated=True,
                label="Glass (flatbed)",
                description="Scans one page at a time from the glass.",
            ),
        }

        result = write_profiles_to_config(config_file, generated, force=True)

        assert result.refreshed == ("default",)
        table = tomllib.loads(config_file.read_text())["profiles"]["default"]
        assert "duplex" not in table

    def test_merge_result_describe_lists_groups_in_order(self, tmp_path: Path) -> None:
        """One line per non-empty group, in a fixed order, names shown with repr."""
        result = ProfileWriteResult(
            path=tmp_path / "saneless.toml",
            added=("a", "b"),
            refreshed=("c",),
            skipped_not_generated=("d", "default"),
            skipped_existing=("e",),
            removed=("f",),
        )
        assert result.describe() == [
            "Added: 'a', 'b'",
            "Refreshed: 'c'",
            "Skipped (not auto-generated): 'default' -- add auto_generated = "
            "true to its table to let auto-profiles --force refresh it",
            "Skipped (not auto-generated): 'd' -- not created by "
            "auto-profiles (no auto_generated = true); rename or delete it to "
            "regenerate",
            "Skipped (already exists; use --force to refresh): 'e'",
            "Removed (scanner no longer offers it): 'f'",
        ]

    def test_default_gets_its_own_skip_line(self, tmp_path: Path) -> None:
        """
        A hand-written ``default`` is told how to hand it back, on its own line.

        ``default`` cannot be renamed or deleted -- saneless requires it -- so
        the advice every other skipped profile gets would not work for it.
        """
        result = ProfileWriteResult(
            path=tmp_path / "saneless.toml",
            skipped_not_generated=("default", "receipts"),
        )
        assert result.describe() == [
            "Skipped (not auto-generated): 'default' -- add auto_generated = "
            "true to its table to let auto-profiles --force refresh it",
            "Skipped (not auto-generated): 'receipts' -- not created by "
            "auto-profiles (no auto_generated = true); rename or delete it to "
            "regenerate",
        ]

    def test_skip_line_without_default_is_unchanged(self, tmp_path: Path) -> None:
        """With no ``default`` among the skipped, the one generic line remains."""
        result = ProfileWriteResult(
            path=tmp_path / "saneless.toml",
            skipped_not_generated=("receipts",),
        )
        assert result.describe() == [
            "Skipped (not auto-generated): 'receipts' -- not created by "
            "auto-profiles (no auto_generated = true); rename or delete it to "
            "regenerate",
        ]

    def test_skip_line_for_default_alone(self, tmp_path: Path) -> None:
        """A skipped ``default`` alone gets its line and no empty generic one."""
        result = ProfileWriteResult(
            path=tmp_path / "saneless.toml",
            skipped_not_generated=("default",),
        )
        assert result.describe() == [
            "Skipped (not auto-generated): 'default' -- add auto_generated = "
            "true to its table to let auto-profiles --force refresh it",
        ]

    def test_merge_result_describe_omits_empty_groups(self, tmp_path: Path) -> None:
        """Empty groups print nothing; an empty result describes nothing."""
        path = tmp_path / "saneless.toml"
        assert ProfileWriteResult(path=path, refreshed=("x",)).describe() == [
            "Refreshed: 'x'"
        ]
        assert ProfileWriteResult(path=path).describe() == []


class TestLabelAndDescriptionAreOwnedKeys:
    """
    ``label`` and ``description`` behave exactly like every other owned key.

    D-18 chose one consistent ownership rule over a special case for free
    text: ``--force`` overwrites both in place on a flagged profile, and a
    profile without ``auto_generated`` is never touched. The fixture is the
    same shape ``TestForceMerge`` uses, so the comment and ``default_tags``
    preservation assertions are the ones that file already trusts.
    """

    _EXISTING = """\
# saneless configuration -- keep this comment
[profiles.scan]
# generated, then customised by hand
label = "My own name for this"
description = "My own sentence."
source = "Old Source"
resolution = 600
mode = "Gray"
default_tags = [3, 7]
title = "Scanned"
auto_generated = true

[profiles.handwritten]
source = "Flatbed"
resolution = 600
mode = "Gray"

[profiles.default]
source = "Flatbed"
resolution = 300
mode = "Color"
auto_generated = true
"""

    @staticmethod
    def _generated() -> dict[str, ProfileConfig]:
        """Build a fresh generation for ``scan``, ``handwritten`` and ``default``."""
        return generate_profiles(
            DeviceCapabilities(
                sources=["Flatbed", "ADF Duplex"],
                resolutions=[300],
                modes=["Color"],
            )
        ) | {
            "scan": ProfileConfig(
                source="ADF Front",
                resolution=300,
                mode="Color",
                auto_generated=True,
                label=_profile_label("ADF Front"),
                description=_profile_description("ADF Front"),
            ),
            "handwritten": ProfileConfig(
                source="ADF Front",
                resolution=300,
                mode="Color",
                auto_generated=True,
                label=_profile_label("ADF Front"),
                description=_profile_description("ADF Front"),
            ),
        }

    def _write(self, tmp_path: Path, *, force: bool) -> Path:
        """Write the regenerated set over the fixture and return the path."""
        config_file = tmp_path / "saneless.toml"
        config_file.write_text(self._EXISTING)
        write_profiles_to_config(config_file, self._generated(), force=force)
        return config_file

    def test_label_and_description_lead_the_owned_key_tuple(self) -> None:
        """
        The tuple order is the file key order, so the human name comes first.

        A reader opening a freshly written config meets the label before the
        SANE source string.
        """
        assert auto_profiles._OWNED_KEYS[:2] == ("label", "description")
        assert set(auto_profiles._OWNED_KEYS) >= {
            "label",
            "description",
            "source",
            "resolution",
            "mode",
            "auto_generated",
        }

    @pytest.mark.parametrize(
        "source", ["Flatbed", "ADF Front", "ADF Duplex", "Auto", "Mystery Tray"]
    )
    def test_generated_values_always_emits_both_keys(self, source: str) -> None:
        """
        Emission is unconditional, unlike auto_source_mode and duplex.

        This is what makes Phase 27 D-03's delete branch unreachable for the
        two free-text keys.
        """
        profile = ProfileConfig(
            source=source,
            resolution=300,
            mode="Color",
            auto_generated=True,
            label=_profile_label(source),
            description=_profile_description(source),
        )
        values = auto_profiles._generated_values(profile)
        assert values["label"] == profile.label
        assert values["description"] == profile.description
        assert list(values)[:2] == ["label", "description"]

    def test_generated_values_writes_the_profile_label_not_a_rederived_one(
        self,
    ) -> None:
        """
        The written text is the generated profile's own, ordinal included.

        A label depends on the whole source set once two sources share one, so
        the source name alone cannot reproduce it; re-deriving it here would
        write both feeders of a pair under the same name.
        """
        values = auto_profiles._generated_values(
            ProfileConfig(
                source="ADF",
                resolution=300,
                mode="Color",
                auto_generated=True,
                label="Feeder, single-sided 2",
                description="Scans one side of every page using the document feeder.",
            )
        )
        assert values["label"] == "Feeder, single-sided 2"
        assert (
            values["description"]
            == "Scans one side of every page using the document feeder."
        )

    def test_force_overwrites_a_hand_typed_label_and_description(
        self, tmp_path: Path
    ) -> None:
        """D-18: free text is owned text while the flag is set."""
        config_file = self._write(tmp_path, force=True)
        scan = tomllib.loads(config_file.read_text())["profiles"]["scan"]
        assert scan["label"] == "Feeder, front side only"
        assert scan["description"] == _profile_description("ADF Front")

    def test_force_refresh_keeps_unowned_keys_and_comments(
        self, tmp_path: Path
    ) -> None:
        """Overwriting the two new keys costs nothing else in the table."""
        config_file = self._write(tmp_path, force=True)
        text = config_file.read_text()
        scan = tomllib.loads(text)["profiles"]["scan"]
        assert scan["default_tags"] == [3, 7]
        assert scan["title"] == "Scanned"
        assert "# generated, then customised by hand" in text
        assert "# saneless configuration -- keep this comment" in text

    @pytest.mark.parametrize("force", [False, True])
    def test_a_profile_without_the_flag_never_gains_a_label(
        self, tmp_path: Path, *, force: bool
    ) -> None:
        """D-01: an unflagged profile's absent label stays absent."""
        config_file = self._write(tmp_path, force=force)
        handwritten = tomllib.loads(config_file.read_text())["profiles"]["handwritten"]
        assert "label" not in handwritten
        assert "description" not in handwritten

    def test_the_delete_branch_never_fires_for_the_two_free_text_keys(
        self, tmp_path: Path
    ) -> None:
        """
        A table that had both keys still has both after a refresh.

        Phase 27 D-03 deletes an owned key a fresh generation does not write.
        Unconditional emission is what keeps a free-text field from being
        silently pruned; this asserts the consequence rather than the cause.
        """
        config_file = self._write(tmp_path, force=True)
        profiles = tomllib.loads(config_file.read_text())["profiles"]
        assert "label" in profiles["scan"]
        assert "description" in profiles["scan"]
        assert "label" in profiles["default"]
        assert "description" in profiles["default"]

    def test_a_refreshed_config_reloads_with_the_generated_text(
        self, tmp_path: Path
    ) -> None:
        """The written values survive a real load, caps and all."""
        config_file = self._write(tmp_path, force=True)
        settings = load_settings(str(config_file))
        assert settings.profiles["scan"].label == "Feeder, front side only"
        assert settings.profiles["default"].label == "Glass (flatbed)"


class TestScanProfileHowToDocumentsOwnership:
    """
    The how-to says in plain words what ``--force`` does to a typed label.

    D-18's overwrite rule makes ``label`` the first free-text casualty, so
    the escape hatch has to be stated next to the rule and not only implied
    by the owned-key list (30-RESEARCH §7, Amendment A-3).
    """

    @staticmethod
    def _guide() -> str:
        """Read the how-to guide from the repository."""
        return (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "how-to"
            / "configure-scan-profiles.md"
        ).read_text(encoding="utf-8")

    @classmethod
    def _section(cls, heading: str) -> str:
        """
        Return the guide's ``## heading`` section, up to the next ``##`` heading.

        Args:
            heading: The section's title, without the hashes.

        Returns:
            The section's text, its own heading line excluded.

        """
        match = re.search(
            rf"^## {re.escape(heading)}\n(?P<body>.*?)(?=^## |\Z)",
            cls._guide(),
            re.MULTILINE | re.DOTALL,
        )
        assert match is not None, f"the guide has no '## {heading}' section"
        return match.group("body")

    def test_field_reference_lists_every_profile_key(self) -> None:
        """The field reference table has one row per profile key, and no others."""
        rows = re.findall(
            r"^\| `(?P<key>[a-z_]+)` \|",
            self._section("Profile field reference"),
            re.MULTILINE,
        )
        keys = {
            field.alias or name for name, field in ProfileConfig.model_fields.items()
        }
        assert sorted(rows) == sorted(keys)

    def test_guide_names_the_force_command_and_the_escape_hatch(self) -> None:
        """The auto-generated section states the overwrite rule and the way out."""
        section = self._section("Auto-generated profiles")
        assert re.search(r"\bsaneless auto-profiles --force\b", section)
        assert re.search(r"\bdelete the `auto_generated` line\b", section)

    def test_owned_key_list_in_the_guide_names_every_owned_key(self) -> None:
        """
        The guide's list of keys ``--force`` rewrites is exactly ``_OWNED_KEYS``.

        The list is prose, so nothing but a test keeps it honest.
        """
        match = re.search(
            r"only the generated keys \((?P<keys>[^)]*)\)",
            self._section("Auto-generated profiles"),
        )
        assert match is not None, "the guide no longer lists the generated keys"
        listed = re.findall(r"`([^`]+)`", match.group("keys"))
        assert len(listed) == len(set(listed)), listed
        assert set(listed) == set(auto_profiles._OWNED_KEYS)


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
        config_file = tmp_path / "saneless.toml"
        config_file.write_text(
            '[profiles.default]\nsource = "Flatbed"\n\n'
            f'[profiles.{name}]\nsource = "mine"\nauto_generated = "{flag}"\n'
        )
        return config_file

    @staticmethod
    def _generated_with_a_source() -> dict[str, ProfileConfig]:
        """
        Build a generated set that names a source besides ``default``.

        A set holding only ``default`` is what a scanner with no source option
        generates, and the prune never runs for it; these tests are about how
        the prune reads the flag, so the set must be one the prune runs for.
        """
        return {
            "default": ProfileConfig(
                source="Flatbed", resolution=300, mode="Color", auto_generated=True
            ),
            "flatbed": ProfileConfig(
                source="Flatbed", resolution=300, mode="Color", auto_generated=True
            ),
        }

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
        generated = self._generated_with_a_source()

        result = write_profiles_to_config(config_file, generated, force=force)

        assert result.removed == ()
        profiles = tomllib.loads(config_file.read_text())["profiles"]
        assert profiles["zzz"] == {"source": "mine", "auto_generated": flag}

    def test_a_string_true_flag_is_still_owned(self, tmp_path: Path) -> None:
        """``"yes"`` loads as True, so the writer owns and prunes it too."""
        config_file = self._config(tmp_path, "zzz", "yes")
        generated = self._generated_with_a_source()

        result = write_profiles_to_config(config_file, generated)

        assert result.removed == ("zzz",)

    def test_an_unparseable_flag_is_not_owned(self, tmp_path: Path) -> None:
        """A flag pydantic rejects is never read as the tool's (fail safe)."""
        config_file = tmp_path / "saneless.toml"
        config_file.write_text(
            '[profiles.default]\nsource = "Flatbed"\n\n'
            '[profiles.zzz]\nsource = "mine"\nauto_generated = "maybe"\n'
        )
        generated = self._generated_with_a_source()

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
        config_file = tmp_path / "saneless.toml"
        config_file.write_bytes(
            '# café\r\n[profiles.default]\r\nsource = "Flatbed"\r\n'.encode()
        )

        write_profiles_to_config(config_file, self._generated())

        data = config_file.read_bytes()
        assert "# café\r\n".encode() in data
        assert re.search(rb"(?<!\r)\n", data) is None
        profiles = tomllib.loads(data.decode("utf-8"))["profiles"]
        assert set(profiles) == {"default", "flatbed"}

    def test_mixed_line_endings_are_not_rewritten(self, tmp_path: Path) -> None:
        """
        A file mixing CRLF and LF keeps each line's ending (WR-08, D-05).

        One CRLF anywhere used to turn every bare LF in the output into CRLF,
        rewriting lines the user wrote with LF.
        """
        config_file = tmp_path / "saneless.toml"
        original = (
            b'[output]\r\nweb_port = 1\n\n[profiles.default]\nsource = "Flatbed"\n'
        )
        config_file.write_bytes(original)

        write_profiles_to_config(config_file, self._generated())

        data = config_file.read_bytes()
        assert data.startswith(original)
        assert data.count(b"\r\n") == 1

    def test_bare_lf_in_a_multiline_string_is_kept(self, tmp_path: Path) -> None:
        """A CRLF file whose multi-line string holds a bare LF keeps that value."""
        config_file = tmp_path / "saneless.toml"
        config_file.write_bytes(
            b'[profiles.default]\r\nsource = "Flatbed"\r\ntitle = """two\nlines"""\r\n'
        )

        write_profiles_to_config(config_file, self._generated())

        data = config_file.read_bytes()
        assert b'title = """two\nlines"""\r\n' in data
        assert "flatbed" in tomllib.loads(data.decode("utf-8"))["profiles"]

    def test_lf_utf8_config_stays_lf(self, tmp_path: Path) -> None:
        """An LF file gains no carriage returns."""
        config_file = tmp_path / "saneless.toml"
        config_file.write_bytes(b'[profiles.default]\nsource = "Flatbed"\n')

        write_profiles_to_config(config_file, self._generated())

        assert b"\r" not in config_file.read_bytes()

    def test_non_utf8_config_is_refused_utf8(self, tmp_path: Path) -> None:
        """Bytes that are not UTF-8 raise ConfigError and leave the file alone."""
        config_file = tmp_path / "saneless.toml"
        original = b"# caf\xe9\n"
        config_file.write_bytes(original)

        with pytest.raises(ConfigError, match="UTF-8") as caught:
            write_profiles_to_config(config_file, self._generated())

        assert str(config_file) in str(caught.value)
        assert config_file.read_bytes() == original

    def test_invalid_toml_config_is_config_error(self, tmp_path: Path) -> None:
        """
        A config tomlkit cannot parse is a ConfigError, file untouched (D-12).

        tomlkit's ``ParseError`` used to escape ``write_profiles_to_config`` raw
        (M-17, EXC-01); it now names the file, line and column and is chained,
        since tomlkit's message holds no document text.
        """
        config_file = tmp_path / "saneless.toml"
        original = b"a = = 1\n"
        config_file.write_bytes(original)

        with pytest.raises(ConfigError) as caught:
            write_profiles_to_config(config_file, self._generated(), force=False)

        message = str(caught.value)
        assert message.startswith(f"Cannot update {config_file}:")
        assert "line 1, column 4" in message
        # The position is given once, not again in tomlkit's own suffix.
        assert " col 4" not in message
        assert isinstance(caught.value.__cause__, ParseError)
        assert config_file.read_bytes() == original

    def test_toml_error_that_is_not_a_parse_error_is_config_error(
        self, tmp_path: Path
    ) -> None:
        """
        Every tomlkit error is a ConfigError, not only ``ParseError`` (IN-03).

        A table redefined under a dotted header raises ``KeyAlreadyPresent``,
        a ``TOMLKitError`` that is not a ``ParseError`` and carries no position,
        so the message claims none.
        """
        config_file = tmp_path / "saneless.toml"
        original = b"[a]\nb = 1\n[a.b]\nc = 1\n"
        config_file.write_bytes(original)

        with pytest.raises(ConfigError) as caught:
            write_profiles_to_config(config_file, self._generated(), force=False)

        assert str(caught.value) == (
            f'Cannot update {config_file}: it is not valid TOML (Key "b" already '
            "exists.)"
        )
        assert isinstance(caught.value.__cause__, TOMLKitError)
        assert not isinstance(caught.value.__cause__, ParseError)
        assert config_file.read_bytes() == original

    def test_inline_profiles_section_stays_valid_toml(self, tmp_path: Path) -> None:
        """A profile added to an inline ``profiles = {...}`` section re-parses."""
        config_file = tmp_path / "saneless.toml"
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
        config_file = tmp_path / "saneless.toml"
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
        config_file = tmp_path / "saneless.toml"
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
        config_file = tmp_path / "saneless.toml"
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
        config_file = tmp_path / "saneless.toml"
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
        config_file = tmp_path / "saneless.toml"
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
        real = tmp_path / "dotfiles" / "saneless.toml"
        real.parent.mkdir()
        real.write_text('[profiles.default]\nsource = "Flatbed"\n')
        link = tmp_path / "saneless.toml"
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

    def test_symlinked_parent_directory_logs_no_symlink_line(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        A regular config reached through a linked directory is no symlink.

        The file written is the file named, so a line calling it a symlink
        would send the operator looking for a link that does not exist.
        """
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        linked_dir = tmp_path / "linked"
        linked_dir.symlink_to(real_dir, target_is_directory=True)
        config_file = linked_dir / "saneless.toml"
        config_file.write_text('[profiles.default]\nsource = "Flatbed"\n')
        assert not config_file.is_symlink()

        with caplog.at_level(logging.INFO, logger="saneless.auto_profiles"):
            write_profiles_to_config(config_file, self._generated())

        assert "flatbed" in tomllib.loads(config_file.read_text())["profiles"]
        assert not [
            record
            for record in caplog.records
            if record.name == "saneless.auto_profiles"
            and "(symlink to" in record.getMessage()
        ]


class TestDevicePin:
    """
    ``device=`` writes the scanner a run used into an unset ``[scanner] device``.

    With ``scanner.device`` empty every scan goes to whichever device SANE
    lists first, so a scanner that appears on the LAN later can take them.
    Writing the id discovery chose closes that gap.  The key carries no
    ``auto_generated`` marker, so it is never the tool's to change once set:
    a non-empty value is left alone under ``force`` too.
    """

    _DEVICE = "net:h:1"

    @staticmethod
    def _generated() -> dict[str, ProfileConfig]:
        """One generated profile, the smallest set a run writes."""
        return {
            "default": ProfileConfig(
                source="Flatbed", resolution=300, mode="Color", auto_generated=True
            ),
        }

    def test_pin_adds_a_scanner_table_when_there_is_none(self, tmp_path: Path) -> None:
        """A file with no ``[scanner]`` table gains one holding the device."""
        config_file = tmp_path / "saneless.toml"
        config_file.write_text('[paperless]\nurl = "http://nas:8000"\n')

        result = write_profiles_to_config(
            config_file, self._generated(), device=self._DEVICE
        )

        data = tomllib.loads(config_file.read_text())
        assert data["scanner"]["device"] == self._DEVICE
        assert data["paperless"]["url"] == "http://nas:8000"
        assert result.pinned_device == self._DEVICE

    def test_pin_fills_an_empty_device(self, tmp_path: Path) -> None:
        """``device = ""`` is unset, so it is filled; its neighbours survive."""
        config_file = tmp_path / "saneless.toml"
        config_file.write_text(
            '[scanner]\nhost = "scanbox"  # the saned host\ndevice = ""\n'
        )

        result = write_profiles_to_config(
            config_file, self._generated(), device=self._DEVICE
        )

        text = config_file.read_text()
        data = tomllib.loads(text)
        assert data["scanner"] == {"host": "scanbox", "device": self._DEVICE}
        assert "# the saned host" in text
        assert result.pinned_device == self._DEVICE

    @pytest.mark.parametrize("force", [False, True])
    def test_pin_never_overwrites_a_set_device(
        self, tmp_path: Path, *, force: bool
    ) -> None:
        """A device the operator chose is left byte for byte, ``force`` or not."""
        config_file = tmp_path / "saneless.toml"
        # A hand-written default, so nothing else in the run changes the file
        # and a byte comparison isolates the pin.
        original = (
            b'[scanner]\ndevice = "other"\n\n'
            b'[profiles.default]\nsource = "ADF"\nauto_generated = false\n'
        )
        config_file.write_bytes(original)

        result = write_profiles_to_config(
            config_file, self._generated(), force=force, device=self._DEVICE
        )

        assert config_file.read_bytes() == original
        assert result.pinned_device is None
        assert not any(line.startswith("Pinned") for line in result.describe())

    def test_pin_counts_as_a_change_when_the_profiles_do_not(
        self, tmp_path: Path
    ) -> None:
        """Profiles already present do not stop the pin from being written."""
        config_file = tmp_path / "saneless.toml"
        config_file.write_text(
            '[profiles.default]\nsource = "Flatbed"\nresolution = 300\n'
            'mode = "Color"\nauto_generated = true\n'
        )

        result = write_profiles_to_config(
            config_file, self._generated(), device=self._DEVICE
        )

        assert result.added == ()
        assert result.skipped_existing == ("default",)
        assert result.pinned_device == self._DEVICE
        scanner = tomllib.loads(config_file.read_text())["scanner"]
        assert scanner["device"] == self._DEVICE

    def test_pin_absent_without_a_device(self, tmp_path: Path) -> None:
        """No ``device`` argument, no ``[scanner]`` table: the worker's call."""
        config_file = tmp_path / "saneless.toml"

        result = write_profiles_to_config(config_file, self._generated())

        assert "scanner" not in tomllib.loads(config_file.read_text())
        assert result.pinned_device is None

    def test_pin_refuses_a_scanner_key_that_is_not_a_table(
        self, tmp_path: Path
    ) -> None:
        """A scalar ``scanner`` is not overwritten; the file is left intact."""
        config_file = tmp_path / "saneless.toml"
        original = b'scanner = "oops"\n'
        config_file.write_bytes(original)

        with pytest.raises(ConfigError, match=r"\[scanner\]"):
            write_profiles_to_config(
                config_file, self._generated(), device=self._DEVICE
            )

        assert config_file.read_bytes() == original

    def test_pin_is_logged_with_repr(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The id is untrusted, so the log line quotes it with ``%r``."""
        config_file = tmp_path / "saneless.toml"

        with caplog.at_level(logging.INFO, logger="saneless.auto_profiles"):
            write_profiles_to_config(
                config_file, self._generated(), device=self._DEVICE
            )

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "saneless.auto_profiles"
            and "Pinned [scanner] device" in record.getMessage()
        ]
        assert messages == ["Pinned [scanner] device to 'net:h:1'"]

    def test_pin_skips_an_id_holding_control_characters(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        r"""
        An id with control characters is not pinned; the profiles still are.

        tomlkit writes ESC as ``\e``, which Python's TOML 1.0 reader rejects,
        so the round-trip guard would refuse the whole run.  Skipping the pin
        keeps the profiles and says why, with the id quoted by ``%r``.
        """
        config_file = tmp_path / "saneless.toml"
        device = "net:h:1\x1b[2J"

        with caplog.at_level(logging.INFO, logger="saneless.auto_profiles"):
            result = write_profiles_to_config(
                config_file, self._generated(), device=device
            )

        data = tomllib.loads(config_file.read_text())
        assert "scanner" not in data
        assert "default" in data["profiles"]
        assert result.pinned_device is None
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.name == "saneless.auto_profiles"
            and record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert repr(device) in warnings[0]
        assert "\x1b" not in warnings[0]

    def test_pin_describe_names_the_device(self, tmp_path: Path) -> None:
        """The pin is its own line, after the profile groups, quoted with repr."""
        result = ProfileWriteResult(
            path=tmp_path / "saneless.toml",
            added=("a",),
            pinned_device=self._DEVICE,
        )
        assert result.describe() == [
            "Added: 'a'",
            "Pinned [scanner] device: 'net:h:1'",
        ]
