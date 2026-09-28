"""
SANE scanner backend implementation wrapping python-sane.

This module provides the concrete SaneBackend that communicates with
physical scanners through the SANE (Scanner Access Now Easy) library.
Key safety measures:
- sane.init() runs behind a module-level guard: the first SaneBackend built
  initialises SANE and every later one reuses it, and after shutdown() a later
  init is allowed again.  The long-running server also re-initialises SANE at
  the start of each scan job (SaneBackend.reinitialise), because after a saned
  restart the net backend's stale control connection fails every later open
  until SANE is restarted.  Descriptor counts were measured flat over hundreds
  of exit/init cycles on both supported libsane builds, so the restart costs
  no descriptors.  It is refused while a read is stuck or any handle is open:
  sane_exit closes open handles with a request that waits for a reply, and on
  a host that has silently vanished that wait was measured to outlast 40
  seconds.
- Scanners are listed in a short-lived child process and never in this one,
  because a listing on a lost net control connection kills the process that
  makes it, and listing across repeated init/exit cycles corrupts memory in
  some local backends.
- Device handles managed via context manager with cancel+close, so an
  exception cannot skip the close and leave the scanner reporting "device
  busy" to the next scan; skipped entirely while a read is still inside SANE,
  because SANE allows no other call on a device while a read is outstanding
- No progress callbacks to snap(): the C extension does not validate them,
  and a wrong signature or a raising callback segfaults the process
- Source option validated against device capabilities
- Every blocking acquisition, fed or flatbed, runs on a daemon thread under
  one per-page timeout, with inline validation
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
    The import is repeated on each call rather than remembered, which costs a
    ``sys.modules`` lookup and keeps ``sys.modules`` the only record: a caller
    that evicts or blocks the module there sees that on the very next call.

    Returns:
        The patched module, or the imported python-sane.

    Raises:
        ImportError: If python-sane or its shared library cannot be loaded.

    """
    return sane if sane is not None else importlib.import_module("sane")


def require_sane() -> None:
    """
    Import python-sane, or fail at once with an install hint.

    This is the single python-sane availability check the SANE-using CLI
    commands run first.  A failure is a setup problem, not a scan
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


def _launch_listing(request: ListingRequest, *, configured_host: str) -> ListingReply:
    """
    Start one listing child and return its reply.

    This is the one place this module starts a listing child, and it is looked
    up at call time, so the test suite can replace it with an in-process
    stand-in that answers from a fake python-sane module.

    Args:
        request: What to ask the child for beyond the listing.
        configured_host: The ``scanner.host`` setting, possibly empty.

    Returns:
        The child's validated reply.

    """
    return run_listing_child(request, configured_host=configured_host)


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


# The per-page timeout scales with the page the device agreed to send.
#
# The reference page is A4 at 600 dpi in 8-bit colour, 4961 x 7016 pixels of
# three bytes each, and a generous estimate for it is one minute. A page is
# given twice its share of that minute, by bytes, so a page twice the size gets
# twice the time: A4 colour at 1200 dpi gets 480 s, A4 grey at 1200 dpi 160 s.
# The factor of two leaves room for a slow USB or Wi-Fi link without letting a
# dead one hold a job for long.
#
# The floor keeps every page at two minutes or more, the fixed limit this
# replaced, so anything at 300 dpi, where a page typically takes 10-15 s, is
# bounded exactly as before.
#
# The ceiling keeps every page at an hour or less. The parameters are numbers
# the device reports, over the LAN for a `net` scanner, and nothing else bounds
# them: a frame claiming 2**31-1 bytes a line would otherwise earn a budget of
# months, or one too large for `threading.Event.wait` to accept at all. An
# hour is six times the budget of a legal colour page at 1200 dpi and more
# than a 2400 dpi A4 colour page needs.
#
# The inputs are the parameters the device reports once every option and the
# scan area are set -- the page it will actually send, not the one asked for.
# A device that does not know the length in advance, as a feeder may not, is
# budgeted for a legal sheet, the longest paper size a profile can name.
#
# One budget bounds one page on BOTH acquisition paths: one fed sheet's
# next(iterator) and one flatbed sheet's start()+snap() alike. There is no
# config key: the device's parameters already say how big the page is.
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
#   operator, and the resulting wedge already recovers by itself the moment
#   the read does come back.
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
# only way a size floor can be safe.
_MIN_PAGE_BYTES: int = 10_000  # 10 KB

# Upper bound on the number of pages one scan_pages() call will acquire.
#
# WHAT IT BOUNDS: python-sane's _SaneIterator.__next__ stops only on the exact
# string "Document feeder out of documents". On hardware that is not a feeder,
# start()/snap() keep succeeding, so the iterator re-scans the platen and the
# loop never terminates on its own -- reproduced live against a Flatbed source.
# The per-page timeout is no defence: a scan that succeeds satisfies it every
# iteration. This cap is what stops that loop for a source named as a feeder;
# _MAX_AUTO_FEEDER_PAGES below stops it much sooner for one that is not.
#
# Reaching it is not a failure. The sheet past the cap -- the only place the
# overrun can be seen -- is discarded, the pages already kept are returned, and
# the batch records which sheet was fed but not kept, so the operator knows
# where to resume.
#
# WHAT IT DOES NOT BOUND: memory. At A4 300 dpi colour a page is roughly 26 MB,
# so at 500 pages a backend that accumulated them would hold roughly 13 GB.
# This cap must not be described as a memory bound, because it is not one: the
# bound is that a page is handed to the sink and forgotten as it arrives, so
# peak memory is a small constant number of decoded pages whatever this cap is.
# The two are independent, and raising this one is not a memory decision.
#
# WHAT IT STILL BOUNDS, INDIRECTLY: spool disk. Every page this loop acquires
# is written to the job's spool, so the page count fixed here is also the
# ceiling on how much of the workspace one runaway pass can consume. It is a
# ceiling, not the check: SpooledPageSink._check_room_for refuses each page
# individually, against that page's decoded size plus the operator's
# min_free_space_mb reserve, and that is what actually protects the disk.
# This cap's contribution is only that the loop cannot keep asking
# forever while that check does its work.
#
# SCOPE: the cap is per scan_pages() call, so it is a per-pass cap, not a
# per-job one: every pass of a job calls scan_pages() separately.
#
# The value is base.MAX_PAGES_PER_PASS, not a second literal: the scope and the
# hopper-size judgement behind the number are recorded there, and a document
# cap that has to be read together with this one is paired with that constant
# rather than with a copy of it.
_MAX_ADF_PAGES: int = MAX_PAGES_PER_PASS

# The per-pass bound for a source sent through the feeder that does not
# classify as a feeder: in practice Auto with auto_source_mode = "adf", whether
# the profile asked for Auto or Auto stood in for a flatbed request.
#
# Such a source may be a platen rescanned as a feeder. Measured on the SANE
# test backend, a Flatbed source driven through multi_scan() never reports the
# end of its feed, so the only thing that ends the pass is a cap, and at
# _MAX_ADF_PAGES that is hours of a scanner rescanning one sheet. Fifty bounds
# it to minutes while still covering any plausible stack fed through Auto.
#
# It is a module constant beside _MAX_ADF_PAGES and deliberately not a config
# key. Stopping after a run of identical pages was considered and rejected: it
# needs fuzzy image comparison, and a stack of identical forms would trip it.
_MAX_AUTO_FEEDER_PAGES: Final = 50

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

# SANE's value-type codes for an integer and a fixed-point option, read at
# index 4 of the tuple get_options() reports.  python-sane refuses an int for a
# fixed-point option and a float for an integer one, even a whole float, so a
# value has to be written in the option's own type.
_SANE_TYPE_INT = 1
_SANE_TYPE_FIXED = 2

# The options a feeder that centres the sheet uses to learn the paper size, in
# the hyphenated spelling get_options() reports.  The fujitsu and canon_dr
# backends offer them, active only while a feeder source is selected; told the
# paper size, the device places its scan window over the middle of the feed
# path, and the scan area is then measured from that window's corner.
_PAGE_SIZE_OPTIONS: tuple[str, str] = ("page-width", "page-height")

# The capability bits, at index 7, that say whether software may set an option
# right now: it must be software-selectable and not inactive.
_SANE_CAP_SOFT_SELECT = 1
_SANE_CAP_INACTIVE = 32

# The bits per sample saneless scans at, and the depth it refuses.  Its pages
# are 8-bit, and python-sane misreads a 16-bit frame: the image comes back
# twice as tall, read past the end of its buffer.  python-sane itself refuses
# every depth other than 1, 8 and 16, so 16 is the only one to refuse here.
_EIGHT_BITS = 8
_SIXTEEN_BITS = 16

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
    never raises: a malformed constraint must not take down a capability
    query.

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
    over. A third copy would give that defect a third life.

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

    Args:
        raw_options: The device's option tuples, as ``get_options()`` returns
            them.
        name: The option name, in the hyphenated spelling ``get_options()``
            uses.

    Returns:
        The option's nine-element tuple, or None if the device does not report
        it or reports it too short to read.

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

    An inactive option refuses a value with ``AttributeError``, which the
    configuring code would report as a failed scan, so an option a device
    lists but has switched off -- ``depth`` in a line-art mode, say -- is one
    to leave alone rather than to write.

    Args:
        raw_options: The device's option tuples.
        name: The hyphenated option name.

    Returns:
        True if the option is reported, software-selectable and active.

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

    Args:
        found: What the device reports for ``depth``.

    Returns:
        True if 8 is a value the device accepts as it is.

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

    A record rather than a tuple partly because ``_configure_device`` takes it
    whole: passing its fields one by one would put that function over ruff's
    five-parameter limit. It also keeps the three facts from being confused
    with each other at the one call site that unpacks them.

    Attributes:
        effective: The name to assign to the device: the device's own
            spelling of a matched entry, the device's ``Auto`` entry standing
            in for a flatbed, the trimmed request when the device's list
            cannot be read, or the request as given when the device has no
            source option at all.
        has_option: Whether the device reports a ``source`` option. It is a
            different fact from whether its constraint could be read; see
            :class:`_OptionConstraint`.
        substituted_from: The source the profile asked for when the device's
            ``Auto`` entry was chosen in its place, otherwise None. Whether
            that substitution matters depends on routing, which is decided
            later, so this only records that it happened.

    """

    effective: str
    has_option: bool
    substituted_from: str | None


