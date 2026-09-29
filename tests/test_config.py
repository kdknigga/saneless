"""Tests for configuration loading and validation."""

from __future__ import annotations

import errno
import logging
import os
import pwd
import re
import stat
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Final, NamedTuple, TypedDict, Unpack

import pytest
from click.testing import CliRunner
from pydantic import ValidationError
from pydantic_settings.exceptions import SettingsError

import saneless.config as config_mod
import saneless.vocabulary as vocabulary_mod
from saneless.cli import cli
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

        A developer's real ``~/.config/saneless/saneless.toml`` must not leak in.
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
        """
        The relative search entry that was found is recorded absolute.

        A relative path is worth nothing to a reader without the working
        directory it was relative to, and the log, ``doctor`` and the write
        target all read this one recording.
        """
        (empty_cwd_and_home / "saneless.toml").write_text("[profiles.default]\n")
        settings = load_settings()
        assert settings.config_path == empty_cwd_and_home / "saneless.toml"

    def test_home_search_path_is_recorded(
        self, empty_cwd_and_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config found under the redirected HOME is recorded (D-16)."""
        monkeypatch.delenv("XDG_CONFIG_HOME")
        home_config = empty_cwd_and_home / "home" / ".config" / "saneless"
        home_config.mkdir(parents=True)
        expected = home_config / config_mod.CONFIG_FILENAME
        expected.write_text("[profiles.default]\n")
        settings = load_settings()
        assert settings.config_path == expected

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
            config_mod.xdg_config_home() / "saneless" / "saneless.toml",
            Path("/etc/saneless/saneless.toml"),
        )

    def test_every_search_path_uses_the_one_config_filename(self) -> None:
        """
        One blessed filename in all three locations (Phase 37 CFG-01, D-01).

        The tuple test above would still pass if a later edit reintroduced a
        second spelling somewhere; this one cannot.
        """
        assert config_mod.CONFIG_FILENAME == "saneless.toml"
        assert {p.name for p in config_mod.config_search_paths()} == {
            config_mod.CONFIG_FILENAME
        }

    def test_config_search_paths_reads_home_at_call_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A HOME change after import is honoured by the search list (D-16)."""
        monkeypatch.delenv("XDG_CONFIG_HOME")
        monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
        expected = (
            tmp_path / "elsewhere" / ".config" / "saneless" / config_mod.CONFIG_FILENAME
        )
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
        """``$XDG_CONFIG_HOME/saneless/saneless.toml`` is searched second."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(empty_cwd_and_home / "xdg"))
        expected = empty_cwd_and_home / "xdg" / "saneless" / config_mod.CONFIG_FILENAME
        assert config_mod.config_search_paths()[1] == expected

    def test_config_under_xdg_config_home_is_loaded(
        self, empty_cwd_and_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config under ``$XDG_CONFIG_HOME`` is found and recorded (CFG-03)."""
        xdg_dir = empty_cwd_and_home / "xdg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_dir))
        config_dir = xdg_dir / "saneless"
        config_dir.mkdir(parents=True)
        expected = config_dir / config_mod.CONFIG_FILENAME
        expected.write_text('[scanner]\nhost = "from-xdg"\n\n[profiles.default]\n')
        settings = load_settings()
        assert settings.scanner.host == "from-xdg"
        assert settings.config_path == expected

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


_NO_HOME_FIX = "set HOME, or set XDG_CONFIG_HOME and XDG_STATE_HOME to absolute paths"
"""The part of the no-home refusal that says how to fix it."""


class TestUnresolvableHome:
    """
    A process with no home directory gets a configuration error, not a traceback.

    A container user with no HOME and no passwd entry has nowhere for the XDG
    defaults to live. ``Path.home()`` raised RuntimeError, which escaped the
    loader as an unexpected error with a traceback and no hint of the fix.
    """

    @pytest.fixture
    def no_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset HOME and both XDG variables, and give this uid no passwd entry."""

        def no_passwd_entry(uid: int) -> pwd.struct_passwd:
            raise KeyError(uid)

        monkeypatch.delenv("HOME")
        monkeypatch.delenv("XDG_CONFIG_HOME")
        monkeypatch.delenv("XDG_STATE_HOME")
        monkeypatch.setattr(pwd, "getpwuid", no_passwd_entry)

    @pytest.mark.usefixtures("no_home")
    @pytest.mark.parametrize("source", ["search", "explicit"])
    def test_unresolvable_home_is_a_config_error(
        self, tmp_path: Path, source: str
    ) -> None:
        """
        Both the search and the state defaults name the fix.

        The search reaches the home directory through the XDG config
        candidate; an explicit file skips the search and reaches it through
        the ``data_dir`` default instead.
        """
        config_file = tmp_path / "x.toml"
        config_file.write_text("[profiles.default]\n")

        with pytest.raises(ConfigError) as exc_info:
            load_settings(str(config_file) if source == "explicit" else None)

        assert _NO_HOME_FIX in str(exc_info.value)

    @pytest.mark.usefixtures("no_home")
    def test_unresolvable_home_exits_2_from_the_cli(self) -> None:
        """A command that loads settings exits 2 with the fix, not a traceback."""
        result = CliRunner().invoke(cli, ["jobs"])

        assert result.exit_code == 2, result.output
        assert _NO_HOME_FIX in result.output
        assert "Traceback" not in result.output


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
        assert settings.output.tmp_dir == (
            Path(tempfile.gettempdir()) / f"saneless-{os.getuid()}"
        )
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

    def test_title_longer_than_paperless_keeps_fails_to_load(
        self, tmp_config_dir: Path
    ) -> None:
        """
        A 119-character profile title is a load-time ConfigError naming the key.

        paperless-ngx keeps 127 characters of a title and a split duplex job
        appends " (fronts)", so 118 is the longest title that arrives whole.
        """
        toml_content = f'[profiles.default]\ntitle = "{"x" * 119}"\n'
        err = _load_error(tmp_config_dir / "long_title.toml", toml_content)
        assert "[profiles.default] title: String should have at most 118" in str(err)

    def test_title_of_what_paperless_keeps_loads(self, tmp_config_dir: Path) -> None:
        """A 118-character profile title loads unchanged."""
        title = "x" * 118
        config_file = tmp_config_dir / "cap_title.toml"
        config_file.write_text(f'[profiles.default]\ntitle = "{title}"\n')
        settings = load_settings(config_path=str(config_file))
        assert settings.profiles["default"].default_title == title


_MALFORMED_ID_NAMES = ["zero", "negative", "22-digit", "one-over"]


class TestProfileIds:
    """
    A profile's default ids are bounded to what paperless-ngx can hold.

    paperless-ngx keys are 32-bit auto-increment integers, so an id outside
    1..2147483647 cannot name anything; it is refused when the config loads.
    Repeated tag ids collapse to one, in the order first written.
    """

    @pytest.mark.parametrize(
        "value", ["0", "-5", str(10**22), "2147483648"], ids=_MALFORMED_ID_NAMES
    )
    def test_malformed_default_tag_fails_to_load(
        self, tmp_config_dir: Path, value: str
    ) -> None:
        """A default tag id no paperless-ngx can have is a load-time error."""
        err = _load_error(
            tmp_config_dir / "bad_tag.toml",
            f"[profiles.default]\ndefault_tags = [3, {value}]\n",
        )
        assert "[profiles.default] default_tags" in str(err)

    @pytest.mark.parametrize(
        "value", ["0", "-5", str(10**22), "2147483648"], ids=_MALFORMED_ID_NAMES
    )
    def test_malformed_default_correspondent_fails_to_load(
        self, tmp_config_dir: Path, value: str
    ) -> None:
        """A default correspondent id no paperless-ngx can have is a load error."""
        err = _load_error(
            tmp_config_dir / "bad_correspondent.toml",
            f"[profiles.default]\ndefault_correspondent = {value}\n",
        )
        assert "[profiles.default] default_correspondent" in str(err)

    def test_largest_paperless_id_loads(self, tmp_config_dir: Path) -> None:
        """The largest id a paperless-ngx key can hold loads unchanged."""
        config_file = tmp_config_dir / "largest_id.toml"
        config_file.write_text(
            "[profiles.default]\n"
            "default_tags = [1, 2147483647]\n"
            "default_correspondent = 2147483647\n"
        )
        profile = load_settings(config_path=str(config_file)).profiles["default"]
        assert profile.default_tags == [1, 2_147_483_647]
        assert profile.default_correspondent == 2_147_483_647

    def test_repeated_default_tags_dedupe_in_order(self, tmp_config_dir: Path) -> None:
        """Repeated default tag ids collapse to one, first occurrence kept."""
        config_file = tmp_config_dir / "repeated_tags.toml"
        config_file.write_text("[profiles.default]\ndefault_tags = [3, 7, 3, 7, 1]\n")
        profile = load_settings(config_path=str(config_file)).profiles["default"]
        assert profile.default_tags == [3, 7, 1]

    def test_repeated_default_tags_dedupe_on_construction(self) -> None:
        """A profile built in code collapses repeats the same way."""
        assert ProfileConfig(default_tags=[3, 7, 3]).default_tags == [3, 7]


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


_PROFILE_VALID_KEYS = ", ".join(
    field.alias or name for name, field in ProfileConfig.model_fields.items()
)
"""A profile table's valid keys, as the unknown-key line lists them."""

_WRITE_AS_TITLE = (
    "unknown key 'default_title' in [profiles.default] (write it as 'title'); "
    f"valid keys: {_PROFILE_VALID_KEYS}"
)
"""The environment form of the ``default_title`` refusal, after the name."""


class TestTitleSpelling:
    """
    A profile's title key has one spelling: ``title``.

    ``default_title`` is the field's name in code, not a config key. It used
    to be accepted as a second spelling, so a table holding both loaded one
    of them without a word about the other.
    """

    def test_default_title_alone_says_to_write_title(
        self, tmp_config_dir: Path
    ) -> None:
        """``default_title`` on its own is refused with the key to write."""
        err = _load_error(
            tmp_config_dir / "default_title.toml",
            '[profiles.default]\ndefault_title = "x"\n',
        )
        assert _error_lines(err)[1:] == [
            "  [profiles.default] unknown key 'default_title' "
            f"(write it as 'title'); valid keys: {_PROFILE_VALID_KEYS}"
        ]
        assert "default_title" not in _PROFILE_VALID_KEYS.split(", ")

    def test_default_title_beside_title_is_refused_the_same_way(
        self, tmp_config_dir: Path
    ) -> None:
        """Both spellings in one table never silently pick one."""
        err = _load_error(
            tmp_config_dir / "both_titles.toml",
            '[profiles.default]\ntitle = "a"\ndefault_title = "b"\n',
        )
        assert _error_lines(err)[1:] == [
            "  [profiles.default] unknown key 'default_title' "
            f"(write it as 'title'); valid keys: {_PROFILE_VALID_KEYS}"
        ]

    @pytest.mark.usefixtures("no_discovered_config")
    def test_default_title_variable_says_to_write_title(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The environment form names the variable and the same fix."""
        variable = "SANELESS_PROFILES__DEFAULT__DEFAULT_TITLE"
        monkeypatch.setenv(variable, "x")
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        assert _env_line(exc_info.value, variable) == (
            f"  environment variable {variable!r}: {_WRITE_AS_TITLE}"
        )

    def test_title_alone_loads_into_the_title_field(self, tmp_config_dir: Path) -> None:
        """``title`` is the spelling that loads."""
        config_file = tmp_config_dir / "title_only.toml"
        config_file.write_text('[profiles.default]\ntitle = "a"\n')
        profile = load_settings(config_path=str(config_file)).profiles["default"]
        assert profile.default_title == "a"

    def test_a_typo_near_default_title_suggests_title(
        self, tmp_config_dir: Path
    ) -> None:
        """A near miss of the field's code name is pointed at ``title``."""
        err = _load_error(
            tmp_config_dir / "default_titel.toml",
            '[profiles.default]\ndefault_titel = "x"\n',
        )
        (line,) = _error_lines(err)[1:]
        assert "'default_title'" not in line
        assert line.startswith(
            "  [profiles.default] unknown key 'default_titel' "
            "(did you mean 'title'?); valid keys: "
        )


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
        assert "[profiles.default] table is required" in str(err)
        assert "Value error" not in str(err)

    def test_missing_default_profile_is_named_without_a_pydantic_prefix(
        self, tmp_config_dir: Path
    ) -> None:
        """A config with no ``[profiles.default]`` says which table is missing."""
        err = _load_error(
            tmp_config_dir / "no_default.toml",
            '[profiles.receipts]\nsource = "ADF"\n',
        )
        matching = [
            line
            for line in _error_lines(err)
            if "[profiles.default] table is required" in line
        ]
        assert len(matching) == 1, str(err)
        assert "Value error" not in matching[0]
        assert "scan uses when none is named" in matching[0]


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
        2); the loader names the variable with fixed text, never the value.
        """
        value = "notjson"
        monkeypatch.setenv("SANELESS_PAPERLESS", value)
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        assert "'SANELESS_PAPERLESS'" in str(exc_info.value)
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

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("p@ss!", id="symbol-inside-and-trailing"),
            pytest.param("ab-", id="trailing-dash"),
            pytest.param("(tok)", id="parenthesised"),
        ],
    )
    def test_redact_input_strikes_a_value_edged_with_punctuation(
        self, value: str
    ) -> None:
        """
        A short value that starts or ends with punctuation is still struck.

        A word boundary needs a word character on one side of it, so a value
        whose first or last character is punctuation has no boundary to find
        where it meets a space or a bracket. Such a value used to survive in
        the message whole: exactly the short, symbol-heavy kind a password is.
        """
        hostile: ErrorDetails = {
            "type": "string_type",
            "loc": ("paperless", "token"),
            "msg": f"Input should be a valid string (got {value})",
            "input": value,
        }
        lines = config_mod._render_error_lines([hostile], {})
        assert len(lines) == 1
        assert value not in lines[0]
        assert lines[0] == f"[paperless] token: {_REDACTED_MESSAGE}"

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


_TOKEN_RULE = "must contain only visible ASCII characters"
"""The start of the refusal every malformed token gets, whatever is wrong."""

_URL_SHAPE_RULE = "address that names a host"
"""The tail of the refusal a scheme-less, non-HTTP or host-less URL gets."""

_URL_USERINFO_RULE = "put the paperless-ngx API token in paperless.token"
"""The part of the userinfo refusal that says where a credential belongs."""


def _only_body_line(err: ConfigError) -> str:
    """
    Return the one rendered line under the header, asserting there is one.

    Args:
        err: The load's ConfigError.

    Returns:
        The single body line, leading indentation included.

    """
    body = _error_lines(err)[1:]
    assert len(body) == 1, str(err)
    return body[0]


class TestPaperlessTokenAndUrlAtLoad:
    """
    ``paperless.token`` and ``paperless.url`` are checked where they are loaded.

    Both are typed by hand or pasted from a secret file, and both used to be
    kept exactly as typed: a trailing newline from a secret file ended up in
    every request's Authorization header, and ``paperless:8000`` failed only
    when the first scan tried to upload. The load is the one place a bad value
    can be refused before any request exists, so that is where it happens --
    with a message that names the key and never the value.
    """

    @pytest.mark.parametrize(
        ("toml_value", "raw_value"),
        [
            pytest.param('"ab c"', "ab c", id="inner-space"),
            pytest.param('"ab\\u0001c"', "ab\x01c", id="control-character"),
            pytest.param('"t\\u00f6k"', "tök", id="non-ascii"),
        ],
    )
    def test_a_token_with_a_bad_character_inside_is_refused(
        self, tmp_config_dir: Path, toml_value: str, raw_value: str
    ) -> None:
        """Stripping cannot fix a character inside the token, so it is refused."""
        err = _load_error(
            tmp_config_dir / "bad_token.toml",
            f"[paperless]\ntoken = {toml_value}\n",
        )
        line = _only_body_line(err)
        assert line.startswith("  [paperless] token: ")
        assert _TOKEN_RULE in line
        TestConfigErrorsNeverEchoValues._assert_value_absent(err, raw_value)

    @pytest.mark.usefixtures("no_discovered_config")
    def test_a_token_with_a_bad_character_from_the_environment_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same rule holds for ``SANELESS_PAPERLESS__TOKEN``."""
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", "ab c")
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        line = _env_line(exc_info.value, "SANELESS_PAPERLESS__TOKEN")
        assert _TOKEN_RULE in line
        TestConfigErrorsNeverEchoValues._assert_value_absent(exc_info.value, "ab c")

    @pytest.mark.parametrize(
        ("toml_value", "expected"),
        [
            pytest.param('"tok\\r\\n"', "tok", id="crlf"),
            pytest.param('" tok "', "tok", id="spaces"),
            pytest.param('"\\ttok\\n"', "tok", id="tab-and-newline"),
        ],
    )
    def test_whitespace_around_a_token_is_stripped(
        self, tmp_config_dir: Path, toml_value: str, expected: str
    ) -> None:
        """A secret file's trailing newline is not part of the token."""
        config_file = tmp_config_dir / "padded_token.toml"
        config_file.write_text(f"[paperless]\ntoken = {toml_value}\n")
        settings = load_settings(config_path=str(config_file))
        assert settings.paperless.token.get_secret_value() == expected

    @pytest.mark.usefixtures("no_discovered_config")
    def test_whitespace_around_an_environment_token_is_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A CRLF ``.env`` line loads the same token as a clean one."""
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", "tok\r\n")
        settings = load_settings()
        assert settings.paperless.token.get_secret_value() == "tok"

    def test_a_directly_built_token_is_stripped_too(self) -> None:
        """A token string handed straight to the model is stripped the same way."""
        config = PaperlessConfig(token=" tok\n")
        assert config.token.get_secret_value() == "tok"

    @pytest.mark.parametrize(
        "toml_value",
        [
            pytest.param('""', id="empty"),
            pytest.param('"changeme"', id="placeholder"),
            pytest.param('"  "', id="blank"),
        ],
    )
    def test_an_unset_token_still_loads(
        self, tmp_config_dir: Path, toml_value: str
    ) -> None:
        """
        An unset or stand-in token is not a load error.

        ``serve`` must still start so its status strip can say the token is
        missing; refusing it here would turn that explanation into a crash.
        """
        config_file = tmp_config_dir / "unset_token.toml"
        config_file.write_text(f"[paperless]\ntoken = {toml_value}\n")
        settings = load_settings(config_path=str(config_file))
        assert is_placeholder_token(settings.paperless.token.get_secret_value())

    @pytest.mark.parametrize(
        ("toml_value", "raw_value", "rule"),
        [
            pytest.param(
                '"paperless:8000"', "paperless:8000", _URL_SHAPE_RULE, id="no-scheme"
            ),
            pytest.param('"ftp://h"', "ftp://h", _URL_SHAPE_RULE, id="ftp-scheme"),
            pytest.param('"https://"', "https://", _URL_SHAPE_RULE, id="no-host"),
            pytest.param(
                '"http://h:abc"', "http://h:abc", "is not a valid URL", id="bad-port"
            ),
            pytest.param(
                '"http://pa perless:8000"',
                "http://pa perless:8000",
                "must contain only visible ASCII characters",
                id="inner-space",
            ),
            pytest.param(
                '"http://b\\u00fccher.lan"',
                "http://bücher.lan",
                "xn--",
                id="non-ascii-host",
            ),
        ],
    )
    def test_a_malformed_url_is_refused(
        self, tmp_config_dir: Path, toml_value: str, raw_value: str, rule: str
    ) -> None:
        """A URL no request could use is refused at load, not at first upload."""
        err = _load_error(
            tmp_config_dir / "bad_url.toml",
            f"[paperless]\nurl = {toml_value}\n",
        )
        line = _only_body_line(err)
        assert line.startswith("  [paperless] url: ")
        assert rule in line
        TestConfigErrorsNeverEchoValues._assert_value_absent(err, raw_value)

    @pytest.mark.parametrize(
        "url", ["http://", "https://", "paperless:8000", "ftp://paperless"]
    )
    def test_a_misshapen_url_gets_the_whole_url_rule(
        self, tmp_config_dir: Path, url: str
    ) -> None:
        """
        A likely wrong URL gets the rule in full, not a rule with a hole in it.

        The input is struck wherever it stands alone in the message, so a rule
        that spelled out ``http://`` or an example address would lose those
        very words for the operator who typed them.
        """
        err = _load_error(
            tmp_config_dir / "host_less_url.toml",
            f'[paperless]\nurl = "{url}"\n',
        )
        assert _only_body_line(err) == (
            "  [paperless] url: Value error, must be an http or https address "
            "that names a host"
        )

    @pytest.mark.usefixtures("no_discovered_config")
    def test_a_malformed_url_from_the_environment_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same rule holds for ``SANELESS_PAPERLESS__URL``."""
        monkeypatch.setenv("SANELESS_PAPERLESS__URL", "paperless:8000")
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        line = _env_line(exc_info.value, "SANELESS_PAPERLESS__URL")
        assert _URL_SHAPE_RULE in line
        TestConfigErrorsNeverEchoValues._assert_value_absent(
            exc_info.value, "paperless:8000"
        )

    def test_an_invalid_url_error_never_carries_the_parser_text(
        self, tmp_config_dir: Path
    ) -> None:
        """
        The URL parser's own complaint is never shown: it can quote a password.

        With an ``@`` after a ``/`` the parser reads ``scanner:hunter`` as a
        host and port and reports ``Invalid port: 'hunter'`` -- half of the
        password, in the one message an operator pastes into a bug report.
        """
        err = _load_error(
            tmp_config_dir / "invalid_url.toml",
            '[paperless]\nurl = "http://scanner:hunter/2secret@host"\n',
        )
        line = _only_body_line(err)
        assert line.startswith("  [paperless] url: ")
        assert "is not a valid URL" in line
        for fragment in ("hunter", "2secret"):
            TestConfigErrorsNeverEchoValues._assert_value_absent(err, fragment)

    def test_a_userinfo_url_is_refused_and_points_at_the_token(
        self, tmp_config_dir: Path
    ) -> None:
        """
        A ``user:password@`` in the URL is refused, never sent as credentials.

        The client would otherwise send it as Basic auth in place of the
        token, and the password would ride along wherever the URL is shown.
        """
        err = _load_error(
            tmp_config_dir / "userinfo_url.toml",
            '[paperless]\nurl = "http://u:pw-7f3a@paperless:8000"\n',
        )
        line = _only_body_line(err)
        assert line.startswith("  [paperless] url: ")
        assert _URL_USERINFO_RULE in line
        TestConfigErrorsNeverEchoValues._assert_value_absent(err, "pw-7f3a")

    def test_a_userinfo_url_never_reaches_repr_or_json(self) -> None:
        """
        No settings object can hold a URL with a password in it.

        ``repr`` and ``model_dump_json`` show ``url`` in the clear -- only the
        token is masked -- so the guarantee is that such a URL cannot load.
        """
        with pytest.raises(ValidationError) as exc_info:
            Settings(paperless=PaperlessConfig(url="http://u:pw-7f3a@paperless:8000"))
        [error] = exc_info.value.errors()
        assert error["loc"] == ("url",)
        assert _URL_USERINFO_RULE in error["msg"]

    @pytest.mark.parametrize(
        ("toml_value", "expected"),
        [
            pytest.param('" http://h \\n"', "http://h", id="padded"),
            pytest.param('""', "", id="unset"),
            pytest.param('"   "', "", id="blank-is-unset"),
            pytest.param(
                '"HTTP://Host:8000/sub/"', "HTTP://Host:8000/sub/", id="upper-case"
            ),
        ],
    )
    def test_a_usable_url_loads_stripped(
        self, tmp_config_dir: Path, toml_value: str, expected: str
    ) -> None:
        """Surrounding whitespace goes, the rest is kept as written."""
        config_file = tmp_config_dir / "good_url.toml"
        config_file.write_text(f"[paperless]\nurl = {toml_value}\n")
        settings = load_settings(config_path=str(config_file))
        assert settings.paperless.url == expected

    @pytest.mark.usefixtures("no_discovered_config")
    def test_whitespace_around_an_environment_url_is_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``SANELESS_PAPERLESS__URL`` is stripped like the file value."""
        monkeypatch.setenv("SANELESS_PAPERLESS__URL", " http://h \n")
        settings = load_settings()
        assert settings.paperless.url == "http://h"


class _SearchDirs(NamedTuple):
    """The three directories a patched config search looks in, in order."""

    cwd: Path
    xdg: Path
    etc: Path


@pytest.fixture
def patched_search_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _SearchDirs:
    """
    Redirect all three config search candidates into tmp_path (Phase 37 CFG-01).

    The real third candidate is ``/etc/saneless``, which no test may create,
    write or chmod; patching the search list keeps the whole search, including
    the stale-file probe, inside ``tmp_path``. The first candidate stays
    relative, exactly as the real one is, so an assertion that the log prints
    absolute paths is able to fail.

    Returns:
        The three directories, in search order.

    """
    dirs = _SearchDirs(
        cwd=tmp_path / "cwd",
        xdg=tmp_path / "xdg" / "saneless",
        etc=tmp_path / "etc" / "saneless",
    )
    for directory in dirs:
        directory.mkdir(parents=True)
    monkeypatch.chdir(dirs.cwd)
    candidates = (
        Path(config_mod.CONFIG_FILENAME),
        dirs.xdg / config_mod.CONFIG_FILENAME,
        dirs.etc / config_mod.CONFIG_FILENAME,
    )
    monkeypatch.setattr(config_mod, "config_search_paths", lambda: candidates)
    return dirs


_MINIMAL_TOML = "[profiles.default]\n"
"""A config file with nothing in it that any assertion here looks at."""

_STALE_TOML = (
    "[paperless]\n"
    'token = "real-token-from-stale"\n'
    'url = "http://paperless.example:8000"\n'
)
"""A valid old-name file whose values must never reach the settings."""


class TestConfigDiscovery:
    """
    What config discovery found is recorded once and derived once (Phase 37).

    An operator who mounted ``/etc/saneless/saneless.toml`` got no config at
    all, because that directory was searched for the other spelling, and no
    surface named the cause. Discovery now records every candidate it searched,
    the file it loaded, and any old-name file left beside a candidate -- which
    it stats and never opens.
    """

    @staticmethod
    def _stale_in(directory: Path) -> Path:
        """
        Return the old-name file path inside ``directory``.

        Args:
            directory: A searched directory.

        Returns:
            The old-name sibling path, which need not exist.

        """
        return directory / config_mod.LEGACY_CONFIG_FILENAME

    def test_xdg_new_name_is_discovered(
        self, patched_search_paths: _SearchDirs
    ) -> None:
        """A correctly named file under XDG loads (Phase 37 CFG-01)."""
        expected = patched_search_paths.xdg / config_mod.CONFIG_FILENAME
        expected.write_text(_MINIMAL_TOML)
        settings = load_settings()
        assert settings.config_path == expected
        assert (
            config_mod.config_file_state(settings)
            is vocabulary_mod.ConfigFileState.LOADED
        )

    def test_xdg_old_name_alone_is_not_loaded(
        self, patched_search_paths: _SearchDirs
    ) -> None:
        """An old-name file under XDG is recorded as stale, not loaded (D-08)."""
        stale = self._stale_in(patched_search_paths.xdg)
        stale.write_text(_MINIMAL_TOML)
        settings = load_settings()
        assert settings.config_path is None
        discovery = settings.config_discovery
        assert discovery is not None
        assert discovery.stale == (stale,)

    def test_etc_candidate_loads_the_new_name(
        self, patched_search_paths: _SearchDirs
    ) -> None:
        """
        The 2026-09-22 failure, inverted (Phase 37 CFG-01).

        A container mounting the app-named file into the system config
        directory got nothing, because that directory was searched for the
        other spelling. It now loads.
        """
        mounted = patched_search_paths.etc / config_mod.CONFIG_FILENAME
        mounted.write_text('[scanner]\nhost = "from-etc"\n\n[profiles.default]\n')
        settings = load_settings()
        assert settings.config_path == mounted
        assert settings.scanner.host == "from-etc"
        assert (
            config_mod.config_file_state(settings)
            is vocabulary_mod.ConfigFileState.LOADED
        )

    def test_stale_file_is_never_parsed(
        self, patched_search_paths: _SearchDirs
    ) -> None:
        """
        Invalid TOML under the old name does not break the load (D-08).

        Proof that the file is stat-ed and not read: were it parsed, this would
        raise.
        """
        stale = self._stale_in(patched_search_paths.etc)
        stale.write_text("this is [not toml")
        settings = load_settings()
        assert settings.config_path is None
        discovery = settings.config_discovery
        assert discovery is not None
        assert discovery.stale == (stale,)
        assert (
            config_mod.config_file_state(settings)
            is vocabulary_mod.ConfigFileState.STALE_ONLY
        )

    def test_stale_file_values_are_never_used(
        self, patched_search_paths: _SearchDirs
    ) -> None:
        """
        A live token under the old name populates nothing (D-08, Phase 37 CFG-03).

        The dangerous shape of "detection": stat-ing a file and then quietly
        merging it. The settings must still be the unconfigured defaults, and
        the file must be untouched.
        """
        stale = self._stale_in(patched_search_paths.xdg)
        stale.write_text(_STALE_TOML)
        before = stale.read_bytes()

        settings = load_settings()

        assert is_placeholder_token(settings.paperless.token.get_secret_value())
        assert settings.paperless.url != "http://paperless.example:8000"
        assert "real-token-from-stale" not in settings.model_dump_json()
        assert stale.read_bytes() == before

    def test_discover_config_lists_found_loaded_and_stale(self, tmp_path: Path) -> None:
        """
        ``found`` is every existing candidate and ``loaded`` is the first (D-05).

        Driven with a plain tuple, so nothing here depends on the environment.
        """
        first, second, third = (tmp_path / name for name in ("a", "b", "c"))
        for directory in (first, second, third):
            directory.mkdir()
        candidates = tuple(
            directory / config_mod.CONFIG_FILENAME
            for directory in (first, second, third)
        )
        candidates[1].write_text(_MINIMAL_TOML)
        candidates[2].write_text(_MINIMAL_TOML)
        self._stale_in(third).write_text(_MINIMAL_TOML)
        self._stale_in(first).write_text(_MINIMAL_TOML)

        discovery = config_mod.discover_config(candidates)

        assert discovery.searched == candidates
        assert discovery.found == (candidates[1], candidates[2])
        assert discovery.loaded == candidates[1]
        assert discovery.stale == (self._stale_in(first), self._stale_in(third))
        assert discovery.explicit is None

    def test_discover_config_with_nothing_present(self, tmp_path: Path) -> None:
        """An empty directory yields no found, no loaded and no stale."""
        candidates = (tmp_path / config_mod.CONFIG_FILENAME,)
        discovery = config_mod.discover_config(candidates)
        assert discovery.found == ()
        assert discovery.loaded is None
        assert discovery.stale == ()

    def test_unreadable_stale_file_still_counts(self, tmp_path: Path) -> None:
        """
        A stale file with no permissions is present, not absent (Phase 37 D-09).

        Present-but-unreadable is deliberately not a distinct state this phase:
        a stat is all the detection needs.
        """
        stale = self._stale_in(tmp_path)
        stale.write_text(_MINIMAL_TOML)
        stale.chmod(0o000)
        try:
            discovery = config_mod.discover_config(
                (tmp_path / config_mod.CONFIG_FILENAME,)
            )
            assert discovery.stale == (stale,)
        finally:
            stale.chmod(0o600)

    def test_state_loaded_with_leftover(
        self, patched_search_paths: _SearchDirs
    ) -> None:
        """A leftover beside a loaded file is its own state (D-10)."""
        (patched_search_paths.cwd / config_mod.CONFIG_FILENAME).write_text(
            _MINIMAL_TOML
        )
        self._stale_in(patched_search_paths.etc).write_text(_MINIMAL_TOML)
        settings = load_settings()
        assert (
            config_mod.config_file_state(settings)
            is vocabulary_mod.ConfigFileState.LOADED_WITH_LEFTOVER
        )

    def test_state_loaded_with_shadowed(
        self, patched_search_paths: _SearchDirs
    ) -> None:
        """A second distinct config file makes the shadowed state."""
        (patched_search_paths.cwd / config_mod.CONFIG_FILENAME).write_text(
            _MINIMAL_TOML
        )
        (patched_search_paths.etc / config_mod.CONFIG_FILENAME).write_text(
            _MINIMAL_TOML
        )
        settings = load_settings()
        assert (
            config_mod.config_file_state(settings)
            is vocabulary_mod.ConfigFileState.LOADED_WITH_SHADOWED
        )

    def test_shadowed_takes_precedence_over_a_leftover(
        self, patched_search_paths: _SearchDirs
    ) -> None:
        """
        Two config files and an old-name leftover is still the shadowed state.

        The shadowed file may be the edit an operator already made and is not
        read; the leftover is only a trap for the next one.
        """
        (patched_search_paths.cwd / config_mod.CONFIG_FILENAME).write_text(
            _MINIMAL_TOML
        )
        (patched_search_paths.etc / config_mod.CONFIG_FILENAME).write_text(
            _MINIMAL_TOML
        )
        self._stale_in(patched_search_paths.xdg).write_text(_MINIMAL_TOML)
        settings = load_settings()
        assert (
            config_mod.config_file_state(settings)
            is vocabulary_mod.ConfigFileState.LOADED_WITH_SHADOWED
        )

    def test_state_not_found(self, patched_search_paths: _SearchDirs) -> None:
        """Nothing anywhere is NOT_FOUND, which is a supported deployment (D-07)."""
        assert patched_search_paths.cwd.is_dir()
        settings = load_settings()
        assert (
            config_mod.config_file_state(settings)
            is vocabulary_mod.ConfigFileState.NOT_FOUND
        )

    def test_explicit_path_bypasses_stale_detection(self, tmp_path: Path) -> None:
        """
        ``--config`` searches nothing, so it detects nothing (Phase 37, deferred).

        Current behaviour, pinned so a later change to it is deliberate.
        """
        explicit = tmp_path / "elsewhere.toml"
        explicit.write_text(_MINIMAL_TOML)
        self._stale_in(tmp_path).write_text(_MINIMAL_TOML)

        settings = load_settings(str(explicit))

        discovery = settings.config_discovery
        assert discovery is not None
        assert discovery.explicit == explicit
        assert discovery.searched == ()
        assert discovery.stale == ()
        assert (
            config_mod.config_file_state(settings)
            is vocabulary_mod.ConfigFileState.LOADED
        )

    def test_directly_constructed_settings_are_not_found(self) -> None:
        """``Settings()`` recorded no discovery, so it loaded no file (D-05)."""
        settings = Settings()
        assert settings.config_discovery is None
        assert (
            config_mod.config_file_state(settings)
            is vocabulary_mod.ConfigFileState.NOT_FOUND
        )

    def test_settings_with_only_a_path_are_loaded(self, tmp_path: Path) -> None:
        """A recorded path without a discovery still reads as LOADED (D-05)."""
        settings = Settings()
        settings._config_path = tmp_path / config_mod.CONFIG_FILENAME
        assert (
            config_mod.config_file_state(settings)
            is vocabulary_mod.ConfigFileState.LOADED
        )

    def test_documented_spelling_is_positional_not_path_derived(
        self, tmp_path: Path
    ) -> None:
        """
        The three spellings come from search position (Phase 37 D-14).

        The status strip carries no filesystem path, so the stale file is named
        by the spelling the documentation uses. Driving it with tmp_path
        candidates proves the mapping is positional: a path-derived
        implementation would echo tmp_path back.
        """
        dirs = tuple(tmp_path / name for name in ("a", "b", "c"))
        candidates = tuple(directory / config_mod.CONFIG_FILENAME for directory in dirs)
        discovery = config_mod.discover_config(candidates)

        stale_spellings = [
            discovery.documented_spelling(self._stale_in(directory))
            for directory in dirs
        ]
        assert stale_spellings == [
            f"./{config_mod.LEGACY_CONFIG_FILENAME}",
            f"$XDG_CONFIG_HOME/saneless/{config_mod.LEGACY_CONFIG_FILENAME}",
            f"/etc/saneless/{config_mod.LEGACY_CONFIG_FILENAME}",
        ]
        assert [discovery.documented_spelling(p) for p in candidates] == [
            f"./{config_mod.CONFIG_FILENAME}",
            f"$XDG_CONFIG_HOME/saneless/{config_mod.CONFIG_FILENAME}",
            f"/etc/saneless/{config_mod.CONFIG_FILENAME}",
        ]
        assert all(str(tmp_path) not in text for text in stale_spellings)

    def test_documented_spelling_rejects_an_unsearched_path(
        self, tmp_path: Path
    ) -> None:
        """A path beside no candidate has no documented spelling (D-14)."""
        discovery = config_mod.discover_config(
            (tmp_path / "a" / config_mod.CONFIG_FILENAME,)
        )
        with pytest.raises(ValueError, match="searched"):
            discovery.documented_spelling(tmp_path / "z" / config_mod.CONFIG_FILENAME)

    def test_recorded_search_is_absolute(
        self, patched_search_paths: _SearchDirs
    ) -> None:
        """
        Every recorded path is absolute, and the search keeps its three places.

        The fixture's first candidate is relative, exactly as the real one is,
        so this fails for a recording that keeps the spelling it was given.
        """
        expected = patched_search_paths.cwd / config_mod.CONFIG_FILENAME
        expected.write_text(_MINIMAL_TOML)
        settings = load_settings()
        assert settings.config_path == expected
        discovery = settings.config_discovery
        assert discovery is not None
        assert discovery.searched == (
            expected,
            patched_search_paths.xdg / config_mod.CONFIG_FILENAME,
            patched_search_paths.etc / config_mod.CONFIG_FILENAME,
        )
        assert all(path.is_absolute() for path in discovery.searched)
        assert discovery.found == (expected,)
        assert discovery.loaded == expected

    def test_explicit_relative_path_is_recorded_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative ``--config`` path is recorded against the working directory."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "rel").mkdir()
        expected = tmp_path / "rel" / config_mod.CONFIG_FILENAME
        expected.write_text(_MINIMAL_TOML)
        settings = load_settings(config_path=f"rel/{config_mod.CONFIG_FILENAME}")
        assert settings.config_path == expected
        discovery = settings.config_discovery
        assert discovery is not None
        assert discovery.explicit == expected
        assert discovery.found == (expected,)
        assert discovery.loaded == expected

    def test_same_file_through_two_spellings_is_one_file(
        self, patched_search_paths: _SearchDirs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Running from inside the XDG directory finds one file, not two.

        The relative first candidate then names the XDG file.  Counting it
        twice would report the file in use as also shadowed by itself.
        """
        monkeypatch.chdir(patched_search_paths.xdg)
        the_file = patched_search_paths.xdg / config_mod.CONFIG_FILENAME
        the_file.write_text(_MINIMAL_TOML)
        settings = load_settings()
        discovery = settings.config_discovery
        assert discovery is not None
        assert discovery.found == (the_file,)
        assert discovery.loaded == the_file
        assert discovery.duplicates == (the_file,)
        assert len(discovery.searched) == len(config_mod.config_search_paths())
        assert (
            config_mod.config_file_state(settings)
            is vocabulary_mod.ConfigFileState.LOADED
        )

    def test_same_file_through_a_symlink_is_one_file(
        self, patched_search_paths: _SearchDirs
    ) -> None:
        """A ``./saneless.toml`` linked to the XDG file is that file."""
        target = patched_search_paths.xdg / config_mod.CONFIG_FILENAME
        target.write_text(_MINIMAL_TOML)
        link = patched_search_paths.cwd / config_mod.CONFIG_FILENAME
        link.symlink_to(target)
        settings = load_settings()
        discovery = settings.config_discovery
        assert discovery is not None
        assert discovery.found == (link,)
        assert discovery.loaded == link
        assert discovery.duplicates == (target,)
        assert (
            config_mod.config_file_state(settings)
            is vocabulary_mod.ConfigFileState.LOADED
        )

    def test_same_leftover_through_two_spellings_is_one_leftover(
        self, patched_search_paths: _SearchDirs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A superseded-name file seen through two spellings is named once."""
        monkeypatch.chdir(patched_search_paths.xdg)
        stale = self._stale_in(patched_search_paths.xdg)
        stale.write_text(_MINIMAL_TOML)
        discovery = load_settings().config_discovery
        assert discovery is not None
        assert discovery.stale == (stale,)

    def test_two_distinct_files_are_both_found(
        self, patched_search_paths: _SearchDirs
    ) -> None:
        """Two different files are two, in search order, and the first loads."""
        first = patched_search_paths.cwd / config_mod.CONFIG_FILENAME
        second = patched_search_paths.etc / config_mod.CONFIG_FILENAME
        first.write_text(_MINIMAL_TOML)
        second.write_text(_MINIMAL_TOML)
        discovery = load_settings().config_discovery
        assert discovery is not None
        assert discovery.found == (first, second)
        assert discovery.loaded == first
        assert discovery.duplicates == ()

    def test_search_order_is_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The working directory, then XDG, then ``/etc/saneless``."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert config_mod.config_search_paths() == (
            Path(config_mod.CONFIG_FILENAME),
            tmp_path / "xdg" / "saneless" / config_mod.CONFIG_FILENAME,
            Path("/etc/saneless") / config_mod.CONFIG_FILENAME,
        )


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


_ENV_JSON_RULE = "must be JSON (a list or table is written as JSON, for example [3, 7])"
"""The fixed text a variable holding invalid JSON is refused with."""


def _json_line(variable: str) -> str:
    """
    Return the rendered line refusing ``variable`` for invalid JSON.

    Args:
        variable: The environment variable name, as spelled.

    Returns:
        The indented line, as it appears in the ConfigError.

    """
    return f"  environment variable {variable!r}: {_ENV_JSON_RULE}"


class TestEnvironmentJson:
    """
    A variable holding invalid JSON is named, and the file is still checked.

    pydantic-settings refuses such a variable before any field is validated,
    with a message naming the field but not the variable. The loader used to
    turn that into one anonymous line and stop, so a CSV ``default_tags``
    variable hid every error in the file beside it.
    """

    def test_bad_json_variable_is_named_beside_every_file_error(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A CSV list variable, a key typo and a bad port give three lines."""
        variable = "SANELESS_PROFILES__DEFAULT__DEFAULT_TAGS"
        monkeypatch.setenv(variable, "3,7")
        config_file = tmp_config_dir / "json_and_file.toml"
        err = _load_error(
            config_file,
            '[paperless]\ntokne = "x"\n\n[output]\nweb_port = 70000\n\n'
            "[profiles.default]\n",
        )
        assert _error_lines(err) == [
            f"Configuration error in {config_file}:",
            "  [output] web_port: Input should be less than or equal to 65535",
            "  [paperless] unknown key 'tokne' (did you mean 'token'?); "
            "valid keys: url, token, consume_dir",
            _json_line(variable),
        ]

    def test_two_bad_json_variables_are_each_named(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each bad variable gets its own line, sorted with the file's lines."""
        monkeypatch.setenv("SANELESS_WEB__ALLOWED_HOSTS", "scan.example.com")
        monkeypatch.setenv("SANELESS_PROFILES__DEFAULT__DEFAULT_TAGS", "3,7")
        config_file = tmp_config_dir / "two_json.toml"
        err = _load_error(config_file, "[output]\nweb_port = 70000\n")
        assert _error_lines(err) == [
            f"Configuration error in {config_file}:",
            "  [output] web_port: Input should be less than or equal to 65535",
            _json_line("SANELESS_PROFILES__DEFAULT__DEFAULT_TAGS"),
            _json_line("SANELESS_WEB__ALLOWED_HOSTS"),
        ]

    @pytest.mark.usefixtures("no_discovered_config")
    def test_bad_json_section_holding_the_token_never_shows_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed JSON ``[paperless]`` variable is named, its token is not."""
        monkeypatch.setenv("SANELESS_PAPERLESS", '{"token": "sekrit"')
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        assert _error_lines(exc_info.value) == [
            "Configuration error (defaults and environment):",
            _json_line("SANELESS_PAPERLESS"),
        ]
        assert "sekrit" not in str(exc_info.value)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__

    @pytest.mark.usefixtures("no_discovered_config")
    def test_bad_json_variable_is_named_with_no_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no file loaded, the line sits under the defaults header."""
        variable = "SANELESS_PROFILES__DEFAULT__DEFAULT_TAGS"
        monkeypatch.setenv(variable, "3,7")
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        assert _error_lines(exc_info.value) == [
            "Configuration error (defaults and environment):",
            _json_line(variable),
        ]

    @pytest.mark.usefixtures("no_discovered_config")
    def test_good_json_variable_still_loads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A JSON list in the same variable loads as the list."""
        monkeypatch.setenv("SANELESS_PROFILES__DEFAULT__DEFAULT_TAGS", "[3, 7]")
        settings = load_settings()
        assert settings.profiles["default"].default_tags == [3, 7]

    @pytest.mark.usefixtures("no_discovered_config")
    def test_lowercase_bad_json_variable_is_named_as_spelled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lower-case variable is read, so it is refused under its own name."""
        variable = "saneless_profiles__default__default_tags"
        monkeypatch.setenv(variable, "3,7")
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        assert _json_line(variable) in _error_lines(exc_info.value)

    def test_a_refusal_no_variable_explains_is_still_redacted(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        With no single variable to blame, the upstream line is kept, redacted.

        The refusal is forced while every variable parses on its own, so the
        per-variable probe finds nothing and the fallback line is what renders.
        """
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", _HOSTILE_SECRET)

        def hostile_env_contribution() -> dict[str, object]:
            msg = f"error parsing value {_HOSTILE_SECRET} for field 'paperless'"
            raise SettingsError(msg)

        monkeypatch.setattr(config_mod, "_env_contribution", hostile_env_contribution)
        err = _load_error(tmp_config_dir / "unexplained.toml", "[profiles.default]\n")
        body = _error_lines(err)[1:]
        assert len(body) == 1
        assert body[0].startswith("  environment: error parsing value ")
        assert _HOSTILE_SECRET not in str(err)


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

    @staticmethod
    def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
        """Return the WARNING messages saneless.config emitted."""
        return [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING and record.name == "saneless.config"
        ]

    @staticmethod
    def _emit(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
        """
        Run the startup config log with both levels captured.

        Args:
            settings: The loaded settings to report on.
            caplog: The capture fixture to record into.

        """
        with caplog.at_level(logging.INFO, logger="saneless.config"):
            config_mod.log_config_sources(settings)

    def test_configuration_line_is_absolute(
        self,
        patched_search_paths: _SearchDirs,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The file found through the relative first candidate is logged absolute."""
        loaded = patched_search_paths.cwd / config_mod.CONFIG_FILENAME
        loaded.write_text(_MINIMAL_TOML)
        settings = load_settings()

        self._emit(settings, caplog)

        messages = self._info(caplog)
        assert len(messages) == 1
        assert messages[0].startswith(f"Configuration: {loaded}; ")

    def test_configuration_line_absolutizes_a_recorded_relative_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A path recorded relative is still logged absolute.

        Settings built directly may carry any path; the log line is read
        without the working directory, so it never prints a relative one.
        """
        monkeypatch.chdir(tmp_path)
        settings = Settings()
        settings._config_path = Path(config_mod.CONFIG_FILENAME)

        self._emit(settings, caplog)

        messages = self._info(caplog)
        assert len(messages) == 1
        expected = tmp_path / config_mod.CONFIG_FILENAME
        assert messages[0].startswith(f"Configuration: {expected}; ")

    def test_shadowed_config_logs_a_warning_naming_both(
        self,
        patched_search_paths: _SearchDirs,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        One WARNING per config file that is not read, naming it and the winner.

        Both absolute, because the log is read on the machine, where the
        path is the half an operator can act on.
        """
        loaded = patched_search_paths.cwd / config_mod.CONFIG_FILENAME
        unread = patched_search_paths.etc / config_mod.CONFIG_FILENAME
        loaded.write_text(_MINIMAL_TOML)
        unread.write_text(_MINIMAL_TOML)
        settings = load_settings()

        self._emit(settings, caplog)

        assert self._warnings(caplog) == [
            f"Not reading {unread}: {loaded} is in use and was found first; "
            f"move anything you still need from it into {loaded}, or delete "
            "it if it is not deliberate"
        ]

    def test_every_unread_config_file_gets_its_own_warning(
        self,
        patched_search_paths: _SearchDirs,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Three files found gives two warnings, in search order."""
        for directory in patched_search_paths:
            (directory / config_mod.CONFIG_FILENAME).write_text(_MINIMAL_TOML)
        settings = load_settings()

        self._emit(settings, caplog)

        warnings = self._warnings(caplog)
        assert len(warnings) == 2
        assert warnings[0].startswith(
            f"Not reading {patched_search_paths.xdg / config_mod.CONFIG_FILENAME}:"
        )
        assert warnings[1].startswith(
            f"Not reading {patched_search_paths.etc / config_mod.CONFIG_FILENAME}:"
        )

    def test_one_file_seen_twice_logs_no_shadow_warning(
        self,
        patched_search_paths: _SearchDirs,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Running from inside the XDG directory finds one file and warns of none."""
        monkeypatch.chdir(patched_search_paths.xdg)
        (patched_search_paths.xdg / config_mod.CONFIG_FILENAME).write_text(
            _MINIMAL_TOML
        )
        settings = load_settings()

        self._emit(settings, caplog)

        assert self._warnings(caplog) == []

    @pytest.mark.usefixtures("patched_search_paths")
    def test_no_config_logs_every_searched_path_absolute(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        With nothing found, the one INFO line says where it looked (D-11, CFG-02).

        The 2026-09-22 log said only "no config file" and the operator had to
        guess which three paths that meant. The relative first candidate is
        printed absolute, because a relative path in a log is worth nothing
        without the working directory.
        """
        secret = "tok-SECRET-51ab"
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", secret)
        settings = load_settings()

        self._emit(settings, caplog)

        messages = self._info(caplog)
        assert len(messages) == 1
        assert "no config file; defaults + environment" in messages[0]
        positions = [
            messages[0].find(str(path.absolute()))
            for path in config_mod.config_search_paths()
        ]
        assert all(position >= 0 for position in positions)
        assert positions == sorted(positions)
        assert self._warnings(caplog) == []
        assert secret not in messages[0]

    def test_loaded_config_does_not_log_the_search_list(
        self,
        patched_search_paths: _SearchDirs,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A successful load names one file and no others (D-11)."""
        loaded = patched_search_paths.xdg / config_mod.CONFIG_FILENAME
        loaded.write_text(_MINIMAL_TOML)
        settings = load_settings()

        self._emit(settings, caplog)

        messages = self._info(caplog)
        assert len(messages) == 1
        assert str(loaded) in messages[0]
        assert str(patched_search_paths.etc) not in messages[0]
        assert str(patched_search_paths.cwd) not in messages[0]
        assert self._warnings(caplog) == []

    def test_stale_only_warns_once_per_file_with_its_rename(
        self,
        patched_search_paths: _SearchDirs,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        Every old-name file is named with the rename to perform (Phase 37 CFG-03).

        This is the line that would have ended the 2026-09-22 evening.
        """
        stale = [
            directory / config_mod.LEGACY_CONFIG_FILENAME
            for directory in (patched_search_paths.xdg, patched_search_paths.etc)
        ]
        for path in stale:
            path.write_text(_MINIMAL_TOML)
        settings = load_settings()

        self._emit(settings, caplog)

        assert len(self._info(caplog)) == 1
        warnings = self._warnings(caplog)
        assert len(warnings) == len(stale)
        for path, text in zip(stale, warnings, strict=True):
            assert str(path.absolute()) in text
            assert str(path.with_name(config_mod.CONFIG_FILENAME).absolute()) in text
            assert "rename" in text

    def test_leftover_beside_a_loaded_file_says_move_then_delete(
        self,
        patched_search_paths: _SearchDirs,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A leftover may hold the only copy of the token (Phase 37 D-17).

        In the documented container layout the generated profiles are written
        to a different directory than the mounted config, so the old-name file
        left behind after an upgrade can be the only place the URL and token
        survive. The next step must never be a bare "delete it".
        """
        loaded = patched_search_paths.cwd / config_mod.CONFIG_FILENAME
        loaded.write_text(_MINIMAL_TOML)
        leftover = patched_search_paths.etc / config_mod.LEGACY_CONFIG_FILENAME
        leftover.write_text(_STALE_TOML)
        settings = load_settings()

        self._emit(settings, caplog)

        assert len(self._info(caplog)) == 1
        warnings = self._warnings(caplog)
        assert len(warnings) == 1
        assert str(leftover.absolute()) in warnings[0]
        assert settings.config_path is not None
        assert str(settings.config_path.absolute()) in warnings[0]
        assert "move anything" in warnings[0]
        assert "then delete" in warnings[0]

    def test_stale_warnings_never_carry_a_secret(
        self,
        patched_search_paths: _SearchDirs,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Paths and key names reach the log; values never do (T-37-02)."""
        secret = "tok-SECRET-51ab"
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", secret)
        stale = patched_search_paths.etc / config_mod.LEGACY_CONFIG_FILENAME
        stale.write_text(_STALE_TOML)
        settings = load_settings()

        self._emit(settings, caplog)

        records = self._info(caplog) + self._warnings(caplog)
        assert records
        for text in records:
            assert secret not in text
            assert "real-token-from-stale" not in text


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
        directory = tmp_path / config_mod.CONFIG_FILENAME
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
    """ProfileConfig's one blank-page knob: the ink-coverage threshold."""

    def test_custom_threshold_values(self, tmp_config_dir: Path) -> None:
        """ProfileConfig accepts a custom coverage threshold from TOML."""
        toml_content = """\
[profiles.default]
source = "ADF"
empty_page_coverage_threshold = 0.01
"""
        config_file = tmp_config_dir / "thresholds.toml"
        config_file.write_text(toml_content)
        settings = load_settings(config_path=str(config_file))
        profile = settings.profiles["default"]
        assert profile.empty_page_coverage_threshold == 0.01

    def test_the_default_keeps_sparse_pages(self) -> None:
        """The shipped default is a thousandth of a per cent of the inset."""
        assert ProfileConfig().empty_page_coverage_threshold == 0.001

    @pytest.mark.parametrize(
        "key", ["empty_page_mean_threshold", "empty_page_stddev_threshold"]
    )
    def test_the_removed_thresholds_fail_to_load(
        self, tmp_config_dir: Path, key: str
    ) -> None:
        """A profile still setting a removed key is refused by name, with no alias."""
        err = _load_error(
            tmp_config_dir / "removed.toml",
            f"[profiles.default]\n{key} = 250.0\n",
        )
        assert any(
            line.startswith(f"  [profiles.default] unknown key '{key}'")
            for line in _error_lines(err)
        ), str(err)

    @pytest.mark.parametrize(
        ("value", "bound"),
        [(-0.1, "greater_than_equal"), (100.1, "less_than_equal")],
    )
    def test_a_threshold_outside_a_percentage_is_refused(
        self, value: float, bound: str
    ) -> None:
        """
        Below zero or above a hundred per cent is not a coverage.

        Asserted by the error's type, not only its field name: an unknown key
        is refused under the same name, and that is not the refusal meant.
        """
        with pytest.raises(ValidationError) as exc_info:
            ProfileConfig(empty_page_coverage_threshold=value)
        (error,) = exc_info.value.errors()
        assert error["loc"] == ("empty_page_coverage_threshold",)
        assert error["type"] == bound

    @pytest.mark.parametrize("value", [0.0, 100.0])
    def test_the_bounds_are_inclusive(self, value: float) -> None:
        """Zero (remove only inkless pages) and a hundred are both accepted."""
        profile = ProfileConfig(empty_page_coverage_threshold=value)
        assert profile.empty_page_coverage_threshold == value


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


class TestOperatorWaitTimeoutSeconds:
    """
    OutputConfig operator_wait_timeout_seconds field.

    The one bound on every wait for a person: the manual-duplex flip and each
    prompt of a multi-page scan.  The wait is bounded to one second through
    one day.  Zero or a negative value makes the wait expire at once, failing
    every manual-duplex job right after pass A; a value above
    ``threading.TIMEOUT_MAX`` makes ``Event.wait`` raise ``OverflowError`` at
    the same point.  Both are rejected at load, where the CLI reports a
    configuration error.  The key was renamed from ``flip_timeout_seconds``
    with no alias, so the old key is refused like any other unknown key.
    """

    def test_operator_wait_timeout_seconds_default(self) -> None:
        """Settings default the operator wait to ten minutes."""
        assert Settings().output.operator_wait_timeout_seconds == 600

    @pytest.mark.parametrize("value", [0, -5, 86_401])
    def test_operator_wait_timeout_seconds_out_of_bounds_rejected(
        self, value: int
    ) -> None:
        """Zero, a negative value and anything above a day fail validation."""
        with pytest.raises(ValidationError, match="operator_wait_timeout_seconds"):
            OutputConfig(operator_wait_timeout_seconds=value)

    @pytest.mark.parametrize("value", [1, 86_400])
    def test_operator_wait_timeout_seconds_bounds_accepted(self, value: int) -> None:
        """One second and exactly one day are the inclusive bounds."""
        output = OutputConfig(operator_wait_timeout_seconds=value)
        assert output.operator_wait_timeout_seconds == value

    def test_operator_wait_timeout_seconds_zero_in_toml_rejected(
        self, tmp_config_dir: Path
    ) -> None:
        """A TOML ``operator_wait_timeout_seconds = 0`` fails at load."""
        toml_content = """\
[output]
operator_wait_timeout_seconds = 0

[profiles.default]
"""
        config_file = tmp_config_dir / "operator_wait_zero.toml"
        config_file.write_text(toml_content)
        with pytest.raises(ConfigError, match="operator_wait_timeout_seconds"):
            load_settings(config_path=str(config_file))

    def test_operator_wait_timeout_seconds_from_toml(
        self, tmp_config_dir: Path
    ) -> None:
        """The timeout is read from the [output] section."""
        toml_content = """\
[output]
operator_wait_timeout_seconds = 90

[profiles.default]
"""
        config_file = tmp_config_dir / "operator_wait.toml"
        config_file.write_text(toml_content)
        settings = load_settings(config_path=str(config_file))
        assert settings.output.operator_wait_timeout_seconds == 90

    def test_old_flip_timeout_key_is_rejected_with_a_hint(
        self, tmp_config_dir: Path
    ) -> None:
        """The pre-rename key fails to load and points at the new name."""
        err = _load_error(
            tmp_config_dir / "old_flip_timeout.toml",
            "[output]\nflip_timeout_seconds = 30\n\n[profiles.default]\n",
        )
        matching = [
            line
            for line in _error_lines(err)
            if "unknown key 'flip_timeout_seconds'" in line
        ]
        assert len(matching) == 1
        assert "did you mean 'operator_wait_timeout_seconds'" in matching[0]

    @pytest.mark.usefixtures("no_discovered_config")
    def test_operator_wait_timeout_seconds_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``SANELESS_OUTPUT__OPERATOR_WAIT_TIMEOUT_SECONDS`` sets the field."""
        monkeypatch.setenv("SANELESS_OUTPUT__OPERATOR_WAIT_TIMEOUT_SECONDS", "42")
        settings = load_settings()
        assert settings.output.operator_wait_timeout_seconds == 42

    @pytest.mark.usefixtures("no_discovered_config")
    def test_old_flip_timeout_variable_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pre-rename environment variable fails at startup with a hint."""
        monkeypatch.setenv("SANELESS_OUTPUT__FLIP_TIMEOUT_SECONDS", "42")
        with pytest.raises(ConfigError) as exc_info:
            load_settings()
        line = _env_line(exc_info.value, "SANELESS_OUTPUT__FLIP_TIMEOUT_SECONDS")
        assert "unknown key 'flip_timeout_seconds' in [output]" in line
        assert "did you mean 'operator_wait_timeout_seconds'" in line


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


def _output_bound_line(
    tmp_config_dir: Path, key: str, value: int, name: str
) -> list[str]:
    """
    Load ``[output] <key> = <value>`` and return the rendered error lines.

    Args:
        tmp_config_dir: Where to write the TOML.
        key: The ``[output]`` key to set.
        value: The out-of-range value to give it.
        name: The config file's stem.

    Returns:
        The load's ConfigError message, split into lines.

    """
    err = _load_error(
        tmp_config_dir / f"{name}.toml",
        f"[output]\n{key} = {value}\n\n[profiles.default]\n",
    )
    return _error_lines(err)


class TestHistoryRetentionDays:
    """
    OutputConfig history_retention_days is bounded to 1..36,500.

    The worker prunes finished jobs older than this many days.  Zero would
    prune every finished job on the next pass, and a huge value overflows the
    prune's date arithmetic on every pass.  About a century is how "keep
    forever" is spelled.
    """

    @pytest.mark.parametrize("value", [0, -1, 36_501])
    def test_history_retention_days_out_of_range_rejected(self, value: int) -> None:
        """Zero, a negative value and anything above about a century fail."""
        with pytest.raises(ValidationError, match="history_retention_days"):
            OutputConfig(history_retention_days=value)

    @pytest.mark.parametrize("value", [1, 36_500])
    def test_history_retention_days_bounds_accepted(self, value: int) -> None:
        """One day and 36,500 days are the inclusive bounds."""
        output = OutputConfig(history_retention_days=value)
        assert output.history_retention_days == value

    def test_history_retention_days_zero_in_toml_rejected(
        self, tmp_config_dir: Path
    ) -> None:
        """A TOML ``history_retention_days = 0`` fails at load, naming the key."""
        lines = _output_bound_line(
            tmp_config_dir, "history_retention_days", 0, "retention_zero"
        )
        assert (
            "  [output] history_retention_days: "
            "Input should be greater than or equal to 1"
        ) in lines

    @pytest.mark.usefixtures("no_discovered_config")
    def test_history_retention_days_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``SANELESS_OUTPUT__HISTORY_RETENTION_DAYS`` sets the field."""
        monkeypatch.setenv("SANELESS_OUTPUT__HISTORY_RETENTION_DAYS", "42")
        assert load_settings().output.history_retention_days == 42


class TestHistoryMaxRows:
    """
    OutputConfig history_max_rows is bounded to 1..1,000,000.

    Zero would delete the whole job history on the next prune, and a value
    past SQLite's integer range makes every prune raise.
    """

    @pytest.mark.parametrize("value", [0, -1, 1_000_001])
    def test_history_max_rows_out_of_range_rejected(self, value: int) -> None:
        """Zero, a negative value and anything above a million fail."""
        with pytest.raises(ValidationError, match="history_max_rows"):
            OutputConfig(history_max_rows=value)

    @pytest.mark.parametrize("value", [1, 1_000_000])
    def test_history_max_rows_bounds_accepted(self, value: int) -> None:
        """One row and a million rows are the inclusive bounds."""
        assert OutputConfig(history_max_rows=value).history_max_rows == value

    def test_history_max_rows_zero_in_toml_rejected(self, tmp_config_dir: Path) -> None:
        """A TOML ``history_max_rows = 0`` fails at load, naming the key."""
        lines = _output_bound_line(
            tmp_config_dir, "history_max_rows", 0, "max_rows_zero"
        )
        assert (
            "  [output] history_max_rows: Input should be greater than or equal to 1"
        ) in lines

    @pytest.mark.usefixtures("no_discovered_config")
    def test_history_max_rows_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``SANELESS_OUTPUT__HISTORY_MAX_ROWS`` sets the field."""
        monkeypatch.setenv("SANELESS_OUTPUT__HISTORY_MAX_ROWS", "42")
        assert load_settings().output.history_max_rows == 42


class TestPaperlessTaskTimeout:
    """
    OutputConfig paperless_task_timeout is bounded to one second..one day.

    Zero fails every accepted upload as unconfirmed the moment its first
    poll comes back without a terminal status.
    """

    @pytest.mark.parametrize("value", [0, -1, 86_401])
    def test_paperless_task_timeout_out_of_range_rejected(self, value: int) -> None:
        """Zero, a negative value and anything above a day fail."""
        with pytest.raises(ValidationError, match="paperless_task_timeout"):
            OutputConfig(paperless_task_timeout=value)

    @pytest.mark.parametrize("value", [1, 86_400])
    def test_paperless_task_timeout_bounds_accepted(self, value: int) -> None:
        """One second and exactly one day are the inclusive bounds."""
        output = OutputConfig(paperless_task_timeout=value)
        assert output.paperless_task_timeout == value

    def test_paperless_task_timeout_zero_in_toml_rejected(
        self, tmp_config_dir: Path
    ) -> None:
        """A TOML ``paperless_task_timeout = 0`` fails at load, naming the key."""
        lines = _output_bound_line(
            tmp_config_dir, "paperless_task_timeout", 0, "task_timeout_zero"
        )
        assert (
            "  [output] paperless_task_timeout: "
            "Input should be greater than or equal to 1"
        ) in lines

    @pytest.mark.usefixtures("no_discovered_config")
    def test_paperless_task_timeout_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``SANELESS_OUTPUT__PAPERLESS_TASK_TIMEOUT`` sets the field."""
        monkeypatch.setenv("SANELESS_OUTPUT__PAPERLESS_TASK_TIMEOUT", "42")
        assert load_settings().output.paperless_task_timeout == 42


class TestLogMaxBytesBound:
    """
    OutputConfig log_max_bytes is at least one byte.

    Zero is the rotating handler's "never rotate", so the log file would grow
    without bound; it is not a way to spell that here.
    """

    @pytest.mark.parametrize("value", [0, -1])
    def test_log_max_bytes_out_of_range_rejected(self, value: int) -> None:
        """Zero and a negative size fail."""
        with pytest.raises(ValidationError, match="log_max_bytes"):
            OutputConfig(log_max_bytes=value)

    @pytest.mark.parametrize("value", [1])
    def test_log_max_bytes_bounds_accepted(self, value: int) -> None:
        """One byte is the inclusive floor."""
        assert OutputConfig(log_max_bytes=value).log_max_bytes == value

    def test_log_max_bytes_zero_in_toml_rejected(self, tmp_config_dir: Path) -> None:
        """A TOML ``log_max_bytes = 0`` fails at load, naming the key."""
        lines = _output_bound_line(tmp_config_dir, "log_max_bytes", 0, "max_bytes")
        assert (
            "  [output] log_max_bytes: Input should be greater than or equal to 1"
        ) in lines

    @pytest.mark.usefixtures("no_discovered_config")
    def test_log_max_bytes_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``SANELESS_OUTPUT__LOG_MAX_BYTES`` sets the field."""
        monkeypatch.setenv("SANELESS_OUTPUT__LOG_MAX_BYTES", "42")
        assert load_settings().output.log_max_bytes == 42


class TestLogBackupCount:
    """
    OutputConfig log_backup_count is bounded to 1..1,000.

    Zero keeps no backup at all, and a huge count makes every rollover walk
    that many file names.
    """

    @pytest.mark.parametrize("value", [0, -1, 1_001])
    def test_log_backup_count_out_of_range_rejected(self, value: int) -> None:
        """Zero, a negative count and anything above a thousand fail."""
        with pytest.raises(ValidationError, match="log_backup_count"):
            OutputConfig(log_backup_count=value)

    @pytest.mark.parametrize("value", [1, 1_000])
    def test_log_backup_count_bounds_accepted(self, value: int) -> None:
        """One backup and a thousand backups are the inclusive bounds."""
        assert OutputConfig(log_backup_count=value).log_backup_count == value

    def test_log_backup_count_zero_in_toml_rejected(self, tmp_config_dir: Path) -> None:
        """A TOML ``log_backup_count = 0`` fails at load, naming the key."""
        lines = _output_bound_line(
            tmp_config_dir, "log_backup_count", 0, "backup_count_zero"
        )
        assert (
            "  [output] log_backup_count: Input should be greater than or equal to 1"
        ) in lines

    @pytest.mark.usefixtures("no_discovered_config")
    def test_log_backup_count_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``SANELESS_OUTPUT__LOG_BACKUP_COUNT`` sets the field."""
        monkeypatch.setenv("SANELESS_OUTPUT__LOG_BACKUP_COUNT", "42")
        assert load_settings().output.log_backup_count == 42


class TestMinFreeSpaceMb:
    """
    OutputConfig min_free_space_mb is never negative.

    Zero is a real setting (no reserve); a negative reserve means nothing.
    """

    @pytest.mark.parametrize("value", [-1])
    def test_min_free_space_mb_out_of_range_rejected(self, value: int) -> None:
        """A negative reserve fails."""
        with pytest.raises(ValidationError, match="min_free_space_mb"):
            OutputConfig(min_free_space_mb=value)

    @pytest.mark.parametrize("value", [0])
    def test_min_free_space_mb_bounds_accepted(self, value: int) -> None:
        """Zero is the inclusive floor."""
        assert OutputConfig(min_free_space_mb=value).min_free_space_mb == value

    def test_min_free_space_mb_negative_in_toml_rejected(
        self, tmp_config_dir: Path
    ) -> None:
        """A TOML ``min_free_space_mb = -1`` fails at load, naming the key."""
        lines = _output_bound_line(
            tmp_config_dir, "min_free_space_mb", -1, "free_space_negative"
        )
        assert (
            "  [output] min_free_space_mb: Input should be greater than or equal to 0"
        ) in lines

    @pytest.mark.usefixtures("no_discovered_config")
    def test_min_free_space_mb_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``SANELESS_OUTPUT__MIN_FREE_SPACE_MB`` sets the field."""
        monkeypatch.setenv("SANELESS_OUTPUT__MIN_FREE_SPACE_MB", "42")
        assert load_settings().output.min_free_space_mb == 42


class TestPaperlessCacheTtlBound:
    """
    OutputConfig paperless_cache_ttl_seconds is never negative.

    Zero is a real setting (never cache); a negative lifetime means nothing.
    """

    @pytest.mark.parametrize("value", [-1])
    def test_paperless_cache_ttl_out_of_range_rejected(self, value: int) -> None:
        """A negative lifetime fails."""
        with pytest.raises(ValidationError, match="paperless_cache_ttl_seconds"):
            OutputConfig(paperless_cache_ttl_seconds=value)

    @pytest.mark.parametrize("value", [0])
    def test_paperless_cache_ttl_bounds_accepted(self, value: int) -> None:
        """Zero is the inclusive floor."""
        output = OutputConfig(paperless_cache_ttl_seconds=value)
        assert output.paperless_cache_ttl_seconds == value

    def test_paperless_cache_ttl_negative_in_toml_rejected(
        self, tmp_config_dir: Path
    ) -> None:
        """A TOML ``paperless_cache_ttl_seconds = -1`` fails at load."""
        lines = _output_bound_line(
            tmp_config_dir, "paperless_cache_ttl_seconds", -1, "cache_ttl_negative"
        )
        assert (
            "  [output] paperless_cache_ttl_seconds: "
            "Input should be greater than or equal to 0"
        ) in lines

    @pytest.mark.usefixtures("no_discovered_config")
    def test_paperless_cache_ttl_from_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``SANELESS_OUTPUT__PAPERLESS_CACHE_TTL_SECONDS`` sets the field."""
        monkeypatch.setenv("SANELESS_OUTPUT__PAPERLESS_CACHE_TTL_SECONDS", "42")
        assert load_settings().output.paperless_cache_ttl_seconds == 42


class TestResolutionBound:
    """
    ProfileConfig resolution is bounded to 1..12,800 dpi.

    Zero or a negative resolution cannot be scanned at all.  12,800 dpi is
    the highest value any SANE backend offers, so the ceiling refuses no real
    device.
    """

    @pytest.mark.parametrize("value", [0, -1, 12_801])
    def test_resolution_out_of_range_rejected(self, value: int) -> None:
        """Zero, a negative value and anything above 12,800 dpi fail."""
        with pytest.raises(ValidationError, match="resolution"):
            ProfileConfig(resolution=value)

    @pytest.mark.parametrize("value", [1, 12_800])
    def test_resolution_bounds_accepted(self, value: int) -> None:
        """One dpi and 12,800 dpi are the inclusive bounds."""
        assert ProfileConfig(resolution=value).resolution == value

    def test_resolution_zero_in_toml_rejected(self, tmp_config_dir: Path) -> None:
        """A TOML ``resolution = 0`` fails at load, naming the profile."""
        err = _load_error(
            tmp_config_dir / "resolution_zero.toml",
            "[profiles.default]\nresolution = 0\n",
        )
        assert (
            "  [profiles.default] resolution: "
            "Input should be greater than or equal to 1"
        ) in _error_lines(err)

    @pytest.mark.usefixtures("no_discovered_config")
    def test_resolution_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``SANELESS_PROFILES__DEFAULT__RESOLUTION`` sets the field."""
        monkeypatch.setenv("SANELESS_PROFILES__DEFAULT__RESOLUTION", "600")
        assert load_settings().profiles["default"].resolution == 600


_BOOL_NOT_NUMBER = "must be a number, not true or false"
"""The fixed refusal every numeric setting gives a TOML boolean."""


class TestBoolIsNotANumber:
    """
    A TOML boolean is refused by every numeric setting.

    pydantic's lax mode reads ``true`` as 1, so ``web_port = true`` used to
    bind port 1 and ``resolution = true`` scanned at 1 dpi.  The refusal is a
    fixed sentence that carries no value, and the plain strings the
    ``SANELESS_*`` variables supply still load.
    """

    @pytest.mark.parametrize(
        ("toml_content", "expected"),
        [
            pytest.param(
                "[output]\nweb_port = true\n\n[profiles.default]\n",
                f"  [output] web_port: {_BOOL_NOT_NUMBER}",
                id="web_port",
            ),
            pytest.param(
                "[output]\nhistory_max_rows = false\n\n[profiles.default]\n",
                f"  [output] history_max_rows: {_BOOL_NOT_NUMBER}",
                id="history_max_rows",
            ),
            pytest.param(
                "[profiles.default]\nresolution = true\n",
                f"  [profiles.default] resolution: {_BOOL_NOT_NUMBER}",
                id="resolution",
            ),
            pytest.param(
                "[profiles.default]\ndefault_tags = [true]\n",
                f"  [profiles.default] default_tags[0]: {_BOOL_NOT_NUMBER}",
                id="default_tags",
            ),
            pytest.param(
                "[profiles.default]\ndefault_correspondent = true\n",
                f"  [profiles.default] default_correspondent: {_BOOL_NOT_NUMBER}",
                id="default_correspondent",
            ),
            pytest.param(
                "[profiles.default]\nempty_page_coverage_threshold = true\n",
                "  [profiles.default] empty_page_coverage_threshold: "
                f"{_BOOL_NOT_NUMBER}",
                id="empty_page_coverage_threshold",
            ),
        ],
    )
    def test_toml_boolean_is_refused(
        self, tmp_config_dir: Path, toml_content: str, expected: str
    ) -> None:
        """The boolean is refused with one fixed line that echoes no value."""
        err = _load_error(tmp_config_dir / "bool.toml", toml_content)
        lines = _error_lines(err)
        assert expected in lines
        detail = lines[1:]
        assert all("Value error" not in line for line in detail)
        assert all("True" not in line and "False" not in line for line in detail)
        assert all(not line.endswith(("= true", "= false")) for line in detail)

    @pytest.mark.usefixtures("no_discovered_config")
    def test_environment_strings_still_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The string values ``SANELESS_*`` variables supply are not refused."""
        monkeypatch.setenv("SANELESS_OUTPUT__WEB_PORT", "8080")
        monkeypatch.setenv("SANELESS_OUTPUT__OPERATOR_WAIT_TIMEOUT_SECONDS", " 7 ")
        settings = load_settings()
        assert settings.output.web_port == 8080
        assert settings.output.operator_wait_timeout_seconds == 7


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

    def test_validate_missing_tmp_dir_is_not_created(self, tmp_path: Path) -> None:
        """The startup check only inspects: a missing ``tmp_dir`` stays missing."""
        tmp_dir = tmp_path / "scratch"
        settings = Settings(
            output=OutputConfig(tmp_dir=str(tmp_dir), data_dir=str(tmp_path)),
            profiles={"default": ProfileConfig()},
        )
        validate_settings_dirs(settings)
        assert not os.path.lexists(tmp_dir)

    def test_validate_owned_0755_tmp_dir_passes(self, tmp_path: Path) -> None:
        """An existing ``tmp_dir`` the user owns may be group- and world-readable."""
        tmp_dir = tmp_path / "scratch"
        tmp_dir.mkdir()
        tmp_dir.chmod(0o755)
        settings = Settings(
            output=OutputConfig(tmp_dir=str(tmp_dir), data_dir=str(tmp_path)),
            profiles={"default": ProfileConfig()},
        )
        validate_settings_dirs(settings)
        assert stat.S_IMODE(tmp_dir.stat().st_mode) == 0o755

    @pytest.mark.parametrize("shape", ["symlink", "world-writable", "group-writable"])
    def test_validate_unsafe_existing_tmp_dir_fails(
        self, tmp_path: Path, shape: str
    ) -> None:
        """
        A squatted or loosened ``tmp_dir`` stops startup with a fatal ConfigError.

        Another local user can create the predictable default first; the
        refusal names the setting and says what to do about it.
        """
        tmp_dir = tmp_path / "scratch"
        if shape == "symlink":
            real = tmp_path / "elsewhere"
            real.mkdir(mode=0o700)
            tmp_dir.symlink_to(real)
        else:
            tmp_dir.mkdir()
            tmp_dir.chmod(0o777 if shape == "world-writable" else 0o770)
        settings = Settings(
            output=OutputConfig(tmp_dir=str(tmp_dir), data_dir=str(tmp_path)),
            profiles={"default": ProfileConfig()},
        )
        with pytest.raises(ConfigError) as exc_info:
            validate_settings_dirs(settings)
        message = str(exc_info.value)
        assert "output.tmp_dir" in message
        assert str(tmp_dir) in message
        assert "chmod 700" in message

    def test_validate_tmp_dir_owned_by_another_user_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``tmp_dir`` owned by another UID is refused even at mode 0700."""
        tmp_dir = tmp_path / "scratch"
        tmp_dir.mkdir(mode=0o700)
        owner = tmp_dir.stat().st_uid
        settings = Settings(
            output=OutputConfig(tmp_dir=str(tmp_dir), data_dir=str(tmp_path)),
            profiles={"default": ProfileConfig()},
        )
        monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
        with pytest.raises(ConfigError, match=r"output\.tmp_dir"):
            validate_settings_dirs(settings)

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


class TestDirectorySettingsMustBeDirectories:
    """
    A directory setting that names a file is refused at start-up.

    ``os.access`` says a regular file is writable, so a ``data_dir`` that was
    a file -- or that sat under one -- passed the start-up check and failed
    mid-scan, when a failed scan needed preserving.
    """

    @staticmethod
    def _settings_with(label: str, value: Path, writable: Path) -> Settings:
        """
        Build settings with ``label`` set to ``value`` and the others writable.

        Returns:
            The settings.

        """
        return Settings(
            output=OutputConfig(
                tmp_dir=str(value if label == "tmp_dir" else writable / "t"),
                data_dir=str(value if label == "data_dir" else writable / "d"),
            ),
            paperless=PaperlessConfig(
                consume_dir=str(value) if label == "consume_dir" else ""
            ),
            profiles={"default": ProfileConfig()},
        )

    @pytest.mark.parametrize("label", ["data_dir", "consume_dir"])
    def test_directory_setting_naming_a_file_is_refused(
        self, tmp_path: Path, label: str
    ) -> None:
        """An existing regular file is not a directory, and the message says so."""
        a_file = tmp_path / "a-file"
        a_file.write_text("")

        with pytest.raises(ConfigError) as exc_info:
            validate_settings_dirs(self._settings_with(label, a_file, tmp_path))

        assert str(exc_info.value) == f"{label} is not a directory: {a_file}"

    def test_tmp_dir_naming_a_file_is_refused_as_not_a_directory(
        self, tmp_path: Path
    ) -> None:
        """``tmp_dir`` meets the private-directory check first; it names the key."""
        a_file = tmp_path / "a-file"
        a_file.write_text("")

        with pytest.raises(ConfigError) as exc_info:
            validate_settings_dirs(self._settings_with("tmp_dir", a_file, tmp_path))

        message = str(exc_info.value)
        assert "tmp_dir" in message
        assert "is not a directory" in message

    @pytest.mark.parametrize("label", ["tmp_dir", "data_dir", "consume_dir"])
    def test_directory_setting_under_a_file_is_refused(
        self, tmp_path: Path, label: str
    ) -> None:
        """A missing directory whose nearest existing ancestor is a file is refused."""
        a_file = tmp_path / "a-file"
        a_file.write_text("")
        under = a_file / "sub" / "dir"

        with pytest.raises(ConfigError) as exc_info:
            validate_settings_dirs(self._settings_with(label, under, tmp_path))

        assert str(exc_info.value) == f"{label} parent is not a directory: {a_file}"


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
        assert output.tmp_dir == Path(tempfile.gettempdir()) / f"saneless-{os.getuid()}"
        assert isinstance(output.data_dir, Path)
        assert output.log_file == output.data_dir / "saneless.log"

    def test_default_tmp_dir_is_computed_per_instance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The default ``tmp_dir`` follows the temp directory at construction.

        It used to be computed once, at import, from whatever temp directory
        the process saw then.
        """
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        assert OutputConfig().tmp_dir == tmp_path / f"saneless-{os.getuid()}"

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


class _RelativeLayout(NamedTuple):
    """A config directory and a different working directory to load it from."""

    conf: Path
    elsewhere: Path


def _write_relative_config(directory: Path, body: str) -> Path:
    """
    Write ``saneless.toml`` into ``directory`` with a default profile.

    Args:
        directory: Where the file goes; created if missing.
        body: TOML to put before the ``[profiles.default]`` table.

    Returns:
        The written file.

    """
    directory.mkdir(parents=True, exist_ok=True)
    config_file = directory / config_mod.CONFIG_FILENAME
    config_file.write_text(f"{body}\n[profiles.default]\n")
    return config_file


class TestRelativePaths:
    """
    A relative path setting is pinned absolute at load, next to its config file.

    A relative ``data_dir`` used to be taken relative to whatever directory a
    command ran in, so ``saneless jobs`` from another directory opened -- and
    created -- an empty database there. File and environment values follow
    the same rule: the directory of the loaded file, or the working directory
    when no file was loaded.
    """

    @pytest.fixture
    def layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> _RelativeLayout:
        """
        Make a config directory and run from a different, empty one.

        Returns:
            The config directory and the working directory.

        """
        conf = tmp_path / "conf"
        elsewhere = tmp_path / "elsewhere"
        conf.mkdir()
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        return _RelativeLayout(conf=conf, elsewhere=elsewhere)

    def test_relative_data_dir_follows_the_config_file(
        self, layout: _RelativeLayout
    ) -> None:
        """``data_dir = "state"`` lands beside the file, not in the working dir."""
        config_file = _write_relative_config(
            layout.conf, '[output]\ndata_dir = "state"\n'
        )

        settings = load_settings(str(config_file))

        assert settings.output.data_dir == layout.conf / "state"
        assert settings.output.db_path == layout.conf / "state" / "saneless.db"

    def test_relative_data_dir_gives_one_database_from_any_directory(
        self, layout: _RelativeLayout, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Two working directories open the same database.

        The location compared is the one a command would open: the recorded
        path taken against the working directory it ran in.
        """
        config_file = _write_relative_config(
            layout.conf, '[output]\ndata_dir = "state"\n'
        )
        opened: list[Path] = []
        for directory in (layout.elsewhere, layout.conf):
            monkeypatch.chdir(directory)
            opened.append(Path.cwd() / load_settings(str(config_file)).output.db_path)

        assert opened == [layout.conf / "state" / "saneless.db"] * 2

    def test_every_relative_path_setting_follows_the_config_file(
        self, layout: _RelativeLayout
    ) -> None:
        """``tmp_dir``, ``log_file`` and ``consume_dir`` are pinned the same way."""
        config_file = _write_relative_config(
            layout.conf,
            '[output]\ntmp_dir = "t"\nlog_file = "logs/saneless.log"\n\n'
            '[paperless]\nconsume_dir = "consume"\n',
        )

        settings = load_settings(str(config_file))

        assert settings.output.tmp_dir == layout.conf / "t"
        assert settings.output.log_file == layout.conf / "logs" / "saneless.log"
        assert settings.paperless.consume_dir == layout.conf / "consume"

    def test_relative_data_dir_in_a_discovered_file_follows_that_file(
        self, patched_search_paths: _SearchDirs
    ) -> None:
        """A file found by the search is the anchor, not the working directory."""
        _write_relative_config(
            patched_search_paths.xdg, '[output]\ndata_dir = "state"\n'
        )

        settings = load_settings()

        assert settings.output.data_dir == patched_search_paths.xdg / "state"

    def test_relative_environment_value_follows_the_loaded_file(
        self, layout: _RelativeLayout, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``SANELESS_OUTPUT__DATA_DIR=state`` with a file loaded lands beside it."""
        config_file = _write_relative_config(layout.conf, "")
        monkeypatch.setenv("SANELESS_OUTPUT__DATA_DIR", "state")

        settings = load_settings(str(config_file))

        assert settings.output.data_dir == layout.conf / "state"

    def test_relative_environment_value_without_a_file_follows_the_working_directory(
        self, no_discovered_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no file loaded, the working directory is the anchor, pinned at load."""
        monkeypatch.setenv("SANELESS_OUTPUT__DATA_DIR", "state")

        settings = load_settings()

        assert settings.config_path is None
        assert settings.output.data_dir == no_discovered_config / "state"
        assert settings.output.data_dir.is_absolute()

    def test_relative_rule_leaves_home_and_absolute_paths_alone(
        self, layout: _RelativeLayout
    ) -> None:
        """``~`` still means the home directory and an absolute path is kept."""
        absolute = layout.elsewhere / "abs" / "x"
        config_file = _write_relative_config(
            layout.conf, f'[output]\ndata_dir = "~/x"\ntmp_dir = "{absolute}"\n'
        )

        settings = load_settings(str(config_file))

        assert settings.output.data_dir == Path.home() / "x"
        assert settings.output.tmp_dir == absolute

    @pytest.mark.usefixtures("layout")
    def test_relative_data_dir_follows_the_directory_a_symlinked_file_was_found_in(
        self, tmp_path: Path
    ) -> None:
        """
        A symlinked config anchors at the link's directory, not the target's.

        That is the directory the operator named, and the one the log prints.
        """
        real = _write_relative_config(
            tmp_path / "real", '[output]\ndata_dir = "state"\n'
        )
        link_dir = tmp_path / "link"
        link_dir.mkdir()
        link = link_dir / config_mod.CONFIG_FILENAME
        link.symlink_to(real)

        settings = load_settings(str(link))

        assert settings.output.data_dir == link_dir / "state"


# The shipped files an operator copies a token stand-in out of.
_STAND_IN_ROOT: Final = Path(__file__).resolve().parents[1]
# ``token = "..."`` inside a fenced TOML block, commented out or not.
_TOML_TOKEN_LINE: Final = re.compile(r'^\s*#?\s*token\s*=\s*"(?P<value>[^"]*)"')
# ``SANELESS_PAPERLESS__TOKEN=...`` anywhere on a line, so the ``export ``,
# ``- `` and ``#`` prefixes of shell, compose and commented forms all match;
# the value may be quoted.
_TOKEN_VARIABLE_VALUE: Final = re.compile(
    r"""SANELESS_PAPERLESS__TOKEN=(?P<quote>["']?)(?P<value>[^"'\s`]*)(?P=quote)"""
)
# A Markdown table row naming the variable in its first cell.
_TOKEN_VARIABLE_CELL: Final = "`SANELESS_PAPERLESS__TOKEN`"
# The headings the reference tables give their example-value column.
_EXAMPLE_COLUMN_HEADINGS: Final = frozenset({"example", "typical value"})
_TABLE_SEPARATOR: Final = re.compile(r"^\|\s*:?-{3,}")


def _table_cells(line: str) -> list[str]:
    """Split a Markdown table row into its stripped cells."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _documented_token_stand_ins() -> list[tuple[str, str]]:
    """
    Collect every paperless-ngx token value the docs, README and compose show.

    Three forms are read: ``token = "..."`` in a fenced TOML block of a doc
    page or the README; ``SANELESS_PAPERLESS__TOKEN=...`` in any of those
    files or in ``docker-compose.yml``; and the example column of a Markdown
    table row whose first cell is the variable. Each value is returned with
    ``file:line`` so a failure names where it is.

    Returns:
        ``(location, value)`` pairs, in file and line order.

    """
    pages = [
        *sorted((_STAND_IN_ROOT / "docs").rglob("*.md")),
        _STAND_IN_ROOT / "README.md",
    ]
    found: list[tuple[str, str]] = []
    for page in [*pages, _STAND_IN_ROOT / "docker-compose.yml"]:
        name = page.relative_to(_STAND_IN_ROOT)
        fence: str | None = None
        header: list[str] = []
        previous = ""
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
            where = f"{name}:{number}"
            stripped = line.strip()
            if stripped.startswith("```"):
                fence = None if fence is not None else stripped[3:].strip().lower()
                continue
            if fence == "toml" and (match := _TOML_TOKEN_LINE.match(line)):
                found.append((where, match.group("value")))
            found.extend(
                (where, match.group("value"))
                for match in _TOKEN_VARIABLE_VALUE.finditer(line)
            )
            if _TABLE_SEPARATOR.match(stripped):
                header = [cell.lower() for cell in _table_cells(previous)]
            elif stripped.startswith("|"):
                cells = _table_cells(stripped)
                if cells[0] == _TOKEN_VARIABLE_CELL:
                    column = next(
                        (
                            index
                            for index, heading in enumerate(header)
                            if heading in _EXAMPLE_COLUMN_HEADINGS
                        ),
                        None,
                    )
                    assert column is not None, f"{where}: no example column"
                    found.append((where, cells[column].strip("`")))
            previous = stripped
    return found


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

    def test_every_documented_token_stand_in_is_a_placeholder(self) -> None:
        """
        Every token value the docs, README and compose file show is a stand-in.

        An operator copies these verbatim. A value the predicate does not
        recognise is taken for a real token, so the appliance tries to upload
        with it and reports an authentication failure instead of saying the
        token was never set. The count floor keeps the collector honest: a
        pattern that stopped matching would otherwise pass on nothing.
        """
        found = _documented_token_stand_ins()
        assert len(found) >= 12, (
            f"the collector found only {len(found)} token values: {found}"
        )
        offenders = [
            f"{where}: {value!r}"
            for where, value in found
            if not is_placeholder_token(value)
        ]
        assert not offenders, (
            "documented token values that are not recognised placeholders; "
            "use 'your-api-token-here':\n" + "\n".join(offenders)
        )

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
            "valid keys: show_tags, show_correspondent, allowed_hosts"
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


_ALLOWED_HOSTS_RULE = "must be a host name or a .suffix"
"""The start of the refusal every bad ``allowed_hosts`` entry gets."""


class TestWebAllowedHosts:
    """
    ``[web] allowed_hosts`` adds names saneless answers to, and nothing else.

    Every entry is an exact name or a leading-dot suffix.  There is no ``*``:
    one line would reopen DNS rebinding for every name at once.  A port, a
    scheme or a path is a sign the operator pasted a URL, and is refused at load
    with a message that names the key and never the value.
    """

    def test_allowed_hosts_defaults_to_empty(self) -> None:
        """No extra names are trusted until the operator adds some."""
        assert WebConfig().allowed_hosts == ()

    def test_allowed_hosts_entries_are_stripped_and_lower_cased(self) -> None:
        """Entries are compared the way Host is: without case or padding."""
        web = WebConfig.model_validate(
            {"allowed_hosts": [" Scan.Example.COM ", ".Home.Example", "nas.lan."]}
        )
        assert web.allowed_hosts == ("scan.example.com", ".home.example", "nas.lan")

    def test_allowed_hosts_loads_from_toml(self, tmp_config_dir: Path) -> None:
        """The TOML form is a list of strings under ``[web]``."""
        config_file = tmp_config_dir / "allowed.toml"
        config_file.write_text('[web]\nallowed_hosts = ["scan.example.com"]\n')
        settings = load_settings(config_path=str(config_file))
        assert settings.web.allowed_hosts == ("scan.example.com",)

    @pytest.mark.usefixtures("no_discovered_config")
    def test_allowed_hosts_loads_from_the_environment_as_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The environment form is a JSON list."""
        monkeypatch.setenv(
            "SANELESS_WEB__ALLOWED_HOSTS", '["scan.example.com", ".home.example"]'
        )
        settings = load_settings()
        assert settings.web.allowed_hosts == ("scan.example.com", ".home.example")

    @pytest.mark.usefixtures("no_discovered_config")
    def test_a_bare_string_in_the_environment_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare name is not a JSON list, and loads as an error, not a guess."""
        monkeypatch.setenv("SANELESS_WEB__ALLOWED_HOSTS", "scan.example.com")
        with pytest.raises(ConfigError):
            load_settings()

    @pytest.mark.parametrize(
        "entry",
        [
            pytest.param("", id="empty"),
            pytest.param("   ", id="blank"),
            pytest.param("*", id="star"),
            pytest.param("*.zqmarker.example", id="wildcard"),
            pytest.param("zqmarker.example:8080", id="port"),
            pytest.param("http://zqmarker.example", id="scheme"),
            pytest.param("zqmarker.example/scan", id="path"),
            pytest.param("user@zqmarker.example", id="userinfo"),
            pytest.param(".zqmarkercom", id="suffix-of-a-top-level-name"),
            pytest.param("zqmarker..example", id="empty-label"),
            pytest.param("zqmarker example", id="inner-space"),
            pytest.param("zqmärker.example", id="non-ascii"),
            pytest.param("[::1]", id="bracketed-ipv6"),
        ],
    )
    def test_a_bad_entry_is_refused_without_echoing_it(
        self, tmp_config_dir: Path, entry: str
    ) -> None:
        """A bad entry is a ConfigError naming the key and never the value."""
        err = _load_error(
            tmp_config_dir / "bad_allowed.toml",
            f"[web]\nallowed_hosts = [{_toml_string(entry)}]\n",
        )
        line = _only_body_line(err)
        assert line.startswith("  [web] allowed_hosts: ")
        assert _ALLOWED_HOSTS_RULE in line
        assert "zqmarker" not in str(err)
        assert "zqmärker" not in str(err)

    def test_one_bad_entry_refuses_the_whole_list(self) -> None:
        """A good entry beside a bad one does not let the list load."""
        with pytest.raises(ValidationError, match="allowed_hosts"):
            WebConfig.model_validate({"allowed_hosts": ["scan.example.com", "*"]})


def _toml_string(value: str) -> str:
    """
    Quote ``value`` as a TOML basic string, escaping what TOML requires.

    Args:
        value: The raw string.

    Returns:
        A TOML basic-string literal that parses back to ``value``.

    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
