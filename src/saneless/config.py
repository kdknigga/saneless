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
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from saneless.exceptions import ConfigError
from saneless.vocabulary import TITLE_MAX_LENGTH

if TYPE_CHECKING:
    from collections.abc import Sequence
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
    "load_settings",
    "resolve_job_title",
    "validate_settings_dirs",
    "warn_on_legacy_duplex_sources",
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
    # under tmp_dir, which is disposable scratch space. The hardcoded
    # Path.home() form matches log_file below so Phase 27's XDG expansion
    # changes both defaults in a single edit; no env var is consulted here.
    data_dir: str = str(Path.home() / ".local" / "state" / "saneless")
    log_file: str = str(Path.home() / ".local" / "state" / "saneless" / "saneless.log")
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


class Settings(BaseSettings):
    """
    Application settings with TOML + env var loading.

    Environment variables use the SANELESS_ prefix with __ as the
    nested delimiter. For example, SANELESS_SCANNER__HOST sets
    settings.scanner.host.
    """

    model_config = SettingsConfigDict(
        env_prefix="SANELESS_",
        env_nested_delimiter="__",
        # pydantic-settings already forbids unknown top-level names; stated
        # explicitly so it cannot drift from the nested models (CFG-01).
        extra="forbid",
    )

    scanner: ScannerConfig = ScannerConfig()
    paperless: PaperlessConfig = PaperlessConfig()
    output: OutputConfig = OutputConfig()
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


def _describe_unknown_key(label: str, key: str, model: type[BaseModel]) -> str:
    """
    Describe an unknown key inside a section or profile table (D-11).

    Args:
        label: The section label, e.g. ``paperless`` or ``profiles.default``.
        key: The unknown key.
        model: The model of the table the key was found in.

    Returns:
        The error line, without indentation.

    """
    owner = _section_owning(key, exclude=model)
    if owner is not None:
        return f"[{label}] unknown key {key!r}; it belongs in [{owner}]"
    hint = difflib.get_close_matches(key, _match_candidates(model), n=1)
    did_you_mean = f" (did you mean {hint[0]!r}?)" if hint else ""
    valid = ", ".join(_valid_keys(model))
    return f"[{label}] unknown key {key!r}{did_you_mean}; valid keys: {valid}"


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


def _render_error(loc: tuple[str | int, ...], error_type: str, message: str) -> str:
    """
    Render one pydantic error as a ``[section] key`` line (D-10, D-11).

    Args:
        loc: The error's location.
        error_type: The error's pydantic type, e.g. ``extra_forbidden``.
        message: pydantic's short ``msg``, which never contains the input.

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
    if (
        error_type == "extra_forbidden"
        and model is not None
        and len(rest) == 1
        and isinstance(rest[0], str)
    ):
        return _describe_unknown_key(label, rest[0], model)
    key_path = _format_loc_path(rest)
    return f"[{label}] {key_path}: {message}" if key_path else f"[{label}]: {message}"


def _render_error_lines(errors: Sequence[ErrorDetails]) -> list[str]:
    """
    Render every validation error as one line, sorted for stable output (D-10).

    Only ``loc``, ``type`` and ``msg`` are read. ``input`` and ``ctx`` are
    never touched: for ``tokne = "..."`` the input is the Paperless token, and
    ``str(ValidationError)`` embeds it (D-14, CFG-05).

    Args:
        errors: ``ValidationError.errors()``.

    Returns:
        The error lines, without indentation or header.

    """
    return sorted(_render_error(err["loc"], err["type"], err["msg"]) for err in errors)


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
    defaults and environment when no file was loaded (D-10). A TOML syntax
    error is not a validation error and propagates unchanged (Phase 28).

    Args:
        toml_file: The TOML file to load, or None for defaults plus environment.

    Returns:
        The validated settings.

    Raises:
        ConfigError: If validation failed; the message holds no input value.

    """
    lines: list[str] = []
    settings: Settings | None = None
    try:
        if toml_file is not None:
            settings = cast("_SettingsFactory", Settings)(_toml_file=toml_file)
        else:
            settings = Settings()
    except ValidationError as exc:
        lines.extend(_render_error_lines(exc.errors()))
    if settings is not None and not lines:
        return settings
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


def validate_settings_dirs(settings: Settings) -> None:
    """
    Fail fast with ConfigError if tmp_dir, data_dir or consume_dir are unwritable.

    Validates directory writability at startup so permission errors surface
    immediately rather than mid-scan, or - for data_dir - at the moment a
    failed scan needs preserving. Per D-13, raises ConfigError (not
    ValueError) for writability failures.

    Args:
        settings: Application settings to validate.

    Raises:
        ConfigError: If any configured directory is not writable.

    """
    tmp = Path(settings.output.tmp_dir)
    if tmp.exists() and not os.access(tmp, os.W_OK):
        msg = f"tmp_dir is not writable: {tmp}"
        raise ConfigError(msg)
    if not tmp.exists():
        parent = tmp.parent
        if parent.exists() and not os.access(parent, os.W_OK):
            msg = f"tmp_dir parent is not writable: {parent}"
            raise ConfigError(msg)
    data = Path(settings.output.data_dir)
    if data.exists() and not os.access(data, os.W_OK):
        msg = f"data_dir is not writable: {data}"
        raise ConfigError(msg)
    if not data.exists():
        parent = data.parent
        if parent.exists() and not os.access(parent, os.W_OK):
            msg = f"data_dir parent is not writable: {parent}"
            raise ConfigError(msg)
    consume = settings.paperless.consume_dir
    if consume:
        consume_path = Path(consume)
        if consume_path.exists() and not os.access(consume_path, os.W_OK):
            msg = f"consume_dir is not writable: {consume_path}"
            raise ConfigError(msg)
        if not consume_path.exists():
            parent = consume_path.parent
            if parent.exists() and not os.access(parent, os.W_OK):
                msg = f"consume_dir parent is not writable: {parent}"
                raise ConfigError(msg)


def config_search_paths() -> tuple[Path, ...]:
    """
    List the config file locations searched when no explicit path is given.

    The single search list for both loading and the CLI's write target
    (D-16). A function rather than a module constant so ``Path.home()`` is
    read when called, not at import.

    Returns:
        The candidate paths, in search order.

    """
    return (
        Path("./saneless.toml"),
        Path.home() / ".config" / "saneless" / "config.toml",
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
