"""
Custom exception hierarchy for saneless.

All saneless-specific exceptions inherit from SanelessError,
allowing callers to catch broad or narrow exception types.
"""

__all__ = [
    "ConfigError",
    "FeederEmptyError",
    "PaperlessError",
    "PaperlessTimeoutError",
    "SanelessError",
    "ScanError",
    "StorageError",
]


class SanelessError(Exception):
    """Base exception for all saneless errors."""


class ConfigError(SanelessError):
    """Configuration loading or validation failure."""


class ScanError(SanelessError):
    """Scanner operation failure."""


class FeederEmptyError(ScanError):
    """ADF feeder is empty -- no paper detected."""


class PaperlessError(SanelessError):
    """Paperless-ngx API operation failure."""


class PaperlessTimeoutError(PaperlessError):
    """Paperless-ngx did not resolve a consume task before the deadline."""


class StorageError(SanelessError):
    """Job store schema or persistence failure."""