def _match_source(available: list[str], requested: str) -> str | None:
    """
    Find the device's entry a profile's source names, ignoring case and padding.

    An entry equal to ``requested`` wins outright. Otherwise the entries are
    compared with surrounding whitespace removed and case folded on both
    sides, and a single such match is returned in the device's spelling:
    libsane strips nothing, so the name assigned has to be the one it listed.

    Nothing is matched by prefix, although libsane itself would take a unique
    prefix: ``"ADF"`` is a prefix of ``"ADF Duplex"`` as well, and a request
    that names neither exactly is a guess saneless will not make on the
    operator's behalf. For the same reason, two entries that differ only in
    case refuse rather than one being picked.

    Args:
        available: The source names the device reports, in its order.
        requested: The source the profile asked for.

    Returns:
        The device's entry, or None if no entry matches.

    Raises:
        ScanError: If more than one entry matches once case and surrounding
            whitespace are ignored.

    """
    if requested in available:
        return requested
    folded = requested.strip().casefold()
    matches = [entry for entry in available if entry.strip().casefold() == folded]
    if len(matches) > 1:
        # The entries are device-supplied text, and this message reaches the
        # terminal, the log and the job's error as it is built.
        ambiguous_msg = ambiguous_source_error(
            neutralise_controls(requested),
            [neutralise_controls(entry) for entry in matches],
        )
        raise ScanError(ambiguous_msg)
    return matches[0] if matches else None


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
            a resolution the device silently refused would reintroduce the
            silent resolution substitution one layer further down.

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


def _option_unit(
    raw_options: list[tuple], name: str, *, fallback: str
) -> GeometryUnit | None:
    """
    Read the unit the device reports for one option, index 5 of its tuple.

    The conversion is defensive because the tuple is device-supplied.  A
    conforming backend cannot report a code outside the seven, but nothing in
    the protocol stops a broken one, and scaling by a garbage factor would
    silently mis-size the page.

    Args:
        raw_options: The device's option tuples.
        name: The hyphenated option name.
        fallback: What the scan does instead, for the WARNING an unknown code
            logs, e.g. ``"will crop after scanning"``.

    Returns:
        The reported unit, or None if the device does not report the option or
        reports something that is not a SANE unit at all.

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

    The unit lives at index 5 of the option tuple, which the geometry
    arithmetic used to ignore entirely, assuming millimetres.

    ``br-x`` is the option read: the four scan-area options describe one box,
    and a device reports one unit for all of them.

    Args:
        raw_options: The device's option tuples.

    Returns:
        The reported unit, or None if the device reported something that is not
        a SANE unit at all.

    """
    return _option_unit(raw_options, "br-x", fallback="will crop after scanning")


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
    Check that the device kept the scan area it was given.

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


def _in_option_type(raw_options: list[tuple], name: str, value: float) -> int | float:
    """
    Express a number in an option's own SANE type.

    Args:
        raw_options: The device's option tuples.
        name: The hyphenated option name.
        value: The number to write, in the option's unit.

    Returns:
        ``round(value)`` for an integer option, which refuses a float, and
        ``float(value)`` otherwise, since a fixed-point option refuses an int.

    """
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

    **The presence check is what makes the crop fallback reachable, and an
    exception handler is not a substitute for it.**  ``SaneDev.__setattr__``
    stores an unrecognised option name straight into ``__dict__`` and returns --
    no device call, no validation, no raise (``sane.py:188``).  So on a scanner
    with no scan-area options, ``dev.br_y = 297.0`` *succeeds*, this function
    used to return True, and ``_maybe_crop`` never ran: ``paper_size`` was
    silently ignored and the user got a full-bed scan.  Asking the
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
    # Each corner in its option's own type: a scan area in pixels is an
    # integer option on a real device, and python-sane refuses a float there.
    # The box compared below is the one written, rounding and all, so the
    # read-back is checked in the same unit and type it was set in.
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
        # Name what was swallowed.  A device that reports the options and then
        # refuses them is a different fault from one that never had them, and
        # a bare "does not support geometry" hid which had happened.
        logger.warning(
            "Scanner rejected the scan-area options (%s), will crop after scanning",
            exc,
        )
        return False
    # A device can accept all four assignments and still quietly shrink the
    # area, so what it reports back is what decides.
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
        unit = _option_unit(raw_options, name, fallback="will scan the full window")
        scale = None if unit is None else _units_per_mm(unit, resolution)
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

    **Where the sheet sits is what decides.**  On the flatbed it sits in the
    top-left corner, so the scan area is set from that corner, and when the
    device will not take the area the page is cropped from the same corner.
    In a feeder it sits wherever the feeder guides it: against one side on
    some, in the middle on others.  A box measured from the top-left corner
    cuts the right edge off every page of a feeder that centres the sheet, and
    saneless has no way to see which kind it has.

    So on a pass through the feeder:

    - A device that reports ``page-width`` and ``page-height``, active and
      settable for the source selected, is told the paper size first.  It
      then places its own window over the sheet, and the scan area is set
      inside that window, from its corner, exactly as on the flatbed; a crop
      from that corner is equally safe if the area is refused.
    - A device that does not is left alone: no scan area and no crop.  The
      paper size is **not applied** and the page is the full window, larger
      but complete.  That is one INFO line, not a warning, because nothing is
      lost.

    It is the routing that makes a pass a feeder pass, not the source's name:
    an ``Auto`` source sent through the feeder has the same unknown
    registration as a named feeder.

    Args:
        dev: Open SANE device handle.
        options: The option list read after the source was assigned, which is
            the only one that says whether the page-size options are active.
        settings: The requested scan settings, for ``paper_size``.
        use_adf: Whether this pass reads from the feeder.
        resolution: The resolution the device actually reported, in dpi.

    Returns:
        The framing every page is cropped to and laid out at.  A paper size
        that was not applied is framed as ``"full"``, the whole window.

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

    The crop is measured from the page's top-left corner, which is right only
    where the sheet's corner is the window's: on the flatbed, and in a feeder
    window the device has centred on the paper itself.  A paper size that was
    not applied reaches here as ``"full"``, so a feeder page whose placement is
    unknown is never cropped.

    Args:
        image: Scanned page image.
        paper_size: Paper size key (e.g. ``"a4"``), or ``"full"`` for none.
        resolution: The resolution the device actually reported, in dpi.
            Deliberately not the requested one: the crop arithmetic and the
            device's real sampling rate have to agree, or a silently clamped
            resolution yields a cut-off page even when this fallback runs
            exactly as intended.
        geometry_set: Whether the scan area was already set on the device.

    Returns:
        The cropped image, or the original if no crop is needed.

    """
    if paper_size != "full" and not geometry_set:
        return crop_to_paper_size(image, paper_size, resolution)
    return image


@dataclass(frozen=True)
class _PageFraming:
    """
    What every accepted page of one scan is cropped to and laid out at.

    Both acquisition paths hand each page to the sink as
    ``sink.add(framing.crop(page), dpi=framing.resolution)``. The crop and the
    dpi are one fact seen twice -- the resolution the device read back -- so
    they travel as one record: a crop closure alongside a separate ``dpi``
    parameter would push ``_acquire_pages`` and ``_snap_flatbed`` past ruff's
    ``PLR0913`` limit, and would let the two disagree.

    Attributes:
        paper_size: The paper size key the pages are framed to, e.g. ``"a4"``,
            or ``"full"`` for the whole window: either because none was asked
            for, or because it was not applied, on a feeder that cannot say
            where it places the sheet.
        resolution: The resolution the device actually reported, in dpi. The
            crop arithmetic uses it, and every PDF lays the page out at it.
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

    Two integrity checks, and only two: nonzero dimensions, and a raw byte
    count at or above ``_MIN_PAGE_BYTES``. Both answer "did the device hand
    back something decodable?". Neither looks at what is printed on the page.

    That byte count is arithmetic over the page's dimensions and band count,
    never a materialised copy of its raw buffer: measuring the length of one
    copied a whole 26 MB page to learn a number that width, height and band
    count already give. The product is exact for
    ``L`` and ``RGB``, which are the only two modes a SANE ``snap()`` produces
    on either path here. It would be eight times too large for mode ``"1"``,
    where Pillow packs eight pixels into a byte, and a bilevel page would
    therefore clear the ``_MIN_PAGE_BYTES`` floor on eight times less data
    than it looks like. That is recorded rather than handled because nothing
    in this project produces mode ``"1"``; a path that starts to must revisit
    the floor rather than this formula.

    Blank-page policy deliberately does not live here. The profile exposes
    ``enable_empty_page_detection`` along with a user-visible ink-coverage
    threshold, so a page discarded at this level would be discarded behind the
    user's back and would make that toggle untrue -- which is precisely how
    real pages used to vanish, and what broke manual-duplex parity. Content is
    judged in exactly one place, ``pipeline._drop_blank_pages``.

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
    What the module remembers about a reader thread still inside SANE.

    Module-level and **mutated, never rebound**.  Rebinding would need a
    ``global`` statement, which the ``PL`` rules in ruff's ``select`` reject
    and which this project does not suppress, so this state lives in a
    container instead of in a name.

    ``device`` and ``iterator`` are **strong** references, deliberately.
    ``SaneDev_dealloc`` calls ``sane_close()`` and ``_SaneIterator.__del__``
    calls ``device.cancel()``, so letting a wedged handle be garbage-collected
    would reintroduce exactly the close-while-reading this module now avoids.

    ``done`` identifies *which* acquisition is wedged.  A reader that wakes up
    long afterwards compares against it, so a late wake-up belonging to an
    abandoned acquisition cannot clear a wedge that a later one recorded.

    The record is written **before** a timed-out read is cancelled, not after
    the grace runs out.  An interrupt landing while the worker waits out the
    grace then cannot skip the write, and the handle is never closed under a
    read that is still running.  ``settling`` and ``outstanding`` say who may
    finish the job.  Every access holds ``_WEDGE_LOCK``.

    - ``settling`` is True from the moment the record is written until the
      worker stops waiting (``_settle_or_wedge``).  The worker sets it, and
      the worker clears it.  While it is True the worker owns the outcome, so
      neither the reader nor the cancel thread closes the handle, even as the
      last to finish.  When the worker stops waiting it either clears the
      whole record, because both have finished and the device context may
      close the handle as usual, or sets ``settling`` to False, which makes
      this a real wedge.
    - ``outstanding`` holds a token for each thread still inside SANE on the
      handle: the reader's and the cancel thread's.  The worker writes both
      with the record, and each thread removes its own as it finishes
      (``_release_wedge``).  Once ``settling`` is False, the thread that
      removes the last token closes the handle and clears the record.  It
      has to be the last one, because a close while SANE is still inside a
      cancel on the handle is as unsafe as a close under a read: on ``net``
      the cancel is a request that may be slow to come back.
    """

    stuck: bool = False
    done: threading.Event | None = None
    device: SaneDevice | None = None
    iterator: object = None
    device_id: str = ""
    page_label: str = ""
    settling: bool = False
    outstanding: set[str] = field(default_factory=set)


# Guards every read and write of _WEDGE.  Three threads reach it -- the worker
# that gave up, the reader that eventually returns, and whichever thread starts
# the next job -- and the close-or-wedge decision is a check and a write that
# must not be split.
_WEDGE_LOCK = threading.Lock()
_WEDGE = _Wedge()

# The two tokens ``_Wedge.outstanding`` holds: one for the thread reading the
# page and one for the thread cancelling it.
_READER: Final = "reader"
_CANCELLER: Final = "canceller"


@dataclass
class _Init:
    """
    What the module remembers about the current ``sane_init``.

    Module-level and **mutated, never rebound**, for the reason ``_Wedge``
    gives: rebinding a module-level name needs a ``global`` statement, which
    the ``PL`` rules in ruff's ``select`` reject, and CLAUDE.md forbids adding
    a second suppression to say otherwise.  Keeping it beside ``_WEDGE`` also
    keeps this module's process-global state in one place rather than two.

    ``host`` is the configured ``host`` argument the initialising construction
    passed, and it is what a later construction is compared against: what the
    comparison has to catch is a second operator-configured host arriving
    while SANE is already initialised with the first.  ``effective`` is the
    host list SANE's net backend will
    actually read, which differs from ``host`` whenever a non-empty
    ``SANE_NET_HOSTS`` was exported, and it is what that comparison's warning
    names, so the operator is pointed at the host SANE is really using.

    ``written`` and ``previous`` let ``shutdown()`` undo saneless's own write,
    so a later init writes its own host rather than finding the old one and
    reporting it as set externally.

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


