"""
Configuration loading with pydantic-settings, TOML files, and env var overrides.

Settings are loaded from TOML config files with environment variable overrides
using the SANELESS_ prefix and __ nested delimiter. A default scan profile must
always be present in the configuration.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import stat
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal, Protocol, cast

import httpx2
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)
from pydantic_settings.exceptions import SettingsError

from saneless.exceptions import ConfigError
from saneless.private_dirs import check_private_dir
from saneless.vocabulary import (
    MAX_PAPERLESS_ID,
    TITLE_MAX_LENGTH,
    ConfigFileState,
    PaperSize,
    ProfileStorage,
    local_time,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from datetime import datetime

    from pydantic_core import ErrorDetails
    from pydantic_settings.main import InitSettingsSource

__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_RESOLUTION",
    "LEGACY_CONFIG_FILENAME",
    "PLACEHOLDER_TOKENS",
    "PROFILE_DESCRIPTION_MAX_LENGTH",
    "PROFILE_LABEL_MAX_LENGTH",
    "ConfigDiscovery",
    "LogLevel",
    "OutputConfig",
    "PaperlessConfig",
    "PaperlessId",
    "ProfileConfig",
    "ScannerConfig",
    "Settings",
    "WebConfig",
    "absolute_or_as_spelled",
    "config_file_state",
    "config_search_paths",
    "discover_config",
    "env_sourced_keys",
    "is_placeholder_token",
    "load_settings",
    "log_config_sources",
    "nearest_existing_ancestor",
    "profile_storage_for_loaded",
    "resolve_job_title",
    "validate_settings_dirs",
    "warn_on_legacy_duplex_sources",
    "xdg_config_home",
    "xdg_state_home",
]

logger = logging.getLogger(__name__)

DEFAULT_RESOLUTION = 300
"""Default scan resolution in DPI.

300 DPI is the minimum recommended by Tesseract OCR and the industry
standard for professional document scanning.
"""

# Both fields are rendered into HTML, and nothing else bounds what a config
# file can put on the page.
PROFILE_LABEL_MAX_LENGTH: Final = 64
"""The longest ``profiles.<name>.label`` a config may carry."""

PROFILE_DESCRIPTION_MAX_LENGTH: Final = 200
"""The longest ``profiles.<name>.description`` a config may carry."""

# The message names no value, so the rendered ``[section] key:`` line stays
# free of whatever the config held; a custom error carries no "Value error,"
# prefix.
_BOOL_NOT_NUMBER_TYPE: Final = "bool_not_number"
_BOOL_NOT_NUMBER_MESSAGE: Final = "must be a number, not true or false"


def _refuse_bool(value: object) -> object:
    """
    Refuse a boolean before pydantic's lax mode reads it as 0 or 1.

    ``bool`` is a subclass of ``int``, so without this ``web_port = true``
    binds port 1 and ``resolution = true`` scans at 1 dpi.

    Raises:
        PydanticCustomError: The input is ``True`` or ``False``.

    """
    if isinstance(value, bool):
        raise PydanticCustomError(_BOOL_NOT_NUMBER_TYPE, _BOOL_NOT_NUMBER_MESSAGE)
    return value


# A number setting that refuses a TOML boolean.  Strict mode is not used: it
# also refuses a string, and every SANELESS_* environment variable arrives as
# one, so "8080" must still read as 8080.
WholeNumber = Annotated[int, BeforeValidator(_refuse_bool)]

RealNumber = Annotated[float, BeforeValidator(_refuse_bool)]

# paperless-ngx keys are 32-bit auto-increment integers, so anything outside
# 1..MAX_PAPERLESS_ID cannot name a tag or correspondent.  It is refused where
# it enters, never stored or sent.
PaperlessId = Annotated[WholeNumber, Field(ge=1, le=MAX_PAPERLESS_ID)]


def _dedupe_ids(values: list[int]) -> list[int]:
    """
    Return ``values`` with each id kept once, where it first appears.

    A repeated tag means nothing more than the tag once, so it is collapsed
    silently rather than refused.
    """
    return list(dict.fromkeys(values))


CONFIG_FILENAME: Final = "saneless.toml"
"""The one name a configuration file may have, in every searched location."""

# A file under this name is stat-ed so it can be reported, and never opened,
# parsed or merged.  Build every other mention of the name from this constant.
LEGACY_CONFIG_FILENAME: Final = "config.toml"
"""The superseded name, detected and reported in searched directories only."""

# Positionally aligned with ``config_search_paths()``: index i is how the
# documentation spells the directory of candidate i. The status strip is
# visible to anyone on the LAN and carries no filesystem path, so it names a
# file by its documented spelling; the log and ``doctor`` print real paths,
# absolute unless the working directory has been removed.
_SEARCH_DIR_SPELLINGS: Final = ("./", "$XDG_CONFIG_HOME/saneless/", "/etc/saneless/")


@dataclass(frozen=True, slots=True)
class ConfigDiscovery:
    """
    What the search for a configuration file found, as it found it.

    Frozen because the settings in hand were built from this search: nothing
    downstream may edit it or re-run the search, since a file created or
    renamed since startup would describe a program that is not running.

    A superseded-name file in a searched directory is recorded in ``stale``,
    stat-ed and never opened, parsed or merged: it may belong to another tool.
    The status strip names ``stale[0]``, the one whose rename makes it load.
    An explicit ``--config`` path searches nothing, so ``searched`` and
    ``stale`` are empty.

    Every path is absolute against the load-time working directory and never
    resolved through symlinks, so it is the path the operator wrote.  A
    relative candidate searched from a removed working directory stays as
    spelled and is never found.  An alias of an earlier file goes in
    ``duplicates``, not ``found``, so the file in use is never also reported
    as unread.

    Attributes:
        explicit: The path given on the command line, or None when the search
            list was used.
        searched: Every candidate looked at, in search order.  An alias stays
            here, because ``documented_spelling`` and ``doctor``'s table read
            this tuple by position.
        found: The candidates that are regular files, in search order, each
            file once.
        loaded: The candidate the settings came from, or None.
        stale: Existing superseded-name files beside a candidate, in search
            order, each file once.
        duplicates: Candidates that exist but are the same file as an earlier
            entry of ``found``, reached through another spelling, in search
            order.

    """

    explicit: Path | None
    searched: tuple[Path, ...]
    found: tuple[Path, ...]
    loaded: Path | None
    stale: tuple[Path, ...]
    duplicates: tuple[Path, ...] = ()

    def documented_spelling(self, path: Path) -> str:
        """
        Name a searched file the way the documentation spells it.

        By search position, never by resolved path: the caller is the status
        strip, which is visible to anyone on the LAN and therefore carries no
        filesystem path.  Position is also what makes the answer stable when
        the search list is redirected under a test's temporary directory.

        Args:
            path: A file in one of the searched directories, under either the
                current or the superseded name.

        Returns:
            One of the three documented directory spellings, with the file's
            own name appended.

        Raises:
            ValueError: If ``path`` sits beside no searched candidate.

        """
        for index, candidate in enumerate(self.searched):
            if candidate.parent == path.parent:
                return _SEARCH_DIR_SPELLINGS[index] + path.name
        msg = f"Not beside any searched config candidate: {path}"
        raise ValueError(msg)


def _file_identity(path: Path) -> tuple[int, int] | None:
    """
    Say which regular file a path names, without opening it.

    ``stat()`` follows symlinks, so a link and its target give the same
    answer, which is the point: the answer is the file, not the spelling.

    Returns:
        The file's ``(st_dev, st_ino)``, or None when nothing can be stat-ed
        there or what is there is not a regular file.

    """
    try:
        status = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode):
        return None
    return (status.st_dev, status.st_ino)


def _first_of_each_file(
    paths: tuple[Path, ...],
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """
    Split the paths that name a regular file into first sightings and aliases.

    Args:
        paths: The paths to look at, in search order.

    Returns:
        The first path to each distinct file, then every later path to a file
        already seen; both in search order, and neither holding a path to
        nothing.

    """
    seen: set[tuple[int, int]] = set()
    first: list[Path] = []
    again: list[Path] = []
    for path in paths:
        identity = _file_identity(path)
        if identity is None:
            continue
        if identity in seen:
            again.append(path)
        else:
            seen.add(identity)
            first.append(path)
    return tuple(first), tuple(again)


def _working_directory() -> Path | None:
    """
    Return the working directory, or None when it has been removed.

    A shell can sit in a directory another process has since removed, and
    then ``os.getcwd`` raises a FileNotFoundError that names no file.  Such a
    directory is empty and can gain no entry.
    """
    try:
        return Path.cwd()
    except FileNotFoundError:
        return None


def absolute_or_as_spelled(path: Path) -> Path:
    """
    Make a path absolute for a reader, or keep its spelling when that cannot be.

    A search from a removed working directory records its relative candidate
    as it is spelled, because there is no directory to join it onto; asking
    for its absolute form would ask for that directory again.

    Args:
        path: A path to show in a log line or on a terminal.

    Returns:
        ``path`` made absolute, or ``path`` itself when it is relative to a
        working directory that no longer exists.

    """
    if path.is_absolute():
        return path
    cwd = _working_directory()
    return path if cwd is None else cwd / path


def discover_config(candidates: tuple[Path, ...]) -> ConfigDiscovery:
    """
    Look for a configuration file, and for superseded-name files beside one.

    Each candidate and each superseded-name sibling is ``stat()``-ed once and
    never opened: a sibling that is invalid TOML must not break the load, and
    a live token in one must not reach the settings.  A path that cannot be
    stat-ed counts as not found.

    The candidates are made absolute first.  When the working directory has
    been removed, a relative candidate is recorded as spelled and counts as
    not found; the later candidates are searched as usual.

    Args:
        candidates: The paths to search, in priority order.

    Returns:
        The recording, with the first existing candidate as ``loaded``.

    """
    cwd = (
        _working_directory()
        if any(not candidate.is_absolute() for candidate in candidates)
        else None
    )
    searched = tuple(
        candidate if cwd is None or candidate.is_absolute() else cwd / candidate
        for candidate in candidates
    )
    anchored = tuple(candidate for candidate in searched if candidate.is_absolute())
    found, duplicates = _first_of_each_file(anchored)
    stale, _ = _first_of_each_file(
        tuple(candidate.with_name(LEGACY_CONFIG_FILENAME) for candidate in anchored)
    )
    return ConfigDiscovery(
        explicit=None,
        searched=searched,
        found=found,
        loaded=found[0] if found else None,
        stale=stale,
        duplicates=duplicates,
    )


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
"""The logging level names ``output.log_level`` accepts.

