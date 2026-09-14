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
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Protocol, assert_never

import PIL.Image
from PIL import Image

from saneless.exceptions import FeederEmptyError, ScanError
from saneless.paper_sizes import PAPER_SIZES_MM, crop_to_paper_size
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScanBatch,
    ScannerBackend,
    ScanSettings,
    SourceKind,
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

# The four scan-area options, spelled as ``get_options()`` REPORTS them, with
# hyphens.  This set is for the presence lookup only.
#
# Attribute ASSIGNMENT uses the underscore spelling instead -- ``dev.tl_x``, not
# ``dev["tl-x"]`` -- and the two are deliberately not derived from one another.
# python-sane reports ``tl-x`` from ``get_options()`` while ``__setattr__`` keys
# on ``tl_x``, so a single set of constants used for both sides would be wrong on
# one of them, silently.  Measured, not assumed:
#
#     OPT (24, 'tl-x', 'Top-left x', ..., type=2, unit=3, (0.0, 200.0, 1.0))
#     dev.tl_x = 0.0
_REPORTED_GEOMETRY_OPTIONS: tuple[str, ...] = ("tl-x", "tl-y", "br-x", "br-y")

# Millimetres per inch, for converting a paper size into pixel-denominated
# scan-area units.  The same factor crop_to_paper_size() uses.
_MM_PER_INCH = 25.4

# How far the area a device reports may differ from the one requested before it
# counts as having been clamped, in millimetres.
#
# This is a tolerance and NOT an equality test, on purpose.  SANE geometry
# options are TYPE_FIXED -- a 16.16 fixed-point integer -- so a length that is
# not a multiple of 1/65536 cannot be represented exactly and reads back
# differing in the low bits although the device clamped nothing at all.
# Letter's 215.9 mm is exactly such a length.  Comparing for equality would
# send every letter-sized scan down the crop path for no reason.
#
# One millimetre is the chosen value because the smallest thing being compared
# is a paper size, where a millimetre is far below what anyone could notice,
# while the clamping this has to catch is measured in tens of millimetres
# (210 -> 200, measured).  Three orders of magnitude separate the two, so the
# exact figure is not delicate.
_AREA_TOLERANCE_MM = 1.0

__all__ = ["GeometryUnit", "SaneBackend"]

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


@dataclass(frozen=True)
class _OptionConstraint:
    """
    What a device reports for one option, in whichever shape it used.

    ``present`` answers a genuinely different question from the other two, and
    is not implied by either: a device may expose an option whose constraint
    saneless cannot read, and it must still be recognised as *having* that
    option. A record reporting only the parsed constraint would collapse the
    two questions into one and silently stop saneless assigning the source on
    such a device.

    Attributes:
        present: Whether the device reports the option at all.
        values: The word list the device gave, or None if it gave another shape.
        span: The ``(min, max, step)`` the device gave, or None if it gave
            another shape.

    """

    present: bool
    values: list | None
    span: tuple[float, float, float] | None


def _is_number(value: object) -> bool:
    """
    Report whether a constraint member is a real number.

    ``bool`` is excluded deliberately: it is a subclass of ``int``, so a
    device reporting ``True`` would otherwise convert to ``1.0`` and be
    accepted as a legitimate bound.

    Args:
        value: One member of a device-supplied constraint.

    Returns:
        True if the value is an int or float that is not a bool.

    """
    return isinstance(value, int | float) and not isinstance(value, bool)


def _as_span(constraint: object) -> tuple[float, float, float] | None:
    """
    Read a ``(min, max, step)`` range, or None if that is not what this is.

    The constraint comes from the device, so nothing guarantees it is one of
    the three shapes SANE defines. A tuple of the wrong arity, or one whose
    members are not numbers, yields None rather than a guessed value, and this
    never raises: a malformed constraint must not take down a capability query
    (T-24-27).

    Args:
        constraint: The constraint object the device reported.

    Returns:
        The range as floats, or None if the constraint is not a SANE range.

    """
    if not isinstance(constraint, tuple):
        # None is the third documented shape -- an unconstrained option -- and
        # is not worth a warning. Anything else simply is not a range.
        return None
    if len(constraint) != 3 or not all(_is_number(member) for member in constraint):
        logger.warning(
            "Scanner reports %r for a range-constrained option, which is not a "
            "(minimum, maximum, step) triple; ignoring it rather than guessing",
            constraint,
        )
        return None
    low, high, step = constraint
    return (float(low), float(high), float(step))


