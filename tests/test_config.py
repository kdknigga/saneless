"""Tests for configuration loading and validation."""

from __future__ import annotations

import logging
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import saneless.config as config_mod
from saneless.config import (
    DEFAULT_RESOLUTION,
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    Settings,
    load_settings,
    resolve_job_title,
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
from saneless.vocabulary import TITLE_MAX_LENGTH


class TestLoadSettingsFromToml:
    """Settings load correctly from a TOML file."""

    def test_load_settings_from_toml(self, sample_toml: Path) -> None:
        """TOML values are correctly loaded into Settings fields."""
        settings = load_settings(config_path=str(sample_toml))
        assert settings.scanner.host == "192.168.1.50"
        assert settings.paperless.url == "http://paperless:8000"
        expected_auth = "abc123"
        assert settings.paperless.token.get_secret_value() == expected_auth

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
        assert settings.paperless.token.get_secret_value() == expected_auth

    def test_nested_env_delimiter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Double underscore delimiter supports nested settings."""
        monkeypatch.setenv("SANELESS_OUTPUT__LOG_LEVEL", "DEBUG")
        settings = load_settings()
        assert settings.output.log_level == "DEBUG"


class TestDefaultProfile:
    """Default profile validation."""

    def test_default_profile_required(self, tmp_config_dir: Path) -> None:
        """Missing default profile raises a rendered ConfigError (D-10)."""
        toml_content = """\
[scanner]
host = "192.168.1.50"

[profiles.custom]
source = "ADF"
"""
        config_file = tmp_config_dir / "no_default.toml"
        config_file.write_text(toml_content)
        with pytest.raises(ConfigError, match="default") as exc_info:
            load_settings(config_path=str(config_file))
        assert "[profiles]: " in str(exc_info.value)

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


class TestLoadedConfigPath:
    """
    Settings record the config file that was actually loaded (D-16, M-04).

    The worker used to re-derive a write target that ignored ``--config``; the
    loaded path now travels with ``Settings`` so every consumer writes to the
    file the operator's settings came from.
    """

    @pytest.fixture
    def empty_cwd_and_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Path:
        """
        Run in an empty CWD with HOME redirected into tmp_path.

        A developer's real ``~/.config/saneless/config.toml`` must not leak in.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        return tmp_path

    def test_explicit_path_is_recorded(self, tmp_path: Path) -> None:
        """An explicit config path that exists is recorded as given (D-16)."""
        config_file = tmp_path / "x.toml"
        config_file.write_text("[profiles.default]\n")
        settings = load_settings(str(config_file))
        assert settings.config_path == config_file

    def test_missing_config_explicit_path_is_an_error(self, tmp_path: Path) -> None:
        """An explicit path that does not exist is a ConfigError naming it (CFG-02)."""
        missing = str(tmp_path / "absent.toml")
        with pytest.raises(ConfigError, match=r"absent\.toml") as exc_info:
            load_settings(missing)
        assert missing in str(exc_info.value)

    def test_found_search_path_is_recorded(self, empty_cwd_and_home: Path) -> None:
        """The relative search entry that was found is recorded (D-16, M-04)."""
        (empty_cwd_and_home / "saneless.toml").write_text("[profiles.default]\n")
        settings = load_settings()
        assert settings.config_path == Path("saneless.toml")

    def test_home_search_path_is_recorded(self, empty_cwd_and_home: Path) -> None:
        """A config found under the redirected HOME is recorded (D-16)."""
        home_config = empty_cwd_and_home / "home" / ".config" / "saneless"
        home_config.mkdir(parents=True)
        (home_config / "config.toml").write_text("[profiles.default]\n")
        settings = load_settings()
        assert settings.config_path == home_config / "config.toml"

    def test_no_file_found_records_none(self, empty_cwd_and_home: Path) -> None:
        """With no config file anywhere, config_path is None (D-16)."""
        settings = load_settings()
        assert settings.config_path is None

    def test_env_var_cannot_set_config_path(
        self, empty_cwd_and_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        SANELESS_CONFIG_PATH cannot forge the loaded path (D-16, T-26-05).

        A public field would be populated from this variable and redirect
        profile writes; the private attribute is not.
        """
        evil = empty_cwd_and_home / "evil.toml"
        evil.write_text("[profiles.default]\n")
        monkeypatch.setenv("SANELESS_CONFIG_PATH", str(evil))
        settings = load_settings()
        assert settings.config_path is None

    def test_toml_config_path_key_is_rejected(self, tmp_path: Path) -> None:
        """A top-level TOML ``config_path`` key is an unknown section (D-16)."""
        config_file = tmp_path / "forged.toml"
        config_file.write_text('config_path = "x"\n\n[profiles.default]\n')
        with pytest.raises(ConfigError, match="config_path"):
            load_settings(str(config_file))

    def test_directly_constructed_settings_have_no_config_path(self) -> None:
        """``Settings()`` built directly was loaded from no file (D-16)."""
        assert Settings().config_path is None

    def test_config_search_paths_order(self) -> None:
        """The single search list holds the three locations in order (D-16)."""
        assert config_mod.config_search_paths() == (
            Path("./saneless.toml"),
            Path.home() / ".config" / "saneless" / "config.toml",
            Path("/etc/saneless/config.toml"),
        )

    def test_config_search_paths_reads_home_at_call_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A HOME change after import is honoured by the search list (D-16)."""
        monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
        expected = tmp_path / "elsewhere" / ".config" / "saneless" / "config.toml"
        assert config_mod.config_search_paths()[1] == expected


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


class TestSecretToken:
    """
    The Paperless token is a ``SecretStr`` (CFG-05, N-15).

    A settings object is formatted in reprs, tracebacks and dumps; none of
    those may carry the token. Only the ``PaperlessClient`` construction sites
    unwrap it.
    """

    def test_secret_token_absent_from_repr(self) -> None:
        """``repr(settings)`` masks the token."""
        secret = "tok-SECRET-4b1d"
        settings = Settings(paperless=PaperlessConfig(token=secret))
        assert secret not in repr(settings)

    def test_secret_token_absent_from_json_dump(self) -> None:
        """``model_dump(mode="json")`` masks the token."""
        secret = "tok-SECRET-4b1d"
        settings = Settings(paperless=PaperlessConfig(token=secret))
        assert secret not in str(settings.model_dump(mode="json"))

    def test_secret_token_unwraps_to_the_value(self) -> None:
        """``get_secret_value()`` still returns the configured token."""
        secret = "tok-SECRET-4b1d"
        settings = Settings(paperless=PaperlessConfig(token=secret))
        assert settings.paperless.token.get_secret_value() == secret

    def test_secret_token_default_is_empty(self) -> None:
        """An unconfigured token unwraps to the empty string."""
        assert PaperlessConfig().token.get_secret_value() == ""


class TestLogLevelValidation:
    """
    ``output.log_level`` accepts only the five standard names (CFG-04, M-21).

    ``getattr(logging, name)`` used to accept garbage and crash on ``TRACE``;
    the value is now validated at load, case-insensitively, with ``warn`` read
    as ``WARNING``.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("warn", "WARNING"),
            (" debug ", "DEBUG"),
            ("critical", "CRITICAL"),
            ("Error", "ERROR"),
            ("INFO", "INFO"),
        ],
    )
    def test_log_level_is_normalised(self, raw: str, expected: str) -> None:
        """Names are trimmed and upper-cased; ``warn`` becomes ``WARNING``."""
        output = OutputConfig.model_validate({"log_level": raw})
        assert output.log_level == expected

    def test_log_level_unknown_name_is_rejected(self) -> None:
        """``TRACE`` fails validation with a ``literal_error`` on log_level."""
        with pytest.raises(ValidationError) as exc_info:
            OutputConfig.model_validate({"log_level": "TRACE"})
        errors = exc_info.value.errors()
        assert [(e["type"], e["loc"]) for e in errors] == [
            ("literal_error", ("log_level",))
        ]

    def test_log_level_non_string_is_rejected(self) -> None:
        """A numeric level is not silently accepted."""
        with pytest.raises(ValidationError, match="log_level"):
            OutputConfig.model_validate({"log_level": 10})

    def test_log_level_default_is_info(self) -> None:
        """The default level is INFO."""
        assert OutputConfig().log_level == "INFO"

    def test_log_level_env_is_validated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An invalid ``SANELESS_OUTPUT__LOG_LEVEL`` fails at load (D-10)."""
        monkeypatch.setenv("SANELESS_OUTPUT__LOG_LEVEL", "TRACE")
        with pytest.raises(ConfigError, match="log_level"):
            load_settings()


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
        """The 'title' field in [profiles.default] maps to default_title (D-15)."""
        toml_content = '[profiles.default]\ntitle = "My Doc"\n'
        config_file = tmp_config_dir / "title_alias.toml"
        config_file.write_text(toml_content)
        settings = load_settings(config_path=str(config_file))
        assert settings.profiles["default"].default_title == "My Doc"

    def test_title_env_var_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Env var SANELESS_PROFILES__DEFAULT__TITLE sets default_title (D-15)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SANELESS_PROFILES__DEFAULT__TITLE", "EnvTitle")
        settings = load_settings()
        assert settings.profiles["default"].default_title == "EnvTitle"

    def test_title_over_max_length_is_rejected(self) -> None:
        """
        A profile title longer than TITLE_MAX_LENGTH fails validation (D-15).

        The route's form bound only checks a typed title, so an unbounded
        profile title would bypass ROBU-08.
        """
        with pytest.raises(ValidationError, match="title"):
            ProfileConfig(title="x" * (TITLE_MAX_LENGTH + 1))

    def test_title_at_max_length_is_accepted(self) -> None:
        """A profile title of exactly TITLE_MAX_LENGTH characters loads."""
        title = "x" * TITLE_MAX_LENGTH
        assert ProfileConfig(title=title).default_title == title


def _load_error(config_file: Path, toml_content: str) -> ConfigError:
    """
    Write ``toml_content`` to ``config_file`` and return the load's ConfigError.

    Args:
        config_file: Where to write the TOML.
        toml_content: The TOML text to load.

    Returns:
        The ConfigError ``load_settings`` raised.

    """
    config_file.write_text(toml_content)
    with pytest.raises(ConfigError) as exc_info:
        load_settings(config_path=str(config_file))
    return exc_info.value


def _error_lines(err: ConfigError) -> list[str]:
    """Return the rendered message split into lines, header first."""
    return str(err).split("\n")


class TestUnknownKeyRendering:
    """
    Every validation error is rendered by its full ``loc`` (D-10, D-11, CFG-01).

    Nested models used to ignore unknown keys, so ``[paperless] tokne`` left the
    token unset without a word (M-18). Each error is now one line under a
    header naming the file, with a close-match suggestion and the valid keys.
    """

    def test_unknown_key_in_paperless_suggests_and_lists_valid_keys(
        self, tmp_config_dir: Path
    ) -> None:
        """A typo under [paperless] names the key, the fix and the valid keys."""
        config_file = tmp_config_dir / "tokne.toml"
        err = _load_error(config_file, '[paperless]\ntokne = "x"\n')
        lines = _error_lines(err)
        assert lines[0] == f"Configuration error in {config_file}:"
        assert (
            "  [paperless] unknown key 'tokne' (did you mean 'token'?); "
            "valid keys: url, token, consume_dir"
        ) in lines

    def test_profile_unknown_key_suggests_and_shows_title_alias(
        self, tmp_config_dir: Path
    ) -> None:
        """A profile typo lists ``title`` (the alias), not ``default_title``."""
        err = _load_error(
            tmp_config_dir / "resoluton.toml",
            "[profiles.default]\nresoluton = 600\n",
        )
        matching = [
            line
            for line in _error_lines(err)
            if line.startswith(
                "  [profiles.default] unknown key 'resoluton' "
                "(did you mean 'resolution'?); valid keys: "
            )
        ]
        assert len(matching) == 1
        valid = matching[0].split("valid keys: ", 1)[1].split(", ")
        assert "title" in valid
        assert "default_title" not in valid
        assert "resolution" in valid

    @pytest.mark.parametrize(
        ("toml_content", "name"),
        [
            ('[paperless]\n"tok\\nne" = 1\n', "tok\\nne"),
            ('"bad\\nsection" = 1\n', "bad\\nsection"),
        ],
        ids=["section-key", "top-level-name"],
    )
    def test_unknown_key_with_control_character_is_escaped(
        self, tmp_config_dir: Path, toml_content: str, name: str
    ) -> None:
        """A quoted key holding a newline is rendered escaped (T-27-11)."""
        err = _load_error(tmp_config_dir / "control.toml", toml_content)
        message = str(err)
        assert name in message
        assert name.replace("\\n", "\n") not in message

    def test_wrong_section_key_names_owning_section(self, tmp_config_dir: Path) -> None:
        """A key that belongs to another section says where it belongs (D-11)."""
        err = _load_error(
            tmp_config_dir / "misplaced.toml",
            "[paperless]\nweb_port = 9\n\n[profiles.default]\n",
        )
        assert "  [paperless] unknown key 'web_port'; it belongs in [output]" in (
            _error_lines(err)
        )

    def test_wrong_section_top_level_key_names_owning_section(
        self, tmp_config_dir: Path
    ) -> None:
        """A section key written at the top level says where it belongs (D-11)."""
        err = _load_error(
            tmp_config_dir / "top_level_key.toml",
            "web_port = 9\n\n[profiles.default]\n",
        )
        matching = [
            line
            for line in _error_lines(err)
            if "unknown key 'web_port'" in line
            and line.endswith("it belongs in [output]")
        ]
        assert len(matching) == 1

    def test_wrong_section_capitalised_suggests_real_section(
        self, tmp_config_dir: Path
    ) -> None:
        """``[Paperless]`` is a miscased section, not a profile (M-18, D-11)."""
        err = _load_error(
            tmp_config_dir / "capitalised.toml",
            '[Paperless]\nurl = "x"\n\n[profiles.default]\n',
        )
        message = str(err)
        assert "did you mean [paperless]" in message
        assert "profiles.Paperless" not in message

    def test_renders_every_error_one_per_line(self, tmp_config_dir: Path) -> None:
        """Unknown keys and type errors are all reported, one line each (D-10)."""
        err = _load_error(
            tmp_config_dir / "several.toml",
            """\
[paperless]
tokne = "x"

[output]
web_port = "abc"
log_level = "TRACE"

[profiles.default]
""",
        )
        lines = _error_lines(err)
        body = lines[1:]
        assert len(body) == 3
        assert all(line.startswith("  [") for line in body)
        assert any(
            line.startswith("  [paperless] unknown key 'tokne'") for line in body
        )
        assert any(
            line.startswith("  [output] web_port: Input should be a valid integer")
            for line in body
        )
        assert any(
            line.startswith("  [output] log_level: Input should be 'DEBUG', ")
            for line in body
        )

    def test_renders_every_error_with_list_index(self, tmp_config_dir: Path) -> None:
        """An integer ``loc`` element is rendered as a list index (D-10)."""
        err = _load_error(
            tmp_config_dir / "tags.toml",
            '[profiles.default]\ndefault_tags = ["x"]\n',
        )
        assert any(
            line.startswith("  [profiles.default] default_tags[0]: ")
            for line in _error_lines(err)
        )

    def test_renders_every_error_header_without_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no file loaded the header names defaults and environment (D-10)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("SANELESS_OUTPUT__WEB_PORT", "abc")
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        lines = _error_lines(exc_info.value)
        assert lines[0] == "Configuration error (defaults and environment):"
        assert len(lines) == 2


class TestConfigErrorsNeverEchoValues:
    """
    A config error never contains an input value (D-14, CFG-05).

    pydantic error dicts carry ``input``, and ``str(ValidationError)`` embeds
    it; for ``tokne = "..."`` that is the Paperless token. The chain is
    suppressed too, because a traceback prints ``__cause__``.
    """

    @staticmethod
    def _assert_value_absent(err: ConfigError, value: str) -> None:
        """Assert ``value`` is in neither the message, the repr, nor the chain."""
        assert value not in str(err)
        assert value not in repr(err)
        assert err.__cause__ is None
        assert err.__suppress_context__ is True

    def test_never_echoes_token_under_mistyped_key(self, tmp_config_dir: Path) -> None:
        """A token-shaped value under ``tokne`` is absent from the error."""
        secret = "tok-SECRET-7c2a"
        err = _load_error(
            tmp_config_dir / "secret_key.toml",
            f'[paperless]\ntokne = "{secret}"\n',
        )
        self._assert_value_absent(err, secret)

    def test_never_echoes_value_of_type_error(self, tmp_config_dir: Path) -> None:
        """A token-shaped value that fails a type check is absent from the error."""
        secret = "tok-SECRET-7c2a"
        err = _load_error(
            tmp_config_dir / "secret_type.toml",
            f'[output]\nweb_port = "{secret}"\n',
        )
        assert "web_port" in str(err)
        self._assert_value_absent(err, secret)


class TestExplicitConfigPath:
    """
    An explicit ``--config`` path must be a regular file (CFG-02, M-19).

    A missing path used to load defaults silently, and Docker creates a
    directory where a single-file bind mount's source is missing.
    """

    def test_missing_config_names_the_path(self) -> None:
        """A path that does not exist is a ConfigError naming it."""
        with pytest.raises(ConfigError, match=r"/nope/missing\.toml"):
            load_settings("/nope/missing.toml")

    def test_missing_config_expands_tilde(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The error names the ``~``-expanded path (CFG-02, CFG-03)."""
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        with pytest.raises(ConfigError) as exc_info:
            load_settings("~/cfg.toml")
        assert str(home / "cfg.toml") in str(exc_info.value)

    def test_not_a_file_directory_is_rejected(self, tmp_path: Path) -> None:
        """A directory passed as the config path is a ConfigError naming it."""
        directory = tmp_path / "config.toml"
        directory.mkdir()
        with pytest.raises(ConfigError) as exc_info:
            load_settings(str(directory))
        assert str(directory) in str(exc_info.value)

    def test_not_a_file_directory_is_skipped_by_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A directory named ``saneless.toml`` in the cwd is not "found"."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "saneless.toml").mkdir()
        settings = load_settings()
        assert settings.config_path is None


class TestResolveJobTitle:
    """
    One title rule for every front end (D-16, M-24).

    A typed title wins when it is non-blank after stripping; otherwise the
    profile's ``title``; otherwise ``Scan <UTC YYYY-MM-DD HH:MM>``. The
    documented ``title`` key used to do nothing.
    """

    _NOW = datetime(2026, 9, 15, 13, 5, tzinfo=UTC)

    def test_title_typed_wins(self) -> None:
        """A non-blank typed title is used over the profile's title."""
        profile = ProfileConfig(title="Receipt")
        assert resolve_job_title("Invoice", profile, now=self._NOW) == "Invoice"

    @pytest.mark.parametrize("typed", ["", "   ", None])
    def test_title_blank_typed_uses_profile_title(self, typed: str | None) -> None:
        """An empty, whitespace or missing typed title falls back to the profile."""
        profile = ProfileConfig(title="Receipt")
        assert resolve_job_title(typed, profile, now=self._NOW) == "Receipt"

    def test_title_without_profile_title_uses_timestamp(self) -> None:
        """A profile with no title falls through to the UTC timestamp."""
        title = resolve_job_title("", ProfileConfig(), now=self._NOW)
        assert title == "Scan 2026-09-15 13:05"

    def test_title_blank_profile_title_uses_timestamp(self) -> None:
        """A whitespace profile title counts as blank."""
        profile = ProfileConfig(title="  ")
        title = resolve_job_title(None, profile, now=self._NOW)
        assert title == "Scan 2026-09-15 13:05"

    def test_title_no_profile_uses_timestamp(self) -> None:
        """With no profile at all the timestamp is used."""
        assert resolve_job_title(None, None, now=self._NOW) == "Scan 2026-09-15 13:05"

    def test_title_timestamp_is_rendered_in_utc(self) -> None:
        """A non-UTC aware ``now`` renders as UTC (local time is APPL-12)."""
        plus_two = datetime(2026, 9, 15, 15, 5, tzinfo=timezone(timedelta(hours=2)))
        assert resolve_job_title("", None, now=plus_two) == "Scan 2026-09-15 13:05"

    def test_title_typed_is_returned_unstripped(self) -> None:
        """A non-blank typed title is returned as given, matching today."""
        typed = " Invoice "
        assert resolve_job_title(typed, None, now=self._NOW) == typed


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
        with pytest.raises(ConfigError, match="flip_timeout_seconds"):
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
