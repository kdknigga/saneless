"""
One faithful double for python-sane 2.9.2, shared by every test that needs one.

This module exists because hand-written doubles disagreed with the real library
and with each other, and each disagreement let a shipped defect earn a green
test.  The specification is not this docstring: it is
``tests/test_fake_sane_contract.py``, which runs each rule below against this
double and against the real SANE ``test:0`` backend in the same parametrised
row, so a rule the double gets wrong turns red there.  python-sane's own
``sane.py`` and ``_sane.c``, and libsane's ``sanei_constrain_value``, are what
the rules are ported from.

What the double models:

1. Assigning an **unknown** option name stores it silently in ``__dict__``.
   There is no device call, no validation and no raise, so code cannot learn
   that a device lacks an option by catching an error; it has to read the
   option list.
2. A **string** value is first cut to the option's size less one, as
   python-sane copies it into a buffer of that size.  A string list then takes
   a case-insensitive unique prefix and stores the listed spelling: ``"gray"``
   and ``"G"`` both become ``"Gray"``.  An exact match of any case wins at
   once; no match, or several, raises the local error type (the real
   ``_sane.error: Invalid argument``).  Nothing is stripped.
3. A **number** is compared as a SANE word.  A word list snaps to its nearest
   member, a tie keeping the earlier entry.  A range clamps, then quantises to
   its step, rounding half up.  Neither ever raises.  ``TYPE_FIXED`` values go
   through SANE's 16.16 fixed-point representation, truncated toward zero, so
   a length like letter's 215.9 mm does not read back exactly as written even
   where a range has no step.
4. Assigning the **wrong Python type** raises a plain ``TypeError`` from the C
   layer before SANE is reached: a ``TYPE_INT`` option refuses a float, even a
   whole one, and a ``TYPE_FIXED`` option refuses a string.  ``TYPE_INT``
   values are stored and read back as ``int``, ``True`` as ``1``.
5. Buttons, groups, inactive options and the read-only attributes raise
   ``AttributeError`` with a specific message.
6. ``multi_scan()`` **cannot raise** -- it only constructs the iterator.  The
   iterator calls ``start()`` then ``snap()`` once per page, and converts
   exactly one message, ``Document feeder out of documents``, to
   ``StopIteration``.
7. A cancelled read raises with SANE's own status text, ``Operation was
   canceled``, and ``init()`` returns python-sane's 4-tuple: the packed version
   code, then its major, minor and build.
8. ``FakeSaneModule.open()`` returns a **new handle** every time, and every
   handle shares the one device, so an option value set through one handle is
   still set on the next.  A closed handle refuses every call but ``close()``
   with ``SaneDev object is closed`` before the device counts anything, and
   closing it again does nothing.  The feeder iterator cancels the handle that
   made it when it is finalised, swallowing any error, so dropping one is a
   cancel.  A test arranges and inspects the device itself through
   ``FakeSaneModule.device``; a ``FakeSaneDev`` used directly behaves as a
   handle that is always open.
9. ``get_parameters()`` returns python-sane's five-tuple ``(format,
   last_frame, (pixels_per_line, lines), depth, bytes_per_line)``.  The format
   is ``"color"`` for a colour mode and ``"gray"`` otherwise, the size is the
   page size, the depth is the ``depth`` option's value when the table has one
   and 8 when it does not, and a line is ``pixels_per_line`` samples of
   ``depth`` bits per channel, rounded up to whole bytes.  Changing the depth
   changes the bytes per line, never the frame's size.

One deliberate, documented divergence: the real ``__load_option_dict`` filters
``TYPE_GROUP`` options out of ``opt``, which makes the library's own "Groups
don't have values" branch unreachable.  This fake keeps groups in the table so
that branch is exercisable.  Either way a group raises ``AttributeError``; only
the message differs.

``narrow_resolution_for_source`` models the option-reload *hazard* rather than
a measured device.  ``sane.py`` reloads every option descriptor when a
``set_option`` reports ``INFO_RELOAD_OPTIONS``, which a source change does, so a
resolution accepted against the platen's range can be left standing against a
feeder's narrower one.  The knob swaps in the narrower range on a source
assignment and deliberately does **not** re-validate the value already stored,
which is precisely what makes assignment ordering observable.  The SANE
``test`` backend was measured **not** to behave this way, so this hazard
cannot be reproduced against real hardware and is modelled here on purpose
rather than discovered there.
"""

from __future__ import annotations

import contextlib
import operator
import threading
import weakref
from enum import StrEnum
from typing import TYPE_CHECKING, Any, assert_never

from PIL import Image, ImageDraw

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "FakeSaneDev",
    "FakeSaneError",
    "FakeSaneHandle",
    "FakeSaneModule",
    "ReadBlockMode",
    "build_option_table",
    "build_test0_option_table",
]

# SANE value types, read from _sane rather than guessed.
_TYPE_BOOL = 0
_TYPE_INT = 1
_TYPE_FIXED = 2
_TYPE_STRING = 3
_TYPE_BUTTON = 4
_TYPE_GROUP = 5

# SANE units.  There is no UNIT_CM and no UNIT_INCH.
_UNIT_NONE = 0
_UNIT_PIXEL = 1
_UNIT_MM = 3
_UNIT_DPI = 4

# SANE capability bits, and the three combinations this table needs.
_CAP_SOFT_SELECT = 1
_CAP_SOFT_DETECT = 4
_CAP_INACTIVE = 32
_CAP_SETTABLE = _CAP_SOFT_SELECT | _CAP_SOFT_DETECT
_CAP_NOT_SETTABLE = _CAP_SOFT_DETECT
_CAP_INACTIVE_OPTION = _CAP_INACTIVE | _CAP_SETTABLE

# sane.py:190 -- assigning any of these raises, whatever the option table says.
_READ_ONLY_ATTRIBUTES = frozenset(
    {"dev", "optlist", "area", "sane_signature", "scanner_model"}
)

# sane.py:130 -- the single message the ADF iterator converts to StopIteration.
_FEEDER_EMPTY_MESSAGE = "Document feeder out of documents"

# SANE_Fixed is a 16.16 fixed-point integer, so a TYPE_FIXED option can only
# represent multiples of 1/65536.
_SANE_FIXED_SCALE = 65536

_INVALID_ARGUMENT = "Invalid argument"
_SANE_FIXED_TYPE_ERROR = "SANE_FIXED requires a floating point number"
_SANE_INT_TYPE_ERROR = "SANE_INT and SANE_BOOL require an integer"

# Realistic constraints.  The long feeder name is one a real device reports and
# code has mishandled, and resolution is a range because code has shipped that
# only understood a list.
_DEFAULT_SOURCES = ["Flatbed", "Automatic Document Feeder", "ADF Duplex"]
_DEFAULT_MODES = ["Color", "Gray", "Lineart"]
_DEFAULT_RESOLUTION_RANGE = (1.0, 1200.0, 1.0)
_DEFAULT_GEOMETRY_RANGE = (0.0, 300.0, 1.0)

_DEVICE_TUPLE = ("test:0", "TestVendor", "TestModel", "scanner")

# The (major, minor, build) libsane 1.0.32 reports from sane_init.
_SANE_VERSION = (1, 0, 32)

_GEOMETRY_OPTIONS = (
    ("tl-x", "Top-left x"),
    ("tl-y", "Top-left y"),
    ("br-x", "Bottom-right x"),
    ("br-y", "Bottom-right y"),
)

# The four geometry names alone, in the hyphenated spelling get_options()
# reports.  Attribute assignment uses underscores (dev.tl_x); both spellings are
# load-bearing and one set used for both would be wrong on one side.
_GEOMETRY_NAMES = tuple(name for name, _title in _GEOMETRY_OPTIONS)

# Small enough to keep a multi-page feeder test cheap, and deliberately smaller
# than any paper size at a realistic dpi -- see set_page_size().
_DEFAULT_PAGE_SIZE = (200, 300)

# The ceiling every Event-gated read waits under, and the reason it has one.
#
# A gate that is deliberately never released must still not outlive the test
# session, so the wait is bounded rather than infinite.  30 s sits under
# pytest-timeout's 60 s, so an abandoned reader expires inside the run instead
# of after it, and comfortably above the backend's 10 s cancel grace, so it is
# always the gate -- never this ceiling -- that decides when a blocked read
# comes back.  It is a bounded wait and not a sleep: nothing waits it out on a
# passing test, which this suite does not allow.
_READ_GATE_CEILING_SECONDS = 30.0