def _constraint(raw_options: list[tuple], name: str) -> _OptionConstraint:
    """
    Read what a device reports for one option, in whichever shape it used.

    This is the single place any option constraint is parsed. Two call sites
    used to carry their own copy of the word-list case and neither handled the
    other two shapes, so a range-reporting device's answer was discarded twice
    over -- the whole of N-01. A third copy would be that defect's third life.

    Presence and constraint are reported separately because they are different
    facts; see :class:`_OptionConstraint`.

    Args:
        raw_options: The device's option tuples, as ``get_options()`` returns
            them.
        name: The option name to look for, in the hyphenated spelling
            ``get_options()`` uses.

    Returns:
        What the device reports for that option. An option tuple too short to
        carry a constraint is skipped rather than being an error.

    """
    for opt in raw_options:
        # SANE option tuple:
        # (index, name, title, desc, type, unit, size, cap, constraint)
        if len(opt) >= 9 and opt[1] == name:
            constraint = opt[8]
            if isinstance(constraint, list):
                return _OptionConstraint(present=True, values=constraint, span=None)
            return _OptionConstraint(
                present=True, values=None, span=_as_span(constraint)
            )
    return _OptionConstraint(present=False, values=None, span=None)


class GeometryUnit(IntEnum):
    """
    The unit a SANE device reports for its scan-area options.

    These seven are the complete set of SANE unit codes, confirmed against the
    ``_sane`` extension itself rather than against ``sane.UNIT_STR``, which is
    a display dict for ``Option.__repr__``.  **A backend cannot report
    centimetres or inches** -- SANE defines no such code -- so no such member
    exists here and no such arm exists below.  Offering the operator a scan
    area in those terms is a separate, deferred idea; nothing here implements
    it, and a dead branch for a unit no device can send would be a lie about
    what was measured.

    Dispatch on this is a ``match`` with ``assert_never``, and never a mapping
    keyed on the enum, on purpose.  A mapping missing a member draws no
    diagnostic from either ``ty`` or ``pyrefly``; the same enum in a match is
    caught by both, at edit time, before an unhandled unit can silently
    mis-scale a page.
    """

    UNIT_NONE = 0
    UNIT_PIXEL = 1
    UNIT_BIT = 2
    UNIT_MM = 3
    UNIT_DPI = 4
    UNIT_PERCENT = 5
    UNIT_MICROSECOND = 6


def _units_per_mm(unit: GeometryUnit, resolution: int) -> float | None:
    """
    Return how many device units one millimetre is, or None if unconvertible.

    One factor serves both jobs the geometry path has: scaling a paper size
    into the device's own units, and expressing the read-back tolerance in
    those same units.

    Args:
        unit: The unit the device reports for its scan-area options.
        resolution: The resolution the device actually reported, in dpi.  Read
            back from the device, never the requested value -- converting with
            a resolution the device silently refused would reintroduce M-16's
            substitution one layer further down.

    Returns:
        The number of device units in one millimetre, or None when saneless
        cannot convert the unit and the caller should crop instead.

    """
    match unit:
        case GeometryUnit.UNIT_MM:
            scale = 1.0
        case GeometryUnit.UNIT_PIXEL:
            scale = resolution / _MM_PER_INCH
        case (
            GeometryUnit.UNIT_NONE
            | GeometryUnit.UNIT_BIT
            | GeometryUnit.UNIT_DPI
            | GeometryUnit.UNIT_PERCENT
            | GeometryUnit.UNIT_MICROSECOND
        ):
            logger.warning(
                "Scanner reports its scan area in %s, which saneless cannot "
                "convert to a paper size, will crop after scanning",
                unit.name,
            )
            scale = None
        case _:
            assert_never(unit)
    return scale


