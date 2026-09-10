"""Tests for automatic scanner profile generation."""

from __future__ import annotations

from typing import TYPE_CHECKING

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


class TestSourceToSlug:
    """Source name to profile slug conversion."""

    def test_flatbed(self) -> None:
        """Flatbed source maps to flatbed-scan slug."""
        assert source_to_slug("Flatbed") == "flatbed-scan"

    def test_adf(self) -> None:
        """ADF source maps to adf-simplex slug."""
        assert source_to_slug("ADF") == "adf-simplex"

    def test_automatic_document_feeder(self) -> None:
        """Automatic Document Feeder maps to adf-simplex."""
        assert source_to_slug("Automatic Document Feeder") == "adf-simplex"

    def test_adf_duplex(self) -> None:
        """ADF Duplex maps to adf-duplex."""
        assert source_to_slug("ADF Duplex") == "adf-duplex"

    def test_case_insensitive_adf_duplex(self) -> None:
        """Case-insensitive ADF duplex mapping."""
        assert source_to_slug("Adf-duplex") == "adf-duplex"

    def test_adf_front(self) -> None:
        """ADF Front maps to adf-simplex."""
        assert source_to_slug("ADF Front") == "adf-simplex"

    def test_auto_source(self) -> None:
        """Auto source maps to auto-scan slug."""
        assert source_to_slug("Auto") == "auto-scan"

    def test_auto_source_case_insensitive(self) -> None:
        """Auto source mapping is case-insensitive."""
        assert source_to_slug("auto") == "auto-scan"

    def test_adf_back_fallback(self) -> None:
        """ADF Back produces a slug that is not adf-simplex or adf-duplex."""
        slug = source_to_slug("ADF Back")
        assert slug not in ("adf-simplex", "adf-duplex")


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
        """Single flatbed source generates flatbed-scan and default profiles."""
        caps = DeviceCapabilities(
            sources=["Flatbed"],
            resolutions=[150, 300, 600],
            modes=["Color", "Gray"],
        )
        profiles = generate_profiles(caps)
        assert "flatbed-scan" in profiles
        assert "default" in profiles
        assert profiles["flatbed-scan"].source == "Flatbed"
        assert profiles["flatbed-scan"].resolution == 300
        assert profiles["flatbed-scan"].mode == "Color"
        assert profiles["flatbed-scan"].auto_generated is True

    def test_multiple_sources(self) -> None:
        """Multiple sources generate correct number of profiles."""
        caps = DeviceCapabilities(
            sources=["Flatbed", "ADF", "ADF Duplex"],
            resolutions=[200, 400],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert "flatbed-scan" in profiles
        assert "adf-simplex" in profiles
        assert "adf-duplex" in profiles
        assert "default" in profiles
        assert len(profiles) == 4
        # Closest to 300 from [200, 400] is 200
        assert profiles["flatbed-scan"].resolution == 200

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
        assert "auto-scan" in profiles
        assert profiles["auto-scan"].auto_source_mode == "adf"

    def test_auto_with_flatbed_sets_flatbed_mode(self) -> None:
        """Auto source defaults to flatbed mode when Flatbed source exists."""
        caps = DeviceCapabilities(
            sources=["Auto", "Flatbed"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert "auto-scan" in profiles
        assert profiles["auto-scan"].auto_source_mode == "flatbed"

    def test_auto_only_sets_adf_mode(self) -> None:
        """Auto source alone (no Flatbed, no ADF) defaults to adf mode."""
        caps = DeviceCapabilities(
            sources=["Auto"],
            resolutions=[300],
            modes=["Color"],
        )
        profiles = generate_profiles(caps)
        assert "auto-scan" in profiles
        assert profiles["auto-scan"].auto_source_mode == "adf"

    def test_flatbed_no_regression(self) -> None:
        """Flatbed source still returns flatbed-scan slug (no regression)."""
        assert source_to_slug("Flatbed") == "flatbed-scan"


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
            "auto-scan": ProfileConfig(
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
            "auto-scan": ProfileConfig(
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
