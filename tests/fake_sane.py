"""
One faithful double for python-sane 2.9.2, shared by every test that needs one.

This module exists because three hand-written doubles disagreed with the real
library and with each other, and each disagreement let a shipped defect earn a
green test (M-32).  The rules below are not invented: every one was executed
against python-sane 2.9.2 and recorded in ``24-RESEARCH.md`` Finding 7.  The
specification is ``.venv/lib/python3.14/site-packages/sane.py`` -- lines
107-134 for the ADF iterator and 188-236 for attribute access -- not this
docstring.

The five behaviours a double gets wrong, and which this one gets right:

1. Assigning an **unknown** option name stores it silently in ``__dict__``.
   There is no device call, no validation and no raise.  The geometry-less
   double this replaced *raised* here, the exact inverse, and that is why
   ``_set_geometry`` could always return True while the crop fallback it
   guarded was unreachable.  That double is deleted (D-09).
2. Assigning a **bad value** to a known option raises the local error type
   (the real ``_sane.error: Invalid argument``) -- but only for a *list*
   constraint.  A *range* constraint clamps silently and reports success.
3. Assigning the **wrong Python type** raises a plain ``TypeError`` from the C
   layer before SANE is reached.  This is the row CONTEXT.md does not name.
4. Buttons, groups, inactive options and the read-only attributes raise
   ``AttributeError`` with a specific message.
5. ``multi_scan()`` **cannot raise** -- it only constructs the iterator.  The
   iterator calls ``start()`` then ``snap()`` once per page, and converts
   exactly one message, ``Document feeder out of documents``, to
   ``StopIteration``.

A sixth behaviour, from the SANE specification rather than from an execution:
``TYPE_FIXED`` values round-trip through SANE's 16.16 fixed-point
representation, so a length like letter's 215.9 mm does not read back exactly
as written.  D-19's clamp detection therefore has to compare with a tolerance;
an equality test would send every such scan down the crop path.

One deliberate, documented divergence: the real ``__load_option_dict`` filters
``TYPE_GROUP`` options out of ``opt``, which makes the library's own "Groups
don't have values" branch unreachable.  This fake keeps groups in the table so
that branch is exercisable.  Either way a group raises ``AttributeError``; only
the message differs.

``narrow_resolution_for_source`` models the option-reload *hazard* rather than
a measured device.  ``sane.py:188-213`` reloads every option descriptor when a
``set_option`` reports ``INFO_RELOAD_OPTIONS``, which a source change does, so a
resolution accepted against the platen's range can be left standing against a
feeder's narrower one.  The knob swaps in the narrower range on a source
assignment and deliberately does **not** re-validate the value already stored,
which is precisely what makes assignment ordering observable.  Measured caveat
(assumption A5): the SANE ``test`` backend does **not** behave this way, so this
hazard cannot be reproduced against real hardware and is modelled here on
purpose rather than discovered there.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "FakeSaneDev",
    "FakeSaneError",
    "FakeSaneModule",
    "build_option_table",
]

# SANE value types, read from _sane rather than guessed.
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

# Realistic constraints.  The long feeder name is the one C-06 mishandled, and
# resolution is a range because N-01 shipped against a list-only double.
_DEFAULT_SOURCES = ["Flatbed", "Automatic Document Feeder", "ADF Duplex"]
_DEFAULT_MODES = ["Color", "Gray", "Lineart"]
_DEFAULT_RESOLUTION_RANGE = (1.0, 1200.0, 1.0)
_DEFAULT_GEOMETRY_RANGE = (0.0, 300.0, 1.0)

_DEVICE_TUPLE = ("test:0", "TestVendor", "TestModel", "scanner")

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


def _option_is_active(cap: int) -> bool:
    """Mirror ``_sane.OPTION_IS_ACTIVE``."""
    return not cap & _CAP_INACTIVE


def _option_is_settable(cap: int) -> bool:
    """Mirror ``_sane.OPTION_IS_SETTABLE``."""
    return bool(cap & _CAP_SOFT_SELECT)


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
    attribute access uses underscores (``dev.tl_x``); D-09's presence check
    reads the first spelling and the assignment uses the second, so both are
    load-bearing.

    Args:
        sources: Source names for the ``source`` list constraint.
        modes: Mode names for the ``mode`` list constraint.
        resolution_range: The ``(min, max, step)`` resolution constraint.
        geometry_range: The ``(min, max, step)`` constraint shared by the four
            geometry options.

    Returns:
        The option table, ready to hand to :class:`FakeSaneDev`.

    """
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
            1,
            _CAP_SETTABLE,
            list(_DEFAULT_SOURCES if sources is None else sources),
        ),
        (
            2,
            "mode",
            "Scan mode",
            "Selects the scan mode",
            _TYPE_STRING,
            _UNIT_NONE,
            1,
            _CAP_SETTABLE,
            list(_DEFAULT_MODES if modes is None else modes),
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
            1,
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
            1,
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

    ``omit`` exists for D-09.  A device whose option list simply does not
    mention the geometry options is the case the old geometry-less double
    claimed to model and got backwards: the real library *stores* ``dev.br_y``
    on such a device rather than raising, so an omitted option table is the
    only way to reproduce the condition that makes the crop fallback
    reachable.

    ``geometry_unit`` exists for D-10.  The unit lives at index 5 of the option
    tuple, and a backend is free to report its scan area in something other
    than millimetres, which the geometry arithmetic has to scale by rather than
    assume away (N-03).

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
            low, high = float(constraint[0]), float(constraint[1])
            if key.startswith("br_"):
                values[key] = high
            elif key == "resolution":
                values[key] = min(max(300.0, low), high)
            else:
                values[key] = low
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


def _clamp(value: float, constraint: tuple[float, float, float]) -> float:
    """
    Clamp to a range constraint silently, exactly as SANE was measured to.

    Measured: ``resolution = 5000`` reads back ``1200.0`` and ``resolution =
    0`` reads back ``1.0``, with no error and no INFO_INEXACT visible to the
    caller.  The step is deliberately not quantised -- every measured step is
    ``1.0``, so quantising would encode a guess rather than an observation.

    Args:
        value: The requested value.
        constraint: The ``(min, max, step)`` triple.

    Returns:
        The value the device would report on read-back.

    """
    low, high = float(constraint[0]), float(constraint[1])
    return min(max(value, low), high)


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


def _to_sane_fixed(value: float) -> float:
    """
    Round to SANE's 16.16 fixed-point grid, as ``SANE_Fixed`` does.

    A length that is not a multiple of 1/65536 -- letter's 215.9 mm, for
    instance -- cannot be stored exactly, so it reads back differing in the low
    bits without the device having clamped anything.  This is the reason D-19
    compares areas with a tolerance instead of for equality.

    Args:
        value: The requested value.

    Returns:
        The nearest value SANE can actually represent.

    """
    return round(value * _SANE_FIXED_SCALE) / _SANE_FIXED_SCALE


def _constrain(option: tuple, key: str, value: object) -> object:
    """
    Apply the option's type and constraint to an assigned value.

    Args:
        option: The nine-element option tuple.
        key: The option name, for messages.
        value: The assigned value.

    Returns:
        The value the device would store.

    Raises:
        FakeSaneError: If a list constraint rejects the value.

    """
    value_type, constraint = option[4], option[8]
    if value_type == _TYPE_FIXED:
        number = _as_float(value)
        if isinstance(constraint, tuple):
            return _to_sane_fixed(_clamp(number, constraint))
        if isinstance(constraint, list) and number not in constraint:
            raise FakeSaneError(_INVALID_ARGUMENT)
        return _to_sane_fixed(number)
    if value_type == _TYPE_STRING:
        text = _as_str(key, value)
        if isinstance(constraint, list) and text not in constraint:
            raise FakeSaneError(_INVALID_ARGUMENT)
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
    where D-03 and D-04 live.
    """

    def __init__(self, device: FakeSaneDev) -> None:
        """
        Wrap a device.

        Args:
            device: The device to drive.

        """
        self._device = device

    def __iter__(self) -> _FakeSaneIterator:
        """Return self, as the real iterator does."""
        return self

    def __next__(self) -> Image.Image:
        """
        Scan one page.

        Returns:
            The next page image.

        Raises:
            StopIteration: When the feeder reports it is out of documents.

        """
        try:
            self._device.start()
            return self._device.snap(no_cancel=True)
        except Exception as exc:
            if str(exc) == _FEEDER_EMPTY_MESSAGE:
                raise StopIteration from None
            raise


class FakeSaneDev:
    """
    A SANE device handle that behaves like the real one.

    Configure it through the constructor rather than by subclassing, so that a
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
    # real library does not have, which is M-32's "fake kinder than the library"
    # in miniature.
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
    _options: list[tuple]
    _values: dict[str, Any]
    _pages: int
    _start_error: BaseException | None
    _start_error_page: int
    _page_index: int
    _page_size: tuple[int, int]
    _page_images: list[Image.Image]
    _page_delay: float
    _source_resolution_ranges: dict[str, tuple[float, float, float]]
    _call_errors: dict[str, BaseException]

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
                geometry options.  The default admits A4; plan 24-06 passes
                ``(0.0, 200.0, 1.0)`` to reproduce D-19's measured clamp.

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
        state["_page_delay"] = 0.0
        state["_source_resolution_ranges"] = {}
        state["_call_errors"] = {}
        state["calls"] = []
        state["assignments"] = []
        state["cancel_calls"] = 0
        state["close_calls"] = 0

    def __setattr__(self, key: str, value: object) -> None:
        """
        Store or validate an assignment, following ``sane.py:188-213``.

        Args:
            key: The attribute or option name.
            value: The value assigned.

        Raises:
            AttributeError: For a read-only attribute, a button, a group, an
                inactive option or one not settable by software.

        """
        if key in _READ_ONLY_ATTRIBUTES:
            msg = f"Read-only attribute: {key}"
            raise AttributeError(msg)
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

    def set_page_delay(self, seconds: float) -> None:
        """
        Make each page take time to arrive, so a timeout can be exercised.

        A scan that is merely slow is a real condition, and the backend's
        per-page timeout exists precisely for it, so the delay belongs in the
        device rather than in a bespoke blocking iterator written per test.  A
        hand-rolled blocking double is what this replaces, and it modelled a
        device handle -- exactly what D-17 leaves only one of.

        A method rather than a constructor keyword for the usual reason:
        ``__init__`` already carries ruff's maximum of five arguments.

        Args:
            seconds: How long each ``start()`` blocks before its page.

        """
        self.__dict__["_page_delay"] = seconds

    def fail_call(self, method: str, error: BaseException) -> None:
        """
        Arm a device method to raise, so the backend's translation is exercised.

        python-sane raises ``_sane.error``, ``RuntimeError`` or
        ``AttributeError`` from its device methods with no shared base, and the
        backend has to turn each into a saneless type naming the device (D-08).
        A method rather than a constructor keyword for the usual reason:
        ``__init__`` already carries ruff's maximum of five arguments.

        ``close`` still counts the call before raising, so a test can assert the
        handle was released and that the close failure did not mask anything.

        Args:
            method: The device method to fail: ``"close"``.
            error: The exception that method raises.

        """
        self.__dict__["_call_errors"][method] = error

    def report_sources(self, sources: list[str]) -> None:
        """
        Replace the list constraint the device reports for ``source``.

        The default table carries three realistic source names.  A test needing
        one outside them -- this project's "ADF Manual Duplex" pseudo-source, or
        a name chosen to exercise the classifier -- narrows the constraint here
        rather than by subclassing the fake, which would reintroduce exactly the
        per-file drift D-17 exists to prevent.

        Args:
            sources: The source names the device should report.

        """
        self._replace_constraint("source", list(sources))

    def _replace_constraint(self, name: str, constraint: object) -> None:
        """
        Swap one option's constraint, keeping the lookup table consistent.

        Args:
            name: The hyphenated option name, as ``get_options()`` reports it.
            constraint: The constraint the device should report from now on.

        """
        options = self.__dict__["_options"]
        key = name.replace("-", "_")
        for index, option in enumerate(options):
            if option[1] == name:
                replaced = (*option[:8], constraint)
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

        """
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

        """
        return list(self._options)

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
            # No delay on this path: the end-of-feed probe is not a page being
            # scanned.  Charging it one made the wall-clock arithmetic in the
            # per-page timeout tests wrong by a whole delay.
            raise FakeSaneError(_FEEDER_EMPTY_MESSAGE)
        if self._page_delay:
            time.sleep(self._page_delay)

    def snap(self, *, no_cancel: bool = False) -> Image.Image:
        """
        Return the current page and advance the feeder.

        ``no_cancel`` is keyword-only here.  The real signature takes it
        positionally, but a positional boolean is not allowed by this
        project's lint rules and nothing outside this module passes it.

        Args:
            no_cancel: Accepted for signature compatibility; unused.

        Returns:
            The page image -- the one ``load_feeder()`` supplied for this
            position, or a generated page when the feeder was not loaded with
            exact images.

        """
        self.calls.append("snap")
        loaded = self._page_images
        index = self._page_index
        page = (
            loaded[index]
            if index < len(loaded)
            else _page_image(index, self._page_size)
        )
        self._page_index += 1
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
        """Record that the device was cancelled."""
        self.cancel_calls += 1

    def close(self) -> None:
        """
        Record that the device was closed.

        Raises:
            BaseException: The error armed with ``fail_call("close", ...)``,
                after the call has been counted.

        """
        self.close_calls += 1
        error = self._call_errors.get("close")
        if error is not None:
            raise error


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
            device: The shared device handle ``open()`` returns.
            devices: The four-element device tuples ``get_devices()`` returns.
            init_error: An exception ``init()`` raises after counting the call,
                as a SANE that cannot start (``_sane.error``) would.
            open_error: An exception ``open()`` raises, as the real module does
                with ``_sane.error("Invalid argument")`` for an unknown device.
            get_devices_error: An exception ``get_devices()`` raises.

        """
        self.init_call_count = 0
        self.exit_call_count = 0
        self._init_error = init_error
        self._open_error = open_error
        self._get_devices_error = get_devices_error
        self._device = FakeSaneDev() if device is None else device
        self._devices = (
            [_DEVICE_TUPLE, ("test:1", "TestVendor", "TestModel", "scanner")]
            if devices is None
            else list(devices)
        )

    def init(self) -> tuple[int, int, int]:
        """
        Record the call and report a SANE version.

        Returns:
            The version tuple the real ``sane.init()`` returns.

        Raises:
            BaseException: The configured ``init_error``.

        """
        self.init_call_count += 1
        if self._init_error is not None:
            raise self._init_error
        return (1, 0, 3)

    def get_devices(self) -> list[tuple[str, str, str, str]]:
        """
        Enumerate devices.

        Returns:
            Four-element ``(name, vendor, model, type)`` tuples.

        Raises:
            BaseException: The configured ``get_devices_error``.

        """
        if self._get_devices_error is not None:
            raise self._get_devices_error
        return list(self._devices)

    def open(self, device_id: str) -> FakeSaneDev:
        """
        Open a device.

        Args:
            device_id: Ignored; one shared handle is returned so a test can
                configure the device before the code under test opens it.

        Returns:
            The shared device handle.

        Raises:
            BaseException: The configured ``open_error``.

        """
        if self._open_error is not None:
            raise self._open_error
        return self._device

    def exit(self) -> None:
        """Record the shutdown call."""
        self.exit_call_count += 1
