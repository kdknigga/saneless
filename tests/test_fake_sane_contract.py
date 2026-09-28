"""
The shared SANE double, held to the real SANE ``test`` backend row by row.

``tests/fake_sane.py`` stands in for python-sane in every scanner test that does
not drive libsane, so a rule it gets wrong keeps a defect green everywhere at
once.  This table is how it is kept honest.  Each dual-target row runs twice:
against the double (``[fake]``) and against libsane's ``test:0`` device
(``[libsane]``, marked ``sane_hardware`` so CI's hardware job runs it).  The
libsane run is what proves a row right; the fake run is what holds the double
to it.  The table, not the double's docstring, is the specification.

Two rules shape every dual-target row:

* ``test:0`` keeps option values across handles, so every row sets each option
  it reads rather than trusting a default another row may have moved, and the
  libsane target puts back what the rows touch when it is torn down.
* ``test:0`` allows one open handle at a time, so every row opens exactly one,
  and the target closes it before the per-test SANE shutdown runs.

A few rows need the device to misbehave, and the two targets are told to in
different ways: libsane's ``test`` backend has a ``read-return-value`` option,
while the double has its own knobs.  Only that arrange step differs; the
assertion is shared.

Rows that test the double's own instrumentation and knobs -- call recording,
the page budget, an armed jam, a narrowed constraint, the groups it keeps on
purpose -- have no libsane counterpart and live in a separate fake-only class
at the end.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

import pytest

import saneless.scanner.sane_backend as sane_backend_mod
from saneless.scanner.sane_backend import SaneBackend
from tests.fake_sane import (
    FakeSaneDev,
    FakeSaneError,
    FakeSaneModule,
    ReadBlockMode,
    build_option_table,
    build_test0_option_table,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from PIL import Image

    from saneless.scanner.sane_backend import SaneDevice

_DEVICE = "test:0"

_INVALID_ARGUMENT = "Invalid argument"
_INT_TYPE_ERROR = "SANE_INT and SANE_BOOL require an integer"
_FIXED_TYPE_ERROR = "SANE_FIXED requires a floating point number"
_CANCELLED = "Operation was canceled"
_JAMMED = "Document feeder jammed"
_CLOSED = "SaneDev object is closed"
_FEEDER = "Automatic Document Feeder"

# Option names the rows assign that the ``SaneDevice`` protocol does not
# declare, so they are reached through ``setattr`` and ``getattr``.
_DEPTH = "depth"
_PPL_LOSS = "ppl_loss"
_READ_RETURN_VALUE = "read_return_value"
_UNKNOWN_NAME = "no_such_option"
# A method name, assigned to spy on it: python-sane stores any name that is not
# an option on the handle itself, which shadows the method of that name.
_CANCEL = "cancel"

# The options the rows below assign on the real device, in the order they are
# put back.  Read once before the first row touches them and written back
# afterwards, so a later test in the same process -- a hardware scan, say --
# finds the device as it was.
_RESTORED_OPTIONS = (
    "source",
    "mode",
    "depth",
    "ppl_loss",
    "tl_x",
    "tl_y",
    "br_x",
    "br_y",
    "read_return_value",
)


class ReadStatus(StrEnum):
    """
    A way for the next read to fail, named by the SANE status that causes it.

    The values are ``test:0``'s own ``read-return-value`` spellings.
    """

    CANCELLED = "SANE_STATUS_CANCELLED"
    JAMMED = "SANE_STATUS_JAMMED"
    NO_DOCS = "SANE_STATUS_NO_DOCS"


class _FakeTarget:
    """The double, over an option table shaped like ``test:0``."""

    error_type: type[Exception] = FakeSaneError

    def __init__(self) -> None:
        """Build a fresh device and module, so no row sees another's state."""
        self._device = FakeSaneDev(options=build_test0_option_table())
        self._module = FakeSaneModule(device=self._device)

    def open(self) -> SaneDevice:
        """
        Open the device.

        Returns:
            A handle to the device.

        """
        return self._module.open(_DEVICE)

    def init_version(self) -> object:
        """
        Report what ``init()`` returns.

        Returns:
            The module double's version value.

        """
        return self._module.init()

    def arrange_read(self, handle: SaneDevice, status: ReadStatus) -> None:
        """
        Make the next read fail the way ``status`` would on the real device.

        Args:
            handle: Unused; the double's knobs live on the device itself.
            status: How the read should fail.

        """
        match status:
            case ReadStatus.CANCELLED:
                # A read the frontend cancelled: the gate opens at once and
                # the read ends with the cancelled status.
                self._device.block_read(ReadBlockMode.RAISE)
                self._device.cancel()
            case ReadStatus.JAMMED:
                self._device.fail_call("snap", FakeSaneError(_JAMMED))
            case ReadStatus.NO_DOCS:
                # An empty hopper: the double's own end-of-feed message.
                self._device.load_feeder([])