def _geometry_unit(raw_options: list[tuple]) -> GeometryUnit | None:
    """
    Read the unit the device reports for its scan-area options.

    The unit lives at index 5 of the option tuple, which the geometry
    arithmetic used to ignore entirely, assuming millimetres (N-03).

    ``br-x`` is the option read: the four scan-area options describe one box,
    and a device reports one unit for all of them.

    The conversion is defensive because the tuple is device-supplied.  A
    conforming backend cannot report a code outside the seven, but nothing in
    the protocol stops a broken one, and scaling by a garbage factor would
    silently mis-size the page.

    Args:
        raw_options: The device's option tuples.

    Returns:
        The reported unit, or None if the device reported something that is not
        a SANE unit at all.

    """
    reported = next(
        (opt[5] for opt in raw_options if len(opt) >= 9 and opt[1] == "br-x"),
        None,
    )
    try:
        return GeometryUnit(reported)
    except ValueError:
        logger.warning(
            "Scanner reports scan-area unit code %s, which is not a SANE unit, "
            "will crop after scanning",
            reported,
        )
        return None


def _missing_geometry_options(raw_options: list[tuple]) -> list[str]:
    """
    Name the scan-area options the device does not report.

    Args:
        raw_options: The device's option tuples, as ``get_options()`` returns
            them.

    Returns:
        The absent option names in their hyphenated spelling, empty when the
        device reports all four.

    """
    reported = {opt[1] for opt in raw_options if len(opt) >= 9}
    return [name for name in _REPORTED_GEOMETRY_OPTIONS if name not in reported]


def _geometry_scale(raw_options: list[tuple], resolution: int) -> float | None:
    """
    Decide whether this device's scan area can be set, and on what scale.

    Three separate ways a device can rule geometry out -- an option it does not
    report at all, a unit code that is not a SANE unit, and a unit saneless
    cannot convert into a length -- collapse here into one answer, so the
    caller has a single decision to make.  Each cause logs its own WARNING
    naming what was wrong before returning None, so the three stay
    distinguishable in the log even though they share a return value.

    Args:
        raw_options: The device's option tuples.
        resolution: The resolution the device actually reported, in dpi.

    Returns:
        The number of device units in one millimetre, or None if the caller
        should crop the image after scanning instead.

    """
    missing = _missing_geometry_options(raw_options)
    if missing:
        logger.warning(
            "Scanner does not report scan-area option(s) %s, will crop after scanning",
            ", ".join(missing),
        )
        return None
    unit = _geometry_unit(raw_options)
    if unit is None:
        return None
    return _units_per_mm(unit, resolution)


def _area_matches(
    actual: tuple[tuple[float, float], tuple[float, float]],
    expected: tuple[float, float],
    tolerance: float,
) -> bool:
    """
    Check that the device kept the scan area it was given (D-19).

    Accepting an assignment is not the same as honouring it.  Measured: writing
    A4's 210 mm to a device whose ``br-x`` range is ``(0.0, 200.0, 1.0)`` stores
    200.0, with no error and no ``INFO_INEXACT`` the caller can see.  Only what
    the device reports back can tell the two apart.

    What is compared is the **box**, ``br - tl``, and not the far corner alone.
    Both corners are written and a device clamps either of them just as
    silently; a device whose ``tl-x``/``tl-y`` range does not start at 0 clamps
    the ``tl = 0.0`` write while accepting ``br`` verbatim, so a far-corner
    comparison agrees with itself over an area that is short by the whole
    minimum.  Measured on a device with a ``(10.0, 300.0, 1.0)`` range: an A4
    request became a 200 x 287 mm scan reported as success.

    Args:
        actual: The ``((tl_x, tl_y), (br_x, br_y))`` the device reports.
        expected: The requested box's ``(width, height)``, in device units.
        tolerance: How far the two may differ, in device units.

    Returns:
        True if the device kept the requested area, False if it clamped it and
        the caller should crop the image instead.

    """
    (tl_x, tl_y), (br_x, br_y) = actual
    expected_x, expected_y = expected
    # The top-left was already being read back here and then discarded, which
    # is what let a clamped tl through: br matched, so the function returned
    # True and _maybe_crop never ran.
    actual_x, actual_y = br_x - tl_x, br_y - tl_y
    within_x = abs(actual_x - expected_x) <= tolerance
    within_y = abs(actual_y - expected_y) <= tolerance
    if within_x and within_y:
        return True
    logger.warning(
        "Scanner clamped the scan area: requested %.1f x %.1f, device reports "
        "%.1f x %.1f in its own units, will crop after scanning",
        expected_x,
        expected_y,
        actual_x,
        actual_y,
    )
    return False


