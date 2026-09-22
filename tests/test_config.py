"""Tests for configuration loading and validation."""

from __future__ import annotations

import errno
import logging
import os
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, TypedDict, Unpack

import pytest
from pydantic import ValidationError
from pydantic_settings.exceptions import SettingsError

import saneless.config as config_mod
from saneless.config import (
    DEFAULT_RESOLUTION,
    PLACEHOLDER_TOKENS,
    PROFILE_DESCRIPTION_MAX_LENGTH,
    PROFILE_LABEL_MAX_LENGTH,
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    Settings,
    WebConfig,
    is_placeholder_token,
    load_settings,
    profile_storage_for_loaded,
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
from saneless.vocabulary import TITLE_MAX_LENGTH, ProfileStorage, local_time

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from pydantic_core import ErrorDetails


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

    def test_home_search_path_is_recorded(
        self, empty_cwd_and_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config found under the redirected HOME is recorded (D-16)."""
        monkeypatch.delenv("XDG_CONFIG_HOME")
        home_config = empty_cwd_and_home / "home" / ".config" / "saneless"
        home_config.mkdir(parents=True)
        (home_config / "config.toml").write_text("[profiles.default]\n")
        settings = load_settings()
        assert settings.config_path == home_config / "config.toml"

    def test_no_file_found_records_none(self, empty_cwd_and_home: Path) -> None:
        """With no config file anywhere, config_path is None (D-16)."""
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
        """The search list is cwd, then the XDG config home, then /etc (D-16)."""
        assert config_mod.config_search_paths() == (
            Path("./saneless.toml"),
            config_mod.xdg_config_home() / "saneless" / "config.toml",
            Path("/etc/saneless/config.toml"),
        )

    def test_config_search_paths_reads_home_at_call_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A HOME change after import is honoured by the search list (D-16)."""
        monkeypatch.delenv("XDG_CONFIG_HOME")
        monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
        expected = tmp_path / "elsewhere" / ".config" / "saneless" / "config.toml"
        assert config_mod.config_search_paths()[1] == expected


@dataclass(frozen=True)
class _XdgBase:
    """One XDG base directory: its variable, helper, and HOME-relative fallback."""

    variable: str
    function: str
    fallback: tuple[str, ...]

    def resolve(self) -> Path:
        """
        Call the ``saneless.config`` helper for this base directory.

        Returns:
            The helper's result, read from the environment now.

        """
        helper: Callable[[], Path] = getattr(config_mod, self.function)
        return helper()


_XDG_BASES = [
    pytest.param(
        _XdgBase("XDG_CONFIG_HOME", "xdg_config_home", (".config",)), id="config"
    ),
    pytest.param(
        _XdgBase("XDG_STATE_HOME", "xdg_state_home", (".local", "state")), id="state"
    ),
]


class TestXdgBaseDirectories:
    """
    Config discovery and state defaults follow the XDG base directories (CFG-03).

    The docs promised XDG behaviour the code did not have (M-20, doc row 27).
    Per the basedir spec, an unset or empty ``$XDG_CONFIG_HOME`` /
    ``$XDG_STATE_HOME`` means ``$HOME/.config`` / ``$HOME/.local/state``, and a
    relative value is invalid and ignored. Both are read at call time, so a
    change after import is honoured.
    """

    @pytest.fixture
    def empty_cwd_and_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Path:
        """
        Run in an empty CWD with HOME redirected and both XDG variables unset.

        The suite's autouse ``hermetic_env`` points every XDG variable into a
        fake home; these tests are about what happens without them.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.delenv("XDG_CONFIG_HOME")
        monkeypatch.delenv("XDG_STATE_HOME")
        return tmp_path

    @pytest.mark.parametrize("base", _XDG_BASES)
    def test_xdg_unset_falls_back_under_home(
        self, empty_cwd_and_home: Path, base: _XdgBase
    ) -> None:
        """With the variable unset, the base is under the redirected HOME."""
        assert base.variable not in os.environ
        expected = (empty_cwd_and_home / "home").joinpath(*base.fallback)
        assert base.resolve() == expected

    @pytest.mark.parametrize("base", _XDG_BASES)
    def test_xdg_absolute_value_is_used(
        self,
        empty_cwd_and_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        base: _XdgBase,
    ) -> None:
        """An absolute value is the base directory itself."""
        target = empty_cwd_and_home / "xdg"
        monkeypatch.setenv(base.variable, str(target))
        assert base.resolve() == target

    @pytest.mark.parametrize("value", ["", "relative/dir"], ids=["empty", "relative"])
    @pytest.mark.parametrize("base", _XDG_BASES)
    def test_xdg_empty_or_relative_value_is_ignored(
        self,
        empty_cwd_and_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        base: _XdgBase,
        value: str,
    ) -> None:
        """An empty or relative value falls back, per the basedir spec (T-27-25)."""
        monkeypatch.setenv(base.variable, value)
        expected = (empty_cwd_and_home / "home").joinpath(*base.fallback)
        assert base.resolve() == expected

    def test_xdg_config_home_is_the_second_search_path(
        self, empty_cwd_and_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``$XDG_CONFIG_HOME/saneless/config.toml`` is searched second."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(empty_cwd_and_home / "xdg"))
        expected = empty_cwd_and_home / "xdg" / "saneless" / "config.toml"
        assert config_mod.config_search_paths()[1] == expected

    def test_config_under_xdg_config_home_is_loaded(
        self, empty_cwd_and_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config under ``$XDG_CONFIG_HOME`` is found and recorded (CFG-03)."""
        xdg_dir = empty_cwd_and_home / "xdg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_dir))
        config_dir = xdg_dir / "saneless"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text(
            '[scanner]\nhost = "from-xdg"\n\n[profiles.default]\n'
        )
        settings = load_settings()
        assert settings.scanner.host == "from-xdg"
        assert settings.config_path == config_dir / "config.toml"

    def test_xdg_state_home_set_after_import_moves_state_defaults(
        self, empty_cwd_and_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The state defaults are computed at instantiation, not import (Pitfall 3).

        ``saneless.config`` was imported long before this test set the variable;
        a ``Settings.output`` default built at import would still point at the
        old location.
        """
        state = empty_cwd_and_home / "state"
        monkeypatch.setenv("XDG_STATE_HOME", str(state))
        settings = Settings()
        assert settings.output.data_dir == state / "saneless"
        assert settings.output.log_file == state / "saneless" / "saneless.log"
        assert OutputConfig().data_dir == state / "saneless"
        assert OutputConfig().log_file == state / "saneless" / "saneless.log"

    def test_xdg_unset_home_change_after_import_moves_state_defaults(
        self, empty_cwd_and_home: Path
    ) -> None:
        """With XDG unset, both state defaults follow a HOME changed after import."""
        state = empty_cwd_and_home / "home" / ".local" / "state" / "saneless"
        output = Settings().output
        assert output.data_dir == state
        assert output.log_file == state / "saneless.log"


class _OpenOptions(TypedDict, total=False):
    """The keyword arguments ``Path.open`` takes after its mode."""

    buffering: int
    encoding: str | None
    errors: str | None
    newline: str | None


def _refuse_opening(monkeypatch: pytest.MonkeyPatch, refused: Path) -> None:
    """
    Make opening ``refused`` fail with EACCES; every other open still works.

    Injecting the failure rather than taking the file's permissions away keeps
    the test meaningful as root, whom file modes do not stop.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        refused: The file whose opening must be refused.

    """
    real_open = Path.open

    def refusing(
        self: Path, mode: str = "r", **options: Unpack[_OpenOptions]
    ) -> IO[Any]:
        if self == refused:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(self))
        return real_open(self, mode, **options)

    monkeypatch.setattr(Path, "open", refusing)


class TestInvalidToml:
    """
    A config file that cannot be read or parsed is a ConfigError (D-12, EXC-01).

    ``tomllib.TOMLDecodeError``, ``UnicodeDecodeError`` and ``OSError`` used to
    escape ``load_settings`` raw, so the CLI's generic catch reported a bad file
    as an unexpected failure (M-17). Each now becomes one line under the Phase
    27 header, chained to its cause, and never carries file content: the
    decode error's ``doc`` holds the whole file, token included (T-28-05).
    """

    @staticmethod
    def _assert_token_absent(err: ConfigError, token: str) -> None:
        """
        Assert ``token`` is absent from the message, the repr and the cause.

        Only the cause's ``str`` is checked, because that is what a traceback
        prints for a chained exception. ``repr(UnicodeDecodeError)`` includes
        its ``object`` bytes, so it is never rendered by the loader.
        """
        assert token not in str(err)
        assert token not in repr(err)
        assert err.__cause__ is not None
        assert token not in str(err.__cause__)

    def test_invalid_toml(self, tmp_config_dir: Path) -> None:
        """Malformed TOML raises ConfigError, not a raw ValueError (D-12)."""
        bad_file = tmp_config_dir / "bad.toml"
        bad_file.write_text("this is not [valid toml\n===broken===")
        with pytest.raises(ConfigError):
            load_settings(config_path=str(bad_file))

    def test_toml_syntax_error_names_file_line_and_column(
        self, tmp_config_dir: Path
    ) -> None:
        """A syntax error is one ``line N, column M`` row under the header (D-12)."""
        bad_file = tmp_config_dir / "syntax.toml"
        err = _load_error(bad_file, "a = = 1\n")
        lines = str(err).splitlines()
        assert lines[0] == f"Configuration error in {bad_file}:"
        assert lines[1].startswith("  line 1, column 5: ")
        assert "Invalid value" in lines[1]
        assert isinstance(err.__cause__, tomllib.TOMLDecodeError)

    def test_toml_syntax_error_never_echoes_token(self, tmp_config_dir: Path) -> None:
        """A token earlier in a broken file is absent from the error (T-28-05)."""
        token = "tok-SECRET-91fe"
        err = _load_error(
            tmp_config_dir / "secret_syntax.toml",
            f'[paperless]\ntoken = "{token}"\nurl = = 1\n',
        )
        assert "line 3, column 7" in str(err)
        self._assert_token_absent(err, token)

    def test_toml_key_with_control_character_is_escaped(
        self, tmp_config_dir: Path
    ) -> None:
        """A key path holding an escape character is never rendered raw (T-28-06)."""
        err = _load_error(
            tmp_config_dir / "control_syntax.toml",
            '["s\\u001b"]\n["s\\u001b"]\n',
        )
        message = str(err)
        assert "\x1b" not in message
        assert "\\x1b" in message
        assert isinstance(err.__cause__, tomllib.TOMLDecodeError)

    def test_non_utf8_config_file_is_config_error(self, tmp_config_dir: Path) -> None:
        """A file that is not UTF-8 is one ``not valid UTF-8`` row (D-12)."""
        bad_file = tmp_config_dir / "latin1.toml"
        bad_file.write_bytes(b'title = "\xff"\n')
        with pytest.raises(ConfigError) as exc_info:
            load_settings(config_path=str(bad_file))
        lines = str(exc_info.value).splitlines()
        assert lines[0] == f"Configuration error in {bad_file}:"
        assert any("not valid UTF-8" in line for line in lines[1:])
        assert isinstance(exc_info.value.__cause__, UnicodeDecodeError)

    def test_non_utf8_config_file_never_echoes_token(
        self, tmp_config_dir: Path
    ) -> None:
        """A token beside the undecodable byte is absent from the error (T-28-05)."""
        token = "tok-SECRET-91fe"
        bad_file = tmp_config_dir / "secret_latin1.toml"
        bad_file.write_bytes(f'[paperless]\ntoken = "{token}"\n'.encode() + b"\xff\n")
        with pytest.raises(ConfigError) as exc_info:
            load_settings(config_path=str(bad_file))
        self._assert_token_absent(exc_info.value, token)

    def test_unreadable_config_file_is_config_error(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file that cannot be opened is one ``cannot read the file`` row (D-12)."""
        token = "tok-SECRET-91fe"
        bad_file = tmp_config_dir / "unreadable.toml"
        bad_file.write_text(f'[paperless]\ntoken = "{token}"\n')
        _refuse_opening(monkeypatch, bad_file)
        with pytest.raises(ConfigError) as exc_info:
            load_settings(config_path=str(bad_file))
        lines = str(exc_info.value).splitlines()
        assert lines[0] == f"Configuration error in {bad_file}:"
        assert lines[1].startswith("  cannot read the file:")
        assert "Permission denied" in lines[1]
        assert isinstance(exc_info.value.__cause__, PermissionError)
        self._assert_token_absent(exc_info.value, token)


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
        assert settings.output.tmp_dir == Path(tempfile.gettempdir()) / "saneless"
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


class TestTomlKeyCaseContract:
    """
    TOML section and key names are matched byte-exactly (DEP-02, DEP-03, D-06).

    pydantic-settings 2.15.0 made its file sources honour ``case_sensitive``,
    which defaults to False, so a miscased top-level ``[Scanner]`` would bind
    into ``scanner`` instead of being reported and the Phase 27 strictness
    contract would weaken without a word. Nested keys were never folded; they
    are pinned here so a later flip upstream cannot pass unnoticed.
    """

    def test_miscased_section_is_reported_not_bound(self, tmp_config_dir: Path) -> None:
        """``[Scanner]`` is an unknown section, never a bound ``scanner`` (D-06)."""
        err = _load_error(
            tmp_config_dir / "miscased_section.toml",
            '[Scanner]\nhost = "x"\n\n[profiles.default]\n',
        )
        matching = [
            line
            for line in _error_lines(err)
            if line.startswith("  unknown section 'Scanner'")
        ]
        assert len(matching) == 1

    def test_miscased_nested_key_stays_unknown(self, tmp_config_dir: Path) -> None:
        """``[paperless] Token`` is still matched case-sensitively (DEP-03)."""
        err = _load_error(
            tmp_config_dir / "miscased_token.toml",
            '[paperless]\nToken = "x"\n\n[profiles.default]\n',
        )
        matching = [
            line
            for line in _error_lines(err)
            if line.startswith("  [paperless] unknown key 'Token'")
        ]
        assert len(matching) == 1

    def test_miscased_nested_key_with_underscore_stays_unknown(
        self, tmp_config_dir: Path
    ) -> None:
        """``[output] Web_Port`` is still matched case-sensitively (DEP-03)."""
        err = _load_error(
            tmp_config_dir / "miscased_web_port.toml",
            "[output]\nWeb_Port = 9\n\n[profiles.default]\n",
        )
        matching = [
            line
            for line in _error_lines(err)
            if line.startswith("  [output] unknown key 'Web_Port'")
        ]
        assert len(matching) == 1

    def test_miscased_section_still_suggests_the_real_section(
        self, tmp_config_dir: Path
    ) -> None:
        """The ``[Paperless]`` close-match hint survives the upgrade (D-05)."""
        err = _load_error(
            tmp_config_dir / "miscased_hint.toml",
            '[Paperless]\nurl = "x"\n\n[profiles.default]\n',
        )
        message = str(err)
        assert "did you mean [paperless]" in message
        assert "profiles.Paperless" not in message

    def test_correctly_cased_section_still_loads(self, tmp_config_dir: Path) -> None:
        """
        Positive control: the correct spelling still binds (D-06).

        Without it this class could pass because every section errors.
        """
        config_file = tmp_config_dir / "correctly_cased.toml"
        config_file.write_text('[scanner]\nhost = "x"\n\n[profiles.default]\n')
        settings = load_settings(config_path=str(config_file))
        assert settings.scanner.host == "x"

    def test_capitalised_profile_name_is_a_distinct_profile(
        self, tmp_config_dir: Path
    ) -> None:
        """``[profiles.Default]`` is not the default profile (D-08)."""
        err = _load_error(
            tmp_config_dir / "miscased_profile.toml",
            '[profiles.Default]\nsource = "ADF"\n',
        )
        assert "A 'default' profile must be defined in config" in str(err)


_HOSTILE_SECRET = "tok-SECRET-4f1c9ba27e5d8031-DISTINCTIVE"
"""A distinctive token stand-in for the hostile-upstream-message guards."""

_HOSTILE_MESSAGE = f"Input should be a valid string (got {_HOSTILE_SECRET})"
"""An upstream ``msg`` that interpolates the input, which pydantic's does not."""

_REDACTED_MESSAGE = "Input should be a valid string (got <value omitted>)"
"""``_HOSTILE_MESSAGE`` as it must appear once the input has been struck out."""


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

    @pytest.mark.usefixtures("no_discovered_config")
    def test_never_echoes_env_token_under_mistyped_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A token-shaped value in ``SANELESS_PAPERLESS__TOKNE`` is absent."""
        secret = "tok-SECRET-9f8e"
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKNE", secret)
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        assert "SANELESS_PAPERLESS__TOKNE" in str(exc_info.value)
        self._assert_value_absent(exc_info.value, secret)

    @pytest.mark.usefixtures("no_discovered_config")
    def test_never_echoes_invalid_json_env_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Invalid JSON for a whole section is a ConfigError, not a SettingsError.

        pydantic-settings raises ``SettingsError`` before validation (Pitfall
        2); its message names the field and source, never the value.
        """
        value = "notjson"
        monkeypatch.setenv("SANELESS_PAPERLESS", value)
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        assert "paperless" in str(exc_info.value)
        self._assert_value_absent(exc_info.value, value)

    def test_never_echoes_token_whatever_wording_upstream_carries(
        self, tmp_config_dir: Path
    ) -> None:
        """
        A token reaching the render boundary as an error's input is redacted.

        pydantic's ``msg`` carries no input today, so the load is the standing
        case and the direct render call is the guard: an upstream wording
        change that started interpolating the input must still not put the
        Paperless token in a user-visible line (D-07, CFG-05).
        """
        secret = "tok-SECRET-4f1c9ba27e5d8031-DISTINCTIVE"
        err = _load_error(
            tmp_config_dir / "long_token.toml",
            f'[paperless]\ntokne = "{secret}"\n\n[profiles.default]\n',
        )
        self._assert_value_absent(err, secret)
        hostile: ErrorDetails = {
            "type": "string_type",
            "loc": ("paperless", "token"),
            "msg": f"Input should be a valid string (got {secret})",
            "input": secret,
        }
        lines = config_mod._render_error_lines([hostile], {})
        assert len(lines) == 1
        assert secret not in lines[0]

    def test_redaction_leaves_the_product_contract_lines_alone(
        self, tmp_config_dir: Path
    ) -> None:
        """
        Redacting inputs keeps the one-line-per-error shape (D-07, D-10).

        The mistyped key now carries ``"url"``, a real ``PaperlessConfig``
        field name, so the fixture exercises the case where redaction can
        reach this project's own vocabulary. The body must therefore be free
        of the redaction marker: none of these three errors involves an
        upstream message that quotes its input, so any marker in the body is
        redaction eating a field name rather than a value.
        """
        err = _load_error(
            tmp_config_dir / "several_redacted.toml",
            """\
[paperless]
tokne = "url"

[output]
web_port = "abc"
log_level = "TRACE"

[profiles.default]
""",
        )
        body = _error_lines(err)[1:]
        assert len(body) == 3
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
        assert [line for line in body if "<value omitted>" in line] == []
        assert (
            "  [paperless] unknown key 'tokne' (did you mean 'token'?); "
            "valid keys: url, token, consume_dir"
        ) in body

    def test_redaction_leaves_a_hint_that_collides_with_an_input_alone(
        self, tmp_config_dir: Path
    ) -> None:
        """
        A value equal to a field name is not struck out of the hint.

        ``hst = "host"`` misspells a key whose value happens to be another of
        the section's field names. The did-you-mean hint and the valid-keys
        list are built from this project's own field names, so neither may
        lose a word to redaction; the whole line is pinned because the damage
        lands in its tail.
        """
        err = _load_error(
            tmp_config_dir / "collision.toml",
            '[scanner]\nhst = "host"\n\n[profiles.default]\n',
        )
        assert _error_lines(err)[1:] == [
            "  [scanner] unknown key 'hst' (did you mean 'host'?); "
            "valid keys: host, device"
        ]

    @pytest.mark.parametrize(
        ("loc", "error_type", "expected"),
        [
            pytest.param(
                ("paperless", "token"),
                "string_type",
                f"[paperless] token: {_REDACTED_MESSAGE}",
                id="section-key",
            ),
            pytest.param(
                ("paperless",),
                "model_type",
                f"[paperless]: {_REDACTED_MESSAGE}",
                id="whole-section",
            ),
            pytest.param(
                ("nosuchsection", "x"),
                "string_type",
                f"nosuchsection.x: {_REDACTED_MESSAGE}",
                id="unknown-loc-path",
            ),
            pytest.param(
                ("profiles", "default", "source"),
                "string_type",
                f"[profiles.default] source: {_REDACTED_MESSAGE}",
                id="profile-key",
            ),
        ],
    )
    def test_every_rendered_branch_redacts_a_hostile_upstream_message(
        self, loc: tuple[str | int, ...], error_type: str, expected: str
    ) -> None:
        """
        Every branch that carries pydantic's wording strikes the input out.

        The narrowed redaction boundary must not weaken the guarantee: each
        line that interpolates an upstream ``msg`` still loses the input's
        text, whatever wording upstream arrives with (D-07, CFG-05).
        """
        hostile: ErrorDetails = {
            "type": error_type,
            "loc": loc,
            "msg": _HOSTILE_MESSAGE,
            "input": _HOSTILE_SECRET,
        }
        lines = config_mod._render_error_lines([hostile], {})
        assert len(lines) == 1
        assert _HOSTILE_SECRET not in lines[0]
        assert lines[0] == expected

    def test_a_short_value_inside_upstream_prose_leaves_the_prose_intact(
        self, tmp_config_dir: Path
    ) -> None:
        """
        Striking the input out must not shred pydantic's own words (CR-01).

        ``web_port = "in"`` is a two-character typo, and ``in`` occurs inside
        ``integer`` and ``string`` in pydantic's message. An unanchored
        replacement turns the explanation into ``a valid <value omitted>teger``
        -- the operator loses the one sentence that says what was wanted. The
        input is not a word here, so nothing needs striking at all.
        """
        err = _load_error(
            tmp_config_dir / "short_value.toml",
            '[output]\nweb_port = "in"\n',
        )
        [line] = [ln for ln in _error_lines(err) if "web_port" in ln]
        assert "<value omitted>" not in line
        assert line.strip() == (
            "[output] web_port: Input should be a valid integer, "
            "unable to parse string as an integer"
        )

    def test_a_value_that_cannot_be_struck_cleanly_withholds_the_message(
        self,
    ) -> None:
        """
        A credential glued into upstream prose withholds it whole (CR-01).

        Word-anchored striking cannot reach a value upstream joined to its
        own words with no separator, and a value this long cannot be there by
        coincidence -- pydantic's longest word is ``integer``. Splicing would
        corrupt the prose and leaving it would echo the input, so the message
        is dropped entirely: not echoing outranks explaining. The section and
        the key are this module's own words and survive.
        """
        hostile: ErrorDetails = {
            "type": "string_type",
            "loc": ("paperless", "token"),
            "msg": f"Input should be valid{_HOSTILE_SECRET}string",
            "input": _HOSTILE_SECRET,
        }
        lines = config_mod._render_error_lines([hostile], {})
        assert len(lines) == 1
        assert _HOSTILE_SECRET not in lines[0]
        assert lines[0] == "[paperless] token: <value omitted>"

    def test_a_short_midword_value_is_left_alone(self, tmp_config_dir: Path) -> None:
        """
        A short value buried in a word is coincidence, not an echo (CR-01).

        ``eger`` occurs only inside ``integer``. No reader recovers the input
        from that, so withholding the message would cost the explanation and
        hide nothing -- the boundary between this and the case above is
        length, not position.
        """
        err = _load_error(
            tmp_config_dir / "midword_value.toml",
            '[output]\nweb_port = "eger"\n',
        )
        [line] = [ln for ln in _error_lines(err) if "web_port" in ln]
        assert "<value omitted>" not in line
        assert line.strip() == (
            "[output] web_port: Input should be a valid integer, "
            "unable to parse string as an integer"
        )

    def test_a_nested_input_value_is_struck_from_the_message(self) -> None:
        """
        A secret inside a non-string input is struck out too (WR-01).

        ``SANELESS_PAPERLESS__TOKEN__X=<token>`` makes pydantic's ``input`` a
        mapping, not a string. D-07 promises the line is incapable of carrying
        the input "whatever upstream wording arrives", so a bare
        ``isinstance(value, str)`` test leaves that promise unkept.
        """
        hostile: ErrorDetails = {
            "type": "string_type",
            "loc": ("paperless", "token"),
            "msg": _HOSTILE_MESSAGE,
            "input": {"x": _HOSTILE_SECRET},
        }
        lines = config_mod._render_error_lines([hostile], {})
        assert len(lines) == 1
        assert _HOSTILE_SECRET not in lines[0]

    def test_the_environment_branch_strikes_values_from_upstream_wording(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The SettingsError branch redacts too, whatever wording arrives (WR-02).

        ``_env_contribution`` raises ``SettingsError`` when a complex-typed
        variable will not parse, and its text goes straight into the rendered
        body. pydantic-settings names only the field and source today, but
        that wording is upstream-owned -- which is the exact dependency D-07
        exists to remove. The raise is forced here so the guard tests this
        module's behaviour rather than upstream's current phrasing.
        """
        malformed = f'{{"token": "{_HOSTILE_SECRET}"'
        monkeypatch.setenv("SANELESS_PAPERLESS", malformed)

        def hostile_env_contribution() -> dict[str, object]:
            msg = f"error parsing value {malformed} for field 'paperless'"
            raise SettingsError(msg)

        monkeypatch.setattr(config_mod, "_env_contribution", hostile_env_contribution)
        err = _load_error(
            tmp_config_dir / "env_settings_error.toml",
            '[paperless]\nurl = "http://example.invalid"\n',
        )
        assert _HOSTILE_SECRET not in str(err)

    def test_env_branch_redacts_a_hostile_upstream_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The environment-attributed branch strikes the input out too.

        This is the fifth render branch: it names the variable that supplied
        the value, so the value itself must still be gone (D-07, CFG-05).
        """
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", _HOSTILE_SECRET)
        hostile: ErrorDetails = {
            "type": "string_type",
            "loc": ("paperless", "token"),
            "msg": _HOSTILE_MESSAGE,
            "input": _HOSTILE_SECRET,
        }
        lines = config_mod._render_error_lines(
            [hostile], {"paperless": {"token": _HOSTILE_SECRET}}
        )
        assert len(lines) == 1
        assert _HOSTILE_SECRET not in lines[0]
        assert lines[0] == (
            "environment variable 'SANELESS_PAPERLESS__TOKEN': "
            f"token in [paperless]: {_REDACTED_MESSAGE}"
        )

    def test_a_lowercase_env_name_is_struck_out_like_the_shouted_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A lowercase ``saneless_*`` value is redacted too (PR #13 review).

        ``SettingsConfigDict`` leaves ``case_sensitive`` at its default, so
        pydantic-settings reads ``saneless_paperless__token`` exactly as it
        reads the shouted spelling -- this test asserts that first, so it
        fails if that default ever changes rather than quietly passing on a
        premise that stopped being true.

        The redaction filter used to collect only names beginning with the
        uppercase prefix, so a token that pydantic *had* read arrived at
        ``_redact_input`` as a value it was never told about and survived
        into the rendered line.
        """
        monkeypatch.delenv("SANELESS_PAPERLESS__TOKEN", raising=False)
        monkeypatch.setenv("saneless_paperless__token", _HOSTILE_SECRET)

        # The premise: pydantic reads the lowercase spelling.
        assert Settings().paperless.token.get_secret_value() == _HOSTILE_SECRET

        assert _HOSTILE_SECRET not in config_mod._redact_environment(
            f"upstream said {_HOSTILE_SECRET} here"
        )


@pytest.fixture
def no_discovered_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Run in an empty CWD with HOME redirected, so no config file is discovered.

    Returns:
        The temporary directory that is now the CWD.

    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def _env_line(err: ConfigError, variable: str) -> str:
    """
    Return the single rendered line attributed to ``variable``.

    Args:
        err: The load's ConfigError.
        variable: The environment variable name, as spelled.

    Returns:
        The one line starting with ``environment variable '<variable>': ``.

    """
    prefix = f"  environment variable {variable!r}: "
    matching = [line for line in _error_lines(err) if line.startswith(prefix)]
    assert len(matching) == 1, str(err)
    return matching[0]


class TestEnvironmentAttribution:
    """
    An error whose value came from a SANELESS_* variable names it (D-12).

    pydantic's ``loc`` is identical for TOML and environment input, so without
    attribution an env typo would be reported against a file that does not
    contain it.
    """

    @pytest.mark.usefixtures("no_discovered_config")
    def test_attributes_env_unknown_key_to_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown nested key set from the environment names the variable."""
        monkeypatch.setenv("SANELESS_SCANNER__HOSTNAME", "x")
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        assert _env_line(exc_info.value, "SANELESS_SCANNER__HOSTNAME") == (
            "  environment variable 'SANELESS_SCANNER__HOSTNAME': unknown key "
            "'hostname' in [scanner] (did you mean 'host'?); valid keys: host, device"
        )

    @pytest.mark.usefixtures("no_discovered_config")
    def test_attributes_env_as_spelled_in_lower_case(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lower-case variable is named exactly as it is spelled."""
        monkeypatch.setenv("saneless_scanner__hostname", "x")
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        assert "unknown key 'hostname' in [scanner]" in _env_line(
            exc_info.value, "saneless_scanner__hostname"
        )

    def test_attributes_env_type_error_over_loaded_file(
        self, sample_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a valid file loaded, an env type error names the variable."""
        monkeypatch.setenv("SANELESS_OUTPUT__WEB_PORT", "abc")
        with pytest.raises(ConfigError) as exc_info:
            load_settings(str(sample_toml))
        lines = _error_lines(exc_info.value)
        assert lines[0] == f"Configuration error in {sample_toml}:"
        line = _env_line(exc_info.value, "SANELESS_OUTPUT__WEB_PORT")
        assert "web_port" in line
        assert "[output]" in line
        assert not any(line.startswith("  [output]") for line in lines)

    def test_attributes_env_not_for_the_same_error_in_toml(
        self, tmp_config_dir: Path
    ) -> None:
        """The same type error written in the file names no variable."""
        err = _load_error(
            tmp_config_dir / "port.toml",
            '[output]\nweb_port = "abc"\n\n[profiles.default]\n',
        )
        assert "environment variable" not in str(err)
        assert any(
            line.startswith("  [output] web_port: ") for line in _error_lines(err)
        )

    def test_attributes_env_profile_key_to_variable(
        self, sample_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A profile typo from the environment names the variable and profile."""
        monkeypatch.setenv("SANELESS_PROFILES__RECEIPT__RESOLUTON", "1")
        with pytest.raises(ConfigError) as exc_info:
            load_settings(str(sample_toml))
        line = _env_line(exc_info.value, "SANELESS_PROFILES__RECEIPT__RESOLUTON")
        assert "unknown key 'resoluton' in [profiles.receipt]" in line
        assert "(did you mean 'resolution'?)" in line

    def test_attributes_env_not_for_differently_cased_toml_profile(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A TOML ``[profiles.Receipt]`` error is not blamed on a ``RECEIPT`` variable.

        The check walks the environment's contribution, as pydantic-settings
        built it, by the exact string elements of the error ``loc``.
        pydantic-settings case-folds variable names, so the variable created
        profile ``receipt``; the TOML error's ``loc`` holds ``Receipt``, which
        is not in that contribution, and ``SANELESS_PROFILES`` itself is unset.
        """
        monkeypatch.setenv("SANELESS_PROFILES__RECEIPT__TITLE", "R")
        err = _load_error(
            tmp_config_dir / "cased_profile.toml",
            "[profiles.default]\n\n[profiles.Receipt]\nresoluton = 1\n",
        )
        assert "environment variable" not in str(err)
        assert any(
            line.startswith("  [profiles.Receipt] unknown key 'resoluton'")
            for line in _error_lines(err)
        )

    def test_attributes_env_not_to_a_json_section_for_a_file_key(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A JSON section variable is not blamed for the file's typo (WR-04).

        ``SANELESS_OUTPUT`` supplies ``web_port`` only; ``tmpdir`` is in the
        file. The shorter-prefix fallback used to match ``SANELESS_OUTPUT``
        even though the failing key was not in what the environment supplied.
        """
        monkeypatch.setenv("SANELESS_OUTPUT", '{"web_port": 1234}')
        err = _load_error(
            tmp_config_dir / "typo.toml",
            '[output]\ntmpdir = "x"\n\n[profiles.default]\n',
        )
        assert "environment variable" not in str(err)
        assert any(
            line.startswith("  [output] unknown key 'tmpdir'")
            for line in _error_lines(err)
        )

    def test_attributes_env_not_to_a_json_profiles_variable_for_a_file_key(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``SANELESS_PROFILES`` JSON is not blamed for a TOML profile typo."""
        monkeypatch.setenv("SANELESS_PROFILES", '{"default": {"source": "x"}}')
        err = _load_error(
            tmp_config_dir / "typo.toml",
            "[profiles.default]\nresoluton = 1\n",
        )
        assert "environment variable" not in str(err)
        assert any(
            line.startswith("  [profiles.default] unknown key 'resoluton'")
            for line in _error_lines(err)
        )

    def test_attributes_env_json_section_value_to_its_variable(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bad value inside a JSON section variable still names that variable."""
        monkeypatch.setenv("SANELESS_OUTPUT", '{"web_port": "abc"}')
        err = _load_error(tmp_config_dir / "ok.toml", "[profiles.default]\n")
        assert "web_port" in _env_line(err, "SANELESS_OUTPUT")

    def test_attributes_env_deeper_variable_under_a_scalar_key(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``SANELESS_PAPERLESS__URL__X`` is named for the ``url`` error (WR-04).

        The variable turns ``url`` into a mapping, so the error's ``loc`` stops
        at ``url``; no variable is spelled exactly ``SANELESS_PAPERLESS__URL``,
        and the file has no ``[paperless]`` section to blame instead.
        """
        monkeypatch.setenv("SANELESS_PAPERLESS__URL__X", "1")
        err = _load_error(tmp_config_dir / "ok.toml", "[profiles.default]\n")
        line = _env_line(err, "SANELESS_PAPERLESS__URL__X")
        assert "url in [paperless]" in line
        assert not any(line.startswith("  [paperless]") for line in _error_lines(err))


class TestUnknownEnvironmentVariables:
    """
    A SANELESS_* variable naming no section is rejected at load (D-13).

    pydantic-settings silently ignores such names, so a mistyped
    ``SANELESS_PAPERLES__TOKEN`` left the token unset without a word.
    """

    @pytest.mark.usefixtures("no_discovered_config")
    def test_unknown_env_misspelled_section_suggests_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A misspelled section names the variable and the corrected spelling."""
        monkeypatch.setenv("SANELESS_PAPERLES__TOKEN", "t")
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        line = _env_line(exc_info.value, "SANELESS_PAPERLES__TOKEN")
        assert "did you mean SANELESS_PAPERLESS__TOKEN" in line
        assert "valid sections: scanner, paperless, output, web, profiles" in line

    @pytest.mark.usefixtures("no_discovered_config")
    def test_unknown_env_single_underscore_suggests_double(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``SANELESS_OUTPUT_WEB_PORT`` is pointed at ``SANELESS_OUTPUT__WEB_PORT``."""
        monkeypatch.setenv("SANELESS_OUTPUT_WEB_PORT", "9")
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        line = _env_line(exc_info.value, "SANELESS_OUTPUT_WEB_PORT")
        assert "SANELESS_OUTPUT__WEB_PORT" in line

    def test_unknown_env_cannot_set_config_path(
        self, no_discovered_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        SANELESS_CONFIG_PATH is rejected outright (D-13, D-16, T-26-05).

        It could never forge the loaded path, which is a private attribute;
        it is now also refused, rather than silently ignored.
        """
        evil = no_discovered_config / "evil.toml"
        evil.write_text("[profiles.default]\n")
        monkeypatch.setenv("SANELESS_CONFIG_PATH", str(evil))
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        assert "unknown section" in _env_line(exc_info.value, "SANELESS_CONFIG_PATH")

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("SANELESS_OUTPUT__DATA_DIR", "/var/lib/saneless"),
            ("SANELESS_PROFILES__RECEIPT__TITLE", "R"),
            ("SANELESS_OUTPUT", '{"web_port": 9}'),
        ],
    )
    def test_unknown_env_scan_accepts_valid_names(
        self,
        sample_toml: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        value: str,
    ) -> None:
        """
        Nested, profile and JSON-valued variables are not unknown.

        A file with a default profile is loaded, because a profile variable
        alone would replace the built-in ``default`` profile.
        """
        monkeypatch.setenv(name, value)
        settings = load_settings(str(sample_toml))
        assert "default" in settings.profiles

    def test_unknown_env_reported_with_toml_errors(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown variables and file errors arrive together in one message."""
        monkeypatch.setenv("SANELESS_BOGUS", "1")
        err = _load_error(
            tmp_config_dir / "both.toml",
            '[paperless]\ntokne = "x"\n\n[profiles.default]\n',
        )
        lines = _error_lines(err)
        assert any(
            line.startswith("  [paperless] unknown key 'tokne'") for line in lines
        )
        assert "unknown section" in _env_line(err, "SANELESS_BOGUS")

    def test_unknown_env_fails_an_otherwise_valid_load(
        self, sample_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A valid file does not excuse an unknown variable."""
        monkeypatch.setenv("SANELESS_BOGUS", "1")
        with pytest.raises(ConfigError) as exc_info:
            load_settings(str(sample_toml))
        assert _error_lines(exc_info.value)[0] == (
            f"Configuration error in {sample_toml}:"
        )

    def test_unknown_env_does_not_affect_direct_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scan lives in the loader, not a validator (Pitfall 9, S-10)."""
        monkeypatch.setenv("SANELESS_BOGUS", "1")
        assert Settings().config_path is None


class TestConfigSources:
    """
    The loaded file and the env-sourced key names are reported (CFG-11, U-01).

    Names only, never values: the token is commonly supplied by environment.
    """

    def test_config_sources_env_sourced_keys_lists_dotted_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Environment contributions are flattened to sorted dotted names."""
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", "t")
        monkeypatch.setenv("SANELESS_PAPERLESS__URL", "u")
        monkeypatch.setenv("SANELESS_PROFILES__RECEIPT__TITLE", "R")
        assert config_mod.env_sourced_keys() == [
            "paperless.token",
            "paperless.url",
            "profiles.receipt.title",
        ]

    def test_config_sources_env_sourced_keys_empty(self) -> None:
        """With no SANELESS_* variables the list is empty."""
        assert config_mod.env_sourced_keys() == []

    def test_config_sources_env_sourced_keys_json_section(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A JSON-valued section is reported by its leaf names."""
        monkeypatch.setenv("SANELESS_OUTPUT", '{"web_port": 9}')
        assert config_mod.env_sourced_keys() == ["output.web_port"]

    @staticmethod
    def _info(caplog: pytest.LogCaptureFixture) -> list[str]:
        """Return the INFO messages saneless.config emitted."""
        return [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.INFO and record.name == "saneless.config"
        ]

    def test_config_sources_logs_file_and_names_never_secret(
        self,
        sample_toml: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One INFO line names the loaded file and ``paperless.token``, not its value."""
        secret = "tok-SECRET-51ab"
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", secret)
        settings = load_settings(str(sample_toml))
        with caplog.at_level(logging.INFO, logger="saneless.config"):
            config_mod.log_config_sources(settings)
        messages = self._info(caplog)
        assert len(messages) == 1
        assert str(sample_toml) in messages[0]
        assert "paperless.token" in messages[0]
        assert secret not in messages[0]

    @pytest.mark.usefixtures("no_discovered_config")
    def test_config_sources_without_file_never_logs_secret(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """With no file the line says so, and still carries no value."""
        secret = "tok-SECRET-51ab"
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", secret)
        settings = load_settings()
        with caplog.at_level(logging.INFO, logger="saneless.config"):
            config_mod.log_config_sources(settings)
        messages = self._info(caplog)
        assert len(messages) == 1
        assert "no config file; defaults + environment" in messages[0]
        assert "paperless.token" in messages[0]
        assert secret not in messages[0]


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

    def test_empty_config_path_is_rejected_not_discovered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty explicit path is a ConfigError, not a discovery run (WR-05)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "saneless.toml").write_text("[profiles.default]\n")
        with pytest.raises(ConfigError, match="empty"):
            load_settings("")

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


@pytest.fixture
def config_local_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[Callable[[str], None]]:
    """
    Give one test control of the process's local zone, then restore it.

    Plan 30-01's ``local_zone`` technique: ``local_time`` renders whatever zone
    the C library reports, so pinning ``TZ`` and calling ``time.tzset()`` is
    the only way to assert an exact string on a host in an unknown zone. The
    trailing ``tzset`` is what makes the C library notice the removal.

    Yields:
        A function that switches the process's zone for the rest of the test.

    """

    def _use(zone: str) -> None:
        monkeypatch.setenv("TZ", zone)
        time.tzset()

    yield _use
    time.tzset()


class TestResolveJobTitle:
    """
    One title rule for every front end (D-16, M-24).

    A typed title wins when it is non-blank after stripping; otherwise the
    profile's ``title``; otherwise ``Scan <local YYYY-MM-DD HH:MM ZZZ>``
    (APPL-12). The documented ``title`` key used to do nothing.
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

    def test_title_without_profile_title_uses_timestamp(
        self, config_local_zone: Callable[[str], None]
    ) -> None:
        """A profile with no title falls through to the local timestamp."""
        config_local_zone("America/Chicago")
        title = resolve_job_title("", ProfileConfig(), now=self._NOW)
        assert title == "Scan 2026-09-15 08:05 CDT"

    def test_title_blank_profile_title_uses_timestamp(
        self, config_local_zone: Callable[[str], None]
    ) -> None:
        """A whitespace profile title counts as blank."""
        config_local_zone("America/Chicago")
        profile = ProfileConfig(title="  ")
        title = resolve_job_title(None, profile, now=self._NOW)
        assert title == "Scan 2026-09-15 08:05 CDT"

    def test_title_no_profile_uses_timestamp(
        self, config_local_zone: Callable[[str], None]
    ) -> None:
        """With no profile at all the timestamp is used."""
        config_local_zone("America/Chicago")
        assert resolve_job_title(None, None, now=self._NOW) == (
            "Scan 2026-09-15 08:05 CDT"
        )

    def test_title_timestamp_names_utc_on_a_utc_server(
        self, config_local_zone: Callable[[str], None]
    ) -> None:
        """A UTC server still gets the zone named on the title (D-35)."""
        config_local_zone("UTC")
        assert resolve_job_title("", None, now=self._NOW) == "Scan 2026-09-15 13:05 UTC"

    def test_title_timestamp_ignores_the_zone_now_carries(
        self, config_local_zone: Callable[[str], None]
    ) -> None:
        """A non-UTC aware ``now`` still renders in the server's zone (APPL-12)."""
        config_local_zone("America/Chicago")
        plus_two = datetime(2026, 9, 15, 15, 5, tzinfo=timezone(timedelta(hours=2)))
        assert resolve_job_title("", None, now=plus_two) == "Scan 2026-09-15 08:05 CDT"

    def test_title_timestamp_uses_the_shared_formatter(
        self, config_local_zone: Callable[[str], None]
    ) -> None:
        """
        The title, the ``jobs`` table and the web history cannot disagree.

        Asserted against ``local_time`` itself rather than a second copy of the
        format string, which is the whole point of D-35.
        """
        config_local_zone("America/Chicago")
        assert resolve_job_title("", None, now=self._NOW) == (
            f"Scan {local_time(self._NOW)}"
        )

    def test_title_typed_is_returned_unstripped(self) -> None:
        """A non-blank typed title is returned as given, matching today."""
        typed = " Invoice "
        assert resolve_job_title(typed, None, now=self._NOW) == typed


class TestProfileConfigThresholds:
    """ProfileConfig empty page threshold fields."""

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

    def test_profile_default_matches_constant(self) -> None:
        """ProfileConfig resolution default matches DEFAULT_RESOLUTION."""
        profile = ProfileConfig()
        assert profile.resolution == DEFAULT_RESOLUTION


class TestEmptyPageDetectionToggle:
    """Empty page detection toggle on ProfileConfig."""

    def test_enable_empty_page_detection_false(self) -> None:
        """ProfileConfig accepts enable_empty_page_detection=False."""
        profile = ProfileConfig(enable_empty_page_detection=False)
        assert profile.enable_empty_page_detection is False


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


class TestWebPort:
    """
    OutputConfig web_port is a real TCP port number.

    The resolver truncates a service number to 16 bits, so an out-of-range
    value would otherwise bind a different port without any error: 70000
    became 4464 and 65536 became an OS-chosen port.
    """

    @pytest.mark.parametrize("value", [-1, 65_536, 70_000])
    def test_web_port_out_of_range_rejected(self, value: int) -> None:
        """A negative port and anything above 65535 fail validation."""
        with pytest.raises(ValidationError, match="web_port"):
            OutputConfig(web_port=value)

    @pytest.mark.parametrize("value", [0, 65_535])
    def test_web_port_bounds_accepted(self, value: int) -> None:
        """0 (an OS-chosen port) and 65535 are the inclusive bounds."""
        assert OutputConfig(web_port=value).web_port == value

    def test_web_port_out_of_range_in_toml_rejected(self, tmp_config_dir: Path) -> None:
        """A TOML ``web_port = 80800`` fails at load instead of binding 15264."""
        config_file = tmp_config_dir / "web_port_typo.toml"
        config_file.write_text("[output]\nweb_port = 80800\n\n[profiles.default]\n")
        with pytest.raises(ConfigError, match="web_port"):
            load_settings(config_path=str(config_file))


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


def _deny_write_access(monkeypatch: pytest.MonkeyPatch, denied: Path) -> None:
    """
    Make ``os.access`` report ``denied`` unwritable; every other path is asked for real.

    The directory check asks ``os.access``, so answering for it is the whole
    failure.  Injecting it rather than taking the directory's write bit away
    keeps the test meaningful as root, for whom ``os.access`` ignores modes.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        denied: The directory to report as not writable.

    """
    real_access = os.access

    def access(path: str | os.PathLike[str], mode: int) -> bool:
        if Path(path) == denied and mode & os.W_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr("saneless.config.os.access", access)


class TestValidateSettingsDirs:
    """Writability validation via validate_settings_dirs."""

    def test_validate_writable_tmp_dir_passes(self, tmp_path: Path) -> None:
        """No error when tmp_dir is writable, and the check writes nothing there."""
        settings = Settings(
            output=OutputConfig(tmp_dir=str(tmp_path)),
            profiles={"default": ProfileConfig()},
        )
        validate_settings_dirs(settings)
        assert list(tmp_path.iterdir()) == []

    def test_validate_unwritable_tmp_dir_fails_with_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unwritable tmp_dir raises ConfigError with 'not writable' message."""
        unwritable = tmp_path / "readonly"
        unwritable.mkdir()
        _deny_write_access(monkeypatch, unwritable)
        settings = Settings(
            output=OutputConfig(tmp_dir=str(unwritable)),
            profiles={"default": ProfileConfig()},
        )
        with pytest.raises(ConfigError, match="tmp_dir is not writable"):
            validate_settings_dirs(settings)

    def test_validate_writable_data_dir_passes(self, tmp_path: Path) -> None:
        """No error when data_dir is writable, and the check writes nothing there."""
        settings = Settings(
            output=OutputConfig(tmp_dir=str(tmp_path), data_dir=str(tmp_path)),
            profiles={"default": ProfileConfig()},
        )
        validate_settings_dirs(settings)
        assert list(tmp_path.iterdir()) == []

    def test_validate_unwritable_data_dir_fails_with_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unwritable data_dir raises ConfigError with 'not writable' message."""
        unwritable = tmp_path / "readonly"
        unwritable.mkdir()
        _deny_write_access(monkeypatch, unwritable)
        settings = Settings(
            output=OutputConfig(tmp_dir=str(tmp_path), data_dir=str(unwritable)),
            profiles={"default": ProfileConfig()},
        )
        with pytest.raises(ConfigError, match="data_dir is not writable"):
            validate_settings_dirs(settings)

    def test_validate_missing_data_dir_unwritable_parent_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing data_dir under an unwritable parent raises ConfigError."""
        parent = tmp_path / "readonly"
        parent.mkdir()
        _deny_write_access(monkeypatch, parent)
        settings = Settings(
            output=OutputConfig(
                tmp_dir=str(tmp_path), data_dir=str(parent / "saneless")
            ),
            profiles={"default": ProfileConfig()},
        )
        with pytest.raises(ConfigError, match="data_dir parent is not writable"):
            validate_settings_dirs(settings)

    @pytest.mark.parametrize("label", ["tmp_dir", "data_dir", "consume_dir"])
    def test_validate_deep_missing_dir_under_unwritable_ancestor_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str
    ) -> None:
        """
        A deep missing directory is checked against its nearest ancestor (M-20).

        Only the immediate parent used to be checked, and only when it existed,
        so ``<unwritable>/a/b/c`` passed at startup and failed mid-scan.
        """
        ancestor = tmp_path / "readonly"
        ancestor.mkdir()
        deep = str(ancestor / "a" / "b" / "c")
        settings = Settings(
            output=OutputConfig(
                tmp_dir=deep if label == "tmp_dir" else str(tmp_path),
                data_dir=deep if label == "data_dir" else str(tmp_path),
            ),
            paperless=PaperlessConfig(
                consume_dir=deep if label == "consume_dir" else ""
            ),
            profiles={"default": ProfileConfig()},
        )
        _deny_write_access(monkeypatch, ancestor)
        with pytest.raises(ConfigError, match="not writable") as exc_info:
            validate_settings_dirs(settings)
        message = str(exc_info.value)
        assert message.startswith(label)
        assert str(ancestor) in message

    def test_validate_deep_missing_dirs_under_writable_ancestor_pass(
        self, tmp_path: Path
    ) -> None:
        """Missing directories whose nearest existing ancestor is writable pass."""
        settings = Settings(
            output=OutputConfig(
                tmp_dir=str(tmp_path / "t" / "a" / "b"),
                data_dir=str(tmp_path / "d" / "a" / "b"),
            ),
            paperless=PaperlessConfig(consume_dir=str(tmp_path / "c" / "a" / "b")),
            profiles={"default": ProfileConfig()},
        )
        validate_settings_dirs(settings)
        assert not (tmp_path / "t").exists()

    def test_nearest_existing_ancestor_walks_up(self, tmp_path: Path) -> None:
        """The nearest ancestor of a missing path is its deepest existing one."""
        assert config_mod._nearest_existing_ancestor(tmp_path / "a" / "b") == tmp_path
        assert config_mod._nearest_existing_ancestor(tmp_path) == tmp_path


class TestPathExpansion:
    """
    A leading ``~`` is expanded in every path setting (CFG-03, M-20).

    ``~/scans`` in a config file used to be taken literally, creating a
    directory named ``~`` in the working directory. By decision (Claude's
    Discretion), only ``~`` is expanded: ``$VAR`` stays literal (T-27-26), and
    an empty ``consume_dir`` is None because empty means disabled.
    """

    @pytest.fixture
    def home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """
        Redirect HOME into tmp_path and run in an empty CWD.

        Returns:
            The redirected home directory.

        """
        monkeypatch.chdir(tmp_path)
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        return home

    def test_expanduser_output_paths(self, home: Path) -> None:
        """``tmp_dir``, ``data_dir`` and ``log_file`` expand ``~`` under HOME."""
        output = OutputConfig(
            tmp_dir="~/t", data_dir="~/d", log_file="~/l/saneless.log"
        )
        assert output.tmp_dir == home / "t"
        assert output.data_dir == home / "d"
        assert output.log_file == home / "l" / "saneless.log"

    def test_expanduser_consume_dir(self, home: Path) -> None:
        """``consume_dir`` expands ``~`` under HOME."""
        paperless = PaperlessConfig(consume_dir="~/consume")
        assert paperless.consume_dir == home / "consume"

    def test_expanduser_from_toml(self, home: Path, tmp_path: Path) -> None:
        """A ``~`` path read from a TOML file is expanded."""
        config_file = tmp_path / "x.toml"
        config_file.write_text('[output]\ndata_dir = "~/state"\n\n[profiles.default]\n')
        settings = load_settings(str(config_file))
        assert settings.output.data_dir == home / "state"

    def test_expanduser_from_environment(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``~`` path from a SANELESS_* variable is expanded."""
        monkeypatch.setenv("SANELESS_OUTPUT__LOG_FILE", "~/x.log")
        settings = load_settings()
        assert settings.output.log_file == home / "x.log"

    @pytest.mark.usefixtures("home")
    def test_expanduser_leaves_empty_consume_dir_disabled(self) -> None:
        """An empty ``consume_dir`` means disabled: None, not ``Path(".")``."""
        assert PaperlessConfig(consume_dir="").consume_dir is None

    @pytest.mark.usefixtures("home")
    def test_expanduser_does_not_expand_variables(self) -> None:
        """``$HOME`` in a path setting is kept literally (T-27-26)."""
        assert OutputConfig(data_dir="$HOME/x").data_dir == Path("$HOME/x")

    @pytest.mark.usefixtures("home")
    def test_unknown_user_in_a_toml_path_is_a_config_error(
        self, tmp_path: Path
    ) -> None:
        """
        ``~nosuchuser`` names the section and key, never the value (WR-03).

        ``Path.expanduser`` raises RuntimeError, which pydantic does not turn
        into a validation error, so it used to escape the D-10 renderer as a
        bare "Could not determine home directory." naming no file or key.
        """
        err = _load_error(
            tmp_path / "x.toml",
            '[output]\ndata_dir = "~saneless-no-such-user-xyz/state"\n\n'
            "[profiles.default]\n",
        )

        message = str(err)
        assert str(tmp_path / "x.toml") in message
        assert any(
            line.strip().startswith("[output] data_dir:") and "'~'" in line
            for line in _error_lines(err)
        ), message
        assert "saneless-no-such-user-xyz" not in message

    @pytest.mark.usefixtures("home")
    def test_unknown_user_in_consume_dir_is_a_config_error(self) -> None:
        """``consume_dir`` goes through the same expansion and the same error."""
        with pytest.raises(ValidationError) as exc_info:
            PaperlessConfig(consume_dir="~saneless-no-such-user-xyz/consume")
        assert exc_info.value.errors()[0]["loc"] == ("consume_dir",)

    def test_unknown_user_in_config_path_is_a_config_error(self) -> None:
        """``--config ~nosuchuser/c.toml`` is a ConfigError naming the path."""
        with pytest.raises(ConfigError) as exc_info:
            load_settings("~saneless-no-such-user-xyz/c.toml")
        message = str(exc_info.value)
        assert "~saneless-no-such-user-xyz/c.toml" in message
        assert "'~'" in message


class TestPathTypedSettings:
    """
    The path settings are ``Path`` values on the model, not strings.

    ``consume_dir`` is the one optional path: empty or omitted means the
    fallback copy is disabled, so it loads as None. Pydantic would otherwise
    turn ``""`` into ``Path(".")`` and the fallback would write PDFs into the
    working directory.
    """

    def test_output_path_defaults_are_paths(self) -> None:
        """``tmp_dir``, ``data_dir`` and ``log_file`` default to Path values."""
        output = OutputConfig()
        assert output.tmp_dir == Path(tempfile.gettempdir()) / "saneless"
        assert isinstance(output.data_dir, Path)
        assert output.log_file == output.data_dir / "saneless.log"

    def test_output_paths_given_as_strings_become_paths(self, tmp_path: Path) -> None:
        """A string from a config file or constructor is stored as a Path."""
        output = OutputConfig(
            tmp_dir=str(tmp_path / "t"),
            data_dir=str(tmp_path / "d"),
            log_file=str(tmp_path / "l.log"),
        )
        assert output.tmp_dir == tmp_path / "t"
        assert output.data_dir == tmp_path / "d"
        assert output.log_file == tmp_path / "l.log"

    def test_consume_dir_defaults_to_disabled(self) -> None:
        """An omitted ``consume_dir`` is None."""
        assert PaperlessConfig().consume_dir is None

    def test_whitespace_only_consume_dir_is_disabled(self) -> None:
        """A whitespace-only ``consume_dir`` is None, never ``Path("  ")``."""
        assert PaperlessConfig(consume_dir="   ").consume_dir is None

    def test_a_configured_consume_dir_is_a_path(self, tmp_path: Path) -> None:
        """A non-empty ``consume_dir`` string is stored as a Path."""
        target = tmp_path / "consume"
        assert PaperlessConfig(consume_dir=str(target)).consume_dir == target

    def test_empty_consume_dir_from_environment_is_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``SANELESS_PAPERLESS__CONSUME_DIR=""`` loads as None."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SANELESS_PAPERLESS__CONSUME_DIR", "")
        assert load_settings().paperless.consume_dir is None

    def test_empty_consume_dir_from_toml_is_disabled(self, tmp_path: Path) -> None:
        """A TOML ``consume_dir = ""`` loads as None."""
        config_file = tmp_path / "x.toml"
        config_file.write_text('[paperless]\nconsume_dir = ""\n\n[profiles.default]\n')
        assert load_settings(str(config_file)).paperless.consume_dir is None


class TestDataDir:
    """OutputConfig.data_dir and its computed db_path / failed_dir properties."""

    def test_data_dir_default_is_under_local_state(self) -> None:
        """The default data_dir is a Path ending in .local/state/saneless."""
        data_dir = OutputConfig().data_dir
        assert isinstance(data_dir, Path)
        assert data_dir.parts[-3:] == (".local", "state", "saneless")

    def test_data_dir_default_is_not_the_temp_dir(self) -> None:
        """The default data_dir is the XDG state directory, not tmp_dir."""
        config = OutputConfig()
        assert config.data_dir == Path(os.environ["XDG_STATE_HOME"]) / "saneless"
        assert config.data_dir != config.tmp_dir

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
        assert settings.output.data_dir == override
        assert settings.output.db_path == override / "saneless.db"


class TestPlaceholderToken:
    """
    ``is_placeholder_token`` is an exact-match predicate, not a heuristic (D-14).

    ``doctor``, the status strip, the scan route and ``saneless scan`` must all
    agree on whether the appliance can upload, so there is one predicate. The
    set is a small fixed literal set deliberately: refusing a legitimate token
    from a future paperless-ngx version is worse than missing an exotic
    placeholder (APPL-07).
    """

    @pytest.mark.parametrize("blank", ["", " ", "   ", "\t\n", "\t \n "])
    def test_blank_is_a_placeholder(self, blank: str) -> None:
        """An unset or whitespace-only token is a placeholder."""
        assert is_placeholder_token(blank) is True

    @pytest.mark.parametrize("token", sorted(PLACEHOLDER_TOKENS))
    def test_every_literal_in_the_set_is_a_placeholder(self, token: str) -> None:
        """Every member of PLACEHOLDER_TOKENS is recognised."""
        assert is_placeholder_token(token) is True

    @pytest.mark.parametrize(
        "known",
        [
            "changeme",
            "your-token-here",
            "your-api-token-here",
            "your_token_here",
            "token",
            "replace-me",
        ],
    )
    def test_the_documented_literals_are_members(self, known: str) -> None:
        """The literals the docs and compose file ship are in the set."""
        assert known in PLACEHOLDER_TOKENS

    @pytest.mark.parametrize(
        "written",
        ["CHANGEME", " changeme ", "ChangeMe", "\tCHANGEME\n", "YOUR-TOKEN-HERE"],
    )
    def test_case_and_surrounding_space_do_not_hide_a_placeholder(
        self, written: str
    ) -> None:
        """Matching is case-insensitive after stripping."""
        assert is_placeholder_token(written) is True

    @pytest.mark.parametrize(
        "real",
        [
            "40characterhexlookingrealapitokenvalue00",
            "changeme7f3a91",
            "my-changeme",
            "notchangeme",
            "tokenizer",
            "your-token-here-really",
        ],
    )
    def test_a_real_token_is_not_a_placeholder(self, real: str) -> None:
        """Membership is exact: a superstring of a placeholder is a real token."""
        assert is_placeholder_token(real) is False

    def test_the_example_config_ships_a_token_the_predicate_refuses(self) -> None:
        """
        ``saneless.toml.example``'s token is a member, read not hard-coded.

        The example and the predicate cannot drift apart: if someone edits the
        example's placeholder, this test fails until the set is updated.
        """
        example = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "saneless.toml.example").read_text()
        )
        shipped = example["paperless"]["token"]
        assert shipped in PLACEHOLDER_TOKENS
        assert is_placeholder_token(shipped) is True

    def test_predicate_is_annotated_to_take_a_plain_string(self) -> None:
        """
        The predicate takes an already-unwrapped ``str`` (CFG-05, ASVS V7).

        Asserted on the annotation rather than by calling it with a
        ``SecretStr``, because a call that type-checkers reject would need a
        suppression to write down.
        """
        assert is_placeholder_token.__annotations__["value"] == "str"
        assert is_placeholder_token.__annotations__["return"] == "bool"

    def test_config_module_adds_no_secret_unwrap_site(self) -> None:
        """
        The predicate adds no secret-unwrapping call to ``config.py``.

        Only ``cli.py scan`` and ``web/app.py create_app`` unwrap the token
        (T-30-05, N-15); ``config.py`` itself never does, and naming the method
        in a comment is not a call site -- the call form is what is asserted.
        """
        source = Path(config_mod.__file__).read_text()
        assert ".get_secret_value(" not in source


class TestProfileLabelAndDescription:
    """
    ``label`` and ``description`` are persisted, tool-owned profile keys (D-18).

    ``saneless auto-profiles`` writes them and ``--force`` overwrites them in
    place; the operator's escape hatch is removing ``auto_generated``. Both
    default to ``""`` so a config written before this phase still loads under
    ``extra="forbid"`` (APPL-05).
    """

    def test_both_round_trip_from_toml(self, tmp_config_dir: Path) -> None:
        """A TOML table carrying both keys loads both values verbatim."""
        config_file = tmp_config_dir / "labelled.toml"
        config_file.write_text(
            "[profiles.default]\n"
            'label = "Feeder, double-sided"\n'
            'description = "Scans both sides of every page using the feeder."\n'
        )
        profile = load_settings(config_path=str(config_file)).profiles["default"]
        assert profile.label == "Feeder, double-sided"
        assert profile.description == "Scans both sides of every page using the feeder."

    def test_a_config_written_before_this_phase_still_loads(
        self, tmp_config_dir: Path
    ) -> None:
        """
        A profile table with neither key loads under ``extra="forbid"``.

        Defaulting to ``""`` is what keeps an existing deployment loading; the
        dropdown renders ``label or name`` (Amendment A-3) so such a profile is
        never a blank option.
        """
        config_file = tmp_config_dir / "pre_phase.toml"
        config_file.write_text(
            '[profiles.default]\nsource = "Flatbed"\n'
            "resolution = 300\nauto_generated = true\n"
        )
        profile = load_settings(config_path=str(config_file)).profiles["default"]
        assert profile.label == ""
        assert profile.description == ""
        assert profile.auto_generated is True

    def test_label_over_max_length_is_rejected(self) -> None:
        """An over-long label fails validation; it is rendered into HTML."""
        with pytest.raises(ValidationError, match="label"):
            ProfileConfig(label="x" * (PROFILE_LABEL_MAX_LENGTH + 1))

    def test_label_at_max_length_is_accepted(self) -> None:
        """A label of exactly PROFILE_LABEL_MAX_LENGTH characters loads."""
        label = "x" * PROFILE_LABEL_MAX_LENGTH
        assert ProfileConfig(label=label).label == label

    def test_description_over_max_length_is_rejected(self) -> None:
        """An over-long description fails validation (T-30-08, ROBU-08)."""
        with pytest.raises(ValidationError, match="description"):
            ProfileConfig(description="x" * (PROFILE_DESCRIPTION_MAX_LENGTH + 1))

    def test_description_at_max_length_is_accepted(self) -> None:
        """A description of exactly its cap loads."""
        text = "x" * PROFILE_DESCRIPTION_MAX_LENGTH
        assert ProfileConfig(description=text).description == text

    def test_label_has_no_alias(self) -> None:
        """
        ``label`` is spelled one way; ``populate_by_name`` gives it no second name.

        ``default_title`` carries the ``title`` alias, so the error machinery
        lists aliases -- ``label`` deliberately has none to list.
        """
        assert ProfileConfig.model_fields["label"].alias is None
        assert ProfileConfig.model_fields["description"].alias is None

    def test_both_are_listed_as_valid_profile_keys(self, tmp_config_dir: Path) -> None:
        """A profile typo lists ``label`` and ``description`` among valid keys."""
        err = _load_error(
            tmp_config_dir / "labl.toml",
            '[profiles.default]\nlabl = "x"\n',
        )
        matching = [line for line in _error_lines(err) if "unknown key 'labl'" in line]
        assert len(matching) == 1
        valid = matching[0].split("valid keys: ", 1)[1].split(", ")
        assert "label" in valid
        assert "description" in valid


class TestWebConfig:
    """
    The ``[web]`` section decides the scan form's shape (D-28, D-29, APPL-10).

    One appliance, one configured form shape -- not a per-browser toggle. Both
    keys default on, so an existing deployment's form is unchanged, and hiding
    a control changes the form and never the scan.
    """

    def test_web_config_absent_table_gets_the_defaults(
        self, tmp_config_dir: Path
    ) -> None:
        """A config file that predates the section loads with both defaults."""
        config_file = tmp_config_dir / "no_web.toml"
        config_file.write_text('[scanner]\nhost = "192.168.1.50"\n')
        settings = load_settings(config_path=str(config_file))
        assert settings.web.show_tags is True
        assert settings.web.show_correspondent is True

    def test_web_config_toml_turns_a_control_off(self, tmp_config_dir: Path) -> None:
        """``[web] show_tags = false`` hides the tag control."""
        config_file = tmp_config_dir / "web_off.toml"
        config_file.write_text("[web]\nshow_tags = false\n")
        settings = load_settings(config_path=str(config_file))
        assert settings.web.show_tags is False
        assert settings.web.show_correspondent is True

    def test_web_config_env_var_turns_a_control_off(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``SANELESS_WEB__SHOW_TAGS=false`` hides the tag control."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SANELESS_WEB__SHOW_TAGS", "false")
        settings = load_settings()
        assert settings.web.show_tags is False

    def test_web_config_unknown_key_is_a_rendered_error(
        self, tmp_config_dir: Path
    ) -> None:
        """
        A mistyped ``[web]`` key is a loud error, not a silent default (T-30-07).

        The new section must be visible to the same unknown-key machinery that
        renders ``[paperless] unknown key 'tokne'``.
        """
        err = _load_error(
            tmp_config_dir / "web_typo.toml",
            "[web]\nshow_tag = false\n",
        )
        assert (
            "  [web] unknown key 'show_tag' (did you mean 'show_tags'?); "
            "valid keys: show_tags, show_correspondent"
        ) in _error_lines(err)

    def test_web_config_key_under_wrong_section_says_where_it_belongs(
        self, tmp_config_dir: Path
    ) -> None:
        """``show_tags`` under ``[output]`` is pointed at ``[web]``."""
        err = _load_error(
            tmp_config_dir / "web_misplaced.toml",
            "[output]\nshow_tags = false\n",
        )
        assert "  [output] unknown key 'show_tags'; it belongs in [web]" in (
            _error_lines(err)
        )

    def test_web_config_is_extra_forbid(self) -> None:
        """WebConfig forbids unknown keys the way every other section does."""
        assert WebConfig.model_config["extra"] == "forbid"

    def test_web_config_is_built_with_a_default_factory(self) -> None:
        """
        ``web`` is hung with ``default_factory``, matching the other sections.

        A plain instance default is built once at import; every section on
        ``Settings`` uses a factory so none can freeze import-time state
        (config.py's comment on ``scanner``/``paperless``/``output``).
        """
        field = Settings.model_fields["web"]
        assert field.default_factory is WebConfig


class TestEverySectionRendersUnknownKeys:
    """
    Every plain section renders the D-11 unknown-key line, not a bare message.

    The error renderer looks a section's model up in a hand-maintained mapping,
    while every other reader derives the section list from ``Settings``' own
    fields. A section added to one and not the other loads fine and then falls
    back to pydantic's bare "Extra inputs are not permitted" -- exactly the
    silence CFG-01 exists to remove. Asserted over the sections themselves so a
    future section cannot be added without being wired up (CFG-01, M-18).
    """

    @pytest.mark.parametrize(
        "section", sorted(set(Settings.model_fields) - {"profiles"})
    )
    def test_section_names_its_valid_keys(
        self, section: str, tmp_config_dir: Path
    ) -> None:
        """An unknown key names the section, the key and that section's keys."""
        err = _load_error(
            tmp_config_dir / f"{section}_bogus.toml",
            f"[{section}]\nzzz_bogus_key = 1\n",
        )
        matching = [
            line
            for line in _error_lines(err)
            if line.startswith(f"  [{section}] unknown key 'zzz_bogus_key'")
        ]
        assert len(matching) == 1
        assert "valid keys: " in matching[0]


class TestProfileStorageForLoaded:
    """
    ``profile_storage_for_loaded`` is the one rule for "nothing was written".

    CR-01 was this rule written twice -- spelled out in ``saneless doctor`` and
    left out by omission in ``ScanWorker`` -- with only one copy correct, so the
    web status strip printed a permanent amber Profiles row contradicting what
    ``doctor`` printed for the very same appliance. D-02 promises both surfaces
    report the same checks in the same words, so there may be exactly one
    derivation and both must call it.
    """

    def test_a_loaded_config_file_means_persisted(self, tmp_path: Path) -> None:
        """Settings that came from a file have a file to have come from."""
        config_file = tmp_path / "saneless.toml"
        config_file.write_text("# loaded by --config\n")
        settings = Settings()
        settings._config_path = config_file

        assert profile_storage_for_loaded(settings) is ProfileStorage.PERSISTED

    def test_no_loaded_config_file_means_in_memory(self) -> None:
        """Defaults and environment variables only: there is nothing to save to."""
        assert (
            profile_storage_for_loaded(Settings())
            is ProfileStorage.IN_MEMORY_NO_CONFIG_FILE
        )

    @pytest.mark.parametrize("filename", ["saneless.toml", None])
    def test_it_never_reports_the_unwritable_member(
        self, tmp_path: Path, filename: str | None
    ) -> None:
        """
        No write was attempted, so the refused-write outcome is unreachable.

        ``IN_MEMORY_UNWRITABLE`` is what the *worker* records when its one
        startup persist attempt was refused. A caller that attempted no write
        has no such outcome and must not invent one by probing: Phase 27 D-09's
        motivating failure is EBUSY on a single-file bind mount, where the
        directory is writable and only the rename fails.

        Args:
            tmp_path: pytest's per-test directory.
            filename: The config file to record, or None for no loaded file.

        """
        settings = Settings()
        if filename is not None:
            settings._config_path = tmp_path / filename

        assert profile_storage_for_loaded(settings) is not (
            ProfileStorage.IN_MEMORY_UNWRITABLE
        )

    def test_a_config_path_that_does_not_exist_is_still_persisted(
        self, tmp_path: Path
    ) -> None:
        """
        The question is about the loaded settings, not about the disk.

        Reaching for the filesystem here would make the answer depend on
        whatever happened to the file after it was read, which is a different
        question from the one the Profiles row asks.
        """
        missing = tmp_path / "gone.toml"
        settings = Settings()
        settings._config_path = missing

        assert not missing.exists()
        assert profile_storage_for_loaded(settings) is ProfileStorage.PERSISTED

    def test_it_is_pure_and_touches_no_filesystem(self, tmp_path: Path) -> None:
        """Called twice with the same settings it gives the same answer twice."""
        missing = tmp_path / "gone.toml"
        settings = Settings()
        settings._config_path = missing

        first = profile_storage_for_loaded(settings)
        second = profile_storage_for_loaded(settings)

        assert first is second
        assert list(tmp_path.iterdir()) == []