# The denominator that makes a post-cancel page truncated rather than absent.
#
# Measured on real libsane: a cancelled ``snap()``
# returns a *truncated image* rather than raising -- 3779x242 of a full page,
# which clears the backend's 10 KB floor and would be spooled by any code that
# used a late value.  A quarter of the default 200x300 page is 200x75, i.e.
# 45,000 bytes of RGB data, which clears that same floor for the same reason.
# Modelling the truncation this way is what makes the backend's discard rule
# testable instead of incidental.
_TRUNCATED_PAGE_DIVISOR = 4

# The bit depths ``test:0`` lists for its ``depth`` option, which is what
# ``offer_depth()`` offers unless told otherwise.
_TEST0_DEPTHS = (1, 8, 16)

# The depth a frame reports when the table has no ``depth`` option: a device
# without one scans at 8 bits per sample unless its mode says otherwise, which
# is what ``set_parameters(depth=...)`` models.
_DEFAULT_DEPTH = 8

# Samples per pixel in a colour frame; a gray frame has one.
_COLOR_SAMPLES = 3

# The names ``set_parameters()`` accepts, one per element of the tuple
# ``get_parameters()`` returns.
_PARAMETER_FIELDS = frozenset(
    {
        "frame_format",
        "last_frame",
        "pixels_per_line",
        "lines",
        "depth",
        "bytes_per_line",
    }
)

# What python-sane raises for any call on a handle after ``close()``; the C
# layer checks for it before it makes a SANE call.
_CLOSED_MESSAGE = "SaneDev object is closed"

# The message a real cancelled read raises with when it raises at all: SANE's
# ``SANE_STATUS_CANCELLED`` renders as this string through ``sane_strstatus``.
_CANCELLED_MESSAGE = "Operation was canceled"


class ReadBlockMode(StrEnum):
    """
    How an Event-gated read ends, and whether ``cancel()`` ends it at all.

    The three variants are the three things real libsane was measured or read
    to do when a read is cancelled, and a test double that offered only one of
    them would let the backend's timeout path look correct on the other two.
    """

    # cancel() releases the gate and the read returns a truncated page -- the
    # measured behaviour, and the one the backend must refuse to spool.
    PARTIAL = "partial"
    # cancel() releases the gate and the read raises, as a backend reporting a
    # status that is neither GOOD nor EOF does.
    RAISE = "raise"
    # cancel() does not release the gate at all: the scanner never answers.
    # Only release_read() ends this one, which is how a test drives the
    # backend's wedge and then its recovery.
    NEVER = "never"


def _option_is_active(cap: int) -> bool:
    """Mirror ``_sane.OPTION_IS_ACTIVE``."""
    return not cap & _CAP_INACTIVE


def _option_is_settable(cap: int) -> bool:
    """Mirror ``_sane.OPTION_IS_SETTABLE``."""
    return bool(cap & _CAP_SOFT_SELECT)


def _string_size(entries: list[str]) -> int:
    """
    Size a string option the way a backend does: its longest entry plus a NUL.

    Args:
        entries: The option's string list.

    Returns:
        The byte size SANE reports at index 6 of the option tuple.

    """
    return max((len(entry) for entry in entries), default=0) + 1


def _build_option_table(
    *,
    sources: list[str] | None = None,
    modes: list[str] | None = None,
    resolution_range: tuple[float, float, float] = _DEFAULT_RESOLUTION_RANGE,
    geometry_range: tuple[float, float, float] = _DEFAULT_GEOMETRY_RANGE,
) -> list[tuple]:
    """
    Build a nine-element SANE option table.

    The layout is ``(index, name, title, desc, type, unit, size, cap,
    constraint)``.  Note that names here are **hyphenated** (``tl-x``) while
    attribute access uses underscores (``dev.tl_x``); the backend's presence
    check reads the first spelling and the assignment uses the second, so both
    are load-bearing.

    Args:
        sources: Source names for the ``source`` list constraint.
        modes: Mode names for the ``mode`` list constraint.
        resolution_range: The ``(min, max, step)`` resolution constraint.
        geometry_range: The ``(min, max, step)`` constraint shared by the four
            geometry options.

    Returns:
        The option table, ready to hand to :class:`FakeSaneDev`.

    """
    source_list = list(_DEFAULT_SOURCES if sources is None else sources)
    mode_list = list(_DEFAULT_MODES if modes is None else modes)
    geometry = [
        (
            index,
            name,
            title,
            f"{title} position of the scan area",
            _TYPE_FIXED,
            _UNIT_MM,
            4,
            _CAP_SETTABLE,
            geometry_range,
        )
        for index, (name, title) in enumerate(_GEOMETRY_OPTIONS, start=4)
    ]
    return [
        (
            1,
            "source",
            "Scan source",
            "Selects the scan source",
            _TYPE_STRING,
            _UNIT_NONE,
            _string_size(source_list),
            _CAP_SETTABLE,
            source_list,
        ),
        (
            2,
            "mode",
            "Scan mode",
            "Selects the scan mode",
            _TYPE_STRING,
            _UNIT_NONE,
            _string_size(mode_list),
            _CAP_SETTABLE,
            mode_list,
        ),
        (
            3,
            "resolution",
            "Resolution",
            "Sets the resolution of the scanned image",
            _TYPE_FIXED,
            _UNIT_DPI,
            4,
            _CAP_SETTABLE,
            resolution_range,
        ),
        *geometry,
        (
            8,
            "scan-button",
            "Scan button",
            "A button, which has no value",
            _TYPE_BUTTON,
            _UNIT_NONE,
            0,
            _CAP_SETTABLE,
            None,
        ),
        (
            9,
            "geometry-group",
            "Geometry",
            "A group, which has no value",
            _TYPE_GROUP,
            _UNIT_NONE,
            0,
            _CAP_SETTABLE,
            None,
        ),
        (
            10,
            "inactive-opt",
            "Inactive option",
            "An option the device currently reports as inactive",
            _TYPE_STRING,
            _UNIT_NONE,
            _string_size(["x"]),
            _CAP_INACTIVE_OPTION,
            ["x"],
        ),
        (
            11,
            "readonly-opt",
            "Read-only option",
            "An option the device reports but will not let software set",
            _TYPE_STRING,
            _UNIT_NONE,
            _string_size(["x"]),
            _CAP_NOT_SETTABLE,
            ["x"],
        ),
    ]


def build_option_table(
    *,
    geometry_range: tuple[float, float, float] = _DEFAULT_GEOMETRY_RANGE,
    geometry_unit: int = _UNIT_MM,
    omit: tuple[str, ...] = (),
    geometry_settable: bool = True,
) -> list[tuple]:
    """
    Build the default option table, adjusted for the case a test must model.

    This is the public entry point for the cases a constructor keyword cannot
    reach: ``FakeSaneDev.__init__`` already carries ruff's maximum of five
    arguments (``PLR0913``), and this project forbids suppressing the rule.

    ``omit`` exists for the crop fallback.  A device whose option list does not
    mention the geometry options is the case the old geometry-less double
    claimed to model and got backwards: the real library *stores* ``dev.br_y``
    on such a device rather than raising, so an omitted option table is the
    only way to reproduce the condition that makes the crop fallback
    reachable.

    ``geometry_unit`` exists for the unit conversion.  The unit lives at index
    5 of the option tuple, and a backend is free to report its scan area in
    something other than millimetres, which the geometry arithmetic has to
    scale by rather than assume away.

    Args:
        geometry_range: The ``(min, max, step)`` constraint shared by the four
            geometry options.
        geometry_unit: The SANE unit code the geometry options report at index
            5.  Defaults to ``UNIT_MM``, which is what real hardware was
            measured reporting.
        omit: Hyphenated option names to leave out of the table entirely, as a
            device lacking them would report it.
        geometry_settable: When False the geometry options are still reported
            but are marked not software-settable, so assigning one raises the
            measured ``AttributeError`` instead of storing the value.

    Returns:
        The option table, ready to hand to :class:`FakeSaneDev`.

    """
    cap = _CAP_SETTABLE if geometry_settable else _CAP_NOT_SETTABLE
    return [
        (*option[:5], geometry_unit, option[6], cap, option[8])
        if option[1] in _GEOMETRY_NAMES
        else option
        for option in _build_option_table(geometry_range=geometry_range)
        if option[1] not in omit
    ]


