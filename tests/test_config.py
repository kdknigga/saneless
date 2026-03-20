"""Tests for configuration loading and validation."""

import os

import pytest

from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
    load_settings,
)
from saneless.exceptions import (
    ConfigError,
    PaperlessError,
    SanelessError,
    ScanError,
)


class TestLoadSettingsFromToml:
    """Settings load correctly from a TOML file."""

    def test_load_settings_from_toml(self, sample_toml):
        settings = load_settings(config_path=str(sample_toml))
        assert settings.scanner.host == "192.168.1.50"
        assert settings.paperless.url == "http://paperless:8000"
        assert settings.paperless.token == "abc123"

    def test_env_var_override(self, sample_toml, monkeypatch):
        monkeypatch.setenv("SANELESS_SCANNER__HOST", "10.0.0.1")
        settings = load_settings(config_path=str(sample_toml))
        assert settings.scanner.host == "10.0.0.1"

    def test_env_prefix(self, monkeypatch):
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", "envtoken")
        settings = load_settings()
        assert settings.paperless.token == "envtoken"

    def test_nested_env_delimiter(self, monkeypatch):
        monkeypatch.setenv("SANELESS_OUTPUT__LOG_LEVEL", "DEBUG")
        settings = load_settings()
        assert settings.output.log_level == "DEBUG"


class TestDefaultProfile:
    """Default profile validation."""

    def test_default_profile_required(self, tmp_config_dir):
        toml_content = """\
[scanner]
host = "192.168.1.50"

[profiles.custom]
source = "ADF"
"""
        config_file = tmp_config_dir / "no_default.toml"
        config_file.write_text(toml_content)
        with pytest.raises(Exception, match="default"):
            load_settings(config_path=str(config_file))

    def test_default_profile_present(self, sample_toml):
        settings = load_settings(config_path=str(sample_toml))
        assert "default" in settings.profiles


class TestProfileFields:
    """Profile configuration fields."""

    def test_profile_fields(self, tmp_config_dir):
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

    def test_config_file_search_explicit(self, sample_toml, tmp_config_dir):
        # Create a different file that would be found by search
        other_toml = tmp_config_dir / "other.toml"
        other_toml.write_text("[scanner]\nhost = 'other'\n\n[profiles.default]\n")
        settings = load_settings(config_path=str(sample_toml))
        assert settings.scanner.host == "192.168.1.50"

    def test_config_file_search_fallback(self, tmp_path, monkeypatch):
        # Put saneless.toml in current directory
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

    def test_no_config_file(self, tmp_path, monkeypatch):
        # Ensure no config files exist
        monkeypatch.chdir(tmp_path)
        settings = load_settings()
        assert settings.scanner.host == ""
        assert "default" in settings.profiles


class TestInvalidToml:
    """Invalid TOML handling."""

    def test_invalid_toml(self, tmp_config_dir):
        bad_file = tmp_config_dir / "bad.toml"
        bad_file.write_text("this is not [valid toml\n===broken===")
        with pytest.raises(Exception):
            load_settings(config_path=str(bad_file))


class TestSettingsDefaults:
    """Default values when no config or env vars."""

    def test_settings_defaults(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        settings = load_settings()
        assert settings.scanner.host == ""
        assert settings.paperless.url == ""
        assert settings.output.tmp_dir == "/tmp/saneless"
        assert settings.output.log_level == "INFO"


class TestExceptionHierarchy:
    """Custom exception hierarchy."""

    def test_exception_hierarchy(self):
        assert issubclass(SanelessError, Exception)
        assert issubclass(ConfigError, SanelessError)
        assert issubclass(ScanError, SanelessError)
        assert issubclass(PaperlessError, SanelessError)

    def test_exceptions_are_raisable(self):
        with pytest.raises(SanelessError):
            raise ConfigError("bad config")
        with pytest.raises(SanelessError):
            raise ScanError("scan failed")
        with pytest.raises(SanelessError):
            raise PaperlessError("api error")
