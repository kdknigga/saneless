"""
What the scan child does with the device, one pass at a time.

python-sane is reached only through ``_ensure_sane()``, so importing this
module never loads libsane.

- No progress callbacks to snap(): the C extension does not validate them,
  and a wrong signature or a raising callback segfaults the process.
- A read that fails waits, bounded, for the native threads the backend started
  for it to end before anything may cancel it (``_BACKEND_THREAD_EXIT_SECONDS``).
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import math
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, assert_never

from PIL import Image

from saneless.exceptions import FeederEmptyError, ScanError, describe
from saneless.paper_sizes import PAPER_SIZES_MM, crop_to_paper_size
from saneless.scanner.base import (
    MAX_PAGES_PER_PASS,
    PassCapReached,
    ScanSettings,
    SourceKind,
    classify_source,
)
from saneless.scanner.options import (
    _constraint,
    _is_number,
    _option_tuple,
    _option_type,
    _OptionConstraint,
)
from saneless.scanner.page_budget import _MM_PER_INCH, _ScanParameters
from saneless.text_safety import neutralise_controls
from saneless.vocabulary import (
    ScanStage,
    ambiguous_source_error,
    sane_init_failure_message,
    sixteen_bit_error,
    source_not_offered_error,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from types import ModuleType

    from saneless.vocabulary import PaperSize

__all__ = [
    "GeometryUnit",
    "PageOutlet",
    "PassOutcome",
    "PassStopped",
    "restart_library",
    "run_pass",
    "start_library",
]

logger = logging.getLogger(__name__)


# Tests patch a fake python-sane module into this name, so the backend can be
# driven without the real C extension.  Production leaves it None and every SANE
# call goes through _ensure_sane(), which keeps python-sane out of import time.
sane: Any = None


def _ensure_sane() -> ModuleType:
    """
    Return the python-sane module every SANE call goes through.

    A module patched into ``sane`` wins; otherwise python-sane is imported.
    The import is not cached, so ``sys.modules`` stays the only record and a
    caller that evicts or blocks the module there sees it on the next call.

    Raises:
        ImportError: If python-sane or its shared library cannot be loaded.

    """
    return sane if sane is not None else importlib.import_module("sane")


# Minimum raw image data size, which catches a corrupt or truncated buffer.
# The smallest legitimate page measured on the SANE `test` backend is 69,620
# bytes (Gray, 75 dpi, 80x100 mm), so the floor does not fire on real pages.
_MIN_PAGE_BYTES: int = 10_000

# Pages one scan_pages() call (one pass, not one job) will acquire.
# python-sane's iterator stops only on "Document feeder out of documents", so
# a source that is not really a feeder rescans its platen until this cap.  It
# is not a memory bound: pages go to the sink as they arrive.
_MAX_ADF_PAGES: int = MAX_PAGES_PER_PASS

# The tighter per-pass cap for a source sent through the feeder that does not
# classify as a feeder, in practice Auto with auto_source_mode = "adf".  It
# bounds a platen rescanned as a feeder to minutes rather than hours.
# See docs/explanation/decisions/0007-auto-feeder-page-cap.md.
_MAX_AUTO_FEEDER_PAGES: Final = 50

_FEEDER_EMPTY_MESSAGE = "No paper detected in feeder"

# The one error python-sane's feeder iterator turns into the end of the feed.
_END_OF_FEED: Final = "Document feeder out of documents"

# The scan-area options as ``get_options()`` reports them, with hyphens, for
# the presence lookup only.  Assignment uses the underscore spelling
# (``dev.tl_x``), because python-sane's ``__setattr__`` keys on that; do not
# derive one spelling from the other.
_REPORTED_GEOMETRY_OPTIONS: tuple[str, ...] = ("tl-x", "tl-y", "br-x", "br-y")


# How far the area a device reports may differ from the one requested before it
# counts as clamped.  A tolerance, not equality: a device rounds to its step
# (the `test` backend reads 115.9 back as 116.0) and 16.16 fixed-point
# geometry reads back differing in the low bits.
_AREA_TOLERANCE_MM = 1.0

# SANE's value-type codes, index 4 of an option tuple.  python-sane refuses a
# float for an integer option, even a whole float, so a value has to be
# written in the option's own type.
_SANE_TYPE_INT = 1
_SANE_TYPE_FIXED = 2

# The options a feeder that centres the sheet (fujitsu, canon_dr) uses to learn
# the paper size, active only while a feeder source is selected.  The scan
# area is then measured from the centred window's corner.
_PAGE_SIZE_OPTIONS: tuple[str, str] = ("page-width", "page-height")

# Capability bits at index 7: settable means software-selectable and active.
_SANE_CAP_SOFT_SELECT = 1
_SANE_CAP_INACTIVE = 32

# saneless scans at 8 bits and refuses 16: python-sane misreads a 16-bit
# frame, which comes back twice as tall, read past the end of its buffer.
_EIGHT_BITS = 8
_SIXTEEN_BITS = 16


def _as_image(obj: object) -> Image.Image:
    """
    Narrow an object handed back through a thread's result slot to an image.

    The check is real rather than a cast: it is the one place a device double
    returning something else would be caught.
    """
    if not isinstance(obj, Image.Image):
        msg = f"Expected Image, got {type(obj)}"
        raise TypeError(msg)
    return obj


def _option_is_writable(raw_options: list[tuple], name: str) -> bool:
    """
    Report whether software may set an option now: selectable and active.

    An inactive option, such as ``depth`` in a line-art mode, refuses a value
    with ``AttributeError``, which would fail the scan.
    """
    opt = _option_tuple(raw_options, name)
    if opt is None or not isinstance(opt[7], int):
        return False
    return bool(opt[7] & _SANE_CAP_SOFT_SELECT) and not opt[7] & _SANE_CAP_INACTIVE


def _includes_eight(found: _OptionConstraint) -> bool:
    """
    Report whether a ``depth`` constraint lets the device scan at 8 bits.

    A word list includes 8 when 8 is one of its words. A range includes it when
    8 lies between its bounds and on its step grid, counted from the minimum;
    a step of 0 means any value in the range.
    """
    if found.values is not None:
        return any(_is_number(word) and word == _EIGHT_BITS for word in found.values)
    if found.span is None:
        return False
    low, high, step = found.span
    if not low <= _EIGHT_BITS <= high:
        return False
    if step <= 0:
        return True
    steps = (_EIGHT_BITS - low) / step
    return math.isclose(steps, round(steps), abs_tol=1e-9)


def _eight_bit_depth(raw_options: list[tuple]) -> int | float | None:
    """
    Decide the ``depth`` value to write, if the device can scan at 8 bits.

    Args:
        raw_options: The device's option tuples, as reported for the source
            already selected.

    Returns:
        ``8`` for an integer option, ``8.0`` for a fixed-point one, or None
        when the device has no writable ``depth`` option that includes 8.

    """
    if not _option_is_writable(raw_options, "depth"):
        return None
    if not _includes_eight(_constraint(raw_options, "depth")):
        return None
    if _option_type(raw_options, "depth") == _SANE_TYPE_FIXED:
        return float(_EIGHT_BITS)
    return _EIGHT_BITS


@dataclass(frozen=True)
class _SourceChoice:
    """
    Which source to assign, and what resolving it found out along the way.

    Attributes:
        effective: The name to assign to the device: the device's own
            spelling of a matched entry, the device's ``Auto`` entry standing
            in for a flatbed, the trimmed request when the device's list
            cannot be read, or the request as given when the device has no
            source option at all.
        has_option: Whether the device reports a ``source`` option, whether
            or not its constraint could be read.
        substituted_from: The source the profile asked for when the device's
            ``Auto`` entry was chosen in its place, otherwise None.

    """

    effective: str
    has_option: bool
    substituted_from: str | None


def _match_source(available: list[str], requested: str) -> str | None:
    """
    Find the device's entry a profile's source names, ignoring case and padding.

    A single match is returned in the device's spelling, since libsane strips
    nothing.  Nothing is matched by prefix, although libsane would take a
    unique one: ``"ADF"`` is a prefix of ``"ADF Duplex"`` too.

    Raises:
        ScanError: If more than one entry matches once case and surrounding
            whitespace are ignored.

    """
    if requested in available:
        return requested
    folded = requested.strip().casefold()
    matches = [entry for entry in available if entry.strip().casefold() == folded]
    if len(matches) > 1:
        # The entries are device-supplied text bound for the terminal and log.
        ambiguous_msg = ambiguous_source_error(
            neutralise_controls(requested),
            [neutralise_controls(entry) for entry in matches],
        )
        raise ScanError(ambiguous_msg)
    return matches[0] if matches else None


class GeometryUnit(IntEnum):
    """
    The unit a SANE device reports for its scan-area options.

    These seven are the complete set of SANE unit codes; a backend cannot
    report centimetres or inches.

    Dispatch on this with a ``match`` and ``assert_never``, never a mapping: a
    mapping missing a member draws no diagnostic from ``ty`` or ``pyrefly``.
    """

    UNIT_NONE = 0
    UNIT_PIXEL = 1
    UNIT_BIT = 2
    UNIT_MM = 3
    UNIT_DPI = 4
    UNIT_PERCENT = 5
    UNIT_MICROSECOND = 6


# What each path does when the device's unit cannot be converted, as the
# WARNING says it.
_GEOMETRY_FALLBACK: Final = "will crop after scanning"
_PAGE_SIZE_FALLBACK: Final = "will scan the full window"


def _units_per_mm(
    unit: GeometryUnit, resolution: int, *, fallback: str
) -> float | None:
    """
    Return how many device units one millimetre is, or None if unconvertible.

    One factor scales both the paper size and the read-back tolerance.

    Args:
        unit: The unit the device reports for its scan-area options.
        resolution: The resolution the device actually reported, in dpi,
            never the requested one, which the device may have refused.
        fallback: What the scan does instead, for the WARNING an
            unconvertible unit logs, e.g. ``"will crop after scanning"``.

    Returns:
        The number of device units in one millimetre, or None when saneless
        cannot convert the unit and the caller should fall back instead.

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
                "convert to a paper size, %s",
                unit.name,
                fallback,
            )
            scale = None
        case _:
            assert_never(unit)
    return scale


