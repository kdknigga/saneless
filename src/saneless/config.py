"""
Configuration loading with pydantic-settings, TOML files, and env var overrides.

Settings are loaded from TOML config files with environment variable overrides
using the SANELESS_ prefix and __ nested delimiter. A default scan profile must
always be present in the configuration.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
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
    "load_settings",
]

DEFAULT_RESOLUTION = 300
"""Default scan resolution in DPI.

300 DPI is the minimum recommended by Tesseract OCR and the industry
standard for professional document scanning. See Phase 11 research.
"""


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
    default_tags: list[int] = []
    default_correspondent: int | None = None
    default_title_template: str = Field(default="", alias="title")
    empty_page_mean_threshold: float = 250.0
    empty_page_stddev_threshold: float = 5.0
    auto_generated: bool = False


class OutputConfig(BaseModel):
    """Output and logging configuration."""

    tmp_dir: str = str(Path(tempfile.gettempdir()) / "saneless")
    log_file: str = str(Path.home() / ".local" / "state" / "saneless" / "saneless.log")
    log_level: str = "INFO"
    log_max_bytes: int = 10_485_760
    log_backup_count: int = 5
    history_retention_days: int = 7
    history_max_rows: int = 500
    paperless_task_timeout: int = 300
    paperless_cache_ttl_seconds: int = 60
    web_host: str = "0.0.0.0"
    web_port: int = 8080


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


_VALID_SECTIONS = ("scanner", "paperless", "output", "profiles")


def _build_settings(
    toml_file: Path | None = None,
) -> Settings:
    """Build Settings, converting extra-field errors to user-friendly messages."""
    try:
        if toml_file is not None:
            return Settings(_toml_file=toml_file)  # type: ignore[call-arg] -- ty cannot see BaseSettings dynamic __init__ kwargs
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


def load_settings(config_path: str | None = None) -> Settings:
    """
    Load settings from TOML file with env var overrides.

    Args:
        config_path: Explicit path to a TOML config file. If provided,
            loads from that path directly. Otherwise searches standard
            locations.

    Returns:
        Fully validated Settings instance.

    Raises:
        ConfigError: If the TOML file has unrecognized top-level sections.

    """
    if config_path:
        return _build_settings(toml_file=Path(config_path))

    search_paths = [
        Path("./saneless.toml"),
        Path.home() / ".config" / "saneless" / "config.toml",
        Path("/etc/saneless/config.toml"),
    ]

    for path in search_paths:
        if path.exists():
            return _build_settings(toml_file=path)

    # No config file found -- use defaults + env vars only
    return _build_settings()