class _LibsaneTarget:
    """libsane's ``test:0`` device, opened through python-sane."""

    error_type: type[Exception]

    def __init__(self) -> None:
        """
        Initialise SANE the way saneless does, and remember the device state.

        ``SaneBackend()`` initialises SANE through the process-wide guard, so
        the version it records is the one a second ``sane.init()`` must never
        be called to re-read.
        """
        self._backend = SaneBackend()
        self._sane = sane_backend_mod._ensure_sane()
        self.error_type = self._sane._sane.error
        self._handles: list[SaneDevice] = []
        handle = self._sane.open(_DEVICE)
        try:
            self._saved = {name: getattr(handle, name) for name in _RESTORED_OPTIONS}
        finally:
            handle.close()

    def open(self) -> SaneDevice:
        """
        Open ``test:0``, recording the handle so teardown can close it.

        Returns:
            A new python-sane handle.

        """
        handle = self._sane.open(_DEVICE)
        self._handles.append(handle)
        return handle

    def init_version(self) -> object:
        """
        Report what ``sane.init()`` returned when the backend initialised SANE.

        Returns:
            The value the init guard recorded.

        """
        return sane_backend_mod._INIT.version

    def arrange_read(self, handle: SaneDevice, status: ReadStatus) -> None:
        """
        Make the next read fail with ``status``, through ``read-return-value``.

        Args:
            handle: The open handle.
            status: How the read should fail.

        """
        setattr(handle, _READ_RETURN_VALUE, status.value)

    def finish(self) -> None:
        """Close every handle, then put back the options the rows touched."""
        try:
            for handle in self._handles:
                handle.close()
        finally:
            restorer = self._sane.open(_DEVICE)
            try:
                for name, value in self._saved.items():
                    setattr(restorer, name, value)
            finally:
                restorer.close()


type ContractTarget = _FakeTarget | _LibsaneTarget


@pytest.fixture(
    params=[
        pytest.param("fake", id="fake"),
        pytest.param(_DEVICE, id="libsane", marks=pytest.mark.sane_hardware),
    ]
)
def target(request: pytest.FixtureRequest) -> Iterator[ContractTarget]:
    """
    Yield one side of the contract: the double, or libsane's ``test:0``.

    The libsane side requests the shared ``test``-only SANE configuration
    before anything initialises SANE, and closes its handles here, which runs
    before the suite's per-test SANE shutdown.

    Args:
        request: Carries which side this run is.

    Yields:
        The target the row runs against.

    """
    if request.param == "fake":
        yield _FakeTarget()
        return
    request.getfixturevalue("sane_test_backend_config")
    real = _LibsaneTarget()
    try:
        yield real
    finally:
        real.finish()


def _read_one_page(handle: SaneDevice) -> Image.Image:
    """
    Start a frame and read it, as a single-page scan does.

    Args:
        handle: The open handle.

    Returns:
        The page, if the read succeeds.

    """
    handle.start()
    return handle.snap()