def _option_unit(
    raw_options: list[tuple], name: str, *, fallback: str
) -> GeometryUnit | None:
    """
    Read the unit the device reports for one option, index 5 of its tuple.

    Defensive because the tuple is device-supplied: a broken backend can
    report a code outside the seven, and a garbage factor would mis-size the
    page.
    """
    opt = _option_tuple(raw_options, name)
    reported = None if opt is None else opt[5]
    try:
        return GeometryUnit(reported)
    except ValueError:
        logger.warning(
            "Scanner reports unit code %s for %s, which is not a SANE unit, %s",
            reported,
            name,
            fallback,
        )
        return None


def _geometry_unit(raw_options: list[tuple]) -> GeometryUnit | None:
    """
    Read the unit the device reports for its scan-area options.

    ``br-x`` stands for all four: they describe one box in one unit.
    """
    return _option_unit(raw_options, "br-x", fallback=_GEOMETRY_FALLBACK)


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

    A missing option, an unknown unit code and an unconvertible unit all
    return None, each after its own WARNING, so the log tells them apart.
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
    return _units_per_mm(unit, resolution, fallback=_GEOMETRY_FALLBACK)


def _area_matches(
    actual: tuple[tuple[float, float], tuple[float, float]],
    expected: tuple[float, float],
    tolerance: float,
) -> bool:
    """
    Check that the device kept the scan area it was given.

    A device clamps an out-of-range write silently, with no ``INFO_INEXACT``
    the caller can see, so only the read-back tells.  Compare the box,
    ``br - tl``, not the far corner: a ``tl`` range that does not start at 0
    clamps the ``tl = 0.0`` write while ``br`` is accepted verbatim.
    """
    (tl_x, tl_y), (br_x, br_y) = actual
    expected_x, expected_y = expected
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


def _in_option_type(raw_options: list[tuple], name: str, value: float) -> int | float:
    """Express a number in an option's own SANE type."""
    if _option_type(raw_options, name) == _SANE_TYPE_INT:
        return round(value)
    return float(value)


def _in_option_types(
    raw_options: list[tuple], values: dict[str, float]
) -> dict[str, int | float]:
    """
    Express several numbers each in its own option's SANE type.

    Args:
        raw_options: The device's option tuples.
        values: The number for each hyphenated option name, in its unit.

    Returns:
        The same names, in the same order, with values ``_in_option_type``
        gives them.

    """
    return {
        name: _in_option_type(raw_options, name, value)
        for name, value in values.items()
    }


