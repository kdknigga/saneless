"""Tests for configuration loading and validation."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

import saneless.config as config_mod
from saneless.config import (
    DEFAULT_RESOLUTION,
    OutputConfig,
    ProfileConfig,
    Settings,
    load_settings,
    validate_settings_dirs,
    warn_on_legacy_duplex_sources,
)
from saneless.exceptions import (
    ConfigError,
    FeederEmptyError,
    PaperlessError,
    SanelessError,
    ScanError,
)


class TestLoadSettingsFromToml:
    """Settings load correctly from a TOML file."""

    def test_load_settings_from_toml(self, sample_toml: Path) -> None:
        """TOML values are correctly loaded into Settings fields."""
        settings = load_settings(config_path=str(sample_toml))
        assert settings.scanner.host == "192.168.1.50"
        assert settings.paperless.url == "http://paperless:8000"
        expected_auth = "abc123"
        assert settings.paperless.token == expected_auth

    def test_env_var_override(
        self, sample_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Environment variables override TOML values."""
        monkeypatch.setenv("SANELESS_SCANNER__HOST", "10.0.0.1")
        settings = load_settings(config_path=str(sample_toml))
        assert settings.scanner.host == "10.0.0.1"

    def test_env_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SANELESS_ prefix is used for environment variable discovery."""
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", "envtoken")
        settings = load_settings()
        expected_auth = "envtoken"
        assert settings.paperless.token == expected_auth

    def test_nested_env_delimiter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Double underscore delimiter supports nested settings."""
        monkeypatch.setenv("SANELESS_OUTPUT__LOG_LEVEL", "DEBUG")
        settings = load_settings()
        assert settings.output.log_level == "DEBUG"


class TestDefaultProfile:
    """Default profile validation."""

    def test_default_profile_required(self, tmp_config_dir: Path) -> None:
        """Missing default profile raises a validation error."""
        toml_content = """\
[scanner]
host = "192.168.1.50"

[profiles.custom]
source = "ADF"
"""
        config_file = tmp_config_dir / "no_default.toml"
        config_file.write_text(toml_content)
        with pytest.raises(ValueError, match="default"):
            load_settings(config_path=str(config_file))

    def test_default_profile_present(self, sample_toml: Path) -> None:
        """Valid TOML includes a default profile."""
        settings = load_settings(config_path=str(sample_toml))
        assert "default" in settings.profiles


class TestProfileFields:
    """Profile configuration fields."""

    def test_profile_fields(self, tmp_config_dir: Path) -> None:
        """Profile source, resolution, and mode are loaded correctly."""
        toml_content = """\
[profiles.default]
source = "Flatbed"
resolution = 600
mode = "color"
"""
        config_file = tmp_config_dir / "profiles.toml"
        config_file.write_text(toml_content)
        settings = load_settings(config_path=str(config_file))
        profile = settings.profiles["default"]
        assert profile.source == "Flatbed"
        assert profile.resolution == 600
        assert profile.mode == "color"


class TestConfigFileSearch:
    """Config file search behavior."""

    def test_config_file_search_explicit(
        self, sample_toml: Path, tmp_config_dir: Path
    ) -> None:
        """Explicit config_path takes precedence over search paths."""
        other_toml = tmp_config_dir / "other.toml"
        other_toml.write_text("[scanner]\nhost = 'other'\n\n[profiles.default]\n")
        settings = load_settings(config_path=str(sample_toml))
        assert settings.scanner.host == "192.168.1.50"

    def test_config_file_search_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Auto-discovery finds saneless.toml in the current directory."""
        monkeypatch.chdir(tmp_path)
        toml_content = """\
[scanner]
host = "found-by-search"

[profiles.default]
source = "Flatbed"
"""
        (tmp_path / "saneless.toml").write_text(toml_content)
        settings = load_settings()
        assert settings.scanner.host == "found-by-search"

    def test_no_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No config file results in default settings."""
        monkeypatch.chdir(tmp_path)
        settings = load_settings()
        assert settings.scanner.host == ""
        assert "default" in settings.profiles


class TestInvalidToml:
    """Invalid TOML handling."""

    def test_invalid_toml(self, tmp_config_dir: Path) -> None:
        """Malformed TOML raises an exception during loading."""
        bad_file = tmp_config_dir / "bad.toml"
        bad_file.write_text("this is not [valid toml\n===broken===")
        with pytest.raises(ValueError, match=r"(?i)invalid|expected|toml"):
            load_settings(config_path=str(bad_file))