class TestStringLists:
    """A string list takes libsane's case-insensitive unique prefix, nothing else."""

    @pytest.mark.parametrize(
        ("option", "value", "expected"),
        [
            ("mode", "gray", "Gray"),
            ("mode", "GRAY", "Gray"),
            ("mode", "Col", "Color"),
            ("mode", "G", "Gray"),
            ("mode", "c", "Color"),
            ("source", "automatic", "Automatic Document Feeder"),
            ("source", "Flat", "Flatbed"),
        ],
    )
    def test_a_case_differing_unique_prefix_becomes_the_listed_entry(
        self, target: ContractTarget, option: str, value: str, expected: str
    ) -> None:
        """
        The device stores its own spelling of the one entry the value prefixes.

        This is libsane's rule, not saneless's: saneless matches a source by
        its whole name, and the double models the library faithfully so a test
        of that stricter rule is not flattered by a double stricter still.
        """
        handle = target.open()

        setattr(handle, option, value)

        assert getattr(handle, option) == expected

    @pytest.mark.parametrize(
        ("option", "value"),
        [
            ("mode", " Gray"),
            ("mode", "Gray "),
            ("mode", ""),
            ("mode", "Sepia"),
            ("source", "adf"),
            ("source", "Nope"),
        ],
    )
    def test_anything_else_is_refused_and_the_value_stands(
        self, target: ContractTarget, option: str, value: str
    ) -> None:
        """
        No stripping, no substring match, and an empty value is ambiguous.

        ``"adf"`` is the row that matters to saneless: it abbreviates
        ``Automatic Document Feeder`` but is not a prefix of it, so the device
        refuses it rather than guessing.
        """
        handle = target.open()
        entries = {opt[1]: opt[8] for opt in handle.get_options()}[option]
        setattr(handle, option, entries[0])

        with pytest.raises(target.error_type) as raised:
            setattr(handle, option, value)

        assert str(raised.value) == _INVALID_ARGUMENT
        assert getattr(handle, option) == entries[0]


class TestStringSizes:
    """python-sane cuts a string to the option's size before libsane sees it."""

    def test_the_device_reports_its_longest_entry_plus_one(
        self, target: ContractTarget
    ) -> None:
        """A string option's size is its longest entry plus the terminating NUL."""
        handle = target.open()

        sizes = {opt[1]: opt[6] for opt in handle.get_options()}

        assert sizes["source"] == 26
        assert sizes["mode"] == 6

    @pytest.mark.parametrize(
        ("option", "value", "expected"),
        [
            (
                "source",
                "Automatic Document Feeder Duplex",
                "Automatic Document Feeder",
            ),
            ("mode", "Colorful", "Color"),
        ],
    )
    def test_a_value_longer_than_the_option_is_truncated_first(
        self, target: ContractTarget, option: str, value: str, expected: str
    ) -> None:
        """
        A name longer than every entry is cut down and then matched.

        So asking a device for a duplex feeder it does not list can silently
        select its simplex one: the cut leaves an exact match behind.
        """
        handle = target.open()

        setattr(handle, option, value)

        assert getattr(handle, option) == expected


class TestWordLists:
    """A value outside an INT word list snaps to the nearest listed word."""

    @pytest.mark.parametrize(
        ("value", "expected"), [(8, 8), (12, 8), (20, 16), (4, 1), (5, 8)]
    )
    def test_the_nearest_member_is_stored(
        self, target: ContractTarget, value: int, expected: int
    ) -> None:
        """
        Nearest by absolute difference; a tie keeps the earlier entry.

        ``12`` is four from both ``8`` and ``16`` and becomes ``8``.
        """
        handle = target.open()

        setattr(handle, _DEPTH, value)

        assert getattr(handle, _DEPTH) == expected


class TestRanges:
    """A range clamps, then quantises to its step, and never raises."""

    @pytest.mark.parametrize(
        ("option", "value", "expected"),
        [
            ("resolution", 5000, 1200.0),
            ("resolution", 0, 1.0),
            ("resolution", 150, 150.0),
            ("resolution", 115.9, 116.0),
            ("resolution", 115.4, 115.0),
            ("resolution", 115.5, 116.0),
            ("resolution", 150.7, 151.0),
            ("br_x", 115.9, 116.0),
            ("br_x", 0.4, 0.0),
            ("br_x", 0.6, 1.0),
            ("br_x", 215.9, 200.0),
        ],
    )
    def test_a_fixed_range_stores_the_quantised_value(
        self, target: ContractTarget, option: str, value: float, expected: float
    ) -> None:
        """
        A step of 1.0 rounds half up to a whole number within the range.

        So letter's 215.9 mm width reads back as 200.0 on a 200 mm bed, and
        115.9 as 116.0: what is read back is the quantised word, not the
        requested value.
        """
        handle = target.open()

        setattr(handle, option, value)

        assert getattr(handle, option) == expected

    @pytest.mark.parametrize(
        ("value", "expected"), [(5, 5), (200, 128), (-3, 0), (True, 1)]
    )
    def test_an_int_range_stores_an_int(
        self, target: ContractTarget, value: int, expected: int
    ) -> None:
        """An INT option clamps to its range and reads back a plain ``int``."""
        handle = target.open()

        setattr(handle, _PPL_LOSS, value)

        stored = getattr(handle, _PPL_LOSS)
        assert stored == expected
        assert type(stored) is int

    def test_area_reflects_the_geometry_clamp(self, target: ContractTarget) -> None:
        """An A4 box on a 200 mm bed reads back clamped, with no error."""
        handle = target.open()

        handle.tl_x = 0.0
        handle.tl_y = 0.0
        handle.br_x = 210.0
        handle.br_y = 297.0

        assert handle.area == ((0.0, 0.0), (200.0, 200.0))

    def test_resolution_reads_back_as_float(self, target: ContractTarget) -> None:
        """A FIXED option reads back ``float`` even when an ``int`` went in."""
        handle = target.open()

        handle.resolution = 300

        assert isinstance(handle.resolution, float)
        assert handle.resolution == 300.0


