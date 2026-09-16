"""
SANE scanner backend implementation wrapping python-sane.

This module provides the concrete SaneBackend that communicates with
physical scanners through the SANE (Scanner Access Now Easy) library.
Key safety measures:
- sane.init() runs once per process, behind a module-level guard: the first
  SaneBackend built initialises SANE and every later one does not, and after
  shutdown() a later init is allowed again (Pitfall #1, HARD-05, D-17)
- Device handles managed via context manager with cancel+close (Pitfall #4),
  skipped entirely while a read is still inside SANE (HARD-03, D-12/D-13)
- No progress callbacks to snap() (Pitfall #2)
- Source option validated against device capabilities (Pitfall #5)
- Every blocking acquisition, fed or flatbed, runs on a daemon thread under
  one per-page timeout, with inline validation (HARD-03, HARD-04)
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import threading
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Protocol, assert_never

import PIL.Image
from PIL import Image

from saneless.exceptions import ConfigError, FeederEmptyError, ScanError, describe
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
    from collections.abc import Callable, Generator, Iterator

    from saneless.scanner.base import PageRecord, PageSink

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


def require_sane() -> None:
    """
    Import python-sane, or fail at once with an install hint.

    This is the single python-sane availability check the SANE-using CLI
    commands run first (D-05).  A failure is a setup problem, not a scan
    problem, so it is a ``ConfigError`` and exits 2 through that category.

    It is never called at import, so ``--help`` and every command that does not
    touch a scanner stay free of the import.  python-sane is a mandatory
    dependency, and failing loudly before any work starts beats failing
    mid-scan with a bare ``ModuleNotFoundError``.

    Both measured failure shapes are covered by one ``except ImportError``:
    a missing package raises ``ModuleNotFoundError`` (a subclass), and a
    missing ``libsane.so`` raises a plain ``ImportError`` naming the shared
    object.  The import's own reason is kept in the message, because it is
    what tells those two cases apart.

    Raises:
        ConfigError: If python-sane cannot be imported, chained to the
            ``ImportError``.

    """
    try:
        _ensure_sane()
    except ImportError as exc:
        msg = (
            f"python-sane cannot be imported ({describe(exc)}). Install the SANE "
            "development package (libsane-dev on Debian/Ubuntu, "
            "sane-backends-devel on Fedora/RHEL) and reinstall saneless; see "
            "Install on Bare Metal in the documentation"
        )
        raise ConfigError(msg) from exc


# Allow high-DPI scans without triggering Pillow's decompression bomb check.
# 600 DPI A4 color = ~34.8M pixels; 1200 DPI = ~139M pixels.
# Pillow default limit is 89.5M pixels.
PIL.Image.MAX_IMAGE_PIXELS = 200_000_000

# Per-page timeout: 2x a generous single-page scan estimate (60s at 600 DPI).
# At 300 DPI typical scan is ~10-15s, so 120s is very conservative.
#
# It bounds one page on BOTH acquisition paths: one fed sheet's next(iterator)
# and one flatbed sheet's start()+snap() alike, with no second constant and no
# config key of its own (HARD-04, D-14).
_DEFAULT_PAGE_TIMEOUT_SECONDS: float = 120.0

# How long the timeout path waits for a cancelled read to come back before it
# gives up on the handle entirely.
#
# The value is worth justifying rather than asserting, because every candidate
# is defensible in isolation and only the three cases together decide it:
#
# - A cooperative backend returns in microseconds. Measured against the real
#   SANE `test` backend, dev.cancel() returned in 0.000 s from another thread
#   and the blocked snap() returned 0.0 s after that.
# - A `net` backend with a live saned returns within one RPC round trip, since
#   saned select()s on the control fd while scanning and the client's blocked
#   read then sees EOF.
# - A `net` backend with a dead link will not return within ANY grace. A
#   longer wait therefore buys nothing except a later error message for the
#   operator, and D-13 already makes the resulting wedge recover by itself the
#   moment the read does come back.
#
# 10 s also sits inside pytest-timeout's 60 s, so a test that hits the whole
# grace still fails as an assertion rather than as a session timeout, and
# outside the web worker's STOP_JOIN_SECONDS of 5 s, which is deliberate: a
# shutdown during the grace returns promptly anyway, because the thread the
# grace is waiting on is a daemon and nothing joins it.
_CANCEL_GRACE_SECONDS: float = 10.0

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
# so at 500 pages a backend that accumulated them would hold roughly 13 GB.
# This cap must not be described as a memory bound, because it is not one: the
# bound is that a page is handed to the sink and forgotten as it arrives, so
# peak memory is a small constant number of decoded pages whatever this cap is
# (HARD-01, D-01). The two are independent, and raising this one is not a
# memory decision.
#
# WHAT IT STILL BOUNDS, INDIRECTLY: spool disk. Every page this loop acquires
# is written to the job's spool, so the page count fixed here is also the
# ceiling on how much of the workspace one runaway pass can consume. It is a
# ceiling, not the check: SpooledPageSink._check_room_for refuses each page
# individually, against that page's decoded size plus the operator's
# min_free_space_mb reserve, and that is what actually protects the disk
# (D-07). This cap's contribution is only that the loop cannot keep asking
# forever while that check does its work.
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

__all__ = ["GeometryUnit", "SaneBackend", "require_sane", "shutdown"]

logger = logging.getLogger(__name__)


def _as_image(obj: object) -> Image.Image:
    """
    Cast an object to Image.Image for type checker satisfaction.

    A value handed back through a thread's result slot has lost its generic
    type, so neither checker can infer that what the iterator yielded is an
    ``Image.Image``. The check is real rather than a cast: it is the one place
    a device double returning something else would be caught.
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

    That byte count is arithmetic over the page's dimensions and band count,
    never a materialised copy of its raw buffer: measuring the length of one
    copied a whole 26 MB page to learn a number that width, height and band
    count already give (M-08's cheap win, D-06). The product is exact for
    ``L`` and ``RGB``, which are the only two modes a SANE ``snap()`` produces
    on either path here. It would be eight times too large for mode ``"1"``,
    where Pillow packs eight pixels into a byte, and a bilevel page would
    therefore clear the ``_MIN_PAGE_BYTES`` floor on eight times less data
    than it looks like. That is recorded rather than handled because nothing
    in this project produces mode ``"1"``; a path that starts to must revisit
    the floor rather than this formula.

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

    # Check 2: Minimum file size (raw pixel data), computed from the page
    # rather than copied out of it -- see the docstring's caveat about mode
    # "1" before reusing this formula anywhere else.
    raw_size = page_image.size[0] * page_image.size[1] * len(page_image.getbands())
    if raw_size < _MIN_PAGE_BYTES:
        logger.warning("Page %d: too small (%d bytes), skipping", page_num, raw_size)
        return False

    return True


class _Slot:
    """
    The one value a reader thread hands back, or the one it raised.

    A mutable cell rather than a queue because there is exactly one producer,
    exactly one value, and exactly one consumer -- and because the consumer
    may give up before the producer ever writes, which is the whole point.
    """

    __slots__ = ("error", "value")

    def __init__(self) -> None:
        """Start empty: no value written, and nothing raised."""
        self.value: object = None
        self.error: BaseException | None = None


@dataclass
class _Wedge:
    """
    What the module remembers about a reader thread still inside SANE (D-13).

    Module-level and **mutated, never rebound**.  Rebinding would need a
    ``global`` statement, which the ``PL`` rules in ruff's ``select`` reject;
    the one existing rebinding in this file carries a ``# noqa`` for exactly
    that, and CLAUDE.md forbids adding another, so this state lives in a
    container instead of in a name.

    ``device`` and ``iterator`` are **strong** references, deliberately.
    ``SaneDev_dealloc`` calls ``sane_close()`` and ``_SaneIterator.__del__``
    calls ``device.cancel()``, so letting a wedged handle be garbage-collected
    would reintroduce exactly the close-while-reading this module now avoids
    (29-RESEARCH.md Pitfall 3).

    ``done`` identifies *which* acquisition is wedged.  A reader that wakes up
    long afterwards compares against it, so a late wake-up belonging to an
    abandoned acquisition cannot clear a wedge that a later one recorded.
    """

    stuck: bool = False
    done: threading.Event | None = None
    device: SaneDevice | None = None
    iterator: object = None
    device_id: str = ""
    page_label: str = ""


# Guards every read and write of _WEDGE.  Three threads reach it -- the worker
# that gave up, the reader that eventually returns, and whichever thread starts
# the next job -- and the close-or-wedge decision is a check and a write that
# must not be split.
_WEDGE_LOCK = threading.Lock()
_WEDGE = _Wedge()


@dataclass
class _Init:
    """
    What the module remembers about the one ``sane_init`` of this process.

    Module-level and **mutated, never rebound**, for the reason ``_Wedge``
    gives: rebinding a module-level name needs a ``global`` statement, which
    the ``PL`` rules in ruff's ``select`` reject, and CLAUDE.md forbids adding
    a second suppression to say otherwise.  Keeping it beside ``_WEDGE`` also
    keeps this module's process-global state in one place rather than two.

    ``host`` is the ``host`` argument the initialising construction passed, not
    the environment variable it may have set.  The two differ whenever
    ``SANE_NET_HOSTS`` was already set externally, and it is the *argument*
    that a later construction is compared against: what the comparison has to
    catch is a second operator-configured host arriving too late to be read.

    Attributes:
        done: Whether ``sane.init()`` has returned successfully and not yet
            been undone by ``shutdown()``.
        host: The host argument that was in effect at that init.
        version: Whatever ``sane.init()`` returned, kept for the log line.

    """

    done: bool = False
    host: str = ""
    version: object = None


# Guards every read and write of _INIT.  Two threads reach it in production --
# whichever builds the backend and whichever shuts it down -- and "look, then
# initialise" is a check and a write that must not be split, or a racing pair
# of constructions would each see an uninitialised SANE and call sane_init
# twice.
_INIT_LOCK = threading.Lock()
_INIT = _Init()

# The prefix every acquisition thread is named with, so a stuck reader is
# identifiable in a ``faulthandler`` dump or a debugger without guessing.
_READER_THREAD_PREFIX = "sane-read-"


def _ensure_initialised(host: str) -> object:
    """
    Initialise SANE, once per process, whoever asks (HARD-05, D-17).

    ``sane_init`` is a process-global call, not a per-object one, so the
    guard is here rather than in ``SaneBackend.__init__``: three one-shot CLI
    commands and the web server each build their own backend, and the second
    ``sane_init`` in a process is at best wasted work.  It is a guard and not
    a singleton deliberately -- every one of those callers keeps getting its
    own ``SaneBackend``, which is what lets the tests and the CLI construct one
    wherever they need it.

    A later construction naming a *different* host is the case worth a WARNING
    rather than silence.  The sane-net backend reads ``SANE_NET_HOSTS`` when it
    is initialised and never again, so the second host is not merely redundant:
    it does nothing at all, while the operator who configured it has every
    reason to believe it is in effect (N-04).  Only the hostnames from the
    operator's own configuration are named, which the existing INFO line
    already logs; no credential is in scope here (ASVS V7).

    The failure translation is ``ScanError`` because a SANE that will not start
    is a scanning failure the caller reports, and it catches ``Exception``
    because python-sane raises ``_sane.error``, ``RuntimeError`` or
    ``AttributeError`` with no shared base (D-08).  A failed init records
    nothing, so the next construction tries again rather than assuming an
    initialised SANE that is not there.

    Args:
        host: Colon-separated sane-net hosts from the caller's configuration,
            or the empty string when none was configured.

    Returns:
        Whatever ``sane.init()`` returned for this process.

    Raises:
        ScanError: If ``sane.init()`` fails, chained to the SANE error.

    """
    with _INIT_LOCK:
        if _INIT.done:
            if host and host != _INIT.host:
                logger.warning(
                    "SANE is already initialised with scanner host %s, so the "
                    "host %s configured here has no effect: SANE_NET_HOSTS is "
                    "read once, at the first initialisation of the process. "
                    "Run one saneless per scanner host, or list both hosts "
                    "colon-separated in one configuration",
                    _INIT.host or "none",
                    host,
                )
            return _INIT.version
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
        try:
            version = sane.init()
        except Exception as exc:
            # python-sane raises _sane.error, RuntimeError or AttributeError,
            # with no shared base, so the boundary catches Exception (D-08).
            init_msg = f"Could not initialise SANE: {describe(exc)}"
            raise ScanError(init_msg) from exc
        _INIT.done = True
        _INIT.host = host
        _INIT.version = version
        logger.info("SANE initialized, version %s", version)
        return version


def _read_outstanding() -> bool:
    """
    Report whether any reader thread is still inside a SANE read.

    ``_wedged_by`` answers the same question about one handle; the shutdown
    path has no handle to ask about and needs the process-wide answer.

    Returns:
        True if a read recorded in the wedge has not come back.

    """
    with _WEDGE_LOCK:
        return _WEDGE.stuck


def shutdown() -> None:
    """
    Shut SANE down for this process, or explain why it was not (D-18).

    Called at an entry point's shutdown and nowhere else: never from a request
    path, and never from an interpreter-exit hook, which would run while a
    daemon reader thread may still be inside ``sane_read``.  It is idempotent,
    so an entry point that closes more than one backend calls ``sane_exit``
    once.

    Two conditions skip the call rather than making it, and both are logged
    because a silently skipped shutdown is indistinguishable from one that
    happened:

    - **SANE was never initialised in this process.** There is nothing to undo,
      and ``sane_exit`` before ``sane_init`` is undefined by the standard.
    - **A read has not returned.** ``sane_exit`` closes every open handle by
      specification, and ``PySane_exit`` runs holding the GIL while
      ``sane_read`` has released it -- exactly the close-while-reading sequence
      ``_open_device`` already refuses on one handle (D-12, D-13), applied to
      all of them at once.  The process is ending anyway, so an un-exited SANE
      costs nothing next to a segfault on the way out.

    Nothing escapes: a failing ``sane_exit`` is logged with its traceback and
    swallowed, because this runs while the process is on its way out and an
    exception here would replace whatever error the operator is being shown.
    The guard is re-armed either way, so a later ``SaneBackend`` initialises
    rather than assuming a SANE that a failed exit may well have left broken.
    """
    with _INIT_LOCK:
        if not _INIT.done:
            logger.debug("SANE was never initialised in this process; nothing to undo")
            return
        if _read_outstanding():
            logger.warning(
                "Leaving SANE initialised: a read has not returned, and "
                "sane_exit() closes every open handle, which SANE forbids "
                "while one is outstanding"
            )
            return
        try:
            sane.exit()
        except Exception:
            logger.warning("Could not shut SANE down", exc_info=True)
        _INIT.done = False
        _INIT.host = ""
        _INIT.version = None
        logger.info("SANE shut down")


def _page_label(page_num: int) -> str:
    """
    Name one page, for its timeout message and its reader thread.

    Args:
        page_num: Zero-based index of the page being acquired.

    Returns:
        The one-based human label, e.g. ``"Page 3"``.

    """
    return f"Page {page_num + 1}"


def _cancel_and_settle(dev: SaneDevice, done: threading.Event, grace: float) -> bool:
    """
    Cancel a blocked read from a second thread, then wait for it to return.

    The cancel runs on a thread of its own and not on the caller's, and that
    is a correctness requirement rather than tidiness.  On the ``net`` backend
    ``sane_cancel`` is ``sanei_w_call(SANE_NET_CANCEL)`` -- a blocking RPC on
    the control wire, issued before the local data fd is closed -- so against a
    saned that has stopped answering it hangs whoever calls it.  Whoever calls
    it here is the worker thread running the whole job, so the grace would
    bound nothing at all (``backend/net.c``; 29-RESEARCH.md Pitfall 2).

    It is sound to call at all only because ``sane_cancel`` releases the GIL,
    as ``sane_read`` does, which is what lets one Python thread cancel what
    another is blocked in (``_sane.c`` 2.9.2, verified).  The cancel thread is
    a daemon for the same reason the reader is: if the RPC never returns, it
    must not keep the process alive.

    A failing cancel is logged and swallowed.  There is nothing else to do
    with it -- the read is already lost -- and raising here would replace the
    timeout the operator actually needs to see.

    Args:
        dev: The device handle the blocked read is inside.
        done: The event the reader sets when it returns, however it returns.
        grace: Seconds to wait for the reader after the cancel is fired.

    Returns:
        True if the reader returned within the grace, False if it did not.

    """

    def fire() -> None:
        try:
            dev.cancel()
        except Exception:
            logger.warning("Cancelling the blocked read failed", exc_info=True)

    canceller = threading.Thread(target=fire, name="sane-cancel", daemon=True)
    canceller.start()
    return done.wait(grace)


def _mark_wedged(dev: SaneDevice, done: threading.Event, label: str) -> bool:
    """
    Record that a read never came back, unless it just did.

    The check and the write are one critical section because the reader may
    return in the instant between the grace expiring and this call.  Reading
    ``done`` under the same lock the reader takes *after* setting it is what
    makes that window closed rather than merely narrow.

    Args:
        dev: The handle the reader is still inside.
        done: The event identifying this acquisition.
        label: The page label, for the refusal message.

    Returns:
        True if the backend is now wedged, False if the reader beat the call.

    """
    with _WEDGE_LOCK:
        if done.is_set():
            return False
        _WEDGE.stuck = True
        _WEDGE.done = done
        _WEDGE.device = dev
        _WEDGE.page_label = label
        return True


def _release_wedge(dev: SaneDevice, done: threading.Event) -> None:
    """
    Close the handle and clear the wedge, from the reader thread itself.

    The reader closes rather than the thread that gave up on it, because by
    then the thread that gave up has long since raised -- and the reader is
    the only thread that knows the read is over, which is the one fact SANE
    requires before any other operation may run on the handle.

    A close failure is logged and never raised: this runs in a daemon thread
    whose exception nobody would see, and an unhandled one would surface in a
    test run as a ``PytestUnhandledThreadExceptionWarning`` turned into an
    error.

    Args:
        dev: The handle to release.
        done: The event identifying this acquisition; a reader belonging to
            some earlier, already-forgotten acquisition matches nothing here
            and does nothing.

    """
    with _WEDGE_LOCK:
        if not (_WEDGE.stuck and _WEDGE.done is done):
            return
        logger.warning(
            "The read on %s returned at last (%s); closing the handle",
            _WEDGE.device_id or "the scanner",
            _WEDGE.page_label,
        )
        try:
            dev.close()
        except Exception:
            logger.warning("Could not close the released scanner", exc_info=True)
        _WEDGE.stuck = False
        _WEDGE.done = None
        _WEDGE.device = None
        _WEDGE.iterator = None
        _WEDGE.device_id = ""
        _WEDGE.page_label = ""


def _wedged_by(dev: SaneDevice) -> bool:
    """
    Report whether this handle is the one a reader is still inside.

    Args:
        dev: The handle to test.

    Returns:
        True if a reader never returned from a read on this device.

    """
    with _WEDGE_LOCK:
        return _WEDGE.stuck and _WEDGE.device is dev


def _retain_iterator(dev: SaneDevice, iterator: object) -> bool:
    """
    Keep a wedged device's iterator alive, reversing the old ``del``.

    ``del iterator`` used to be the cleanup; on this path it is the hazard.
    ``_SaneIterator.__del__`` calls ``device.cancel()``, so dropping the last
    reference to a wedged device's iterator issues a SANE call on a handle a
    read is still inside -- the thing this whole sequence exists to prevent.

    Args:
        dev: The handle the iterator drives.
        iterator: The ``multi_scan()`` iterator.

    Returns:
        True if the iterator was retained because the device is wedged.

    """
    with _WEDGE_LOCK:
        if _WEDGE.stuck and _WEDGE.device is dev:
            _WEDGE.iterator = iterator
            return True
        return False


def _name_wedged_device(dev: SaneDevice, device_id: str) -> bool:
    """
    Record which device is wedged, and report that it is.

    The acquisition helper knows the handle but not its SANE name; the device
    context manager knows both.  This is where the two meet, so the refusal a
    later call raises can name the device the operator has to deal with.

    Only the device id is recorded.  No host string and no credential from
    ``SANE_NET_HOSTS`` is written anywhere on this path (ASVS V7).

    Args:
        dev: The handle being released, or not.
        device_id: Its SANE name.

    Returns:
        True if a reader is still inside this handle.

    """
    with _WEDGE_LOCK:
        if _WEDGE.stuck and _WEDGE.device is dev:
            _WEDGE.device_id = device_id
            return True
        return False


def _refuse_if_wedged(device_id: str, operation: str) -> None:
    """
    Refuse a new SANE operation while a read is still outstanding (D-13).

    Called before anything is opened, because the refusal is worthless
    otherwise: ``sane_open`` on a device whose previous read never returned is
    itself one of the operations the SANE standard forbids.

    The wedge is not permanent.  When the late read finally returns, the
    reader thread closes the handle and clears this record, so a transient
    network hang recovers without a restart -- which is why the message says
    "if it does not" rather than "restart saneless".

    Args:
        device_id: The device the refused call was for.
        operation: What the caller was about to do, in the message.

    Raises:
        ScanError: If a reader is still inside SANE.

    """
    with _WEDGE_LOCK:
        if not _WEDGE.stuck:
            return
        wedged_id = _WEDGE.device_id or "the scanner"
        label = _WEDGE.page_label or "an earlier page"
    wedged_msg = (
        f"Could not {operation} {device_id}: a read on {wedged_id} ({label}) "
        f"has not returned, and SANE allows no other operation on a device "
        f"while one is outstanding. The scan will be possible again as soon "
        f"as the scanner releases it. Restart saneless if it does not."
    )
    raise ScanError(wedged_msg)


def _settle_or_wedge(
    dev: SaneDevice, done: threading.Event, grace: float, label: str
) -> bool:
    """
    Run the D-12 tail: cancel, wait out the grace, then close or wedge.

    Args:
        dev: The handle the blocked read is inside.
        done: The event identifying this acquisition.
        grace: Seconds to wait for the reader after the cancel.
        label: The page label, for the log and the later refusal.

    Returns:
        True if the read returned, so the caller's device context may close
        the handle normally.  False if it did not, in which case the handle is
        wedged and nothing may touch it.

    """
    if _cancel_and_settle(dev, done, grace):
        return True
    if not _mark_wedged(dev, done, label):
        return True
    logger.critical(
        "%s: the scanner did not respond to the cancel within %.0fs. The "
        "device handle is being left open because a read is still inside "
        "SANE; no further scan can run until it returns.",
        label,
        grace,
    )
    return False


def _acquire_with_timeout(
    dev: SaneDevice,
    work: Callable[[], object],
    page_label: str,
    timeout: float,
    grace: float = _CANCEL_GRACE_SECONDS,
) -> Image.Image:
    """
    Run one blocking SANE acquisition under a wall-clock bound (D-11, D-12).

    ``signal.alarm`` is not safe off the main thread, so the bound has to come
    from a second thread either way.  What changed is *which* second thread.
    The thread pool that used to supply it was the wrong one: every worker
    ``concurrent.futures`` starts is non-daemon, and its ``_python_exit``
    hook -- registered with ``threading._register_atexit`` -- joins all of
    them at interpreter exit.  A pooled worker stuck in a blocking C call
    therefore stops the process from exiting at all: ``docker stop`` waits out
    its grace and then SIGKILLs, and a test session hangs.  Measured on
    CPython 3.14.2 against a real blocking read: pooled worker stuck, never
    exits; non-daemon thread stuck, never exits; ``daemon=True`` thread stuck,
    exits in 0.24 s.

    So each acquisition gets a fresh ``daemon=True`` thread.  A thread per page
    costs nothing beside a multi-second scan, and -- unlike a shared pool --
    one stuck read cannot poison the next job.

    On timeout the sequence is cancel, wait, then close only if the read
    returned, and the cancel goes out on a thread of its own
    (``_cancel_and_settle``).  **The late value is discarded unconditionally.**
    Only whether the reader *returned* is consulted, never what it returned:
    measured on real libsane, a cancelled ``snap()`` hands back a truncated
    image rather than raising -- 3779x242 of a full page -- and that image
    clears ``_validate_page_image``, so "use it, it arrived after all" would
    put one more page in the PDF than the error message claims (Pitfall 1).

    A ``KeyboardInterrupt`` arriving while this waits takes the identical path
    and is then re-raised, so Ctrl-C during a read leaves the device in the
    same state a timeout does (D-15).

    ``reader.start()`` is inside the guarded block, so an interrupt landing
    once the thread exists cannot abandon it with no cancel ever fired.  The
    handler then asks whether there *is* a reader before running the D-12 tail,
    because the other end of that window is real too and worse: an interrupt
    arriving before the thread was created would otherwise fire ``dev.cancel()``
    on a handle with no read in progress, block for the whole grace on an event
    nothing will ever set, and then mark a wedge that nothing could ever clear
    -- ``_release_wedge`` is only called from a reader's ``finally``, and there
    would be no reader.  Every later ``scan_pages`` and ``get_capabilities``
    would refuse and ``shutdown()`` would permanently skip ``sane.exit()``
    (WR-05).

    ``started`` and ``is_alive()`` are both consulted because they answer for
    different halves of that window: the flag for an interrupt after
    ``start()`` returned, and the liveness check for one that landed inside it,
    after the thread had already been handed to the OS.

    The reader catches ``BaseException`` and stores it: nothing may reach
    ``threading.excepthook``, where an unhandled thread exception becomes a
    ``PytestUnhandledThreadExceptionWarning`` and, under this project's
    ``filterwarnings = ["error"]``, an error in an unrelated test.

    Args:
        dev: The open handle the work will block inside.
        work: The blocking call, as a no-argument callable.
        page_label: Names the page in the timeout message and the thread.
        timeout: Maximum seconds to wait for the page.
        grace: Maximum seconds to wait for the read after cancelling it.

    Returns:
        The acquired page.

    Raises:
        ScanError: If the page did not arrive within the timeout, naming the
            unresponsive cancel as well when the read never came back.
        BaseException: Whatever the work raised, re-raised unchanged --
            including ``StopIteration``, by design, because that is the
            feeder-empty signal the caller's ladder is written around.

    """
    done = threading.Event()
    slot = _Slot()

    def read() -> None:
        try:
            slot.value = work()
        except BaseException as exc:
            # Handed to the waiter rather than raised: see the docstring.
            slot.error = exc
        finally:
            done.set()
            _release_wedge(dev, done)

    reader = threading.Thread(
        target=read, name=f"{_READER_THREAD_PREFIX}{page_label}", daemon=True
    )
    started = False
    try:
        reader.start()
        started = True
        finished = done.wait(timeout)
    except KeyboardInterrupt:
        if started or reader.is_alive():
            _settle_or_wedge(dev, done, grace, page_label)
        raise
    if finished:
        if slot.error is not None:
            raise slot.error
        return _as_image(slot.value)

    returned = _settle_or_wedge(dev, done, grace, page_label)
    logger.error("%s timed out after %.0fs", page_label, timeout)
    timeout_msg = f"{page_label} timed out after {timeout:.0f}s"
    if not returned:
        timeout_msg += (
            "; the scanner did not respond to the cancel, so saneless is "
            "still waiting for that read to return"
        )
    raise ScanError(timeout_msg)


def _acquire_pages(
    dev: SaneDevice,
    sink: PageSink,
    crop: Callable[[Image.Image], Image.Image],
    timeout_per_page: float,
    grace: float = _CANCEL_GRACE_SECONDS,
) -> tuple[list[PageRecord], int]:
    """
    Spool the validated ADF pages, and report how many sheets were skipped.

    A page flows device -> validate -> crop -> ``sink.add`` -> record, one at a
    time, and no list of images exists anywhere along it (HARD-01, D-01).  The
    only image-typed name that outlives a loop iteration is the one page being
    acquired, which is why the live-page high-water mark a 12-page scan
    measures is 2 and not 1: the loop variable still references page *k-1*
    while page *k* is being read.  That is left alone deliberately -- a ``del``
    added to make the number 1 would exist only to satisfy a test
    (29-RESEARCH.md Finding 4).

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
        sink: Where each accepted page goes.  ``add`` is called exactly once
            per accepted page, after it passed its integrity checks and was
            cropped, and nothing here retains the image afterwards.
        crop: Applied to each accepted page before the sink sees it, so what
            is spooled is what the PDF embeds.  It is the caller's closure
            over the paper size, the resolution the device chose and whether
            the scan area was set on the device.
        timeout_per_page: Maximum seconds to wait for each page.
        grace: Maximum seconds to wait for a timed-out read to come back
            after it has been cancelled.  Injectable for the same reason
            ``timeout_per_page`` is: a test proving the unresponsive-cancel
            path must not wait out the module's real ten seconds.

    Returns:
        The records the sink returned, in acquisition order, and how many fed
        sheets were skipped for failing their integrity checks.  The count
        leaves the backend inside D-12's ``ScanBatch`` and by no other route.

    Raises:
        FeederEmptyError: If the feeder produced no pages at all.
        ScanError: If a page times out, the device reports a fault, the page
            count runs past ``_MAX_ADF_PAGES``, every fed page failed its
            integrity checks, or the sink could not take a page.

    """
    iterator = dev.multi_scan()

    records: list[PageRecord] = []
    page_num = 0
    # Returned to the caller now rather than kept local: surfacing this count is
    # D-07, and its channel is D-12's ScanBatch and no second mechanism. It is
    # deliberately kept out of the pipeline's blank-page removal count: Phase 23
    # defined that field as empty-page detection and Phase 30 renders it to
    # users as pages removed for being blank, so reporting a corrupt page
    # through it would be a new small lie in a phase about removing them.
    rejected_pages = 0
    try:
        while True:
            try:
                page_image = _acquire_with_timeout(
                    dev,
                    functools.partial(next, iterator),
                    _page_label(page_num),
                    timeout_per_page,
                    grace,
                )
            except StopIteration:
                break
            except ScanError:
                # FeederEmptyError subclasses ScanError, so saneless's own
                # errors -- including the timeout path's -- propagate here.
                raise
            except Exception as exc:
                scan_error_msg = (
                    f"Scanner error on page {page_num + 1}: {describe(exc)}"
                )
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

            # Strip EXIF (Pitfall #5: invalid EXIF breaks img2pdf).  Before
            # the crop rather than after it: Pillow copies ``info`` into the
            # cropped result, so a strip afterwards would have to be repeated
            # on whichever object came back.
            page_image.info.pop("exif", None)

            # Cropped here, per page, instead of over a finished list
            # afterwards.  The sink is where this page stops being ours, so
            # everything that has to happen to it happens before the hand-off
            # -- and what is spooled is exactly what the PDF embeds (D-03).
            records.append(sink.add(crop(page_image)))
    finally:
        # Pitfall #1's ``del iterator`` was a cleanup; on the wedge path it is
        # the hazard, and this branch is that reversal. Dropping the last
        # reference runs ``_SaneIterator.__del__``, which calls
        # ``device.cancel()`` -- a SANE call on a handle a read is still
        # inside, which is the one thing D-12 exists to prevent. When the
        # device is wedged the record takes the reference instead, and the
        # reader thread drops it when it finally returns. On every other path
        # the ``del`` is exactly what it always was.
        if not _retain_iterator(dev, iterator):
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

    return records, rejected_pages


def _resolve_feeder_source(available_sources: list[str], requested: str) -> str:
    """
    Pick the single-sided document feeder a manual-duplex pass scans through.

    The operator's ``requested`` source wins when the device reports it and it
    is a single-sided feeder, so someone who deliberately chose one of two
    feeders gets that one. Otherwise the first reported single-sided feeder is
    used -- read from the device, never guessed. Hardcoding the short feeder
    name was declined: consumer feeders report ``"Automatic Document
    Feeder"``, and a name the device does not list would fail on exactly the
    hardware manual duplex exists for.

    Whether a source feeds, and whether it scans both sides, is asked of
    ``classify_source`` and nothing else; Phase 24's D-01 makes it the only
    classification rule.

    A feeder that scans both sides (``SourceKind.FEEDER_DUPLEX``) is never
    used (WR-02). Each pass through it returns 2N pages, the two passes'
    counts agree, ``_interleave_duplex`` pairs a front+back sequence with a
    reversed back+front one, and the job reports ``DONE`` with 4N pages in
    scrambled order. This narrows D-02's "``FEEDER`` or ``FEEDER_DUPLEX``"
    wording on purpose: when the operator named a both-sides source and a
    single-sided one exists, the single-sided one is used with a WARNING
    naming both; when every feeder scans both sides, manual duplex is refused
    before any page, because a WARNING beside a green ``DONE`` on an
    unattended appliance is still the silent corruption D-02 exists to stop.

    There is deliberately no ``Auto`` fallback here. ``Auto`` does not feed,
    and with ``auto_source_mode`` at its ``"flatbed"`` default substituting it
    takes one platen snapshot per pass and reports success -- C-01's exact
    failure. A device with no feeder is refused instead, before any page.

    Args:
        available_sources: The source names the device reports. Empty when
            the device's ``source`` constraint cannot be read.
        requested: The source name the profile asked for.

    Returns:
        The single-sided feeder source name to assign to the device.

    Raises:
        ScanError: If every feeder the device reports scans both sides, or
            if the device reports no source that feeds.

    """
    kinds = {source: classify_source(source) for source in available_sources}
    if kinds.get(requested) is SourceKind.FEEDER:
        return requested
    feeder = next(
        (source for source, kind in kinds.items() if kind is SourceKind.FEEDER),
        None,
    )
    if feeder is not None:
        if kinds.get(requested) is SourceKind.FEEDER_DUPLEX:
            logger.warning(
                "Source %r scans both sides of each sheet, so a manual duplex "
                "pass through it would return every page twice; using the "
                "single-sided feeder %r instead",
                requested,
                feeder,
            )
        return feeder
    if SourceKind.FEEDER_DUPLEX in kinds.values():
        msg = (
            "Manual duplex needs a single-sided document feeder, and every "
            "feeder the device reports scans both sides; set "
            'duplex = "hardware" with one of them instead. '
            f"Available: {available_sources}"
        )
        raise ScanError(msg)
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
            the requested name nor ``"Auto"`` to fall back to. For manual
            duplex: if the device has no source option and ``requested`` does
            not name a feeder, if every feeder it reports scans both sides, or
            if it reports no source that feeds.

    """
    reported = _constraint(raw_options, "source")
    has_source_option = reported.present
    available_sources = [str(s) for s in reported.values or []]

    # A disjoint early branch, not a guard inside the flow below: returning
    # here makes the Auto substitution structurally unreachable for manual
    # duplex rather than merely conditioned off, and that substitution is
    # C-01's mechanism. A device with no source option at all feeds without
    # being told and nothing is assigned to it, so the both-sides concern
    # cannot arise; the simplex path already trusts the classifier on the
    # configured name for such a device, and manual duplex does the same
    # (WR-03), which keeps a legacy "Manual Duplex" profile working there.
    if resolve_feeder:
        if not has_source_option:
            if classify_source(requested).uses_feeder:
                return requested, False
            msg = (
                "Manual duplex needs a feeder source, and this device "
                "exposes no source option to choose one; set source to the "
                f"name of its feeder (got {requested!r})"
            )
            raise ScanError(msg)
        return _resolve_feeder_source(available_sources, requested), True

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
    device_id: str,
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
        device_id: The SANE device name, for the error messages.

    Returns:
        The resolution the device actually reports, as an ``int``.  The device
        returns a float; callers downstream want whole dpi.

    Raises:
        ScanError: If the device refuses an assignment -- python-sane raises
            ``_sane.error`` for a bad value and ``AttributeError`` for an
            inactive option -- or the resolution cannot be read back.  The
            message names the option, the value (``!r``, so control characters
            are escaped) and the device, and the original is the cause (D-08).

    """
    # One try per assignment, in order, so the message names the option that
    # failed.  The order itself is the load-bearing part described above.
    assignments: list[tuple[str, str | int]] = []
    if has_source_option:
        assignments.append(("source", effective_source))
    assignments.append(("mode", settings.mode))
    assignments.append(("resolution", settings.resolution))
    for name, value in assignments:
        try:
            setattr(dev, name, value)
        except Exception as exc:
            set_msg = (
                f"Could not set {name} to {value!r} on {device_id}: {describe(exc)}"
            )
            raise ScanError(set_msg) from exc

    try:
        actual_resolution = int(dev.resolution)
    except Exception as exc:
        read_msg = f"Could not read back resolution from {device_id}: {describe(exc)}"
        raise ScanError(read_msg) from exc
    if actual_resolution != settings.resolution:
        logger.warning(
            "Scanner substituted resolution: requested %s dpi, device reports %s dpi",
            settings.resolution,
            actual_resolution,
        )
    return actual_resolution


def _read_options(dev: SaneDevice, device_id: str) -> list:
    """
    Read the device's option list, as a saneless error on failure.

    Shared by ``get_capabilities`` and ``scan_pages``, so both report a failed
    read with the same message.

    Args:
        dev: Open SANE device handle.
        device_id: The SANE device name, for the error message.

    Returns:
        The option tuples ``get_options()`` reports.

    Raises:
        ScanError: If the device cannot report its options, naming the device
            and chained to the original (D-08).

    """
    try:
        return dev.get_options()
    except Exception as exc:
        options_msg = f"Could not read options from {device_id}: {describe(exc)}"
        raise ScanError(options_msg) from exc


@dataclass(frozen=True)
class _PageBudget:
    """
    How long one page may take, and how long its cancel may.

    Bundled into one record rather than passed as two parameters because
    ``_snap_flatbed`` already sits exactly on ruff's ``PLR0913`` argument
    limit, and CLAUDE.md forbids both raising the limit and suppressing the
    rule. ``_acquire_pages`` takes the two separately and is at the same limit
    without needing this, so the asymmetry is the lint's, not a design
    statement; ``pipeline._DeliveryContext`` is the same answer to the same
    constraint.

    Both defaults are the module constants the ADF path uses, so "one sheet is
    one sheet, whichever way it was presented" (D-14) is expressed in the
    default rather than merely asserted about it.

    Attributes:
        timeout: Maximum seconds to wait for the sheet.
        grace: Maximum seconds to wait for a cancelled read to return.

    """

    timeout: float = _DEFAULT_PAGE_TIMEOUT_SECONDS
    grace: float = _CANCEL_GRACE_SECONDS


# The shared default instance.  A module constant and not an inline
# ``_PageBudget()`` in the signature, because a call in a default argument is
# what ruff's B008 rejects; a frozen instance is safe to share.
_DEFAULT_PAGE_BUDGET = _PageBudget()


def _snap_flatbed(
    dev: SaneDevice,
    device_id: str,
    sink: PageSink,
    crop: Callable[[Image.Image], Image.Image],
    budget: _PageBudget = _DEFAULT_PAGE_BUDGET,
) -> PageRecord:
    """
    Acquire one flatbed page under the ADF's timeout, validate it, and spool it.

    ``start()`` opens the SANE data channel and ``snap()`` drains it, so the
    two are wrapped together and nothing else is.  They go through
    ``_acquire_with_timeout`` as a single unit of work, under the same
    ``_DEFAULT_PAGE_TIMEOUT_SECONDS`` a fed sheet gets and with no config key
    of its own: one sheet is one sheet, whichever way it was presented, and a
    platen that stops answering used to hang the job forever while the
    identical failure on a feeder was reported in two minutes (HARD-04, D-14).

    The validate-crop-spool sequence that follows is deliberately outside that
    guard: it is the same sequence the feeder path runs, so one page reaches
    the sink the same way however it was acquired (HARD-04's validation half).

    The one message python-sane's own ADF iterator treats as the end of the
    feed (``sane.py:130``) is mapped to ``FeederEmptyError`` by the same exact
    string test, so a device routed here while reporting an empty feeder still
    tells the operator to load paper (Phase 24 D-03).  Every other failure --
    ``_sane.error`` from ``start()``, ``RuntimeError("Scanner returned no
    data")`` from ``snap()`` -- is a ``ScanError`` naming the device.

    Args:
        dev: Open SANE device handle, already configured.
        device_id: The SANE device name, for the error message.
        sink: Where the page goes.  ``add`` is called exactly once, after the
            page passed its integrity checks and was cropped.
        crop: Applied to the page before the sink sees it.
        budget: The per-page timeout and the cancel grace.  Both default to the
            constants the feeder path uses, and both are injectable for the
            reason ``_acquire_pages``' are: a test proving the
            unresponsive-cancel path must not wait out the module's real ten
            seconds, and without an injectable grace there could be no fast
            flatbed equivalent of ``test_did_not_respond_to_cancel`` at all
            (WR-10).

    Returns:
        The record the sink returned for the one scanned page.

    Raises:
        FeederEmptyError: If SANE reports the feeder out of documents.
        ScanError: If the sheet did not arrive within the timeout, if the page
            fails its integrity checks, or for any other failure, chained to
            the original (D-08).

    """

    def start_and_snap() -> Image.Image:
        dev.start()
        return dev.snap()

    try:
        image = _acquire_with_timeout(
            dev, start_and_snap, _page_label(0), budget.timeout, budget.grace
        )
    except ScanError:
        # The timeout path's own error, already worded and already logged.
        # FeederEmptyError subclasses ScanError and reaches here the same way.
        raise
    except Exception as exc:
        if str(exc) == "Document feeder out of documents":
            raise FeederEmptyError(_FEEDER_EMPTY_MESSAGE) from exc
        snap_msg = f"Scanner error on {device_id}: {describe(exc)}"
        raise ScanError(snap_msg) from exc

    # The same two integrity checks the feeder path runs.  The reason once
    # given for omitting them here -- that the caller sees any failure as an
    # exception -- describes the case they are not for: a zero-dimension
    # image, or a buffer too small to be a page, returned *successfully*.
    # Such an image flowed into the crop and then into assemble_pdf, where
    # saving it was the first thing to notice, while the identical page
    # arriving from a feeder was skipped, counted and reported.
    #
    # Fatal here rather than skipped: a flatbed exposes one sheet at a time,
    # so there is no next page to fall back to and nothing to carry on to.
    if not _validate_page_image(image, 1):
        unreadable_msg = (
            "The scanner returned an unreadable page (zero dimensions, or "
            f"below {_MIN_PAGE_BYTES} bytes of image data)"
        )
        raise ScanError(unreadable_msg)

    # Strip EXIF from flatbed scans too, before the crop copies ``info``.
    image.info.pop("exif", None)
    return sink.add(crop(image))


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

    The first backend built in a process initialises SANE, behind a
    module-level guard; every later one reuses that initialisation, and after
    ``shutdown()`` a later one initialises again.  A backend is otherwise an
    ordinary object -- there is no singleton and no factory, because the three
    one-shot CLI commands and the web server each construct their own.

    Device handles are opened via a context manager that ensures cancel() and
    close() are called on all code paths, unless a read on the handle has not
    returned, in which case neither may be issued at all.
    """

    def __init__(self, host: str = "") -> None:
        """
        Join this process's SANE, initialising it if nobody has yet.

        Args:
            host: Colon-separated sane-net hosts, applied only at the first
                initialisation in the process, and only when
                ``SANE_NET_HOSTS`` is not already set.  A later, differing host
                is reported as having no effect rather than applied.

        Raises:
            ConfigError: If python-sane cannot be imported (``require_sane``).
            ScanError: If ``sane.init()`` fails, chained to the SANE error.

        """
        require_sane()
        self._sane_version = _ensure_initialised(host)

    def close(self) -> None:
        """
        Shut this process's SANE down, through the backend abstraction (D-18).

        The work is ``shutdown()``'s, and it is process-level rather than
        per-object: what is released is the one ``sane_init`` this process
        made, not anything this instance owns.  The method exists so that an
        entry point holding a ``ScannerBackend`` can end it without naming the
        concrete class -- and so that a backend holding nothing process-global
        can go on inheriting the base's no-op.

        It never raises, for the reason ``_open_device``'s ``finally`` already
        states about ``dev.close()``: this runs from a click close callback or
        a lifespan shutdown, where an exception would replace the error the
        operator actually needs to see.  ``shutdown()`` swallows its own, so
        there is nothing left here to catch.
        """
        shutdown()

    @contextlib.contextmanager
    def _open_device(self, device_id: str) -> Generator[SaneDevice]:
        """
        Context manager for SANE device lifecycle.

        Opens the device, yields it for use, then ensures cancel()
        and close() are called on all exit paths (normal and error) --
        **unless** a reader thread is still inside SANE on this handle, in
        which case both are skipped.  That is not an omission: the SANE
        standard forbids any other operation while one is outstanding, and
        ``sane_close`` additionally runs holding the GIL while ``sane_read``
        has released it, so a close racing a blocked read is the one sequence
        python-sane cannot survive (D-12).  The handle is released later by
        the reader thread itself, and until then the wedge record holds it.

        Args:
            device_id: SANE device identifier string.

        Yields:
            An open SANE device handle.

        Raises:
            ScanError: If the device cannot be opened, naming it and chained to
                the SANE error.

        """
        try:
            dev: SaneDevice = sane.open(device_id)
        except Exception as exc:
            open_msg = f"Could not open scanner {device_id}: {describe(exc)}"
            raise ScanError(open_msg) from exc
        try:
            yield dev
        finally:
            if _name_wedged_device(dev, device_id):
                logger.critical(
                    "Leaving scanner %s open: a read has not returned, so "
                    "neither cancel() nor close() may be issued on it",
                    device_id,
                )
            else:
                with contextlib.suppress(Exception):
                    dev.cancel()
                try:
                    dev.close()
                except Exception:
                    # Logged, never raised: an exception from close() here
                    # would replace the one that ended the scan, which is the
                    # error the operator needs to see (T-28-16).
                    logger.warning(
                        "Could not close scanner %s", device_id, exc_info=True
                    )

    def get_devices(self) -> list[DeviceInfo]:
        """
        Enumerate available scanning devices.

        Refuses while a read is outstanding, exactly as ``scan_pages`` and
        ``get_capabilities`` do (D-13). ``sane_get_devices`` is not a
        handle-level call, but the phase's own rule is "never call another SANE
        operation while one is outstanding", and on the ``net`` backend
        enumeration is an RPC on the same control wire the stuck read is on.

        The path is not hypothetical: ``_resolve_device`` calls this whenever
        ``scanner.device`` is empty -- the documented auto-detection default --
        and it does so *before* ``scan_pages``, which is to say before the
        refusal that would otherwise have stopped the job (WR-04).

        Returns:
            List of DeviceInfo objects for each discovered device.

        Raises:
            ScanError: If a previous read has not returned, in which case no
                SANE call is made at all (D-13); or if SANE cannot enumerate
                devices, chained to its error.

        """
        # No device to name, because enumeration is the call that finds out
        # which devices there are.  _refuse_if_wedged names the *wedged* device
        # from its own record either way, so the message still says which
        # scanner is holding things up.
        _refuse_if_wedged("the scanners", "list")
        try:
            raw_devices = sane.get_devices()
        except Exception as exc:
            list_msg = f"Could not list scanners: {describe(exc)}"
            raise ScanError(list_msg) from exc
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

        Raises:
            ScanError: If a previous read has not returned, in which case no
                SANE call is made at all (D-13).

        """
        _refuse_if_wedged(device_id, "read the capabilities of")
        with self._open_device(device_id) as dev:
            raw_options = _read_options(dev, device_id)
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
        sink: PageSink,
        crop: Callable[[Image.Image], Image.Image],
        timeout_per_page: float = _DEFAULT_PAGE_TIMEOUT_SECONDS,
        grace: float = _CANCEL_GRACE_SECONDS,
    ) -> tuple[list[PageRecord], int]:
        """
        Spool the validated ADF pages, with a per-page timeout.

        Per user decision: "Wrap ADF iteration with per-page timeout (not per-job)
        -- cancel if single page takes longer than 2-3x expected duration."

        The work lives in the module-level ``_acquire_pages``, which runs each
        blocking ``next(iterator)`` on a fresh ``daemon=True`` thread and waits
        on an event: ``signal.alarm`` is not safe off the main thread, and the
        thread pool this replaced left non-daemon workers that
        ``concurrent.futures`` joins at interpreter exit, so one stuck read
        stopped the process from exiting at all (D-11).

        Args:
            dev: Open SANE device handle.
            sink: Where each accepted page goes.
            crop: Applied to each accepted page before the sink sees it.
            timeout_per_page: Maximum seconds to wait for each page.
            grace: Maximum seconds to wait for a cancelled read to return.

        Returns:
            The records the sink returned, and the count of fed sheets skipped
            for failing their integrity checks.

        """
        return _acquire_pages(dev, sink, crop, timeout_per_page, grace)

    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Acquire pages from scanner, handing each one to the sink.

        Opens the device, validates the requested source against
        available options, sets scan parameters, and returns the finished
        batch. Uses multi_scan() for ADF sources, snap() for flatbed.
        Does NOT pass a progress callback to snap() to prevent
        segfaults (Pitfall #2).

        Acquisition is eager, and that is what lets the two facts this backend
        measures leave it at all: a generator hands back images only, and its
        return value is discarded by the ``list()`` every caller wrapped it in.

        Eager is not the same as accumulating, though, and this method holds no
        list of images on either path (HARD-01, D-01). Each page is validated,
        cropped and handed to ``sink`` as it arrives, and what comes back is a
        record: where the page was written and what was measured about it. The
        sink belongs to the caller because where a page lands is a pipeline
        fact -- the workspace, the ``tmp_dir`` under it, the pass label in the
        file name -- and none of that is the backend's business.

        The device being closed by the time this returns is a **consequence**
        of that, not a goal -- the handle previously stayed open until the
        generator was drained or garbage-collected. The one exception is a
        read that never returned: the handle is then deliberately left open
        and this method refuses outright until it does (D-12, D-13).

        Args:
            device_id: SANE device identifier string.
            settings: Scan settings (source, resolution, mode).
            sink: Where each accepted page goes, exactly once per page, after
                validation and cropping.

        Returns:
            A ScanBatch carrying the records the sink returned, the resolution
            the device actually used, and how many fed sheets failed their
            integrity checks.

        Raises:
            ScanError: If a previous read has not returned, in which case no
                SANE call is made at all (D-13); if the device does not
                support the requested source; if a page times out; if a
                flatbed scan returns a page that fails its integrity checks --
                unlike a fed sheet, there is no next page to skip to -- or if
                the sink could not take a page.
            FeederEmptyError: If the ADF feeder is empty.

        """
        _refuse_if_wedged(device_id, "scan from")
        with self._open_device(device_id) as dev:
            # Fetched once and passed on: _resolve_source reads the source
            # constraint from it and _set_geometry reads the scan-area options,
            # and a second get_options() call would be a second device round
            # trip for a list that cannot have changed in between.
            raw_options = _read_options(dev, device_id)

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
                device_id=device_id,
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

            # Bound once here and handed down, so both acquisition paths crop
            # the same way and neither has to carry the three geometry facts
            # as extra parameters past ruff's PLR0913 ceiling.  _maybe_crop
            # itself is unchanged; only where it is called from moved.
            crop: Callable[[Image.Image], Image.Image] = functools.partial(
                _maybe_crop,
                paper_size=settings.paper_size,
                resolution=actual_resolution,
                geometry_set=geometry_set,
            )

            if use_adf:
                # ADF/duplex: use multi_scan() for multi-page acquisition
                records, pages_rejected = self._scan_adf_pages(dev, sink, crop)
            else:
                # Flatbed: start() initiates the SANE data channel, then
                # snap() drains it via sane_read() loop.  Without start()
                # the read loop has no data source.  Validation and the crop
                # live in there too, so one sheet reaches the sink by the same
                # route however it was acquired.
                records = [_snap_flatbed(dev, device_id, sink, crop)]
                # Nothing was skipped: an unreadable sheet raised in there.
                pages_rejected = 0

        # Assembled inside the device context but returned outside it, so the
        # handle is released before the caller ever sees the batch.
        return ScanBatch(
            pages=tuple(records),
            actual_resolution=actual_resolution,
            pages_rejected=pages_rejected,
        )