class TestSettingsDefaults:
    """Default values when no config or env vars."""

    def test_settings_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default settings provide empty strings and standard paths."""
        monkeypatch.chdir(tmp_path)
        settings = load_settings()
        assert settings.scanner.host == ""
        assert settings.paperless.url == ""
        assert settings.output.tmp_dir.endswith("saneless")
        assert settings.output.log_level == "INFO"


class TestExceptionHierarchy:
    """Custom exception hierarchy."""

    def test_exception_hierarchy(self) -> None:
        """All custom exceptions inherit from SanelessError."""
        assert issubclass(SanelessError, Exception)
        assert issubclass(ConfigError, SanelessError)
        assert issubclass(ScanError, SanelessError)
        assert issubclass(PaperlessError, SanelessError)

    def test_exceptions_are_raisable(self) -> None:
        """Each custom exception can be raised and caught as SanelessError."""
        msg = "bad config"
        with pytest.raises(SanelessError):
            raise ConfigError(msg)
        msg = "scan failed"
        with pytest.raises(SanelessError):
            raise ScanError(msg)
        msg = "api error"
        with pytest.raises(SanelessError):
            raise PaperlessError(msg)

    def test_feeder_empty_error_is_scan_error(self) -> None:
        """FeederEmptyError is a subclass of ScanError."""
        assert issubclass(FeederEmptyError, ScanError)
        msg = "no paper"
        with pytest.raises(ScanError):
            raise FeederEmptyError(msg)


class TestTomlStructureErrors:
    """User-friendly error messages for common TOML structure mistakes."""

    def test_wrong_section_name_gives_helpful_error(self, tmp_config_dir: Path) -> None:
        """TOML [default] instead of [profiles.default] gives a helpful ConfigError."""
        toml_content = '[default]\ntitle = "Test Doc"\n'
        config_file = tmp_config_dir / "wrong_section.toml"
        config_file.write_text(toml_content)
        with pytest.raises(ConfigError, match=r"profiles\.default"):
            load_settings(config_path=str(config_file))

    def test_unknown_toplevel_section_error(self, tmp_config_dir: Path) -> None:
        """Unknown top-level TOML section raises ConfigError naming the section."""
        toml_content = '[bogus]\nfoo = "bar"\n\n[profiles.default]\n'
        config_file = tmp_config_dir / "bogus_section.toml"
        config_file.write_text(toml_content)
        with pytest.raises(ConfigError, match="bogus"):
            load_settings(config_path=str(config_file))

    def test_title_alias_works(self, tmp_config_dir: Path) -> None:
        """The 'title' field in [profiles.default] maps to default_title_template."""
        toml_content = '[profiles.default]\ntitle = "My Doc"\n'
        config_file = tmp_config_dir / "title_alias.toml"
        config_file.write_text(toml_content)
        settings = load_settings(config_path=str(config_file))
        assert settings.profiles["default"].default_title_template == "My Doc"

    def test_title_env_var_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Env var SANELESS_PROFILES__DEFAULT__TITLE sets default_title_template."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SANELESS_PROFILES__DEFAULT__TITLE", "EnvTitle")
        settings = load_settings()
        assert settings.profiles["default"].default_title_template == "EnvTitle"


class TestProfileConfigThresholds:
    """ProfileConfig empty page threshold fields."""

    def test_default_mean_threshold(self) -> None:
        """ProfileConfig has empty_page_mean_threshold defaulting to 250.0."""
        profile = ProfileConfig()
        assert profile.empty_page_mean_threshold == 250.0

    def test_default_stddev_threshold(self) -> None:
        """ProfileConfig has empty_page_stddev_threshold defaulting to 5.0."""
        profile = ProfileConfig()
        assert profile.empty_page_stddev_threshold == 5.0

    def test_custom_threshold_values(self, tmp_config_dir: Path) -> None:
        """ProfileConfig accepts custom threshold values from TOML."""
        toml_content = """\
