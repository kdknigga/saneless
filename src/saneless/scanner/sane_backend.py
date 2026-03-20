"""
SANE scanner backend implementation wrapping python-sane.

This module provides the concrete SaneBackend that communicates with
physical scanners through the SANE (Scanner Access Now Easy) library.
Key safety measures:
- sane.init() called exactly once at construction time (Pitfall #1)
- Device handles managed via context manager with cancel+close (Pitfall #4)
- No progress callbacks to snap() (Pitfall #2)
- Source option validated against device capabilities (Pitfall #5)
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any, Protocol

import PIL.Image
from PIL import Image

from saneless.exceptions import ScanError
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScannerBackend,
    ScanSettings,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

# python-sane is imported as a module-level name so that tests can
# monkeypatch ``sane_backend.sane`` without the real C extension
# being installed.  The actual import is deferred to avoid a hard
# dependency at collection time.
sane: Any = None


def _ensure_sane() -> None:
    """Import the real sane module on first use."""
    global sane  # noqa: PLW0603
    if sane is None:
        import sane as _sane  # type: ignore[no-redef]  # noqa: PLC0415

        sane = _sane


# Allow high-DPI scans without triggering Pillow's decompression bomb check.
# 600 DPI A4 color = ~34.8M pixels; 1200 DPI = ~139M pixels.
# Pillow default limit is 89.5M pixels.
PIL.Image.MAX_IMAGE_PIXELS = 200_000_000

__all__ = ["SaneBackend"]

logger = logging.getLogger(__name__)


class SaneDevice(Protocol):
    """Protocol describing the SANE device handle interface."""

    mode: str
    resolution: int
    source: str

    def get_options(self) -> list: ...
    def snap(self) -> Image.Image: ...
    def multi_scan(self) -> Iterator[Image.Image]: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...


class SaneBackend(ScannerBackend):
    """
    Scanner backend wrapping python-sane.

    Calls sane.init() exactly once at construction. Device handles
    are opened via a context manager that ensures cancel() and close()
    are called on all code paths.
    """

    def __init__(self) -> None:
        """Initialize SANE and store the library version."""
        _ensure_sane()
        self._sane_version = sane.init()
        logger.info("SANE initialized, version %s", self._sane_version)

    @contextlib.contextmanager
    def _open_device(self, device_id: str) -> Generator[SaneDevice]:
        """
        Context manager for SANE device lifecycle.

        Opens the device, yields it for use, then ensures cancel()
        and close() are called on all exit paths (normal and error).

        Args:
            device_id: SANE device identifier string.

        Yields:
            An open SANE device handle.

        """
        dev: SaneDevice = sane.open(device_id)
        try:
            yield dev
        finally:
            with contextlib.suppress(Exception):
                dev.cancel()
            dev.close()

    def get_devices(self) -> list[DeviceInfo]:
        """
        Enumerate available scanning devices.

        Returns:
            List of DeviceInfo objects for each discovered device.

        """
        raw_devices = sane.get_devices()
        return [
            DeviceInfo(
                name=d[0],
                vendor=d[1],
                model=d[2],
                device_type=d[3],
            )
            for d in raw_devices
        ]

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """
        Query device capabilities and available options.

        Opens the device, reads its option list, and extracts
        available sources, resolutions, and modes.

        Args:
            device_id: SANE device identifier string.

        Returns:
            DeviceCapabilities with parsed option information.

        """
        with self._open_device(device_id) as dev:
            raw_options = dev.get_options()
            sources: list[str] = []
            resolutions: list[int] = []
            modes: list[str] = []

            for opt in raw_options:
                # SANE option tuple:
                # (index, name, title, desc, type, unit, size, cap, constraint)
                if len(opt) < 9:
                    continue
                name = opt[1]
                constraint = opt[8]
                if name == "source" and isinstance(constraint, list):
                    sources = [str(s) for s in constraint]
                elif name == "resolution" and isinstance(constraint, list):
                    resolutions = [int(r) for r in constraint]
                elif name == "mode" and isinstance(constraint, list):
                    modes = [str(m) for m in constraint]

            return DeviceCapabilities(
                sources=sources,
                resolutions=resolutions,
                modes=modes,
                raw_options=raw_options,
            )

    def scan_pages(
        self, device_id: str, settings: ScanSettings
    ) -> Iterator[Image.Image]:
        """
        Acquire pages from scanner.

        Opens the device, validates the requested source against
        available options, sets scan parameters, and yields the
        scanned image. Does NOT pass a progress callback to snap()
        to prevent segfaults (Pitfall #2).

        Args:
            device_id: SANE device identifier string.
            settings: Scan settings (source, resolution, mode).

        Yields:
            PIL Image for each scanned page.

        Raises:
            ScanError: If the device does not support the requested source.

        """
        with self._open_device(device_id) as dev:
            # Validate source option against device capabilities
            raw_options = dev.get_options()
            available_sources: list[str] = []
            has_source_option = False

            for opt in raw_options:
                if len(opt) >= 9 and opt[1] == "source":
                    has_source_option = True
                    constraint = opt[8]
                    if isinstance(constraint, list):
                        available_sources = [str(s) for s in constraint]
                    break

            if has_source_option and settings.source not in available_sources:
                msg = (
                    f"Device does not support source '{settings.source}'. "
                    f"Available: {available_sources}"
                )
                raise ScanError(msg)

            # Set device options
            dev.mode = settings.mode
            dev.resolution = settings.resolution
            if has_source_option:
                dev.source = settings.source

            # Snap without progress callback (Pitfall #2 prevention)
            image = dev.snap()
            yield image