class TestTypes:
    """python-sane checks the Python type before libsane sees the value."""

    @pytest.mark.parametrize(
        ("option", "value", "message"),
        [
            ("ppl_loss", 5.0, _INT_TYPE_ERROR),
            ("depth", 8.0, _INT_TYPE_ERROR),
            ("resolution", "banana", _FIXED_TYPE_ERROR),
            ("tl_x", "banana", _FIXED_TYPE_ERROR),
        ],
    )
    def test_the_wrong_python_type_is_a_type_error(
        self, target: ContractTarget, option: str, value: object, message: str
    ) -> None:
        """An INT option refuses a float, even a whole one; FIXED refuses a string."""
        handle = target.open()

        with pytest.raises(TypeError) as raised:
            setattr(handle, option, value)

        assert str(raised.value) == message


class TestAttributeAccess:
    """Which names raise, which are stored, and with what message."""

    @pytest.mark.parametrize(
        ("name", "value", "message"),
        [
            ("dev", 1, "Read-only attribute: dev"),
            ("area", ((0.0, 0.0), (1.0, 1.0)), "Read-only attribute: area"),
            ("optlist", [], "Read-only attribute: optlist"),
            ("print_options", 1, "Buttons don't have values: print_options"),
            ("three_pass_order", "RGB", "Inactive option: three_pass_order"),
        ],
    )
    def test_setattr_raises_the_measured_attribute_error(
        self, target: ContractTarget, name: str, value: object, message: str
    ) -> None:
        """Read-only attributes, buttons and inactive options refuse a value."""
        handle = target.open()

        with pytest.raises(AttributeError) as raised:
            setattr(handle, name, value)

        assert str(raised.value) == message

    @pytest.mark.parametrize(
        ("name", "message"),
        [
            ("print_options", "Buttons don't have values: print_options"),
            ("three_pass_order", "Inactive option: three_pass_order"),
            ("definitely_absent", "No such attribute: definitely_absent"),
        ],
    )
    def test_getattr_raises_the_measured_attribute_error(
        self, target: ContractTarget, name: str, message: str
    ) -> None:
        """Reading a button, an inactive option or an absent name raises."""
        handle = target.open()

        with pytest.raises(AttributeError) as raised:
            getattr(handle, name)

        assert str(raised.value) == message

    def test_an_unknown_name_is_stored_silently(self, target: ContractTarget) -> None:
        """
        An unrecognised name is stored on the handle, with no device call.

        So assigning an option the device does not have cannot be detected by
        catching an error: there is none.  Code has to check the option list.
        """
        handle = target.open()

        setattr(handle, _UNKNOWN_NAME, 1)

        assert getattr(handle, _UNKNOWN_NAME) == 1

    def test_option_names_use_hyphens_but_attributes_use_underscores(
        self, target: ContractTarget
    ) -> None:
        """``get_options()`` reports ``tl-x``; assignment writes ``tl_x``."""
        handle = target.open()

        handle.tl_x = 5.0

        assert "tl-x" in [opt[1] for opt in handle.get_options()]
        assert handle.tl_x == 5.0

    def test_the_error_type_subclasses_exception_directly(
        self, target: ContractTarget
    ) -> None:
        """The SANE error is not an ``OSError`` or any richer type."""
        assert target.error_type.__mro__[1:] == (Exception, BaseException, object)