def _set_geometry(
    dev: SaneDevice,
    paper_size: str,
    raw_options: list[tuple],
    resolution: int,
) -> bool:
    """
    Constrain the scan area to a paper size, if the device really can.

    **The presence check is what makes the crop fallback reachable, and an
    exception handler is not a substitute for it.**  ``SaneDev.__setattr__``
    stores an unrecognised option name straight into ``__dict__`` and returns --
    no device call, no validation, no raise (``sane.py:188``).  So on a scanner
    with no scan-area options, ``dev.br_y = 297.0`` *succeeds*, this function
    used to return True, and ``_maybe_crop`` never ran: ``paper_size`` was
    silently ignored and the user got a full-bed scan (M-15).  Asking the
    device's own option list first is the only way to tell the two cases apart.

    Args:
        dev: Open SANE device handle.
        paper_size: Paper size key (e.g. ``"a4"``).
        raw_options: The device's option tuples, already fetched by the caller.
        resolution: The resolution the device actually reported, in dpi, used
            to convert the paper size when the device denominates its scan area
            in pixels.

    Returns:
        True if the scan area was set on the device, False if the caller should
        crop the image afterwards instead.

    """
    # "full" means no constraint at all and is deliberately absent from
    # PAPER_SIZES_MM, so this one lookup answers both "is a constraint wanted?"
    # and "is it a size we know?".
    dims = PAPER_SIZES_MM.get(paper_size)
    if dims is None:
        return False
    scale = _geometry_scale(raw_options, resolution)
    if scale is None or scale <= 0.0:
        # Guard the value, not just its absence.  ``_units_per_mm`` returns
        # ``resolution / _MM_PER_INCH`` for UNIT_PIXEL, which is 0.0 for any
        # device whose read-back resolution truncates to 0 -- and 0.0 passes an
        # ``is None`` test, makes ``expected`` (0.0, 0.0), stores a zero-size
        # box on the device, and then lets ``_area_matches`` agree with itself
        # inside a tolerance that is also 0.  A zero-area scan reported as
        # success is worse than the crop fallback it bypasses.
        return False
    width_mm, height_mm = dims
    expected = (width_mm * scale, height_mm * scale)
    try:
        dev.tl_x = 0.0
        dev.tl_y = 0.0
        dev.br_x = expected[0]
        dev.br_y = expected[1]
        actual = dev.area
    except Exception as exc:
        # Name what was swallowed.  A device that reports the options and then
        # refuses them is a different fault from one that never had them, and
        # a bare "does not support geometry" hid which had happened (M-15).
        logger.warning(
            "Scanner rejected the scan-area options (%s), will crop after scanning",
            exc,
        )
        return False
    # A device can accept all four assignments and still quietly shrink the
    # area, so what it reports back is what decides (D-19).
    if not _area_matches(actual, expected, _AREA_TOLERANCE_MM * scale):
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
    paper_size: str,
    resolution: int,
    *,
    geometry_set: bool,
) -> Image.Image:
    """
    Crop to the paper size when the area could not be set on the device.

    Args:
        image: Scanned page image.
        paper_size: Paper size key (e.g. ``"a4"``).
        resolution: The resolution the device actually reported, in dpi.
            Deliberately not the requested one: the crop arithmetic and the
            device's real sampling rate have to agree, or a silently clamped
            resolution yields a cut-off page even when this fallback runs
            exactly as intended (M-16).
        geometry_set: Whether the scan area was already set on the device.

    Returns:
        The cropped image, or the original if no crop is needed.

    """
    if paper_size != "full" and not geometry_set:
        return crop_to_paper_size(image, paper_size, resolution)
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
) -> tuple[list[Image.Image], int]:
    """
    Return the validated ADF pages, and how many sheets were skipped.

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

    Returns:
        The validated pages, and how many fed sheets were skipped for failing
        their integrity checks.  The count leaves the backend inside D-12's
        ``ScanBatch`` and by no other route.

    Raises:
        FeederEmptyError: If the feeder produced no pages at all.
        ScanError: If a page times out, the device reports a fault, the page
            count runs past ``_MAX_ADF_PAGES``, or every fed page failed its
            integrity checks.

    """
    iterator = dev.multi_scan()

    pages: list[Image.Image] = []
    page_num = 0
    # Returned to the caller now rather than kept local: surfacing this count is
    # D-07, and its channel is D-12's ScanBatch and no second mechanism. It is
    # deliberately kept out of the pipeline's blank-page removal count: Phase 23
    # defined that field as empty-page detection and Phase 30 renders it to
    # users as pages removed for being blank, so reporting a corrupt page
    # through it would be a new small lie in a phase about removing them.
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
            pages.append(page_image)
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

    return pages, rejected_pages