# Guards every read and write of _INIT.  Three threads reach it in production
# -- whichever builds the backend, whichever shuts it down, and the worker that
# re-initialises SANE at the start of a scan job -- and "look, then initialise"
# is a check and a write that must not be split, or a racing pair of
# constructions would each see an uninitialised SANE and call sane_init twice.
# It is not reentrant, so shutdown() and _ensure_initialised() are called one
# after the other and never one inside the other.
_INIT_LOCK = threading.Lock()
_INIT = _Init()


@dataclass
class _OpenHandles:
    """
    How many device handles this process has open right now.

    ``sane_exit`` closes every handle that is still open, and on the net
    backend each close is a request that waits for saned's reply
    (``backend/net.c``: ``sane_exit`` calls ``sane_close`` on each one).  On a
    host that has silently vanished that wait was measured to last longer than
    40 seconds on both supported libsane builds.  So SANE is only restarted
    when no handle is open, and this record is what makes "no handle is open"
    something ``SaneBackend.reinitialise()`` can check rather than assume.

    Handles are kept by identity rather than as a bare count, so closing one
    handle twice cannot also count a different, still-open handle as closed.
    A handle's identity is stable while it is recorded, because whoever
    closes it holds a reference to it until then.

    Module-level and **mutated, never rebound**, for the reason ``_Wedge``
    gives.  A handle left open by a read that never returned stays recorded
    until the reader thread closes it, because until then it is exactly the
    kind of open handle ``sane_exit`` would try to close.

    It also enforces one cancel per timed-out read.  Once the timeout's own
    cancel has gone out on a handle, two more used to follow it: the feeder
    iterator's finaliser (``_SaneIterator.__del__`` calls ``cancel()``) when
    the iterator was dropped, and the routine cancel before close.  On the
    ``net`` backend each is a request to a host that may have stopped
    answering, and both ran on the worker thread with no bound.  So a handle
    on which a cancel was issued is recorded in ``cancelled``; the device
    context skips its routine cancel for it, and the feeder's iterator is
    parked here instead of dropped.  ``_handle_closed`` drops a parked
    iterator only once the handle is closed, when its finaliser's cancel is
    refused by python-sane ("SaneDev object is closed") before it reaches
    SANE, and swallowed.

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


# Guards every read and write of _OPEN_HANDLES.  It is taken inside
# _WEDGE_LOCK by the reader that closes a stuck handle late, and never the
# other way round, so the two cannot deadlock.
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

    Its cancel record goes with it, and so does any iterator parked for it,
    which is dropped here, after the close and outside the lock: its
    finaliser's cancel then meets a closed handle and never reaches SANE.

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

    Dropping it any earlier would run its finaliser's cancel on an open
    handle: a second cancel after the one already issued, and on the worker
    thread, where nothing bounds it.

    Args:
        dev: The handle the iterator drives.
        iterator: The ``multi_scan()`` iterator.

    Returns:
        True if the iterator was parked because a cancel was issued.

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


# What a scan job is told when SANE cannot be restarted because a handle is
# open.  It names no device and no host: the count does not know which device
# the handle belongs to, and a net: id is a LAN address (ASVS V7).
_HANDLE_OPEN_REFUSAL: Final = (
    "Could not start a scan: a scanner handle from an earlier operation is "
    "still open, and SANE cannot be restarted safely while it is. Restart "
    "saneless if this does not clear."
)

# The prefix every acquisition thread is named with, so a stuck reader is
# identifiable in a ``faulthandler`` dump or a debugger without guessing.
_READER_THREAD_PREFIX = "sane-read-"

# The name of the thread that cancels a timed-out read, for the same reason:
# a cancel the scanner is slow to answer leaves it running past the grace.
_CANCEL_THREAD_NAME = "sane-cancel"


def _restore_sane_net_hosts() -> None:
    """
    Undo saneless's own ``SANE_NET_HOSTS`` write, and forget it either way.

    The caller holds ``_INIT_LOCK``.  The variable is put back only while it
    still holds the value saneless wrote: absent again when it was absent,
    empty again when it was exported empty.  A value someone else wrote after
    init is theirs and is left alone.
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

    ``sane_init`` is a process-global call, not a per-object one, so the
    guard is here rather than in ``SaneBackend.__init__``: three one-shot CLI
    commands and the web server each build their own backend, and a second
    ``sane_init`` without a ``sane_exit`` between them is at best wasted work.
    It is a guard and not a singleton deliberately -- every one of those
    callers keeps getting its own ``SaneBackend``, which is what lets the tests
    and the CLI construct one wherever they need it.  The per-job restart,
    ``SaneBackend.reinitialise()``, calls ``shutdown()`` first, so it passes
    this guard with SANE uninitialised.

    A later construction naming a *different* host is the case worth a WARNING
    rather than silence.  The net backend reads ``SANE_NET_HOSTS`` when SANE
    initialises it (``backend/net.c`` ``sane_init``, which the dll backend
    calls lazily), so a host configured while SANE is already initialised is
    not used for this process's own opens until the next initialisation, while
    the operator who configured it has every reason to believe it is in
    effect.  Listings are held to the same host list: a listing child's
    ``SANE_NET_HOSTS`` comes from ``effective_sane_net_hosts``, which prefers
    an exported value, and the host this function exported at init is one, so
    the later backend's listings dial it too.  The warning names the host list
    that was in effect at init, which is what SANE is using.  Only host names from the operator's
    own configuration or environment are named, which the existing INFO line
    already logs; no credential is in scope here (ASVS V7).

    ``log_level`` is the level of the success lines.  A construction logs them
    at INFO; the per-job restart passes DEBUG and logs one line of its own, so
    each scan job adds one INFO line rather than three.

    The failure translation is ``ScanError`` because a SANE that will not start
    is a scanning failure the caller reports, and it catches ``Exception``
    because python-sane raises ``_sane.error``, ``RuntimeError`` or
    ``AttributeError`` with no shared base.  A failed init records
    nothing, the environment included, so the next construction tries again
    rather than assuming an initialised SANE that is not there, and does not
    mistake this attempt's host for one set externally.

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
        # SANE_NET_HOSTS tells the sane-net backend which hosts to probe for
        # scanners.  Multiple hosts are separated by colons — see sane-net(5).
        # An exported non-empty value wins over the configured host; an
        # exported empty value names no host and counts as unset.
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

    The shutdown path has no handle to ask about, so it needs this
    process-wide answer rather than one about a single device.  A read being
    cancelled counts from the moment its cancel is decided on, because the
    wedge is recorded before the cancel fires.

    Returns:
        True if a read or cancel recorded in the wedge has not come back.

    """
    with _WEDGE_LOCK:
        return _WEDGE.stuck


def shutdown(*, log_level: int = logging.INFO) -> None:
    """
    Shut SANE down for this process, or explain why it was not.

    Called at an entry point's shutdown, and by ``SaneBackend.reinitialise()``
    at the start of a scan job, under the scanner gate and only once it has
    checked that no handle is open.  Never from a request path, and never from
    an interpreter-exit hook, which would run while a daemon reader thread may
    still be inside ``sane_read``.  It is idempotent, so an entry point that
    closes more than one backend calls ``sane_exit`` once.

    ``log_level`` is the level of the "SANE shut down" line: INFO at an entry
    point's shutdown, DEBUG on the per-job restart, which logs its own line.
    The skip lines keep their levels.

    Two conditions skip the call rather than making it, and both are logged
    because a silently skipped shutdown is indistinguishable from one that
    happened:

    - **SANE was never initialised in this process.** There is nothing to undo,
      and ``sane_exit`` before ``sane_init`` is undefined by the standard.
    - **A read has not returned.** ``sane_exit`` closes every open handle by
      specification, and ``PySane_exit`` runs holding the GIL while
      ``sane_read`` has released it -- exactly the close-while-reading sequence
      ``_open_device`` already refuses on one handle, applied to
      all of them at once.  At an entry point's shutdown the process is ending
      anyway, so an un-exited SANE costs nothing next to a segfault on the way
      out.  The per-job restart refuses before it gets here, so on that path
      this check is a second guard, not the first.

    Nothing escapes: a failing ``sane_exit`` is logged with its traceback and
    swallowed, because at an entry point's shutdown the process is on its way
    out and an exception here would replace whatever error the operator is
    being shown.  On the per-job restart the ``sane_init`` that follows is what
    reports a SANE that a failed exit left broken.
    The guard is re-armed either way, so a later ``SaneBackend`` initialises
    rather than assuming a SANE that a failed exit may well have left broken.

    ``SANE_NET_HOSTS`` is put back as it was before init when it still holds
    the value saneless wrote, so a later init writes its own host rather than
    finding this one and reporting it as set externally.  The two early
    returns leave it alone, because SANE is still initialised there.
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

    The cancel runs on a thread of its own and not on the worker's, and that
    is a correctness requirement rather than tidiness.  On the ``net`` backend
    ``sane_cancel`` is ``sanei_w_call(SANE_NET_CANCEL)`` -- a blocking RPC on
    the control wire, issued before the local data fd is closed -- so against a
    saned that has stopped answering it hangs whoever calls it.  Whoever calls
    it here would be the worker thread running the whole job, so the grace
    would bound nothing at all (``backend/net.c``).

    It is sound to call at all only because ``sane_cancel`` releases the GIL,
    as ``sane_read`` does, which is what lets one Python thread cancel what
    another is blocked in (``_sane.c`` 2.9.2, verified).  The thread is a
    daemon for the same reason the reader is: if the RPC never returns, it
    must not keep the process alive.

    A failing cancel is logged and swallowed.  There is nothing else to do
    with it -- the read is already lost -- and an exception here would reach
    ``threading.excepthook`` and nobody else.  Either way the thread gives up
    its token on the wedge as it ends, and closes the handle if it is the last
    one out (``_release_wedge``).

    Args:
        dev: The device handle the blocked read is inside.
        done: The event identifying the acquisition being cancelled.

    """
    try:
        dev.cancel()
    except Exception:
        logger.warning("Cancelling the blocked read failed", exc_info=True)
    finally:
        _release_wedge(dev, done, _CANCELLER)


def _clear_wedge() -> None:
    """
    Forget the wedge record.  The caller holds ``_WEDGE_LOCK``.

    Dropping the retained iterator here runs its finaliser's cancel, which is
    harmless only because every caller has either closed the handle first or
    never retained one.
    """
    _WEDGE.stuck = False
    _WEDGE.done = None
    _WEDGE.device = None
    _WEDGE.iterator = None
    _WEDGE.device_id = ""
    _WEDGE.page_label = ""
    _WEDGE.settling = False
    _WEDGE.outstanding.clear()


def _begin_settle(dev: SaneDevice, done: threading.Event, label: str) -> bool:
    """
    Record the wedge before the cancel fires, unless the read just returned.

    The check and the write are one critical section because the reader may
    return in the instant between the timeout and this call.  Reading ``done``
    under the same lock the reader takes *after* setting it is what makes that
    window closed rather than merely narrow.

    Written first, and not once the grace has run out, so that nothing the
    worker is interrupted by while it waits can leave the handle unrecorded
    and closable under a read that is still running.

    Args:
        dev: The handle the reader is inside.
        done: The event identifying this acquisition.
        label: The page label, for the refusal message.

    Returns:
        True if the wedge is now recorded and settling, False if the reader
        had already returned and there is nothing to cancel.

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
    done: threading.Event, canceller: threading.Thread, *, cancel_started: bool
) -> bool:
    """
    Stop waiting: clear the wedge if nothing is left inside SANE, else keep it.

    The reader counts as finished once ``done`` is set, because its work is
    over by then and all it has left is to give up its token.  The cancel
    thread counts as finished once it has given up its own, or if it never
    started, which is what an interrupt landing before or inside its
    ``start()`` can leave behind.  ``cancel_started`` and ``is_alive()`` are
    both consulted for the reason ``_acquire_with_timeout`` gives for the
    reader.

    Args:
        done: The event identifying this acquisition.
        canceller: The cancel thread, started or not.
        cancel_started: Whether its ``start()`` returned.

    Returns:
        True if both threads are out of SANE, so the device context may close
        the handle as usual.  False if one is still inside, in which case the
        wedge stands and the last of them to finish closes the handle.

    """
    with _WEDGE_LOCK:
        if not (_WEDGE.stuck and _WEDGE.done is done):
            # Nothing was recorded: the read returned before the cancel.
            return True
        if done.is_set():
            _WEDGE.outstanding.discard(_READER)
        if not (cancel_started or canceller.is_alive()):
            _WEDGE.outstanding.discard(_CANCELLER)
        if not _WEDGE.outstanding:
            _clear_wedge()
            return True
        _WEDGE.settling = False
        return False


