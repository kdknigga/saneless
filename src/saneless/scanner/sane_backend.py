"""
SANE scanner backend implementation wrapping python-sane.

This module provides the concrete SaneBackend that communicates with
physical scanners through the SANE (Scanner Access Now Easy) library.
Key safety measures:
- sane.init() called exactly once at construction time (Pitfall #1)
- Device handles managed via context manager with cancel+close (Pitfall #4)
- No progress callbacks to snap() (Pitfall #2)
- Source option validated against device capabilities (Pitfall #5)
- ADF multi-page scan with per-page timeout and inline validation
"""

from __future__ import annotations

import contextlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING, Any, Protocol

import PIL.Image
from PIL import Image, ImageStat

from saneless.exceptions import FeederEmptyError, ScanError
from saneless.paper_sizes import PAPER_SIZES_MM, crop_to_paper_size
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
        import sane as _sane  # noqa: PLC0415

        sane = _sane


# Allow high-DPI scans without triggering Pillow's decompression bomb check.
# 600 DPI A4 color = ~34.8M pixels; 1200 DPI = ~139M pixels.
# Pillow default limit is 89.5M pixels.
PIL.Image.MAX_IMAGE_PIXELS = 200_000_000

# Per-page timeout: 2x a generous single-page scan estimate (60s at 600 DPI).
# At 300 DPI typical scan is ~10-15s, so 120s is very conservative.
_DEFAULT_PAGE_TIMEOUT_SECONDS: float = 120.0

# Minimum raw image data size in bytes. A valid scanned page at any reasonable
# resolution will be well above this. Catches corrupt/truncated pages.
_MIN_PAGE_BYTES: int = 10_000  # 10 KB

# Thresholds for pure white/black detection at the scanner level.
# These are intentionally extreme (tighter than the configurable empty-page
# thresholds in pages.py) to only catch obviously invalid images.
_SCANNER_WHITE_MEAN_THRESHOLD: float = 254.0
_SCANNER_WHITE_STDDEV_THRESHOLD: float = 1.0
_SCANNER_BLACK_MEAN_THRESHOLD: float = 1.0
_SCANNER_BLACK_STDDEV_THRESHOLD: float = 1.0

__all__ = ["SaneBackend"]

logger = logging.getLogger(__name__)


def _as_image(obj: object) -> Image.Image:
    """
    Cast an object to Image.Image for type checker satisfaction.

    ThreadPoolExecutor.submit(next, iterator) loses generic type info,
    so ty cannot infer the result is Image.Image. This cast is safe
    because the iterator is known to yield Image.Image.
    """
    if not isinstance(obj, Image.Image):
        msg = f"Expected Image, got {type(obj)}"
        raise TypeError(msg)
    return obj


def _set_geometry(dev: SaneDevice, paper_size: str) -> bool:
    """
    Set SANE geometry options for paper size constraint.

    SANE devices expose geometry options (tl_x, tl_y, br_x, br_y) as
    dynamic attributes.  Not all scanners support them, so assignment
    failures are caught and cause a fallback to Pillow cropping.

    Args:
        dev: Open SANE device handle.
        paper_size: Paper size key (e.g. ``"a4"``).

    Returns:
        True if geometry was set successfully, False otherwise.

    """
    if paper_size == "full":
        return False
    dims = PAPER_SIZES_MM.get(paper_size)
    if dims is None:
        return False
    width_mm, height_mm = dims
    try:
        dev.tl_x = 0.0
        dev.tl_y = 0.0
        dev.br_x = width_mm
        dev.br_y = height_mm
    except Exception:
        logger.warning(
            "Scanner does not support geometry options, will crop after scanning",
        )
        return False
    logger.info(
        "Scan area set to %s: %.1f x %.1f mm",
        paper_size,
        width_mm,
        height_mm,
    )
    return True


def _maybe_crop(
    image: Image.Image,
    settings: ScanSettings,
    *,
    geometry_set: bool,
) -> Image.Image:
    """
    Apply Pillow crop fallback when geometry options were not set.

    Args:
        image: Scanned page image.
        settings: Scan settings with paper_size and resolution.
        geometry_set: Whether SANE geometry was already applied.

    Returns:
        Cropped image, or original if no crop needed.

    """
    if settings.paper_size != "full" and not geometry_set:
        return crop_to_paper_size(image, settings.paper_size, settings.resolution)
    return image