def _resolve_feeder_source(available_sources: list[str], requested: str) -> str:
    """
    Pick the document feeder a manual-duplex pass scans through (D-02).

    The operator's ``requested`` source wins when the device reports it and it
    feeds, so someone who deliberately chose one of two feeders gets that one.
    Otherwise the first reported source that feeds is used -- read from the
    device, never guessed. Hardcoding the short feeder name was declined:
    consumer feeders report ``"Automatic Document Feeder"``, and a name the
    device does not list would fail on exactly the hardware manual duplex
    exists for.

    Whether a source feeds is asked of ``classify_source`` and nothing else;
    Phase 24's D-01 makes it the only classification rule.

    There is deliberately no ``Auto`` fallback here. ``Auto`` does not feed,
    and with ``auto_source_mode`` at its ``"flatbed"`` default substituting it
    takes one platen snapshot per pass and reports success -- C-01's exact
    failure. A device with no feeder is refused instead, before any page.

    Args:
        available_sources: The source names the device reports. Empty when
            the device exposes no readable ``source`` option.
        requested: The source name the profile asked for.

    Returns:
        The feeder source name to assign to the device.

    Raises:
        ScanError: If the device reports no source that feeds.

    """
    if requested in available_sources and classify_source(requested).uses_feeder:
        return requested
    for source in available_sources:
        if classify_source(source).uses_feeder:
            return source
    msg = (
        f"Manual duplex needs a document feeder, and the device reports none. "
        f"Available: {available_sources}"
    )
    raise ScanError(msg)


def _resolve_source(
    raw_options: list[tuple], requested: str, *, resolve_feeder: bool = False
) -> tuple[str, bool]:
    """
    Decide which source name to use, and whether the device has the option.

    The two return values answer genuinely different questions and both are
    load-bearing.  ``has_source_option`` records the *presence* of a ``source``
    option, independently of whether its constraint is a list: a device may
    expose ``source`` with a constraint this code cannot read, and it must
    still be assigned.  A helper returning only the parsed constraint would
    collapse the two and silently stop setting the source on such a device.

    The ``Auto`` substitution is a WARNING rather than an INFO because it can
    change how many pages come back.  ``scan_pages`` classifies the *effective*
    source, so once this returns ``"Auto"`` the routing is decided by the
    profile's ``auto_source_mode``, which defaults to ``"flatbed"``: a profile
    asking for ``"ADF Duplex"`` on a device offering only ``Flatbed`` and
    ``Auto`` quietly returns one page from a whole stack.  The comparable
    resolution substitution has been visible since M-16, and a substitution
    that silently drops pages cannot be the quieter of the two.

    Args:
        raw_options: The device's option tuples, as ``get_options()`` returns
            them.
        requested: The source name the caller asked for.
        resolve_feeder: Manual duplex. Resolve a feeder from the device's own
            list via ``_resolve_feeder_source`` instead of validating
            ``requested`` verbatim.

    Returns:
        A ``(effective_source, has_source_option)`` pair.

    Raises:
        ScanError: If the device exposes a source list that contains neither
            the requested name nor ``"Auto"`` to fall back to, or -- for
            manual duplex -- if the device reports no source that feeds.

    """
    reported = _constraint(raw_options, "source")
    has_source_option = reported.present
    available_sources = [str(s) for s in reported.values or []]

    # A disjoint early branch, not a guard inside the flow below: returning
    # here makes the Auto substitution structurally unreachable for manual
    # duplex rather than merely conditioned off, and that substitution is
    # C-01's mechanism. A device with no source option at all yields an empty
    # list and is refused the same way -- it cannot be told to feed.
    if resolve_feeder:
        feeder = _resolve_feeder_source(available_sources, requested)
        return feeder, has_source_option

    effective_source = requested
    if has_source_option and effective_source not in available_sources:
        if "Auto" in available_sources:
            logger.warning(
                "Source '%s' not available; falling back to 'Auto', whose "
                "routing is decided by the profile's auto_source_mode and may "
                "not be multi-page -- a whole stack can come back as one page",
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
) -> int:
    """
    Assign the scan options to the open device, source first (D-11).

    **Order is load-bearing.**  ``sane.py:188-213`` reloads every option
    descriptor when a ``set_option`` reports ``INFO_RELOAD_OPTIONS``, and a
    source change does exactly that.  Setting the source last therefore lets a
    resolution validated against the platen's constraint be stranded under a
    feeder's narrower one.  Asking the device what it is scanning *from* before
    telling it *how* removes that whole class of failure.  Geometry is set
    afterwards, by ``_set_geometry`` in the caller.

    The resolution is then read back, because SANE substitutes silently:
    measured against the real ``test`` backend, ``5000`` comes back as
    ``1200.0`` and ``0`` as ``1.0``, with no error and no signal to the caller.
    Since Phase 23 made the resolution authoritative for the PDF's page
    geometry, an unnoticed substitution yields both a mis-cropped page and a
    wrong MediaBox, so the substitution has to be visible (M-16, T-24-15).

    Args:
        dev: Open SANE device handle.
        settings: The requested scan settings.
        effective_source: The source name resolved by ``_resolve_source``.
        has_source_option: Whether the device exposes a ``source`` option at
            all.  Keyword-only, because a positional boolean is not allowed by
            this project's lint rules.

    Returns:
        The resolution the device actually reports, as an ``int``.  The device
        returns a float; callers downstream want whole dpi.

    """
    if has_source_option:
        dev.source = effective_source
    dev.mode = settings.mode
    dev.resolution = settings.resolution

    actual_resolution = int(dev.resolution)
    if actual_resolution != settings.resolution:
        logger.warning(
            "Scanner substituted resolution: requested %s dpi, device reports %s dpi",
            settings.resolution,
            actual_resolution,
        )
    return actual_resolution