def build_test0_option_table() -> list[tuple]:
    """
    Build an option table shaped like the real SANE ``test:0`` device.

    Only the options the contract table exercises are here, with the indices,
    types, units, sizes and constraints ``test:0`` reports for them:
    ``mode`` and ``source`` string lists, ``depth`` an INT word list,
    ``resolution`` a FIXED dpi range, the four geometry options FIXED
    millimetre ranges, ``ppl-loss`` an INT pixel range, ``print-options`` a
    button, and ``three-pass-order`` a string list that is inactive until
    three-pass scanning is switched on.  The group separators ``test:0``
    reports are left out, because python-sane drops them from its option
    dictionary anyway.

    Returns:
        The option table, ready to hand to :class:`FakeSaneDev`.

    """
    modes = ["Gray", "Color"]
    sources = ["Flatbed", "Automatic Document Feeder"]
    frame_orders = ["RGB", "RBG", "GBR", "GRB", "BRG", "BGR"]
    geometry = [
        (
            index,
            name,
            title,
            f"{title} position of scan area.",
            _TYPE_FIXED,
            _UNIT_MM,
            4,
            _CAP_SETTABLE,
            (0.0, 200.0, 1.0),
        )
        for index, (name, title) in enumerate(_GEOMETRY_OPTIONS, start=24)
    ]
    return [
        (
            2,
            "mode",
            "Scan mode",
            "Selects the scan mode.",
            _TYPE_STRING,
            _UNIT_NONE,
            _string_size(modes),
            _CAP_SETTABLE,
            modes,
        ),
        (
            3,
            "depth",
            "Bit depth",
            "Number of bits per sample.",
            _TYPE_INT,
            _UNIT_NONE,
            4,
            _CAP_SETTABLE,
            [1, 8, 16],
        ),
        (
            6,
            "three-pass-order",
            "Set the order of frames",
            "Set the order of frames in three-pass color mode.",
            _TYPE_STRING,
            _UNIT_NONE,
            _string_size(frame_orders),
            _CAP_INACTIVE_OPTION,
            frame_orders,
        ),
        (
            7,
            "resolution",
            "Scan resolution",
            "Sets the resolution of the scanned image.",
            _TYPE_FIXED,
            _UNIT_DPI,
            4,
            _CAP_SETTABLE,
            (1.0, 1200.0, 1.0),
        ),
        (
            8,
            "source",
            "Scan source",
            "Selects the scan source.",
            _TYPE_STRING,
            _UNIT_NONE,
            _string_size(sources),
            _CAP_SETTABLE,
            sources,
        ),
        (
            17,
            "ppl-loss",
            "Loss of pixels per line",
            "The number of pixels that are wasted at the end of each line.",
            _TYPE_INT,
            _UNIT_PIXEL,
            4,
            _CAP_SETTABLE,
            (0, 128, 1),
        ),
        (
            22,
            "print-options",
            "Print options",
            "Print a list of all options.",
            _TYPE_BUTTON,
            _UNIT_NONE,
            0,
            _CAP_SETTABLE,
            None,
        ),
        *geometry,
    ]


def _default_values(table: list[tuple]) -> dict[str, Any]:
    """
    Pick a starting value for every option that has one.

    Buttons and groups are skipped because they have no value at all.  A
    bottom-right geometry option defaults to the top of its range so the
    default scan area is the whole bed.

    Args:
        table: The option table to derive defaults from.

    Returns:
        A mapping of underscore-spelled option name to starting value.

    """
    values: dict[str, Any] = {}
    for option in table:
        name, value_type, constraint = option[1], option[4], option[8]
        if value_type in (_TYPE_BUTTON, _TYPE_GROUP):
            continue
        key = name.replace("-", "_")
        if isinstance(constraint, list) and constraint:
            values[key] = constraint[0]
        elif isinstance(constraint, tuple):
            number = int if value_type in {_TYPE_INT, _TYPE_BOOL} else float
            low, high = number(constraint[0]), number(constraint[1])
            if key.startswith("br_"):
                values[key] = high
            elif key == "resolution":
                values[key] = min(max(number(300), low), high)
            else:
                values[key] = low
        elif value_type in {_TYPE_INT, _TYPE_BOOL}:
            values[key] = 0
        elif value_type == _TYPE_FIXED:
            values[key] = 0.0
        else:
            values[key] = ""
    return values