[profiles.default]
source = "ADF"
empty_page_mean_threshold = 240.0
empty_page_stddev_threshold = 10.0
"""
        config_file = tmp_config_dir / "thresholds.toml"
        config_file.write_text(toml_content)
        settings = load_settings(config_path=str(config_file))
        profile = settings.profiles["default"]
        assert profile.empty_page_mean_threshold == 240.0
        assert profile.empty_page_stddev_threshold == 10.0


class TestDefaultResolution:
    """DEFAULT_RESOLUTION constant validation."""

    def test_constant_value(self) -> None:
        """DEFAULT_RESOLUTION is 300 DPI per Tesseract OCR recommendation."""
        assert DEFAULT_RESOLUTION == 300

    def test_profile_default_matches_constant(self) -> None:
        """ProfileConfig resolution default matches DEFAULT_RESOLUTION."""
        profile = ProfileConfig()
        assert profile.resolution == DEFAULT_RESOLUTION


class TestEmptyPageDetectionToggle:
    """Empty page detection toggle on ProfileConfig."""

    def test_enable_empty_page_detection_default_true(self) -> None:
        """ProfileConfig has enable_empty_page_detection defaulting to True."""
        profile = ProfileConfig()
        assert profile.enable_empty_page_detection is True

    def test_enable_empty_page_detection_false(self) -> None:
        """ProfileConfig accepts enable_empty_page_detection=False."""
        profile = ProfileConfig(enable_empty_page_detection=False)
        assert profile.enable_empty_page_detection is False


class TestMinFreeSpaceMb:
    """OutputConfig min_free_space_mb field."""

    def test_min_free_space_mb_default(self) -> None:
        """OutputConfig has min_free_space_mb defaulting to 500."""
        output = OutputConfig()
        assert output.min_free_space_mb == 500


class TestFlipTimeoutSeconds:
    """
    OutputConfig flip_timeout_seconds field (DPLX-05, D-10, WR-01).

    The wait is bounded to one second through one day.  Zero or a negative
    value makes the flip wait expire at once, failing every manual-duplex job
    right after pass A; a value above ``threading.TIMEOUT_MAX`` makes
    ``Event.wait`` raise ``OverflowError`` at the same point.  Both are
    rejected at load, where the CLI reports a configuration error.
    """

    def test_flip_timeout_seconds_default(self) -> None:
        """Settings default the manual-duplex flip wait to ten minutes."""
        assert Settings().output.flip_timeout_seconds == 600

    @pytest.mark.parametrize("value", [0, -5, 86_401])
    def test_flip_timeout_seconds_out_of_bounds_rejected(self, value: int) -> None:
        """Zero, a negative value and anything above a day fail validation."""
        with pytest.raises(ValidationError, match="flip_timeout_seconds"):
            OutputConfig(flip_timeout_seconds=value)

    @pytest.mark.parametrize("value", [1, 86_400])
    def test_flip_timeout_seconds_bounds_accepted(self, value: int) -> None:
        """One second and exactly one day are the inclusive bounds."""
        assert OutputConfig(flip_timeout_seconds=value).flip_timeout_seconds == value

    def test_flip_timeout_seconds_zero_in_toml_rejected(
        self, tmp_config_dir: Path
    ) -> None:
        """A TOML ``flip_timeout_seconds = 0`` fails at load, not after pass A."""
        toml_content = """\
[output]
flip_timeout_seconds = 0

[profiles.default]
"""
        config_file = tmp_config_dir / "flip_timeout_zero.toml"
        config_file.write_text(toml_content)
        with pytest.raises(ValidationError, match="flip_timeout_seconds"):
            load_settings(config_path=str(config_file))

    def test_flip_timeout_seconds_from_toml(self, tmp_config_dir: Path) -> None:
        """The timeout is read from the [output] section."""
        toml_content = """\
[output]
flip_timeout_seconds = 90