class TestReads:
    """How a read ends, through a single-page read and the feeder iterator."""

    def test_a_cancelled_read_raises_the_sane_message(
        self, target: ContractTarget
    ) -> None:
        """A read ended by a cancel raises with SANE's own status text."""
        handle = target.open()
        handle.source = "Flatbed"
        target.arrange_read(handle, ReadStatus.CANCELLED)

        with pytest.raises(target.error_type) as raised:
            _read_one_page(handle)

        assert str(raised.value) == _CANCELLED

    def test_multi_scan_returns_an_iterator(self, target: ContractTarget) -> None:
        """``multi_scan()`` hands back something with ``__next__``."""
        handle = target.open()

        assert hasattr(handle.multi_scan(), "__next__")

    def test_multi_scan_cannot_raise(self, target: ContractTarget) -> None:
        """
        ``multi_scan()`` only builds the iterator; the failure comes from ``next``.

        A double that raised from ``multi_scan()`` itself would make a
        ``try`` around that call look meaningful when it is unreachable.
        """
        handle = target.open()
        handle.source = "Flatbed"
        target.arrange_read(handle, ReadStatus.JAMMED)
        iterator = handle.multi_scan()

        with pytest.raises(target.error_type) as raised:
            next(iterator)

        assert str(raised.value) == _JAMMED

    def test_out_of_documents_becomes_stop_iteration(
        self, target: ContractTarget
    ) -> None:
        """The one message the iterator turns into a clean end of feed."""
        handle = target.open()
        handle.source = "Flatbed"
        target.arrange_read(handle, ReadStatus.NO_DOCS)

        assert list(handle.multi_scan()) == []


class TestScanParameters:
    """What ``get_parameters()`` reports for the frame the handle is set up for."""

    @pytest.mark.parametrize(
        ("mode", "depth", "frame_format", "bytes_per_pixel"),
        [
            pytest.param("Gray", 8, "gray", 1, id="gray-8"),
            pytest.param("Gray", 16, "gray", 2, id="gray-16"),
            pytest.param("Color", 8, "color", 3, id="color-8"),
        ],
    )
    def test_the_parameters_describe_the_configured_frame(
        self,
        target: ContractTarget,
        mode: str,
        depth: int,
        frame_format: str,
        bytes_per_pixel: int,
    ) -> None:
        """
        A five-tuple: format, last frame, (pixels, lines), depth, bytes per line.

        A line is ``pixels_per_line`` samples of ``depth`` bits for each of the
        format's channels, so a 16-bit gray line is twice an 8-bit one and an
        8-bit colour line three times.
        """
        handle = target.open()
        handle.source = "Flatbed"
        handle.resolution = 75
        handle.mode = mode
        setattr(handle, _DEPTH, depth)

        parameters = handle.get_parameters()

        reported_format, last_frame, size, reported_depth, bytes_per_line = parameters
        pixels_per_line, lines = size
        assert reported_format == frame_format
        assert last_frame
        assert type(pixels_per_line) is int
        assert type(lines) is int
        assert pixels_per_line > 0
        assert lines > 0
        assert reported_depth == depth
        assert bytes_per_line == bytes_per_pixel * pixels_per_line

    def test_depth_does_not_change_the_frame_size(self, target: ContractTarget) -> None:
        """
        A 16-bit frame reports the same pixels and lines as an 8-bit one.

        So a 16-bit page that comes back twice as tall has been misread, and
        the device's own parameters are what shows it was 16-bit to begin with.
        """
        handle = target.open()
        handle.source = "Flatbed"
        handle.resolution = 75
        handle.mode = "Gray"
        setattr(handle, _DEPTH, 8)
        eight_bit = handle.get_parameters()[2]

        setattr(handle, _DEPTH, 16)

        assert handle.get_parameters()[2] == eight_bit


def _write_mode(handle: SaneDevice) -> None:
    """
    Assign an option, as configuring a scan does.

    Args:
        handle: The handle to write through.

    """
    handle.mode = "Gray"


def _next_page(handle: SaneDevice) -> Image.Image:
    """
    Ask a fresh feeder iterator for its first page.

    Args:
        handle: The handle to scan through.

    Returns:
        The page, if the read succeeds.

    """
    return next(handle.multi_scan())


# Every call a closed handle refuses, named for the row id.  ``close()`` is the
# one call left out: closing again is allowed and does nothing.
_CALLS_AFTER_CLOSE: list[tuple[str, Callable[[SaneDevice], object]]] = [
    ("cancel", lambda handle: handle.cancel()),
    ("get_options", lambda handle: handle.get_options()),
    ("get_parameters", lambda handle: handle.get_parameters()),
    ("option_read", lambda handle: handle.mode),
    ("option_write", _write_mode),
    ("start", lambda handle: handle.start()),
    ("snap", lambda handle: handle.snap()),
    ("next_page", _next_page),
]


