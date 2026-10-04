"""
SANE scanner backend implementation wrapping python-sane.

Key safety measures:
- sane.init() runs once behind a module-level guard, and the server
  re-initialises SANE at the start of each job, because after a saned restart
  the net backend's stale control connection fails every later open.  The
  restart is refused while a read is stuck or any handle is open: sane_exit
  closes open handles with a request that can wait forever on a vanished host.
- Scanners are listed in a short-lived child process, never in this one.  See
  docs/explanation/decisions/0002-listing-in-a-child-process.md.
- Device handles are closed by a context manager with cancel+close, except
  while a read is still inside SANE, which allows no other call on the device.
- No progress callbacks to snap(): the C extension does not validate them,
  and a wrong signature or a raising callback segfaults the process.
- Every blocking acquisition, fed or flatbed, runs on a daemon thread under
  one per-page timeout.
"""

from __future__ import annotations

import contextlib
import functools
import importlib
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, assert_never

from PIL import Image

from saneless.exceptions import (
    ConfigError,
    FeederEmptyError,
    ScanError,
    describe,
    describe_text,
)
from saneless.paper_sizes import PAPER_SIZES_MM, crop_to_paper_size
from saneless.scanner.base import (
    MAX_PAGES_PER_PASS,
    DeviceCapabilities,
    DeviceInfo,
    DeviceSurvey,
    PassCapReached,
    ScanBatch,
    ScannerBackend,
    ScanSettings,
    SourceKind,
    classify_source,
)
from saneless.scanner.listing import ListingReply, ListingRequest, run_listing_child
from saneless.scanner.net_hosts import (
    SANE_NET_HOSTS,
    effective_sane_net_hosts,
    exported_sane_net_hosts,
)
from saneless.text_safety import neutralise_controls
from saneless.vocabulary import (
    ambiguous_source_error,
    page_timeout_error,
    scan_page_description,
    sixteen_bit_error,
    source_not_offered_error,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator
    from types import ModuleType

    from saneless.scanner.base import PageRecord, PageSink
    from saneless.vocabulary import PaperSize

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


def require_sane() -> None:
    """
    Import python-sane, or fail at once with an install hint.

    The SANE-using CLI commands run this first; it is never called at import,
    so ``--help`` stays free of python-sane.  A missing package raises
    ``ModuleNotFoundError`` and a missing ``libsane.so`` a plain
    ``ImportError``; the message keeps the import's own reason to tell them
    apart.

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
        # The configuration category's own advice cannot know that the fix is
        # an install, and nothing in the config file brings the library back.
        raise ConfigError(
            msg,
            next_step=(
                "Install the SANE development package and reinstall saneless, "
                "as the error says, then run the command again."
            ),
        ) from exc


def _launch_listing(
    request: ListingRequest,
    *,
    configured_host: str,
    abort: threading.Event | None = None,
) -> ListingReply:
    """
    Start one listing child and return its reply.

    This is the one place this module starts a listing child, and it is looked
    up at call time, so the test suite can replace it with an in-process
    stand-in that answers from a fake python-sane module.
    """
    return run_listing_child(request, configured_host=configured_host, abort=abort)


def _listed_devices(reply: ListingReply) -> tuple[DeviceInfo, ...]:
    """
    Turn the devices a listing child reported into ``DeviceInfo`` objects.

    Args:
        reply: The child's validated reply.

    Returns:
        One ``DeviceInfo`` per device, in the order the child listed them.

    """
    return tuple(
        DeviceInfo(name=name, vendor=vendor, model=model, device_type=device_type)
        for name, vendor, model, device_type in reply.devices
    )


# The per-page timeout scales with the page the device agreed to send: twice
# the reference page's minute (A4, 600 dpi, 8-bit colour), pro rata by bytes.
# The floor keeps every page at two minutes or more.  The ceiling exists
# because the parameters are device-reported numbers, over the LAN for a `net`
# scanner, and a frame claiming 2**31-1 bytes a line would otherwise earn a
# budget too large for `threading.Event.wait` to accept.
#
# A device that does not know the length in advance, as a feeder may not, is
# budgeted for a legal sheet, the longest paper size a profile can name.
_REFERENCE_PAGE_BYTES: Final = 4961 * 7016 * 3
_REFERENCE_PAGE_SECONDS: Final = 60.0
_PAGE_TIMEOUT_FLOOR_SECONDS: Final = 120.0
_PAGE_TIMEOUT_CEILING_SECONDS: Final = 3600.0
_UNKNOWN_LENGTH_MM: Final = PAPER_SIZES_MM["legal"][1]

# The frame formats of a three-pass colour scan, which sends a page as three
# frames, one per channel, each described by the same parameters.
_THREE_PASS_FORMATS: Final = frozenset({"red", "green", "blue"})

# Every frame format of a colour page, sent as one frame or as three.
_COLOUR_FORMATS: Final = _THREE_PASS_FORMATS | {"color"}

# How long the timeout path waits for a cancelled read to come back before it
# gives up on the handle.  A cooperative backend or a live saned returns
# within a round trip, and a `net` backend with a dead link returns within no
# grace at all, so a longer wait only delays the operator's error message.
_CANCEL_GRACE_SECONDS: float = 10.0

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

# The scan-area options as ``get_options()`` reports them, with hyphens, for
# the presence lookup only.  Assignment uses the underscore spelling
# (``dev.tl_x``), because python-sane's ``__setattr__`` keys on that; do not
# derive one spelling from the other.
_REPORTED_GEOMETRY_OPTIONS: tuple[str, ...] = ("tl-x", "tl-y", "br-x", "br-y")

# The same factor crop_to_paper_size() uses.
_MM_PER_INCH = 25.4

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

__all__ = ["GeometryUnit", "SaneBackend", "require_sane", "shutdown"]

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class _OptionConstraint:
    """
    What a device reports for one option, in whichever shape it used.

    ``present`` is not implied by the other two: a device may expose an option
    whose constraint saneless cannot read, and it must still be recognised as
    having that option, or the source would silently go unassigned.

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
    """
    return isinstance(value, int | float) and not isinstance(value, bool)


def _as_span(constraint: object) -> tuple[float, float, float] | None:
    """
    Read a ``(min, max, step)`` range, or None if that is not what this is.

    The constraint is device-supplied, so a malformed one yields None rather
    than a guess, and this never raises: it must not take down a capability
    query.
    """
    if not isinstance(constraint, tuple):
        # None, an unconstrained option, is a documented shape: no warning.
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

    This is the single place any option constraint is parsed; a second copy
    tends to handle only the word-list shape.
    """
    opt = _option_tuple(raw_options, name)
    if opt is None:
        return _OptionConstraint(present=False, values=None, span=None)
    constraint = opt[8]
    if isinstance(constraint, list):
        return _OptionConstraint(present=True, values=constraint, span=None)
    return _OptionConstraint(present=True, values=None, span=_as_span(constraint))


def _option_tuple(raw_options: list[tuple], name: str) -> tuple | None:
    """
    Find one option's tuple in the device's option list.

    A SANE option tuple is ``(index, name, title, desc, type, unit, size, cap,
    constraint)``; one too short to carry all nine is skipped rather than being
    an error.
    """
    for opt in raw_options:
        if len(opt) >= 9 and opt[1] == name:
            return opt
    return None


def _option_type(raw_options: list[tuple], name: str) -> int | None:
    """
    Read an option's SANE value type, index 4 of its tuple.

    Args:
        raw_options: The device's option tuples.
        name: The hyphenated option name.

    Returns:
        The value-type code, or None if the device does not report the option.

    """
    opt = _option_tuple(raw_options, name)
    return None if opt is None else opt[4]


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


class _Slot:
    """
    The one value a reader thread hands back, or the one it raised.

    The consumer may give up before the producer ever writes.
    """

    __slots__ = ("error", "value")

    def __init__(self) -> None:
        """Start empty: no value written, and nothing raised."""
        self.value: object = None
        self.error: BaseException | None = None


@dataclass
class _Wedge:
    """
    What the module remembers about a reader thread still inside SANE.

    Mutated, never rebound.  ``device`` is a strong reference on purpose:
    ``SaneDev_dealloc`` calls ``sane_close()``, so a collected wedged handle
    would be closed under its read.  ``done`` identifies which acquisition is
    wedged, so a late wake-up cannot clear a wedge a later one recorded.

    The record is written before a timed-out read is cancelled, so an
    interrupt during the grace cannot skip it.  Every access holds
    ``_WEDGE_LOCK``.

    - ``settling`` is True while the worker waits out the grace
      (``_settle_or_wedge``); the worker owns the outcome then, and neither
      other thread closes the handle.  The worker ends it by clearing the
      record or by setting ``settling`` False, which makes a real wedge.
    - ``outstanding`` holds a token for each thread still inside SANE on the
      handle, the reader and the canceller.  Once ``settling`` is False, the
      thread that removes the last token closes the handle: a close while a
      ``net`` cancel is still in flight is as unsafe as one under a read.
    """

    stuck: bool = False
    done: threading.Event | None = None
    device: SaneDevice | None = None
    device_id: str = ""
    page_label: str = ""
    settling: bool = False
    outstanding: set[str] = field(default_factory=set)


# The close-or-wedge decision is a check and a write that must not be split.
_WEDGE_LOCK = threading.Lock()
_WEDGE = _Wedge()

_READER: Final = "reader"
_CANCELLER: Final = "canceller"


@dataclass
class _Init:
    """
    What the module remembers about the current ``sane_init``.

    Mutated, never rebound.  A later construction's host is compared against
    ``host``, and the warning names ``effective``, which differs whenever a
    non-empty ``SANE_NET_HOSTS`` was exported.

    Attributes:
        done: Whether ``sane.init()`` has returned successfully and not yet
            been undone by ``shutdown()``.
        host: The host argument that was in effect at that init.
        effective: The host list SANE's net backend will read, recorded at
            init; empty when there is none.
        written: The value saneless wrote into ``SANE_NET_HOSTS``, or ``None``
            when it wrote nothing.
        previous: What ``SANE_NET_HOSTS`` held before that write: ``None``
            when it was absent, ``""`` when it was exported empty.
        version: Whatever ``sane.init()`` returned, kept for the log line.

    """

    done: bool = False
    host: str = ""
    effective: str = ""
    written: str | None = None
    previous: str | None = None
    version: object = None


# "Look, then initialise" must not be split, or two racing constructions both
# call sane_init.  Not reentrant: shutdown() and _ensure_initialised() are
# called one after the other, never one inside the other.
_INIT_LOCK = threading.Lock()
_INIT = _Init()


@dataclass
class _OpenHandles:
    """
    How many device handles this process has open right now.

    On the net backend ``sane_exit`` closes each open handle with a request
    that waits for saned's reply, measured to outlast 40 seconds on a vanished
    host, so SANE is restarted only when this record holds no handle.  Handles
    are kept by identity, so closing one twice cannot count another as closed.

    It also enforces one cancel per timed-out read: on ``net`` each cancel is
    an unbounded request.  A cancelled handle skips the routine cancel before
    close, and its feeder iterator, whose ``__del__`` calls ``cancel()``, is
    parked until the handle is closed, when python-sane refuses that cancel.

    Attributes:
        handles: The ``id()`` of each handle opened by ``_open_device`` and
            not yet closed.
        cancelled: The ``id()`` of each open handle on which a cancel has
            already been issued.
        parked: The feeder iterator of a cancelled handle, by the handle's
            ``id()``, held until that handle is closed.

    """

    handles: set[int] = field(default_factory=set)
    cancelled: set[int] = field(default_factory=set)
    parked: dict[int, object] = field(default_factory=dict)


# Taken inside _WEDGE_LOCK, never the other way round, so the two cannot
# deadlock.
_HANDLES_LOCK = threading.Lock()
_OPEN_HANDLES = _OpenHandles()


def _handle_opened(dev: SaneDevice) -> None:
    """
    Record a handle ``sane_open`` has just returned.

    Args:
        dev: The handle.

    """
    with _HANDLES_LOCK:
        _OPEN_HANDLES.handles.add(id(dev))


def _handle_closed(dev: SaneDevice) -> None:
    """
    Record a handle as closed, whether or not its close succeeded.

    A parked iterator is dropped here, after the close and outside the lock,
    so its finaliser's cancel meets a closed handle and never reaches SANE.

    Args:
        dev: The handle.

    """
    with _HANDLES_LOCK:
        _OPEN_HANDLES.handles.discard(id(dev))
        _OPEN_HANDLES.cancelled.discard(id(dev))
        parked = _OPEN_HANDLES.parked.pop(id(dev), None)
    del parked


def _note_cancel_issued(dev: SaneDevice) -> None:
    """
    Record that a cancel has gone out on an open handle.

    A handle ``_open_device`` did not open is not recorded, as nothing would
    ever close it and clear the record.

    Args:
        dev: The handle the cancel was sent on.

    """
    with _HANDLES_LOCK:
        if id(dev) in _OPEN_HANDLES.handles:
            _OPEN_HANDLES.cancelled.add(id(dev))


def _cancel_was_issued(dev: SaneDevice) -> bool:
    """
    Report whether a cancel has already gone out on this handle.

    Args:
        dev: The handle.

    Returns:
        True if a cancel was issued on it since it was opened.

    """
    with _HANDLES_LOCK:
        return id(dev) in _OPEN_HANDLES.cancelled


def _park_iterator(dev: SaneDevice, iterator: object) -> bool:
    """
    Hold a cancelled handle's feeder iterator until the handle is closed.

    Dropping it earlier would send a second, unbounded cancel on the open
    handle from the worker thread.
    """
    with _HANDLES_LOCK:
        if id(dev) not in _OPEN_HANDLES.cancelled:
            return False
        _OPEN_HANDLES.parked[id(dev)] = iterator
        return True


def _handles_open() -> int:
    """
    Report how many device handles this process has open.

    Returns:
        The number of handles opened and not yet closed.

    """
    with _HANDLES_LOCK:
        return len(_OPEN_HANDLES.handles)


# Names no device and no host: a net: id is a LAN address.
_HANDLE_OPEN_REFUSAL: Final = (
    "Could not start a scan: a scanner handle from an earlier operation is "
    "still open, and SANE cannot be restarted safely while it is. Restart "
    "saneless if this does not clear."
)

# Thread names, so a stuck reader or cancel shows up in a faulthandler dump.
_READER_THREAD_PREFIX = "sane-read-"
_CANCEL_THREAD_NAME = "sane-cancel"

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


def _restore_sane_net_hosts() -> None:
    """
    Undo saneless's own ``SANE_NET_HOSTS`` write, and forget it either way.

    The caller holds ``_INIT_LOCK``.  The variable is put back only while it
    still holds the value saneless wrote; a value someone else wrote after
    init is left alone.
    """
    if _INIT.written is not None and os.environ.get(SANE_NET_HOSTS) == _INIT.written:
        if _INIT.previous is None:
            os.environ.pop(SANE_NET_HOSTS, None)
        else:
            os.environ[SANE_NET_HOSTS] = _INIT.previous
    _INIT.written = None
    _INIT.previous = None


def _ensure_initialised(host: str, *, log_level: int = logging.INFO) -> object:
    """
    Initialise SANE unless it already is, whoever asks.

    ``sane_init`` is process-global, so the guard is here, not in
    ``SaneBackend.__init__``: every caller still builds its own backend.

    A later construction naming a different host gets a WARNING: the net
    backend reads ``SANE_NET_HOSTS`` only when SANE initialises, so that host
    is not used until the next initialisation.

    A failed init records nothing, the environment included, so the next
    construction tries again.

    Args:
        host: Colon-separated sane-net hosts from the caller's configuration,
            or the empty string when none was configured.
        log_level: The level of the success lines; the warnings keep theirs.

    Returns:
        Whatever ``sane.init()`` returned for the current initialisation.

    Raises:
        ScanError: If ``sane.init()`` fails, chained to the SANE error.

    """
    with _INIT_LOCK:
        if _INIT.done:
            if host and host != _INIT.host:
                logger.warning(
                    "SANE is already initialised with scanner host %s, so the "
                    "host %s configured here is not used for this process's "
                    "own scanner opens until SANE is next initialised. "
                    "Run one saneless per scanner host, or list both hosts "
                    "colon-separated in one configuration",
                    _INIT.effective or "none",
                    host,
                )
            return _INIT.version
        # An exported non-empty value wins over the configured host; an
        # exported empty value counts as unset.
        exported = exported_sane_net_hosts()
        if host and not exported:
            _INIT.previous = os.environ.get(SANE_NET_HOSTS)
            os.environ[SANE_NET_HOSTS] = host
            _INIT.written = host
            logger.log(log_level, "SANE net host discovery configured: %s", host)
        elif host:
            logger.log(
                log_level,
                "SANE_NET_HOSTS already set externally (%s), ignoring scanner.host config",
                exported,
            )
        try:
            version = _ensure_sane().init()
        except Exception as exc:
            # python-sane raises _sane.error, RuntimeError or AttributeError,
            # with no shared base, so the boundary catches Exception.
            _restore_sane_net_hosts()
            init_msg = f"Could not initialise SANE: {describe(exc)}"
            raise ScanError(init_msg) from exc
        _INIT.done = True
        _INIT.host = host
        _INIT.effective = effective_sane_net_hosts(host)
        _INIT.version = version
        logger.log(log_level, "SANE initialized, version %s", version)
        return version


def _read_outstanding() -> bool:
    """
    Report whether any thread is still inside a SANE read or its cancel.

    A read being cancelled counts from the moment its cancel is decided on,
    because the wedge is recorded before the cancel fires.

    Returns:
        True if a read or cancel recorded in the wedge has not come back.

    """
    with _WEDGE_LOCK:
        return _WEDGE.stuck


def shutdown(*, log_level: int = logging.INFO) -> None:
    """
    Shut SANE down for this process, or explain why it was not.

    Called at an entry point's shutdown and by
    ``SaneBackend.reinitialise()``; never from an interpreter-exit hook, which
    would run while a daemon reader may still be inside ``sane_read``.
    Idempotent.

    The call is skipped, with a log line, when SANE was never initialised, or
    while a read has not returned: ``sane_exit`` closes every open handle
    holding the GIL, which is close-while-reading on all of them at once.

    A failing ``sane_exit`` is logged and swallowed, and the guard is re-armed
    either way, so a later ``SaneBackend`` initialises afresh.
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
            _ensure_sane().exit()
        except Exception:
            logger.warning("Could not shut SANE down", exc_info=True)
        _restore_sane_net_hosts()
        _INIT.done = False
        _INIT.host = ""
        _INIT.effective = ""
        _INIT.version = None
        logger.log(log_level, "SANE shut down")


def _page_label(page_num: int) -> str:
    """
    Name one page, for its timeout message and its reader thread.

    Args:
        page_num: Zero-based index of the page being acquired.

    Returns:
        The one-based human label, e.g. ``"Page 3"``.

    """
    return f"Page {page_num + 1}"


def _cancel_read(dev: SaneDevice, done: threading.Event) -> None:
    """
    Cancel a blocked read, on the thread ``_settle_or_wedge`` starts for it.

    On the ``net`` backend ``sane_cancel`` is a blocking RPC that hangs
    against a saned that stopped answering, so on the worker thread the grace
    would bound nothing.  It is sound off-thread because ``sane_cancel``
    releases the GIL, as ``sane_read`` does.

    A failing cancel is logged and swallowed; the read is already lost.
    Either way the thread gives up its wedge token (``_release_wedge``).
    """
    try:
        dev.cancel()
    except Exception:
        logger.warning("Cancelling the blocked read failed", exc_info=True)
    finally:
        _release_wedge(dev, done, _CANCELLER)


def _clear_wedge() -> None:
    """Forget the wedge record.  The caller holds ``_WEDGE_LOCK``."""
    _WEDGE.stuck = False
    _WEDGE.done = None
    _WEDGE.device = None
    _WEDGE.device_id = ""
    _WEDGE.page_label = ""
    _WEDGE.settling = False
    _WEDGE.outstanding.clear()


def _begin_settle(dev: SaneDevice, done: threading.Event, label: str) -> bool:
    """
    Record the wedge before the cancel fires, unless the read just returned.

    The check and the write are one critical section: the reader takes the
    same lock after setting ``done``, which closes the window in which it
    returns just after the timeout.
    """
    with _WEDGE_LOCK:
        if done.is_set():
            return False
        _WEDGE.stuck = True
        _WEDGE.done = done
        _WEDGE.device = dev
        _WEDGE.page_label = label
        _WEDGE.settling = True
        _WEDGE.outstanding.clear()
        _WEDGE.outstanding.update((_READER, _CANCELLER))
        return True


def _end_settle(
    done: threading.Event,
    canceller: threading.Thread | None,
    *,
    cancel_started: bool,
) -> bool:
    """
    Stop waiting: clear the wedge if nothing is left inside SANE, else keep it.

    The reader counts as finished once ``done`` is set.  The cancel thread
    counts as finished once it has given up its token, or if an interrupt
    stopped it from starting; ``cancel_started`` and ``is_alive()`` cover the
    two halves of an interrupted ``start()``.  True means the device context
    may close the handle as usual.
    """
    with _WEDGE_LOCK:
        if not (_WEDGE.stuck and _WEDGE.done is done):
            # Nothing was recorded: the read returned before the cancel.
            return True
        if done.is_set():
            _WEDGE.outstanding.discard(_READER)
        cancel_running = canceller is not None and canceller.is_alive()
        if not (cancel_started or cancel_running):
            _WEDGE.outstanding.discard(_CANCELLER)
        if not _WEDGE.outstanding:
            _clear_wedge()
            return True
        _WEDGE.settling = False
        return False


def _release_wedge(dev: SaneDevice, done: threading.Event, holder: str) -> None:
    """
    Give up one thread's token, and close the handle if it was the last.

    Only the last one out knows nothing is left inside SANE on the handle,
    which SANE requires before any other operation.  While the worker is
    still ``settling`` it owns the outcome, so nothing closes here.  A close
    failure is logged, never raised, as this runs on a daemon thread.

    Args:
        dev: The handle to release.
        done: The event identifying this acquisition; a thread belonging to
            some earlier, already-forgotten acquisition matches nothing here
            and does nothing.
        holder: Which token to give up: the reader's or the cancel thread's.

    """
    with _WEDGE_LOCK:
        if not (_WEDGE.stuck and _WEDGE.done is done):
            return
        _WEDGE.outstanding.discard(holder)
        if _WEDGE.settling or _WEDGE.outstanding:
            return
        logger.warning(
            "The %s on %s returned at last (%s); closing the handle",
            "read" if holder == _READER else "cancel",
            _WEDGE.device_id or "the scanner",
            _WEDGE.page_label,
        )
        try:
            dev.close()
        except Exception:
            logger.warning("Could not close the released scanner", exc_info=True)
        # Counted as closed either way, or every later SANE restart refuses.
        _handle_closed(dev)
        _clear_wedge()


def _name_wedged_device(dev: SaneDevice, device_id: str) -> bool:
    """
    Record which device is wedged, and report that it is.

    The device context knows the SANE name the acquisition helper lacks, so
    the later refusal can name the device.  Only the device id is recorded,
    never a host string.
    """
    with _WEDGE_LOCK:
        if _WEDGE.stuck and _WEDGE.device is dev:
            _WEDGE.device_id = device_id
            return True
        return False


def _refuse_if_wedged(device_id: str, operation: str) -> None:
    """
    Refuse a new SANE operation while a read is still outstanding.

    Called before anything is opened: ``sane_open`` on a device whose read
    never returned is itself forbidden.  The wedge clears itself when the late
    read returns, hence "if it does not" in the message.

    Raises:
        ScanError: If a reader is still inside SANE.

    """
    with _WEDGE_LOCK:
        if not _WEDGE.stuck:
            return
        wedged_id = _WEDGE.device_id or "the scanner"
        label = _WEDGE.page_label or "an earlier page"
    # Both ids can be LAN-supplied text bound for the terminal and log.
    shown = neutralise_controls(device_id)
    wedged_msg = (
        f"Could not {operation} {shown}: a read on "
        f"{neutralise_controls(wedged_id)} ({label}) "
        f"has not returned, and SANE allows no other operation on a device "
        f"while one is outstanding. The scan will be possible again as soon "
        f"as the scanner releases it. Restart saneless if it does not."
    )
    raise ScanError(wedged_msg)


def _settle_or_wedge(
    dev: SaneDevice, done: threading.Event, grace: float, label: str
) -> bool:
    """
    Run the cancel tail: record the wedge, cancel, wait out the grace, decide.

    The reader and the cancel thread share one grace.  A cancel still in
    flight when it runs out leaves the handle wedged even if the read
    returned: closing under a cancel is no safer than under a read.

    The wedge is recorded first and decided in a ``finally``, so an interrupt
    during the wait still ends it.  A signal landing between the record and
    the ``try`` leaves it settling forever, which refuses later scans but
    never closes a handle under a running call.

    Args:
        dev: The handle the blocked read is inside.
        done: The event identifying this acquisition.
        grace: Seconds to wait, after the cancel is fired, for the read to
            return and the cancel to come back.
        label: The page label, for the log and the later refusal.

    Returns:
        True if the read returned and the cancel, if one was sent, came back,
        so the caller's device context may close the handle normally.  False
        if not, in which case the handle is wedged and nothing may touch it.

    """
    if not _begin_settle(dev, done, label):
        return True
    began = time.monotonic()
    canceller: threading.Thread | None = None
    started = False
    try:
        deadline = began + grace
        canceller = threading.Thread(
            target=_cancel_read,
            args=(dev, done),
            name=_CANCEL_THREAD_NAME,
            daemon=True,
        )
        # Before the cancel goes out, so no later cleanup sends a second one.
        _note_cancel_issued(dev)
        canceller.start()
        started = True
        done.wait(_remaining(deadline))
        canceller.join(_remaining(deadline))
    finally:
        settled = _end_settle(done, canceller, cancel_started=started)
        if not settled:
            logger.critical(
                "%s: the scanner did not respond to the cancel within %.0fs. "
                "The device handle is being left open because SANE is still "
                "inside a call on it; no further scan can run until it returns.",
                label,
                time.monotonic() - began,
            )
    return settled


def _remaining(deadline: float) -> float:
    """
    Report how much of a wait is left.

    Args:
        deadline: The ``time.monotonic()`` reading the wait ends at.

    Returns:
        The seconds left, never negative.

    """
    return max(0.0, deadline - time.monotonic())


def _acquire_with_timeout(
    dev: SaneDevice,
    work: Callable[[], object],
    page_label: str,
    budget: _PageBudget,
) -> Image.Image:
    """
    Run one blocking SANE acquisition under a wall-clock bound.

    Each acquisition gets a fresh ``daemon=True`` thread: a non-daemon or
    pooled thread stuck in a blocking C call is joined at interpreter exit and
    stops the process from exiting at all.

    On timeout, or any exception once the reader may have started, the wedge
    is recorded, the read cancelled and both waited for (``_settle_or_wedge``).
    The late value is discarded unconditionally: measured on real libsane, a
    cancelled ``snap()`` hands back a truncated image rather than raising, and
    it passes ``_validate_page_image``.

    With no reader started there is nothing to settle; a cancel there would
    record a wedge no reader could ever clear.  A failed read waits for the
    backend's own threads before reporting (``_BACKEND_THREAD_EXIT_SECONDS``).

    Args:
        dev: The open handle the work will block inside.
        work: The blocking call, as a no-argument callable.
        page_label: Names the page in the timeout message and the thread.
        budget: How long to wait for the page and, after cancelling it, for
            the read; its description of the page goes in the timeout message.

    Returns:
        The acquired page.

    Raises:
        ScanError: If the page did not arrive within the timeout, naming the
            limit and the page it was for, and the unresponsive cancel as well
            when the read never came back.
        BaseException: Whatever the work raised, re-raised unchanged --
            including ``StopIteration``, by design, because that is the
            feeder-empty signal the caller's ladder is written around.

    """
    done = threading.Event()
    slot = _Slot()

    def read() -> None:
        before = _native_thread_ids()
        try:
            slot.value = work()
        except BaseException as exc:
            # Handed to the waiter: nothing may reach threading.excepthook.
            slot.error = exc
            # Before ``done`` is set, so nothing can cancel this failed read
            # while the backend's own reader is still running.
            _await_backend_threads(before)
        finally:
            done.set()
            _release_wedge(dev, done, _READER)

    # ``read`` holds ``work``, and through it the feeder iterator, until the
    # call returns; ``_acquire_pages`` drops its own reference after a wedge on
    # the strength of that, so the reader must never release it early.
    reader = threading.Thread(
        target=read, name=f"{_READER_THREAD_PREFIX}{page_label}", daemon=True
    )
    started = False
    try:
        reader.start()
        started = True
        finished = done.wait(budget.timeout)
    except BaseException:
        # A started reader is settled exactly as on a timeout, so the handle
        # is never closed under a running read; the exception is re-raised
        # unchanged.
        if started or reader.is_alive():
            _settle_or_wedge(dev, done, budget.grace, page_label)
        raise
    if finished:
        if slot.error is not None:
            raise slot.error
        return _as_image(slot.value)

    returned = _settle_or_wedge(dev, done, budget.grace, page_label)
    raise ScanError(
        page_timeout_error(page_label, budget.timeout, budget.page, returned=returned)
    )


@dataclass(frozen=True)
class _PageBudget:
    """
    How long one page may take, how long its cancel may, and how many a pass may.

    Both acquisition paths take the same record, so one sheet is bounded the
    same way whichever way it was presented; the flatbed path ignores the
    page cap.

    Attributes:
        timeout: Maximum seconds to wait for the sheet.
        grace: Maximum seconds to wait for a cancelled read to return.
        max_pages: The most sheets one feeder pass keeps. The sheet past it is
            fed, discarded and reported, never spooled.
        page: The page the timeout was worked out for, as the operator reads
            it in a timeout message, or ``None`` when it was not worked out
            from a page.

    """

    timeout: float = _PAGE_TIMEOUT_FLOOR_SECONDS
    grace: float = _CANCEL_GRACE_SECONDS
    max_pages: int = _MAX_ADF_PAGES
    page: str | None = None


_DEFAULT_PAGE_BUDGET = _PageBudget()


@dataclass(frozen=True)
class _FeedResult:
    """
    What one feeder pass produced.

    Attributes:
        records: The records the sink returned, in acquisition order.
        rejected: How many fed sheets were skipped for failing their integrity
            checks.
        sheet_not_kept: The number of the sheet fed past the page cap and
            discarded, or ``None`` when the feed ended on its own.

    """

    records: list[PageRecord]
    rejected: int
    sheet_not_kept: int | None


def _acquire_pages(
    dev: SaneDevice,
    sink: PageSink,
    framing: _PageFraming,
    budget: _PageBudget = _DEFAULT_PAGE_BUDGET,
) -> _FeedResult:
    """
    Spool the validated ADF pages, and report what was skipped or not kept.

    Pages flow one at a time to the sink; no list of images exists.

    python-sane's iterator turns exactly one message into ``StopIteration``,
    the only feeder-empty signal.  Every other exception is a real fault (a
    jam, an open cover, a busy device) and is reported as itself; do not infer
    "feeder empty" from a failure on the first page.

    An unreadable page is skipped and counted, but a pass in which every fed
    page was unreadable raises.  A skipped page may break manual-duplex
    parity; the pipeline reports that rather than hiding it.

    Args:
        dev: Open SANE device handle.
        sink: Where each accepted page goes, once, after validation and crop.
        framing: The crop applied to each accepted page and the dpi the sink
            records it at.
        budget: The per-page timeout, the cancel grace, and the most sheets
            the pass keeps.

    Returns:
        The records the sink returned, in acquisition order, how many fed
        sheets were skipped for failing their integrity checks, and the
        number of the sheet fed past the cap when there was one.

    Raises:
        FeederEmptyError: If the feeder produced no pages at all.
        ScanError: If a page times out, the device reports a fault, every
            sheet kept or skipped failed its integrity checks, or the sink
            could not take a page.

    """
    iterator = dev.multi_scan()

    records: list[PageRecord] = []
    page_num = 0
    # Kept apart from the pipeline's blank-page count: a corrupt page was not
    # removed for being blank.
    rejected_pages = 0
    sheet_not_kept: int | None = None
    try:
        while True:
            try:
                page_image = _acquire_with_timeout(
                    dev,
                    functools.partial(next, iterator),
                    _page_label(page_num),
                    budget,
                )
            except StopIteration:
                break
            except ScanError:
                raise
            except Exception as exc:
                scan_error_msg = (
                    f"Scanner error on page {page_num + 1}: {describe(exc)}"
                )
                raise ScanError(scan_error_msg) from exc

            # Detected on the sheet past the cap, since a full hopper only ends
            # when the next probe raises; that fed sheet is discarded uncounted.
            if page_num >= budget.max_pages:
                sheet_not_kept = page_num + 1
                logger.warning(
                    "Stopped the feed at the %d-page cap: sheet %d was fed "
                    "but not kept",
                    budget.max_pages,
                    sheet_not_kept,
                )
                break

            page_num += 1

            if not _validate_page_image(page_image, page_num):
                rejected_pages += 1
                continue

            records.append(sink.add(framing.crop(page_image), dpi=framing.resolution))
    finally:
        # After a cancel, the iterator's finaliser would send a second,
        # unbounded one, so it is parked until the close.  Otherwise dropping
        # it is safe: a wedged reader's callable keeps it alive.
        if not _park_iterator(dev, iterator):
            del iterator

    if page_num == 0:
        raise FeederEmptyError(_FEEDER_EMPTY_MESSAGE)

    # "Unreadable", not "load paper".
    if rejected_pages == page_num:
        all_rejected_msg = (
            f"All {page_num} page(s) fed were unreadable and were skipped "
            f"(zero dimensions, or below {_MIN_PAGE_BYTES} bytes of image "
            f"data); no usable page was produced"
        )
        raise ScanError(all_rejected_msg)

    return _FeedResult(records, rejected_pages, sheet_not_kept)


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


@dataclass(frozen=True)
class _ScanParameters:
    """
    The frame the device reports it is set up to scan, before any page starts.

    What python-sane's ``get_parameters()`` returns, with names, read after
    every option and the scan area are set.

    Attributes:
        frame_format: SANE's frame format, such as ``"gray"`` or ``"color"``.
        last_frame: Whether this is the last frame of the image.
        pixels_per_line: The width of the frame in pixels.
        lines: The height of the frame in lines, or -1 when the device does
            not know it in advance, as a feeder may not.
        depth: The bits per sample.
        bytes_per_line: The length of one line of image data in bytes.

    """

    frame_format: str
    last_frame: bool
    pixels_per_line: int
    lines: int
    depth: int
    bytes_per_line: int


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


def _page_budget_seconds(parameters: _ScanParameters, resolution: int) -> float:
    """
    Return how long one page may take, from the page the device will send.

    See the constants for the formula.  A three-pass colour scan counts three
    frames, and a negative line length counts as no data.
    """
    lines = parameters.lines
    if lines <= 0:
        # SANE reports -1 when the length is not known in advance.
        lines = math.ceil(resolution * _UNKNOWN_LENGTH_MM / _MM_PER_INCH)
    frames = 3 if parameters.frame_format in _THREE_PASS_FORMATS else 1
    page_bytes = max(parameters.bytes_per_line, 0) * lines * frames
    estimate = _REFERENCE_PAGE_SECONDS * page_bytes / _REFERENCE_PAGE_BYTES
    return min(
        _PAGE_TIMEOUT_CEILING_SECONDS,
        max(_PAGE_TIMEOUT_FLOOR_SECONDS, 2.0 * estimate),
    )


def _describe_page(parameters: _ScanParameters, resolution: int) -> str:
    """
    Describe the page a budget was worked out for, as a timeout message names it.

    Args:
        parameters: What the configured device reports.
        resolution: The resolution the device settled on.

    Returns:
        The page's description.

    """
    return scan_page_description(
        parameters.pixels_per_line,
        parameters.lines,
        colour=parameters.frame_format in _COLOUR_FORMATS,
        dpi=resolution,
    )


def _snap_flatbed(
    dev: SaneDevice,
    device_id: str,
    sink: PageSink,
    framing: _PageFraming,
    budget: _PageBudget = _DEFAULT_PAGE_BUDGET,
) -> PageRecord:
    """
    Acquire one flatbed page under the ADF's timeout, validate it, and spool it.

    ``start()`` and ``snap()`` run as one unit under the same page budget a
    fed sheet gets.  The message python-sane's ADF iterator treats as the end
    of the feed maps to ``FeederEmptyError``; every other failure is a
    ``ScanError`` naming the device.

    Raises:
        FeederEmptyError: If SANE reports the feeder out of documents.
        ScanError: If the sheet did not arrive within the timeout, if the page
            fails its integrity checks, or for any other failure, chained to
            the original.

    """

    def start_and_snap() -> Image.Image:
        dev.start()
        # No cancel from snap() on failure: the backend's reader thread may
        # still be running.  The device context cancels once it has ended
        # (``_BACKEND_THREAD_EXIT_SECONDS``).
        return dev.snap(no_cancel=True)

    try:
        image = _acquire_with_timeout(dev, start_and_snap, _page_label(0), budget)
    except ScanError:
        raise
    except Exception as exc:
        if str(exc) == "Document feeder out of documents":
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

    return sink.add(framing.crop(image), dpi=framing.resolution)


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


class SaneBackend(ScannerBackend):
    """
    Scanner backend wrapping python-sane.

    The first backend built in a process initialises SANE and later ones
    reuse it; the server also restarts SANE at the start of each scan job
    (``reinitialise``).  Scanners are listed, and the health check's open
    made, in a short-lived child process, never in this one.  Handles this
    process opens are cancelled and closed on every path unless a read on
    the handle has not returned.
    """

    def __init__(self, host: str = "") -> None:
        """
        Join this process's SANE, initialising it if nobody has yet.

        Args:
            host: Colon-separated sane-net hosts, applied at every
                initialisation this backend makes (construction and each
                ``reinitialise``) when ``SANE_NET_HOSTS`` is not already set to
                a non-empty value, and handed to every listing child.  A
                differing host arriving while SANE is already initialised is
                reported as not used until the next initialisation.

        Raises:
            ConfigError: If python-sane cannot be imported (``require_sane``).
            ScanError: If ``sane.init()`` fails, chained to the SANE error.

        """
        require_sane()
        # A listing child's SANE_NET_HOSTS is derived from this setting, never
        # copied from this process's environment.
        self._host = host
        _ensure_initialised(host)

    def close(self) -> None:
        """
        Shut this process's SANE down, through the backend abstraction.

        Process-level, not per-object: what is released is this process's
        current ``sane_init``.  Never raises, because an exception at shutdown
        would replace the error the operator needs to see.
        """
        shutdown()

    def reinitialise(self) -> None:
        """
        Restart this process's SANE before a scan job or a later pass, or refuse to.

        Called under the scanner gate with no handle open, at the top of each
        scan job and before each later pass of a multi-pass scan.  After a
        saned restart the net backend never reconnects its stale control
        connection, so every later open fails until SANE is restarted.

        It refuses, before any SANE call, while a read has not returned or a
        handle is open: ``sane_exit`` would close it, and on a vanished
        ``net`` host each close waits with the gate held.

        Raises:
            ScanError: If a read has not returned, if a handle is open, or if
                ``sane.init()`` fails (chained to the SANE error; SANE is then
                left uninitialised, so the next job tries again).

        """
        _refuse_if_wedged("the scanner", "start a scan on")
        if _handles_open():
            raise ScanError(_HANDLE_OPEN_REFUSAL)
        shutdown(log_level=logging.DEBUG)
        _ensure_initialised(self._host, log_level=logging.DEBUG)
        logger.info("SANE re-initialised before this scan")

    @contextlib.contextmanager
    def _open_device(self, device_id: str) -> Generator[SaneDevice]:
        """
        Context manager for SANE device lifecycle.

        Cancel and close run on every exit path unless a reader is still
        inside SANE on this handle: ``sane_close`` holds the GIL while
        ``sane_read`` has released it, which python-sane cannot survive.  The
        cancel is also skipped once one was issued (``_OpenHandles``).

        This process never lists before it opens.  That is measured only for
        a ``net:`` device; a discovery backend such as ``escl`` or ``airscan``
        that cannot open an unlisted id would fail here with SANE's error.

        Args:
            device_id: SANE device identifier string.

        Yields:
            An open SANE device handle.

        Raises:
            ScanError: If the device cannot be opened, naming it and chained to
                the SANE error.

        """
        try:
            dev: SaneDevice = _ensure_sane().open(device_id)
        except Exception as exc:
            # The id may be LAN-supplied text; defused where the message is
            # built, so every sink gets the safe spelling.
            open_msg = (
                f"Could not open scanner {neutralise_controls(device_id)}: "
                f"{describe(exc)}"
            )
            raise ScanError(open_msg) from exc
        _handle_opened(dev)
        try:
            yield dev
        finally:
            if _name_wedged_device(dev, device_id):
                # Still counted: the reader thread closes it, and counts it
                # closed, when the late read returns (_release_wedge).
                logger.critical(
                    "Leaving scanner %s open: a read has not returned, so "
                    "neither cancel() nor close() may be issued on it",
                    device_id,
                )
            else:
                if not _cancel_was_issued(dev):
                    with contextlib.suppress(Exception):
                        dev.cancel()
                try:
                    dev.close()
                except Exception:
                    # Never raised: it would replace the error that ended the
                    # scan.
                    logger.warning(
                        "Could not close scanner %s", device_id, exc_info=True
                    )
                finally:
                    # Counted closed either way, or every later SANE restart
                    # refuses.
                    _handle_closed(dev)

    def get_devices(self) -> list[DeviceInfo]:
        """
        List the available scanning devices, in a short-lived child process.

        See docs/explanation/decisions/0002-listing-in-a-child-process.md.
        It still refuses while a read is outstanding, before any child is
        started, to keep one rule for every SANE entry point.

        Returns:
            List of DeviceInfo objects for each discovered device.

        Raises:
            ListingCrashedError: The listing child died from a signal.
            ListingTimedOutError: The listing child did not finish in time.
            ListingNoAnswerError: The listing child gave no usable answer, or
                could not be started.
            ScanError: If a previous read has not returned, in which case no
                child is started; or if SANE could not list the devices, with
                its message normalised and its control characters escaped.

        """
        # The refusal names the wedged device from its own record.
        _refuse_if_wedged("the scanners", "list")
        reply = _launch_listing(ListingRequest(), configured_host=self._host)
        if reply.list_error is not None:
            # libsane's text can repeat what a LAN peer sent.
            reason = describe_text(reply.list_error.message, reply.list_error.type_name)
            list_msg = f"Could not list scanners: {neutralise_controls(reason)}"
            raise ScanError(list_msg)
        return list(_listed_devices(reply))

    def list_and_open(
        self, open_if_unlisted: str, *, abort: threading.Event | None = None
    ) -> DeviceSurvey:
        """
        List the devices and open an unlisted configured one, in one child.

        The Scanner health check's list-then-open, both in one child.  The
        child opens the configured id only when its listing lacks it, then
        closes it at once.  The survey carries exception class names only,
        since a ``net:`` id is a LAN address and SANE's text can repeat it.

        Args:
            open_if_unlisted: The configured device id, or ``""`` when none
                is configured.
            abort: Set by another thread to stop the child part way; the
                launcher then kills and reaps it on this thread.

        Returns:
            What the child's listing and open found.

        Raises:
            ListingCrashedError: The listing child died from a signal.
            ListingTimedOutError: The listing child did not finish in time.
            ListingNoAnswerError: The listing child gave no usable answer, or
                could not be started.
            ListingAbortedError: ``abort`` was set while the child ran.
            ScanError: If a previous read has not returned, in which case no
                child is started.

        """
        _refuse_if_wedged("the scanners", "list")
        reply = _launch_listing(
            ListingRequest(open=open_if_unlisted or None),
            configured_host=self._host,
            abort=abort,
        )
        return DeviceSurvey(
            devices=_listed_devices(reply),
            list_error=reply.list_error.type_name if reply.list_error else None,
            configured_opened=reply.opened,
            open_error=reply.open_error.type_name if reply.open_error else None,
        )

    def open_and_close(self, device_id: str) -> None:
        """
        Open a device and close it again, in a short-lived listing child.

        The base class's in-process default would log the device and the
        exception's text, so the open runs in a listing child instead.  A
        listed device is taken as reachable, as the health check takes it.

        Args:
            device_id: SANE device identifier string.

        Raises:
            ListingCrashedError: The listing child died from a signal.
            ListingTimedOutError: The listing child did not finish in time.
            ListingNoAnswerError: The listing child gave no usable answer, or
                could not be started.
            ScanError: If ``device_id`` is empty or a previous read has not
                returned, in which case no child is started; or if the open
                failed, naming the failure's class only.

        """
        if not device_id:
            # An empty id would list only and report success.
            msg = "No scanner was named to open"
            raise ScanError(msg)
        survey = self.list_and_open(device_id)
        if survey.configured_opened is False:
            reason = survey.open_error or "unknown error"
            msg = f"Could not open the scanner ({reason})"
            raise ScanError(msg)

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
                SANE call is made at all.

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
                # Option 0 is named '' and a group heading None; neither is an
                # option anyone can set.
                option_names=tuple(
                    str(opt[1]) for opt in raw_options if len(opt) >= 2 and opt[1]
                ),
                resolution_range=resolution.span,
            )

    def _scan_adf_pages(
        self,
        dev: SaneDevice,
        sink: PageSink,
        framing: _PageFraming,
        budget: _PageBudget = _DEFAULT_PAGE_BUDGET,
    ) -> _FeedResult:
        """
        Spool the validated ADF pages, with a per-page timeout.

        The timeout is per page, not per job, so a long stack is never cut
        short merely for being long.
        """
        return _acquire_pages(dev, sink, framing, budget)

    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Acquire pages from scanner, handing each one to the sink.

        Matches the requested source (refusing before anything is started
        when none matches), sets the scan parameters, and acquires through
        multi_scan() for a feeder pass or snap() for the flatbed.  No progress
        callback is passed to snap(): a bad one segfaults the process.

        Each page is validated, cropped and handed to ``sink`` as it arrives;
        no list of images is held.  See
        docs/explanation/decisions/0005-page-sink-contract.md.

        Args:
            device_id: SANE device identifier string.
            settings: Scan settings (source, resolution, mode).
            sink: Where each accepted page goes, exactly once per page, after
                validation and cropping.

        Returns:
            A ScanBatch carrying the records the sink returned, the resolution
            the device actually used, how many fed sheets failed their
            integrity checks, the requested source when the device's
            ``Auto`` stood in for it and fed, and the cap the pass stopped at
            when the feeder was still feeding there.  A capped pass keeps its
            pages and is not an error.

        Raises:
            ScanError: If a previous read has not returned, in which case no
                SANE call is made at all; if the requested source matches
                none the device offers, or several; if a page times out; if a
                flatbed scan returns a page that fails its integrity checks --
                unlike a fed sheet, there is no next page to skip to -- or if
                the sink could not take a page.
            FeederEmptyError: If the ADF feeder is empty.

        """
        _refuse_if_wedged(device_id, "scan from")
        with self._open_device(device_id) as dev:
            # Decisions after configuration use configured.options instead:
            # assigning the source or the mode reloads the descriptors.
            raw_options = _read_options(dev, device_id)

            choice = _resolve_source(
                raw_options,
                settings.source,
                resolve_feeder=settings.duplex == "manual",
            )

            configured = _configure_device(
                dev, settings, choice, options=raw_options, device_id=device_id
            )
            actual_resolution = configured.resolution

            # Before the paper size: whether the pass feeds decides how it
            # can be applied without cutting an edge off the page.
            use_adf = _route(choice, settings)

            framing = _apply_paper_size(
                dev,
                configured.options,
                settings,
                use_adf=use_adf,
                resolution=actual_resolution,
            )

            parameters = _read_parameters(dev, device_id)
            _refuse_sixteen_bit(parameters, device_id)

            # A source not named as a feeder (Auto sent through the feeder)
            # may be a platen rescanned forever, so it gets the lower cap.
            named_feeder = classify_source(choice.effective).uses_feeder
            budget = _PageBudget(
                timeout=_page_budget_seconds(parameters, actual_resolution),
                max_pages=_MAX_ADF_PAGES if named_feeder else _MAX_AUTO_FEEDER_PAGES,
                page=_describe_page(parameters, actual_resolution),
            )

            cap_reached: PassCapReached | None = None
            if use_adf:
                fed = self._scan_adf_pages(dev, sink, framing, budget)
                records, pages_rejected = fed.records, fed.rejected
                if fed.sheet_not_kept is not None:
                    cap_reached = PassCapReached(
                        cap=budget.max_pages,
                        sheet_not_kept=fed.sheet_not_kept,
                        auto_source=not named_feeder,
                    )
            else:
                records = [_snap_flatbed(dev, device_id, sink, framing, budget)]
                # Nothing was skipped: an unreadable sheet raised in there.
                pages_rejected = 0

        # Built after the device context has closed the handle.  An Auto that
        # stood in for a flatbed is reported only when it fed.
        return ScanBatch(
            pages=tuple(records),
            actual_resolution=actual_resolution,
            pages_rejected=pages_rejected,
            substituted_source=choice.substituted_from if use_adf else None,
            cap_reached=cap_reached,
        )