Each is a key of ``logging.getLevelNamesMapping()`` and, lower-cased, a valid
uvicorn ``log_level``.
"""


def _xdg_base(variable: str, *fallback: str) -> Path:
    """
    Resolve an XDG base directory from the environment at call time.

    Per the XDG Base Directory Specification, an unset, empty or relative
    value means the default below ``$HOME`` joined with ``fallback``.

    Raises:
        ConfigError: The fallback is needed and there is no home directory,
            as in a container run under an arbitrary uid with no ``HOME``.

    """
    value = os.environ.get(variable, "")
    if value and Path(value).is_absolute():
        return Path(value)
    try:
        home = Path.home()
    except RuntimeError:
        # Path.home() says only "Could not determine home directory."; the
        # operator needs to know which variables stand in for it.
        msg = (
            "Cannot determine the home directory: set HOME, or set "
            "XDG_CONFIG_HOME and XDG_STATE_HOME to absolute paths"
        )
        raise ConfigError(
            msg,
            next_step=(
                "Set HOME, or set XDG_CONFIG_HOME and XDG_STATE_HOME to "
                "absolute paths, then try again."
            ),
        ) from None
    return home.joinpath(*fallback)


def xdg_config_home() -> Path:
    """
    Return the XDG config home, ``$XDG_CONFIG_HOME`` or ``~/.config``.

    Read at call time, not import, so a later HOME or XDG change is honoured.
    An empty or relative ``$XDG_CONFIG_HOME`` is ignored, per the basedir spec.

    Returns:
        The base directory user configuration files are searched under.

    Raises:
        ConfigError: ``$XDG_CONFIG_HOME`` is not usable and there is no home
            directory to fall back to.

    """
    return _xdg_base("XDG_CONFIG_HOME", ".config")


def xdg_state_home() -> Path:
    """
    Return the XDG state home, ``$XDG_STATE_HOME`` or ``~/.local/state``.

    Read at call time, not import, so a later HOME or XDG change is honoured.
    An empty or relative ``$XDG_STATE_HOME`` is ignored, per the basedir spec.

    Returns:
        The base directory durable state (database, log) defaults live under.

    Raises:
        ConfigError: ``$XDG_STATE_HOME`` is not usable and there is no home
            directory to fall back to.

    """
    return _xdg_base("XDG_STATE_HOME", ".local", "state")


def _default_data_dir() -> Path:
    """
    Compute the default ``output.data_dir``: ``$XDG_STATE_HOME/saneless``.

    Returns:
        The default durable state directory.

    """
    return xdg_state_home() / "saneless"


def _default_tmp_dir() -> Path:
    """
    Compute the default ``output.tmp_dir``: ``<temp dir>/saneless-<uid>``.

    Computed when settings are built, never at import: ``gettempdir()``
    probes the file system on its first call.  The name is per user and
    stable; another local user can still create it first, and the directory
    checks refuse it then.
    """
    return Path(tempfile.gettempdir()) / f"saneless-{os.getuid()}"


def _default_log_file() -> Path:
    """
    Compute the default ``output.log_file``, inside the default ``data_dir``.

    Returns:
        ``$XDG_STATE_HOME/saneless/saneless.log``.

    """
    return xdg_state_home() / "saneless" / "saneless.log"


def _expand_user(value: Path) -> Path:
    """
    Expand a leading ``~`` in a path setting.

    ``$VAR`` is left literal, so a value cannot silently pick up an unrelated
    environment variable.

    Raises:
        ValueError: ``~`` cannot be expanded.  pydantic would let the
            RuntimeError escape the config error renderer; the message never
            repeats the value, which may be private.

    """
    try:
        return value.expanduser()
    except RuntimeError:
        msg = "cannot expand '~': unknown user or home directory"
        raise ValueError(msg) from None


def _is_legacy_manual_duplex_source(source: str) -> bool:
    """
    Recognise the deprecated ``source = "Manual Duplex"`` config form.

    Used only at load, to translate the profile to ``duplex = "manual"`` and
    to warn about it.  It never chooses a scanning strategy: that comes from
    ``ProfileConfig.duplex``, and ``source`` is a pure SANE value.
    """
    lowered = source.lower()
    return "manual" in lowered and "duplex" in lowered


class ScannerConfig(BaseModel):
    """Scanner connection settings."""

    model_config = ConfigDict(extra="forbid")

    host: str = ""
    device: str = ""


# Compared exactly, never as substrings.
PLACEHOLDER_TOKENS: Final[frozenset[str]] = frozenset(
    {
        # Older compose files still carry this value; the appliance must
        # recognise it on an upgraded image.
        "changeme",
        "change-me",
        "change_me",
        # What saneless.toml.example carries; a test reads the file, so the two
        # cannot drift apart.
        "your-api-token-here",
        "your-token-here",
        "your_token_here",
        "your-api-token",
        "yourtokenhere",
        "token",
        "api-token",
        "replace-me",
        "replaceme",
        "placeholder",
        "xxx",
        "todo",
    }
)
"""The literal token values that mean "nobody has configured this"."""


def is_placeholder_token(value: str) -> bool:
    """
    Say whether a Paperless token is unset or a stand-in nobody replaced.

    This is the one predicate ``doctor``, the web status strip, the scan route
    and ``saneless scan`` share, so all four agree on whether the appliance can
    upload. A value counts as a placeholder when it is empty or
    whitespace-only, or when stripping and lower-casing it lands on a member of
    ``PLACEHOLDER_TOKENS``.

    The set is a fixed literal set, deliberately not a shape heuristic:
    refusing a legitimate token from a future paperless-ngx version is worse
    than missing an exotic placeholder.  Membership is exact, never a
    substring match: ``changeme7f3a91`` is a real token.

    ASVS 4.0.3 V7.1.1 (no credentials in logs): this function neither logs
    nor returns the value it is given, and callers must not log or render it
    either.

    Args:
        value: The token as configured, already unwrapped from its SecretStr.

    Returns:
        True when the token is blank or a known placeholder literal.

    """
    normalised = value.strip().lower()
    return not normalised or normalised in PLACEHOLDER_TOKENS


# Visible ASCII: what an HTTP header value or a URL can carry without quoting.
_VISIBLE_ASCII: Final = range(0x21, 0x7F)


class PaperlessConfig(BaseModel):
    """
    Paperless-ngx API connection settings.

    Surrounding whitespace is stripped from ``url`` and ``token`` as they load.
    A set token must then be visible ASCII only, and a set URL must be an
    ``http``/``https`` address that names a host and carries no user name or
    password; anything else is refused with a message naming the key, never
    the value.
    """

    # An unknown key such as a mistyped ``tokne`` is refused, not ignored.
    model_config = ConfigDict(extra="forbid")

    url: str = ""
    # Masked in repr, tracebacks and model_dump. Code that needs the value
    # calls get_secret_value() where it uses it; settings never hold the
    # unwrapped string, and nothing may log or render it.
    token: SecretStr = SecretStr("")
    # None means the fallback copy is disabled.
    consume_dir: Path | None = None

    @field_validator("token", mode="before")
    @classmethod
    def _normalise_token(cls, value: object) -> object:
        """
        Strip the token and refuse one no Authorization header can carry.

        A secret file ends in a newline and a Windows ``.env`` in CRLF, so the
        edges are stripped.  What is left must be visible ASCII: a line break
        would split the request header.  The message names the rule, never the
        value, and a ``SecretStr`` passes through unwrapped.

        Raises:
            ValueError: The stripped token is not all visible ASCII.

        """
        if isinstance(value, str):
            value = value.strip()
            if any(ord(char) not in _VISIBLE_ASCII for char in value):
                msg = (
                    "must contain only visible ASCII characters: no spaces, "
                    "line breaks or control characters inside it"
                )
                raise ValueError(msg)
        return value

    @field_validator("url", mode="before")
    @classmethod
    def _normalise_url(cls, value: object) -> object:
        """
        Strip the URL and refuse one no upload could use.

        Parsed with ``httpx2.URL``, the client's own parser, so what loads is
        what the client accepts; a scheme-less ``paperless:8000`` parses with no
        host and is refused here.  A ``user:password@`` would be sent in place
        of the token, so it is refused.  The parser's own complaint is never
        used: for ``http://user:pass/word@host`` it quotes the password back.

        Raises:
            ValueError: The URL holds whitespace, a control or non-ASCII
                character, does not parse, is not ``http``/``https`` with a
                host, or carries a user name or password.

        """
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            return value
        if any(ord(char) not in _VISIBLE_ASCII for char in value):
            msg = (
                "must contain only visible ASCII characters; write an "
                "international host name in its xn-- form"
            )
            raise ValueError(msg)
        try:
            parsed = httpx2.URL(value)
        except httpx2.InvalidURL:
            msg = "is not a valid URL"
            raise ValueError(msg) from None
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            # No URL in the text: the renderer strikes the input wherever it
            # stands alone, so an example URL would vanish when typed as the
            # value.
            msg = "must be an http or https address that names a host"
            raise ValueError(msg)
        if parsed.userinfo:
            msg = (
                "must not contain a user name or password; put the "
                "paperless-ngx API token in paperless.token"
            )
            raise ValueError(msg)
        return value

    @field_validator("consume_dir", mode="before")
    @classmethod
    def _empty_consume_dir_is_disabled(cls, value: object) -> object:
        """
        Read an empty or whitespace-only ``consume_dir`` as disabled.

        Pydantic would turn ``""`` into ``Path(".")``, and the fallback copy
        would then write scanned PDFs into the working directory.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("consume_dir", mode="after")
    @classmethod
    def _expand_consume_dir(cls, value: Path | None) -> Path | None:
        """
        Expand a leading ``~`` in a configured ``consume_dir``.

        Args:
            value: The validated ``consume_dir``, None when disabled.

        Returns:
            The path with ``~`` expanded (``$VAR`` is not), or None.

        """
        return None if value is None else _expand_user(value)


