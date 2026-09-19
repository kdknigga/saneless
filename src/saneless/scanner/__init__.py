"""
Scanner abstraction layer for saneless.

Provides the ScannerBackend ABC along with data types for device information,
capabilities, and scan settings.  The python-sane implementation lives in
``saneless.scanner.sane_backend`` and is imported from there by name, so
importing this package never reaches python-sane.
"""

from .base import (
    DeviceCapabilities,
    DeviceInfo,
    ScanBatch,
    ScannerBackend,
    ScanSettings,
)

__all__ = [
    "DeviceCapabilities",
    "DeviceInfo",
    "ScanBatch",
    "ScanSettings",
    "ScannerBackend",
]