def _as_float(value: object) -> float:
    """
    Coerce a value for a ``TYPE_FIXED`` option, or raise as the C layer does.

    Args:
        value: The assigned value.

    Returns:
        The value as a float.

    Raises:
        TypeError: If the value is not a real number, carrying the exact text
            the C layer produces.

    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(_SANE_FIXED_TYPE_ERROR)
    return float(value)


def _as_int(value: object) -> int:
    """
    Coerce a value for a ``TYPE_INT`` or ``TYPE_BOOL`` option, or raise.

    python-sane takes any Python ``int`` here, and ``bool`` is one, so
    ``True`` is stored as ``1``.  Anything else -- a whole-number float
    included -- is refused by the C layer before SANE sees it.

    Args:
        value: The assigned value.

    Returns:
        The value as a plain ``int``.

    Raises:
        TypeError: If the value is not an ``int``, carrying the exact text the
            C layer produces.

    """
    if not isinstance(value, int):
        raise TypeError(_SANE_INT_TYPE_ERROR)
    # operator.index always returns an exact int, so a bool is stored as 1 or 0
    # and reads back as an int, as it does from the real device.
    return operator.index(value)


def _as_str(key: str, value: object) -> str:
    """
    Coerce a value for a ``TYPE_STRING`` option, or raise.

    Args:
        key: The option name, for the message.
        value: The assigned value.

    Returns:
        The value as a string.

    Raises:
        TypeError: If the value is not a string.

    """
    if not isinstance(value, str):
        msg = f"Option {key} requires a string"
        raise TypeError(msg)
    return value


def _sane_fix(value: float) -> int:
    """
    Convert to a ``SANE_Fixed`` word, as the ``SANE_FIX`` macro does.

    ``SANE_Fixed`` is a 16.16 integer, and the macro's C cast truncates toward
    zero, so a length that is not a multiple of 1/65536 -- letter's 215.9 mm,
    for instance -- loses its low bits on the way in.

    Args:
        value: The number to convert.

    Returns:
        The fixed-point word.

    """
    return int(value * _SANE_FIXED_SCALE)


def _sane_unfix(word: int) -> float:
    """
    Convert a ``SANE_Fixed`` word back to the float python-sane reads out.

    Args:
        word: The fixed-point word.

    Returns:
        The value as a float.

    """
    return word / _SANE_FIXED_SCALE


def _match_string(entries: list[str], value: str) -> str:
    """
    Pick the list entry a string selects, as ``sanei_constrain_value`` does.

    An entry matches when the value is a case-insensitive prefix of it.  An
    entry the value matches in full wins at once, whatever its case; otherwise
    exactly one prefix match wins.  Nothing is stripped, so a leading or
    trailing space is part of the value, and an empty value prefixes every
    entry.

    Args:
        entries: The option's string list.
        value: The (already truncated) value assigned.

    Returns:
        The device's own spelling of the selected entry.

    Raises:
        FakeSaneError: When no entry, or more than one, matches.

    """
    length = len(value)
    folded = value.casefold()
    matches = [
        entry
        for entry in entries
        if length <= len(entry) and entry[:length].casefold() == folded
    ]
    for entry in matches:
        if len(entry) == length:
            return entry
    if len(matches) == 1:
        return matches[0]
    raise FakeSaneError(_INVALID_ARGUMENT)


def _nearest_word(word: int, words: list[int]) -> int:
    """
    Snap a word to the nearest member of a word list.

    Ties keep the earlier entry, because only a strictly smaller distance
    replaces the best found so far.

    Args:
        word: The requested word.
        words: The word list, in the order the device reports it.

    Returns:
        The member closest to ``word``.

    """
    best = words[0]
    for member in words[1:]:
        if abs(member - word) < abs(best - word):
            best = member
    return best


def _quantise_word(word: int, low: int, high: int, quant: int) -> int:
    """
    Clamp a word to a range, then round it to the range's step.

    Rounding is half up from the bottom of the range, and a step of zero
    means none.  A range never raises: an out-of-range value is silently
    brought inside it.

    Args:
        word: The requested word.
        low: The range's minimum.
        high: The range's maximum.
        quant: The range's step, or 0.

    Returns:
        The word the device stores.

    """
    word = min(max(word, low), high)
    if quant:
        word = min((word - low + quant // 2) // quant * quant + low, high)
    return word


def _constrain_word(word: int, constraint: object) -> int:
    """
    Apply a word list or a range to a word; anything else leaves it alone.

    Args:
        word: The requested word.
        constraint: The option's constraint, already in words.

    Returns:
        The word the device stores.

    """
    if isinstance(constraint, list) and constraint:
        return _nearest_word(word, constraint)
    if isinstance(constraint, tuple):
        low, high, quant = constraint
        return _quantise_word(word, low, high, quant)
    return word


def _fixed_words(constraint: object) -> object:
    """
    Express a ``TYPE_FIXED`` constraint in ``SANE_Fixed`` words.

    Args:
        constraint: A float word list, a float ``(min, max, step)`` range, or
            no constraint.

    Returns:
        The same constraint in words, which is how libsane compares.

    """
    if isinstance(constraint, list):
        return [_sane_fix(entry) for entry in constraint]
    if isinstance(constraint, tuple):
        return tuple(_sane_fix(bound) for bound in constraint)
    return constraint


def _reject_unsettable(option: tuple, key: str) -> None:
    """
    Raise the measured ``AttributeError`` for an option that cannot be set.

    Args:
        option: The nine-element option tuple.
        key: The option name, for the message.

    Raises:
        AttributeError: With the message python-sane produces for buttons,
            groups, inactive options and options not settable by software.

    """
    value_type, cap = option[4], option[7]
    if value_type == _TYPE_BUTTON:
        msg = f"Buttons don't have values: {key}"
        raise AttributeError(msg)
    if value_type == _TYPE_GROUP:
        msg = f"Groups don't have values: {key}"
        raise AttributeError(msg)
    if not _option_is_active(cap):
        msg = f"Inactive option: {key}"
        raise AttributeError(msg)
    if not _option_is_settable(cap):
        msg = f"Option can't be set by software: {key}"
        raise AttributeError(msg)


def _reject_unreadable(option: tuple, key: str) -> None:
    """
    Raise the measured ``AttributeError`` for an option that cannot be read.

    Reads do not check settability -- a read-only option reads back fine.

    Args:
        option: The nine-element option tuple.
        key: The option name, for the message.

    Raises:
        AttributeError: For buttons, groups and inactive options.

    """
    value_type, cap = option[4], option[7]
    if value_type == _TYPE_BUTTON:
        msg = f"Buttons don't have values: {key}"
        raise AttributeError(msg)
    if value_type == _TYPE_GROUP:
        msg = f"Groups don't have values: {key}"
        raise AttributeError(msg)
    if not _option_is_active(cap):
        msg = f"Inactive option: {key}"
        raise AttributeError(msg)


def _constrain(option: tuple, key: str, value: object) -> object:
    """
    Apply the option's type and constraint to an assigned value.

    A port of what python-sane and ``sanei_constrain_value`` do between them.
    Strings are cut to the option's size first, leaving room for the NUL,
    because python-sane copies them into a buffer of that size before libsane
    sees them; a string list then takes a case-insensitive unique prefix.  INT
    and FIXED values are compared as SANE words: a word list snaps to its
    nearest member and a range clamps and quantises.  Only a string list can
    refuse a value; the numeric constraints always store something.

    Args:
        option: The nine-element option tuple.
        key: The option name, for messages.
        value: The assigned value.

    Returns:
        The value the device would store.

    Raises:
        FakeSaneError: If a string list matches no entry, or several.

    """
    value_type, size, constraint = option[4], option[6], option[8]
    if value_type in {_TYPE_INT, _TYPE_BOOL}:
        return _constrain_word(_as_int(value), constraint)
    if value_type == _TYPE_FIXED:
        word = _sane_fix(_as_float(value))
        return _sane_unfix(_constrain_word(word, _fixed_words(constraint)))
    if value_type == _TYPE_STRING:
        text = _as_str(key, value)[: max(size - 1, 0)]
        if isinstance(constraint, list):
            return _match_string(constraint, text)
        return text
    return value


def _page_image(index: int, size: tuple[int, int] = _DEFAULT_PAGE_SIZE) -> Image.Image:
    """
    Build one page with enough variance to survive page validation.

    Args:
        index: Zero-based page number, used to make pages distinguishable.
        size: The ``(width, height)`` pixel size of the page.

    Returns:
        An RGB image well above the backend's 10 KB floor.

    """
    width, height = size
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, width - 10, height - 10), fill="black")
    draw.ellipse((30, 30 + index, width - 30, height - 30 + index), fill="white")
    return image


class FakeSaneError(Exception):
    """
    The local stand-in for ``_sane.error``.

    It subclasses ``Exception`` directly because the real type's MRO is
    ``(error, Exception, BaseException, object)``.  Subclassing ``OSError`` or
    one of saneless's own exceptions would make the backend's translation
    layer look correct when it is not.
    """


class _FakeSaneIterator:
    """
    The ADF iterator, mirroring ``sane._SaneIterator``.

    It calls ``start()`` then ``snap()`` once per page.  A double that returns
    ``iter(list)`` instead cannot show the per-page call pattern, which is
    where the backend's per-page error handling lives.

    Like the real one it cancels the handle that made it when it is
    finalised, and swallows whatever that cancel raises -- on a closed handle,
    "SaneDev object is closed".  So dropping an iterator is itself a cancel,
    sent from whichever thread lets go of the last reference.
    """

    def __init__(self, handle: FakeSaneDev | FakeSaneHandle) -> None:
        """
        Wrap the handle that made the iterator.

        Args:
            handle: The handle to drive, and to cancel when finalised.

        """
        self._handle = handle

    def __iter__(self) -> _FakeSaneIterator:
        """Return self, as the real iterator does."""
        return self

    def __del__(self) -> None:
        """Cancel the handle, swallowing any error, as ``sane.py`` does."""
        with contextlib.suppress(Exception):
            self._handle.cancel()

    def __next__(self) -> Image.Image:
        """
        Scan one page.

        Returns:
            The next page image.

        Raises:
            StopIteration: When the feeder reports it is out of documents.

        """
        try:
            self._handle.start()
            return self._handle.snap(no_cancel=True)
        except Exception as exc:
            if str(exc) == _FEEDER_EMPTY_MESSAGE:
                raise StopIteration from None
            raise


class FakeSaneDev:
    """
    A SANE device that behaves like the real one, and the state its handles share.

    ``FakeSaneModule.open()`` wraps it in a :class:`FakeSaneHandle`; a test
    can also drive it directly, as a handle that is never closed.  Configure it through the constructor rather than by subclassing, so that a
    later plan can narrow a constraint or arm an error without creating a
    fourth divergent double.
    """

    # The options the default table serves, declared with the types the real
    # device hands them back as.
    #
    # These are annotations only: no value is assigned, so attribute lookup
    # still falls through to __getattr__ at runtime and the dynamic behaviour is
    # completely unchanged.  Declaring them is what lets the fake be passed to
    # production code that expects the ``SaneDevice`` protocol.  The real
    # python-sane object serves these through __getattr__ too, so a purely
    # structural check against it can never succeed -- and the three deleted
    # doubles satisfied the protocol only by declaring concrete attributes the
    # real library does not have: a fake kinder than the library, in
    # miniature.
    mode: str
    resolution: float
    source: str
    tl_x: float
    tl_y: float
    br_x: float
    br_y: float

    opt: dict[str, tuple]
    calls: list[str]
    assignments: list[str]
    cancel_calls: int
    close_calls: int
    get_options_calls: int
    get_parameters_calls: int
    issued_pages: list[weakref.ref[Image.Image]]
    high_water_live_pages: int
    read_gate: threading.Event
    read_started: threading.Event
    close_while_blocked: bool
    _block_mode: ReadBlockMode | None
    _blocked_readers: int
    _options: list[tuple]
    _values: dict[str, Any]
    _pages: int
    _start_error: BaseException | None
    _start_error_page: int
    _page_index: int
    _page_size: tuple[int, int]
    _page_images: list[Image.Image]
    _source_resolution_ranges: dict[str, tuple[float, float, float]]
    _call_errors: dict[str, BaseException]
    _assignment_errors: dict[str, BaseException]
    _read_errors: dict[str, BaseException]
    _parameter_overrides: dict[str, int | str]

    def __init__(
        self,
        *,
        options: list[tuple] | None = None,
        pages: int = 3,
        start_error: BaseException | None = None,
        start_error_page: int = 0,
        geometry_range: tuple[float, float, float] = _DEFAULT_GEOMETRY_RANGE,
    ) -> None:
        """
        Create a device.

        Args:
            options: A full option table.  When given, ``geometry_range`` is
                ignored because the table already carries its constraints.
            pages: How many sheets the feeder holds.
            start_error: An exception to raise from ``start()``.  Use
                ``FakeSaneError("Document feeder out of documents")`` for a
                clean end of feed and any other message for a real failure.
            start_error_page: The zero-based page index at which
                ``start_error`` fires.
            geometry_range: The ``(min, max, step)`` constraint for the four
                geometry options.  The default admits A4; pass
                ``(0.0, 200.0, 1.0)`` to reproduce ``test:0``'s measured clamp.

        """
        state = self.__dict__
        table = (
            _build_option_table(geometry_range=geometry_range)
            if options is None
            else list(options)
        )
        state["_options"] = table
        state["opt"] = {option[1].replace("-", "_"): option for option in table}
        state["_values"] = _default_values(table)
        state["_pages"] = pages
        state["_start_error"] = start_error
        state["_start_error_page"] = start_error_page
        state["_page_index"] = 0
        state["_page_size"] = _DEFAULT_PAGE_SIZE
        state["_page_images"] = []
        state["_source_resolution_ranges"] = {}
        state["_call_errors"] = {}
        state["_assignment_errors"] = {}
        state["_read_errors"] = {}
        state["_parameter_overrides"] = {}
        state["calls"] = []
        state["assignments"] = []
        state["cancel_calls"] = 0
        state["close_calls"] = 0
        state["get_options_calls"] = 0
        state["get_parameters_calls"] = 0
        state["issued_pages"] = []
        state["high_water_live_pages"] = 0
        state["read_gate"] = threading.Event()
        state["read_started"] = threading.Event()
        state["close_while_blocked"] = False
        state["_block_mode"] = None
        state["_blocked_readers"] = 0

    def __setattr__(self, key: str, value: object) -> None:
        """
        Store or validate an assignment, following ``sane.py:188-213``.

        Args:
            key: The attribute or option name.
            value: The value assigned.

        Raises:
            AttributeError: For a read-only attribute, a button, a group, an
                inactive option or one not settable by software.
            BaseException: The error armed for ``key`` with
                ``fail_assignment``.

        """
        if key in _READ_ONLY_ATTRIBUTES:
            msg = f"Read-only attribute: {key}"
            raise AttributeError(msg)
        armed = self.__dict__.get("_assignment_errors", {}).get(key)
        if armed is not None:
            raise armed
        option = self.__dict__.get("opt", {}).get(key)
        if option is None:
            # The row that matters most: an unrecognised name is stored
            # silently, with no device call and no validation.
            self.__dict__[key] = value
            return
        _reject_unsettable(option, key)
        self.__dict__["_values"][key] = _constrain(option, key, value)
        # Recorded only for names the device actually has: an unrecognised
        # name is stored with no device call, so logging it would invent one.
        self.__dict__["assignments"].append(key)
        if key == "source":
            self._reload_for_source(str(value))

    def narrow_resolution_for_source(
        self, source: str, constraint: tuple[float, float, float]
    ) -> None:
        """
        Arm a narrower resolution range that selecting a given source reveals.

        Real feeders commonly cap resolution below the platen's ceiling, and a
        source change reloads every option descriptor (``sane.py:188-213``).

        This is a method rather than a constructor keyword because ``__init__``
        already carries ruff's maximum of five arguments (``PLR0913``) and this
        project forbids suppressing the rule.

        Args:
            source: The source name whose selection narrows the range.
            constraint: The ``(min, max, step)`` the device reports afterwards.

        """
        self.__dict__["_source_resolution_ranges"][source] = constraint

    def load_feeder(self, pages: list[Image.Image]) -> None:
        """
        Load exact page images into the feeder, replacing whatever it held.

        ``snap()`` otherwise returns a generated page carrying enough variance
        to clear the backend's integrity checks, which is right for a test about
        routing and useless for a test about the checks themselves.  A test that
        means to feed a zero-dimension sheet, a sheet below the byte floor, or a
        uniformly blank one has to supply that exact image.

        Loading also rewinds the feeder, which is the physical act it models: a
        stack taken out and put back in.  That is what makes a two-pass manual
        duplex run drivable through a single device handle, since each pass
        opens the device and drains the feeder to its end.

        A method rather than a constructor keyword for the same reason
        ``narrow_resolution_for_source`` and ``set_page_size`` are methods:
        ``__init__`` already carries ruff's maximum of five arguments and this
        project forbids suppressing the rule.

        Args:
            pages: The images the feeder should hand back, in order.

        """
        self.__dict__["_page_images"] = list(pages)
        self.__dict__["_pages"] = len(pages)
        self.__dict__["_page_index"] = 0

    def block_read(self, mode: ReadBlockMode) -> None:
        """
        Arm the Event-gated blocking read, so a cancel can be exercised.

        A read that has started and has not come back is a real condition --
        a slow page and a scanner that stopped answering both look like this
        to the backend -- so it belongs in the one shared device rather than
        in a bespoke blocking iterator written per test: a hand-rolled blocking
        double is a device handle of its own, and this module exists so there
        is exactly one of those.

        It costs no wall-clock time at all.  The blocked ``snap()`` waits on
        ``read_gate``, so a test observes the block and ends it by setting an
        ``Event`` rather than by outwaiting a sleep; the suite has no sleeps.

        A method rather than a constructor keyword for the usual reason:
        ``__init__`` already carries ruff's maximum of five arguments.

        Two events rather than one, because a test needs both edges: the
        blocked read sets ``read_started`` on the way in and waits on
        ``read_gate`` on the way out, so "a read is now blocked" and "let it
        go" are separately observable.  One event could not do both -- the
        gate is unset for precisely as long as the block lasts.

        Args:
            mode: What the blocked read does when its gate is released, and
                whether ``cancel()`` releases it at all.

        """
        self.__dict__["_block_mode"] = mode
        self.__dict__["read_gate"].clear()
        self.__dict__["read_started"].clear()

    def release_read(self) -> None:
        """
        Release the gate, whatever the armed mode is.

        ``cancel()`` releases it only in the two modes that model a scanner
        which answered.  This is the other way a blocked read ends: the late
        answer that arrives after the backend has already given up on it, and
        the one that lets a test watch a wedged backend recover.
        """
        self.__dict__["read_gate"].set()

    def read_is_blocked(self) -> bool:
        """
        Report whether a reader is inside the gate right now.

        Read by ``close()`` and by ``FakeSaneModule.exit()``, which is the
        whole point: both are operations the SANE standard forbids while a
        read is outstanding, so the fake records them happening rather than
        refusing them, and the test asserts they did not happen.

        Returns:
            True while at least one ``snap()`` is waiting on ``read_gate``.

        """
        return self._blocked_readers > 0

    def _truncated_page(self) -> Image.Image:
        """
        Build the partial page a cancelled read hands back.

        Full width, a fraction of the height, and still above the backend's
        byte floor -- see ``_TRUNCATED_PAGE_DIVISOR`` for the measurement this
        models.  Mode ``RGB``, like every other page this fake produces, so the
        backend's byte-count arithmetic stays exact.

        Returns:
            A short page that would pass ``_validate_page_image``.

        """
        width, height = self._page_size
        short = max(1, height // _TRUNCATED_PAGE_DIVISOR)
        return _page_image(self._page_index, (width, short))

    def _await_gate(self, mode: ReadBlockMode) -> Image.Image:
        """
        Block until the gate opens, then end the read the armed way.

        Args:
            mode: The armed mode.

        Returns:
            The truncated page, for the two modes that return one.

        Raises:
            FakeSaneError: In ``RAISE`` mode, as a backend reporting a status
                that is neither GOOD nor EOF does.

        """
        self.__dict__["_blocked_readers"] += 1
        # Set from inside the reader, so a test that has to act *while* a read
        # is blocked -- delivering a SIGINT, say -- waits on a real event
        # instead of polling ``calls`` behind a sleep.  ``read_gate`` cannot
        # serve: it is the event the reader is about to wait ON, and it is
        # unset for exactly as long as the block lasts.
        self.__dict__["read_started"].set()
        try:
            self.read_gate.wait(_READ_GATE_CEILING_SECONDS)
        finally:
            self.__dict__["_blocked_readers"] -= 1
        match mode:
            case ReadBlockMode.RAISE:
                raise FakeSaneError(_CANCELLED_MESSAGE)
            case ReadBlockMode.PARTIAL | ReadBlockMode.NEVER:
                return self._truncated_page()
            case _:
                assert_never(mode)

    def live_page_images(self) -> int:
        """
        Count the page images this device handed out that are still alive.

        This is the proof instrument for bounded page memory.  Every page
        ``snap()`` returns is recorded in ``issued_pages`` as a
        ``weakref.ref``, and this method calls each one: a reference whose
        referent has been collected answers ``None``, so what is counted is
        exactly the pages something else is still holding.

        Two measured facts decided the mechanism, and neither is a style
        preference:

        1. ``weakref``'s hash-based *set* container cannot hold Pillow images.
           ``Image`` defines ``__eq__`` without ``__hash__``, so it is
           unhashable and building one raises
           ``TypeError: unhashable type: 'Image'``.  A plain list of
           ``weakref.ref`` is used instead, and must stay one -- do not
           "tidy" it into a set.
        2. ``tracemalloc`` cannot see the memory in question.  A 26 MB Pillow
           image adds **460 bytes** to the traced total, because its pixels are
           malloc'd in C rather than allocated by the Python allocator.  RSS is
           the other obvious instrument and is far too noisy for CI.

        What this counter does *not* see is worth stating, so nobody reads the
        number as an allocation ceiling: ``snap()`` itself transiently holds
        three references to a page, and ``crop`` and ``convert("L")`` each
        produce one more decoded image downstream.  The real decoded ceiling is
        therefore roughly two to three pages -- but it is **constant in N**,
        and that constancy is the claim the memory tests actually defend.

        Returns:
            How many issued page images have not yet been collected.

        """
        return sum(1 for reference in self.issued_pages if reference() is not None)

    def fail_call(self, method: str, error: BaseException) -> None:
        """
        Arm a device method to raise, so the backend's translation is exercised.

        python-sane raises ``_sane.error``, ``RuntimeError`` or
        ``AttributeError`` from its device methods with no shared base, and the
        backend has to turn each into a saneless type naming the device.
        A method rather than a constructor keyword for the usual reason:
        ``__init__`` already carries ruff's maximum of five arguments.

        ``snap`` and ``close`` record their call before raising, as they do when
        they succeed, so a test can still assert the call pattern -- and for
        ``close``, that the handle was released and that the close failure did
        not mask anything.  ``get_options`` and ``get_parameters`` are not
        recorded in ``calls`` at all; each has a counter of its own.

        Args:
            method: The device method to fail: ``"get_options"``,
                ``"get_parameters"``, ``"snap"`` or ``"close"``.  A ``start()``
                failure keeps its own constructor keyword, because it needs a
                page index.
            error: The exception that method raises.

        """
        self.__dict__["_call_errors"][method] = error

    def fail_assignment(self, option: str, error: BaseException) -> None:
        """
        Arm one option assignment to raise, whatever the option table says.

        A real device refuses an assignment for reasons the table cannot always
        express -- ``_sane.error("Invalid argument")`` from the backend, or an
        option that went inactive after a reload.  The backend must name the
        option and the value when that happens.

        Args:
            option: The underscore-spelled option name, e.g. ``"mode"``.
            error: The exception the assignment raises.  Nothing is stored.

        """
        self.__dict__["_assignment_errors"][option] = error

    def fail_read(self, option: str, error: BaseException) -> None:
        """
        Arm one option read to raise, as a read-back of an inactive option does.

        Args:
            option: The underscore-spelled option name, e.g. ``"resolution"``.
            error: The exception reading the option raises.

        """
        self.__dict__["_read_errors"][option] = error

    def report_sources(self, sources: list[str]) -> None:
        """
        Replace the list constraint the device reports for ``source``.

        The default table carries three realistic source names.  A test needing
        one outside them -- this project's "ADF Manual Duplex" pseudo-source, or
        a name chosen to exercise the classifier -- narrows the constraint here
        rather than by subclassing the fake, which would reintroduce exactly the
        per-file drift a single shared double exists to prevent.

        Args:
            sources: The source names the device should report.

        """
        self._replace_constraint("source", list(sources))

    def offer_depth(self, depths: list[int] | None = None) -> None:
        """
        Offer a ``depth`` option: an INT word list, as ``test:0`` reports it.

        Opt-in, because most scanner tests use the default table and a
        ``depth`` option there would add an assignment to every sequence they
        expect.  The stored value is the list's first entry, as a device left
        at that depth would report it, so ``offer_depth([16])`` is a device
        that can only scan at 16 bits.

        A method rather than a constructor keyword for the usual reason:
        ``__init__`` already carries ruff's maximum of five arguments.

        Args:
            depths: The bit depths the device lists.  Defaults to ``test:0``'s
                ``[1, 8, 16]``.

        """
        values = list(_TEST0_DEPTHS if depths is None else depths)
        options = self.__dict__["_options"]
        if any(option[1] == "depth" for option in options):
            self._replace_constraint("depth", values)
            return
        index = max((option[0] for option in options), default=0) + 1
        option = (
            index,
            "depth",
            "Bit depth",
            "Number of bits per sample.",
            _TYPE_INT,
            _UNIT_NONE,
            4,
            _CAP_SETTABLE,
            values,
        )
        options.append(option)
        self.__dict__["opt"]["depth"] = option
        if values:
            self.__dict__["_values"]["depth"] = values[0]

    def _replace_constraint(self, name: str, constraint: object) -> None:
        """
        Swap one option's constraint, keeping the lookup table consistent.

        A string option's size is recomputed from a new string list, as a
        device reports it: a size left over from a shorter list would cut the
        new names down before they were matched.

        Args:
            name: The hyphenated option name, as ``get_options()`` reports it.
            constraint: The constraint the device should report from now on.

        """
        options = self.__dict__["_options"]
        key = name.replace("-", "_")
        for index, option in enumerate(options):
            if option[1] == name:
                size = (
                    _string_size(constraint)
                    if option[4] == _TYPE_STRING and isinstance(constraint, list)
                    else option[6]
                )
                replaced = (*option[:6], size, option[7], constraint)
                options[index] = replaced
                self.__dict__["opt"][key] = replaced
                if isinstance(constraint, list) and constraint:
                    self.__dict__["_values"][key] = constraint[0]
                break

    def set_page_size(self, width: int, height: int) -> None:
        """
        Set the pixel size of the pages ``snap()`` returns.

        A method rather than a constructor keyword for the same reason
        ``narrow_resolution_for_source`` is one: ``__init__`` already carries
        ruff's five-argument maximum.

        The default 200x300 page is smaller than any paper size at a realistic
        dpi, so ``crop_to_paper_size`` clamps the crop box to the image and
        returns it unchanged.  A test that means to prove the crop fallback
        actually *ran* needs a page larger than the crop box, which is what this
        provides.

        Args:
            width: Page width in pixels.
            height: Page height in pixels.

        """
        self.__dict__["_page_size"] = (width, height)

    def set_parameters(self, **fields: int | str) -> None:
        """
        Override what ``get_parameters()`` reports, without building that page.

        A test can then report a 16-bit frame from a device with no ``depth``
        option -- a mode that implies 16 bits -- or a page too large to build,
        and the code reading the parameters sees it.  Overrides accumulate
        across calls.  Bytes per line are derived from the overridden format,
        width and depth unless they are overridden as well.

        Keyword arguments rather than a parameter each, because six would put
        this over ruff's five-argument maximum.

        Args:
            **fields: Any of ``frame_format``, ``last_frame``,
                ``pixels_per_line``, ``lines``, ``depth`` and
                ``bytes_per_line``.

        Raises:
            TypeError: For a name that is not one of those, so a misspelt
                override cannot pass silently.

        """
        unknown = sorted(set(fields) - _PARAMETER_FIELDS)
        if unknown:
            msg = f"Not a scan parameter: {', '.join(unknown)}"
            raise TypeError(msg)
        self.__dict__["_parameter_overrides"].update(fields)

    def _reload_for_source(self, source: str) -> None:
        """
        Swap in the source's resolution constraint, as an option reload would.

        The value already stored is deliberately **not** re-validated against
        the new constraint.  That is the entire hazard: a resolution accepted
        against the platen's range stands unchanged against the feeder's
        narrower one, so only assigning the source first keeps it legal.

        Args:
            source: The source name just assigned.

        """
        narrowed = self.__dict__["_source_resolution_ranges"].get(source)
        if narrowed is None:
            return
        options = self.__dict__["_options"]
        for index, option in enumerate(options):
            if option[1] == "resolution":
                reloaded = (*option[:8], narrowed)
                options[index] = reloaded
                self.__dict__["opt"]["resolution"] = reloaded
                break

    def __getattr__(self, key: str) -> object:
        """
        Read an option value, following ``sane.py:215-236``.

        Args:
            key: The option name.

        Returns:
            The stored value.

        Raises:
            AttributeError: For a button, a group, an inactive option, or a
                name the device does not have at all.
            BaseException: The error armed for ``key`` with ``fail_read``.

        """
        armed = self.__dict__.get("_read_errors", {}).get(key)
        if armed is not None:
            raise armed
        option = self.__dict__.get("opt", {}).get(key)
        if option is None:
            msg = f"No such attribute: {key}"
            raise AttributeError(msg)
        _reject_unreadable(option, key)
        return self.__dict__["_values"][key]

    @property
    def area(self) -> tuple[tuple[float, float], tuple[float, float]]:
        """
        The scan area, reflecting whatever clamping the device applied.

        Composed from attribute reads, exactly as ``sane.py:220`` does, so a
        device whose option table omits the geometry options raises
        ``AttributeError("No such attribute: tl_x")``.  Reading ``_values``
        directly raised ``KeyError`` instead -- an exception the real library
        never raises here, and precisely the species of quiet divergence this
        module exists to eliminate.  No production path reaches it today,
        because ``_set_geometry`` checks presence before reading ``area``, but
        that ordering is a property of today's code rather than a guarantee.

        Returns:
            The ``((tl_x, tl_y), (br_x, br_y))`` box.

        Raises:
            AttributeError: If the device does not report the geometry options.

        """
        return (
            (self.tl_x, self.tl_y),
            (self.br_x, self.br_y),
        )

    @property
    def optlist(self) -> list[str]:
        """The option names, in python-sane's underscore spelling."""
        return list(self.opt.keys())

    @property
    def sane_signature(self) -> tuple[str, str, str, str]:
        """The ``(devname, brand, name, type)`` tuple."""
        return _DEVICE_TUPLE

    @property
    def scanner_model(self) -> tuple[str, str]:
        """The ``(brand, name)`` pair."""
        return _DEVICE_TUPLE[1], _DEVICE_TUPLE[2]

    def get_options(self) -> list[tuple]:
        """
        Return the device's option tuples.

        Returns:
            Nine-element tuples whose names are hyphenated.

        Raises:
            BaseException: The error armed with ``fail_call("get_options", ...)``,
                after the call has been counted.

        """
        self.__dict__["get_options_calls"] += 1
        error = self._call_errors.get("get_options")
        if error is not None:
            raise error
        return list(self._options)

    def get_parameters(self) -> tuple[str, int, tuple[int, int], int, int]:
        """
        Report the frame the device is set up to scan, as ``sane.py`` does.

        Derived from the mode, the page size and the ``depth`` option (8 when
        the table has none), then overridden by whatever ``set_parameters()``
        set.  See the module docstring, rule 9, for how each element is
        derived; the contract table checks them against ``test:0``.

        Returns:
            ``(format, last_frame, (pixels_per_line, lines), depth,
            bytes_per_line)``.

        Raises:
            BaseException: The error armed with
                ``fail_call("get_parameters", ...)``, after the call has been
                counted.

        """
        self.__dict__["get_parameters_calls"] += 1
        error = self._call_errors.get("get_parameters")
        if error is not None:
            raise error
        overrides = self._parameter_overrides
        values = self._values
        default_format = (
            "color" if "color" in str(values.get("mode", "")).casefold() else "gray"
        )
        width, height = self._page_size
        frame_format = str(overrides.get("frame_format", default_format))
        pixels_per_line = int(overrides.get("pixels_per_line", width))
        depth = int(overrides.get("depth", values.get("depth", _DEFAULT_DEPTH)))
        samples = _COLOR_SAMPLES if frame_format == "color" else 1
        # Rounded up to whole bytes: ``test:0`` reports 30 for a 236-pixel
        # line at 1 bit.
        derived_bytes = -(-pixels_per_line * samples * depth // 8)
        return (
            frame_format,
            int(overrides.get("last_frame", 1)),
            (pixels_per_line, int(overrides.get("lines", height))),
            depth,
            int(overrides.get("bytes_per_line", derived_bytes)),
        )

    def start(self) -> None:
        """
        Begin one page, raising whatever the device is configured to raise.

        Raises:
            BaseException: The configured ``start_error`` at its page index.
            FakeSaneError: Carrying the measured end-of-feed message once the
                page budget is exhausted.

        """
        self.calls.append("start")
        # The armed error is checked before the page budget so that arming it
        # at the index one past the last page -- the end-of-feed probe -- is
        # reachable at all.  With the budget first, ``FakeSaneDev(pages=3,
        # start_error=..., start_error_page=3)`` never fired: the test quietly
        # became a clean-feed test rather than failing as a misconfiguration.
        if self._start_error is not None and self._page_index == self._start_error_page:
            raise self._start_error
        if self._page_index >= self._pages:
            raise FakeSaneError(_FEEDER_EMPTY_MESSAGE)

    def snap(self, *, no_cancel: bool = False) -> Image.Image:
        """
        Return the current page and advance the feeder.

        ``no_cancel`` is keyword-only here.  The real signature takes it
        positionally, but a positional boolean is not allowed by this
        project's lint rules and nothing outside this module passes it.

        When a blocking mode is armed this is where the block lives, because
        this is where the real ``snap()`` sits in ``sane_read``: ``start()``
        opens the data channel and returns, and it is the read loop that hangs
        when a device stops answering.

        Args:
            no_cancel: Accepted for signature compatibility; unused.

        Returns:
            The page image -- the one ``load_feeder()`` supplied for this
            position, a generated page when the feeder was not loaded with
            exact images, or the truncated page a cancelled read hands back.

        Raises:
            BaseException: The error armed with ``fail_call("snap", ...)``,
                raised after the call is recorded, as the real ``snap()``
                raises ``RuntimeError("Scanner returned no data")``.

        """
        self.calls.append("snap")
        error = self._call_errors.get("snap")
        if error is not None:
            raise error
        mode = self._block_mode
        if mode is None:
            loaded = self._page_images
            index = self._page_index
            page = (
                loaded[index]
                if index < len(loaded)
                else _page_image(index, self._page_size)
            )
        else:
            page = self._await_gate(mode)
        self._page_index += 1
        # Sampled here, immediately before the page leaves the device, so the
        # page about to be returned is itself counted.  That is deliberate: it
        # is what makes 2 the honest high-water mark, because the backend's
        # loop variable still references page k-1 at the moment page k is
        # handed over.  See live_page_images() for why a weakref list
        # and not a weak-reference set, and why not tracemalloc.
        self.issued_pages.append(weakref.ref(page))
        live = self.live_page_images()
        if live > self.high_water_live_pages:
            self.__dict__["high_water_live_pages"] = live
        return page

    def multi_scan(self) -> Iterator[Image.Image]:
        """
        Return the ADF iterator.

        This **cannot** raise: like the real method it only constructs the
        iterator, so any configured failure surfaces from ``next()`` instead.

        Returns:
            An iterator over the feeder's pages.

        """
        return _FakeSaneIterator(self)

    def cancel(self) -> None:
        """
        Record the cancel, and release a gated read when the mode says so.

        ``sane_cancel`` releases the GIL in python-sane, so the real call can
        be made from a second thread while a read blocks in the first -- which
        is exactly what the backend does, and what this models.  Whether the
        read then comes back is the scanner's answer, not the frontend's, so
        it is the armed ``ReadBlockMode`` and not this method that decides.
        """
        self.cancel_calls += 1
        if self._block_mode in {ReadBlockMode.PARTIAL, ReadBlockMode.RAISE}:
            self.__dict__["read_gate"].set()

    def close(self) -> None:
        """
        Record that the device was closed, and whether a read was blocked.

        ``close_while_blocked`` is what the close-ordering tests assert on.
        The SANE standard forbids any other operation while a read is
        outstanding, and ``sane_close`` additionally runs holding the GIL, so a
        close racing a read is doubly unsafe.  The fake records it rather than
        refusing it: a double that refused would turn the defect into an
        exception the backend could catch, instead of the silent corruption it
        really is.

        Raises:
            BaseException: The error armed with ``fail_call("close", ...)``,
                after the call has been counted.

        """
        if self.read_is_blocked():
            self.__dict__["close_while_blocked"] = True
        self.close_calls += 1
        error = self._call_errors.get("close")
        if error is not None:
            raise error


class FakeSaneHandle:
    """
    One open handle to a :class:`FakeSaneDev`, as ``FakeSaneModule.open()`` makes.

    python-sane's ``open()`` returns a new ``SaneDev`` every time, and the
    device keeps its option values from one handle to the next.  So the state
    lives on the device -- options, feeder, armed errors, block mode and every
    counter a test inspects -- and a handle only forwards to it and owns one
    thing of its own: whether it is closed.

    Once closed, every call but ``close()`` raises the SANE error "SaneDev
    object is closed" before it reaches the device, so nothing is counted or
    changed; closing again does nothing.  A name that is not one of the
    device's options is stored on the handle itself, as python-sane stores it,
    so assigning ``cancel`` shadows the method on this handle alone.
    """

    # The options the default table serves, declared for the ``SaneDevice``
    # protocol exactly as on ``FakeSaneDev``: annotations only, so reads and
    # writes still go through __getattr__ and __setattr__.
    mode: str
    resolution: float
    source: str
    tl_x: float
    tl_y: float
    br_x: float
    br_y: float

    _device: FakeSaneDev
    _closed: bool

    def __init__(self, device: FakeSaneDev) -> None:
        """
        Open a handle to a device.

        Args:
            device: The device the handle forwards to.

        """
        self.__dict__["_device"] = device
        self.__dict__["_closed"] = False

    def _refuse_if_closed(self) -> None:
        """
        Raise the SANE error for a call on a closed handle.

        Raises:
            FakeSaneError: If the handle has been closed.

        """
        if self._closed:
            raise FakeSaneError(_CLOSED_MESSAGE)

    def __setattr__(self, key: str, value: object) -> None:
        """
        Assign an option through the device, following ``sane.py:188-213``.

        Args:
            key: The attribute or option name.
            value: The value assigned.

        Raises:
            AttributeError: For a read-only attribute, or an option that
                cannot be set.
            FakeSaneError: For an option assigned on a closed handle.

        """
        if key in _READ_ONLY_ATTRIBUTES:
            msg = f"Read-only attribute: {key}"
            raise AttributeError(msg)
        option = self._device.opt.get(key)
        if option is None:
            # No device call at all, so a closed handle stores it too.
            self.__dict__[key] = value
            return
        if self._closed:
            _reject_unsettable(option, key)
            raise FakeSaneError(_CLOSED_MESSAGE)
        setattr(self._device, key, value)

    def __getattr__(self, key: str) -> object:
        """
        Read an option, or anything else, from the device.

        Reached only for names the handle does not hold itself.  An option
        read on a closed handle raises; the double's own instrumentation
        (``calls``, ``cancel_calls`` and the rest) reads through regardless,
        because reading it is not a SANE call.

        Args:
            key: The option or attribute name.

        Returns:
            What the device holds under that name.

        Raises:
            FakeSaneError: For an option read on a closed handle.

        """
        device = self._device
        option = device.opt.get(key)
        if option is not None and self._closed:
            _reject_unreadable(option, key)
            raise FakeSaneError(_CLOSED_MESSAGE)
        return getattr(device, key)

    @property
    def area(self) -> tuple[tuple[float, float], tuple[float, float]]:
        """The scan area, composed from option reads as ``sane.py:220`` does."""
        return (
            (self.tl_x, self.tl_y),
            (self.br_x, self.br_y),
        )

    @property
    def optlist(self) -> list[str]:
        """The option names; python-sane reads these without a device call."""
        return self._device.optlist

    @property
    def sane_signature(self) -> tuple[str, str, str, str]:
        """The ``(devname, brand, name, type)`` tuple."""
        return self._device.sane_signature

    @property
    def scanner_model(self) -> tuple[str, str]:
        """The ``(brand, name)`` pair."""
        return self._device.scanner_model

    def get_options(self) -> list[tuple]:
        """
        Return the device's option tuples.

        Returns:
            Nine-element tuples whose names are hyphenated.

        """
        self._refuse_if_closed()
        return self._device.get_options()

    def get_parameters(self) -> tuple[str, int, tuple[int, int], int, int]:
        """
        Report the frame the device is set up to scan.

        Returns:
            ``(format, last_frame, (pixels_per_line, lines), depth,
            bytes_per_line)``.

        """
        self._refuse_if_closed()
        return self._device.get_parameters()

    def start(self) -> None:
        """Begin one page on the device."""
        self._refuse_if_closed()
        self._device.start()

    def snap(self, *, no_cancel: bool = False) -> Image.Image:
        """
        Read the current page from the device.

        Args:
            no_cancel: Passed through to the device.

        Returns:
            The page image.

        """
        self._refuse_if_closed()
        return self._device.snap(no_cancel=no_cancel)

    def multi_scan(self) -> Iterator[Image.Image]:
        """
        Return the ADF iterator, bound to this handle.

        This cannot raise, even on a closed handle: the refusal comes from
        the iterator's first ``next()``.

        Returns:
            An iterator over the feeder's pages.

        """
        return _FakeSaneIterator(self)

    def cancel(self) -> None:
        """Cancel through the device."""
        self._refuse_if_closed()
        self._device.cancel()

    def close(self) -> None:
        """
        Close the handle; a second close does nothing.

        The handle counts as closed even when the device's ``close()`` raises,
        as python-sane drops its SANE handle either way.
        """
        if self._closed:
            return
        self.__dict__["_closed"] = True
        self._device.close()


class FakeSaneModule:
    """
    A stand-in for the ``sane`` module, for ``monkeypatch.setattr`` seams.

    It mirrors the shape of the module the backend actually uses: ``init()``,
    ``get_devices()``, ``open()`` and ``exit()``.
    """

    def __init__(
        self,
        *,
        device: FakeSaneDev | None = None,
        devices: list[tuple[str, str, str, str]] | None = None,
        init_error: BaseException | None = None,
        open_error: BaseException | None = None,
        get_devices_error: BaseException | None = None,
    ) -> None:
        """
        Create the module double.

        Args:
            device: The device every handle ``open()`` returns shares.
            devices: The four-element device tuples ``get_devices()`` returns.
            init_error: An exception ``init()`` raises after counting the call,
                as a SANE that cannot start (``_sane.error``) would.
            open_error: An exception ``open()`` raises, as the real module does
                with ``_sane.error("Invalid argument")`` for an unknown device.
            get_devices_error: An exception ``get_devices()`` raises.

        """
        self.init_call_count = 0
        self.exit_call_count = 0
        # Counted for the same reason FakeSaneDev records its own calls: the
        # wedge refusal has to happen *before* any SANE traffic, and
        # "the call was never made" cannot be asserted on a return value.
        self.get_devices_call_count = 0
        self.exit_while_blocked = False
        self._init_error = init_error
        self._open_error = open_error
        self._get_devices_error = get_devices_error
        self._device = FakeSaneDev() if device is None else device
        self._devices = (
            [_DEVICE_TUPLE, ("test:1", "TestVendor", "TestModel", "scanner")]
            if devices is None
            else list(devices)
        )

    @property
    def device(self) -> FakeSaneDev:
        """
        The device itself, for a test to arrange and inspect.

        Fetching it this way opens nothing, so a test that configures the
        device before the code under test runs is not counted as an open.
        """
        return self._device

    def init(self) -> tuple[int, int, int, int]:
        """
        Record the call and report a SANE version.

        The shape is python-sane's: the packed version code, then its major,
        minor and build.  The numbers are the ones libsane 1.0.32 returns.

        Returns:
            ``(version_code, major, minor, build)``.

        Raises:
            BaseException: The configured ``init_error``.

        """
        self.init_call_count += 1
        if self._init_error is not None:
            raise self._init_error
        major, minor, build = _SANE_VERSION
        return (major << 24 | minor << 16 | build, major, minor, build)

    def get_devices(self) -> list[tuple[str, str, str, str]]:
        """
        Enumerate devices.

        Returns:
            Four-element ``(name, vendor, model, type)`` tuples.

        Raises:
            BaseException: The configured ``get_devices_error``.

        """
        self.get_devices_call_count += 1
        if self._get_devices_error is not None:
            raise self._get_devices_error
        return list(self._devices)

    def open(self, device_id: str) -> FakeSaneHandle:
        """
        Open a new handle to the device.

        Args:
            device_id: Ignored; there is one device, and every handle opened
                on it shares its state, so a value one handle set is still set
                on the next.

        Returns:
            A new handle, as the real ``open()`` returns a new ``SaneDev``.

        Raises:
            BaseException: The configured ``open_error``.

        """
        if self._open_error is not None:
            raise self._open_error
        return FakeSaneHandle(self._device)

    def exit(self) -> None:
        """
        Record the shutdown call, and whether a read was blocked at the time.

        ``sane_exit`` closes every handle that is still open **and** runs
        holding the GIL, so calling it while a read is outstanding is the same
        hazard as ``close()`` and then some.  ``exit_while_blocked`` records
        it; ``exit_call_count`` keeps the meaning the shutdown assertions
        already read it with, unchanged.
        """
        if self._device.read_is_blocked():
            self.exit_while_blocked = True
        self.exit_call_count += 1