def _release_wedge(dev: SaneDevice, done: threading.Event, holder: str) -> None:
    """
    Give up one thread's token, and close the handle if it was the last.

    Called by the reader as it returns and by the cancel thread as it ends.
    The last of them closes, rather than the thread that gave up on them,
    because by then the thread that gave up has long since raised -- and only
    the last one out knows that nothing is left inside SANE on the handle,
    which is the one fact SANE requires before any other operation may run on
    it.  While the worker is still waiting out the grace (``settling``) it
    owns the outcome, so nothing closes here.

    A close failure is logged and never raised: this runs in a daemon thread
    whose exception nobody would see, and an unhandled one would surface in a
    test run as a ``PytestUnhandledThreadExceptionWarning`` turned into an
    error.

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
        # Counted as closed either way, as _open_device does: the close was
        # attempted, and counting the handle open forever would refuse every
        # later scan job's SANE restart until saneless itself was restarted.
        _handle_closed(dev)
        _clear_wedge()


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
    Refuse a new SANE operation while a read is still outstanding.

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
    # Both ids can come from discovery, which is LAN-supplied text, and this
    # message reaches the terminal, the log and a traceback as it is built.
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

    The wait is for both threads that may be inside SANE on the handle: the
    reader, and the thread sending the cancel (``_cancel_read``).  Both come
    out of one grace, so a scanner slow to answer the cancel holds the worker
    for the grace and no longer.  A cancel still in flight when the grace runs
    out leaves the handle wedged even if the read has returned, since closing
    under a cancel is no safer than closing under a read.

    The decision is taken in a ``finally``, so an interrupt landing during the
    wait -- a second Ctrl-C, say -- still ends it.  The wedge was written
    before the cancel fired, so the interrupt leaves it standing, and the
    device context then leaves the handle alone.  A signal can still land in
    the few instructions of that ``finally`` itself; the record then stays
    settling and is never cleared, which refuses later scans until a restart
    but never closes a handle under a running call.

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
    began = time.monotonic()
    deadline = began + grace
    canceller = threading.Thread(
        target=_cancel_read, args=(dev, done), name=_CANCEL_THREAD_NAME, daemon=True
    )
    started = False
    try:
        if not _begin_settle(dev, done, label):
            return True
        # Noted before the cancel goes out, so no later cleanup can race it
        # into sending a second one (``_OpenHandles``).
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

    On timeout the sequence is record the wedge, cancel, wait, then close
    only if the read returned and the cancel came back.  The cancel goes out
    on a thread of its own, and the wait for both is bounded by one grace
    (``_settle_or_wedge``).  **The late value is discarded unconditionally.**
    Only whether the reader *returned* is consulted, never what it returned:
    measured on real libsane, a cancelled ``snap()`` hands back a truncated
    image rather than raising -- 3779x242 of a full page -- and that image
    clears ``_validate_page_image``, so "use it, it arrived after all" would
    put one more page in the PDF than the error message claims.

    A ``KeyboardInterrupt`` arriving while this waits takes the identical path
    and is then re-raised, so Ctrl-C during a read leaves the device in the
    same state a timeout does.  So does any other exception once the reader
    has started, since no exception type may leave a running read behind a
    handle the device context is about to close.  A second one arriving during the grace ends
    the wait early and replaces the first, but the wedge written before the
    cancel still stands, so the handle is not closed under the read.

    ``reader.start()`` is inside the guarded block, so an interrupt landing
    once the thread exists cannot abandon it with no cancel ever fired.  The
    handler then asks whether there *is* a reader before running the cancel
    tail, because the other end of that window is real too and worse: an
    interrupt arriving before the thread was created would otherwise fire
    ``dev.cancel()`` on a handle with no read in progress, block for the whole
    grace on an event nothing will ever set, and then mark a wedge that nothing
    could ever clear -- ``_release_wedge`` is only called from a reader's
    ``finally``, and there would be no reader.  Every later ``scan_pages`` and
    ``get_capabilities`` would refuse and ``shutdown()`` would permanently skip
    ``sane.exit()``.

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
        try:
            slot.value = work()
        except BaseException as exc:
            # Handed to the waiter rather than raised: see the docstring.
            slot.error = exc
        finally:
            done.set()
            _release_wedge(dev, done, _READER)

    reader = threading.Thread(
        target=read, name=f"{_READER_THREAD_PREFIX}{page_label}", daemon=True
    )
    started = False
    try:
        reader.start()
        started = True
        finished = done.wait(budget.timeout)
    except BaseException:
        # Ctrl-C, or a SIGTERM/SIGHUP (or a server stop) raised as
        # ScanInterrupted, landed while this thread waited on the read -- or
        # the wait itself failed.  Whatever it was, a reader that has started
        # must be settled exactly as on a timeout: cancel the read and wait
        # for it, so the handle is never closed under a read that is still
        # running.  With no reader started there is nothing to settle.  The
        # exception is re-raised unchanged either way, so the caller still
        # tells a cancel from an interruption.
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

    Bundled into one record rather than passed as separate parameters because
    ``_acquire_pages`` and ``_snap_flatbed`` would otherwise sit past ruff's
    ``PLR0913`` argument limit, and this project neither raises the limit nor
    suppresses the rule. Both acquisition paths take the same record, so
    one sheet is bounded the same way whichever way it was presented; the page
    cap means nothing to the flatbed path, which takes one sheet.

    A scan builds one from the page the device agreed to send
    (``_page_budget_seconds``), so the timeout grows with the page. Every
    default is the module constant both paths share, so "one sheet is one
    sheet, whichever way it was presented" is expressed in the default rather
    than merely asserted about it.

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


# The shared default instance.  A module constant and not an inline
# ``_PageBudget()`` in the signature, because a call in a default argument is
# what ruff's B008 rejects; a frozen instance is safe to share.
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

    A page flows device -> validate -> crop -> ``sink.add`` -> record, one at a
    time, and no list of images exists anywhere along it.  The
    only image-typed name that outlives a loop iteration is the one page being
    acquired, which is why the live-page high-water mark a 12-page scan
    measures is 2 and not 1: the loop variable still references page *k-1*
    while page *k* is being read.  That is left alone deliberately -- a ``del``
    added to make the number 1 would exist only to satisfy a test.

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
    busy device all tell the operator to load paper.

    A page that fails its integrity checks is skipped and counted, not fatal:
    one corrupt sheet must not fail a fifty-sheet job.  A batch in which
    *every* fed page was rejected does raise, because returning an empty list
    would reach ``assemble_pdf([])`` and record a job that produced nothing as
    a success.

    Reaching the page cap is not a failure either. The overrun can only be
    seen on the sheet *past* the cap, so that sheet has been fed and acquired
    by the time it is recognised; it is discarded without validation or the
    sink, the feed is stopped, and its number goes back to the caller with the
    pages already kept. A capped pass in which every kept-or-skipped sheet was
    unreadable still raises, exactly as an uncapped one does.

    **A skipped page may break manual-duplex parity, and that is accepted
    deliberately.** One skipped front makes ``len(front_pages) !=
    len(back_pages)``, which ``pipeline.py`` routes to its duplex-mismatch
    delivery -- two partial PDFs plus a warning instead of
    one interleaved document.  That is the honest response: a page the device
    could not read genuinely means the two manual-duplex passes no longer
    correspond.  The promise that manual-duplex page parity survives forbids
    parity broken by *policy* -- the backend silently discarding a clean blank
    back page -- and not parity broken by a page that could not be read at all.
    Parity broken that way is reported, never hidden.  The pipeline goes
    further than the count: it splits a manual-duplex run into its
    ``(fronts)`` and ``(backs)`` PDFs whenever either pass skipped a sheet,
    even when the counts still match, because two passes that each lost a
    different sheet agree on the count and pair every later page wrongly.

    Args:
        dev: Open SANE device handle.
        sink: Where each accepted page goes.  ``add`` is called exactly once
            per accepted page, after it passed its integrity checks and was
            cropped, and nothing here retains the image afterwards.
        framing: Its crop is applied to each accepted page before the sink
            sees it, so what is spooled is what the PDF embeds, and its
            resolution -- the one the device read back -- is the dpi the sink
            records the page at.  The caller builds it from the paper size,
            that resolution and whether the scan area was set on the device.
        budget: The per-page timeout, the grace a timed-out read is given
            to come back after it has been cancelled, and the most sheets the
            pass keeps.  The first two are injectable so a test proving the
            unresponsive-cancel path need not wait out the module's real ten
            seconds; the cap is chosen by the caller from the source.

    Returns:
        The records the sink returned, in acquisition order, how many fed
        sheets were skipped for failing their integrity checks, and the
        number of the sheet fed past the cap when there was one.  Both facts
        leave the backend inside ``ScanBatch`` and by no other route.

    Raises:
        FeederEmptyError: If the feeder produced no pages at all.
        ScanError: If a page times out, the device reports a fault, every
            sheet kept or skipped failed its integrity checks, or the sink
            could not take a page.

    """
    iterator = dev.multi_scan()

    records: list[PageRecord] = []
    page_num = 0
    # Returned to the caller rather than kept local: the operator is told how
    # many sheets were skipped, not left to find it in a log line, and
    # ScanBatch is that count's one channel. It is deliberately kept out of the
    # pipeline's blank-page removal count: that field means empty-page
    # detection and is shown to users as pages removed for being blank, so
    # reporting a corrupt page through it would tell them something untrue.
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
                # FeederEmptyError subclasses ScanError, so saneless's own
                # errors -- including the timeout path's -- propagate here.
                raise
            except Exception as exc:
                scan_error_msg = (
                    f"Scanner error on page {page_num + 1}: {describe(exc)}"
                )
                raise ScanError(scan_error_msg) from exc

            # The overrun is detected on the page *past* the cap, not on the
            # cap itself: a legitimate maximal stack only learns it is finished
            # when the next probe raises, so stopping at equality would reject
            # a full hopper.  That sheet has been fed, so it is discarded here,
            # before validation or the sink, and not counted in page_num: the
            # checks below are about the sheets the pass kept or skipped.
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

            # Integrity only: nonzero dimensions and minimum raw size. Whether
            # the page is worth keeping is the pipeline's decision, not ours.
            if not _validate_page_image(page_image, page_num):
                # Skip and count, never abort. The per-page WARNING naming the
                # page number and the reason comes from _validate_page_image.
                rejected_pages += 1
                continue

            # Cropped here, per page, instead of over a finished list
            # afterwards.  The sink is where this page stops being ours, so
            # everything that has to happen to it happens before the hand-off
            # -- and what is spooled is exactly what the PDF embeds.
            records.append(sink.add(framing.crop(page_image), dpi=framing.resolution))
    finally:
        # Dropping the last reference runs ``_SaneIterator.__del__``, which
        # calls ``device.cancel()``, so where the iterator goes depends on
        # whether a cancel may still be sent.  When the device is wedged a
        # read is still inside it: the wedge record takes the reference, and
        # the reader thread drops it when it finally returns.  When a cancel
        # was already issued -- the page timed out, or the wait was
        # interrupted -- one more would be a second, unbounded request on this
        # thread: the handle's record takes the reference and drops it after
        # the close.  Only a scan that ended without a cancel drops it here.
        if not _retain_iterator(dev, iterator) and not _park_iterator(dev, iterator):
            del iterator

    if page_num == 0:
        raise FeederEmptyError(_FEEDER_EMPTY_MESSAGE)

    # Paper was fed but none of it was readable. This is a distinct condition
    # from an empty feeder and is reported distinctly: the operator needs to
    # hear "unreadable", not "load paper".
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

    The operator's ``requested`` source wins when the device reports it and it
    is a single-sided feeder, so someone who deliberately chose one of two
    feeders gets that one. "Reports it" means what it means for every other
    source: ``_match_source`` ignores case and surrounding whitespace and
    returns the device's spelling, so ``"adf"`` picks a listed ``"ADF"``
    rather than merely the first feeder. Otherwise the first reported single-sided feeder is
    used -- read from the device, never guessed. Hardcoding the short feeder
    name was declined: consumer feeders report ``"Automatic Document
    Feeder"``, and a name the device does not list would fail on exactly the
    hardware manual duplex exists for.

    Whether a source feeds, and whether it scans both sides, is asked of
    ``classify_source`` and nothing else, so no call site can classify a
    source differently from another.

    A feeder that scans both sides (``SourceKind.FEEDER_DUPLEX``) is never
    used. Each pass through it returns 2N pages, the two passes' counts agree,
    ``_interleave_duplex`` pairs a front+back sequence with a reversed
    back+front one, and the job reports ``DONE`` with 4N pages in scrambled
    order. So "any feeder" is deliberately not good enough: when the operator
    named a both-sides source and a single-sided one exists, the single-sided
    one is used with a WARNING naming both; when every feeder scans both sides,
    manual duplex is refused before any page, because a WARNING beside a green
    ``DONE`` on an unattended appliance is still silent corruption.

    There is deliberately no ``Auto`` fallback here. ``Auto`` does not feed,
    and with ``auto_source_mode`` at its ``"flatbed"`` default substituting it
    takes one platen snapshot per pass and reports success, so manual duplex
    silently does not work at all. A device with no feeder is refused instead,
    before any page.

    Args:
        available_sources: The source names the device reports. Empty when
            the device's ``source`` constraint cannot be read.
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