class ProfileConfig(BaseModel):
    """Scan profile configuration."""

    # The title key is spelled ``title``, its alias; ``default_title`` is not
    # accepted by name, or a table holding both spellings would load one
    # without a word about the other.
    model_config = ConfigDict(extra="forbid")

    # Tool-owned keys, written by ``saneless auto-profiles`` like ``source``.
    # The ``""`` default keeps a config without them loading, and the dropdown
    # renders ``label or name``.
    label: str = Field(default="", max_length=PROFILE_LABEL_MAX_LENGTH)
    description: str = Field(default="", max_length=PROFILE_DESCRIPTION_MAX_LENGTH)

    source: str = "Flatbed"
    # 12,800 dpi is the highest value any SANE backend offers (epson2), so the
    # ceiling refuses no real device.  Pillow's pixel limit depends on the page
    # size too, so it is checked at scan time, not here.
    resolution: WholeNumber = Field(default=DEFAULT_RESOLUTION, ge=1, le=12_800)
    mode: str = "color"
    auto_source_mode: Literal["flatbed", "adf"] = "flatbed"
    # "manual" drives the two-pass flip workflow.  "hardware" sets a separate
    # ADF-mode option to Duplex where the backend has one (epson2, kodakaio,
    # magicolor); elsewhere the source name selects duplex.  Profile fields are
    # deliberately not cross-validated against each other.
    duplex: Literal["none", "hardware", "manual"] = "none"
    paper_size: PaperSize = "full"
    # A malformed id fails the load instead of every scan the profile runs.
    default_tags: Annotated[list[PaperlessId], AfterValidator(_dedupe_ids)] = []
    default_correspondent: PaperlessId | None = None
    # A literal title, not a template.  Bounded because the route's
    # Form(max_length=...) checks only the typed title.
    default_title: str = Field(default="", alias="title", max_length=TITLE_MAX_LENGTH)
    enable_empty_page_detection: bool = True
    # Percent ink coverage inside the edge margin at or below which a page is
    # removed.  The default keeps a lone page number and may keep a dusty blank
    # back: keeping a blank costs a page, dropping content costs the only copy.
    empty_page_coverage_threshold: RealNumber = Field(default=0.001, ge=0.0, le=100.0)
    auto_generated: bool = False

    @model_validator(mode="before")
    @classmethod
    def _translate_legacy_manual_duplex(cls, data: object) -> object:
        """
        Read a legacy ``source = "Manual Duplex"`` profile as manual duplex.

        Runs before field validation so every construction path is covered.
        ``source`` is left verbatim, an explicitly written ``duplex`` is never
        overwritten, and the input mapping is copied, not mutated.
        """
        if isinstance(data, dict) and "duplex" not in data:
            source = data.get("source")
            if isinstance(source, str) and _is_legacy_manual_duplex_source(source):
                return {**data, "duplex": "manual"}
        return data


def resolve_job_title(
    typed: str | None, profile: ProfileConfig | None, *, now: datetime
) -> str:
    """
    Choose a scan job's title by the one rule every front end shares.

    A typed title that is non-blank after stripping wins; otherwise the
    profile's ``title``, when it is non-blank; otherwise ``Scan <time>``,
    rendered through ``local_time`` like the history tables so they cannot
    disagree about when a scan happened. A chosen title is returned as given,
    not stripped.

    Args:
        typed: The title the operator typed, if any.
        profile: The profile the scan uses, if known.
        now: An aware timestamp for the fallback title.

    Returns:
        The title to give the job.

    """
    if typed is not None and typed.strip():
        return typed
    if profile is not None and profile.default_title.strip():
        return profile.default_title
    return f"Scan {local_time(now)}"


