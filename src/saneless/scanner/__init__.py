"""
Scanner abstraction layer for saneless.

Provides the ScannerBackend ABC and the SaneBackend implementation
wrapping python-sane, along with data types for device information,
capabilities, and scan settings.
"""

from .base import DeviceCapabilities, DeviceInfo, ScannerBackend, ScanSettings

__all__ = [
    "DeviceCapabilities",
    "DeviceInfo",
    "SaneBackend",
    "ScanSettings",
    "ScannerBackend",
]


def __getattr__(name: str) -> object:
    """Lazy import for SaneBackend to avoid requiring python-sane at import time."""
    if name == "SaneBackend":
        from .sane_backend import SaneBackend  # noqa: PLC0415

        return SaneBackend
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