def _resolve_source(
    raw_options: list[tuple], requested: str, *, resolve_feeder: bool = False
) -> _SourceChoice:
    """
    Decide which source name to assign, or refuse before anything is scanned.

    Whether the device *has* a ``source`` option is recorded independently of
    whether its constraint is a list: a device may expose ``source`` with a
    constraint this code cannot read, and it must still be assigned. The
    requested name is then handed over with its surrounding whitespace
    removed, and the device accepts or refuses it itself.

    When the list can be read, the request is matched against it by
    ``_match_source``: exactly, or ignoring case and surrounding whitespace,
    and never by prefix. The device's own spelling is what gets assigned.

    A request that matches nothing is refused here, before any ``start()``,
    naming the sources the device offers -- with one exception. A request the
    classifier calls a flatbed may use the device's ``Auto`` source instead,
    because some scanners reach their glass only through ``Auto`` (they list
    ``Auto`` and a feeder, and no ``Flatbed``). Any other request is never
    swapped for ``Auto``: ``Auto`` is routed by ``auto_source_mode``, which
    defaults to the flatbed, so a feeder request swapped for it would bring a
    whole stack back as one page.

    The substitution is recorded on the returned choice rather than logged
    here. Whether it matters depends on whether ``auto_source_mode`` then
    sends ``Auto`` through the feeder, and that is only decided once routing
    is, in ``scan_pages``.

    Args:
        raw_options: The device's option tuples, as ``get_options()`` returns
            them.
        requested: The source name the caller asked for.
        resolve_feeder: Manual duplex. Resolve a feeder from the device's own
            list via ``_choose_feeder_source`` instead of validating
            ``requested`` alone.

    Returns:
        The source to assign, whether the device has a source option, and the
        requested name if the device's ``Auto`` was chosen in its place.

    Raises:
        ScanError: If the device lists its sources and none matches the
            request, unless the request is a flatbed and the device offers
            ``Auto``; or if the request matches several of them. For manual
            duplex: if the device has no source option and ``requested`` does
            not name a feeder, if every feeder it reports scans both sides, or
            if it reports no source that feeds.

    """
    reported = _constraint(raw_options, "source")
    available_sources = [str(s) for s in reported.values or []]

    # A disjoint early branch, not a guard inside the flow below: returning
    # here makes the Auto substitution structurally unreachable for manual
    # duplex rather than merely conditioned off, and that substitution is what
    # turned each pass into one platen snapshot reported as success. A device
    # with no source option at all feeds without
    # being told and nothing is assigned to it, so the both-sides concern
    # cannot arise; the simplex path already trusts the classifier on the
    # configured name for such a device, and manual duplex does the same,
    # which keeps a legacy "Manual Duplex" profile working there.
    if resolve_feeder:
        if not reported.present:
            if classify_source(requested).uses_feeder:
                return _SourceChoice(requested, has_option=False, substituted_from=None)
            msg = (
                "Manual duplex needs a feeder source, and this device "
                "exposes no source option to choose one; set source to the "
                f"name of its feeder (got {requested!r})"
            )
            raise ScanError(msg)
        feeder = _choose_feeder_source(available_sources, requested)
        return _SourceChoice(feeder, has_option=True, substituted_from=None)

    if not reported.present:
        return _SourceChoice(requested, has_option=False, substituted_from=None)
    if reported.values is None:
        return _SourceChoice(requested.strip(), has_option=True, substituted_from=None)

    matched = _match_source(available_sources, requested)
    if matched is not None:
        return _SourceChoice(matched, has_option=True, substituted_from=None)

    if classify_source(requested) is SourceKind.FLATBED:
        auto = next(
            (s for s in available_sources if classify_source(s) is SourceKind.AUTO),
            None,
        )
        if auto is not None:
            return _SourceChoice(auto, has_option=True, substituted_from=requested)

    # The names are device-supplied text, and this message reaches the
    # terminal, the log and the job's error as it is built.
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
        options: The option list read after the mode was assigned. A source
            or mode change reloads the device's option descriptors, so this is
            the list that describes the device as configured, and every later
            decision about the device's options is made from it.

    """

    resolution: int
    options: list[tuple]


def _assign(dev: SaneDevice, name: str, value: object, device_id: str) -> None:
    """
    Assign one option, as a saneless error naming it if the device refuses.

    Args:
        dev: Open SANE device handle.
        name: The option, in the underscore spelling attribute access uses.
        value: The value to assign.
        device_id: The SANE device name, for the error message.

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

    epson2, kodakaio and magicolor list one feeder source and choose between
    one side and both sides of each sheet with this separate string option,
    ``["Simplex", "Duplex"]``. On epson2 it is active only once the feeder is
    selected, and only on hardware that can duplex.

    Hardware duplex asks for the device's ``Duplex`` entry whenever the option
    is reported, active or not. An inactive option then refuses the
    assignment, and the scan fails naming it before any paper moves, which is
    the honest answer for a feeder that cannot scan both sides. Any other scan
    writes ``Simplex`` when the option is active and lists it, because the
    value persists across handles: a hardware-duplex scan earlier would
    otherwise leave the next one-sided scan scanning both sides.

    Args:
        raw_options: The device's option tuples, as reported for the source
            already selected.
        settings: The requested scan settings, for ``duplex``.

    Returns:
        The entry to assign, or None to leave the option alone. For hardware
        duplex on a list that cannot be read, ``"Duplex"``, the name all three
        drivers use.

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

    Most scanners select duplex by the source name, and
    ``classify_source`` recognises a both-sides feeder by it. A scanner that
    reports ``adf-mode`` is told by that option instead; see
    ``_adf_mode_value``. One with neither, handed a one-sided feeder under a
    hardware-duplex profile, has nothing saneless can set: the scan goes ahead,
    as it always has, with a WARNING that only one side of each sheet is
    scanned.

    Args:
        dev: Open SANE device handle.
        settings: The requested scan settings.
        choice: The source ``_resolve_source`` chose.
        options: The option list read after the source was assigned.
        device_id: The SANE device name, for the error message.

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

    **Order is load-bearing.**  ``sane.py:188-213`` reloads every option
    descriptor when a ``set_option`` reports ``INFO_RELOAD_OPTIONS``, and a
    source change does exactly that.  Setting the source last therefore lets a
    resolution validated against the platen's constraint be stranded under a
    feeder's narrower one.  Asking the device what it is scanning *from* before
    telling it *how* removes that whole class of failure.  The paper size is
    applied afterwards, by ``_apply_paper_size`` in the caller.

    The order is source, adf-mode, mode, depth, resolution. The option list
    is read again after the source and again after the mode, because both
    assignments reload the descriptors: the reload may change which options
    the device offers, whether they are active, and what they accept.
    ``adf-mode`` is decided from the list read after the source. ``depth`` is
    decided from the list read after the mode, and so is everything the caller
    decides after this returns, which is why that list is handed back.

    ``adf-mode`` is how some scanners choose between one side and both sides
    of a fed sheet; ``_set_adf_mode`` decides it, straight after the source
    whose reload is what makes it active.

    ``depth`` is set to 8 when the device offers 8, after ``mode`` (which can
    change what ``depth`` accepts, or switch it off, as epson2 does in its
    1-bit modes) and before ``resolution``. It is set silently: saneless's pages are 8-bit whatever the device scans at, and
    python-sane cannot read a 16-bit frame correctly, so nothing the operator
    chose is lost.

    The resolution is then read back, because SANE substitutes silently:
    measured against the real ``test`` backend, ``5000`` comes back as
    ``1200.0`` and ``0`` as ``1.0``, with no error and no signal to the caller.
    Because the resolution is authoritative for the PDF's page geometry, an
    unnoticed substitution yields both a mis-cropped page and a wrong
    MediaBox, so the substitution has to be visible.

    Args:
        dev: Open SANE device handle.
        settings: The requested scan settings.
        choice: The source ``_resolve_source`` chose. Its name is assigned
            only when the device exposes a ``source`` option at all.
        options: The option list the caller read before resolving the source.
            It stands for ``adf-mode`` when no source is assigned.
        device_id: The SANE device name, for the error messages.

    Returns:
        The resolution the device actually reports, as an ``int`` (the device
        returns a float; callers downstream want whole dpi), and the option
        list read after the mode, which describes the configured device.

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
    # A mode change reloads the descriptors too: a 1-bit mode can switch
    # ``depth`` off, or change what it accepts.
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

    The *effective* source is classified, by ``classify_source`` and nothing
    else. An ``Auto`` source says nothing about what is actually loaded, so
    the operator's ``auto_source_mode`` decides for it. That decision stays
    config-driven; only the *recognition* of an ``Auto`` source belongs to the
    classifier, which also recognises a device's lowercase ``auto`` -- an
    equality test against the one spelling ``Auto`` used to miss it, so
    ``auto_source_mode = "adf"`` was silently ignored and a whole stack came
    back as one page.

    This is also where an ``Auto`` that stood in for a flatbed request is
    reported, because only here is it known whether the substitution changed
    anything. Sent through the feeder, the operator asked for the glass and
    got a stack, which is a WARNING (and a fact on the batch, set by the
    caller). Left on the glass, the scan did what was asked, so it is INFO:
    it is the everyday case of the default profile on a scanner that lists
    only ``Auto`` and a feeder, and a warning there would be noise.

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

    Shared by ``get_capabilities`` and ``scan_pages``, so both report a failed
    read with the same message.

    Args:
        dev: Open SANE device handle.
        device_id: The SANE device name, for the error message.

    Returns:
        The option tuples ``get_options()`` reports.

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

    What python-sane's ``get_parameters()`` returns, with names. It describes
    the frame the options now describe, so it is read after every option and
    the scan area are set.

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

    ``_configure_device`` sets ``depth`` to 8 wherever the device offers it, so
    a 16-bit frame reaching here comes from a device that cannot scan at 8 in
    the chosen mode: one whose ``depth`` lists only 16, or whose mode implies
    16 bits with no ``depth`` option at all. python-sane would read such a
    frame as an image twice as tall, so it is refused while no paper has moved.

    Args:
        parameters: What the configured device reports.
        device_id: The SANE device name, for the error message.

    Raises:
        ScanError: If the frame is 16 bits per sample.

    """
    if parameters.depth == _SIXTEEN_BITS:
        raise ScanError(sixteen_bit_error(neutralise_controls(device_id)))


def _page_budget_seconds(parameters: _ScanParameters, resolution: int) -> float:
    """
    Return how long one page may take, from the page the device will send.

    Twice the page's share, by bytes, of the minute a reference page is
    estimated to take, never less than the floor and never more than the
    ceiling; see the constants for the reasoning. ``bytes_per_line`` already accounts for the mode and the
    bit depth. A three-pass colour scan sends three frames of that size, so it
    counts three. A negative line length from a confused device counts as no
    data, so the floor applies.

    Args:
        parameters: What the configured device reports.
        resolution: The resolution the device settled on, used to work out
            the length of a page whose length is not known in advance.

    Returns:
        The page's timeout in seconds.

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

    ``start()`` opens the SANE data channel and ``snap()`` drains it, so the
    two are wrapped together and nothing else is.  They go through
    ``_acquire_with_timeout`` as a single unit of work, under the same
    page budget a fed sheet of that size gets and with no config key of its
    own: one sheet is one sheet, whichever way it was presented, and a
    platen that stops answering used to hang the job forever while the
    identical failure on a feeder was reported in two minutes.

    The validate-crop-spool sequence that follows is deliberately outside that
    guard: it is the same sequence the feeder path runs, so one page reaches
    the sink the same way however it was acquired.

    The one message python-sane's own ADF iterator treats as the end of the
    feed (``sane.py:130``) is mapped to ``FeederEmptyError`` by the same exact
    string test, so a device routed here while reporting an empty feeder still
    tells the operator to load paper.  Every other failure --
    ``_sane.error`` from ``start()``, ``RuntimeError("Scanner returned no
    data")`` from ``snap()`` -- is a ``ScanError`` naming the device.

    Args:
        dev: Open SANE device handle, already configured.
        device_id: The SANE device name, for the error message.
        sink: Where the page goes.  ``add`` is called exactly once, after the
            page passed its integrity checks and was cropped.
        framing: The crop applied to the page before the sink sees it, and
            the read-back dpi the sink records it at.
        budget: The per-page timeout and the cancel grace; its page cap does
            not apply to one sheet.  Both default to the constants the feeder
            path uses, and both are injectable for the reason the feeder
            path's are: a test proving the
            unresponsive-cancel path must not wait out the module's real ten
            seconds, and without an injectable grace there could be no fast
            flatbed equivalent of ``test_did_not_respond_to_cancel`` at
            all.

    Returns:
        The record the sink returned for the one scanned page.

    Raises:
        FeederEmptyError: If SANE reports the feeder out of documents.
        ScanError: If the sheet did not arrive within the timeout, if the page
            fails its integrity checks, or for any other failure, chained to
            the original.

    """

    def start_and_snap() -> Image.Image:
        dev.start()
        return dev.snap()

    try:
        image = _acquire_with_timeout(dev, start_and_snap, _page_label(0), budget)
    except ScanError:
        # The timeout path's own error, already worded and already logged.
        # FeederEmptyError subclasses ScanError and reaches here the same way.
        raise
    except Exception as exc:
        if str(exc) == "Document feeder out of documents":
            raise FeederEmptyError(_FEEDER_EMPTY_MESSAGE) from exc
        snap_msg = f"Scanner error on {neutralise_controls(device_id)}: {describe(exc)}"
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

    return sink.add(framing.crop(image), dpi=framing.resolution)


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
    # type checkers as well, rather than only at runtime.  The geometry check
    # reads it back to catch an area the device silently clamped.
    @property
    def area(self) -> tuple[tuple[float, float], tuple[float, float]]: ...

    def get_options(self) -> list: ...
    def get_parameters(self) -> tuple[str, int, tuple[int, int], int, int]: ...
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
    ``shutdown()`` a later one initialises again.  The long-running server
    also restarts SANE at the start of each scan job (``reinitialise``), so
    every scan gets a fresh control connection.  A backend is otherwise an
    ordinary object -- there is no singleton and no factory, because the three
    one-shot CLI commands and the web server each construct their own.

    Scanners are listed in a short-lived child process, never in this one
    (``get_devices``), because a listing on a lost net control connection
    kills the process that makes it.  The Scanner health check's open of an
    unlisted configured device happens in that same child
    (``list_and_open``), and so does ``open_and_close``: the health check
    never opens a device in this process.

    The device handles this process opens itself, to scan or to read
    capabilities, go through a context manager that ensures cancel() and
    close() are called on all code paths, unless a read on the handle has not
    returned, in which case neither may be issued at all.
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
        # Kept for every listing child, whose SANE_NET_HOSTS is derived from
        # this setting and never copied from this process's environment.
        self._host = host
        _ensure_initialised(host)

    def close(self) -> None:
        """
        Shut this process's SANE down, through the backend abstraction.

        The work is ``shutdown()``'s, and it is process-level rather than
        per-object: what is released is this process's current ``sane_init``,
        not anything this instance owns.  The method exists so that an
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

    def reinitialise(self) -> None:
        """
        Restart this process's SANE before a scan job or a later pass, or refuse to.

        Called at the top of each scan job, under the scanner gate, before the
        job's first open; and before every pass of a multi-page scan after the
        first, still under the gate the job holds, when the previous pass has
        closed its device.  Nowhere else: the second pass of a manual-duplex
        scan does not call it.  Either way no handle is open.  After a saned
        restart the net backend keeps its old control connection:
        ``sane_open`` in ``backend/net.c`` checks the reply status but never
        drops or reconnects a connection that has gone bad, so every later
        open in this process fails with an I/O error until SANE is restarted.
        Restarting at each job gives every scan a fresh connection, which also
        covers a saned restart between two jobs that no check would have seen,
        and restarting before each later pass covers one during the wait for
        the operator between passes.

        Two states refuse the restart, and both refuse before any SANE call,
        because the call itself is the hazard:

        - **A read has not returned.** ``sane_exit`` would close the handle
          the read is still inside, which SANE forbids.
        - **A device handle is open.** ``sane_exit`` closes every open handle,
          and on the net backend each close waits for saned's reply.  On a
          host that has silently vanished that wait was measured to last
          longer than 40 seconds on both supported libsane builds, with the
          scanner gate held the whole time.

        ``shutdown()`` keeps its own outstanding-read check as a second guard.
        Descriptor counts were measured flat over hundreds of exit/init cycles
        on both builds, with scans in between, so restarting per job does not
        grow the process's descriptor count.

        ``shutdown()`` and ``_ensure_initialised()`` each take the init lock,
        which is not reentrant, so they are called one after the other.  Their
        success lines are logged at DEBUG, and this logs one INFO line, so a
        job adds one line to the log rather than three.

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

        Opens the device, yields it for use, then ensures cancel()
        and close() are called on all exit paths (normal and error) --
        **unless** a reader thread is still inside SANE on this handle, in
        which case both are skipped.  The cancel is also skipped once a
        cancel has already been issued on the handle, after a page timed out
        or its wait was interrupted: that one is all the device gets, since
        a second would be one more unbounded request to a scanner that has
        already failed to answer in time (``_OpenHandles``).  That is not an omission: the SANE
        standard forbids any other operation while one is outstanding, and
        ``sane_close`` additionally runs holding the GIL while ``sane_read``
        has released it, so a close racing a blocked read is the one sequence
        python-sane cannot survive.  The handle is released later by
        the reader thread itself, and until then the wedge record holds it.

        Every handle is counted from a successful open until its close was
        attempted (``_OpenHandles``), so ``reinitialise()`` can refuse to
        restart SANE while one is open.  A handle left open by a stuck read
        stays counted until the reader closes it.

        A failed close is logged, never raised.  The line names the device
        and carries the traceback, because that is what an operator debugging
        a scanner needs.  Only a scan or a capability read opens a handle
        here; the Scanner health check, whose log may name no device, opens
        in the listing child (``list_and_open``).

        Every listing runs in a child, so this process's libsane never lists
        before it opens: a scan job opens on a SANE that ``reinitialise()``
        has just started, with no ``sane_get_devices`` before ``sane_open``.
        That has been measured only for a ``net:`` device served by saned's
        ``test`` backend.  A backend that finds its devices by network
        discovery, such as ``escl`` or ``airscan``, has not been measured
        opening an id it has not listed; if one cannot, the open fails here
        with SANE's error, and listing in this process first would be the
        fix.

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
            # The id may come from discovery, which is LAN-supplied text, so
            # it is defused here, where the message is built: every sink it
            # reaches, a traceback included, then gets the safe spelling.
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
                    # Logged, never raised: an exception from close() here
                    # would replace the one that ended the scan, which is the
                    # error the operator needs to see.
                    logger.warning(
                        "Could not close scanner %s", device_id, exc_info=True
                    )
                finally:
                    # Counted closed whether or not close() raised: the close
                    # was attempted, and a handle counted open forever would
                    # refuse every later scan job's SANE restart.
                    _handle_closed(dev)

    def get_devices(self) -> list[DeviceInfo]:
        """
        List the available scanning devices, in a short-lived child process.

        The listing never runs in this process.  libsane's net backend keeps
        one control connection per scanner host open between listings, and
        once the saned on that host restarts, the next ``sane_get_devices``
        fails its request, ignores the failed status and reads a reply that
        was never filled in (sane-backends ``backend/net.c``,
        ``sane_get_devices``), which kills the process that made the call.  A
        child starts from a fresh ``sane_init`` every time, so it never holds
        a stale connection, and a crash or a hang in it is a failed listing
        rather than a dead server.  Every caller -- the scan path's device
        resolution, the worker, the CLI and the health checks -- lists
        through here.

        It still refuses while a read is outstanding, exactly as
        ``scan_pages`` and ``get_capabilities`` do, and before any child is
        started.  A child could not touch this process's stuck handle; the
        refusal keeps one rule for every SANE entry point, and a scanner
        whose read is stuck cannot scan anyway.  The scan path reaches this
        whenever ``scanner.device`` is empty -- the documented
        auto-detection default -- before ``scan_pages`` would have refused.

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
        # No device to name, because enumeration is the call that finds out
        # which devices there are.  _refuse_if_wedged names the *wedged* device
        # from its own record either way, so the message still says which
        # scanner is holding things up.
        _refuse_if_wedged("the scanners", "list")
        reply = _launch_listing(ListingRequest(), configured_host=self._host)
        if reply.list_error is not None:
            # libsane's text can repeat what a LAN peer sent, so it is
            # defused here, where the message is built.
            reason = describe_text(reply.list_error.message, reply.list_error.type_name)
            list_msg = f"Could not list scanners: {neutralise_controls(reason)}"
            raise ScanError(list_msg)
        return list(_listed_devices(reply))

    def list_and_open(self, open_if_unlisted: str) -> DeviceSurvey:
        """
        List the devices and open an unlisted configured one, in one child.

        This is the Scanner health check's list-then-open.  Both steps run in
        the same short-lived child, for the reason ``get_devices`` gives, so
        the check never touches this process's libsane and always sees a
        fresh control connection.  The child opens the configured id only
        when its own listing does not include it, and cancels and closes it
        again at once.

        Nothing is logged here, and the survey carries exception class names
        only: no device id and no exception text, since a ``net:`` id is a
        LAN address and the text of a SANE error can repeat it.

        It refuses while a read is outstanding, before any child is started,
        exactly as ``get_devices`` does.

        Args:
            open_if_unlisted: The configured device id, or ``""`` when none
                is configured.

        Returns:
            What the child's listing and open found.

        Raises:
            ListingCrashedError: The listing child died from a signal.
            ListingTimedOutError: The listing child did not finish in time.
            ListingNoAnswerError: The listing child gave no usable answer, or
                could not be started.
            ScanError: If a previous read has not returned, in which case no
                child is started.

        """
        _refuse_if_wedged("the scanners", "list")
        reply = _launch_listing(
            ListingRequest(open=open_if_unlisted or None),
            configured_host=self._host,
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

        The base class's default opens through ``get_capabilities``, in this
        process, where the open and close logging of ``_open_device`` names
        the device and carries the exception's text.  That breaks the rule
        the base class sets for this method, so the open happens in a listing
        child instead, through ``list_and_open``.  The child opens the id
        only when its own listing does not include it: a listed device is
        taken as reachable, as the Scanner health check takes it.

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
            # An empty id asks the listing child to list only, so nothing
            # would be opened and the call would report success.
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
                option_names=tuple(str(opt[1]) for opt in raw_options if len(opt) >= 2),
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

        The timeout is per page, not per job: one page is cancelled if it takes
        longer than two to three times its expected duration, so a long stack
        is never cut short merely for being long.

        The work lives in the module-level ``_acquire_pages``, which runs each
        blocking ``next(iterator)`` on a fresh ``daemon=True`` thread and waits
        on an event: ``signal.alarm`` is not safe off the main thread, and the
        thread pool this replaced left non-daemon workers that
        ``concurrent.futures`` joins at interpreter exit, so one stuck read
        stopped the process from exiting at all.

        Args:
            dev: Open SANE device handle.
            sink: Where each accepted page goes.
            framing: The crop applied to each accepted page before the sink
                sees it, and the read-back dpi the sink records it at.
            budget: The per-page timeout, the cancel grace and the page cap.

        Returns:
            The records the sink returned, the count of fed sheets skipped
            for failing their integrity checks, and the sheet fed past the cap
            when there was one.

        """
        return _acquire_pages(dev, sink, framing, budget)

    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Acquire pages from scanner, handing each one to the sink.

        Opens the device, matches the requested source against the ones it
        offers (refusing before anything is started when none matches), sets
        scan parameters, and returns the finished batch. Uses multi_scan() for ADF sources, snap() for flatbed.
        Does NOT pass a progress callback to snap(): python-sane does not
        validate callbacks, and a bad one segfaults the process.

        Acquisition is eager, and that is what lets the two facts this backend
        measures leave it at all: a generator hands back images only, and its
        return value is discarded by the ``list()`` every caller wrapped it in.

        Eager is not the same as accumulating, though, and this method holds no
        list of images on either path. Each page is validated,
        cropped and handed to ``sink`` as it arrives, and what comes back is a
        record: where the page was written and what was measured about it. The
        sink belongs to the caller because where a page lands is a pipeline
        fact -- the workspace, the ``tmp_dir`` under it, the pass label in the
        file name -- and none of that is the backend's business.

        The device being closed by the time this returns is a **consequence**
        of that, not a goal -- the handle previously stayed open until the
        generator was drained or garbage-collected. The one exception is a
        read that never returned: the handle is then deliberately left open
        and this method refuses outright until it does.

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
            # Read once here to match the source against.  Assigning the
            # source or the mode reloads the device's option descriptors, so
            # _configure_device reads the list again after each and hands the
            # last one back; everything decided after configuration is
            # decided from the list for the device as configured.
            raw_options = _read_options(dev, device_id)

            # Match the requested source against the device's list, or
            # refuse here, before anything is started.
            choice = _resolve_source(
                raw_options,
                settings.source,
                resolve_feeder=settings.duplex == "manual",
            )

            # Set device options.  The resolution it hands back is the one the
            # device actually chose, which may not be the one requested.
            configured = _configure_device(
                dev, settings, choice, options=raw_options, device_id=device_id
            )
            actual_resolution = configured.resolution

            # Routing comes before the paper size, because whether the pass
            # feeds decides how the paper size can be applied without cutting
            # an edge off the page.
            use_adf = _route(choice, settings)

            # Frame the pages to the paper size where that loses nothing,
            # scaling by the unit the device reports at the resolution it
            # actually chose.  Built once here and handed down, so both
            # acquisition paths crop the same way and hand the sink the same
            # read-back dpi, and neither has to carry the geometry facts as
            # extra parameters past ruff's PLR0913 ceiling.
            framing = _apply_paper_size(
                dev,
                configured.options,
                settings,
                use_adf=use_adf,
                resolution=actual_resolution,
            )

            # What the device will now deliver, read once every option and
            # the scan area are set.  A 16-bit frame is refused here, before
            # either acquisition path starts a page.
            parameters = _read_parameters(dev, device_id)
            _refuse_sixteen_bit(parameters, device_id)

            # One budget for every page of the pass, sized to the page the
            # device will send, on whichever path it is acquired.  The page
            # cap is the one the source earns: a source named as a feeder
            # gets the per-pass cap; one that is not -- Auto sent through the
            # feeder -- may be a platen rescanned forever, so it gets the much
            # lower one.  The flatbed path takes one sheet and ignores it.
            named_feeder = classify_source(choice.effective).uses_feeder
            budget = _PageBudget(
                timeout=_page_budget_seconds(parameters, actual_resolution),
                max_pages=_MAX_ADF_PAGES if named_feeder else _MAX_AUTO_FEEDER_PAGES,
                page=_describe_page(parameters, actual_resolution),
            )

            cap_reached: PassCapReached | None = None
            if use_adf:
                # ADF/duplex: use multi_scan() for multi-page acquisition.
                fed = self._scan_adf_pages(dev, sink, framing, budget)
                records, pages_rejected = fed.records, fed.rejected
                if fed.sheet_not_kept is not None:
                    cap_reached = PassCapReached(
                        cap=budget.max_pages,
                        sheet_not_kept=fed.sheet_not_kept,
                        auto_source=not named_feeder,
                    )
            else:
                # Flatbed: start() initiates the SANE data channel, then
                # snap() drains it via sane_read() loop.  Without start()
                # the read loop has no data source.  Validation and the crop
                # live in there too, so one sheet reaches the sink by the same
                # route however it was acquired.
                records = [_snap_flatbed(dev, device_id, sink, framing, budget)]
                # Nothing was skipped: an unreadable sheet raised in there.
                pages_rejected = 0

        # Assembled inside the device context but returned outside it, so the
        # handle is released before the caller ever sees the batch.
        # An Auto that stood in for a flatbed is only worth reporting when it
        # fed: on the glass it scanned what was asked for.
        return ScanBatch(
            pages=tuple(records),
            actual_resolution=actual_resolution,
            pages_rejected=pages_rejected,
            substituted_source=choice.substituted_from if use_adf else None,
            cap_reached=cap_reached,
        )