class OutputConfig(BaseModel):
    """Output and logging configuration."""

    model_config = ConfigDict(extra="forbid")

    tmp_dir: Path = Field(default_factory=_default_tmp_dir)
    # Durable state, deliberately not under the disposable tmp_dir.  The
    # defaults are computed per instance, not at import, so a changed HOME or
    # XDG_STATE_HOME is honoured.
    data_dir: Path = Field(default_factory=_default_data_dir)
    # The rotating-file keys apply to one-shot CLI commands only: ``serve``
    # logs to stderr and writes no file.  log_level applies to both.
    log_file: Path = Field(default_factory=_default_log_file)
    log_level: LogLevel = "INFO"
    # The rotating handler reads 0 as "never rotate", so the log would grow
    # without bound; at least one byte is required.
    log_max_bytes: WholeNumber = Field(default=10_485_760, ge=1)
    # At least one backup is kept.  A huge count makes every rollover walk that
    # many file names, so a thousand is the ceiling.
    log_backup_count: WholeNumber = Field(default=5, ge=1, le=1_000)
    # Zero would delete every finished job on the next pass; a value above
    # about a century overflows the prune's date arithmetic.
    history_retention_days: WholeNumber = Field(default=7, ge=1, le=36_500)
    # Zero would wipe the history on the next pass, and 2**63 overflows
    # SQLite's integer so that every prune raises.
    history_max_rows: WholeNumber = Field(default=500, ge=1, le=1_000_000)
    # Zero fails every accepted upload as unconfirmed on its first unfinished
    # poll.
    paperless_task_timeout: WholeNumber = Field(default=300, ge=1, le=86_400)
    # 0 means never cached.
    paperless_cache_ttl_seconds: WholeNumber = Field(default=60, ge=0)
    # The one bound on every wait for a person: a flip that times out fails the
    # job, a multi-page wait that times out finishes it with the pages it has.
    # A value above threading.TIMEOUT_MAX makes Event.wait raise OverflowError.
    operator_wait_timeout_seconds: WholeNumber = Field(default=600, ge=1, le=86_400)
    # 0 means no reserve.
    min_free_space_mb: WholeNumber = Field(default=500, ge=0)
    web_host: str = "0.0.0.0"
    # The resolver truncates a service number to 16 bits, so 70000 would
    # quietly bind port 4464.  0 asks the OS for a free port.
    web_port: WholeNumber = Field(default=8080, ge=0, le=65_535)

    @field_validator("tmp_dir", "data_dir", "log_file", mode="after")
    @classmethod
    def _expand_paths(cls, value: Path) -> Path:
        """
        Expand a leading ``~`` in the path settings, never ``$VAR``.

        The defaults are already absolute, so this does not run on them.
        """
        return _expand_user(value)

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        """
        Normalise a configured level name before the ``Literal`` check.

        The name is trimmed and upper-cased, and ``WARN`` is read as
        ``WARNING`` because ``logging.getLevelNamesMapping()`` lists ``WARN``.
        """
        if isinstance(value, str):
            upper = value.strip().upper()
            return "WARNING" if upper == "WARN" else upper
        return value

    @property
    def db_path(self) -> Path:
        """
        Location of the job history database.

        Creates nothing; callers are responsible for making data_dir exist.

        Returns:
            The path to saneless.db inside data_dir.

        """
        return self.data_dir / "saneless.db"

    @property
    def failed_dir(self) -> Path:
        """
        Directory holding scans preserved after a failed pipeline run.

        Creates nothing; callers are responsible for making it exist.

        Returns:
            The path to the failed/ directory inside data_dir.

        """
        return self.data_dir / "failed"


# One ``allowed_hosts`` entry once stripped and lower-cased: dot-separated
# labels of ASCII letters, digits, hyphens and underscores, optionally led by
# one dot.  No room for ``*``, ``:``, ``/``, ``@``, whitespace or ``..``.
_ALLOWED_HOST_ENTRY: Final = re.compile(r"\.?[a-z0-9_-]+(?:\.[a-z0-9_-]+)*")


class WebConfig(BaseModel):
    """Which optional controls the scan form shows, and which names it answers."""

    # A mistyped key cannot quietly leave visible a control the operator meant
    # to hide.
    model_config = ConfigDict(extra="forbid")

    # Hiding a control changes the form and never the scan: the profile's
    # ``default_tags`` and ``default_correspondent`` still apply.
    show_tags: bool = True
    show_correspondent: bool = True
    # Added to the names web/host_guard.py always trusts, never replacing
    # them, so adding a name cannot lock anyone out of the LAN address.
    allowed_hosts: tuple[str, ...] = ()

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _normalise_allowed_hosts(cls, value: object) -> object:
        """
        Strip and lower-case each entry, and refuse one that is not a name.

        There is no ``*``: one line would reopen DNS rebinding for every name
        at once.  A single-label suffix such as ``.com`` is refused, but a
        public multi-label suffix such as ``.co.uk`` is not, and trusts every
        name under it.  The message names the rule, never the value.

        Raises:
            ValueError: An entry is not a host name or a ``.suffix``.

        """
        if not isinstance(value, list | tuple) or not all(
            isinstance(entry, str) for entry in value
        ):
            return value
        entries: dict[str, None] = {}
        for raw in value:
            entry = str(raw).strip()
            # ASCII before lower-casing: str.lower maps a few non-ASCII
            # letters, such as the Kelvin sign, onto ASCII ones.
            entry = entry.lower().removesuffix(".") if entry.isascii() else ""
            if _ALLOWED_HOST_ENTRY.fullmatch(entry) is None or (
                entry.startswith(".") and "." not in entry[1:]
            ):
                msg = (
                    "must be a host name or a .suffix, with no scheme, port, "
                    "path or '*'"
                )
                raise ValueError(msg)
            entries[entry] = None
        return tuple(entries)


# ``web_host`` and ``web_port`` stay in ``[output]``, not ``[web]``: moving
# them would break every deployment that sets them.

_ENV_PREFIX: Final = "SANELESS_"
"""The environment variable prefix; the unknown-variable scan uses it too."""

_ENV_DELIMITER: Final = "__"
"""The nested delimiter in environment variable names."""