[profiles.default]
"""
        config_file = tmp_config_dir / "flip_timeout.toml"
        config_file.write_text(toml_content)
        settings = load_settings(config_path=str(config_file))
        assert settings.output.flip_timeout_seconds == 90


class TestDuplexField:
    """ProfileConfig duplex field validation (DPLX-01)."""

    def test_duplex_default_none(self) -> None:
        """ProfileConfig defaults duplex to 'none'."""
        assert ProfileConfig().duplex == "none"

    def test_duplex_hardware(self) -> None:
        """ProfileConfig accepts duplex='hardware'."""
        assert ProfileConfig(duplex="hardware").duplex == "hardware"

    def test_duplex_manual(self) -> None:
        """ProfileConfig accepts duplex='manual'."""
        assert ProfileConfig(duplex="manual").duplex == "manual"

    def test_duplex_invalid_raises(self) -> None:
        """A fourth duplex value is rejected at load."""
        with pytest.raises(ValidationError, match="duplex"):
            ProfileConfig.model_validate({"duplex": "both"})

    def test_duplex_manual_keeps_a_real_source(self) -> None:
        """Setting duplex leaves source exactly as written."""
        profile = ProfileConfig(source="ADF", duplex="manual")
        assert profile.source == "ADF"
        assert profile.duplex == "manual"


class TestLegacyManualDuplexSource:
    """
    Tests for the legacy manual-duplex source predicate.

    Relocated from tests/test_pipeline.py: the substring rule no longer
    chooses a scanning strategy, but it still recognises the deprecated
    ``source = "Manual Duplex"`` form (DPLX-02) and keeps its edge cases.
    """

    def test_adf_manual_duplex(self) -> None:
        """Source 'ADF Manual Duplex' is the legacy manual duplex form."""
        assert config_mod._is_legacy_manual_duplex_source("ADF Manual Duplex") is True

    def test_manual_duplex_case_insensitive(self) -> None:
        """Case insensitive detection."""
        assert config_mod._is_legacy_manual_duplex_source("adf manual duplex") is True
        assert config_mod._is_legacy_manual_duplex_source("MANUAL DUPLEX") is True

    def test_flatbed_not_manual_duplex(self) -> None:
        """Flatbed is not manual duplex."""
        assert config_mod._is_legacy_manual_duplex_source("Flatbed") is False

    def test_adf_not_manual_duplex(self) -> None:
        """Plain ADF (no manual) is not manual duplex."""
        assert config_mod._is_legacy_manual_duplex_source("ADF") is False

    def test_hardware_duplex_not_manual(self) -> None:
        """Hardware duplex without 'manual' is not manual duplex."""
        assert config_mod._is_legacy_manual_duplex_source("ADF Duplex") is False


class TestLegacyManualDuplexTranslation:
    """A legacy source = "Manual Duplex" loads as duplex = "manual" (DPLX-02)."""

    def test_manual_duplex_source_translates(self) -> None:
        """The legacy marker sets duplex and leaves source verbatim."""
        profile = ProfileConfig(source="Manual Duplex")
        assert profile.duplex == "manual"
        assert profile.source == "Manual Duplex"

    def test_adf_manual_duplex_source_translates(self) -> None:
        """Any source carrying both words is the legacy marker."""
        assert ProfileConfig(source="ADF Manual Duplex").duplex == "manual"

    def test_hardware_duplex_source_does_not_translate(self) -> None:
        """A hardware duplex source is not manual duplex."""
        assert ProfileConfig(source="ADF Duplex").duplex == "none"

    def test_explicit_duplex_is_never_overwritten(self) -> None:
        """An explicitly written duplex beats the legacy inference (Pitfall 3)."""
        profile = ProfileConfig(source="Manual Duplex", duplex="none")
        assert profile.duplex == "none"

    def test_nested_profile_dict_translates(self) -> None:
        """A raw nested profile dict, as the merged sources produce, translates."""
        settings = Settings.model_validate(
            {"profiles": {"default": {}, "legacy": {"source": "Manual Duplex"}}}
        )
        assert settings.profiles["legacy"].duplex == "manual"
        assert settings.profiles["default"].duplex == "none"

    def test_legacy_toml_round_trips(self, tmp_config_dir: Path) -> None:
        """A legacy TOML profile still loads, read as manual duplex."""
        toml_content = """\
[profiles.default]
source = "Flatbed"

[profiles.legacy]
source = "Manual Duplex"
"""
        config_file = tmp_config_dir / "legacy_duplex.toml"
        config_file.write_text(toml_content)
        settings = load_settings(config_path=str(config_file))
        legacy = settings.profiles["legacy"]
        assert legacy.duplex == "manual"
        assert legacy.source == "Manual Duplex"


class TestLegacyDuplexWarning:
    """
    A legacy manual-duplex source is warned about once, by name (D-04, D-18).

    The warning is emitted by ``warn_on_legacy_duplex_sources``, which the CLI
    calls after logging is configured (WR-05), not by loading settings -- a
    record logged inside ``load_settings`` would never reach ``log_file``.
    """

    _LEGACY_TOML = """\
[profiles.default]
source = "Flatbed"

