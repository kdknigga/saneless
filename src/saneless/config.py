"""
Configuration loading with pydantic-settings, TOML files, and env var overrides.

Settings are loaded from TOML config files with environment variable overrides
using the SANELESS_ prefix and __ nested delimiter. A default scan profile must
always be present in the configuration.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
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

if TYPE_CHECKING:
    from pydantic_settings.main import InitSettingsSource

__all__ = [
    "DEFAULT_RESOLUTION",
    "OutputConfig",
    "PaperlessConfig",
    "ProfileConfig",
    "ScannerConfig",
    "Settings",
    "config_search_paths",
    "load_settings",
    "validate_settings_dirs",
    "warn_on_legacy_duplex_sources",
]

logger = logging.getLogger(__name__)

DEFAULT_RESOLUTION = 300
"""Default scan resolution in DPI.

300 DPI is the minimum recommended by Tesseract OCR and the industry
standard for professional document scanning. See Phase 11 research.
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

    host: str = ""
    device: str = ""


class PaperlessConfig(BaseModel):
    """Paperless-ngx API connection settings."""

    url: str = ""
    token: str = ""
    consume_dir: str = ""


class ProfileConfig(BaseModel):
    """Scan profile configuration."""

    model_config = ConfigDict(populate_by_name=True)

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
    default_title_template: str = Field(default="", alias="title")
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


class OutputConfig(BaseModel):
    """Output and logging configuration."""

    tmp_dir: str = str(Path(tempfile.gettempdir()) / "saneless")
    # Durable state: the job database and preserved scans. Deliberately NOT
    # under tmp_dir, which is disposable scratch space. The hardcoded
    # Path.home() form matches log_file below so Phase 27's XDG expansion
    # changes both defaults in a single edit; no env var is consulted here.
    data_dir: str = str(Path.home() / ".local" / "state" / "saneless")
    log_file: str = str(Path.home() / ".local" / "state" / "saneless" / "saneless.log")
    log_level: str = "INFO"
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


_VALID_SECTIONS = ("scanner", "paperless", "output", "profiles")


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
    """Build Settings, converting extra-field errors to user-friendly messages."""
    try:
        if toml_file is not None:
            return cast("_SettingsFactory", Settings)(_toml_file=toml_file)
        return Settings()
    except ValidationError as exc:
        extra_fields: list[str] = []
        for err in exc.errors():
            if err["type"] == "extra_forbidden":
                loc = err.get("loc", ())
                if loc:
                    extra_fields.append(str(loc[0]))

        if extra_fields:
            names = ", ".join(repr(f) for f in extra_fields)
            valid = ", ".join(_VALID_SECTIONS)
            hints = [f"Did you mean [profiles.{f}]?" for f in extra_fields]
            hint_text = " ".join(hints)
            msg = (
                f"Unknown config section {names}. "
                f"Valid top-level sections: {valid}. {hint_text}"
            )
            raise ConfigError(msg) from exc

        raise


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
        explicit path as given, else the first search path that exists, else
        None when no file was found.

    Raises:
        ConfigError: If the TOML file has unrecognized top-level sections.

    """
    path = (
        Path(config_path)
        if config_path
        else next((p for p in config_search_paths() if p.exists()), None)
    )
    # With no path, only defaults + env vars are used.
    settings = _build_settings(toml_file=path)
    # An explicit path is recorded even if missing; CFG-02 (Phase 27) owns
    # making that an error.
    settings._config_path = path
    return settings