def _is_adf_source(source: str) -> bool:
    """Check if the source string indicates an ADF source."""
    return "adf" in source.lower()


def _validate_page_image(page_image: Image.Image, page_num: int) -> bool:
    """
    Validate a scanned page image inline at the scanner level.

    Checks nonzero dimensions, minimum file size, and not pure white/black.
    Returns True if the page is valid, False if it should be skipped.

    Per user decision: "Validate each scanned image inline before yielding:
    check nonzero dimensions, minimum file size, not pure white/black."
    """
    # Check 1: Nonzero dimensions
    if page_image.size[0] == 0 or page_image.size[1] == 0:
        logger.warning(
            "Page %d: zero dimensions (%s), skipping", page_num, page_image.size
        )
        return False

    # Check 2: Minimum file size (raw pixel data)
    raw_size = len(page_image.tobytes())
    if raw_size < _MIN_PAGE_BYTES:
        logger.warning("Page %d: too small (%d bytes), skipping", page_num, raw_size)
        return False

    # Check 3: Not pure white or pure black (scanner-level, very strict thresholds)
    gray = page_image.convert("L")
    stats = ImageStat.Stat(gray)
    mean_val = stats.mean[0]
    stddev_val = stats.stddev[0]

    if (
        mean_val > _SCANNER_WHITE_MEAN_THRESHOLD
        and stddev_val < _SCANNER_WHITE_STDDEV_THRESHOLD
    ):
        logger.warning(
            "Page %d: pure white (mean=%.1f, stddev=%.1f), skipping",
            page_num,
            mean_val,
            stddev_val,
        )
        return False

    if (
        mean_val < _SCANNER_BLACK_MEAN_THRESHOLD
        and stddev_val < _SCANNER_BLACK_STDDEV_THRESHOLD
    ):
        logger.warning(
            "Page %d: pure black (mean=%.1f, stddev=%.1f), skipping",
            page_num,
            mean_val,
            stddev_val,
        )
        return False

    return True