[profiles.legacy]
source = "Manual Duplex"
"""

    @staticmethod
    def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
        """Return the WARNING messages saneless.config emitted."""
        return [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING and record.name == "saneless.config"
        ]

    def test_loading_settings_alone_emits_nothing(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Loading happens before logging is configured, so it must stay quiet."""
        config_file = tmp_config_dir / "legacy_load_only.toml"
        config_file.write_text(self._LEGACY_TOML)
        with caplog.at_level(logging.WARNING, logger="saneless.config"):
            settings = load_settings(config_path=str(config_file))

        assert settings.profiles["legacy"].duplex == "manual"
        assert self._warnings(caplog) == []

    def test_legacy_profile_warns_once_with_migration_instruction(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The WARNING names the profile, the replacement and the source command."""
        config_file = tmp_config_dir / "legacy_warning.toml"
        config_file.write_text(self._LEGACY_TOML)
        settings = load_settings(config_path=str(config_file))
        with caplog.at_level(logging.WARNING, logger="saneless.config"):
            warn_on_legacy_duplex_sources(settings)

        warnings = self._warnings(caplog)
        assert len(warnings) == 1
        message = warnings[0]
        assert "'legacy'" in message
        assert 'duplex = "manual"' in message
        assert "devices --capabilities" in message

    @pytest.mark.parametrize("duplex", ["none", "hardware"])
    def test_legacy_source_with_non_manual_duplex_warns(
        self,
        duplex: str,
        tmp_config_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A legacy-looking source with an explicit non-manual duplex is warned (IN-04).

        Explicit configuration still wins -- the profile is not read as manual
        duplex -- but the source goes to the scanner verbatim, so the operator
        is told rather than left to find out from a SANE error.
        """
        toml_content = f"""\
[profiles.default]
source = "Flatbed"

[profiles.odd]
source = "Manual Duplex"
duplex = "{duplex}"
"""
        config_file = tmp_config_dir / f"legacy_{duplex}.toml"
        config_file.write_text(toml_content)
        settings = load_settings(config_path=str(config_file))
        with caplog.at_level(logging.WARNING, logger="saneless.config"):
            warn_on_legacy_duplex_sources(settings)

        assert settings.profiles["odd"].duplex == duplex
        warnings = self._warnings(caplog)
        assert len(warnings) == 1
        message = warnings[0]
        assert "'odd'" in message
        assert "'Manual Duplex'" in message
        assert f"duplex = '{duplex}'" in message
        assert "has not read it as manual duplex" in message
        assert "devices --capabilities" in message

    def test_explicit_manual_duplex_does_not_warn(
        self, tmp_config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A profile already using duplex = "manual" and a real source is quiet."""
        toml_content = """\
[profiles.default]
source = "ADF"
duplex = "manual"
"""
        config_file = tmp_config_dir / "modern_duplex.toml"
        config_file.write_text(toml_content)
        settings = load_settings(config_path=str(config_file))
        with caplog.at_level(logging.WARNING, logger="saneless.config"):
            warn_on_legacy_duplex_sources(settings)

        assert settings.profiles["default"].duplex == "manual"
        assert self._warnings(caplog) == []


class TestAutoSourceMode:
    """ProfileConfig auto_source_mode field validation."""

    def test_auto_source_mode_default_flatbed(self) -> None:
        """ProfileConfig defaults auto_source_mode to 'flatbed'."""
        profile = ProfileConfig()
        assert profile.auto_source_mode == "flatbed"

    def test_auto_source_mode_flatbed(self) -> None:
        """ProfileConfig accepts auto_source_mode='flatbed'."""
        profile = ProfileConfig(auto_source_mode="flatbed")
        assert profile.auto_source_mode == "flatbed"

    def test_auto_source_mode_adf(self) -> None:
        """ProfileConfig accepts auto_source_mode='adf'."""
        profile = ProfileConfig(auto_source_mode="adf")
        assert profile.auto_source_mode == "adf"

    def test_auto_source_mode_invalid_raises(self) -> None:
        """ProfileConfig rejects invalid auto_source_mode values."""
        with pytest.raises(ValueError, match="auto_source_mode"):
            ProfileConfig.model_validate({"auto_source_mode": "invalid"})


class TestValidateSettingsDirs:
    """Writability validation via validate_settings_dirs."""

    def test_validate_writable_tmp_dir_passes(self, tmp_path: Path) -> None:
        """No error when tmp_dir is writable."""
        settings = Settings(
            output=OutputConfig(tmp_dir=str(tmp_path)),
            profiles={"default": ProfileConfig()},
        )
        # Should not raise
        validate_settings_dirs(settings)

    def test_validate_unwritable_tmp_dir_fails_with_config_error(
        self, tmp_path: Path
    ) -> None:
        """Unwritable tmp_dir raises ConfigError with 'not writable' message."""
        unwritable = tmp_path / "readonly"
        unwritable.mkdir()
        unwritable.chmod(0o444)
        settings = Settings(
            output=OutputConfig(tmp_dir=str(unwritable)),
            profiles={"default": ProfileConfig()},
        )
        with pytest.raises(ConfigError, match="not writable"):
            validate_settings_dirs(settings)
        # Restore permissions for cleanup
        unwritable.chmod(0o755)

    def test_validate_writable_data_dir_passes(self, tmp_path: Path) -> None:
        """No error when data_dir is writable."""
        settings = Settings(
            output=OutputConfig(tmp_dir=str(tmp_path), data_dir=str(tmp_path)),
            profiles={"default": ProfileConfig()},
        )
        # Should not raise
        validate_settings_dirs(settings)

    def test_validate_unwritable_data_dir_fails_with_config_error(
        self, tmp_path: Path
    ) -> None:
        """Unwritable data_dir raises ConfigError with 'not writable' message."""
        unwritable = tmp_path / "readonly"
        unwritable.mkdir()
        unwritable.chmod(0o444)
        settings = Settings(
            output=OutputConfig(tmp_dir=str(tmp_path), data_dir=str(unwritable)),
            profiles={"default": ProfileConfig()},
        )
        with pytest.raises(ConfigError, match="not writable"):
            validate_settings_dirs(settings)
        # Restore permissions for cleanup
        unwritable.chmod(0o755)

    def test_validate_missing_data_dir_unwritable_parent_fails(
        self, tmp_path: Path
    ) -> None:
        """A missing data_dir under an unwritable parent raises ConfigError."""
        parent = tmp_path / "readonly"
        parent.mkdir()
        parent.chmod(0o444)
        settings = Settings(
            output=OutputConfig(
                tmp_dir=str(tmp_path), data_dir=str(parent / "saneless")
            ),
            profiles={"default": ProfileConfig()},
        )
        with pytest.raises(ConfigError, match="not writable"):
            validate_settings_dirs(settings)
        # Restore permissions for cleanup
        parent.chmod(0o755)


class TestDataDir:
    """OutputConfig.data_dir and its computed db_path / failed_dir properties."""

    def test_data_dir_default_is_under_local_state(self) -> None:
        """The default data_dir is a str ending in .local/state/saneless."""
        data_dir = OutputConfig().data_dir
        assert isinstance(data_dir, str)
        assert data_dir.endswith(".local/state/saneless")

    def test_data_dir_default_is_not_the_temp_dir(self) -> None:
        """The default data_dir is not tmp_dir and is not under the temp root."""
        config = OutputConfig()
        assert config.data_dir != config.tmp_dir
        assert not config.data_dir.startswith(tempfile.gettempdir())

    def test_db_path_is_saneless_db_under_data_dir(self) -> None:
        """db_path is <data_dir>/saneless.db as a Path."""
        config = OutputConfig(data_dir="/x")
        assert config.db_path == Path("/x/saneless.db")
        assert isinstance(config.db_path, Path)

    def test_failed_dir_is_failed_under_data_dir(self) -> None:
        """failed_dir is <data_dir>/failed as a Path."""
        config = OutputConfig(data_dir="/x")
        assert config.failed_dir == Path("/x/failed")
        assert isinstance(config.failed_dir, Path)

    def test_computed_paths_are_read_only(self) -> None:
        """db_path and failed_dir are properties with no setter, not fields."""
        for name in ("db_path", "failed_dir"):
            descriptor = OutputConfig.__dict__[name]
            assert isinstance(descriptor, property)
            assert descriptor.fset is None
            assert name not in OutputConfig.model_fields

    def test_computed_paths_do_no_filesystem_io(self, tmp_path: Path) -> None:
        """Reading db_path and failed_dir creates nothing on disk."""
        target = tmp_path / "state"
        config = OutputConfig(data_dir=str(target))
        assert config.db_path == target / "saneless.db"
        assert config.failed_dir == target / "failed"
        assert not target.exists()

    def test_data_dir_env_var_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SANELESS_OUTPUT__DATA_DIR overrides the default data_dir."""
        monkeypatch.chdir(tmp_path)
        override = tmp_path / "custom-state"
        monkeypatch.setenv("SANELESS_OUTPUT__DATA_DIR", str(override))
        settings = load_settings()
        assert settings.output.data_dir == str(override)
        assert settings.output.db_path == override / "saneless.db"