class _ByteExactTomlSource(TomlConfigSettingsSource):
    """A TOML source whose top-level section and key matching is byte-exact."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        toml_file: Path | None = None,
    ) -> None:
        """
        Load the file, then restore byte-exact top-level key matching.

        Args:
            settings_cls: The settings class this source feeds.
            toml_file: The TOML file to read.

        """
        # toml_file is passed by keyword, always: upstream has a third
        # positional parameter of its own, so a positional argument here would
        # land in the wrong slot the next time that signature moves.
        super().__init__(settings_cls, toml_file=toml_file)
        # pydantic-settings folds the case of top-level keys, so a miscased
        # [Scanner] would bind into scanner instead of being reported.
        # Reassigning after the base __init__ keeps its nested-default merging,
        # which overriding __call__ would bypass.
        self.init_kwargs = dict(self.toml_data)


# A custom error, not a ValueError, so the rendered line carries no "Value
# error," prefix.
_MISSING_DEFAULT_TYPE: Final = "missing_default_profile"

_MISSING_DEFAULT_MESSAGE: Final = (
    "a [profiles.default] table is required: "
    "it is the profile a scan uses when none is named"
)


class Settings(BaseSettings):
    """
    Application settings with TOML + env var loading.

    Environment variables use the SANELESS_ prefix with __ as the
    nested delimiter. For example, SANELESS_SCANNER__HOST sets
    settings.scanner.host.
    """

    model_config = SettingsConfigDict(
        env_prefix=_ENV_PREFIX,
        env_nested_delimiter=_ENV_DELIMITER,
        # pydantic-settings already forbids unknown top-level names; stated
        # explicitly so it cannot drift from the nested models.
        extra="forbid",
    )

    # default_factory, not a plain instance: an ``OutputConfig()`` default is
    # built once at import and would freeze the HOME/XDG state defaults.
    # ``profiles`` keeps a plain default, which pydantic deep-copies for each
    # instance.
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    paperless: PaperlessConfig = Field(default_factory=PaperlessConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    profiles: dict[str, ProfileConfig] = {"default": ProfileConfig()}

    # A PrivateAttr, not a field: a field would be settable from
    # SANELESS_CONFIG_PATH and from a top-level TOML key, letting either
    # redirect profile writes.
    _config_path: Path | None = PrivateAttr(default=None)

    # A PrivateAttr for the same reason, and left None on a directly
    # constructed Settings: no search ran, so there is nothing to report.
    _config_discovery: ConfigDiscovery | None = PrivateAttr(default=None)

    @property
    def config_path(self) -> Path | None:
        """
        The TOML file these settings were loaded from.

        Only ``load_settings`` records it; nothing in the environment or a
        config file can set it.

        Returns:
            The loaded config file, or None when only defaults and environment
            variables were used.

        """
        return self._config_path

    @property
    def config_discovery(self) -> ConfigDiscovery | None:
        """
        What the search that produced these settings found.

        Only ``load_settings`` records it; nothing in the environment or a
        config file can set it.

        Returns:
            The recording, or None for settings built directly rather than
            loaded.

        """
        return self._config_discovery

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """
        Configure settings sources with optional TOML file support.

        The _toml_file init kwarg is extracted and used to create a
        _ByteExactTomlSource whenever it is passed. The private _skip_env init
        kwarg drops the environment source, so the loader can still validate
        the file when a SANELESS_* variable will not parse. pydantic-settings
        calls this by keyword, so the two unused sources keep their names.
        """
        # saneless reads no .env file and no secrets directory.
        del dotenv_settings, file_secret_settings
        # init_settings is always an InitSettingsSource at runtime
        init_src = cast("InitSettingsSource", init_settings)
        toml_file = init_src.init_kwargs.pop("_toml_file", None)
        skip_env = bool(init_src.init_kwargs.pop("_skip_env", False))

        sources: list[PydanticBaseSettingsSource] = [init_settings]
        if not skip_env:
            sources.append(env_settings)
        if toml_file is not None:
            sources.append(_ByteExactTomlSource(settings_cls, toml_file=toml_file))
        return tuple(sources)

    @field_validator("profiles")
    @classmethod
    def validate_default_profile(
        cls,
        v: dict[str, ProfileConfig],
    ) -> dict[str, ProfileConfig]:
        """
        Ensure a default profile is always defined.

        Raises:
            PydanticCustomError: No profile is named ``default``.

        """
        if "default" not in v:
            raise PydanticCustomError(_MISSING_DEFAULT_TYPE, _MISSING_DEFAULT_MESSAGE)
        return v


def profile_storage_for_loaded(settings: Settings) -> ProfileStorage:
    """
    Say where the profiles live when no write to the config file was attempted.

    The one derivation of that fact, so ``saneless doctor`` and the web status
    strip report the same Profiles row for the same appliance.

    It deliberately cannot report ``IN_MEMORY_UNWRITABLE``: only an attempted
    write can produce that outcome.  No probe can stand in for the write,
    because on a single-file bind mount ``os.access`` says yes and only the
    rename fails with EBUSY.

    Args:
        settings: The settings in hand, loaded and unwritten.

    Returns:
        ``PERSISTED`` when a config file was loaded, otherwise
        ``IN_MEMORY_NO_CONFIG_FILE``.

    """
    if settings.config_path is not None:
        return ProfileStorage.PERSISTED
    return ProfileStorage.IN_MEMORY_NO_CONFIG_FILE


def config_file_state(settings: Settings) -> ConfigFileState:
    """
    Say what the search for a configuration file found.

    The one derivation of that fact, so every surface that reports it
    describes the same appliance the same way.  A second distinct config file
    outranks a leftover under the old name: the file not read may hold an
    edit the operator already made.

    It reads the recording made at load and stats nothing.  Settings built
    directly carry no recording and are reported by whether a path was
    recorded on them.

    Args:
        settings: The settings in hand.

    Returns:
        Which ``ConfigFileState`` this process started in.

    """
    discovery = settings.config_discovery
    if discovery is None:
        loaded_by_path = settings.config_path is not None
        return ConfigFileState.LOADED if loaded_by_path else ConfigFileState.NOT_FOUND
    if discovery.loaded is None:
        if discovery.stale:
            return ConfigFileState.STALE_ONLY
        return ConfigFileState.NOT_FOUND
    # Tested before the leftover: a shadowed file outranks it.
    if len(discovery.found) > 1:
        return ConfigFileState.LOADED_WITH_SHADOWED
    if discovery.stale:
        return ConfigFileState.LOADED_WITH_LEFTOVER
    return ConfigFileState.LOADED


def warn_on_legacy_duplex_sources(settings: Settings) -> None:
    """
    Warn, by profile name, about each profile with a legacy-looking source.

    Called after ``configure_logging``, not from a ``Settings`` validator: a
    validator runs before logging is configured, so its record would never
    reach ``log_file``.  The translation stays in ``ProfileConfig``; the
    naming lives here because a profile cannot name itself.

    A legacy-looking source with an explicit non-manual ``duplex`` is warned
    about too: it is not read as manual duplex, but its source goes to the
    scanner verbatim.

    Args:
        settings: The loaded, already-translated settings.

    """
    for name, profile in settings.profiles.items():
        if not _is_legacy_manual_duplex_source(profile.source):
            continue
        if profile.duplex == "manual":
            logger.warning(
                "Profile %r requests manual duplex through the deprecated "
                'source value %r. saneless has read it as duplex = "manual" '
                'for this run. Update the profile to set duplex = "manual" '
                "and source to a source your scanner actually reports -- run "
                "'saneless devices --capabilities' to list them.",
                name,
                profile.source,
            )
        else:
            logger.warning(
                "Profile %r sets source %r, which looks like the deprecated "
                "manual-duplex value, but also sets duplex = %r, so saneless "
                "has not read it as manual duplex and passes that source to "
                "the scanner unchanged. Set source to a source your scanner "
                "actually reports -- run 'saneless devices --capabilities' to "
                'list them -- and set duplex = "manual" if you meant a manual '
                "duplex scan.",
                name,
                profile.source,
                profile.duplex,
            )


# Hand-maintained in step with ``Settings``' own fields: a section missing here
# loads fine but renders a bare pydantic message instead of the "unknown key
# ...; valid keys: ..." line.  A test over ``Settings``' sections catches drift.
_SECTION_MODELS: Final[dict[str, type[BaseModel]]] = {
    "scanner": ScannerConfig,
    "paperless": PaperlessConfig,
    "output": OutputConfig,
    "web": WebConfig,
}
"""The plain ``Settings`` sections, each a single table of keys."""

_PROFILE_LABEL: Final = "profiles.<name>"
"""How a key that belongs in some profile table is named in an error."""


def _escape_name(name: str) -> str:
    """
    Escape the control characters in a user-controlled name, without quotes.

    TOML quoted keys and profile names can hold newlines or terminal escapes;
    ``repr`` escapes them, so an error line cannot forge further log lines.
    """
    return repr(name)[1:-1]


def _valid_keys(model: type[BaseModel]) -> list[str]:
    """
    List the keys a section accepts, as the operator writes them.

    Args:
        model: The section's model.

    Returns:
        Each field's alias where it has one (``title``), else its name.

    """
    return [field.alias or name for name, field in model.model_fields.items()]


def _match_candidates(model: type[BaseModel]) -> list[str]:
    """
    List the spellings a close-match suggestion is drawn from.

    The field names are included beside the aliases so a near miss of a
    field's code name (``default_titel``) is still recognised; the caller
    turns a matched field name into the alias the section accepts.
    """
    names = list(model.model_fields)
    return names + [field.alias for field in model.model_fields.values() if field.alias]


def _owner_hint(key: str, *, exclude: type[BaseModel] | None) -> str | None:
    """
    Say which section a misplaced key really belongs in, and how it is written.

    A key that is the code name of a field with a different config spelling
    (``default_title``, written ``title``) is sent on with that spelling, so
    the next load does not trade this refusal for the owner's.

    Returns:
        ``it belongs in [section]``, with `` as 'spelling'`` appended when the
        key is written differently there; None when no section owns the key.

    """
    owners: list[tuple[str, type[BaseModel]]] = [
        *_SECTION_MODELS.items(),
        (_PROFILE_LABEL, ProfileConfig),
    ]
    for label, model in owners:
        if model is exclude:
            continue
        for name, field in model.model_fields.items():
            spelling = field.alias or name
            if key == spelling:
                return f"it belongs in [{label}]"
            if key == name:
                return f"it belongs in [{label}] as {spelling!r}"
    return None


def _format_loc_path(parts: Sequence[str | int]) -> str:
    """
    Render the key path below a section, e.g. ``default_tags[0]``.

    Args:
        parts: The ``loc`` elements after the section (and profile name).

    Returns:
        String elements joined by ``.`` and escaped, integers as ``[i]``.

    """
    rendered = ""
    for part in parts:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            escaped = _escape_name(part)
            rendered = f"{rendered}.{escaped}" if rendered else escaped
    return rendered


def _describe_unknown_key(
    label: str, key: str, model: type[BaseModel], *, variable: str | None
) -> str:
    """
    Describe an unknown key inside a section or profile table.

    A key that is the code name of a field with a different config spelling
    (``default_title``, spelled ``title``) is told which key to write. A
    close-match suggestion is always given in its config spelling too, and
    never suggests the unknown key back to itself.

    Args:
        label: The section label, e.g. ``paperless`` or ``profiles.default``.
        key: The unknown key.
        model: The model of the table the key was found in.
        variable: The environment variable that supplied the key, if any.

    Returns:
        The error line, without indentation.

    """
    subject = (
        f"[{label}] unknown key {key!r}"
        if variable is None
        else f"environment variable {variable!r}: unknown key {key!r} in [{label}]"
    )
    owner = _owner_hint(key, exclude=model)
    if owner is not None:
        return f"{subject}; {owner}"
    spelled = {name: field.alias for name, field in model.model_fields.items()}
    alias = spelled.get(key)
    if alias is not None and alias != key:
        hint_text = f" (write it as {alias!r})"
    else:
        candidates = [name for name in _match_candidates(model) if name != key]
        hint = difflib.get_close_matches(key, candidates, n=1)
        hint_text = (
            f" (did you mean {(spelled.get(hint[0]) or hint[0])!r}?)" if hint else ""
        )
    valid = ", ".join(_valid_keys(model))
    return f"{subject}{hint_text}; valid keys: {valid}"


def _describe_unknown_top_level(name: str) -> str:
    """
    Describe an unknown top-level name.

    A miscased section (``[Paperless]``) is suggested as the real section,
    never as ``[profiles.Paperless]``; anything that is not a known key is
    most likely a profile table written without its ``profiles.`` prefix.
    """
    sections = list(Settings.model_fields)
    for section in sections:
        if name.casefold() == section:
            return f"unknown section {name!r} (did you mean [{section}]?)"
    owner = _owner_hint(name, exclude=None)
    if owner is not None:
        return f"unknown key {name!r} at the top level; {owner}"
    hints = [f"[{match}]" for match in difflib.get_close_matches(name, sections, n=1)]
    hints.append(f"[profiles.{_escape_name(name)}]")
    return (
        f"unknown section {name!r} (did you mean {' or '.join(hints)}?); "
        f"valid sections: {', '.join(sections)}"
    )


def _env_contribution() -> dict[str, object]:
    """
    Return what the SANELESS_* environment contributes to the settings.

    pydantic-settings' own source does the work, so error attribution cannot
    drift from what was actually loaded.

    Raises:
        SettingsError: If a complex field's variable holds invalid JSON.

    """
    return EnvSettingsSource(Settings)()


# Fixed text: the variable's value is never part of the line, because a
# JSON-valued section variable is exactly where a token can travel.
_ENV_JSON_MESSAGE: Final = (
    "must be JSON (a list or table is written as JSON, for example [3, 7])"
)


def _bad_json_variables() -> list[str]:
    """
    Name every SANELESS_* variable that holds invalid JSON for its field.

    Each variable is handed alone to pydantic-settings' own
    ``EnvSettingsSource``, so the blame follows the loader's own rules.  The
    ``SettingsError`` is dropped unread: its text may quote the value.

    Returns:
        The offending names as spelled, sorted.

    """
    prefix = _ENV_PREFIX.casefold()
    bad: list[str] = []
    for name, value in os.environ.items():
        if not name.casefold().startswith(prefix):
            continue
        source = EnvSettingsSource(Settings)
        # The source lower-cases its own keys, so this one matches them.
        source.env_vars = {name.lower(): value}
        try:
            source()
        except SettingsError:
            bad.append(name)
    return sorted(bad)


def _env_json_lines(exc: SettingsError) -> list[str]:
    """
    Render the environment source's refusal as one line per bad variable.

    Args:
        exc: What the environment source raised for the whole environment.

    Returns:
        One fixed-text line per variable holding invalid JSON. If no single
        variable is to blame, one line from ``exc`` with every SANELESS_*
        value struck out of it.

    """
    names = _bad_json_variables()
    if names:
        return [f"environment variable {name!r}: {_ENV_JSON_MESSAGE}" for name in names]
    # The wording is upstream-owned, so the SANELESS_* values are struck out
    # of it: no upstream phrasing can put one in this line.
    return [f"environment: {_escape_name(_redact_environment(str(exc)))}"]


def _env_variable_for(
    loc: tuple[str | int, ...], env_data: Mapping[str, object]
) -> str | None:
    """
    Name the environment variable an error's value came from, if any.

    Environment beats file after the sources merge, so a value present in
    the case-folded ``env_data`` is the one that failed; a key the walk does
    not find there came from the file.  The longest matching variable name is
    tried first, then a nested one, then each shorter prefix, which covers a
    JSON-valued variable such as ``SANELESS_OUTPUT``.

    Returns:
        The variable name as spelled in the environment, or None.

    """
    parts: list[str] = []
    node: object = env_data
    for element in loc:
        if not isinstance(element, str) or not isinstance(node, dict):
            break
        if element not in node:
            return None
        parts.append(element)
        node = cast("dict[str, object]", node)[element]
    if not parts:
        return None
    by_folded = {name.casefold(): name for name in os.environ}
    full = (_ENV_PREFIX + _ENV_DELIMITER.join(parts)).casefold()
    if full in by_folded:
        return by_folded[full]
    if isinstance(node, dict):
        nested = full + _ENV_DELIMITER.casefold()
        deeper = sorted(
            name for folded, name in by_folded.items() if folded.startswith(nested)
        )
        if deeper:
            return deeper[0]
    for end in range(len(parts) - 1, 0, -1):
        candidate = (_ENV_PREFIX + _ENV_DELIMITER.join(parts[:end])).casefold()
        if candidate in by_folded:
            return by_folded[candidate]
    return None


_MIN_REDACTED_INPUT: Final = 2
"""The shortest input string worth redacting from an upstream message."""

_REDACTED: Final = "<value omitted>"
"""What stands in for an input value that must not reach a rendered line."""

_MIN_WITHHELD_INPUT: Final = 8
"""The length at which an input buried inside a word means upstream put it there.