def _write_options(dev: SaneDevice, values: dict[str, int | float]) -> None:
    """
    Assign options in order, letting any refusal propagate.

    Args:
        dev: Open SANE device handle.
        values: The value for each hyphenated option name, already in its
            option's type, in the order the device should receive them.

    """
    for name, value in values.items():
        setattr(dev, name.replace("-", "_"), value)


def _set_geometry(
    dev: SaneDevice,
    paper_size: PaperSize,
    raw_options: list[tuple],
    resolution: int,
) -> bool:
    """
    Constrain the scan area to a paper size, if the device really can.

    The presence check is what makes the crop fallback reachable: python-sane's
    ``__setattr__`` stores an unrecognised option name in ``__dict__`` without
    a device call or a raise, so ``dev.br_y = 297.0`` succeeds on a scanner
    with no scan-area options.

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
    # "full" is deliberately absent from PAPER_SIZES_MM.
    dims = PAPER_SIZES_MM.get(paper_size)
    if dims is None:
        return False
    scale = _geometry_scale(raw_options, resolution)
    if scale is None or scale <= 0.0:
        # A read-back resolution of 0 makes a pixel scale of 0.0, which would
        # write a zero-size box and then match it inside a zero tolerance.
        return False
    width_mm, height_mm = dims
    # The box compared below is the one written, rounding and all.
    corners = _in_option_types(
        raw_options,
        {
            "tl-x": 0.0,
            "tl-y": 0.0,
            "br-x": width_mm * scale,
            "br-y": height_mm * scale,
        },
    )
    expected = (
        corners["br-x"] - corners["tl-x"],
        corners["br-y"] - corners["tl-y"],
    )
    try:
        _write_options(dev, corners)
        actual = dev.area
    except Exception as exc:
        logger.warning(
            "Scanner rejected the scan-area options (%s), will crop after scanning",
            exc,
        )
        return False
    if not _area_matches(actual, expected, _AREA_TOLERANCE_MM * scale):
        return False
    logger.info(
        "Scan area set to %s: %.1f x %.1f mm",
        paper_size,
        width_mm,
        height_mm,
    )
    return True


def _set_page_size(
    dev: SaneDevice,
    dims: tuple[float, float],
    raw_options: list[tuple],
    resolution: int,
) -> bool:
    """
    Tell a centring feeder the paper size, in each option's unit and type.

    Args:
        dev: Open SANE device handle.
        dims: The paper's ``(width, height)`` in millimetres.
        raw_options: The option list for the source selected, which reports
            ``page-width`` and ``page-height`` as active and settable.
        resolution: The resolution the device actually reported, in dpi, for
            an option denominated in pixels.

    Returns:
        True if both options were set, False if a unit could not be converted
        or the device refused a value, each logged as a WARNING.

    """
    lengths: dict[str, float] = {}
    for name, length_mm in zip(_PAGE_SIZE_OPTIONS, dims, strict=True):
        unit = _option_unit(raw_options, name, fallback=_PAGE_SIZE_FALLBACK)
        scale = (
            None
            if unit is None
            else _units_per_mm(unit, resolution, fallback=_PAGE_SIZE_FALLBACK)
        )
        if scale is None or scale <= 0.0:
            return False
        lengths[name] = length_mm * scale
    try:
        _write_options(dev, _in_option_types(raw_options, lengths))
    except Exception as exc:
        logger.warning(
            "Scanner rejected the page-size options (%s), will scan the full window",
            exc,
        )
        return False
    return True


def _apply_paper_size(
    dev: SaneDevice,
    options: list[tuple],
    settings: ScanSettings,
    *,
    use_adf: bool,
    resolution: int,
) -> _PageFraming:
    """
    Frame every page of the pass to the paper size, where that loses nothing.

    On the flatbed the sheet sits in the top-left corner, so the area is set,
    or the page cropped, from that corner.  A feeder may centre the sheet
    instead, and a box from the corner would cut its right edge off, so a
    feeder pass frames the page only when the device takes ``page-width`` and
    ``page-height`` and centres its own window; otherwise it scans the full
    window.  Routing, not the source's name, makes a pass a feeder pass.
    """
    paper_size = settings.paper_size
    whole = _PageFraming(paper_size="full", resolution=resolution, geometry_set=True)
    dims = PAPER_SIZES_MM.get(paper_size)
    if dims is None:
        return whole
    if use_adf:
        if not all(_option_is_writable(options, name) for name in _PAGE_SIZE_OPTIONS):
            logger.info(
                "paper_size %s not applied: the feeder does not report "
                "page-width/page-height, so where it places the sheet is "
                "unknown; scanning the full window",
                paper_size,
            )
            return whole
        if not _set_page_size(dev, dims, options, resolution):
            return whole
    return _PageFraming(
        paper_size=paper_size,
        resolution=resolution,
        geometry_set=_set_geometry(dev, paper_size, options, resolution),
    )


def _maybe_crop(
    image: Image.Image,
    paper_size: PaperSize,
    resolution: int,
    *,
    geometry_set: bool,
) -> Image.Image:
    """
    Crop to the paper size when the area could not be set on the device.

    The crop is measured from the top-left corner; a feeder page whose
    placement is unknown arrives as ``"full"`` and is never cropped.
    """
    if paper_size != "full" and not geometry_set:
        return crop_to_paper_size(image, paper_size, resolution)
    return image


@dataclass(frozen=True)
class _PageFraming:
    """
    What every accepted page of one scan is cropped to and laid out at.

    The crop and the sink's dpi are one fact, the resolution the device read
    back, so they travel as one record and cannot disagree.

    Attributes:
        paper_size: The paper size key the pages are framed to, e.g. ``"a4"``,
            or ``"full"`` for the whole window.
        resolution: The resolution the device actually reported, in dpi.
        geometry_set: Whether the scan area was already set on the device, in
            which case no crop is needed.

    """

    paper_size: PaperSize
    resolution: int
    geometry_set: bool

    def crop(self, image: Image.Image) -> Image.Image:
        """
        Crop one page to the paper size if the device could not.

        Args:
            image: The page as the device produced it.

        Returns:
            The cropped page, or ``image`` itself if no crop is needed.

        """
        return _maybe_crop(
            image,
            self.paper_size,
            self.resolution,
            geometry_set=self.geometry_set,
        )


def _validate_page_image(page_image: Image.Image, page_num: int) -> bool:
    """
    Check that a scanned page is a readable image at all.

    The byte count is width x height x bands, exact for the ``L`` and ``RGB``
    pages ``snap()`` produces but eight times too large for mode ``"1"``.
    Blank pages are judged only in ``pipeline._drop_blank_pages``, where the
    profile's toggle governs them, never here.
    """
    if page_image.size[0] == 0 or page_image.size[1] == 0:
        logger.warning(
            "Page %d: zero dimensions (%s), skipping", page_num, page_image.size
        )
        return False

    raw_size = page_image.size[0] * page_image.size[1] * len(page_image.getbands())
    if raw_size < _MIN_PAGE_BYTES:
        logger.warning("Page %d: too small (%d bytes), skipping", page_num, raw_size)
        return False

    return True


# How long a failed read waits for the native threads the backend started for
# it to end, before anything may cancel the read.
#
# Some backends (``test``, ``avision``, ``hp``, ``umax``, ``hp3500``) stop their
# reader thread from ``sane_cancel`` with an asynchronous ``pthread_cancel``.
# A reader killed while holding a C-library lock, such as a ``malloc`` arena
# lock, never releases it, and ``sane_cancel`` or a later call hangs.  A read
# that fails at ``sane_start`` still has its reader running about one time in
# twenty, so it is cancelled only once that reader has ended.
#
# The wait is for native thread ids new since the read started that are not
# Python threads; the scanner gate and the CLI ensure no second read runs
# alongside.  A read cancelled while still running (timeout, Ctrl-C, server
# stop) is not covered: it must be cancelled then and there.
_BACKEND_THREAD_EXIT_SECONDS: Final = 1.0

_BACKEND_THREAD_POLL_SECONDS: Final = 0.001

_TASK_DIR: Final = Path("/proc/self/task")


def _native_thread_ids() -> frozenset[int] | None:
    """
    List this process's native thread ids.

    Returns:
        The ids, or ``None`` where the kernel does not list them, in which
        case there is nothing to wait for.

    """
    try:
        return frozenset(int(entry.name) for entry in _TASK_DIR.iterdir())
    except OSError:
        return None


def _await_backend_threads(
    before: frozenset[int] | None,
    limit: float = _BACKEND_THREAD_EXIT_SECONDS,
) -> bool:
    """
    Wait, bounded, for native threads started since ``before`` to end.

    Python threads are excluded; see ``_BACKEND_THREAD_EXIT_SECONDS``.

    Args:
        before: The ids ``_native_thread_ids`` returned just before the read
            started, or ``None`` when they could not be listed.
        limit: The most seconds to wait.

    Returns:
        True if no such thread is left; False if one still was when the wait
        ran out.

    """
    if before is None:
        return True
    deadline = time.monotonic() + limit
    while True:
        current = _native_thread_ids()
        if current is None:
            return True
        python_threads = {thread.native_id for thread in threading.enumerate()}
        if not current - before - python_threads:
            return True
        if time.monotonic() >= deadline:
            logger.debug(
                "A thread the scanner backend started was still running %.1fs "
                "after its read failed; cancelling anyway",
                limit,
            )
            return False
        time.sleep(_BACKEND_THREAD_POLL_SECONDS)


def _choose_feeder_source(available_sources: list[str], requested: str) -> str:
    """
    Pick the single-sided document feeder a manual-duplex pass scans through.

    The ``requested`` source wins when the device reports it (as
    ``_match_source`` matches) and it is a single-sided feeder; otherwise the
    first single-sided feeder the device reports is used.

    A both-sides feeder is never used: each pass returns 2N pages whose
    counts agree, so interleaving reports ``DONE`` with pages in scrambled
    order.  There is no ``Auto`` fallback either, since ``Auto`` may take one
    platen snapshot per pass and report success.

    Args:
        available_sources: The source names the device reports, from a
            ``source`` constraint that could be read.
        requested: The source name the profile asked for.

    Returns:
        The single-sided feeder source name to assign to the device.

    Raises:
        ScanError: If every feeder the device reports scans both sides, if
            the device reports no source that feeds, or if ``requested``
            matches several of the device's sources.

    """
    kinds = {source: classify_source(source) for source in available_sources}
    named = _match_source(available_sources, requested)
    if named is not None and kinds[named] is SourceKind.FEEDER:
        return named
    feeder = next(
        (source for source, kind in kinds.items() if kind is SourceKind.FEEDER),
        None,
    )
    if feeder is not None:
        if named is not None and kinds[named] is SourceKind.FEEDER_DUPLEX:
            logger.warning(
                "Source %r scans both sides of each sheet, so a manual duplex "
                "pass through it would return every page twice; using the "
                "single-sided feeder %r instead",
                named,
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


def _resolve_manual_duplex_source(
    reported: _OptionConstraint, requested: str
) -> _SourceChoice:
    """
    Decide the feeder a manual-duplex pass scans through, or refuse.

    A device with no source option is trusted on the configured name, as on
    the simplex path.  One whose ``source`` list cannot be read accepts only a
    name that classifies as a single-sided feeder.  Otherwise the feeder comes
    from the device's own list (``_choose_feeder_source``).

    Args:
        reported: What the device reports for ``source``.
        requested: The source name the profile asked for.

    Returns:
        The source to assign, and whether the device has a source option.
        Nothing is ever substituted on this path.

    Raises:
        ScanError: If the device has no source option and ``requested`` does
            not name a feeder, if its source list cannot be read and
            ``requested`` does not name a single-sided feeder, if every feeder
            it reports scans both sides, or if it reports no source that feeds.

    """
    if not reported.present:
        if classify_source(requested).uses_feeder:
            return _SourceChoice(requested, has_option=False, substituted_from=None)
        msg = (
            "Manual duplex needs a feeder source, and this device "
            "exposes no source option to choose one; set source to the "
            f"name of its feeder (got {requested!r})"
        )
        raise ScanError(msg)
    if reported.values is None:
        if classify_source(requested) is SourceKind.FEEDER:
            return _SourceChoice(
                requested.strip(), has_option=True, substituted_from=None
            )
        msg = (
            "Manual duplex needs a single-sided document feeder, and this "
            "device's list of sources could not be read to find one; set "
            "source to the name of its single-sided feeder "
            f"(got {requested!r})"
        )
        raise ScanError(msg)
    feeder = _choose_feeder_source([str(s) for s in reported.values], requested)
    return _SourceChoice(feeder, has_option=True, substituted_from=None)


def _resolve_source(
    raw_options: list[tuple], requested: str, *, resolve_feeder: bool = False
) -> _SourceChoice:
    """
    Decide which source name to assign, or refuse before anything is scanned.

    A ``source`` whose list cannot be read is still assigned, with surrounding
    whitespace removed, and the device validates it.  A readable list is
    matched by ``_match_source`` and the device's spelling assigned.

    A request matching nothing is refused before any ``start()``, except that
    a flatbed request may use the device's ``Auto`` when it lists no flatbed:
    some scanners reach their glass only through ``Auto``.  No other request
    is swapped for ``Auto``, which ``auto_source_mode`` routes to the flatbed
    by default.

    Raises:
        ScanError: If the device lists its sources and none matches the
            request, unless the request is a flatbed, the device lists no
            flatbed and it offers ``Auto``; or if the request matches several
            of them. For manual
            duplex: if the device has no source option and ``requested`` does
            not name a feeder, if its source list cannot be read and
            ``requested`` does not name a single-sided feeder, if every feeder
            it reports scans both sides, or if it reports no source that feeds.

    """
    reported = _constraint(raw_options, "source")
    available_sources = [str(s) for s in reported.values or []]

    # An early return keeps the Auto substitution unreachable for manual
    # duplex, where it would make each pass one platen snapshot.
    if resolve_feeder:
        return _resolve_manual_duplex_source(reported, requested)

    if not reported.present:
        return _SourceChoice(requested, has_option=False, substituted_from=None)
    if reported.values is None:
        return _SourceChoice(requested.strip(), has_option=True, substituted_from=None)

    matched = _match_source(available_sources, requested)
    if matched is not None:
        return _SourceChoice(matched, has_option=True, substituted_from=None)

    # Auto stands in only for a flatbed the device does not have.
    kinds = [classify_source(source) for source in available_sources]
    if (
        classify_source(requested) is SourceKind.FLATBED
        and SourceKind.FLATBED not in kinds
    ):
        auto = next(
            (s for s in available_sources if classify_source(s) is SourceKind.AUTO),
            None,
        )
        if auto is not None:
            return _SourceChoice(auto, has_option=True, substituted_from=requested)

    # The names are device-supplied text bound for the terminal and log.
    not_offered_msg = source_not_offered_error(
        neutralise_controls(requested),
        [neutralise_controls(source) for source in available_sources],
    )
    raise ScanError(not_offered_msg)


@dataclass(frozen=True)
class _Configured:
    """
    What configuring the device left behind for the steps after it.

    Attributes:
        resolution: The resolution the device reports after the assignment,
            in whole dpi; it may not be the one requested.
        options: The option list read after the mode was assigned, which
            describes the device as configured.

    """

    resolution: int
    options: list[tuple]


def _assign(dev: SaneDevice, name: str, value: object, device_id: str) -> None:
    """
    Assign one option, as a saneless error naming it if the device refuses.

    Raises:
        ScanError: If the device refuses -- python-sane raises ``_sane.error``
            for a bad value, ``AttributeError`` for an inactive option and
            ``TypeError`` for a value of the wrong type. The message names the
            option, the value (``!r``, so control characters are escaped) and
            the device, and the original is the cause.

    """
    try:
        setattr(dev, name, value)
    except Exception as exc:
        set_msg = (
            f"Could not set {name} to {value!r} on "
            f"{neutralise_controls(device_id)}: {describe(exc)}"
        )
        raise ScanError(set_msg) from exc


_ADF_MODE = "adf-mode"


def _adf_mode_entry(entries: list[str], word: str) -> str | None:
    """
    Find the entry of an ``adf-mode`` list that names a mode, in its spelling.

    Args:
        entries: The entries the device lists for ``adf-mode``.
        word: The mode wanted, casefolded: ``"duplex"`` or ``"simplex"``.

    Returns:
        The device's own entry, or None if it lists no such entry.

    """
    return next((entry for entry in entries if entry.strip().casefold() == word), None)


def _adf_mode_value(raw_options: list[tuple], settings: ScanSettings) -> str | None:
    """
    Decide what to write to ``adf-mode``, if anything.

    epson2, kodakaio and magicolor choose one side or both with this option.
    Hardware duplex asks for ``Duplex`` even when the option is inactive, so
    a feeder that cannot duplex fails before any paper moves.  Any other scan
    writes ``Simplex`` when it can, because the value persists across handles.
    """
    found = _constraint(raw_options, _ADF_MODE)
    if not found.present:
        return None
    entries = [str(entry) for entry in found.values or []]
    if settings.duplex == "hardware":
        return _adf_mode_entry(entries, "duplex") or "Duplex"
    if not _option_is_writable(raw_options, _ADF_MODE):
        return None
    return _adf_mode_entry(entries, "simplex")


def _set_adf_mode(
    dev: SaneDevice,
    settings: ScanSettings,
    choice: _SourceChoice,
    *,
    options: list[tuple],
    device_id: str,
) -> None:
    """
    Write ``adf-mode`` for this scan, or say when hardware duplex cannot happen.

    Most scanners select duplex by the source name.  One with neither that
    nor ``adf-mode``, handed a one-sided feeder under a hardware-duplex
    profile, scans one side with a WARNING.

    Raises:
        ScanError: If the device refuses the value, naming the option.

    """
    value = _adf_mode_value(options, settings)
    if value is not None:
        _assign(dev, _ADF_MODE.replace("-", "_"), value, device_id)
        return
    if (
        settings.duplex == "hardware"
        and not _constraint(options, _ADF_MODE).present
        and classify_source(choice.effective) is SourceKind.FEEDER
    ):
        logger.warning(
            'The profile asks for duplex = "hardware", but this scanner selects '
            "duplex neither by an ADF-mode option nor by the source name %r, so "
            "only one side of each sheet is scanned",
            choice.effective,
        )


def _configure_device(
    dev: SaneDevice,
    settings: ScanSettings,
    choice: _SourceChoice,
    *,
    options: list[tuple],
    device_id: str,
) -> _Configured:
    """
    Assign the scan options to the open device, source first.

    Order is load-bearing: source, adf-mode, mode, depth, resolution.  A
    source or mode change reloads every option descriptor, so a resolution
    set before the source can be stranded under a feeder's narrower
    constraint, and the option list is read again after each.

    The resolution is read back because SANE substitutes silently (the
    ``test`` backend turns ``5000`` into ``1200.0``), and it governs the
    crop and the PDF's page geometry.

    Raises:
        ScanError: If the device refuses an assignment, if the option list
            cannot be read again, or if the resolution cannot be read back,
            naming the device, with the original as the cause.

    """
    if choice.has_option:
        _assign(dev, "source", choice.effective, device_id)
        options = _read_options(dev, device_id)
    _set_adf_mode(dev, settings, choice, options=options, device_id=device_id)
    _assign(dev, "mode", settings.mode, device_id)
    # A 1-bit mode can switch ``depth`` off, or change what it accepts.
    options = _read_options(dev, device_id)
    depth = _eight_bit_depth(options)
    if depth is not None:
        _assign(dev, "depth", depth, device_id)
    _assign(dev, "resolution", settings.resolution, device_id)

    try:
        actual_resolution = int(dev.resolution)
    except Exception as exc:
        read_msg = (
            f"Could not read back resolution from "
            f"{neutralise_controls(device_id)}: {describe(exc)}"
        )
        raise ScanError(read_msg) from exc
    if actual_resolution != settings.resolution:
        logger.warning(
            "Scanner substituted resolution: requested %s dpi, device reports %s dpi",
            settings.resolution,
            actual_resolution,
        )
    return _Configured(resolution=actual_resolution, options=options)


def _route(choice: _SourceChoice, settings: ScanSettings) -> bool:
    """
    Decide whether this pass reads from the document feeder.

    The effective source is classified by ``classify_source``, which
    recognises any spelling of ``Auto``; ``auto_source_mode`` routes an
    ``Auto`` source.  An ``Auto`` standing in for a flatbed is a WARNING only
    when it feeds; left on the glass it did what was asked.

    Args:
        choice: The source ``_resolve_source`` chose.
        settings: The requested scan settings, for ``auto_source_mode``.

    Returns:
        True if the pass reads from the feeder.

    """
    source_kind = classify_source(choice.effective)
    use_adf = source_kind.uses_feeder
    if source_kind is SourceKind.AUTO:
        use_adf = settings.auto_source_mode == "adf"
        logger.info(
            "Auto source routing: auto_source_mode='%s', use_adf=%s",
            settings.auto_source_mode,
            use_adf,
        )
    if choice.substituted_from is not None:
        if use_adf:
            logger.warning(
                "Source %r is not offered, so the scanner's %r source was used "
                "instead, and auto_source_mode=%r sends it through the document "
                "feeder",
                choice.substituted_from,
                choice.effective,
                settings.auto_source_mode,
            )
        else:
            logger.info(
                "Source %r is not offered, so the scanner's %r source was used "
                "instead; auto_source_mode=%r keeps it on the glass",
                choice.substituted_from,
                choice.effective,
                settings.auto_source_mode,
            )
    return use_adf


def _read_options(dev: SaneDevice, device_id: str) -> list:
    """
    Read the device's option list, as a saneless error on failure.

    Raises:
        ScanError: If the device cannot report its options, naming the device
            and chained to the original.

    """
    try:
        return dev.get_options()
    except Exception as exc:
        options_msg = (
            f"Could not read options from {neutralise_controls(device_id)}: "
            f"{describe(exc)}"
        )
        raise ScanError(options_msg) from exc


def _read_parameters(dev: SaneDevice, device_id: str) -> _ScanParameters:
    """
    Read the scan parameters, as a saneless error on failure.

    Args:
        dev: Open SANE device handle, already configured.
        device_id: The SANE device name, for the error message.

    Returns:
        The parameters the device reports.

    Raises:
        ScanError: If the device cannot report its parameters, or reports
            something that is not SANE's five-element shape, naming the device
            and chained to the original.

    """
    try:
        frame_format, last_frame, size, depth, bytes_per_line = dev.get_parameters()
        pixels_per_line, lines = size
        return _ScanParameters(
            frame_format=frame_format,
            last_frame=bool(last_frame),
            pixels_per_line=pixels_per_line,
            lines=lines,
            depth=depth,
            bytes_per_line=bytes_per_line,
        )
    except Exception as exc:
        parameters_msg = (
            f"Could not read the scan parameters from "
            f"{neutralise_controls(device_id)}: {describe(exc)}"
        )
        raise ScanError(parameters_msg) from exc


def _refuse_sixteen_bit(parameters: _ScanParameters, device_id: str) -> None:
    """
    Refuse a 16-bit frame before any page is started.

    A 16-bit frame here comes from a device that cannot scan at 8 bits in the
    chosen mode, and python-sane would misread it.

    Raises:
        ScanError: If the frame is 16 bits per sample.

    """
    if parameters.depth == _SIXTEEN_BITS:
        raise ScanError(sixteen_bit_error(neutralise_controls(device_id)))


class SaneDevice(Protocol):
    """Protocol describing the SANE device handle interface."""

    mode: str
    # The device returns a float: 300 reads back as 300.0.
    resolution: float
    source: str
    tl_x: float
    tl_y: float
    br_x: float
    br_y: float

    # Read-only in python-sane: __setattr__ rejects this name outright.
    @property
    def area(self) -> tuple[tuple[float, float], tuple[float, float]]: ...

    def get_options(self) -> list: ...
    def get_parameters(self) -> tuple[str, int, tuple[int, int], int, int]: ...
    def start(self) -> None: ...
    def snap(self, *, no_cancel: bool = False) -> Image.Image: ...
    def multi_scan(self) -> Iterator[Image.Image]: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...


class PageOutlet(Protocol):
    """
    Where one pass reports its progress and hands its accepted pages.

    The pass talks to nothing else, so the scan child's pipe writer and an
    in-process test double drive it alike.
    """

    def stage(self, stage: ScanStage, page: int | None) -> None:
        """
        Note the stage the pass is about to enter.

        Args:
            stage: The stage about to begin.
            page: The one-based sheet it concerns, or None.

        """

    def configured(
        self, parameters: _ScanParameters, *, resolution: int, use_adf: bool
    ) -> None:
        """
        Note that the device is configured and the first sheet is next.

        Args:
            parameters: The scan parameters the device reports.
            resolution: The resolution the device read back, in dpi.
            use_adf: Whether the pass reads from the document feeder.

        """

    def page(self, image: Image.Image, *, number: int, dpi: int) -> bool:
        """
        Take one accepted, cropped page.

        Args:
            image: The page.
            number: The one-based number of the sheet it came from.
            dpi: The resolution to record it at.

        Returns:
            True once the page is spooled; False to stop the pass.

        """
        ...

    def reading(self, dev: SaneDevice | None) -> None:
        """
        Note the handle a start or read is in progress on, or None after it.

        Once a call with None returns, no cancel of the handle by another
        thread is still running, and none will start.

        Args:
            dev: The handle, or None once the call has returned.

        """

    def cancel_requested(self) -> bool:
        """
        Report whether the pass has been asked to stop.

        Returns:
            True once a cancel was requested.

        """
        ...


@dataclass(frozen=True, slots=True)
class PassOutcome:
    """
    What one pass did, besides the pages it handed on.

    Attributes:
        resolution: The resolution the device read back, in whole dpi.
        rejected: How many fed sheets failed their integrity checks.
        substituted_source: The requested source, when the device's ``Auto``
            stood in for it and fed; None otherwise.
        cap_reached: The cap the pass stopped at, or None when the feed ended
            on its own.

    """

    resolution: int
    rejected: int
    substituted_source: str | None
    cap_reached: PassCapReached | None


class PassStopped(Exception):
    """The pass ended early on a stop or a cancel; the device is already closed."""


def start_library() -> object:
    """
    Initialise SANE in this process.

    Returns:
        Whatever ``sane.init()`` returned.

    Raises:
        ScanError: If initialisation fails, chained to the SANE error.

    """
    try:
        return _ensure_sane().init()
    except Exception as exc:
        # python-sane raises _sane.error, RuntimeError or AttributeError,
        # with no shared base.
        raise ScanError(sane_init_failure_message(describe(exc))) from exc


def restart_library() -> object:
    """
    Restart SANE between passes: ``sane_exit``, then ``sane_init``.

    A failing ``sane_exit`` is logged and the init still runs.

    Returns:
        Whatever ``sane.init()`` returned.

    Raises:
        ScanError: If initialisation fails, chained to the SANE error.

    """
    try:
        _ensure_sane().exit()
    except Exception:
        logger.warning("Could not shut SANE down", exc_info=True)
    return start_library()


def _report_quietly(outlet: PageOutlet, stage: ScanStage) -> Exception | None:
    """
    Report a cleanup stage, keeping a failure to report it for later.

    Returns:
        What the outlet raised, or None.

    """
    try:
        outlet.stage(stage, None)
    except Exception as exc:
        return exc
    return None


@contextlib.contextmanager
def _opened(device_id: str, outlet: PageOutlet) -> Generator[SaneDevice]:
    """
    Open the device for one pass, and cancel and close it on every exit.

    Raises:
        ScanError: If the device cannot be opened, naming it and chained to
            the SANE error.

    """
    outlet.stage(ScanStage.OPEN, None)
    try:
        dev: SaneDevice = _ensure_sane().open(device_id)
    except Exception as exc:
        open_msg = (
            f"Could not open scanner {neutralise_controls(device_id)}: {describe(exc)}"
        )
        raise ScanError(open_msg) from exc
    failed = True
    try:
        yield dev
        failed = False
    finally:
        # The outlet may fail to report a stage (saneless has gone, or a page
        # was cut short); the device is cancelled and closed regardless, and
        # that failure is raised only when the pass raised nothing else.
        report_failure = _report_quietly(outlet, ScanStage.CANCEL)
        with contextlib.suppress(Exception):
            dev.cancel()
        close_failure = _report_quietly(outlet, ScanStage.CLOSE)
        if report_failure is None:
            report_failure = close_failure
        try:
            dev.close()
        except Exception:
            # Never raised: it would replace the pass's own result or error.
            logger.warning(
                "Could not close scanner %s",
                neutralise_controls(device_id),
                exc_info=True,
            )
        if report_failure is not None and not failed:
            raise report_failure


class _SheetCallError(Exception):
    """
    A SANE call starting or reading one sheet failed.

    It keeps the SANE error apart from a failure of the outlet, which is
    raised as it was and never reported as a scanner error.

    Attributes:
        error: What the SANE call raised.

    """

    def __init__(self, error: Exception) -> None:
        """Keep the SANE error."""
        super().__init__(str(error))
        self.error = error


def _start_sheet(dev: SaneDevice) -> None:
    """
    Start one sheet.

    Raises:
        _SheetCallError: ``start`` failed.

    """
    try:
        dev.start()
    except Exception as exc:
        raise _SheetCallError(exc) from exc


def _snap_sheet(dev: SaneDevice) -> object:
    """
    Read the sheet ``start`` began, with no cancel from ``snap`` on failure.

    The backend's reader may still run when the read fails.

    Raises:
        _SheetCallError: ``snap`` failed.

    """
    try:
        return dev.snap(no_cancel=True)
    except Exception as exc:
        raise _SheetCallError(exc) from exc


def _release_or_stop(dev: SaneDevice, outlet: PageOutlet) -> None:
    """
    Release the handle, and cancel the read ``start`` began if a cancel came.

    Releasing waits for a cancel another thread is making on the handle and
    keeps any later one off it, so this thread is then the only one to cancel
    it, and a stage reported afterwards is never written while another thread
    is inside a SANE call.

    Raises:
        PassStopped: A cancel has arrived; the read is cancelled.

    """
    outlet.reading(None)
    if outlet.cancel_requested():
        # python-sane raises _sane.error, RuntimeError or AttributeError with
        # no shared base; a failed cancel leaves the read to the close that
        # follows.
        with contextlib.suppress(Exception):
            dev.cancel()
        raise PassStopped


def _read_sheet(dev: SaneDevice, outlet: PageOutlet, number: int) -> Image.Image:
    """
    Start and read one sheet, as python-sane's feeder iterator would.

    The handle is registered for a cancel only while ``start`` or ``snap``
    runs.  A cancel can arrive while it is not, or while ``start`` runs, when
    cancelling the handle does not stop the read ``start`` begins.  So the
    cancel is looked at again each time the handle is registered or released:
    before ``start`` no sheet is started, and after it the read it began is
    cancelled before anything waits on it.

    Raises:
        _SheetCallError: ``start`` or ``snap`` failed, with no cancel asked.
        PassStopped: If a cancel was requested while the sheet was read; a
            cancelled ``snap()`` can return a truncated image, so it is
            discarded.

    """
    before = _native_thread_ids()
    outlet.stage(ScanStage.START, number)
    outlet.reading(dev)
    try:
        if outlet.cancel_requested():
            raise PassStopped
        _start_sheet(dev)
        _release_or_stop(dev, outlet)
        outlet.stage(ScanStage.READ, number)
        outlet.reading(dev)
        if outlet.cancel_requested():
            _release_or_stop(dev, outlet)
        image = _snap_sheet(dev)
    except Exception as exc:
        outlet.reading(None)
        # An asynchronous cancel can kill a backend reader holding a C-library
        # lock, which then hangs every later SANE call, so nothing cancels this
        # handle until the reader this read started has ended.
        _await_backend_threads(before)
        if isinstance(exc, _SheetCallError) and outlet.cancel_requested():
            raise PassStopped from exc.error
        raise
    outlet.reading(None)
    if outlet.cancel_requested():
        _await_backend_threads(before)
        raise PassStopped
    return _as_image(image)


@dataclass
class _FeedTally:
    """
    What a feeder pass counted.

    Attributes:
        fed: Sheets fed and kept or skipped, not counting one past the cap.
        rejected: Sheets skipped for failing their integrity checks.
        sheet_not_kept: The sheet fed past the cap, or None.

    """

    fed: int = 0
    rejected: int = 0
    sheet_not_kept: int | None = None


def _feed(
    dev: SaneDevice,
    outlet: PageOutlet,
    framing: _PageFraming,
    max_pages: int,
) -> _FeedTally:
    """
    Read the feeder to its end or its cap, handing each accepted page on.

    Sheet N+1 is started only once the outlet has answered for page N.

    Raises:
        FeederEmptyError: If no sheet was fed.
        ScanError: If a sheet fails with anything but end-of-feed, or every
            sheet fed was unreadable.
        PassStopped: If the outlet answered stop or a cancel was requested.

    """
    tally = _FeedTally()
    while True:
        if outlet.cancel_requested():
            raise PassStopped
        number = tally.fed + 1
        try:
            image = _read_sheet(dev, outlet, number)
        except _SheetCallError as failed:
            exc = failed.error
            if str(exc) == _END_OF_FEED:
                break
            scan_error_msg = f"Scanner error on page {number}: {describe(exc)}"
            raise ScanError(scan_error_msg) from exc

        # Seen only on the sheet past the cap, which is discarded uncounted.
        if tally.fed >= max_pages:
            tally.sheet_not_kept = number
            logger.warning(
                "Stopped the feed at the %d-page cap: sheet %d was fed but not kept",
                max_pages,
                number,
            )
            break

        tally.fed = number
        if not _validate_page_image(image, number):
            tally.rejected += 1
            continue
        if not outlet.page(framing.crop(image), number=number, dpi=framing.resolution):
            raise PassStopped

    if tally.fed == 0:
        raise FeederEmptyError(_FEEDER_EMPTY_MESSAGE)
    if tally.rejected == tally.fed:
        all_rejected_msg = (
            f"All {tally.fed} page(s) fed were unreadable and were skipped "
            f"(zero dimensions, or below {_MIN_PAGE_BYTES} bytes of image "
            f"data); no usable page was produced"
        )
        raise ScanError(all_rejected_msg)
    return tally


def _snap_one(
    dev: SaneDevice, device_id: str, outlet: PageOutlet, framing: _PageFraming
) -> None:
    """
    Read the one flatbed page and hand it on.

    Raises:
        FeederEmptyError: If SANE reports the feeder out of documents.
        ScanError: If the page is unreadable, or for any other failure,
            naming the device and chained to the original.
        PassStopped: If the outlet answered stop or a cancel was requested.

    """
    if outlet.cancel_requested():
        raise PassStopped
    try:
        image = _read_sheet(dev, outlet, 1)
    except _SheetCallError as failed:
        exc = failed.error
        if str(exc) == _END_OF_FEED:
            raise FeederEmptyError(_FEEDER_EMPTY_MESSAGE) from exc
        snap_msg = f"Scanner error on {neutralise_controls(device_id)}: {describe(exc)}"
        raise ScanError(snap_msg) from exc

    # Fatal here rather than skipped: a flatbed has no next page.
    if not _validate_page_image(image, 1):
        unreadable_msg = (
            "The scanner returned an unreadable page (zero dimensions, or "
            f"below {_MIN_PAGE_BYTES} bytes of image data)"
        )
        raise ScanError(unreadable_msg)
    if not outlet.page(framing.crop(image), number=1, dpi=framing.resolution):
        raise PassStopped


def run_pass(device_id: str, settings: ScanSettings, outlet: PageOutlet) -> PassOutcome:
    """
    Run one acquisition pass: open, configure, read, close.

    Every page is validated and cropped here; only accepted pages reach the
    outlet, one at a time, and the next sheet is read only after the outlet
    has answered for the last.  The device is closed on every path.

    Args:
        device_id: SANE device identifier string.
        settings: Scan settings (source, resolution, mode, paper size).
        outlet: Where progress goes and accepted pages are handed.

    Returns:
        The resolution read back, the rejected-sheet count, the substituted
        source and the cap state.

    Raises:
        ScanError: If the device cannot be opened or configured, the source
            matches none or several, a sheet fails, or no usable page came.
        FeederEmptyError: If the feeder or flatbed reports no document.
        PassStopped: If the outlet answered stop or a cancel was requested.

    """
    with _opened(device_id, outlet) as dev:
        outlet.stage(ScanStage.CONFIGURE, None)
        raw_options = _read_options(dev, device_id)
        choice = _resolve_source(
            raw_options, settings.source, resolve_feeder=settings.duplex == "manual"
        )
        configured = _configure_device(
            dev, settings, choice, options=raw_options, device_id=device_id
        )
        use_adf = _route(choice, settings)
        framing = _apply_paper_size(
            dev,
            configured.options,
            settings,
            use_adf=use_adf,
            resolution=configured.resolution,
        )
        parameters = _read_parameters(dev, device_id)
        _refuse_sixteen_bit(parameters, device_id)
        outlet.configured(parameters, resolution=configured.resolution, use_adf=use_adf)

        cap_reached: PassCapReached | None = None
        rejected = 0
        if use_adf:
            # A source not named as a feeder (Auto sent through the feeder)
            # may be a platen rescanned forever, so it gets the lower cap.
            named_feeder = classify_source(choice.effective).uses_feeder
            max_pages = _MAX_ADF_PAGES if named_feeder else _MAX_AUTO_FEEDER_PAGES
            tally = _feed(dev, outlet, framing, max_pages)
            rejected = tally.rejected
            if tally.sheet_not_kept is not None:
                cap_reached = PassCapReached(
                    cap=max_pages,
                    sheet_not_kept=tally.sheet_not_kept,
                    auto_source=not named_feeder,
                )
        else:
            _snap_one(dev, device_id, outlet, framing)

    return PassOutcome(
        resolution=configured.resolution,
        rejected=rejected,
        substituted_source=choice.substituted_from if use_adf else None,
        cap_reached=cap_reached,
    )