class TestHandles:
    """
    What a handle is: a new one per open, closed for good, cancelled when dropped.

    ``test:0`` also refuses a second open while one handle is still open, with
    "Device busy".  That is the test backend's rule, and typically a USB
    backend's, not one libsane imposes on every device, so the double does not
    model it and no row asserts it.
    """

    def test_dropping_a_feeder_iterator_cancels_its_handle(
        self, target: ContractTarget
    ) -> None:
        """
        The iterator's finaliser calls ``cancel()`` on the handle that made it.

        So an iterator let go of after a timeout sends a cancel of its own,
        on whichever thread drops the last reference to it.
        """
        handle = target.open()
        handle.source = _FEEDER
        cancels: list[str] = []

        def spy() -> None:
            cancels.append(_CANCEL)

        setattr(handle, _CANCEL, spy)
        iterator = handle.multi_scan()

        del iterator

        assert cancels == [_CANCEL]

    @pytest.mark.parametrize(
        "call",
        [pytest.param(call, id=name) for name, call in _CALLS_AFTER_CLOSE],
    )
    def test_a_closed_handle_refuses_every_call(
        self, target: ContractTarget, call: Callable[[SaneDevice], object]
    ) -> None:
        """Once closed, a handle raises the SANE error for anything but close."""
        handle = target.open()
        handle.close()

        with pytest.raises(target.error_type) as raised:
            call(handle)

        assert str(raised.value) == _CLOSED

    def test_a_second_close_is_harmless_and_does_not_reopen(
        self, target: ContractTarget
    ) -> None:
        """Closing twice raises nothing, and the handle stays closed."""
        handle = target.open()
        handle.close()

        handle.close()

        with pytest.raises(target.error_type) as raised:
            handle.cancel()
        assert str(raised.value) == _CLOSED

    def test_each_open_is_a_new_handle_and_option_values_carry_over(
        self, target: ContractTarget
    ) -> None:
        """
        ``open()`` makes a new handle, but the device keeps its option values.

        So a value one scan left behind is still set when the next scan opens
        the device, unless that scan sets it again.
        """
        first = target.open()
        first.mode = "Color"
        first.close()

        second = target.open()

        assert second is not first
        assert second.mode == "Color"


class TestInit:
    """What ``init()`` returns."""

    def test_init_reports_the_version_code_and_its_three_parts(
        self, target: ContractTarget
    ) -> None:
        """
        A 4-tuple: the packed version code, then its major, minor and build.

        The numbers follow the installed libsane, so the row asserts the shape
        and the packing rather than one release's values.
        """
        version = target.init_version()

        assert isinstance(version, tuple)
        assert len(version) == 4
        code, major, minor, build = version
        assert code == major << 24 | minor << 16 | build
        assert major == 1