class SaneDevice(Protocol):
    """Protocol describing the SANE device handle interface."""

    mode: str
    # The device returns a float -- measured, not assumed: 300 reads back as
    # 300.0 and 5000 as 1200.0.  This was declared ``int`` for three phases,
    # which made every read-back a quiet lie to the type checker.
    resolution: float
    source: str
    tl_x: float
    tl_y: float
    br_x: float
    br_y: float

    # Read-only in python-sane: __setattr__ rejects this name outright.
    # Declaring it a property is what makes that read-only-ness true for the
    # type checkers as well, rather than only at runtime.  D-19 reads it back
    # to catch an area the device silently clamped.
    @property
    def area(self) -> tuple[tuple[float, float], tuple[float, float]]: ...

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

        Opens the device, reads its option list, and records what the device
        reports for its source, resolution and mode options.

        Args:
            device_id: SANE device identifier string.

        Returns:
            DeviceCapabilities with parsed option information. Resolution
            support is reported in whichever shape the device used -- a word
            list or a range -- with at most one of the two populated and
            neither derived from the other.

        """
        with self._open_device(device_id) as dev:
            raw_options = dev.get_options()
            sources = _constraint(raw_options, "source").values or []
            modes = _constraint(raw_options, "mode").values or []
            resolution = _constraint(raw_options, "resolution")

            return DeviceCapabilities(
                sources=[str(s) for s in sources],
                resolutions=[int(r) for r in resolution.values or []],
                modes=[str(m) for m in modes],
                raw_options=raw_options,
                resolution_range=resolution.span,
            )

    def _scan_adf_pages(
        self,
        dev: SaneDevice,
        timeout_per_page: float = _DEFAULT_PAGE_TIMEOUT_SECONDS,
    ) -> tuple[list[Image.Image], int]:
        """
        Return the validated ADF pages, with a per-page timeout.

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
            The validated pages, and the count of fed sheets skipped for
            failing their integrity checks.

        """
        return _acquire_pages(dev, timeout_per_page)

    def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
        """
        Acquire pages from scanner.

        Opens the device, validates the requested source against
        available options, sets scan parameters, and returns the finished
        batch. Uses multi_scan() for ADF sources, snap() for flatbed.
        Does NOT pass a progress callback to snap() to prevent
        segfaults (Pitfall #2).

        Acquisition is eager, and that is what lets the two facts this backend
        measures leave it at all: a generator hands back images only, and its
        return value is discarded by the ``list()`` every caller wrapped it in.

        The device being closed by the time this returns is a **consequence**
        of that, not a goal -- the handle previously stayed open until the
        generator was drained or garbage-collected. Close-while-reading and
        cancel semantics are Phase 29's HARD-03/HARD-04 and are deliberately
        not folded in here.

        Args:
            device_id: SANE device identifier string.
            settings: Scan settings (source, resolution, mode).

        Returns:
            A ScanBatch carrying the pages, the resolution the device actually
            used, and how many fed sheets failed their integrity checks.

        Raises:
            ScanError: If the device does not support the requested source, or
                if a flatbed scan returns a page that fails its integrity
                checks -- unlike a fed sheet, there is no next page to skip to.
            FeederEmptyError: If the ADF feeder is empty.

        """
        with self._open_device(device_id) as dev:
            # Fetched once and passed on: _resolve_source reads the source
            # constraint from it and _set_geometry reads the scan-area options,
            # and a second get_options() call would be a second device round
            # trip for a list that cannot have changed in between.
            raw_options = dev.get_options()

            # Validate source option against device capabilities
            effective_source, has_source_option = _resolve_source(
                raw_options,
                settings.source,
                resolve_feeder=settings.resolve_feeder_source,
            )

            # Set device options.  The return value is the resolution the
            # device actually chose, which may not be the one requested.
            actual_resolution = _configure_device(
                dev,
                settings,
                effective_source,
                has_source_option=has_source_option,
            )

            # Constrain the scan area to the paper size, if the device reports
            # the options to do it with (D-09), scaling by the unit it reports
            # (D-10) at the resolution it actually chose (D-11).
            geometry_set = _set_geometry(
                dev, settings.paper_size, raw_options, actual_resolution
            )

            source_kind = classify_source(effective_source)
            use_adf = source_kind.uses_feeder

            # An Auto source says nothing about what is actually loaded, so the
            # operator's auto_source_mode decides. That decision stays
            # config-driven; only the *recognition* of an Auto source moved
            # here, to the single classifier.
            #
            # The previous test compared the source string for equality against
            # the one exact spelling ``Auto``, so it was case- and
            # whitespace-sensitive: a device reporting its source as
            # lowercase ``auto`` classifies as AUTO, so it took the single-page
            # path and skipped this override entirely. auto_source_mode = "adf"
            # was then silently ignored and a whole stack came back as one page.
            if source_kind is SourceKind.AUTO:
                use_adf = settings.auto_source_mode == "adf"
                logger.info(
                    "Auto source routing: auto_source_mode='%s', use_adf=%s",
                    settings.auto_source_mode,
                    use_adf,
                )

            if use_adf:
                # ADF/duplex: use multi_scan() for multi-page acquisition
                acquired, pages_rejected = self._scan_adf_pages(dev)
            else:
                # Flatbed: start() initiates the SANE data channel, then
                # snap() drains it via sane_read() loop.  Without start()
                # the read loop has no data source.
                dev.start()
                image = dev.snap()
                # The same two integrity checks the feeder path runs.  The
                # reason given for omitting them here -- that the caller sees
                # any failure as an exception -- describes the case they are
                # not for: a zero-dimension image, or a buffer too small to be
                # a page, returned *successfully*.  Such an image flowed into
                # _maybe_crop and then into assemble_pdf, where img.save() on a
                # 0x0 image was the first thing to notice, while the identical
                # page arriving from a feeder was skipped, counted and
                # reported.
                #
                # Fatal here rather than skipped: a flatbed exposes one sheet
                # at a time, so there is no next page to fall back to and
                # nothing to carry on to.
                if not _validate_page_image(image, 1):
                    unreadable_msg = (
                        "The scanner returned an unreadable page (zero "
                        f"dimensions, or below {_MIN_PAGE_BYTES} bytes of "
                        "image data)"
                    )
                    raise ScanError(unreadable_msg)
                # Strip EXIF from flatbed scans too
                image.info.pop("exif", None)
                acquired = [image]
                # Nothing was skipped: an unreadable sheet raised above.
                pages_rejected = 0

            pages = [
                _maybe_crop(
                    page,
                    settings.paper_size,
                    actual_resolution,
                    geometry_set=geometry_set,
                )
                for page in acquired
            ]

        # Assembled inside the device context but returned outside it, so the
        # handle is released before the caller ever sees the batch.
        return ScanBatch(
            pages=pages,
            actual_resolution=actual_resolution,
            pages_rejected=pages_rejected,
        )