The longest word pydantic builds its messages from is ``integer``, at seven
characters, so a value this long cannot be a coincidental fragment of its
prose.  Shorter values collide with ordinary English by chance (``in`` lives
inside ``integer``).
"""


def _input_texts(value: object) -> Iterator[str]:
    """
    Yield every string an error's ``input`` carries, however it is nested.

    ``SANELESS_PAPERLESS__TOKEN__X=<token>`` makes pydantic build a mapping
    for ``token``, so the credential arrives as ``{"x": "<token>"}``.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _input_texts(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            yield from _input_texts(item)


def _redact_input(text: str, value: object) -> str:
    """
    Remove an error's input text from an upstream message fragment.

    For ``tokne = "..."`` the input is the Paperless token, and pydantic's
    wording is not this project's.  Only the message is redacted: the rest of
    the line is this module's own words, and striking across it would lose
    the did-you-mean hint whenever a value equals a field name.

    An occurrence is struck only where no word character touches either edge,
    so ``web_port = "in"`` does not corrupt "integer".  A long value upstream
    glued to its own words withholds the whole message instead.
    """
    for secret in _input_texts(value):
        if len(secret) < _MIN_REDACTED_INPUT:
            continue
        text = re.sub(rf"(?<!\w){re.escape(secret)}(?!\w)", _REDACTED, text)
        if secret in text and len(secret) >= _MIN_WITHHELD_INPUT:
            return _REDACTED
    return text


def _redact_environment(text: str) -> str:
    """
    Strike every ``SANELESS_*`` value out of an upstream message.

    The error arrives before any field is built, so there is no pydantic
    ``input``; the environment's own values are struck out instead.  The name
    is matched case-insensitively because pydantic-settings reads
    ``saneless_paperless__token`` too: whatever it reads, this must strike.
    """
    values = [
        value
        for name, value in os.environ.items()
        if name.upper().startswith(_ENV_PREFIX)
    ]
    return _redact_input(text, values)


def _render_error(
    loc: tuple[str | int, ...],
    error_type: str,
    message: str,
    env_data: Mapping[str, object],
    value: object,
) -> str:
    """
    Render one pydantic error as a ``[section] key`` line.

    The input's text is struck out of ``message`` here, before any branch
    composes a line, so no caller can get a line built from an unredacted
    upstream message.

    Args:
        loc: The error's location.
        error_type: The error's pydantic type, e.g. ``extra_forbidden``.
        message: pydantic's short ``msg``, whose wording is upstream-owned.
        env_data: The environment's contribution, for attribution.
        value: The error's ``input`` member -- the text that must not reach
            the rendered line.

    Returns:
        The error line, without indentation.

    """
    message = _redact_input(message, value)
    head = loc[0] if loc else None
    if not isinstance(head, str) or head not in Settings.model_fields:
        if error_type == "extra_forbidden" and len(loc) == 1:
            return _describe_unknown_top_level(str(head))
        return f"{_format_loc_path(loc) or '(settings)'}: {message}"
    model: type[BaseModel] | None
    if head == "profiles" and len(loc) > 1:
        label = f"profiles.{_escape_name(str(loc[1]))}"
        model, rest = ProfileConfig, loc[2:]
    else:
        label, model, rest = head, _SECTION_MODELS.get(head), loc[1:]
    variable = _env_variable_for(loc, env_data)
    if (
        error_type == "extra_forbidden"
        and model is not None
        and len(rest) == 1
        and isinstance(rest[0], str)
    ):
        return _describe_unknown_key(label, rest[0], model, variable=variable)
    key_path = _format_loc_path(rest)
    if variable is not None:
        where = f"{key_path} in [{label}]" if key_path else f"[{label}]"
        return f"environment variable {variable!r}: {where}: {message}"
    return f"[{label}] {key_path}: {message}" if key_path else f"[{label}]: {message}"


def _render_error_lines(
    errors: Sequence[ErrorDetails], env_data: Mapping[str, object]
) -> list[str]:
    """
    Render every validation error as one line, sorted for stable output.

    Only ``loc``, ``type`` and ``msg`` are rendered; ``input`` is read only to
    strike its own text out of ``msg``, so a rendered line cannot carry a
    config value.  Redaction happens before the sort.
    """
    return sorted(
        _render_error(err["loc"], err["type"], err["msg"], env_data, err["input"])
        for err in errors
    )


def _suggest_env_name(first: str, rest: str) -> str:
    """
    Suggest the variable an unknown SANELESS_* name was probably meant to be.

    difflib's cutoff misses the common single-underscore mistake
    (``SANELESS_OUTPUT_WEB_PORT`` scores about 0.57 against ``output``), so a
    first segment that starts with ``<section>_`` is checked first.

    Args:
        first: The case-folded first segment after the prefix.
        rest: Everything after the first ``__``, as spelled.

    Returns:
        A `` (did you mean SANELESS_...?)`` hint, or an empty string.

    """
    sections = list(Settings.model_fields)
    segments: list[str] = []
    for section in sections:
        if first.startswith(f"{section}_"):
            segments = [section, first.removeprefix(f"{section}_")]
            break
    else:
        match = difflib.get_close_matches(first, sections, n=1)
        if match:
            segments = [match[0]]
    if not segments:
        return ""
    if rest:
        segments.append(rest)
    suggestion = (_ENV_PREFIX + _ENV_DELIMITER.join(segments)).upper()
    return f" (did you mean {_escape_name(suggestion)}?)"


def _unknown_env_lines(environ: Mapping[str, str]) -> list[str]:
    """
    Reject SANELESS_* variables whose first segment names no section.

    pydantic-settings silently ignores them, so ``SANELESS_PAPERLES__TOKEN``
    would leave the token unset without a word.  This runs in the loader, not
    a ``Settings`` validator, so direct construction is unaffected.

    Args:
        environ: The process environment.

    Returns:
        One sorted error line per unknown variable; names only, never values.

    """
    prefix = _ENV_PREFIX.casefold()
    valid = ", ".join(Settings.model_fields)
    lines: list[str] = []
    for name in environ:
        if not name.casefold().startswith(prefix):
            continue
        first, _, rest = name[len(prefix) :].partition(_ENV_DELIMITER)
        first = first.casefold()
        if first in Settings.model_fields:
            continue
        hint = _suggest_env_name(first, rest)
        lines.append(
            f"environment variable {name!r}: unknown section {first!r}{hint}; "
            f"valid sections: {valid}"
        )
    return sorted(lines)


class _SettingsFactory(Protocol):
    """
    Callable view of ``Settings`` that accepts its private init kwargs.

    Neither ``_toml_file`` nor ``_skip_env`` is a declared field; both are
    popped out of ``init_kwargs`` by ``settings_customise_sources``.
    """

    def __call__(
        self, *, _toml_file: Path | None = None, _skip_env: bool = False
    ) -> Settings:
        """
        Construct ``Settings``, optionally from a TOML file.

        ``_toml_file`` names the file to read, None for none. ``_skip_env``
        leaves the SANELESS_* environment out, so the file and the defaults
        are validated alone.
        """
        ...


def _build_settings(
    toml_file: Path | None = None,
) -> Settings:
    """
    Build Settings, rendering every validation error into one ConfigError.

    Each error becomes one line under one header; an error whose value came
    from the environment names the variable.  A variable holding invalid JSON
    stops the environment source, so the file is then validated without the
    environment and its own errors join the same list.

    Raises:
        ConfigError: If anything above failed; the message holds no input value.

    """
    lines = _unknown_env_lines(os.environ)
    settings: Settings | None = None
    cause: Exception | None = None
    env_data: Mapping[str, object]
    try:
        env_data = _env_contribution()
    except SettingsError as exc:
        # The exception (and its JSON-decoding cause) is not chained: its
        # text is upstream-owned and may quote the value.
        lines.extend(_env_json_lines(exc))
        env_data, skip_env = {}, True
    else:
        skip_env = False
    try:
        settings = cast("_SettingsFactory", Settings)(
            _toml_file=toml_file, _skip_env=skip_env
        )
    except ValidationError as exc:
        lines.extend(_render_error_lines(exc.errors(), env_data))
    # Only the position and the parser's own words are rendered: the decode
    # errors' ``doc`` and ``object`` hold file content, possibly the token.
    except tomllib.TOMLDecodeError as exc:
        lines.append(f"line {exc.lineno}, column {exc.colno}: {_escape_name(exc.msg)}")
        cause = exc
    except UnicodeDecodeError as exc:
        lines.append(f"the file is not valid UTF-8 (byte {exc.start}: {exc.reason})")
        cause = exc
    except OSError as exc:
        lines.append(f"cannot read the file: {exc.strerror or type(exc).__name__}")
        cause = exc
    if settings is not None and not lines:
        return settings
    lines.sort()
    header = (
        f"Configuration error in {toml_file}:"
        if toml_file is not None
        else "Configuration error (defaults and environment):"
    )
    msg = "\n".join([header, *(f"  {line}" for line in lines)])
    # A ValidationError stays unchained: its str() embeds the inputs, possibly
    # the token, and a traceback prints the chain.  The other causes are
    # chained: none of their str() forms holds file content.
    if cause is not None:
        raise ConfigError(msg) from cause
    raise ConfigError(msg) from None


def nearest_existing_ancestor(path: Path) -> Path:
    """
    Find the deepest existing path among ``path`` and its ancestors.

    A directory that does not exist yet is created there, so this is where
    creating it would fail. Start-up judges a missing directory setting by it,
    and so does the status strip's Data folder row.

    Args:
        path: A directory that may not exist yet.

    Returns:
        ``path`` itself if it exists, else its nearest existing ancestor, else
        the path's anchor (``.`` for a relative path).

    """
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path(path.anchor or ".")


def _require_writable(label: str, directory: Path) -> None:
    """
    Raise ConfigError unless ``directory`` is, or could be created as, a directory.

    A missing directory is judged by its nearest existing ancestor.  The type
    is checked before writability, because ``os.access`` calls a regular file
    writable.

    Raises:
        ConfigError: The directory, or its nearest existing ancestor, is not
            a writable directory.

    """
    ancestor = nearest_existing_ancestor(directory)
    if not ancestor.is_dir():
        if ancestor == directory:
            msg = f"{label} is not a directory: {directory}"
        else:
            msg = f"{label} parent is not a directory: {ancestor}"
        raise ConfigError(msg)
    if os.access(ancestor, os.W_OK):
        return
    if ancestor == directory:
        msg = f"{label} is not writable: {directory}"
    else:
        msg = f"{label} parent is not writable: {ancestor}"
    raise ConfigError(msg)


def validate_settings_dirs(settings: Settings) -> None:
    """
    Fail fast with ConfigError if a configured directory is unusable or unsafe.

    Validates each directory at startup so errors surface immediately rather
    than mid-scan.  A missing directory is checked against its nearest
    existing ancestor, so ``<unwritable>/a/b/c`` and ``<file>/a/b`` fail here
    too.

    An existing ``tmp_dir`` must also be private: a symlink, a directory
    owned by another user, or one with group- or world-write is refused, as
    ``check_private_dir`` describes. A missing one is not created here; the
    server and each scan create it 0700 when they first need it.

    Args:
        settings: Application settings to validate.

    Raises:
        ConfigError: If any configured directory is not a directory or not
            writable, or an existing ``tmp_dir`` is not safe to use.

    """
    tmp_dir = settings.output.tmp_dir
    if os.path.lexists(tmp_dir):
        check_private_dir(tmp_dir, key="output.tmp_dir")
    _require_writable("tmp_dir", tmp_dir)
    _require_writable("data_dir", settings.output.data_dir)
    if settings.paperless.consume_dir is not None:
        _require_writable("consume_dir", settings.paperless.consume_dir)


def config_search_paths() -> tuple[Path, ...]:
    """
    List the config file locations searched when no explicit path is given.

    The single search list for both loading and the CLI's write target:
    ``./saneless.toml``, then ``$XDG_CONFIG_HOME/saneless/saneless.toml``
    (``~/.config`` when unset), then ``/etc/saneless/saneless.toml``. A
    function rather than a module constant so HOME and ``$XDG_CONFIG_HOME``
    are read when called, not at import.  A file under the superseded name in
    one of these directories is detected and reported, never read.

    Returns:
        The candidate paths, in search order.

    """
    return (
        Path(CONFIG_FILENAME),
        xdg_config_home() / "saneless" / CONFIG_FILENAME,
        Path("/etc/saneless") / CONFIG_FILENAME,
    )


def _pin_relative_paths(settings: Settings, base: Path | None) -> None:
    """
    Make every relative path setting absolute, joined onto ``base``.

    ``base`` is the loaded file's directory, else the working directory, else
    None.  Pinned once at load, every command opens the same job database,
    whichever source supplied a value.  ``base`` comes from
    ``Path.absolute()``, never ``resolve()``, so a symlinked file is anchored
    where the operator named it.

    Raises:
        ConfigError: A value is relative and ``base`` is None.

    """

    def pin(key: str, value: Path) -> Path:
        if value.is_absolute():
            return value
        if base is None:
            msg = (
                f"{key} is relative, and the working directory no longer exists "
                "to resolve it against; cd to an existing directory, pass "
                "--config, or make the path absolute"
            )
            raise ConfigError(msg)
        return base / value

    output = settings.output
    settings.output = output.model_copy(
        update={
            "tmp_dir": pin("output.tmp_dir", output.tmp_dir),
            "data_dir": pin("output.data_dir", output.data_dir),
            "log_file": pin("output.log_file", output.log_file),
        }
    )
    consume_dir = settings.paperless.consume_dir
    if consume_dir is not None:
        settings.paperless = settings.paperless.model_copy(
            update={"consume_dir": pin("paperless.consume_dir", consume_dir)}
        )


_USUAL_SEARCH: Final = "to use the usual search"
"""How the ``--config`` next steps end: what leaving the option out does."""


def load_settings(config_path: str | None = None) -> Settings:
    """
    Load settings from TOML file with env var overrides.

    Every path setting is absolute on return. A relative ``tmp_dir``,
    ``data_dir``, ``log_file`` or ``consume_dir``, whether from the file or
    the environment, is resolved against the loaded file's directory, or
    against the working directory when no file was loaded.

    Args:
        config_path: Explicit path to a TOML config file. If not None,
            loads from that path directly; an empty string is an error, not
            a request to search. When None, searches
            ``config_search_paths()``.

    Returns:
        Fully validated Settings instance carrying ``config_path``: the
        explicit path with ``~`` expanded, else the first search path that is
        a regular file, else None when no file was found.  A path is always
        recorded absolute.

    Raises:
        ConfigError: If an explicit path is empty, cannot have its ``~``
            expanded, is missing or is not a regular file, if no file was
            loaded, the working directory no longer exists and a path setting
            is relative, if the file cannot be read, is not UTF-8 or is not valid TOML
            (chained to the OSError, UnicodeDecodeError or TOMLDecodeError),
            or if the configuration fails validation.

    """
    path: Path | None
    if config_path == "":
        # ``--config "$CFG"`` with CFG unset or empty is an explicit path that
        # names nothing, not "no path": discovery here would load, and
        # auto-profiles would write, whatever file happens to be found.
        msg = "Config file path is empty (was --config given an unset variable?)"
        raise ConfigError(
            msg,
            next_step=(
                "Give --config the path of a saneless config file, or leave "
                f"--config out {_USUAL_SEARCH}."
            ),
        )
    if config_path is not None:
        try:
            explicit = Path(config_path).expanduser()
        except RuntimeError:
            # ``~nosuchuser/...``, or ``~`` with no home directory.
            msg = f"Cannot expand '~' in --config path: {config_path}"
            raise ConfigError(
                msg,
                next_step=(
                    "Give --config a path without '~', or one whose '~' names "
                    f"an existing user, or leave --config out {_USUAL_SEARCH}."
                ),
            ) from None
        # A directory counts as missing: Docker creates one where a
        # single-file bind mount's source does not exist.
        if not explicit.is_file():
            msg = f"Config file not found or not a regular file: {explicit}"
            raise ConfigError(
                msg,
                next_step=(
                    "Give --config the path of an existing saneless config "
                    f"file, or leave --config out {_USUAL_SEARCH}."
                ),
            )
        explicit = explicit.absolute()
        path = explicit
        discovery = ConfigDiscovery(
            explicit=explicit,
            searched=(),
            found=(explicit,),
            loaded=explicit,
            stale=(),
        )
    else:
        discovery = discover_config(config_search_paths())
        path = discovery.loaded
    settings = _build_settings(toml_file=path)
    _pin_relative_paths(
        settings, path.parent if path is not None else _working_directory()
    )
    settings._config_path = path
    settings._config_discovery = discovery
    return settings


def _dotted_leaves(node: Mapping[str, object], prefix: str) -> Iterator[str]:
    """
    Yield the dotted names of the leaves of a nested mapping.

    A non-empty mapping is descended into; anything else is a leaf. Each
    segment is escaped, because profile names reach the log.
    """
    for key, value in node.items():
        name = f"{prefix}.{_escape_name(key)}" if prefix else _escape_name(key)
        if isinstance(value, dict) and value:
            yield from _dotted_leaves(cast("dict[str, object]", value), name)
        else:
            yield name


def env_sourced_keys() -> list[str]:
    """
    List the settings that SANELESS_* environment variables supply.

    Names only, never values: the Paperless token is commonly one of them.
    A JSON-valued section such as ``SANELESS_OUTPUT`` is reported by its
    leaves.

    Returns:
        Sorted dotted names, e.g. ``["paperless.token", "paperless.url"]``.

    """
    return sorted(_dotted_leaves(_env_contribution(), ""))


def log_config_sources(settings: Settings) -> None:
    """
    Log, once at INFO, where the configuration came from.

    Names the loaded file, or every path searched when none loaded, and the
    dotted names of the environment-sourced keys; values are never logged.
    Called after ``configure_logging``, so the record reaches ``log_file``,
    and only after a successful load, so the environment is known to parse.

    Each config file found and not read, and each superseded-name file, gets
    its own warning: the one an operator edits may be the one ignored, and a
    leftover can hold the only copy of the URL and token.

    Args:
        settings: The loaded settings.

    """
    state = config_file_state(settings)
    discovery = settings.config_discovery
    if settings.config_path is not None:
        source = str(settings.config_path.absolute())
    else:
        searched = ", ".join(
            str(absolute_or_as_spelled(candidate))
            for candidate in (discovery.searched if discovery is not None else ())
        )
        source = "no config file; defaults + environment"
        if searched:
            source = f"{source}; searched {searched}"
    keys = ", ".join(env_sourced_keys()) or "(none)"
    logger.info("Configuration: %s; from environment: %s", source, keys)
    if discovery is None:
        return
    loaded = discovery.loaded
    if loaded is not None:
        for other in discovery.found:
            if other == loaded:
                continue
            logger.warning(
                "Not reading %s: %s is in use and was found first; move anything "
                "you still need from it into %s, or delete it if it is not "
                "deliberate",
                other.absolute(),
                loaded.absolute(),
                loaded.absolute(),
            )
    for stale in discovery.stale:
        if state is ConfigFileState.STALE_ONLY:
            logger.warning(
                "Ignoring %s: saneless reads %s, not %s; rename it to %s, "
                "then restart saneless",
                stale.absolute(),
                CONFIG_FILENAME,
                LEGACY_CONFIG_FILENAME,
                stale.with_name(CONFIG_FILENAME).absolute(),
            )
        elif discovery.loaded is not None:
            logger.warning(
                "Ignoring leftover %s: %s is in use; move anything you still "
                "need from it into %s, then delete it",
                stale.absolute(),
                discovery.loaded.absolute(),
                discovery.loaded.absolute(),
            )