class TestTheDoubleItself:
    """
    Rows about the double's own knobs and instrumentation, not about libsane.

    These have no ``test:0`` counterpart: they check that a knob a scanner
    test relies on does what it says, or they pin a deliberate choice the
    double makes.  They run against the default option table, which is the
    one most scanner tests use.
    """

    @pytest.mark.parametrize(
        ("option", "value", "message"),
        [
            ("geometry_group", 1, "Groups don't have values: geometry_group"),
            ("readonly_opt", "x", "Option can't be set by software: readonly_opt"),
        ],
    )
    def test_setattr_raises_for_a_group_and_a_read_only_option(
        self, option: str, value: object, message: str
    ) -> None:
        """
        A group and an option software cannot set both refuse a value.

        The group row is the double's one deliberate divergence: python-sane
        drops groups from its option dictionary, which makes its own "Groups
        don't have values" branch unreachable, while the double keeps them so
        that branch can be exercised.  Either way a group raises
        ``AttributeError``; only the message differs.  ``test:0`` has no
        active read-only option without its test options switched on, so that
        row is the double's alone too.
        """
        dev = FakeSaneDev()
        with pytest.raises(AttributeError) as raised:
            setattr(dev, option, value)
        assert str(raised.value) == message

    def test_getattr_raises_for_a_group(self) -> None:
        """Reading a group raises, as the kept group row above describes."""
        dev = FakeSaneDev()
        with pytest.raises(AttributeError) as raised:
            _ = dev.geometry_group
        assert str(raised.value) == "Groups don't have values: geometry_group"

    def test_iterator_calls_start_then_snap_once_per_page(self) -> None:
        """The iterator drives start()/snap() per page, not ``iter(list)``."""
        dev = FakeSaneDev(pages=2)
        pages = list(dev.multi_scan())
        assert len(pages) == 2
        # The trailing "start" is not an off-by-one: the real iterator learns
        # the feeder is empty only by calling start() one more time and
        # catching the message it raises.  A double built on iter(list) hides
        # that probe, and with it the only place end-of-feed handling runs.
        assert dev.calls == ["start", "snap", "start", "snap", "start"]

    def test_page_budget_is_honoured(self) -> None:
        """A three-sheet feeder yields exactly three pages."""
        dev = FakeSaneDev(pages=3)
        assert len(list(dev.multi_scan())) == 3

    def test_an_empty_feeder_stops_immediately(self) -> None:
        """A zero-sheet feeder stops without yielding."""
        dev = FakeSaneDev(pages=0)
        assert list(dev.multi_scan()) == []

    def test_an_armed_out_of_documents_start_error_ends_the_feed(self) -> None:
        """``start_error`` carrying the end-of-feed message is a clean end."""
        dev = FakeSaneDev(
            pages=5,
            start_error=FakeSaneError("Document feeder out of documents"),
        )
        assert list(dev.multi_scan()) == []

    def test_a_jam_propagates_out_of_next(self) -> None:
        """Any other armed message propagates rather than ending the scan."""
        dev = FakeSaneDev(pages=5, start_error=FakeSaneError(_JAMMED))
        with pytest.raises(FakeSaneError):
            list(dev.multi_scan())

    def test_get_options_reports_a_realistic_feeder_name(self) -> None:
        """The default table carries the long real-world feeder name."""
        dev = FakeSaneDev()
        constraints = {opt[1]: opt[8] for opt in dev.get_options()}
        assert "Automatic Document Feeder" in constraints["source"]

    def test_get_options_reports_resolution_as_a_range(self) -> None:
        """The default table's resolution is a ``(min, max, step)`` range."""
        dev = FakeSaneDev()
        constraints = {opt[1]: opt[8] for opt in dev.get_options()}
        assert constraints["resolution"] == (1.0, 1200.0, 1.0)

    def test_a_fixed_value_is_stored_on_the_sixteen_sixteen_grid(self) -> None:
        """
        With no step to quantise to, a FIXED value keeps SANE_Fixed precision.

        SANE_Fixed is a 16.16 integer, so letter's 215.9 mm is not exactly
        representable and reads back differing in the low bits.  ``test:0``
        quantises every FIXED range it has to a whole step, so this is shown
        on a step-0 range, the case geometry arithmetic needs a tolerance for.
        """
        dev = FakeSaneDev(options=build_option_table(geometry_range=(0.0, 300.0, 0.0)))

        dev.br_x = 215.9

        assert dev.br_x != 215.9
        assert dev.br_x == pytest.approx(215.9, abs=1e-4)

    def test_a_narrowed_constraint_rejects_a_previously_legal_value(self) -> None:
        """The option table is injectable, so a test can narrow a constraint."""
        dev = FakeSaneDev(
            options=[(1, "mode", "Scan mode", "Mode desc", 3, 0, 6, 5, ["Color"])]
        )
        with pytest.raises(FakeSaneError) as raised:
            dev.mode = "Lineart"
        assert str(raised.value) == _INVALID_ARGUMENT

    def test_a_closed_handle_refuses_before_the_device_counts_anything(
        self,
    ) -> None:
        """
        A refused call reaches nothing, and only the first close is counted.

        The C layer checks for a closed handle before it makes any SANE call,
        so the double's counters must not move either: a test counting
        cancels would otherwise count one the device never received.
        """
        module = FakeSaneModule()
        handle = module.open(_DEVICE)
        handle.close()
        handle.close()

        for call in (handle.start, handle.snap, handle.cancel):
            with pytest.raises(FakeSaneError):
                call()

        assert module.device.calls == []
        assert module.device.cancel_calls == 0
        assert module.device.close_calls == 1

    def test_module_init_reports_the_version_libsane_1_0_32_returned(self) -> None:
        """
        The double reports ``(16777248, 1, 0, 32)``, measured on libsane 1.0.32.

        The dual-target row above checks the shape against whatever libsane is
        installed; this one pins the double's own value to a real release.
        """
        assert FakeSaneModule().init() == (16777248, 1, 0, 32)

    def test_module_reports_four_element_device_tuples(self) -> None:
        """``get_devices()`` matches the real ``(name, vendor, model, type)``."""
        devices = FakeSaneModule().get_devices()
        assert devices
        for entry in devices:
            assert len(entry) == 4

    def test_the_default_table_has_no_depth_option_and_reports_eight_bits(
        self,
    ) -> None:
        """
        Without the opt-in, there is no ``depth`` option and a frame is 8-bit.

        Most scanner tests use the default table, so offering ``depth`` there
        would add an assignment to every one of their expected sequences.
        """
        dev = FakeSaneDev()

        assert "depth" not in [opt[1] for opt in dev.get_options()]
        assert dev.get_parameters()[3] == 8

    def test_offer_depth_adds_an_int_word_list_the_parameters_follow(self) -> None:
        """
        ``offer_depth`` adds ``test:0``'s INT word list, and the frame follows it.

        The value stored is the list's first entry, as a device powered on at
        that depth would report it.
        """
        dev = FakeSaneDev()

        dev.offer_depth()

        option = {opt[1]: opt for opt in dev.get_options()}["depth"]
        assert option[4] == 1
        assert option[8] == [1, 8, 16]
        setattr(dev, _DEPTH, 16)
        assert dev.get_parameters()[3] == 16

    def test_offer_depth_takes_the_values_a_device_lists(self) -> None:
        """A device that only lists 16 reports 16 from the start."""
        dev = FakeSaneDev()

        dev.offer_depth([16])

        assert {opt[1]: opt[8] for opt in dev.get_options()}["depth"] == [16]
        assert getattr(dev, _DEPTH) == 16
        assert dev.get_parameters()[3] == 16

    def test_a_mode_can_switch_depth_off_and_on_again(self) -> None:
        """
        ``deactivate_depth_in_modes`` makes ``depth`` follow each ``mode`` set.

        In a listed mode the option is reported inactive and an assignment is
        refused as inactive; in any other mode it is active again.
        """
        dev = FakeSaneDev()
        dev.offer_depth()
        dev.deactivate_depth_in_modes(("Lineart",))

        dev.mode = "Lineart"

        option = {opt[1]: opt for opt in dev.get_options()}["depth"]
        assert option[7] & 32
        with pytest.raises(AttributeError, match="Inactive option: depth"):
            setattr(dev, _DEPTH, 8)

        dev.mode = "Color"

        option = {opt[1]: opt for opt in dev.get_options()}["depth"]
        assert not option[7] & 32
        setattr(dev, _DEPTH, 8)
        assert getattr(dev, _DEPTH) == 8

    def test_set_parameters_overrides_what_is_reported(self) -> None:
        """
        A knob reports a 16-bit or very large frame without building one.

        Bytes per line are derived from whatever was overridden, unless they
        were overridden too.
        """
        dev = FakeSaneDev()
        dev.mode = "Gray"

        dev.set_parameters(depth=16, pixels_per_line=5000, lines=7000)

        assert dev.get_parameters() == ("gray", 1, (5000, 7000), 16, 10000)

        dev.set_parameters(bytes_per_line=3)

        assert dev.get_parameters()[4] == 3

    def test_an_armed_get_parameters_failure_raises(self) -> None:
        """``fail_call`` reaches ``get_parameters`` like any other device method."""
        dev = FakeSaneDev()
        dev.fail_call("get_parameters", FakeSaneError("I/O error"))

        with pytest.raises(FakeSaneError, match="I/O error"):
            dev.get_parameters()

    def test_option_and_parameter_reads_are_counted_through_every_handle(
        self,
    ) -> None:
        """
        Each ``get_options()`` and ``get_parameters()`` a handle makes is counted.

        A closed handle refuses both before the device counts anything.
        """
        module = FakeSaneModule()
        first = module.open(_DEVICE)
        first.get_options()
        first.get_parameters()
        first.close()
        second = module.open(_DEVICE)
        second.get_options()
        second.close()

        with pytest.raises(FakeSaneError):
            second.get_options()
        with pytest.raises(FakeSaneError):
            second.get_parameters()

        assert module.device.get_options_calls == 2
        assert module.device.get_parameters_calls == 1
