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

from pydantic import BaseModel, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

if TYPE_CHECKING:
    from pydantic_settings.main import InitSettingsSource

__all__ = [
    "OutputConfig",
    "PaperlessConfig",
    "ProfileConfig",
    "ScannerConfig",
    "Settings",
    "load_settings",
]


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

    source: str = "Flatbed"
    resolution: int = 300
    mode: str = "color"
    default_tags: list[int] = []
    default_correspondent: int | None = None
    default_title_template: str = ""


class OutputConfig(BaseModel):
    """Output and logging configuration."""

    tmp_dir: str = str(Path(tempfile.gettempdir()) / "saneless")
    log_file: str = "/var/log/saneless/saneless.log"
    log_level: str = "INFO"
    log_max_bytes: int = 10_485_760
    log_backup_count: int = 5
    history_retention_days: int = 7
    history_max_rows: int = 500
    paperless_task_timeout: int = 300


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
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
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
        Exception: If the TOML file is malformed or validation fails.

    """
    if config_path:
        return Settings(_toml_file=Path(config_path))  # type: ignore[call-arg]

    search_paths = [
        Path("./saneless.toml"),
        Path.home() / ".config" / "saneless" / "config.toml",
        Path("/etc/saneless/config.toml"),
    ]

    for path in search_paths:
        if path.exists():
            return Settings(_toml_file=path)  # type: ignore[call-arg]

    # No config file found -- use defaults + env vars only
    return Settings()