class SaneDevice(Protocol):
    """Protocol describing the SANE device handle interface."""

    mode: str
    resolution: int
    source: str
    tl_x: float
    tl_y: float
    br_x: float
    br_y: float

    def get_options(self) -> list: ...
    def start(self) -> None: ...
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

    def __init__(self, host: str = "") -> None:
        """Initialize SANE, optionally configuring network host discovery."""
        _ensure_sane()
        # SANE_NET_HOSTS tells the sane-net backend which hosts to probe for
        # scanners.  Multiple hosts are separated by colons — see sane-net(5).
        # Only set from config when not already present in the environment
        # (explicit env var takes priority over config file).
        if host and "SANE_NET_HOSTS" not in os.environ:
            os.environ["SANE_NET_HOSTS"] = host
            logger.info("SANE net host discovery configured: %s", host)
        elif host and "SANE_NET_HOSTS" in os.environ:
            logger.info(
                "SANE_NET_HOSTS already set externally (%s), ignoring scanner.host config",
                os.environ["SANE_NET_HOSTS"],
            )
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

    def _scan_adf_pages(
        self,
        dev: SaneDevice,
        timeout_per_page: float = _DEFAULT_PAGE_TIMEOUT_SECONDS,
    ) -> Iterator[Image.Image]:
        """
        Yield validated pages from ADF via multi_scan() with per-page timeout.

        Per user decision: "Wrap ADF iteration with per-page timeout (not per-job)
        -- cancel if single page takes longer than 2-3x expected duration."

        Uses concurrent.futures.ThreadPoolExecutor to wrap each next(iterator)
        call with a timeout, since signal.alarm is not safe in non-main threads
        (per RESEARCH.md Open Question 1).

        Args:
            dev: Open SANE device handle.
            timeout_per_page: Maximum seconds to wait for each page.

        Yields:
            Validated PIL Image for each scanned page.

        Raises:
            FeederEmptyError: If the ADF feeder is empty.
            ScanError: If a page times out.

        """
        feeder_empty_msg = "No paper detected in feeder"
        try:
            iterator = dev.multi_scan()
        except Exception as exc:
            raise FeederEmptyError(feeder_empty_msg) from exc

        page_num = 0
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            while True:
                try:
                    # Per-page timeout: wrap next() in a future
                    future = executor.submit(next, iterator)
                    try:
                        page_image = _as_image(future.result(timeout=timeout_per_page))
                    except FuturesTimeoutError as timeout_exc:
                        logger.error(
                            "Page %d timed out after %.0fs",
                            page_num + 1,
                            timeout_per_page,
                        )
                        timeout_msg = (
                            f"Page {page_num + 1} timed out after "
                            f"{timeout_per_page:.0f}s"
                        )
                        raise ScanError(timeout_msg) from timeout_exc
                except StopIteration:
                    break
                except ScanError:
                    raise
                except FeederEmptyError:
                    raise
                except Exception as exc:
                    error_str = str(exc).lower()
                    if page_num == 0:
                        raise FeederEmptyError(feeder_empty_msg) from exc
                    # After first page, end-of-feed signals
                    if "out of documents" in error_str or "no docs" in error_str:
                        break
                    raise

                page_num += 1

                # Validate: nonzero dimensions, min file size, not pure white/black
                if not _validate_page_image(page_image, page_num):
                    continue

                # Strip EXIF (Pitfall #5: invalid EXIF breaks img2pdf)
                page_image.info.pop("exif", None)
                yield page_image
        finally:
            # Shut down the timeout executor
            executor.shutdown(wait=False)
            # Delete iterator before cancel to avoid __del__ issues (Pitfall #1)
            del iterator

        if page_num == 0:
            raise FeederEmptyError(feeder_empty_msg)

    def scan_pages(
        self, device_id: str, settings: ScanSettings
    ) -> Iterator[Image.Image]:
        """
        Acquire pages from scanner.

        Opens the device, validates the requested source against
        available options, sets scan parameters, and yields scanned
        images. Uses multi_scan() for ADF sources, snap() for flatbed.
        Does NOT pass a progress callback to snap() to prevent
        segfaults (Pitfall #2).

        Args:
            device_id: SANE device identifier string.
            settings: Scan settings (source, resolution, mode).

        Yields:
            PIL Image for each scanned page.

        Raises:
            ScanError: If the device does not support the requested source.
            FeederEmptyError: If the ADF feeder is empty.

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

            effective_source = settings.source
            if has_source_option and effective_source not in available_sources:
                if "Auto" in available_sources:
                    logger.info(
                        "Source '%s' not available, falling back to 'Auto'",
                        effective_source,
                    )
                    effective_source = "Auto"
                else:
                    msg = (
                        f"Device does not support source '{effective_source}'. "
                        f"Available: {available_sources}"
                    )
                    raise ScanError(msg)

            # Set device options
            dev.mode = settings.mode
            dev.resolution = settings.resolution
            if has_source_option:
                dev.source = effective_source

            # Set scan area geometry for paper size constraint (D-01)
            geometry_set = _set_geometry(dev, settings.paper_size)

            use_adf = _is_adf_source(effective_source)

            # D-04: Override for "Auto" source using config-driven routing
            if effective_source == "Auto":
                use_adf = settings.auto_source_mode == "adf"
                logger.info(
                    "Auto source routing: auto_source_mode='%s', use_adf=%s",
                    settings.auto_source_mode,
                    use_adf,
                )

            if use_adf:
                # ADF/duplex: use multi_scan() for multi-page acquisition
                for page in self._scan_adf_pages(dev):
                    yield _maybe_crop(page, settings, geometry_set=geometry_set)
            else:
                # Flatbed: start() initiates the SANE data channel, then
                # snap() drains it via sane_read() loop.  Without start()
                # the read loop has no data source.
                dev.start()
                image = dev.snap()
                # Strip EXIF from flatbed scans too
                image.info.pop("exif", None)
                yield _maybe_crop(image, settings, geometry_set=geometry_set)
