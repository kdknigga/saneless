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
from PIL import Image

from saneless.exceptions import FeederEmptyError, ScanError
from saneless.paper_sizes import PAPER_SIZES_MM, crop_to_paper_size
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScannerBackend,
    ScanSettings,
    classify_source,
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

# Minimum raw image data size in bytes. Catches a corrupt or truncated buffer.
#
# This is now one of only two surviving checks, so its value is worth
# justifying rather than asserting. Measured against the SANE `test` backend,
# the smallest legitimate real page is 69,620 bytes -- Gray at 75 dpi on an
# 80x100 mm bed, the least data a real device in this project produces. That is
# nearly 7x this threshold, and a 300 dpi colour page is 3.3 MB. The check
# therefore demonstrably does not fire on legitimate small pages, which is the
# only way a size floor can be safe (24-RESEARCH.md Finding 2, Q2).
_MIN_PAGE_BYTES: int = 10_000  # 10 KB

# Upper bound on the number of pages one scan_pages() call will acquire.
#
# WHAT IT BOUNDS: python-sane's _SaneIterator.__next__ stops only on the exact
# string "Document feeder out of documents". On hardware that is not a feeder,
# start()/snap() keep succeeding, so the iterator re-scans the platen and the
# loop never terminates on its own -- reproduced live against a Flatbed source.
# The per-page timeout is no defence: a scan that succeeds satisfies it every
# iteration. This cap is what stops that loop (Phase 21 security finding W-01).
#
# WHAT IT DOES NOT BOUND: memory. At A4 300 dpi colour a page is roughly 26 MB,
# so 500 pages is roughly 13 GB, and pipeline.py materialises pages with list().
# This cap must not be described as a memory bound, because it is not one.
# Bounding memory is Phase 29's HARD-01/HARD-02.
#
# SCOPE: the cap is per scan_pages() call. Phase 25's two manual-duplex passes
# each call scan_pages() separately, so this is a per-pass cap, not a per-job
# one.
#
# The value matches the largest production ADF hoppers, so no real stack should
# reach it. That is assumption A1 in 24-RESEARCH.md, recorded at LOW confidence
# and cheap to revise precisely because the error names the cap.
_MAX_ADF_PAGES: int = 500

# The one message reported when a feeder produced no pages at all.
_FEEDER_EMPTY_MESSAGE = "No paper detected in feeder"

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


