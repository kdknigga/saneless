"""
Custom exception hierarchy for saneless.

All saneless-specific exceptions inherit from SanelessError,
allowing callers to catch broad or narrow exception types.
"""

__all__ = ["ConfigError", "PaperlessError", "SanelessError", "ScanError"]


class SanelessError(Exception):
    """Base exception for all saneless errors."""


class ConfigError(SanelessError):
    """Configuration loading or validation failure."""


class ScanError(SanelessError):
    """Scanner operation failure."""


class PaperlessError(SanelessError):
    """Paperless-ngx API operation failure."""
