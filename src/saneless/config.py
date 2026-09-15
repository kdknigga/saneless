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
import tempfile
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Protocol, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)
from pydantic_settings.exceptions import SettingsError

from saneless.exceptions import ConfigError
from saneless.vocabulary import TITLE_MAX_LENGTH

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from datetime import datetime

    from pydantic_core import ErrorDetails
    from pydantic_settings.main import InitSettingsSource

__all__ = [
    "DEFAULT_RESOLUTION",
    "LogLevel",
    "OutputConfig",
    "PaperlessConfig",
    "ProfileConfig",
    "ScannerConfig",
    "Settings",
    "config_search_paths",
    "env_sourced_keys",
    "load_settings",
    "log_config_sources",
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
standard for professional document scanning. See Phase 11 research.
"""

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
"""The logging level names ``output.log_level`` accepts (CFG-04, M-21).

Each is a key of ``logging.getLevelNamesMapping()`` and, lower-cased, a valid
uvicorn ``log_level``.
"""


def _xdg_base(variable: str, *fallback: str) -> Path:
    """
    Resolve an XDG base directory from the environment at call time (CFG-03).

    Per the XDG Base Directory Specification, an unset or empty variable means
    the ``$HOME``-relative default, and a relative value is invalid and ignored
    -- otherwise discovery would depend on the working directory (T-27-25).

    Args:
        variable: The environment variable, e.g. ``XDG_CONFIG_HOME``.
        *fallback: The path segments of the default below ``$HOME``.

    Returns:
        The variable's value when it is a non-empty absolute path, else
        ``$HOME`` joined with ``fallback``.

    """
    value = os.environ.get(variable, "")
    if value and Path(value).is_absolute():
        return Path(value)
    return Path.home().joinpath(*fallback)


def xdg_config_home() -> Path:
    """
    Return the XDG config home, ``$XDG_CONFIG_HOME`` or ``~/.config`` (CFG-03).

    Read at call time, not import, so a later HOME or XDG change is honoured.
    An empty or relative ``$XDG_CONFIG_HOME`` is ignored, per the basedir spec.

    Returns:
        The base directory user configuration files are searched under.

    """
    return _xdg_base("XDG_CONFIG_HOME", ".config")


def xdg_state_home() -> Path:
    """
    Return the XDG state home, ``$XDG_STATE_HOME`` or ``~/.local/state`` (CFG-03).

    Read at call time, not import, so a later HOME or XDG change is honoured.
    An empty or relative ``$XDG_STATE_HOME`` is ignored, per the basedir spec.

    Returns:
        The base directory durable state (database, log) defaults live under.

    """
    return _xdg_base("XDG_STATE_HOME", ".local", "state")


def _default_data_dir() -> str:
    """
    Compute the default ``output.data_dir``: ``$XDG_STATE_HOME/saneless``.

    Returns:
        The default durable state directory, as a string.

    """
    return str(xdg_state_home() / "saneless")


def _default_log_file() -> str:
    """
    Compute the default ``output.log_file``, inside the default ``data_dir``.

    Returns:
        ``$XDG_STATE_HOME/saneless/saneless.log``, as a string.

    """
    return str(xdg_state_home() / "saneless" / "saneless.log")


def _expand_user(value: str) -> str:
    """
    Expand a leading ``~`` in a path setting (CFG-03, M-20).

    Only ``~`` is expanded, by decision: ``$VAR`` is left literal, so a value
    cannot silently pick up an unrelated environment variable (T-27-26). An
    empty value is returned unchanged -- for ``consume_dir`` it means disabled.

    Args:
        value: The configured path string.

    Returns:
        The path with ``~`` expanded, or the empty string unchanged.

    """
    return str(Path(value).expanduser()) if value else value


def _is_legacy_manual_duplex_source(source: str) -> bool:
    """
    Recognise the deprecated ``source = "Manual Duplex"`` config form (DPLX-02).

    This exists ONLY to detect a legacy profile at config load so it can be
    translated to ``duplex = "manual"``, and so ``warn_on_legacy_duplex_sources``
    can warn about it once logging is configured. It is never consulted to
    choose a scanning strategy: ``pipeline._is_manual_duplex`` is deleted in
    favour of ``ProfileConfig.duplex``, and ``source`` is a pure SANE value.

    Args:
        source: The profile's configured source string.

    Returns:
        True if the source contains both "manual" and "duplex", ignoring case.

    """
    lowered = source.lower()
    return "manual" in lowered and "duplex" in lowered


class ScannerConfig(BaseModel):
    """Scanner connection settings."""

    # An unknown key is an error, not silently dropped (CFG-01, M-18).
    model_config = ConfigDict(extra="forbid")

    host: str = ""
    device: str = ""


class PaperlessConfig(BaseModel):
    """Paperless-ngx API connection settings."""

    # A mistyped ``tokne`` used to leave the token unset without a word
    # (CFG-01, M-18).
    model_config = ConfigDict(extra="forbid")

    url: str = ""
    # Masked in repr, tracebacks and model_dump (CFG-05, N-15). Unwrapped with
    # get_secret_value only where PaperlessClient is built: cli.py scan and
    # web/app.py create_app.
    token: SecretStr = SecretStr("")
    consume_dir: str = ""

    @field_validator("consume_dir", mode="after")
    @classmethod
    def _expand_consume_dir(cls, value: str) -> str:
        """
        Expand a leading ``~`` in ``consume_dir``; empty stays empty (CFG-03).

        Args:
            value: The validated ``consume_dir``.

        Returns:
            The value with ``~`` expanded; ``$VAR`` is not expanded.

        """
        return _expand_user(value)


class ProfileConfig(BaseModel):
    """Scan profile configuration."""

    # extra="forbid" (CFG-01) is safe alongside the legacy-duplex
    # before-validator: it only ever adds ``duplex``, which is a real field.
    # populate_by_name keeps both ``title`` and ``default_title`` accepted.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: str = "Flatbed"
    resolution: int = DEFAULT_RESOLUTION
    mode: str = "color"
    auto_source_mode: Literal["flatbed", "adf"] = "flatbed"
    # How the profile scans both sides of a sheet. "manual" drives the two-pass
    # flip workflow. Nothing reads "hardware": the device decides duplexing
    # from the source name it is handed, so the value only records operator
    # intent and makes a profile self-describing. Phase 30's APPL-05 (the
    # generated label/description) is its eventual reader. It is deliberately
    # not cross-validated against a FEEDER_DUPLEX source -- profile fields have
    # never been cross-checked (auto_source_mode is not checked against Auto).
    duplex: Literal["none", "hardware", "manual"] = "none"
    paper_size: Literal["full", "a3", "a4", "a5", "letter", "legal"] = "full"
    default_tags: list[int] = []
    default_correspondent: int | None = None
    # A literal title, not a template: no placeholder vocabulary (D-15). Used
    # when a scan is submitted with a blank title (resolve_job_title, D-16).
    # Bounded because the route's Form(max_length=...) only checks the typed
    # title, so an unbounded profile title would bypass ROBU-08.
    default_title: str = Field(default="", alias="title", max_length=TITLE_MAX_LENGTH)
    empty_page_mean_threshold: float = 250.0
    empty_page_stddev_threshold: float = 5.0
    enable_empty_page_detection: bool = True
    auto_generated: bool = False

    @model_validator(mode="before")
    @classmethod
    def _translate_legacy_manual_duplex(cls, data: object) -> object:
        """
        Read a legacy ``source = "Manual Duplex"`` profile as manual duplex.

        Runs before field validation so every construction path is covered:
        TOML, environment variables, and direct ``ProfileConfig(...)`` calls.
        ``source`` is left verbatim, and an explicitly written ``duplex`` is
        never overwritten -- explicit configuration beats the inference.
        The input mapping is copied, not mutated.

        Args:
            data: The raw input before pydantic validates it.

        Returns:
            The input, with ``duplex = "manual"`` added for a legacy source.

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
    Choose a scan job's title by the one rule every front end shares (D-16).

    A typed title that is non-blank after stripping wins; otherwise the
    profile's ``title``, when it is non-blank; otherwise ``Scan <time>``. The
    timestamp is rendered in UTC whatever the zone of ``now`` (local time is
    APPL-12). A chosen title is returned as given, not stripped.

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
    return f"Scan {now.astimezone(UTC).strftime('%Y-%m-%d %H:%M')}"


class OutputConfig(BaseModel):
    """Output and logging configuration."""

    # An unknown key is an error, not silently dropped (CFG-01, M-18).
    model_config = ConfigDict(extra="forbid")

    tmp_dir: str = str(Path(tempfile.gettempdir()) / "saneless")
    # Durable state: the job database and preserved scans. Deliberately NOT
    # under tmp_dir, which is disposable scratch space. Phase 23 (D-14) kept
    # the data_dir and log_file defaults in step; both now follow
    # $XDG_STATE_HOME (CFG-03), computed per instance rather than at import.
    # The Dockerfile's SANELESS_OUTPUT__DATA_DIR still overrides data_dir.
    data_dir: str = Field(default_factory=_default_data_dir)
    log_file: str = Field(default_factory=_default_log_file)
    log_level: LogLevel = "INFO"
    log_max_bytes: int = 10_485_760
    log_backup_count: int = 5
    history_retention_days: int = 7
    history_max_rows: int = 500
    paperless_task_timeout: int = 300
    paperless_cache_ttl_seconds: int = 60
    # How long a manual-duplex job waits for the operator to flip the stack.
    # A config key, unlike the scan-side module constants
    # (_DEFAULT_PAGE_TIMEOUT_SECONDS, _MAX_ADF_PAGES): this is the only timeout
    # that waits on a human rather than a machine, and ten minutes is a guess
    # about someone else's household (D-10). Bounded at load (WR-01): zero or a
    # negative value would fail every manual-duplex job right after pass A, and
    # a value above threading.TIMEOUT_MAX makes Event.wait raise OverflowError
    # at the same point. One day is the ceiling -- far beyond any real flip.
    flip_timeout_seconds: int = Field(default=600, ge=1, le=86_400)
    min_free_space_mb: int = 500
    web_host: str = "0.0.0.0"
    web_port: int = 8080

    @field_validator("tmp_dir", "data_dir", "log_file", mode="after")
    @classmethod
    def _expand_paths(cls, value: str) -> str:
        """
        Expand a leading ``~`` in the path settings (CFG-03, M-20).

        ``~/scans`` used to be taken literally. Only ``~`` is expanded, never
        ``$VAR``. The defaults are already absolute, so this does not run on
        them (no ``validate_default``).

        Args:
            value: The validated path string.

        Returns:
            The value with ``~`` expanded.

        """
        return _expand_user(value)

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        """
        Normalise a configured level name before the ``Literal`` check (CFG-04).

        ``getattr(logging, name)`` used to accept any attribute name and crash
        on an unknown one such as ``TRACE`` long after load (M-21). The name is
        trimmed and upper-cased, and ``WARN`` is read as ``WARNING`` because
        ``logging.getLevelNamesMapping()`` itself lists ``WARN``. Anything that
        is not a string is returned unchanged so pydantic rejects it.

        Args:
            value: The raw ``log_level`` input.

        Returns:
            The normalised name for a string input, else the input unchanged.

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
        return Path(self.data_dir) / "saneless.db"

    @property
    def failed_dir(self) -> Path:
        """
        Directory holding scans preserved after a failed pipeline run.

        Creates nothing; callers are responsible for making it exist.

        Returns:
            The path to the failed/ directory inside data_dir.

        """
        return Path(self.data_dir) / "failed"


_ENV_PREFIX: Final = "SANELESS_"
"""The environment variable prefix; the unknown-variable scan uses it too."""

_ENV_DELIMITER: Final = "__"
"""The nested delimiter in environment variable names."""


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
        # explicitly so it cannot drift from the nested models (CFG-01).
        extra="forbid",
    )

    # default_factory, not a plain instance: an ``OutputConfig()`` default is
    # built once at import and would freeze the HOME/XDG state defaults
    # (CFG-03, RESEARCH Pitfall 3). scanner and paperless match for consistency.
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    paperless: PaperlessConfig = Field(default_factory=PaperlessConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    profiles: dict[str, ProfileConfig] = {"default": ProfileConfig()}

    # A PrivateAttr, not a field: a field would be settable from
    # SANELESS_CONFIG_PATH and from a top-level TOML key, letting either
    # redirect profile writes (D-16, research Pattern 8).
    _config_path: Path | None = PrivateAttr(default=None)

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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003 -- pydantic-settings requires this signature param
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003 -- pydantic-settings requires this signature param
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """
        Configure settings sources with optional TOML file support.

        The _toml_file init kwarg is extracted and used to create a
        TomlConfigSettingsSource if the file exists.
        """
        # init_settings is always an InitSettingsSource at runtime
        init_src = cast("InitSettingsSource", init_settings)
        toml_file = init_src.init_kwargs.pop("_toml_file", None)

        if toml_file is not None:
            toml_source = TomlConfigSettingsSource(
                settings_cls,
                toml_file=toml_file,
            )
            return (init_settings, env_settings, toml_source)

        return (init_settings, env_settings)

    @field_validator("profiles")
    @classmethod
    def validate_default_profile(
        cls,
        v: dict[str, ProfileConfig],
    ) -> dict[str, ProfileConfig]:
        """Ensure a default profile is always defined."""
        if "default" not in v:
            msg = "A 'default' profile must be defined in config"
            raise ValueError(msg)
        return v


def warn_on_legacy_duplex_sources(settings: Settings) -> None:
    """
    Warn, by profile name, about each profile with a legacy-looking source.

    A function the CLI calls right after ``configure_logging``, not a
    ``Settings`` validator (WR-05): a validator runs inside ``load_settings``,
    before ``cli()`` has configured logging, so its record went to Python's
    ``lastResort`` handler on stderr and never reached ``log_file`` -- and
    this message is the operator's only migration instruction, since the
    legacy form is documented nowhere. D-03's split is intact: the translation
    stays in ``ProfileConfig`` (every construction path), and the naming lives
    here because a profile cannot name itself. The replacement is stated
    inline (D-18); no removal is promised.

    A legacy-looking source with an explicit non-manual ``duplex`` is warned
    about too (IN-04): explicit configuration still wins, so it is not read as
    manual duplex, but its source goes to the scanner verbatim.

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


_SECTION_MODELS: Final[dict[str, type[BaseModel]]] = {
    "scanner": ScannerConfig,
    "paperless": PaperlessConfig,
    "output": OutputConfig,
}
"""The plain ``Settings`` sections, each a single table of keys (D-11)."""

_PROFILE_LABEL: Final = "profiles.<name>"
"""How a key that belongs in some profile table is named in an error."""


def _escape_name(name: str) -> str:
    """
    Escape the control characters in a user-controlled name, without quotes.

    TOML quoted keys and profile names can hold newlines or terminal escapes;
    ``repr`` escapes them, so an error line cannot forge further stderr or log
    lines (T-27-11, the ``web/errors.py`` precedent).

    Args:
        name: A key, section or profile name taken from the configuration.

    Returns:
        The name as ``repr`` renders it, minus the surrounding quotes.

    """
    return repr(name)[1:-1]


def _valid_keys(model: type[BaseModel]) -> list[str]:
    """
    List the keys a section accepts, as the operator writes them (D-11).

    Args:
        model: The section's model.

    Returns:
        Each field's alias where it has one (``title``), else its name.

    """
    return [field.alias or name for name, field in model.model_fields.items()]


def _match_candidates(model: type[BaseModel]) -> list[str]:
    """
    List every spelling a section accepts: field names plus aliases (D-11).

    Args:
        model: The section's model.

    Returns:
        The field names followed by the aliases.

    """
    names = list(model.model_fields)
    return names + [field.alias for field in model.model_fields.values() if field.alias]


def _section_owning(key: str, *, exclude: type[BaseModel] | None) -> str | None:
    """
    Name the section a misplaced key really belongs in (D-11).

    Args:
        key: The unknown key.
        exclude: The section model the key was found in, which cannot own it.

    Returns:
        A plain section name, ``profiles.<name>`` for a profile key, or None.

    """
    owners: list[tuple[str, type[BaseModel]]] = [
        *_SECTION_MODELS.items(),
        (_PROFILE_LABEL, ProfileConfig),
    ]
    for label, model in owners:
        if model is not exclude and key in _match_candidates(model):
            return label
    return None


def _format_loc_path(parts: Sequence[str | int]) -> str:
    """
    Render the key path below a section, e.g. ``default_tags[0]`` (D-10).

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
    Describe an unknown key inside a section or profile table (D-11, D-12).

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
    owner = _section_owning(key, exclude=model)
    if owner is not None:
        return f"{subject}; it belongs in [{owner}]"
    hint = difflib.get_close_matches(key, _match_candidates(model), n=1)
    did_you_mean = f" (did you mean {hint[0]!r}?)" if hint else ""
    valid = ", ".join(_valid_keys(model))
    return f"{subject}{did_you_mean}; valid keys: {valid}"


def _describe_unknown_top_level(name: str) -> str:
    """
    Describe an unknown top-level name (D-11, M-18).

    A miscased section (``[Paperless]``) is suggested as the real section,
    never as ``[profiles.Paperless]``; a key of some section says where it
    belongs; anything else is most likely a profile table written without its
    ``profiles.`` prefix.

    Args:
        name: The unknown top-level table or key name.

    Returns:
        The error line, without indentation.

    """
    sections = list(Settings.model_fields)
    for section in sections:
        if name.casefold() == section:
            return f"unknown section {name!r} (did you mean [{section}]?)"
    owner = _section_owning(name, exclude=None)
    if owner is not None:
        return f"unknown key {name!r} at the top level; it belongs in [{owner}]"
    hints = [f"[{match}]" for match in difflib.get_close_matches(name, sections, n=1)]
    hints.append(f"[profiles.{_escape_name(name)}]")
    return (
        f"unknown section {name!r} (did you mean {' or '.join(hints)}?); "
        f"valid sections: {', '.join(sections)}"
    )


def _env_contribution() -> dict[str, object]:
    """
    Return what the SANELESS_* environment contributes to the settings.

    This is pydantic-settings' own prefix, delimiter, case-folding and JSON
    logic, so error attribution (D-12) and the CFG-11 key names cannot drift
    from what was actually loaded (RESEARCH "Don't Hand-Roll").

    Returns:
        The case-folded nested mapping the environment source produces.

    Raises:
        SettingsError: If a complex field's variable holds invalid JSON.

    """
    return EnvSettingsSource(Settings)()


def _env_variable_for(
    loc: tuple[str | int, ...], env_data: Mapping[str, object]
) -> str | None:
    """
    Name the environment variable an error's value came from, if any (D-12).

    ``env_data`` is walked by the string elements of ``loc``. Its keys are
    already case-folded by pydantic-settings, so an error in a TOML
    ``[profiles.Receipt]`` table is not blamed on a variable that created
    profile ``receipt``. For the longest walked path, then each shorter
    prefix, a variable named ``SANELESS_`` plus the path joined by ``__`` is
    looked for, ignoring case -- a prefix covers a JSON-valued variable such as
    ``SANELESS_OUTPUT``. Environment beats file after the sources merge, so a
    value present in ``env_data`` is the one that failed.

    Args:
        loc: The error's location.
        env_data: The environment's contribution (``_env_contribution``).

    Returns:
        The variable name as spelled in the environment, or None.

    """
    parts: list[str] = []
    node: object = env_data
    for element in loc:
        if not isinstance(element, str) or not isinstance(node, dict):
            break
        if element not in node:
            break
        parts.append(element)
        node = cast("dict[str, object]", node)[element]
    by_folded = {name.casefold(): name for name in os.environ}
    for end in range(len(parts), 0, -1):
        candidate = (_ENV_PREFIX + _ENV_DELIMITER.join(parts[:end])).casefold()
        if candidate in by_folded:
            return by_folded[candidate]
    return None


def _render_error(
    loc: tuple[str | int, ...],
    error_type: str,
    message: str,
    env_data: Mapping[str, object],
) -> str:
    """
    Render one pydantic error as a ``[section] key`` line (D-10, D-11, D-12).

    Args:
        loc: The error's location.
        error_type: The error's pydantic type, e.g. ``extra_forbidden``.
        message: pydantic's short ``msg``, which never contains the input.
        env_data: The environment's contribution, for attribution.

    Returns:
        The error line, without indentation.

    """
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
    Render every validation error as one line, sorted for stable output (D-10).

    Only ``loc``, ``type`` and ``msg`` are read. ``input`` and ``ctx`` are
    never touched: for ``tokne = "..."`` the input is the Paperless token, and
    ``str(ValidationError)`` embeds it (D-14, CFG-05).

    Args:
        errors: ``ValidationError.errors()``.
        env_data: The environment's contribution, for attribution (D-12).

    Returns:
        The error lines, without indentation or header.

    """
    return sorted(
        _render_error(err["loc"], err["type"], err["msg"], env_data) for err in errors
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
    Reject SANELESS_* variables whose first segment names no section (D-13).

    pydantic-settings silently ignores them, so ``SANELESS_PAPERLES__TOKEN``
    would leave the token unset without a word, and ``SANELESS_CONFIG_PATH``
    would look as if it did something. This runs in the loader, never in a
    ``Settings`` validator, so direct ``Settings(...)`` construction is
    unaffected (Pitfall 9, S-10).

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
    Callable view of ``Settings`` that accepts the private ``_toml_file`` kwarg.

    ``_toml_file`` is not a declared field on ``Settings``; it is a private init
    kwarg popped out of ``init_kwargs`` by ``settings_customise_sources``. This
    protocol describes the constructor signature that mechanism really provides.
    """

    def __call__(self, *, _toml_file: Path) -> Settings:
        """Construct ``Settings`` from an explicit TOML file path."""
        ...


def _build_settings(
    toml_file: Path | None = None,
) -> Settings:
    """
    Build Settings, rendering every validation error into one ConfigError.

    Each error becomes one line under a header naming the file, or naming
    defaults and environment when no file was loaded (D-10). Errors whose
    value came from the environment name the variable (D-12), unknown
    SANELESS_* variables are added to the same list (D-13), and invalid JSON
    in a variable becomes a line too (Pitfall 2). A TOML syntax error is not a
    validation error and propagates unchanged (Phase 28).

    Args:
        toml_file: The TOML file to load, or None for defaults plus environment.

    Returns:
        The validated settings.

    Raises:
        ConfigError: If anything above failed; the message holds no input value.

    """
    lines = _unknown_env_lines(os.environ)
    settings: Settings | None = None
    try:
        env_data = _env_contribution()
    except SettingsError as exc:
        # The message names the field and source, never the value; the
        # exception (and its JSON-decoding cause) is not chained (T-27-13).
        lines.append(f"environment: {_escape_name(str(exc))}")
    else:
        try:
            if toml_file is not None:
                settings = cast("_SettingsFactory", Settings)(_toml_file=toml_file)
            else:
                settings = Settings()
        except ValidationError as exc:
            lines.extend(_render_error_lines(exc.errors(), env_data))
    if settings is not None and not lines:
        return settings
    lines.sort()
    header = (
        f"Configuration error in {toml_file}:"
        if toml_file is not None
        else "Configuration error (defaults and environment):"
    )
    msg = "\n".join([header, *(f"  {line}" for line in lines)])
    # from None, and raised outside the except block: a chained ValidationError
    # would print its inputs -- possibly the token -- in any traceback
    # (Pitfall 1, D-14).
    raise ConfigError(msg) from None


def _nearest_existing_ancestor(path: Path) -> Path:
    """
    Find the deepest existing path among ``path`` and its ancestors (M-20).

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
    Raise ConfigError unless ``directory`` could be written or created (M-20).

    A missing directory is judged by its nearest existing ancestor, since that
    is where creating it would fail -- not just by an immediate parent that
    may not exist either.

    Args:
        label: The setting name, e.g. ``data_dir``, for the message.
        directory: The configured directory.

    Raises:
        ConfigError: If the directory, or its nearest existing ancestor when
            the directory is missing, is not writable.

    """
    ancestor = _nearest_existing_ancestor(directory)
    if os.access(ancestor, os.W_OK):
        return
    if ancestor == directory:
        msg = f"{label} is not writable: {directory}"
    else:
        msg = f"{label} parent is not writable: {ancestor}"
    raise ConfigError(msg)


def validate_settings_dirs(settings: Settings) -> None:
    """
    Fail fast with ConfigError if tmp_dir, data_dir or consume_dir are unwritable.

    Validates directory writability at startup so permission errors surface
    immediately rather than mid-scan, or - for data_dir - at the moment a
    failed scan needs preserving. Per D-13, raises ConfigError (not
    ValueError) for writability failures. A missing directory is checked
    against its nearest existing ancestor, so ``<unwritable>/a/b/c`` fails
    here too (M-20).

    Args:
        settings: Application settings to validate.

    Raises:
        ConfigError: If any configured directory is not writable.

    """
    _require_writable("tmp_dir", Path(settings.output.tmp_dir))
    _require_writable("data_dir", Path(settings.output.data_dir))
    if settings.paperless.consume_dir:
        _require_writable("consume_dir", Path(settings.paperless.consume_dir))


def config_search_paths() -> tuple[Path, ...]:
    """
    List the config file locations searched when no explicit path is given.

    The single search list for both loading and the CLI's write target
    (D-16): ``./saneless.toml``, then ``$XDG_CONFIG_HOME/saneless/config.toml``
    (``~/.config`` when unset, CFG-03), then ``/etc/saneless/config.toml``. A
    function rather than a module constant so HOME and ``$XDG_CONFIG_HOME``
    are read when called, not at import.

    Returns:
        The candidate paths, in search order.

    """
    return (
        Path("./saneless.toml"),
        xdg_config_home() / "saneless" / "config.toml",
        Path("/etc/saneless/config.toml"),
    )


def load_settings(config_path: str | None = None) -> Settings:
    """
    Load settings from TOML file with env var overrides.

    Args:
        config_path: Explicit path to a TOML config file. If provided,
            loads from that path directly. Otherwise searches
            ``config_search_paths()``.

    Returns:
        Fully validated Settings instance carrying ``config_path``: the
        explicit path with ``~`` expanded, else the first search path that is
        a regular file, else None when no file was found.

    Raises:
        ConfigError: If an explicit path is missing or not a regular file
            (CFG-02), or if the configuration fails validation (D-10).

    """
    path: Path | None
    if config_path:
        explicit = Path(config_path).expanduser()
        # A directory counts as missing: Docker creates one where a
        # single-file bind mount's source does not exist (CFG-02, M-19).
        if not explicit.is_file():
            msg = f"Config file not found or not a regular file: {explicit}"
            raise ConfigError(msg)
        path = explicit
    else:
        path = next((p for p in config_search_paths() if p.is_file()), None)
    # With no path, only defaults + env vars are used.
    settings = _build_settings(toml_file=path)
    settings._config_path = path
    return settings


def _dotted_leaves(node: Mapping[str, object], prefix: str) -> Iterator[str]:
    """
    Yield the dotted names of the leaves of a nested mapping.

    A non-empty mapping is descended into; anything else is a leaf. Each
    segment is escaped, because profile names reach the log (T-27-11).

    Args:
        node: The mapping to flatten.
        prefix: The dotted name of ``node`` itself, empty at the root.

    Yields:
        Dotted names, e.g. ``profiles.receipt.title``.

    """
    for key, value in node.items():
        name = f"{prefix}.{_escape_name(key)}" if prefix else _escape_name(key)
        if isinstance(value, dict) and value:
            yield from _dotted_leaves(cast("dict[str, object]", value), name)
        else:
            yield name


def env_sourced_keys() -> list[str]:
    """
    List the settings that SANELESS_* environment variables supply (CFG-11).

    Names only, never values: the Paperless token is commonly one of them.
    A JSON-valued section such as ``SANELESS_OUTPUT`` is reported by its
    leaves.

    Returns:
        Sorted dotted names, e.g. ``["paperless.token", "paperless.url"]``.

    """
    return sorted(_dotted_leaves(_env_contribution(), ""))


def log_config_sources(settings: Settings) -> None:
    """
    Log, once at INFO, where the configuration came from (CFG-11, U-01).

    Names the loaded file, or says that only defaults and environment were
    used, and lists the dotted names of the environment-sourced keys -- an
    operator can then see that a stray variable overrides the file. Values are
    never logged. Like ``warn_on_legacy_duplex_sources``, this is a function
    the CLI calls after ``configure_logging``, not a validator: a record logged
    during load would never reach ``log_file`` (S-10, WR-05). It runs only
    after a successful load, so the environment is known to parse.

    Args:
        settings: The loaded settings.

    """
    source = (
        str(settings.config_path)
        if settings.config_path is not None
        else "no config file; defaults + environment"
    )
    keys = ", ".join(env_sourced_keys()) or "(none)"
    logger.info("Configuration: %s; from environment: %s", source, keys)