def _validate_page_image(page_image: Image.Image, page_num: int) -> bool:
    """
    Check that a scanned page is a readable image at all.

    Two integrity checks, and only two: nonzero dimensions, and a raw byte
    count at or above ``_MIN_PAGE_BYTES``. Both answer "did the device hand
    back something decodable?". Neither looks at what is printed on the page.

    Blank-page policy deliberately does not live here. The profile exposes
    ``enable_empty_page_detection`` along with user-visible mean and stddev
    thresholds, so a page discarded at this level would be discarded behind the
    user's back and would make that toggle untrue -- which is precisely what
    M-14 found, and what broke manual-duplex parity. Content is judged in
    exactly one place, ``pipeline._drop_empty_pages`` (D-05).

    Args:
        page_image: The acquired page.
        page_num: One-based page number, used in the warning text.

    Returns:
        True if the page is readable, False if it should be skipped.

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

    return True


def _next_page_with_timeout(
    executor: ThreadPoolExecutor,
    iterator: Iterator[Image.Image],
    page_num: int,
    timeout_per_page: float,
) -> Image.Image:
    """
    Acquire one page from the ADF iterator under a wall-clock timeout.

    ``signal.alarm`` is not safe in a non-main thread, so the blocking
    ``next(iterator)`` call is submitted to a single-worker executor and
    waited on with a timeout instead.

    Args:
        executor: Single-worker executor owned by the caller.
        iterator: The ``multi_scan()`` iterator being drained.
        page_num: Zero-based index of the page being acquired.
        timeout_per_page: Maximum seconds to wait for this page.

    Returns:
        The page image.

    Raises:
        ScanError: If the page did not arrive within the timeout.

    """
    future = executor.submit(next, iterator)
    try:
        return _as_image(future.result(timeout=timeout_per_page))
    except FuturesTimeoutError as timeout_exc:
        logger.error(
            "Page %d timed out after %.0fs",
            page_num + 1,
            timeout_per_page,
        )
        timeout_msg = f"Page {page_num + 1} timed out after {timeout_per_page:.0f}s"
        raise ScanError(timeout_msg) from timeout_exc


def _acquire_pages(
    dev: SaneDevice,
    timeout_per_page: float,
) -> Iterator[Image.Image]:
    """
    Yield validated pages from the ADF, one per feeder sheet.

    ``multi_scan()`` returns an iterator object and cannot raise, so the call
    is not guarded.  python-sane's ``_SaneIterator.__next__`` converts exactly
    one message into ``StopIteration``, and that is the only feeder-empty
    signal there is.  Every other exception is a real fault -- a jam, an open
    cover, a busy device, an I/O error -- and is reported as itself.

    The zero-page ``FeederEmptyError`` at the end is therefore the **only**
    path to "No paper detected in feeder", and it is the correct one: a feeder
    that produced no pages at all genuinely has no paper in it.  Do not
    re-introduce a first-page special case; inferring "the feeder is empty"
    from "it failed on iteration zero" is what made a jam, an open cover and a
    busy device all tell the operator to load paper (M-11, D-03).

    A page that fails its integrity checks is skipped and counted, not fatal:
    one corrupt sheet must not fail a fifty-sheet job (D-06).  A batch in which
    *every* fed page was rejected does raise, because returning an empty list
    would reach ``assemble_pdf([])`` and record a job that produced nothing as
    a success.

    **D-08, accepted and recorded deliberately.** One skipped front makes
    ``len(front_pages) != len(back_pages)``, which ``pipeline.py`` routes to
    ``_handle_duplex_mismatch`` -- two partial PDFs plus a warning instead of
    one interleaved document.  That is the honest response: a page the device
    could not read genuinely means the two manual-duplex passes no longer
    correspond.  SCNR-03's "manual duplex page parity survives" forbids parity
    broken by *policy* -- the backend silently discarding a clean blank back
    page -- and not parity broken by a page that could not be read at all.
    Parity broken that way is reported, never hidden.

    Args:
        dev: Open SANE device handle.
        timeout_per_page: Maximum seconds to wait for each page.

    Yields:
        Validated PIL Image for each scanned page.

    Raises:
        FeederEmptyError: If the feeder produced no pages at all.
        ScanError: If a page times out, the device reports a fault, the page
            count runs past ``_MAX_ADF_PAGES``, or every fed page failed its
            integrity checks.

    """
    iterator = dev.multi_scan()

    page_num = 0
    # Kept local on purpose. Surfacing this count to the user is D-07, and its
    # channel is D-12's result object, which plan 24-07 builds. It is
    # deliberately kept out of the pipeline's blank-page removal count: Phase 23
    # defined that field as empty-page detection and Phase 30 renders it to
    # users as "pages removed as blank", so reporting a corrupt page through it
    # would be a new small lie in a phase about removing them.
    rejected_pages = 0
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        while True:
            try:
                page_image = _next_page_with_timeout(
                    executor, iterator, page_num, timeout_per_page
                )
            except StopIteration:
                break
            except ScanError:
                # FeederEmptyError subclasses ScanError, so saneless's own
                # errors -- including the timeout path's -- propagate here.
                raise
            except Exception as exc:
                scan_error_msg = f"Scanner error on page {page_num + 1}: {exc}"
                raise ScanError(scan_error_msg) from exc

            page_num += 1

            # The overrun is detected on the page *past* the cap, not on the
            # cap itself: a legitimate maximal stack only learns it is finished
            # when the next probe raises, so stopping at equality would reject
            # a full hopper.
            if page_num > _MAX_ADF_PAGES:
                cap_msg = (
                    f"ADF page cap exceeded: stopped after {page_num} pages "
                    f"(limit {_MAX_ADF_PAGES})"
                )
                raise ScanError(cap_msg)

            # Integrity only: nonzero dimensions and minimum raw size. Whether
            # the page is worth keeping is the pipeline's decision, not ours.
            if not _validate_page_image(page_image, page_num):
                # Skip and count, never abort. The per-page WARNING naming the
                # page number and the reason comes from _validate_page_image.
                rejected_pages += 1
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
        raise FeederEmptyError(_FEEDER_EMPTY_MESSAGE)

    # Paper was fed but none of it was readable. This is a distinct condition
    # from an empty feeder and is reported distinctly: the operator needs to
    # hear "unreadable", not "load paper" (M-14, D-06).
    if rejected_pages == page_num:
        all_rejected_msg = (
            f"All {page_num} page(s) fed were unreadable and were skipped "
            f"(zero dimensions, or below {_MIN_PAGE_BYTES} bytes of image "
            f"data); no usable page was produced"
        )
        raise ScanError(all_rejected_msg)


def _resolve_source(raw_options: list[tuple], requested: str) -> tuple[str, bool]:
    """
    Decide which source name to use, and whether the device has the option.

    The two return values answer genuinely different questions and both are
    load-bearing.  ``has_source_option`` records the *presence* of a ``source``
    option, independently of whether its constraint is a list: a device may
    expose ``source`` with a constraint this code cannot read, and it must
    still be assigned.  A helper returning only the parsed constraint would
    collapse the two and silently stop setting the source on such a device.

    Args:
        raw_options: The device's option tuples, as ``get_options()`` returns
            them.
        requested: The source name the caller asked for.

    Returns:
        A ``(effective_source, has_source_option)`` pair.

    Raises:
        ScanError: If the device exposes a source list that contains neither
            the requested name nor ``"Auto"`` to fall back to.

    """
    available_sources: list[str] = []
    has_source_option = False

    for opt in raw_options:
        if len(opt) >= 9 and opt[1] == "source":
            has_source_option = True
            constraint = opt[8]
            if isinstance(constraint, list):
                available_sources = [str(s) for s in constraint]
            break

    effective_source = requested
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

    return effective_source, has_source_option


def _configure_device(
    dev: SaneDevice,
    settings: ScanSettings,
    effective_source: str,
    *,
    has_source_option: bool,
) -> None:
    """
    Assign the scan options to the open device.

    Args:
        dev: Open SANE device handle.
        settings: The requested scan settings.
        effective_source: The source name resolved by ``_resolve_source``.
        has_source_option: Whether the device exposes a ``source`` option at
            all.  Keyword-only, because a positional boolean is not allowed by
            this project's lint rules.

    """
    dev.mode = settings.mode
    dev.resolution = settings.resolution
    if has_source_option:
        dev.source = effective_source


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
        Return an iterator of validated ADF pages with a per-page timeout.

        Per user decision: "Wrap ADF iteration with per-page timeout (not per-job)
        -- cancel if single page takes longer than 2-3x expected duration."

        The work lives in the module-level ``_acquire_pages``, which uses
        concurrent.futures.ThreadPoolExecutor to wrap each next(iterator) call
        with a timeout, since signal.alarm is not safe in non-main threads
        (per RESEARCH.md Open Question 1).

        Args:
            dev: Open SANE device handle.
            timeout_per_page: Maximum seconds to wait for each page.

        Returns:
            An iterator of validated PIL Images, one per scanned page.

        """
        return _acquire_pages(dev, timeout_per_page)

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
            effective_source, has_source_option = _resolve_source(
                dev.get_options(), settings.source
            )

            # Set device options
            _configure_device(
                dev,
                settings,
                effective_source,
                has_source_option=has_source_option,
            )

            # Set scan area geometry for paper size constraint (D-01)
            geometry_set = _set_geometry(dev, settings.paper_size)

            use_adf = classify_source(effective_source).uses_feeder

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
