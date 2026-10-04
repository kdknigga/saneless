"""The scanner abstraction and its SANE backend drive a device safely."""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import gc
import importlib
import inspect
import logging
import operator
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, NoReturn, cast

import pytest
from PIL import Image, ImageDraw

import saneless
import saneless.cli as cli_module
import saneless.scanner as scanner_pkg
import saneless.scanner.base as scanner_base
import saneless.scanner.options as options_mod
import saneless.scanner.page_budget as page_budget_mod
import saneless.scanner.sane_backend as sane_backend_mod
import saneless.scanner.scan_session as scan_session_mod
import saneless.web.app as app_module
import saneless.web.server as server_module
from saneless import checks
from saneless.exceptions import (
    ConfigError,
    FeederEmptyError,
    ListingCrashedError,
    ListingNoAnswerError,
    ListingTimedOutError,
    ScanError,
    ScanInterrupted,
)
from saneless.pipeline import _SPOOL_LABEL_A, _SPOOL_LABEL_B
from saneless.scanner.base import (
    MAX_PAGES_PER_PASS,
    DeviceCapabilities,
    DeviceInfo,
    PageRecord,
    PageSink,
    PassCapReached,
    ScanBatch,
    ScannerBackend,
    ScanSettings,
    SourceKind,
    classify_source,
)
from saneless.scanner.listing import ListingReply, ListingRequest
from saneless.scanner.net_hosts import effective_sane_net_hosts
from saneless.scanner.sane_backend import SaneBackend
from saneless.scanner.scan_session import GeometryUnit
from saneless.spool import SpooledPageSink
from saneless.text_safety import has_control_characters
from saneless.vocabulary import (
    PYTHON_SANE_INSTALL_NEXT_STEP,
    python_sane_missing_message,
    scan_page_description,
)
from tests.conftest import (
    StubScannerBackend,
    images_of,
    reset_sane_process_state,
    scan_batch,
    spooling,
)
from tests.fake_sane import (
    TYPE_INT,
    UNNAMED_OPTION_ENTRIES,
    FakeSaneDev,
    FakeSaneError,
    FakeSaneModule,
    ReadBlockMode,
    build_option_table,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator
    from types import FrameType, ModuleType

    from tests.conftest import ListingSeam

# The backend module's own logger, for the tests that read what it reported.
_BACKEND_LOGGER = "saneless.scanner.sane_backend"
_SESSION_LOGGER = "saneless.scanner.scan_session"
# The backend logs from two modules: its own, for SANE's lifecycle and the
# per-page wait, and the device session's, for what is done with the device.
_SCANNER_LOGGERS = frozenset({_BACKEND_LOGGER, _SESSION_LOGGER})


@contextlib.contextmanager
def _signal_raises_scan_interrupted(signum: int) -> Generator[None]:
    """
    Make ``signum`` raise ``ScanInterrupted`` for the block, then restore it.

    The stand-in for the handler a one-shot command installs for SIGTERM and
    SIGHUP.  The previous handler is put back in a ``finally``, because a
    raising SIGTERM handler left behind would outlive this test.

    Args:
        signum: The signal to handle.

    Yields:
        Nothing; the handler is installed for the block.

    """

    def handler(received: int, _frame: FrameType | None) -> None:
        msg = f"Interrupted by signal {received}"
        raise ScanInterrupted(msg, signum=received)

    previous = signal.signal(signum, handler)
    try:
        yield
    finally:
        signal.signal(signum, previous)


# The EXIF orientation tag, set on a page so a test can tell whether any EXIF
# survived into the file the spool wrote.
_EXIF_ORIENTATION_TAG = 0x0112

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_content_image(
    width: int = 200, height: int = 300, color: str = "red"
) -> Image.Image:
    """
    Create a test image with mixed content, comfortably above _MIN_PAGE_BYTES.

    Uses drawing operations to give the image non-trivial pixel variance, so a
    test can tell a real page apart from a uniformly blank one.  The backend
    does not judge content, so a page survives a scan with or without it.
    """
    img = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, width - 10, height - 10], fill="blue")
    draw.ellipse([30, 30, width - 30, height - 30], fill="green")
    return img


def _with_orientation_exif(page: Image.Image) -> Image.Image:
    """
    Attach a real EXIF block, with an orientation tag, to a page.

    Args:
        page: The page to tag; it is modified and returned.

    Returns:
        The same page, carrying ``info["exif"]``.

    """
    exif = Image.Exif()
    exif[_EXIF_ORIENTATION_TAG] = 6
    page.info["exif"] = exif.tobytes()
    return page


def _assert_no_exif_on_disk(record: PageRecord) -> None:
    """
    Assert the spooled file behind a record carries no EXIF at all.

    Args:
        record: The record whose file to open.

    """
    with Image.open(record.path) as spooled:
        assert "exif" not in spooled.info
        assert dict(spooled.getexif()) == {}


# There is exactly one definition of what python-sane does, in
# tests/fake_sane.py, and both this module and test_pipeline.py are written
# against it.  A second double would be free to disagree with the library --
# raising on an unknown option name, say, where python-sane stores it
# silently -- and every disagreement could keep a defect green.


# ---------------------------------------------------------------------------
# Dataclass field tests
# ---------------------------------------------------------------------------


class TestDeviceInfo:
    """DeviceInfo keeps what a device reports, safe to display."""

    def test_device_info_fields(self) -> None:
        """DeviceInfo stores name, vendor, model, and device_type."""
        info = DeviceInfo(
            name="test:device",
            vendor="Test",
            model="Scanner",
            device_type="scanner",
        )
        assert info.name == "test:device"
        assert info.vendor == "Test"
        assert info.model == "Scanner"
        assert info.device_type == "scanner"

    def test_device_info_neutralises_control_characters_in_display_fields(
        self,
    ) -> None:
        """
        A device's vendor, model and type are stored with controls escaped.

        A rogue saned or eSCL device chooses these strings, and they are only
        ever shown. The name is kept byte for byte, because it goes back to
        ``sane.open`` and into the configuration; it is escaped where shown.
        """
        info = DeviceInfo(
            name="net:\x1bx",
            vendor="V\x1b",
            model="\x1b[2J",
            device_type="t\x85",
        )

        assert info.name == "net:\x1bx"
        assert info.vendor == "V\\x1b"
        assert info.model == "\\x1b[2J"
        assert info.device_type == "t\\x85"


class TestScanSettings:
    """ScanSettings stores what it is given, flatbed being the Auto default."""

    def test_scan_settings_fields(self) -> None:
        """ScanSettings stores source, resolution, and mode."""
        # Deliberately lowercase: this is a value-object test that reaches no
        # device, so it is not subject to any device's mode constraint. It
        # asserts only that the dataclass stores what it was handed.
        settings = ScanSettings(source="Flatbed", resolution=300, mode="color")
        assert settings.source == "Flatbed"
        assert settings.resolution == 300
        assert settings.mode == "color"

    def test_auto_source_mode_default_flatbed(self) -> None:
        """ScanSettings defaults auto_source_mode to 'flatbed'."""
        settings = ScanSettings(source="Auto", resolution=300, mode="Color")
        assert settings.auto_source_mode == "flatbed"

    def test_auto_source_mode_adf(self) -> None:
        """ScanSettings accepts auto_source_mode='adf'."""
        settings = ScanSettings(
            source="Auto", resolution=300, mode="Color", auto_source_mode="adf"
        )
        assert settings.auto_source_mode == "adf"


# ---------------------------------------------------------------------------
# Source classification tests
# ---------------------------------------------------------------------------


class TestClassifySource:
    """The single source-classification rule in the codebase."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("Flatbed", SourceKind.FLATBED),
            # The real-world spellings of the SANE `test`, Brother, epson, sharp
            # and umax feeders.  Their lowercase form STARTS WITH "auto", so the
            # AUTO rule must be an exact equality test and must never be a
            # substring test -- a substring test classifies these as AUTO, sends
            # them down the single-page branch, and a stack scans as one page.
            ("Automatic Document Feeder", SourceKind.FEEDER),
            ("Automatic Document Feeder(left aligned)", SourceKind.FEEDER),
            ("Automatic Document Feeder(centrally aligned)", SourceKind.FEEDER),
            ("Document Feeder", SourceKind.FEEDER),
            ("ADF", SourceKind.FEEDER),
            # "ADF Back" is a feeder for ROUTING purposes even though it must
            # not share a profile slug with "ADF Front".
            ("ADF Front", SourceKind.FEEDER),
            ("ADF Back", SourceKind.FEEDER),
            ("ADF Duplex", SourceKind.FEEDER_DUPLEX),
            ("Adf-duplex", SourceKind.FEEDER_DUPLEX),
            ("ADF Manual Duplex", SourceKind.FEEDER_DUPLEX),
            # Ambiguity A -- Fujitsu's "Card Duplex" contains "duplex" and no
            # feeder token, so it classifies FEEDER_DUPLEX and starts routing to
            # multi_scan().  Deliberate: a card-feed path IS a sheet path, and
            # requiring a feeder token AND "duplex" would mis-route real
            # Fujitsu hardware.
            ("Card Duplex", SourceKind.FEEDER_DUPLEX),
            # Ambiguity B -- "Manual Duplex" contains "duplex" and no feeder
            # token, so it classifies FEEDER_DUPLEX like "Card Duplex".  No
            # special case is built for it here.
            ("Manual Duplex", SourceKind.FEEDER_DUPLEX),
            ("Auto", SourceKind.AUTO),
            ("auto", SourceKind.AUTO),
            ("  Auto  ", SourceKind.AUTO),
            ("Transparency Adapter", SourceKind.UNKNOWN),
            ("TMA Slides", SourceKind.UNKNOWN),
            ("TMA Negatives", SourceKind.UNKNOWN),
            # Bell+Howell's manual tray is single-page: "feed" is not "feeder".
            ("Manual Feed Tray", SourceKind.UNKNOWN),
        ],
    )
    def test_classify_source(self, source: str, expected: SourceKind) -> None:
        """Harvested real-world SANE source names classify correctly."""
        assert classify_source(source) is expected

    def test_uses_feeder_true_for_feeder_kinds(self) -> None:
        """FEEDER and FEEDER_DUPLEX feed a stack of sheets."""
        assert SourceKind.FEEDER.uses_feeder
        assert SourceKind.FEEDER_DUPLEX.uses_feeder

    def test_uses_feeder_false_for_single_page_kinds(self) -> None:
        """FLATBED, AUTO, and UNKNOWN take the single-page path."""
        assert not SourceKind.FLATBED.uses_feeder
        assert not SourceKind.AUTO.uses_feeder
        assert not SourceKind.UNKNOWN.uses_feeder


# ---------------------------------------------------------------------------
# ABC contract tests
# ---------------------------------------------------------------------------


class TestScannerBackendABC:
    """ScannerBackend is abstract."""

    def test_scanner_backend_is_abstract(self) -> None:
        """ScannerBackend cannot be instantiated directly."""
        # Bound as a zero-argument callable, because both type checkers
        # correctly refuse a direct call on an abstract class. Being refused is
        # exactly what this test asserts the interpreter does at runtime, so
        # the cast states the shape the call site claims and the raises block
        # below is the proof of what actually happens.
        cls = cast("Callable[[], object]", ScannerBackend)
        with pytest.raises(TypeError, match="abstract"):
            cls()


# ---------------------------------------------------------------------------
# SaneBackend tests (all using mocked sane module)
# ---------------------------------------------------------------------------


_TEST_DEVICE = "test:device:001"

# The call sequence a three-sheet feeder produces, start to finish.
#
# The real ADF iterator calls start() then snap() ONCE PER SHEET, so "snap was
# never called" does not distinguish the feeder path from the flatbed one.
# What distinguishes the feeder is this repeated per-page probe, ending in one
# final start() that reports the feeder empty. A flatbed scan is exactly
# ["start", "snap"], so comparing the whole sequence tells the two paths apart
# without asserting a falsehood about the library.
_THREE_SHEET_FEEDER_CALLS = ["start", "snap"] * 3 + ["start"]

# The free-space reserve the sinks in this module keep beyond the page being
# written.  Zero, deliberately: these tests are about what the backend does
# with a page, not about the spool's shortfall arithmetic, and any positive reserve
# would make every scanner test fail on a CI runner whose disk happened to be
# nearly full.  ``tests/test_spool.py`` owns the shortfall path and exercises
# it with a reserve chosen to fire.
_NO_FREE_SPACE_RESERVE = 0


def _page_sink_for(tmp_path: Path, label: str = _SPOOL_LABEL_A) -> SpooledPageSink:
    """
    Build a real sink spooling one pass's pages under ``tmp_path``.

    A real ``SpooledPageSink`` and never a mock, because the point of handing
    the backend a sink is that the write really happens: a mock sink would let
    a page that cannot be written, measured or read back again still pass.
    The spooled files are what ``images_of`` reads, so a test asserting on
    pixels is asserting on what was actually stored.

    Each pass gets its own subdirectory keyed by its label, so a test driving
    two acquisitions cannot have one pass's files answer for the other's.

    Args:
        tmp_path: The per-test temporary directory pytest removes afterwards.
        label: The pass label, and the prefix every spooled file carries.

    Returns:
        A sink ready to be handed to ``scan_pages`` as its third argument.

    """
    directory = tmp_path / f"spool-{label}"
    directory.mkdir(exist_ok=True)
    return SpooledPageSink(directory, label, _NO_FREE_SPACE_RESERVE)


# The prefix every acquisition thread the backend starts is named with, and the
# bound a test waits for one under.
#
# Joining by name is how a test observes a daemon reader finishing without
# polling and without a sleep: ``Thread.join(timeout)`` returns the instant the
# thread ends.  The bound exists only so a genuinely stuck reader fails the
# assertion that follows rather than hanging the run; five seconds is well
# inside pytest-timeout's sixty.
_READER_THREAD_PREFIX = "sane-read-"
_READER_JOIN_SECONDS = 5.0

# The bound an interrupt delivered mid-read must land inside.  The read's page
# budget is thirty seconds, far above it, so a wait that held the interrupt
# back until the page timed out would miss the bound by seconds.
_INTERRUPT_BOUND_SECONDS = 5.0


def _join_sane_reader_threads(timeout: float = _READER_JOIN_SECONDS) -> None:
    """
    Wait for every acquisition thread the backend started to finish.

    Args:
        timeout: The bound each join waits under.

    """
    for thread in threading.enumerate():
        if thread.name.startswith(_READER_THREAD_PREFIX):
            thread.join(timeout)


def _wedged() -> bool:
    """
    Report whether the backend has a wedge recorded.

    A call rather than a read of the field, so a test can assert it before and
    after the wedge clears without a type checker holding it to the first
    answer.

    Returns:
        The wedge record's ``stuck`` flag.

    """
    return sane_backend_mod._WEDGE.stuck


# The name the backend gives the thread that cancels a timed-out read.
_CANCEL_THREAD_NAME = "sane-cancel"


def _join_sane_cancel_threads(timeout: float = _READER_JOIN_SECONDS) -> None:
    """
    Wait for every cancel thread the backend started to finish.

    Joined like the readers, and for the same reason: it is how a test sees a
    slow cancel come back without polling or a sleep.

    Args:
        timeout: The bound each join waits under.

    """
    for thread in threading.enumerate():
        if thread.name == _CANCEL_THREAD_NAME:
            thread.join(timeout)


# The framing for a direct acquisition call: pages handed on uncropped, at
# 300 dpi.  ``_acquire_pages`` takes the crop and the read-back dpi as one
# record because ``scan_pages`` builds both from the paper size, the
# resolution the device chose and whether the scan area was set on the
# device.  A test driving acquisition directly has none of those, and is not
# about geometry: a "full" paper size never crops.
_UNCROPPED = scan_session_mod._PageFraming(
    paper_size="full", resolution=300, geometry_set=True
)


@pytest.fixture
def page_sink(tmp_path: Path) -> SpooledPageSink:
    """
    Return the sink this test's ``scan_pages`` call spools into.

    ``scan_pages`` takes a sink as its third argument, and it is the pipeline
    that owns one in production, so every test here supplies its own.

    Returns:
        A sink writing into ``tmp_path``, labelled as a first pass.

    """
    return _page_sink_for(tmp_path)


@pytest.fixture
def second_pass_sink(tmp_path: Path) -> SpooledPageSink:
    """
    Return a second sink, for a test that drives two acquisitions.

    One sink serves one acquisition pass, so a test that scans twice needs two
    of them.  This one carries the pass-B label, which is what a manual-duplex
    job's second pass uses, and writes into its own subdirectory.

    Returns:
        A sink writing into ``tmp_path``, labelled as a second pass.

    """
    return _page_sink_for(tmp_path, _SPOOL_LABEL_B)


@pytest.fixture
def fake_sane_module(monkeypatch: pytest.MonkeyPatch) -> FakeSaneModule:
    """
    Patch the one shared fake into scan_session's module-level ``sane`` name.

    ``_ensure_sane()`` returns whatever is patched into that name before it
    would import python-sane, which is the seam that makes the whole approach
    work.

    The device is asked to report ``"ADF"`` alongside the long feeder name so
    the tests below can exercise both spellings. Both are real -- plenty of
    scanners report the short form, and the SANE ``test`` backend reports the
    long one -- and configuring it through the fake's own knob is what keeps
    there being one definition of the device rather than two.
    """
    device = FakeSaneDev()
    device.report_sources(["Flatbed", "ADF", "ADF Duplex", "Automatic Document Feeder"])
    module = FakeSaneModule(
        device=device,
        devices=[(_TEST_DEVICE, "TestVendor", "TestModel", "scanner")],
    )
    monkeypatch.setattr(scan_session_mod, "sane", module)
    return module


@pytest.fixture
def fake_device(fake_sane_module: FakeSaneModule) -> FakeSaneDev:
    """Return the one device the patched module stands for, without opening it."""
    return fake_sane_module.device


@pytest.fixture
def sane_backend(fake_sane_module: FakeSaneModule) -> SaneBackend:
    """Create a SaneBackend over the shared fake."""
    _ = fake_sane_module  # side-effect: patches the sane module
    return SaneBackend()


class TestTheSuiteResetsTheProcessGlobalSaneState:
    """
    The suite-wide reset really re-arms the init guard, in both its branches.

    The guard is process state, so a module that builds a
    ``SaneBackend`` over a fake and leaves ``_INIT.done`` set silently
    suppresses ``sane.init()`` for every later test in the same process -- and
    the later test that then makes real SANE calls fails with an empty device
    list rather than with anything that names the cause.  ``conftest``'s
    ``sane_process_state`` fixture is what makes that unrepresentable; these
    two tests are what stop it being quietly weakened.
    """

    def test_the_reset_rearms_the_guard_after_a_backend_was_built(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """A constructed backend leaves nothing behind once the reset runs."""
        SaneBackend()
        assert sane_backend_mod._INIT.done is True

        reset_sane_process_state()

        assert sane_backend_mod._INIT.done is False
        assert fake_sane_module.exit_call_count == 1
        # The proof that the guard is genuinely re-armed and not merely
        # reported as such: the next construction initialises again.
        SaneBackend()
        assert fake_sane_module.init_call_count == 2

    def test_the_reset_rearms_the_guard_even_when_a_wedge_was_left_behind(
        self, fake_sane_module: FakeSaneModule, fake_device: FakeSaneDev
    ) -> None:
        """
        A leaked wedge does not strand the guard, and no sane_exit() is risked.

        ``shutdown()`` deliberately refuses while a read is recorded as
        outstanding, because ``sane_exit()`` closes every open handle.  Left
        at that, a test that wedged the backend would set
        ``_INIT.done`` for the rest of the process.  The reset finishes the job
        by hand instead -- and ``exit_call_count`` staying 0 is the assertion
        that it did *not* reach for the unsafe call on the way.
        """
        SaneBackend()
        record = sane_backend_mod._WEDGE
        record.stuck = True
        record.done = threading.Event()
        record.device = fake_device
        record.device_id = "fake:0"
        record.page_label = "Page 1"

        reset_sane_process_state()

        assert sane_backend_mod._INIT.done is False
        assert record.stuck is False
        assert record.device is None
        assert record.done is None
        assert fake_sane_module.exit_call_count == 0


class TestSaneBackendInit:
    """Constructing a SaneBackend initialises SANE and sets its host list."""

    def test_sane_backend_init_calls_sane_init(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """SaneBackend constructor calls sane.init()."""
        assert fake_sane_module.init_call_count == 0
        SaneBackend()
        assert fake_sane_module.init_call_count == 1

    def test_sane_backend_sets_sane_net_hosts(
        self, fake_sane_module: FakeSaneModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SaneBackend(host='192.168.1.50') sets SANE_NET_HOSTS env var."""
        monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
        SaneBackend(host="192.168.1.50")
        assert os.environ["SANE_NET_HOSTS"] == "192.168.1.50"

    def test_sane_backend_does_not_override_existing_env(
        self, fake_sane_module: FakeSaneModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SaneBackend does not override externally-set SANE_NET_HOSTS."""
        monkeypatch.setenv("SANE_NET_HOSTS", "external-host")
        SaneBackend(host="config-host")
        assert os.environ["SANE_NET_HOSTS"] == "external-host"

    def test_sane_backend_no_host_no_env_change(
        self, fake_sane_module: FakeSaneModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SaneBackend() with no host does not set SANE_NET_HOSTS."""
        monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
        SaneBackend()
        assert "SANE_NET_HOSTS" not in os.environ

    def test_sane_backend_multi_host_colon_delimiter(
        self, fake_sane_module: FakeSaneModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SaneBackend with colon-delimited hosts sets multi-host value."""
        monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
        SaneBackend(host="192.168.1.50:192.168.1.51")
        assert os.environ["SANE_NET_HOSTS"] == "192.168.1.50:192.168.1.51"

    def test_sane_backend_treats_an_exported_empty_variable_as_unset(
        self,
        fake_sane_module: FakeSaneModule,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        An exported but empty SANE_NET_HOSTS names no host, so the config wins.

        libsane skips empty entries, so leaving the empty value in place would
        give SANE no host at all while the operator configured one.
        """
        monkeypatch.setenv("SANE_NET_HOSTS", "")
        caplog.set_level(logging.INFO, logger=_BACKEND_LOGGER)
        SaneBackend(host="scanbox.lan")

        assert os.environ["SANE_NET_HOSTS"] == "scanbox.lan"
        assert not any("set externally" in m for m in _backend_messages(caplog))

    def test_sane_backend_logs_an_exported_value_as_external(
        self,
        fake_sane_module: FakeSaneModule,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-empty exported value is kept and named once at INFO."""
        monkeypatch.setenv("SANE_NET_HOSTS", "external-host")
        caplog.set_level(logging.INFO, logger=_BACKEND_LOGGER)
        SaneBackend(host="config-host")

        assert os.environ["SANE_NET_HOSTS"] == "external-host"
        external = [m for m in _backend_messages(caplog) if "set externally" in m]
        assert len(external) == 1
        assert "external-host" in external[0]

    @pytest.mark.parametrize(
        "environment",
        [
            pytest.param(None, id="unset"),
            pytest.param("", id="exported-empty"),
            pytest.param("ext-host", id="exported"),
        ],
    )
    def test_sane_backend_writes_what_the_shared_helper_derives(
        self,
        fake_sane_module: FakeSaneModule,
        monkeypatch: pytest.MonkeyPatch,
        environment: str | None,
    ) -> None:
        """
        The backend and the helper agree on the host list in every state.

        The scanner check probes the helper's answer, so any disagreement
        would have it probe a host SANE never dials.
        """
        if environment is None:
            monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
        else:
            monkeypatch.setenv("SANE_NET_HOSTS", environment)
        expected = effective_sane_net_hosts("cfg-host")

        SaneBackend(host="cfg-host")

        assert os.environ.get("SANE_NET_HOSTS") == expected


def _backend_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """
    Collect every message the backend module logged, at any captured level.

    Args:
        caplog: The capturing fixture, already set to the level of interest.

    Returns:
        One string per record from the backend module's logger.

    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _BACKEND_LOGGER
    ]


def _guard_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """
    Collect the scanner backend's WARNING messages, from either module.

    Args:
        caplog: The capturing fixture, already set to WARNING for the module.

    Returns:
        One string per WARNING the backend logged during the call phase.

    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.name in _SCANNER_LOGGERS and record.levelno == logging.WARNING
    ]


class TestSaneInitGuard:
    """
    SANE is initialised once per process, behind a guard.

    ``sane_init`` is a process-global call: the second one is at best wasted
    and at worst -- on a ``net`` backend, whose host list is read only at the
    first -- an operator's configuration silently doing nothing.  The guard is
    a guard rather than a singleton on purpose: three CLI commands and the web
    server each build their own ``SaneBackend``, and every one of them has to
    keep working.
    """

    def test_init_once_for_two_backends_in_one_process(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """Two SaneBackend constructions call sane.init() once between them."""
        SaneBackend()
        SaneBackend()
        assert fake_sane_module.init_call_count == 1

    def test_init_once_warns_when_a_later_host_differs(
        self,
        fake_sane_module: FakeSaneModule,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A second, different host is named alongside the one in effect."""
        monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
        SaneBackend(host="scanner-a.local")
        caplog.set_level(logging.WARNING, logger=_BACKEND_LOGGER)
        SaneBackend(host="scanner-b.local")

        warnings = _guard_warnings(caplog)
        assert len(warnings) == 1
        assert "scanner-a.local" in warnings[0]
        assert "scanner-b.local" in warnings[0]
        # The second host changes nothing: SANE read the list at the first
        # init, so neither the call count nor the environment may move.
        assert fake_sane_module.init_call_count == 1
        assert os.environ["SANE_NET_HOSTS"] == "scanner-a.local"

    def test_init_once_stays_quiet_when_a_later_host_matches(
        self,
        fake_sane_module: FakeSaneModule,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Repeating the configured host logs no warning."""
        monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
        SaneBackend(host="scanner-a.local")
        caplog.set_level(logging.WARNING, logger=_BACKEND_LOGGER)
        SaneBackend(host="scanner-a.local")

        assert _guard_warnings(caplog) == []
        assert fake_sane_module.init_call_count == 1

    def test_init_once_stays_quiet_when_a_later_backend_has_no_host(
        self,
        fake_sane_module: FakeSaneModule,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No host asks for nothing, so nothing was ignored and nothing warns."""
        monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
        SaneBackend(host="scanner-a.local")
        caplog.set_level(logging.WARNING, logger=_BACKEND_LOGGER)
        SaneBackend()

        assert _guard_warnings(caplog) == []
        assert fake_sane_module.init_call_count == 1

    def test_init_once_resets_after_shutdown(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """A shutdown re-arms the guard, so the next construction initialises."""
        SaneBackend()
        sane_backend_mod.shutdown()
        SaneBackend()
        assert fake_sane_module.init_call_count == 2

    def test_a_re_init_after_shutdown_writes_its_own_host(
        self,
        fake_sane_module: FakeSaneModule,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        Saneless's own earlier value is not mistaken for an operator's.

        Without the restore, the second init would find the first host still
        exported, keep it, and log it as set externally.
        """
        monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
        SaneBackend(host="scanner-a")
        sane_backend_mod.shutdown()
        assert "SANE_NET_HOSTS" not in os.environ

        caplog.set_level(logging.INFO, logger=_BACKEND_LOGGER)
        SaneBackend(host="scanner-b")

        assert os.environ["SANE_NET_HOSTS"] == "scanner-b"
        assert not any("set externally" in m for m in _backend_messages(caplog))

    def test_shutdown_restores_an_exported_empty_variable(
        self, fake_sane_module: FakeSaneModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The previous state is put back exactly, empty rather than absent."""
        monkeypatch.setenv("SANE_NET_HOSTS", "")
        SaneBackend(host="scanner-a")
        assert os.environ["SANE_NET_HOSTS"] == "scanner-a"

        sane_backend_mod.shutdown()

        assert os.environ["SANE_NET_HOSTS"] == ""

    def test_shutdown_leaves_a_value_someone_else_wrote_alone(
        self, fake_sane_module: FakeSaneModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only saneless's own value is restored; a later change is not undone."""
        monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
        SaneBackend(host="scanner-a")
        monkeypatch.setenv("SANE_NET_HOSTS", "operator-host")

        sane_backend_mod.shutdown()

        assert os.environ["SANE_NET_HOSTS"] == "operator-host"

    def test_a_failed_init_puts_the_variable_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed init records nothing, the environment included."""
        failing = FakeSaneModule(init_error=FakeSaneError("no SANE here"))
        monkeypatch.setattr(scan_session_mod, "sane", failing)
        monkeypatch.delenv("SANE_NET_HOSTS", raising=False)

        with pytest.raises(ScanError, match="Could not initialise SANE"):
            SaneBackend(host="scanner-a")

        assert "SANE_NET_HOSTS" not in os.environ

    def test_the_later_host_warning_names_the_effective_host_list(
        self,
        fake_sane_module: FakeSaneModule,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        The warning names what SANE is using, not the ignored configured host.

        With an exported value in effect, the first construction's host was
        never used, so naming it would send the operator to the wrong place.
        """
        monkeypatch.setenv("SANE_NET_HOSTS", "ext-host")
        SaneBackend(host="scanner-a")
        caplog.set_level(logging.WARNING, logger=_BACKEND_LOGGER)
        SaneBackend(host="scanner-b")

        warnings = _guard_warnings(caplog)
        assert len(warnings) == 1
        assert "ext-host" in warnings[0]
        assert "scanner-b" in warnings[0]
        assert "scanner-a" not in warnings[0]

    def test_init_once_under_concurrent_construction(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """
        Threads racing to build a backend still produce one init call.

        The web server builds its backend on the main thread while the worker
        thread is already running, so the check and the call have to be one
        critical section rather than merely close together.
        """
        thread_count = 8
        ready = threading.Barrier(thread_count)
        failures: list[BaseException] = []

        def build() -> None:
            ready.wait(5)
            try:
                SaneBackend()
            except Exception as exc:
                # Carried back to the test thread: an exception raised here
                # would be reported as an unhandled thread exception with no
                # assertion attached to it.
                failures.append(exc)

        threads = [
            threading.Thread(target=build, name=f"init-race-{i}", daemon=True)
            for i in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        assert not any(thread.is_alive() for thread in threads)
        assert failures == []
        assert fake_sane_module.init_call_count == 1

    def test_init_once_is_not_recorded_when_init_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed init raises and leaves the guard unset, so a retry runs."""
        failing = FakeSaneModule(init_error=FakeSaneError("no SANE here"))
        monkeypatch.setattr(scan_session_mod, "sane", failing)
        with pytest.raises(ScanError, match="Could not initialise SANE: no SANE here"):
            SaneBackend()
        assert failing.init_call_count == 1

        working = FakeSaneModule()
        monkeypatch.setattr(scan_session_mod, "sane", working)
        SaneBackend()
        assert working.init_call_count == 1

    def test_backend_stays_freely_constructible(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """
        The guard guards init, not construction: no singleton, no factory.

        Every one-shot CLI command builds its own backend and the web server
        builds another; a guard that returned a shared instance would change
        what ``SaneBackend()`` means for all of them.
        """
        first = SaneBackend()
        second = SaneBackend()
        assert first is not second
        assert fake_sane_module.init_call_count == 1


def _failing_exit() -> None:
    """
    Stand in for a ``sane.exit()`` that raises, as a broken backend's does.

    Raises:
        FakeSaneError: Always; that is what the caller is testing against.

    """
    msg = "sane_exit failed"
    raise FakeSaneError(msg)


def _assert_shutdown_failure_logged(caplog: pytest.LogCaptureFixture) -> None:
    """
    Assert a swallowed ``sane.exit()`` failure left one WARNING with its cause.

    Args:
        caplog: The test's log capture fixture.

    """
    failures = [
        record
        for record in caplog.records
        if record.getMessage() == "Could not shut SANE down"
    ]
    assert len(failures) == 1
    assert failures[0].levelno == logging.WARNING
    assert failures[0].exc_info is not None
    assert isinstance(failures[0].exc_info[1], FakeSaneError)


class TestSaneShutdown:
    """
    SANE is shut down at an entry point, once, and never over an error.

    ``shutdown()`` is the other half of the init guard: the entry point that
    owns the process calls it when the process is ending, and nothing else
    calls it at all.  ``atexit`` is not used and must not be, because it runs
    while a daemon reader thread may still be inside ``sane_read``.
    """

    def test_shutdown_calls_sane_exit_once_however_often_it_is_called(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """Repeated shutdowns, one per backend, call sane_exit once."""
        SaneBackend()
        sane_backend_mod.shutdown()
        sane_backend_mod.shutdown()
        assert fake_sane_module.exit_call_count == 1

    def test_shutdown_calls_nothing_when_sane_was_never_initialised(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """Without an init, shutdown calls nothing: sane_exit would be undefined."""
        sane_backend_mod.shutdown()
        assert fake_sane_module.exit_call_count == 0

    def test_shutdown_never_raises_when_sane_exit_fails(
        self,
        fake_sane_module: FakeSaneModule,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A failing shutdown is logged and swallowed.

        It runs while the process is on its way out, under a click close
        callback or a lifespan shutdown, where a raise would replace whatever
        error the operator is actually being shown.
        """
        SaneBackend()

        monkeypatch.setattr(fake_sane_module, "exit", _failing_exit)
        with caplog.at_level(logging.WARNING, logger=sane_backend_mod.__name__):
            sane_backend_mod.shutdown()

        _assert_shutdown_failure_logged(caplog)

    def test_shutdown_re_arms_the_guard_even_when_sane_exit_failed(
        self, fake_sane_module: FakeSaneModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed exit still re-arms the guard, so SANE can initialise again."""
        SaneBackend()
        working_exit = fake_sane_module.exit

        monkeypatch.setattr(fake_sane_module, "exit", _failing_exit)
        sane_backend_mod.shutdown()

        # Only the exit is restored: a blanket undo would take the whole fake
        # module with it and send the next construction at the real one.
        monkeypatch.setattr(fake_sane_module, "exit", working_exit)
        SaneBackend()
        assert fake_sane_module.init_call_count == 2

    def test_close_delegates_to_shutdown(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """An entry point closes the backend it holds; SANE goes down with it."""
        backend = SaneBackend()
        backend.close()
        assert fake_sane_module.exit_call_count == 1

    def test_close_overrides_the_base_no_op(self) -> None:
        """
        The abstraction is what the entry points close through.

        ``create_app`` and the CLI hold a ``ScannerBackend``, so the shutdown
        has to arrive through the declared method rather than by naming the
        concrete class.
        """
        assert SaneBackend.close is not ScannerBackend.close

    def test_close_never_raises(
        self,
        fake_sane_module: FakeSaneModule,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Closing never raises, so it cannot replace the operator's error."""
        backend = SaneBackend()
        monkeypatch.setattr(fake_sane_module, "exit", _failing_exit)
        with caplog.at_level(logging.WARNING, logger=sane_backend_mod.__name__):
            backend.close()

        _assert_shutdown_failure_logged(caplog)

    @pytest.mark.parametrize(
        "module",
        [saneless, sane_backend_mod, cli_module, app_module, server_module],
        ids=["saneless", "sane_backend", "cli", "web.app", "web.server"],
    )
    def test_no_entry_point_installs_an_interpreter_exit_hook(
        self, module: ModuleType
    ) -> None:
        """
        Teardown is explicit at the entry point, never at interpreter exit.

        An interpreter-exit hook runs while a daemon reader thread may still
        be inside ``sane_read``, which is the one sequence ``sane_exit`` --
        which closes every open handle, holding the GIL -- cannot survive.

        Python offers no way to ask which callbacks are registered, so the
        absence is asserted where it is decided: no module here imports the
        registry, and none of them calls anything that registers.  The check
        is structural, because the backend's docstrings discuss exit hooks.
        """
        tree = ast.parse(inspect.getsource(module))
        imported = {
            alias.name.partition(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.partition(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "atexit" not in imported

        called = [
            ast.unparse(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        ]
        assert [name for name in called if "register" in name] == []


class TestSaneBackendGetDevices:
    """SaneBackend lists devices as DeviceInfo records."""

    def test_sane_backend_get_devices(self, sane_backend: SaneBackend) -> None:
        """get_devices returns DeviceInfo objects from sane.get_devices()."""
        devices = sane_backend.get_devices()
        assert len(devices) == 1
        assert isinstance(devices[0], DeviceInfo)
        assert devices[0].name == "test:device:001"
        assert devices[0].vendor == "TestVendor"
        assert devices[0].model == "TestModel"
        assert devices[0].device_type == "scanner"


# A device id the fake never lists, shaped like the ids the Scanner check opens.
_NET_DEVICE = "net:scanbox.lan:test:0"

# The one device the stand-in child reports, as the backend returns it.
_STAND_IN_DEVICE = DeviceInfo(
    name="stand:in", vendor="Stand", model="In", device_type="virtual device"
)

# A stand-in listing child: it reads the request and reports one device.
_STAND_IN_CHILD = """\
import sys

sys.stdin.readline()
sys.stdout.write(
    '{"devices": [["stand:in", "Stand", "In", "virtual device"]]}' + "\\n"
)
"""


def _raise(error: Exception) -> Callable[..., NoReturn]:
    """
    Build a stand-in listing launcher that raises ``error``.

    Args:
        error: What every listing raises.

    Returns:
        A launcher accepting any arguments.

    """

    def launch(*_args: object, **_kwargs: object) -> NoReturn:
        raise error

    return launch


class TestTheMainProcessNeverLists:
    """
    Scanners are listed in a child process, never in this one.

    Listing in the process that already holds libsane's net control
    connections is what crashes it, once a scanner host's saned has restarted.
    So no module calls python-sane's ``get_devices``, and ``SaneBackend``
    hands both its listing and the Scanner check's list-then-open to its
    launcher.  The suite's seam runs that launcher's child logic in this
    process over the fake; the runtime guard below puts the real launcher
    back to show the fake is never asked.
    """

    def test_the_main_process_never_lists_in_process_static(self) -> None:
        """
        Only the child script calls ``get_devices`` on anything but a backend.

        Structural rather than textual, as the interpreter-exit test above is:
        docstrings name ``sane_get_devices`` freely.  The receivers allowed
        are the names the scan path, the worker, the CLI and the checks give
        a ``ScannerBackend``, and ``self`` inside a backend.
        """
        package = Path(saneless.__file__).parent
        child = package / "scanner" / "_listing_child.py"
        receivers = {
            f"{path.relative_to(package)}: {ast.unparse(node.func.value)}"
            for path in sorted(package.rglob("*.py"))
            if path != child
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_devices"
        }
        allowed = {"scanner", "self._scanner", "self"}

        assert receivers, "the walk found no get_devices call at all"
        assert {
            receiver
            for receiver in receivers
            if receiver.partition(": ")[2] not in allowed
        } == set()

    def test_the_main_process_never_lists_in_process_runtime(
        self,
        fake_sane_module: FakeSaneModule,
        stand_in_listing_child: Callable[[str], Path],
    ) -> None:
        """
        A real backend over the fake lists through a real child process.

        The fake is patched in and would answer with its own device, so the
        stand-in's device coming back, with the fake never asked, is what
        shows the listing left this process.
        """
        stand_in_listing_child(_STAND_IN_CHILD)

        devices = SaneBackend(host="scanbox.lan").get_devices()

        assert devices == [_STAND_IN_DEVICE]
        assert fake_sane_module.get_devices_call_count == 0

    def test_list_and_open_opens_an_unlisted_device_and_closes_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An id the listing lacks is opened, cancelled and closed once."""
        device = FakeSaneDev()
        monkeypatch.setattr(
            scan_session_mod, "sane", FakeSaneModule(device=device, devices=[])
        )

        survey = SaneBackend().list_and_open(_NET_DEVICE)

        assert survey == scanner_base.DeviceSurvey(devices=(), configured_opened=True)
        assert device.cancel_calls == 1
        assert device.close_calls == 1

    def test_list_and_open_reports_a_failed_open_by_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The open's failure comes back as its class name, and no text."""
        monkeypatch.setattr(
            scan_session_mod,
            "sane",
            FakeSaneModule(devices=[], open_error=FakeSaneError("Invalid argument")),
        )

        survey = SaneBackend().list_and_open(_NET_DEVICE)

        assert survey.devices == ()
        assert survey.list_error is None
        assert survey.configured_opened is False
        assert survey.open_error == "FakeSaneError"

    def test_list_and_open_reports_a_failed_listing_by_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A listing that raised leaves no devices and its class name."""
        monkeypatch.setattr(
            scan_session_mod,
            "sane",
            FakeSaneModule(get_devices_error=RuntimeError("boom")),
        )

        survey = SaneBackend().list_and_open("")

        assert survey.devices == ()
        assert survey.list_error == "RuntimeError"
        assert survey.configured_opened is None
        assert survey.open_error is None

    def test_list_and_open_does_not_open_a_listed_device(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An id the listing already has is not opened at all."""
        device = FakeSaneDev()
        monkeypatch.setattr(
            scan_session_mod,
            "sane",
            FakeSaneModule(
                device=device, devices=[(_NET_DEVICE, "Vendor", "Model", "scanner")]
            ),
        )

        survey = SaneBackend().list_and_open(_NET_DEVICE)

        assert survey.devices == (
            DeviceInfo(
                name=_NET_DEVICE, vendor="Vendor", model="Model", device_type="scanner"
            ),
        )
        assert survey.configured_opened is None
        assert device.close_calls == 0

    def test_the_configured_host_reaches_the_child_for_list_and_open(
        self,
        fake_sane_module: FakeSaneModule,
        listing_seam: ListingSeam,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The child's host is the one the backend was built with.

        Not whatever this process's environment holds at the moment: a value
        changed after the backend was built does not reach the listing.
        """
        _ = fake_sane_module  # side-effect: patches the sane module
        backend = SaneBackend(host="scanbox.lan")

        backend.get_devices()
        assert listing_seam.calls[-1] == (ListingRequest(), "scanbox.lan")

        monkeypatch.setenv("SANE_NET_HOSTS", "other.lan")
        backend.get_devices()
        assert listing_seam.calls[-1] == (ListingRequest(), "scanbox.lan")

        backend.list_and_open(_NET_DEVICE)
        assert listing_seam.calls[-1] == (
            ListingRequest(open=_NET_DEVICE),
            "scanbox.lan",
        )
        backend.list_and_open("")
        assert listing_seam.calls[-1] == (ListingRequest(), "scanbox.lan")

    def test_list_and_open_passes_abort_to_the_launcher(
        self, fake_sane_module: FakeSaneModule, listing_seam: ListingSeam
    ) -> None:
        """
        The caller's abort Event reaches the launcher, which watches it.

        A listing made without one is given none, so only a caller that can
        be stopped hands its child an abort.
        """
        _ = fake_sane_module  # side-effect: patches the sane module
        backend = SaneBackend()
        abort = threading.Event()

        backend.list_and_open(_NET_DEVICE, abort=abort)
        assert listing_seam.aborts[-1] is abort

        backend.list_and_open(_NET_DEVICE)
        assert listing_seam.aborts[-1] is None
        backend.get_devices()
        assert listing_seam.aborts[-1] is None

    @pytest.mark.parametrize(
        "error",
        [
            ListingCrashedError(
                "The scanner library failed while listing scanners (SIGSEGV)"
            ),
            ListingTimedOutError(
                "The scanner library did not finish listing scanners in time (30 s)"
            ),
        ],
        ids=["crashed", "timed-out"],
    )
    def test_a_failed_child_propagates_from_get_devices_and_list_and_open(
        self,
        sane_backend: SaneBackend,
        monkeypatch: pytest.MonkeyPatch,
        error: ScanError,
    ) -> None:
        """A crashed or stopped child is raised as it is, never re-wrapped."""
        monkeypatch.setattr(
            sane_backend_mod, "_launch_listing", _raise(error), raising=False
        )

        with pytest.raises(type(error)) as listed:
            sane_backend.get_devices()
        assert listed.value is error
        with pytest.raises(type(error)) as surveyed:
            sane_backend.list_and_open(_NET_DEVICE)
        assert surveyed.value is error

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            pytest.param("boom", "Could not list scanners: boom", id="plain"),
            pytest.param(
                "boom\n  again", "Could not list scanners: boom again", id="multiline"
            ),
            pytest.param(
                "red \x1b[31mboom",
                "Could not list scanners: red \\x1b[31mboom",
                id="escape",
            ),
        ],
    )
    def test_a_listing_error_from_the_child_is_a_scan_error(
        self, monkeypatch: pytest.MonkeyPatch, message: str, expected: str
    ) -> None:
        """
        The child's reported error is normalised and defused, then raised.

        The text came from libsane, which can repeat what a LAN peer sent, so
        a control character in it is escaped before it reaches any sink.
        """
        monkeypatch.setattr(
            scan_session_mod,
            "sane",
            FakeSaneModule(get_devices_error=FakeSaneError(message)),
        )

        with pytest.raises(ScanError) as exc_info:
            SaneBackend().get_devices()

        assert str(exc_info.value) == expected
        assert not has_control_characters(str(exc_info.value))

    def test_open_and_close_opens_only_in_the_listing_child(
        self,
        fake_sane_module: FakeSaneModule,
        listing_seam: ListingSeam,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The real backend's open-and-close runs in a listing child.

        The base default opens through ``get_capabilities``, in this process,
        where this backend's own open and close logging can name the device.
        """
        _ = fake_sane_module  # side-effect: patches the sane module

        def open_in_this_process(*_args: object) -> NoReturn:
            msg = "opened a device in this process"
            raise AssertionError(msg)

        monkeypatch.setattr(SaneBackend, "_open_device", open_in_this_process)
        backend = SaneBackend(host="scanbox.lan")

        backend.open_and_close(_NET_DEVICE)

        assert listing_seam.calls[-1] == (
            ListingRequest(open=_NET_DEVICE),
            "scanbox.lan",
        )

    def test_a_failed_open_in_the_child_is_a_scan_error_naming_no_device(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The error carries the failure's class name, never the id or its text."""
        monkeypatch.setattr(
            scan_session_mod,
            "sane",
            FakeSaneModule(
                devices=[], open_error=FakeSaneError(f"cannot reach {_NET_DEVICE}")
            ),
        )

        with pytest.raises(ScanError) as failed:
            SaneBackend().open_and_close(_NET_DEVICE)

        assert str(failed.value) == "Could not open the scanner (FakeSaneError)"

    def test_a_failed_open_with_no_reason_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reply that says the open failed but not why never reads "(None)"."""

        def unexplained_failure(
            _request: ListingRequest,
            *,
            configured_host: str,
            abort: threading.Event | None = None,
        ) -> ListingReply:
            _ = configured_host, abort
            return ListingReply(devices=(), opened=False)

        monkeypatch.setattr(sane_backend_mod, "_launch_listing", unexplained_failure)

        with pytest.raises(ScanError) as failed:
            SaneBackend().open_and_close(_NET_DEVICE)

        assert str(failed.value) == "Could not open the scanner (unknown error)"

    def test_an_empty_device_id_is_refused_without_a_listing(
        self, fake_sane_module: FakeSaneModule, listing_seam: ListingSeam
    ) -> None:
        """
        An empty id opens nothing, so it cannot be reported as opened.

        The listing child reads an empty id as a request to list only.
        """
        _ = fake_sane_module  # side-effect: patches the sane module

        with pytest.raises(ScanError) as failed:
            SaneBackend().open_and_close("")

        assert str(failed.value) == "No scanner was named to open"
        assert listing_seam.calls == []

    def test_the_seam_refuses_to_start_real_libsane(self) -> None:
        """With no fake patched in, a listing fails the test instead of running."""
        assert scan_session_mod.sane is None

        with pytest.raises(AssertionError, match="must not start real libsane"):
            sane_backend_mod._launch_listing(ListingRequest(), configured_host="")


class _SurveyedBackend(StubScannerBackend):
    """A stub whose listing and open are scripted and whose opens are recorded."""

    def __init__(
        self,
        devices: list[DeviceInfo] | None = None,
        *,
        list_error: Exception | None = None,
        open_error: Exception | None = None,
    ) -> None:
        """
        Script the listing and the open.

        Args:
            devices: What ``get_devices`` returns.
            list_error: What ``get_devices`` raises instead, if anything.
            open_error: What ``open_and_close`` raises, if anything.

        """
        self.devices = devices or []
        self.list_error = list_error
        self.open_error = open_error
        self.opened: list[str] = []

    def get_devices(self) -> list[DeviceInfo]:
        """
        Return the scripted devices, or raise the scripted error.

        Returns:
            A copy of the scripted devices.

        Raises:
            Exception: The scripted ``list_error``.

        """
        if self.list_error is not None:
            raise self.list_error
        return list(self.devices)

    def open_and_close(self, device_id: str) -> None:
        """
        Record the open, and raise the scripted error if there is one.

        Args:
            device_id: The device opened.

        Raises:
            Exception: The scripted ``open_error``.

        """
        self.opened.append(device_id)
        if self.open_error is not None:
            raise self.open_error


_LISTED = DeviceInfo(name="listed:0", vendor="V", model="M", device_type="scanner")


class TestTheBaseListAndOpen:
    """The base ``list_and_open``: list, then open an unlisted id, as two calls."""

    def test_list_and_open_with_no_id_opens_nothing(self) -> None:
        """No configured id means no open."""
        backend = _SurveyedBackend([_LISTED])

        survey = backend.list_and_open("")

        assert survey == scanner_base.DeviceSurvey(devices=(_LISTED,))
        assert backend.opened == []

    def test_list_and_open_opens_an_unlisted_id_once(self) -> None:
        """An id missing from the listing is opened once."""
        backend = _SurveyedBackend([_LISTED])

        survey = backend.list_and_open("absent:0")

        assert survey.configured_opened is True
        assert survey.open_error is None
        assert backend.opened == ["absent:0"]

    def test_list_and_open_does_not_open_a_listed_id(self) -> None:
        """An id the listing has is not opened."""
        backend = _SurveyedBackend([_LISTED])

        survey = backend.list_and_open("listed:0")

        assert survey.configured_opened is None
        assert backend.opened == []

    def test_list_and_open_reports_a_failed_open_by_type(self) -> None:
        """The open's failure is its class name."""
        backend = _SurveyedBackend([], open_error=ScanError("Could not open"))

        survey = backend.list_and_open("absent:0")

        assert survey.configured_opened is False
        assert survey.open_error == "ScanError"

    def test_list_and_open_reports_a_failed_listing_and_still_opens(self) -> None:
        """A listing that raised is its class name, and the open still runs."""
        backend = _SurveyedBackend(list_error=ScanError("Could not list"))

        survey = backend.list_and_open("absent:0")

        assert survey.devices == ()
        assert survey.list_error == "ScanError"
        assert survey.configured_opened is True
        assert backend.opened == ["absent:0"]

    @pytest.mark.parametrize(
        "error",
        [ListingCrashedError("crashed"), ListingTimedOutError("timed out")],
        ids=["crashed", "timed-out"],
    )
    def test_list_and_open_re_raises_a_failed_child(self, error: ScanError) -> None:
        """A crashed or stopped listing is not a failed listing: it propagates."""
        backend = _SurveyedBackend(list_error=error)

        with pytest.raises(type(error)) as raised:
            backend.list_and_open("absent:0")

        assert raised.value is error
        assert backend.opened == []


class TestSaneBackendScanPages:
    """scan_pages opens the device, spools, then cancels and closes it."""

    def test_sane_backend_scan_pages_opens_and_closes_device(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """scan_pages opens the device, spools one page, then closes it."""
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages
        assert len(pages) == 1
        assert pages[0].sequence == 1
        assert pages[0].path.exists()
        mock_dev = fake_sane_module.device
        assert mock_dev.close_calls

    def test_sane_backend_cancel_before_close(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """Device cancel() is called before close() on normal exit."""
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        sane_backend.scan_pages("test:device:001", settings, page_sink)
        mock_dev = fake_sane_module.device
        assert mock_dev.cancel_calls
        assert mock_dev.close_calls

    def test_sane_backend_cancel_before_close_on_error(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """Device cancel() and close() are called even when the scan raises."""
        # The fault is armed on the device rather than by swapping out its snap
        # method: start() is where a flatbed scan first touches the hardware,
        # and the fake raises from there with the library's own error type.
        # The library's error leaves the backend as a ScanError naming the
        # device, with the original kept as its cause.
        original = FakeSaneError("scan failed")
        dev = FakeSaneDev(start_error=original)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", settings, page_sink)

        assert str(exc_info.value) == "Scanner error on test:0: scan failed"
        assert exc_info.value.__cause__ is original
        assert dev.cancel_calls
        assert dev.close_calls

    def test_sane_backend_no_progress_callback(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """snap() is called with no progress callback."""
        mock_dev = fake_sane_module.device

        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        sane_backend.scan_pages("test:device:001", settings, page_sink)

        # snap() should be called with no arguments (no progress callback)
        assert mock_dev.calls.count("snap") == 1

    @pytest.mark.usefixtures("fake_sane_module")
    def test_sane_backend_validates_source_option(
        self, page_sink: SpooledPageSink
    ) -> None:
        """Requesting an unsupported source raises ScanError."""
        backend = SaneBackend()
        settings = ScanSettings(
            source="NonExistentSource", resolution=300, mode="Color"
        )

        with pytest.raises(ScanError, match="NonExistentSource"):
            backend.scan_pages("test:device:001", settings, page_sink)


# The child-backed reads that turn a child's start failure into an error.
_CHILD_BACKED_READS = [
    pytest.param(
        operator.methodcaller("get_capabilities", "test:0"), id="capabilities"
    ),
    pytest.param(operator.methodcaller("get_devices"), id="devices"),
]


class TestSaneBackendGetCapabilities:
    """get_capabilities reports what the device offers, as it gave it."""

    def test_sane_backend_get_capabilities(self, sane_backend: SaneBackend) -> None:
        """get_capabilities reports the sources, modes and resolution support."""
        caps = sane_backend.get_capabilities("test:device:001")
        assert isinstance(caps, DeviceCapabilities)
        assert "Flatbed" in caps.sources
        assert "ADF" in caps.sources
        assert "ADF Duplex" in caps.sources
        assert "Color" in caps.modes
        assert "Gray" in caps.modes
        # The shared fake constrains resolution with a range, which is what the
        # real SANE ``test`` backend does.
        # The word list stays empty because the device reported no word list --
        # the two are different facts and neither is derived from the other.
        assert caps.resolution_range == (1.0, 1200.0, 1.0)
        assert caps.resolutions == []
        assert len(caps.option_names) > 0

    def test_get_capabilities_reports_option_names_in_device_order(
        self, sane_backend: SaneBackend
    ) -> None:
        """
        The option names come out as plain strings, in the device's order.

        The backend-agnostic value object carries names, not SANE's
        nine-element option tuples, so nothing outside the SANE backend has to
        know where in a tuple the name sits.
        """
        caps = sane_backend.get_capabilities("test:device:001")

        expected = tuple(str(opt[1]) for opt in build_option_table())
        assert caps.option_names == expected
        assert all(type(name) is str for name in caps.option_names)

    def test_option_names_skip_blank_entries(
        self,
        fake_sane_module: FakeSaneModule,
        sane_backend: SaneBackend,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        An entry with no name is not an option name; the rest keep their order.

        A real device reports the option count with the name '' and each
        group heading with None.  Neither can be read or set, and listed they
        print as a blank line and the word None.
        """
        device = fake_sane_module.device
        named = device.get_options()
        count, group = UNNAMED_OPTION_ENTRIES

        def with_unnamed() -> list[tuple]:
            return [count, *named[:2], group, *named[2:]]

        # The device stores a name that is not one of its options on itself,
        # as python-sane does, so this shadows get_options for this device.
        monkeypatch.setattr(device, "get_options", with_unnamed)

        caps = sane_backend.get_capabilities("test:device:001")

        assert caps.option_names == tuple(str(opt[1]) for opt in named)

    def test_get_capabilities_asks_a_listing_child_and_opens_nothing_here(
        self,
        fake_sane_module: FakeSaneModule,
        listing_seam: ListingSeam,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The capability read is a listing child's capabilities request.

        The open, the option read and the close all happen in the child, so
        this process's own open is never reached.
        """
        _ = fake_sane_module  # side-effect: patches the sane module

        def open_in_this_process(*_args: object) -> NoReturn:
            msg = "opened a device in this process"
            raise AssertionError(msg)

        monkeypatch.setattr(SaneBackend, "_open_device", open_in_this_process)
        backend = SaneBackend(host="scanbox.lan")

        caps = backend.get_capabilities(_NET_DEVICE)

        assert listing_seam.calls[-1] == (
            ListingRequest(capabilities=_NET_DEVICE),
            "scanbox.lan",
        )
        assert "ADF Duplex" in caps.sources
        assert caps.resolution_range == (1.0, 1200.0, 1.0)

    def test_a_malformed_range_from_the_child_is_ignored_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A range the child reports with the wrong arity is not guessed at."""
        caplog.set_level(logging.WARNING, logger=options_mod.__name__)

        caps = _capabilities_for((1.0, 1200.0), monkeypatch)

        assert caps.resolution_range is None
        assert caps.resolutions == []
        assert any(
            "not a (minimum, maximum, step) triple" in record.getMessage()
            for record in caplog.records
        )

    def test_a_capabilities_reply_without_options_is_no_answer(
        self, fake_sane_module: FakeSaneModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reply that names no options and no failure is not an empty device."""
        _ = fake_sane_module  # side-effect: patches the sane module
        backend = SaneBackend()

        def reply_without_options(*_args: object, **_kwargs: object) -> ListingReply:
            return ListingReply(devices=())

        monkeypatch.setattr(sane_backend_mod, "_launch_listing", reply_without_options)

        with pytest.raises(ListingNoAnswerError, match="returned no options"):
            backend.get_capabilities("test:0")

    @pytest.mark.parametrize("read", _CHILD_BACKED_READS)
    def test_a_sane_that_will_not_start_in_the_child_is_a_scan_error(
        self,
        read: Callable[[SaneBackend], object],
        fake_sane_module: FakeSaneModule,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A child whose ``sane.init()`` fails reports it as SANE not starting.

        The backend is built while this process's SANE still starts, then the
        child's fails: the start failure is the child's to report.
        """
        _ = fake_sane_module  # side-effect: patches the sane module
        backend = SaneBackend()
        failing = FakeSaneModule(
            init_error=FakeSaneError("Error during\n device I/O\x1b[31m")
        )
        monkeypatch.setattr(scan_session_mod, "sane", failing)

        with pytest.raises(ScanError) as exc_info:
            read(backend)

        assert not isinstance(exc_info.value, ConfigError)
        assert str(exc_info.value) == (
            "Could not initialise SANE: Error during device I/O\\x1b[31m"
        )

    @pytest.mark.parametrize("error_type", [ImportError, ModuleNotFoundError])
    @pytest.mark.parametrize("read", _CHILD_BACKED_READS)
    def test_a_child_that_cannot_import_python_sane_gives_the_install_hint(
        self,
        read: Callable[[SaneBackend], object],
        error_type: type[ImportError],
        fake_sane_module: FakeSaneModule,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A child that cannot load python-sane is today's install-hint error."""
        _ = fake_sane_module  # side-effect: patches the sane module
        backend = SaneBackend()
        missing = FakeSaneModule(init_error=error_type("No module named 'sane'"))
        monkeypatch.setattr(scan_session_mod, "sane", missing)

        with pytest.raises(ConfigError) as exc_info:
            read(backend)

        assert str(exc_info.value) == python_sane_missing_message(
            "No module named 'sane'"
        )
        assert exc_info.value.next_step == PYTHON_SANE_INSTALL_NEXT_STEP

    @pytest.mark.parametrize(
        "error",
        [FakeSaneError("Error during device I/O"), ModuleNotFoundError("no sane")],
        ids=["init", "import"],
    )
    def test_list_and_open_reports_the_start_failure_by_class(
        self,
        error: Exception,
        fake_sane_module: FakeSaneModule,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        The Scanner check's survey names the class of a child's start failure.

        It is logged once with its reason, which is a SANE status string or
        an import error and names no device.
        """
        _ = fake_sane_module  # side-effect: patches the sane module
        backend = SaneBackend()
        monkeypatch.setattr(scan_session_mod, "sane", FakeSaneModule(init_error=error))
        caplog.set_level(logging.WARNING, logger=_BACKEND_LOGGER)

        survey = backend.list_and_open("")

        assert survey.start_error == type(error).__name__
        started = [
            record.getMessage()
            for record in caplog.records
            if "could not be started" in record.getMessage()
        ]
        assert started == [
            f"The scanner library could not be started: {error}",
        ]

    def test_list_and_open_reports_no_start_failure_for_a_sane_that_starts(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """A child whose SANE starts leaves the survey's start failure empty."""
        _ = fake_sane_module  # side-effect: patches the sane module

        survey = SaneBackend().list_and_open("")

        assert survey.start_error is None
        assert survey.devices


# ---------------------------------------------------------------------------
# ADF scan tests
# ---------------------------------------------------------------------------


class TestSaneBackendADFScan:
    """A feeder scan goes through multi_scan, a flatbed scan through snap."""

    def test_adf_scan_yields_all_pages(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """ADF scan via multi_scan spools all 3 pages, in feed order."""
        _ = fake_sane_module  # fixture provides mock device
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages
        assert len(pages) == 3
        # Order is the records' own, never the directory's.
        assert [page.sequence for page in pages] == [1, 2, 3]
        assert all(page.path.exists() for page in pages)

    def test_adf_scan_uses_multi_scan_not_snap(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """ADF scan calls multi_scan(), not snap()."""
        mock_dev = fake_sane_module.device

        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        sane_backend.scan_pages("test:device:001", settings, page_sink)

        assert mock_dev.calls == _THREE_SHEET_FEEDER_CALLS

    def test_flatbed_still_uses_snap(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """Flatbed scan uses snap(), not multi_scan()."""
        mock_dev = fake_sane_module.device

        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages

        assert len(pages) == 1
        assert mock_dev.calls.count("snap") == 1


class TestSaneBackendAutomaticDocumentFeeder:
    """A feeder whose name contains no "adf" token routes to the feeder."""

    def test_automatic_document_feeder_yields_all_pages(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        Automatic Document Feeder uses multi_scan and spools every page.

        The SANE ``test`` backend names its feeder "Automatic Document Feeder",
        with no "adf" token anywhere in the string.  Sent down the flatbed
        ``start()``/``snap()`` branch, a ten-page stack would produce one page;
        here all three fake pages are spooled and ``snap()`` is never called.
        """
        mock_dev = fake_sane_module.device
        mock_dev.report_sources(["Flatbed", "Automatic Document Feeder"])

        settings = ScanSettings(
            source="Automatic Document Feeder", resolution=300, mode="Color"
        )
        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages

        assert len(pages) == 3
        assert [page.sequence for page in pages] == [1, 2, 3]
        assert mock_dev.calls == _THREE_SHEET_FEEDER_CALLS


class TestSaneBackendDuplex:
    """A duplex feeder scan goes through multi_scan in the hardware's order."""

    def test_duplex_scan_yields_pages(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """ADF Duplex spools pages pre-interleaved by the hardware."""
        _ = fake_sane_module  # fixture provides mock device
        settings = ScanSettings(source="ADF Duplex", resolution=300, mode="Color")
        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages
        assert len(pages) == 3
        assert [page.sequence for page in pages] == [1, 2, 3]

    def test_duplex_scan_uses_multi_scan(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """ADF Duplex uses multi_scan(), not snap()."""
        mock_dev = fake_sane_module.device

        settings = ScanSettings(source="ADF Duplex", resolution=300, mode="Color")
        sane_backend.scan_pages("test:device:001", settings, page_sink)

        assert mock_dev.calls == _THREE_SHEET_FEEDER_CALLS


class TestSaneBackendEmptyFeeder:
    """A feeder that yields no page raises FeederEmptyError."""

    # ``multi_scan()`` itself is a one-line ``return _SaneIterator(self)`` that
    # cannot raise, so nothing here makes it raise.  A fault arriving from the
    # iterator is covered by TestAdfPageErrorsAreTruthful, and the zero-page
    # path below.

    def test_empty_feeder_stop_iteration(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """Multi_scan that yields zero pages raises FeederEmptyError."""
        mock_dev = fake_sane_module.device
        mock_dev.load_feeder([])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")

        with pytest.raises(FeederEmptyError, match="No paper detected in feeder"):
            backend.scan_pages("test:device:001", settings, page_sink)


# The four first-page faults the real SANE ``test`` backend raises with
# ``read_return_value`` set to the matching status.  None of them is an empty
# feeder, and none may reach the operator as "No paper detected in feeder".
_MEASURED_SANE_FAULTS = [
    "Error during device I/O",
    "Document feeder jammed",
    "Scanner cover is open",
    "Device busy",
]

# The one message python-sane converts to StopIteration
# (in _SaneIterator.__next__).
_OUT_OF_DOCUMENTS = "Document feeder out of documents"


def _feeder_settings() -> ScanSettings:
    """
    Build settings that route to the ADF through the shared fake's options.

    The mode is the device's own spelling, ``"Color"``.  The fake would also
    take ``"color"``, as libsane does, by matching a case-differing unique
    prefix and storing the listed entry; using the listed spelling keeps these
    tests about feeder routing rather than about that matching.

    Returns:
        Settings whose source classifies as a feeder.

    """
    return ScanSettings(
        source="Automatic Document Feeder", resolution=300, mode="Color"
    )


def _backend_with(dev: FakeSaneDev, monkeypatch: pytest.MonkeyPatch) -> SaneBackend:
    """
    Wire a configured fake device into SaneBackend via the sane module seam.

    Args:
        dev: The device the backend should open.
        monkeypatch: Fixture used to patch the module-level ``sane`` name.

    Returns:
        A backend whose ``open()`` returns ``dev``.

    """
    monkeypatch.setattr(scan_session_mod, "sane", FakeSaneModule(device=dev))
    return SaneBackend()


class TestAdfPageErrorsAreTruthful:
    """
    A real SANE fault is reported as itself, never as an empty feeder.

    A jam, an open cover, a busy device and an I/O error on page 0 each tell
    the operator what happened rather than to load paper.  python-sane
    converts exactly one message to ``StopIteration``, and a zero-page feeder
    is the only honest source of "No paper detected in feeder".
    """

    @pytest.mark.parametrize("message", _MEASURED_SANE_FAULTS)
    def test_first_page_fault_surfaces_as_scan_error(
        self, message: str, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """Each measured page-0 fault raises ScanError carrying the SANE text."""
        dev = FakeSaneDev(pages=5, start_error=FakeSaneError(message))
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", _feeder_settings(), page_sink)

        assert message in str(exc_info.value)
        assert "page 1" in str(exc_info.value)
        assert not isinstance(exc_info.value, FeederEmptyError)

    def test_first_page_fault_keeps_the_original_as_cause(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """The translated ScanError chains the exception SANE actually raised."""
        original = FakeSaneError("Document feeder jammed")
        dev = FakeSaneDev(pages=5, start_error=original)
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", _feeder_settings(), page_sink)

        assert exc_info.value.__cause__ is original

    def test_mid_stack_jam_names_the_one_based_page(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """A jam on the fourth sheet names page 4, not page 0."""
        dev = FakeSaneDev(
            pages=10,
            start_error=FakeSaneError("Document feeder jammed"),
            start_error_page=3,
        )
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", _feeder_settings(), page_sink)

        assert "Document feeder jammed" in str(exc_info.value)
        assert "page 4" in str(exc_info.value)
        assert not isinstance(exc_info.value, FeederEmptyError)

    def test_out_of_documents_still_means_an_empty_feeder(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """The one converted message is the only path to the feeder message."""
        dev = FakeSaneDev(pages=5, start_error=FakeSaneError(_OUT_OF_DOCUMENTS))
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(FeederEmptyError, match="No paper detected in feeder"):
            backend.scan_pages("test:0", _feeder_settings(), page_sink)

    def test_a_clean_stack_yields_every_page(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """A three-sheet feeder spools three pages and raises nothing."""
        dev = FakeSaneDev(pages=3)
        backend = _backend_with(dev, monkeypatch)

        pages = backend.scan_pages("test:0", _feeder_settings(), page_sink).pages

        assert len(pages) == 3


class TestAdfPageCap:
    """
    The feeder loop is bounded, and reaching the bound keeps what was scanned.

    python-sane's ``_SaneIterator.__next__`` stops only on one exact message,
    so on hardware that is not a feeder ``start()``/``snap()`` keep succeeding
    and the loop never terminates; the per-page timeout cannot stop a scan
    that succeeds every iteration.

    A named feeder is cut off at the per-pass cap, and ``Auto`` sent through
    the feeder much sooner, since it may be a platen rescanned forever.  The
    pass is not a failure: the pages already scanned are kept, and the batch
    names the sheet fed but not kept, so the operator knows where to resume.
    """

    @staticmethod
    def _auto_settings(source: str) -> ScanSettings:
        """
        Build settings that send the device's Auto source through the feeder.

        Args:
            source: The profile's source, ``Auto`` itself or a flatbed request
                the device's Auto stands in for.

        Returns:
            Settings routing Auto to the feeder.

        """
        return ScanSettings(
            source=source, resolution=300, mode="Color", auto_source_mode="adf"
        )

    @staticmethod
    def _auto_device(sheets: int) -> FakeSaneDev:
        """
        Build a device offering only Auto and ADF, holding ``sheets`` sheets.

        Args:
            sheets: How many sheets the feeder holds.

        Returns:
            The device.

        """
        dev = FakeSaneDev(pages=sheets)
        dev.report_sources(["Auto", "ADF"])
        return dev

    def test_an_endless_feeder_is_cut_off_at_the_cap(
        self,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        tmp_path: Path,
    ) -> None:
        """A feeder that never reports end-of-feed keeps 500 pages and names 501."""
        cap = sane_backend_mod._MAX_ADF_PAGES
        # Far more sheets than the cap, so the fake never reports end-of-feed
        # and the loop has to be stopped by the cap rather than by the device.
        dev = FakeSaneDev(pages=cap * 100)
        backend = _backend_with(dev, monkeypatch)

        started = time.monotonic()
        batch = backend.scan_pages("test:0", _feeder_settings(), page_sink)
        elapsed = time.monotonic() - started

        assert len(batch.pages) == cap
        assert batch.cap_reached == PassCapReached(
            cap=cap, sheet_not_kept=cap + 1, auto_source=False
        )
        assert batch.pages_rejected == 0
        # The sheet past the cap was acquired -- that is how the overrun is
        # seen -- and then discarded rather than spooled.
        assert dev.calls.count("start") == cap + 1
        assert len(list((tmp_path / f"spool-{_SPOOL_LABEL_A}").iterdir())) == cap
        # The feed was stopped, not merely abandoned.
        assert dev.cancel_calls >= 1
        # The fake's pages are tiny, so the cap must be reached quickly -- a
        # slow run here would mean the bound is not what stopped the loop.
        assert elapsed < 10.0

    def test_a_maximal_stack_is_not_off_by_one(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """Exactly _MAX_ADF_PAGES sheets complete normally and spool in full."""
        cap = sane_backend_mod._MAX_ADF_PAGES
        dev = FakeSaneDev(pages=cap)
        backend = _backend_with(dev, monkeypatch)

        batch = backend.scan_pages("test:0", _feeder_settings(), page_sink)

        assert len(batch.pages) == cap
        assert batch.cap_reached is None

    def test_auto_on_the_feeder_is_cut_off_at_fifty(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """Auto sent through the feeder keeps 50 pages and names sheet 51."""
        dev = self._auto_device(1000)
        backend = _backend_with(dev, monkeypatch)

        batch = backend.scan_pages("test:0", self._auto_settings("Auto"), page_sink)

        assert dev.source == "Auto"
        assert len(batch.pages) == 50
        assert batch.cap_reached == PassCapReached(
            cap=50, sheet_not_kept=51, auto_source=True
        )
        assert dev.calls.count("start") == 51
        assert batch.substituted_source is None

    def test_an_auto_standing_in_for_a_flatbed_is_capped_and_named(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """A flatbed request fed through Auto carries both facts on one batch."""
        dev = self._auto_device(1000)
        backend = _backend_with(dev, monkeypatch)

        batch = backend.scan_pages("test:0", self._auto_settings("Flatbed"), page_sink)

        assert len(batch.pages) == 50
        assert batch.cap_reached == PassCapReached(
            cap=50, sheet_not_kept=51, auto_source=True
        )
        assert batch.substituted_source == "Flatbed"

    def test_a_named_feeder_is_not_held_to_the_auto_cap(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """Sixty sheets through a named feeder are sixty pages, uncapped."""
        dev = FakeSaneDev(pages=60)
        backend = _backend_with(dev, monkeypatch)

        batch = backend.scan_pages("test:0", _feeder_settings(), page_sink)

        assert len(batch.pages) == 60
        assert batch.cap_reached is None

    def test_unreadable_sheets_inside_a_capped_pass_are_still_counted(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        Three unreadable sheets then an endless stack: 497 pages, sheet 501.

        The cap counts sheets fed, not pages kept, so the sheet not kept is
        still the 501st whatever was skipped before it.
        """
        cap = sane_backend_mod._MAX_ADF_PAGES
        # Far below _MIN_PAGE_BYTES: a 10x10 RGB page is 300 bytes.
        unreadable = Image.new("RGB", (10, 10), "white")
        readable = _make_content_image()
        dev = FakeSaneDev()
        dev.load_feeder([unreadable] * 3 + [readable] * (cap * 2 - 3))
        backend = _backend_with(dev, monkeypatch)

        batch = backend.scan_pages("test:0", _feeder_settings(), page_sink)

        assert len(batch.pages) == cap - 3
        assert batch.pages_rejected == 3
        assert batch.cap_reached == PassCapReached(
            cap=cap, sheet_not_kept=cap + 1, auto_source=False
        )

    def test_a_capped_pass_of_nothing_readable_is_still_an_error(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """Every sheet unreadable, then the cap: the unreadable error, no batch."""
        cap = sane_backend_mod._MAX_ADF_PAGES
        dev = FakeSaneDev()
        dev.load_feeder([Image.new("RGB", (10, 10), "white")] * (cap * 2))
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError, match="unreadable") as exc_info:
            backend.scan_pages("test:0", _feeder_settings(), page_sink)

        assert not isinstance(exc_info.value, FeederEmptyError)
        assert page_sink.records == ()

    def test_the_per_pass_cap_is_the_one_shared_constant(self) -> None:
        """
        The backend's cap is the backend-neutral per-pass number, not a copy.

        A document that grows pass by pass is capped against the same number,
        so the two cannot drift apart by one being edited alone.
        """
        assert MAX_PAGES_PER_PASS == 500
        assert sane_backend_mod._MAX_ADF_PAGES == MAX_PAGES_PER_PASS

    def test_the_budget_defaults_to_the_feeder_cap(self) -> None:
        """A page budget built with no cap carries the named feeder's 500."""
        assert sane_backend_mod._PageBudget().max_pages == 500
        assert sane_backend_mod._DEFAULT_PAGE_BUDGET.max_pages == 500

    def test_the_auto_cap_is_fifty(self) -> None:
        """The bound for a source that is not a named feeder is 50 pages."""
        assert sane_backend_mod._MAX_AUTO_FEEDER_PAGES == 50


class TestSaneBackendPageValidation:
    """A page that is not a readable image is skipped; a readable one survives."""

    def test_zero_dimension_page_skipped(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """Page with zero dimensions is skipped with warning."""
        mock_dev = fake_sane_module.device
        zero_img = Image.new("RGB", (0, 0))
        normal_img = _make_content_image()
        mock_dev.load_feeder([zero_img, normal_img])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        pages = backend.scan_pages("test:device:001", settings, page_sink).pages
        assert len(pages) == 1

    def test_min_file_size_page_skipped(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """Page below MIN_PAGE_BYTES (1x1 pixel) is skipped."""
        mock_dev = fake_sane_module.device
        tiny_img = Image.new("RGB", (1, 1), "red")
        normal_img = _make_content_image()
        mock_dev.load_feeder([tiny_img, normal_img])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        pages = backend.scan_pages("test:device:001", settings, page_sink).pages
        assert len(pages) == 1

    def test_pure_white_page_survives(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """
        A uniformly white page reaches the caller instead of being discarded.

        python-sane expands 1-bit lineart to 0 and 255 bytes, so a clean blank
        page is uniformly 255.  Whether such a page is dropped is decided by the
        pipeline's blank-page filter, under the profile's
        ``enable_empty_page_detection`` toggle, where the user can see it and
        turn it off.
        """
        mock_dev = fake_sane_module.device
        white_img = Image.new("RGB", (200, 300), (255, 255, 255))
        normal_img = _make_content_image()
        mock_dev.load_feeder([white_img, normal_img])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        pages = backend.scan_pages("test:device:001", settings, page_sink).pages
        assert len(pages) == 2
        # The blank itself survived, rather than the content page arriving
        # twice.  Asserted from the record's own measurement, made once at
        # spool time: no ink anywhere, on paper measuring 255.
        assert pages[0].ink_coverage == 0.0
        assert pages[0].paper_white == 255

    def test_pure_black_page_survives(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """
        A uniformly black page reaches the caller instead of being discarded.

        The real SANE ``test`` backend's default picture is solid black, so a
        backend that judged content would destroy every page of a ten-sheet
        stack before the pipeline saw it.  Judging content is not the scanner
        layer's job.
        """
        mock_dev = fake_sane_module.device
        black_img = Image.new("RGB", (200, 300), (0, 0, 0))
        normal_img = _make_content_image()
        mock_dev.load_feeder([black_img, normal_img])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        pages = backend.scan_pages("test:device:001", settings, page_sink).pages
        assert len(pages) == 2
        # Paper measuring 0 is solid black.
        assert pages[0].paper_white == 0

    def test_normal_content_page_passes(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """A page with mixed content passes validation and is kept."""
        mock_dev = fake_sane_module.device
        content_img = _make_content_image()
        mock_dev.load_feeder([content_img])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        pages = backend.scan_pages("test:device:001", settings, page_sink).pages
        assert len(pages) == 1

    def test_exif_stripped(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """
        No EXIF reaches the PNG the ADF path spooled.

        Read back off disk, because the spooled PNG *is* what the PDF embeds:
        an orientation tag in that file is what img2pdf would act on.  The
        page handed over carries a real orientation tag, so the assertion
        fails if anything on the way writes EXIF out.
        """
        mock_dev = fake_sane_module.device
        mock_dev.load_feeder([_with_orientation_exif(_make_content_image())])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        batch = backend.scan_pages("test:device:001", settings, page_sink)

        assert len(batch.pages) == 1
        _assert_no_exif_on_disk(batch.pages[0])

    def test_exif_stripped_flatbed(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """No EXIF reaches the PNG the flatbed path spooled either."""
        mock_dev = fake_sane_module.device
        page = _with_orientation_exif(Image.new("RGB", (100, 100), "white"))
        mock_dev.load_feeder([page])

        backend = SaneBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        batch = backend.scan_pages("test:device:001", settings, page_sink)

        assert len(batch.pages) == 1
        _assert_no_exif_on_disk(batch.pages[0])


class TestFlatbedIntegrityChecks:
    """
    The single-sheet path runs the same two checks as the feeder.

    The checks are for an unreadable image returned *successfully*: unchecked,
    it would flow into ``_maybe_crop`` and then into ``assemble_pdf``, where
    ``img.save()`` on a 0x0 image would be the first thing to notice.

    A failure is fatal here rather than counted, because a flatbed exposes one
    sheet at a time and there is no next page to carry on to.
    """

    def test_a_zero_dimension_sheet_raises(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """A 0x0 image is not a page, however successfully it was returned."""
        mock_dev = fake_sane_module.device
        mock_dev.load_feeder([Image.new("RGB", (0, 0))])

        backend = SaneBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        with pytest.raises(ScanError, match="unreadable"):
            backend.scan_pages("test:device:001", settings, page_sink)

    def test_a_sheet_below_the_byte_floor_raises(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """A 10x10 RGB page is 300 bytes, far below the floor."""
        mock_dev = fake_sane_module.device
        mock_dev.load_feeder([Image.new("RGB", (10, 10), "white")])

        backend = SaneBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        with pytest.raises(ScanError, match="unreadable"):
            backend.scan_pages("test:device:001", settings, page_sink)

    def test_a_readable_sheet_still_reports_no_rejections(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """A good flatbed page is not counted as a reject."""
        mock_dev = fake_sane_module.device
        mock_dev.load_feeder([_make_content_image()])

        backend = SaneBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        batch = backend.scan_pages("test:device:001", settings, page_sink)

        assert len(batch.pages) == 1
        assert batch.pages_rejected == 0


class TestIntegrityFailuresAreSkippedAndCounted:
    """
    One unreadable page costs one page; a wholly unreadable batch raises.

    Raising on the first integrity failure would fail a fifty-sheet job over
    one bad sheet.  A batch in which *every* page was rejected does not return
    an empty list either: the pipeline would hand that straight to
    ``assemble_pdf([])`` and record a job that produced nothing as a success.
    """

    def test_a_mid_stack_integrity_failure_costs_exactly_one_page(
        self,
        fake_sane_module: FakeSaneModule,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """A zero-dimension third sheet is skipped and named; four survive."""
        mock_dev = fake_sane_module.device
        mock_dev.load_feeder(
            [
                _make_content_image(),
                _make_content_image(),
                Image.new("RGB", (0, 0)),
                _make_content_image(),
                _make_content_image(),
            ]
        )

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        with caplog.at_level(logging.WARNING):
            pages = backend.scan_pages("test:device:001", settings, page_sink).pages

        assert len(pages) == 4
        skips = [r for r in caplog.records if "skipping" in r.getMessage()]
        assert len(skips) == 1
        assert "Page 3" in skips[0].getMessage()

    def test_a_wholly_rejected_batch_raises_rather_than_yielding_nothing(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """Three unreadable sheets raise ScanError naming how many were fed."""
        mock_dev = fake_sane_module.device
        mock_dev.load_feeder([Image.new("RGB", (0, 0)) for _ in range(3)])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:device:001", settings, page_sink)

        assert "3" in str(exc_info.value)
        # Distinct condition from an empty feeder: paper *was* fed, and the
        # operator needs to be told it was unreadable rather than absent.
        assert not isinstance(exc_info.value, FeederEmptyError)

    def test_a_zero_page_feeder_still_raises_feeder_empty(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """No paper at all stays FeederEmptyError, not the all-rejected error."""
        mock_dev = fake_sane_module.device
        mock_dev.load_feeder([])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")

        with pytest.raises(FeederEmptyError, match="No paper detected in feeder"):
            backend.scan_pages("test:device:001", settings, page_sink)

    def test_a_clean_stack_logs_no_skip_warning(
        self,
        fake_sane_module: FakeSaneModule,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """Five readable sheets yield five pages and no skip warning at all."""
        mock_dev = fake_sane_module.device
        mock_dev.load_feeder([_make_content_image() for _ in range(5)])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        with caplog.at_level(logging.WARNING):
            pages = backend.scan_pages("test:device:001", settings, page_sink).pages

        assert len(pages) == 5
        assert not [r for r in caplog.records if "skipping" in r.getMessage()]


class TestAutoSourceRouting:
    """The Auto source routes by auto_source_mode; a named source ignores it."""

    def test_auto_source_adf_routes_to_adf_path(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """scan_pages with source='Auto' and auto_source_mode='adf' uses ADF path."""
        # Add "Auto" to available sources
        mock_dev = fake_sane_module.device
        mock_dev.report_sources(["Flatbed", "ADF", "Auto"])
        settings = ScanSettings(
            source="Auto", resolution=300, mode="Color", auto_source_mode="adf"
        )
        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages
        # ADF path yields 3 pages via multi_scan
        assert len(pages) == 3

    def test_auto_source_flatbed_routes_to_flatbed_path(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """scan_pages with source='Auto' and auto_source_mode='flatbed' uses flatbed path."""
        mock_dev = fake_sane_module.device
        mock_dev.report_sources(["Flatbed", "ADF", "Auto"])
        settings = ScanSettings(
            source="Auto", resolution=300, mode="Color", auto_source_mode="flatbed"
        )
        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages
        # Flatbed path yields 1 page via snap
        assert len(pages) == 1
        assert mock_dev.calls.count("snap") == 1

    def test_explicit_adf_ignores_auto_source_mode(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """Explicit ADF source ignores auto_source_mode setting."""
        settings = ScanSettings(
            source="ADF", resolution=300, mode="Color", auto_source_mode="flatbed"
        )
        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages
        # ADF always uses ADF path regardless of auto_source_mode
        assert len(pages) == 3

    def test_explicit_flatbed_ignores_auto_source_mode(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """Explicit Flatbed source ignores auto_source_mode setting."""
        mock_dev = fake_sane_module.device
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", auto_source_mode="adf"
        )
        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages
        # Flatbed always uses flatbed path regardless of auto_source_mode
        assert len(pages) == 1
        assert mock_dev.calls.count("snap") == 1


class TestAutoSourceRecognition:
    """The Auto source is recognised by the classifier, not by ==."""

    @pytest.mark.parametrize("reported", ["Auto", "auto", "  AUTO  "])
    def test_auto_is_recognised_whatever_its_spelling(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        reported: str,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        A device spelling its Auto source differently still honours the routing.

        A case- or whitespace-sensitive ``== "Auto"`` would send a device reporting
        lowercase ``auto`` down the single-page path, silently ignoring
        ``auto_source_mode = "adf"`` and returning one page from a whole stack.
        """
        mock_dev = fake_sane_module.device
        mock_dev.report_sources(["Flatbed", "ADF", reported])
        settings = ScanSettings(
            source=reported, resolution=300, mode="Color", auto_source_mode="adf"
        )

        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages

        assert classify_source(reported) is SourceKind.AUTO
        assert len(pages) == 3
        assert mock_dev.calls == _THREE_SHEET_FEEDER_CALLS

    @pytest.mark.parametrize("reported", ["Auto", "auto", "  AUTO  "])
    def test_auto_still_honours_flatbed_routing(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        reported: str,
        page_sink: SpooledPageSink,
    ) -> None:
        """The override is consulted, not merely coincidentally agreed with."""
        mock_dev = fake_sane_module.device
        mock_dev.report_sources(["Flatbed", "ADF", reported])
        settings = ScanSettings(
            source=reported, resolution=300, mode="Color", auto_source_mode="flatbed"
        )

        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages

        assert len(pages) == 1
        assert mock_dev.calls.count("snap") == 1

    def test_a_long_feeder_name_never_takes_the_auto_override(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """A long feeder name scans as a feeder whatever auto_source_mode says."""
        mock_dev = fake_sane_module.device
        mock_dev.report_sources(["Flatbed", "ADF", "Automatic Document Feeder"])
        settings = ScanSettings(
            source="Automatic Document Feeder",
            resolution=300,
            mode="Color",
            auto_source_mode="flatbed",
        )

        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages

        assert classify_source("Automatic Document Feeder") is SourceKind.FEEDER
        assert len(pages) == 3
        assert mock_dev.calls == _THREE_SHEET_FEEDER_CALLS

    def test_an_unrecognised_source_takes_the_single_page_path(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        An unrecognised source takes the single-page path and skips the override.

        UNKNOWN is not treated as multi-page.  ``auto_source_mode`` is set to
        ``"adf"`` here to show an unrecognised name does not reach the Auto
        override either.
        """
        mock_dev = fake_sane_module.device
        mock_dev.report_sources(["Flatbed", "ADF", "Mystery Tray"])
        settings = ScanSettings(
            source="Mystery Tray",
            resolution=300,
            mode="Color",
            auto_source_mode="adf",
        )

        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages

        assert classify_source("Mystery Tray") is SourceKind.UNKNOWN
        assert len(pages) == 1
        assert mock_dev.calls.count("snap") == 1


def _options_reporting(sources: list[str]) -> list[tuple]:
    """Return the shared fake's option table with the given source list."""
    device = FakeSaneDev()
    device.report_sources(sources)
    return device.get_options()


class TestResolveSourceForManualDuplex:
    """
    Manual duplex resolves a real feeder from the device's own list.

    ``Auto`` is never substituted for a missing feeder: with
    ``auto_source_mode`` defaulting to ``"flatbed"`` it would take one platen
    snapshot per pass and report a green Complete.

    No expected feeder name here is a plain ``"ADF"``: real consumer feeders
    report ``"Automatic Document Feeder"``, and a hardcoded ``"ADF"`` would
    fail on exactly that hardware.
    """

    def test_selects_the_first_source_that_feeds(self) -> None:
        """The device's own first feeder is chosen, not a guessed name."""
        raw = _options_reporting(["Flatbed", "Automatic Document Feeder", "ADF Duplex"])

        choice = scan_session_mod._resolve_source(raw, "Flatbed", resolve_feeder=True)

        assert choice.effective == "Automatic Document Feeder"
        assert choice.has_option is True

    def test_a_single_sided_feeder_the_operator_named_is_honoured(self) -> None:
        """An operator who picked one of two feeders gets that one."""
        raw = _options_reporting(["Flatbed", "ADF Front", "Automatic Document Feeder"])

        choice = scan_session_mod._resolve_source(
            raw, "Automatic Document Feeder", resolve_feeder=True
        )

        assert choice.effective == "Automatic Document Feeder"

    def test_a_named_both_sides_feeder_is_overridden_loudly(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        A named ``"ADF Duplex"`` gives way to the single-sided feeder.

        A both-sides source returns 2N pages per pass, the two passes' counts
        agree, and interleaving them scrambles 4N pages into a green DONE. The
        operator's choice is therefore overridden -- with a WARNING naming both
        sources, so the override is never silent.
        """
        raw = _options_reporting(["Flatbed", "Automatic Document Feeder", "ADF Duplex"])

        with caplog.at_level(logging.WARNING, logger=_SESSION_LOGGER):
            choice = scan_session_mod._resolve_source(
                raw, "ADF Duplex", resolve_feeder=True
            )

        assert choice.effective == "Automatic Document Feeder"
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.name == _SESSION_LOGGER and record.levelno == logging.WARNING
        ]
        assert any(
            "'ADF Duplex'" in message and "'Automatic Document Feeder'" in message
            for message in warnings
        )

    def test_a_single_sided_feeder_wins_over_an_earlier_both_sides_one(
        self,
    ) -> None:
        """The first *single-sided* feeder is chosen, not the first feeder."""
        raw = _options_reporting(["Flatbed", "ADF Duplex", "Automatic Document Feeder"])

        choice = scan_session_mod._resolve_source(raw, "Flatbed", resolve_feeder=True)

        assert choice.effective == "Automatic Document Feeder"

    def test_a_device_whose_only_feeder_scans_both_sides_is_refused(self) -> None:
        """
        Only a both-sides feeder means refusal before any page.

        Using it with a WARNING would still end in a scrambled document beside
        a green DONE on an unattended appliance, so the operator is pointed at
        hardware duplex instead.
        """
        raw = _options_reporting(["Flatbed", "ADF Duplex"])

        with pytest.raises(ScanError, match="scans both sides") as excinfo:
            scan_session_mod._resolve_source(raw, "ADF", resolve_feeder=True)

        message = str(excinfo.value)
        assert 'duplex = "hardware"' in message
        assert "['Flatbed', 'ADF Duplex']" in message

    def test_an_unreported_source_falls_back_to_a_reported_feeder(self) -> None:
        """``source = "ADF"`` on a device that says "Automatic Document Feeder"."""
        raw = _options_reporting(["Flatbed", "Automatic Document Feeder"])

        choice = scan_session_mod._resolve_source(raw, "ADF", resolve_feeder=True)

        assert choice.effective == "Automatic Document Feeder"

    def test_a_flatbed_only_device_is_refused_naming_its_sources(self) -> None:
        """No feeder means no manual duplex, said loudly and before any scan."""
        raw = _options_reporting(["Flatbed"])

        with pytest.raises(ScanError, match="feeder") as excinfo:
            scan_session_mod._resolve_source(raw, "ADF", resolve_feeder=True)

        assert "['Flatbed']" in str(excinfo.value)

    def test_auto_is_never_substituted_for_manual_duplex(self) -> None:
        """
        ``Auto`` on offer still ends in a refusal, never a substitution.

        Substituted, ``Auto`` would take two platen snapshots on a flatbed-only
        device and report success.
        """
        raw = _options_reporting(["Flatbed", "Auto"])

        with pytest.raises(ScanError, match="feeder") as excinfo:
            scan_session_mod._resolve_source(raw, "ADF", resolve_feeder=True)

        assert "['Flatbed', 'Auto']" in str(excinfo.value)

    def test_no_source_option_trusts_a_configured_feeder_name(self) -> None:
        """
        A sheet-fed device with no ``source`` option feeds without being told.

        The simplex path already trusts the classifier on the configured name
        for such a device, so manual duplex does the same and nothing is
        assigned.
        """
        raw = build_option_table(omit=("source",))

        choice = scan_session_mod._resolve_source(raw, "ADF", resolve_feeder=True)

        assert (choice.effective, choice.has_option) == ("ADF", False)

    def test_no_source_option_accepts_the_legacy_manual_duplex_name(self) -> None:
        """A ``source = "Manual Duplex"`` profile runs on a device with no source option."""
        raw = build_option_table(omit=("source",))

        choice = scan_session_mod._resolve_source(
            raw, "Manual Duplex", resolve_feeder=True
        )

        assert (choice.effective, choice.has_option) == ("Manual Duplex", False)

    def test_no_source_option_refuses_a_non_feeder_name(self) -> None:
        """A configured flatbed on a device with no source option is refused."""
        raw = build_option_table(omit=("source",))

        with pytest.raises(ScanError, match="exposes no source option") as excinfo:
            scan_session_mod._resolve_source(raw, "Flatbed", resolve_feeder=True)

        assert "'Flatbed'" in str(excinfo.value)

    def test_an_unreadable_list_assigns_a_named_single_sided_feeder(self) -> None:
        """
        A source list that cannot be read still lets a named feeder through.

        The device has a ``source`` option, so the name is assigned, trimmed,
        and the device accepts or refuses it, as on the simplex path.
        """
        raw = [_option(1, "source", _STRING_OPTION, None)]

        choice = scan_session_mod._resolve_source(raw, " ADF ", resolve_feeder=True)

        assert (choice.effective, choice.has_option) == ("ADF", True)
        assert choice.substituted_from is None

    @pytest.mark.parametrize(
        "requested",
        [
            pytest.param("Flatbed", id="flatbed"),
            pytest.param("ADF Duplex", id="both-sides-feeder"),
            pytest.param("Manual Duplex", id="legacy-name"),
        ],
    )
    def test_an_unreadable_list_refuses_anything_but_a_single_sided_feeder(
        self, requested: str
    ) -> None:
        """
        With the list unreadable, the refusal says so rather than "reports none".

        The both-sides check cannot run on a list that cannot be read, so only
        a name that classifies as a single-sided feeder is accepted, and the
        message does not claim the device reported no feeder.
        """
        raw = [_option(1, "source", _STRING_OPTION, None)]

        with pytest.raises(ScanError) as excinfo:
            scan_session_mod._resolve_source(raw, requested, resolve_feeder=True)

        message = str(excinfo.value)
        assert "could not be read" in message
        assert "reports none" not in message
        assert "Available: []" not in message
        assert repr(requested) in message

    def test_without_the_flag_a_missing_feeder_is_refused_not_auto(self) -> None:
        """
        The simplex path refuses a missing feeder rather than swap ``Auto`` in.

        ``Auto`` routed by the default ``auto_source_mode`` takes one platen
        snapshot, so a stack would come back as one page reported as success.
        """
        raw = _options_reporting(["Flatbed", "Auto"])

        with pytest.raises(ScanError) as excinfo:
            scan_session_mod._resolve_source(raw, "ADF", resolve_feeder=False)

        message = str(excinfo.value)
        assert all(name in message for name in ("'ADF'", "'Flatbed'", "'Auto'"))

    def test_without_the_flag_an_unsupported_source_still_raises(self) -> None:
        """The simplex refusal names what was asked for and what is on offer."""
        raw = _options_reporting(["Flatbed"])

        with pytest.raises(ScanError) as excinfo:
            scan_session_mod._resolve_source(raw, "ADF", resolve_feeder=False)

        message = str(excinfo.value)
        assert "'ADF'" in message
        assert "'Flatbed'" in message

    def test_without_the_flag_a_reported_flatbed_is_kept(self) -> None:
        """Only manual duplex looks for a feeder; a simplex flatbed stays put."""
        raw = _options_reporting(["Flatbed", "Automatic Document Feeder"])

        choice = scan_session_mod._resolve_source(raw, "Flatbed", resolve_feeder=False)

        assert (choice.effective, choice.has_option) == ("Flatbed", True)
        assert choice.substituted_from is None

    def test_scan_settings_does_not_resolve_a_feeder_by_default(self) -> None:
        """ScanSettings resolves no feeder unless it is asked to."""
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        assert settings.duplex == "none"

    def test_scan_pages_feeds_through_the_resolved_feeder(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """The flag reaches the backend, which assigns and drives the real feeder."""
        mock_dev = fake_sane_module.device
        mock_dev.report_sources(["Flatbed", "Automatic Document Feeder"])
        settings = ScanSettings(
            source="ADF",
            resolution=300,
            mode="Color",
            duplex="manual",
        )

        pages = SaneBackend().scan_pages(_TEST_DEVICE, settings, page_sink).pages

        assert mock_dev.source == "Automatic Document Feeder"
        assert len(pages) == 3
        assert mock_dev.calls == _THREE_SHEET_FEEDER_CALLS

    def test_scan_pages_feeds_a_device_with_no_source_option(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """Manual duplex on a no-source-option device feeds the stack."""
        dev = FakeSaneDev(options=build_option_table(omit=("source",)))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="ADF",
            resolution=300,
            mode="Color",
            duplex="manual",
        )

        pages = backend.scan_pages(_TEST_DEVICE, settings, page_sink).pages

        assert len(pages) == 3
        assert dev.calls == _THREE_SHEET_FEEDER_CALLS
        assert "source" not in dev.assignments

    def test_scan_pages_refuses_before_touching_the_platen(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """A flatbed-plus-Auto device takes no snapshot at all for manual duplex."""
        mock_dev = fake_sane_module.device
        mock_dev.report_sources(["Flatbed", "Auto"])
        settings = ScanSettings(
            source="ADF",
            resolution=300,
            mode="Color",
            auto_source_mode="flatbed",
            duplex="manual",
        )

        with pytest.raises(ScanError, match="feeder"):
            SaneBackend().scan_pages(_TEST_DEVICE, settings, page_sink)

        assert mock_dev.calls == []
        assert "source" not in mock_dev.assignments


class TestSaneBackendPerPageTimeout:
    """A page that takes too long raises ScanError; pages in time are spooled."""

    def test_page_timeout_raises_scan_error(
        self,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A page that takes too long raises ScanError naming the timeout, once.

        The read is held on the shared fake's gate rather than slowed by a
        sleep, so the per-page timeout fires at once and nothing waits it out.
        The backend raises and leaves the logging to whoever handles the error
        -- the worker or the CLI -- so the backend itself must not also log the
        timeout at ERROR, or every timeout reads as two failures.
        """
        caplog.set_level(logging.DEBUG, logger=_BACKEND_LOGGER)
        mock_dev = fake_sane_module.device
        mock_dev.block_read(ReadBlockMode.PARTIAL)

        backend = SaneBackend()

        try:
            with pytest.raises(ScanError, match="timed out after"):
                backend._scan_adf_pages(
                    mock_dev,
                    page_sink,
                    _UNCROPPED,
                    sane_backend_mod._PageBudget(timeout=0.05),
                )
        finally:
            mock_dev.release_read()
            _join_sane_reader_threads()

        timeout_errors = [
            record
            for record in caplog.records
            if record.name == _BACKEND_LOGGER
            and record.levelno >= logging.ERROR
            and "timed out after" in record.getMessage()
        ]
        assert timeout_errors == []

    def test_pages_within_timeout_succeed(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """Pages acquired within timeout are spooled and reported in order."""
        mock_dev = fake_sane_module.device
        assert isinstance(mock_dev, FakeSaneDev)

        backend = SaneBackend()
        records = backend._scan_adf_pages(
            mock_dev, page_sink, _UNCROPPED, sane_backend_mod._PageBudget(timeout=5.0)
        ).records
        assert len(records) == 3
        assert [record.sequence for record in records] == [1, 2, 3]


class TestSaneBackendCancelSequence:
    """
    A timed-out read is cancelled and waited for, and closed only once it returns.

    Every test here arms the shared fake's Event-gated read, so nothing waits
    out a sleep: the block is ended by setting an ``Event``, and the only
    bounded waits are the backend's own timeout and grace.

    The ordering these tests assert is not a refinement.  ``sane_close`` runs
    holding the GIL while ``sane_read`` has released it, so a close racing a
    blocked read is the one sequence python-sane cannot survive -- which is
    why "was close called while the read was still blocked" is asserted
    directly rather than inferred from a call count.
    """

    @pytest.fixture(autouse=True)
    def _clear_wedge(self, fake_device: FakeSaneDev) -> Iterator[None]:
        """
        Release any blocked read and clear the wedge after each test here.

        The record is module state by design -- the *next* ``scan_pages`` on
        a fresh backend object has to refuse -- so a test that
        wedges it deliberately has to un-wedge it, or the refusal leaks into
        every test that runs afterwards.

        The gate is opened first, and the readers joined, so the release goes
        through the production path: the reader closes its own handle and
        clears the record itself, exactly as it would in service.  Without
        that, a test arming ``NEVER`` leaves a daemon parked until the fake's
        30 s ceiling, and the *next* test's join waits the difference out.

        Whatever is left afterwards is cleared outright.  A reader still
        blocked at that point identifies its own acquisition by the ``done``
        event it was given, so a late wake-up matches nothing and does
        nothing.

        Args:
            fake_device: The one handle every test in this class drives.

        Yields:
            None, before the release and the clear.

        """
        yield
        fake_device.release_read()
        _join_sane_reader_threads()
        _join_sane_cancel_threads()
        record = sane_backend_mod._WEDGE
        record.stuck = False
        record.done = None
        record.device = None
        record.device_id = ""
        record.page_label = ""
        record.settling = False
        record.outstanding = set()

    @staticmethod
    def _wedge(
        backend: SaneBackend, device: FakeSaneDev, sink: SpooledPageSink
    ) -> None:
        """
        Leave the backend wedged by a read that does not answer the cancel.

        Args:
            backend: The backend to wedge.
            device: The shared fake, armed here rather than by the caller so
                that every wedge in this class is built the same way.
            sink: Where the (never arriving) page would have gone.

        """
        device.block_read(ReadBlockMode.NEVER)
        with (
            pytest.raises(ScanError, match="timed out"),
            backend._open_device(_TEST_DEVICE) as dev,
        ):
            backend._scan_adf_pages(
                dev,
                sink,
                _UNCROPPED,
                sane_backend_mod._PageBudget(timeout=0.05, grace=0.05),
            )

    def test_close_not_called_while_blocked(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        The cancel goes out first and the close waits for the read to return.

        Asserted inside the device context and then again outside it, because
        the two say different things: inside, that nothing closed the handle
        while the read was still in SANE; outside, that the handle really was
        released once the read came back.  A single count at the end could not
        tell "closed after" from "closed during".
        """
        fake_device.block_read(ReadBlockMode.PARTIAL)

        with sane_backend._open_device(_TEST_DEVICE) as dev:
            with pytest.raises(ScanError, match="timed out"):
                sane_backend._scan_adf_pages(
                    dev,
                    page_sink,
                    _UNCROPPED,
                    sane_backend_mod._PageBudget(timeout=0.05),
                )
            assert fake_device.cancel_calls == 1
            assert fake_device.close_while_blocked is False
            assert fake_device.close_calls == 0

        assert fake_device.close_calls == 1
        assert fake_device.close_while_blocked is False

    def test_did_not_respond_to_cancel(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A scanner that never answers the cancel is reported, not closed.

        The grace is injected through the page budget, as the timeout is, so
        the test does not wait out the module's real ten seconds.
        """
        fake_device.block_read(ReadBlockMode.NEVER)

        with (
            caplog.at_level(logging.CRITICAL),
            pytest.raises(ScanError) as raised,
            sane_backend._open_device(_TEST_DEVICE) as dev,
        ):
            sane_backend._scan_adf_pages(
                dev,
                page_sink,
                _UNCROPPED,
                sane_backend_mod._PageBudget(timeout=0.05, grace=0.05),
            )

        message = str(raised.value)
        assert "timed out" in message
        assert "did not answer the cancel" in message
        assert fake_device.close_calls == 0
        assert fake_device.close_while_blocked is False
        assert [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.CRITICAL
        ]

    def test_post_cancel_page_discarded(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
        tmp_path: Path,
    ) -> None:
        """
        The truncated page a cancelled read returns never reaches the sink.

        This is the case a "use it if it arrived after all" shortcut gets
        wrong.  Measured on real libsane, a cancelled ``snap()`` returns a
        truncated image rather than raising, and that image clears
        ``_validate_page_image``: spooling it would put one more page in the
        PDF than the error message claims.
        """
        fake_device.block_read(ReadBlockMode.PARTIAL)

        with (
            pytest.raises(ScanError, match="timed out"),
            sane_backend._open_device(_TEST_DEVICE) as dev,
        ):
            sane_backend._scan_adf_pages(
                dev, page_sink, _UNCROPPED, sane_backend_mod._PageBudget(timeout=0.05)
            )

        assert page_sink.records == ()
        # Asserted on the directory as well as on the sink, because "the sink
        # was never called" and "no page file exists" are different claims and
        # a partial PDF is built from the second one.
        assert list((tmp_path / f"spool-{_SPOOL_LABEL_A}").iterdir()) == []

    def test_a_wedged_backend_refuses_the_next_call_with_no_sane_traffic(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
        second_pass_sink: SpooledPageSink,
    ) -> None:
        """
        A wedged backend refuses to scan or list in this process, touching nothing.

        A capability read is not refused: it runs in a listing child, whose
        SANE is its own, so nothing it does can reach the stuck read.

        "No SANE call" is asserted as the device's own call log being
        byte-for-byte what it was, because the refusal has to happen before
        ``sane.open()``: opening is the very operation the SANE standard forbids
        while a read is outstanding.

        ``get_devices`` refuses too.  ``_resolve_device`` calls it before
        ``scan_pages`` whenever ``scanner.device`` is empty, and on the ``net``
        backend ``sane_get_devices`` is an RPC on the same control wire the
        stuck read is on.
        """
        self._wedge(sane_backend, fake_device, page_sink)
        calls_before = list(fake_device.calls)
        enumerations_before = fake_sane_module.get_devices_call_count
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")

        with pytest.raises(ScanError, match="Restart saneless"):
            sane_backend.scan_pages(_TEST_DEVICE, settings, second_pass_sink)
        with pytest.raises(ScanError, match="Restart saneless") as refusal:
            sane_backend.get_devices()

        assert fake_device.calls == calls_before
        assert fake_sane_module.get_devices_call_count == enumerations_before
        assert fake_device.close_calls == 0
        assert fake_device.cancel_calls == 1
        # The refusal names the wedged device even though the caller had none
        # to name: enumeration is the call that finds out which devices exist.
        assert _TEST_DEVICE in str(refusal.value)

    def test_a_wedged_backend_refuses_list_and_open_without_a_child(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
        listing_seam: ListingSeam,
    ) -> None:
        """
        The Scanner check's list-then-open refuses too, and starts nothing.

        A child could not touch this process's stuck handle, so the refusal is
        not about safety here.  It keeps one rule for every SANE entry point,
        and an appliance whose read is stuck cannot scan anyway.
        """
        self._wedge(sane_backend, fake_device, page_sink)
        calls_before = list(fake_device.calls)
        listings_before = list(listing_seam.calls)
        enumerations_before = fake_sane_module.get_devices_call_count

        with pytest.raises(ScanError, match="Restart saneless"):
            sane_backend.list_and_open(_NET_DEVICE)

        assert listing_seam.calls == listings_before
        assert fake_sane_module.get_devices_call_count == enumerations_before
        assert fake_device.calls == calls_before

    def test_shutdown_leaves_sane_up_while_a_read_is_outstanding(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A wedged backend is not shut down, and the refusal is logged.

        ``sane_exit`` closes every open handle and runs holding the GIL, so it
        is the close-while-reading hazard applied to every handle at once.  An
        un-exited SANE in a process that is ending anyway costs nothing next to
        that, so this path skips rather than risks it.
        """
        self._wedge(sane_backend, fake_device, page_sink)
        caplog.set_level(logging.WARNING, logger=_BACKEND_LOGGER)

        sane_backend.close()

        assert fake_sane_module.exit_call_count == 0
        assert fake_sane_module.exit_while_blocked is False
        assert any("has not returned" in message for message in _guard_warnings(caplog))

    def test_the_wedge_clears_when_the_late_read_finally_returns(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
        second_pass_sink: SpooledPageSink,
    ) -> None:
        """
        A transient hang recovers without a restart, and the reader closes.

        The handle is closed by the reader thread itself, because it is the
        only thread that knows the read is over -- the thread that gave up on
        it has long since raised.  Joining that thread is a bounded wait on a
        real event, not a sleep: it ends the instant the reader returns.
        """
        self._wedge(sane_backend, fake_device, page_sink)

        fake_device.release_read()
        _join_sane_reader_threads()

        assert fake_device.close_calls == 1
        assert fake_device.close_while_blocked is False

        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        batch = sane_backend.scan_pages(_TEST_DEVICE, settings, second_pass_sink)
        assert batch.pages

    def test_flatbed_timeout_matches_the_adf_message_shape(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        One flatbed sheet is bounded exactly as one fed sheet is.

        The defaults are asserted alongside the behaviour because the claim is
        not merely "the flatbed times out" but "with the same constant, and no
        new config key".  A second timeout that happened to be equal would
        satisfy the first half and quietly drift apart later.  The grace is
        pinned for the same reason, and because it is the parameter that makes
        the unresponsive-cancel path testable at all.
        """
        budget = (
            inspect.signature(sane_backend_mod._snap_flatbed)
            .parameters["budget"]
            .default
        )
        assert budget.timeout == page_budget_mod._PAGE_TIMEOUT_FLOOR_SECONDS
        assert budget.grace == sane_backend_mod._CANCEL_GRACE_SECONDS
        fake_device.block_read(ReadBlockMode.PARTIAL)

        with sane_backend._open_device(_TEST_DEVICE) as dev:
            with pytest.raises(ScanError, match="timed out"):
                sane_backend_mod._snap_flatbed(
                    dev,
                    _TEST_DEVICE,
                    page_sink,
                    _UNCROPPED,
                    sane_backend_mod._PageBudget(timeout=0.05),
                )
            # Inside the device context, so the count is the acquisition's own
            # cancel and not the context manager's routine one on the way out.
            assert fake_device.cancel_calls == 1
            assert fake_device.close_while_blocked is False
            assert fake_device.close_calls == 0

        assert page_sink.records == ()

    def test_flatbed_did_not_respond_to_cancel(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A platen scanner that never answers the cancel is reported, not closed.

        The flatbed counterpart of ``test_did_not_respond_to_cancel``: one sheet
        is one sheet, whichever way it was presented, including when the scanner
        ignores the cancel.  The grace is injected through the page budget, so
        the test does not wait out the module's real ten seconds.
        """
        fake_device.block_read(ReadBlockMode.NEVER)
        began = time.monotonic()

        with (
            caplog.at_level(logging.CRITICAL),
            pytest.raises(ScanError) as raised,
            sane_backend._open_device(_TEST_DEVICE) as dev,
        ):
            sane_backend_mod._snap_flatbed(
                dev,
                _TEST_DEVICE,
                page_sink,
                _UNCROPPED,
                sane_backend_mod._PageBudget(timeout=0.05, grace=0.05),
            )

        # The injected grace was really used, not the module's ten seconds --
        # which is the whole difference this parameter makes.
        assert time.monotonic() - began < 1.0
        message = str(raised.value)
        assert "timed out" in message
        assert "did not answer the cancel" in message
        assert fake_device.close_calls == 0
        assert fake_device.close_while_blocked is False
        assert page_sink.records == ()
        assert [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.CRITICAL
        ]

    def test_flatbed_unreadable_sheet_is_fatal_not_skipped(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        The shared page validation is fatal on the platen.

        A fed sheet that fails the same two checks is skipped and counted,
        because there is a next sheet to carry on to.  A flatbed exposes one
        sheet at a time, so there is nothing to carry on to and the scan ends
        -- which is why the call log must show exactly one attempt.
        """
        fake_device.load_feeder([Image.new("RGB", (10, 10), "white")])
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        with pytest.raises(ScanError, match="unreadable"):
            sane_backend.scan_pages(_TEST_DEVICE, settings, page_sink)

        assert fake_device.calls == ["start", "snap"]
        assert page_sink.records == ()

    @pytest.mark.parametrize(
        ("signum", "expected"),
        [
            pytest.param(signal.SIGINT, KeyboardInterrupt, id="ctrl-c"),
            pytest.param(signal.SIGTERM, ScanInterrupted, id="sigterm"),
        ],
    )
    def test_keyboard_interrupt_mid_read(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
        signum: signal.Signals,
        expected: type[BaseException],
    ) -> None:
        """
        Ctrl-C or SIGTERM during a read cancels and settles the device, then re-raises.

        The exception comes back out unchanged, so the CLI keeps its exit 130 and
        a one-shot command keeps the pages already scanned.  The interrupt lands
        well inside the thirty-second page budget: a wait that held it back until
        the page timed out would prove the timeout path instead.

        ``read_started`` is set from inside the blocked read, so the signal is
        sent only once the waiting thread is in the block that handles it.  It
        goes to the main thread with ``pthread_kill``, which is where that wait
        runs; ``raise_signal`` would deliver it to the interrupter's own thread.
        """
        fake_device.block_read(ReadBlockMode.PARTIAL)
        main_thread = threading.main_thread().ident
        assert main_thread is not None

        def interrupt_once_the_read_blocks() -> None:
            if fake_device.read_started.wait(_READER_JOIN_SECONDS):
                signal.pthread_kill(main_thread, signum)

        interrupter = threading.Thread(
            target=interrupt_once_the_read_blocks, name="interrupter", daemon=True
        )
        handling = (
            _signal_raises_scan_interrupted(signum)
            if expected is ScanInterrupted
            else contextlib.nullcontext()
        )

        with handling, sane_backend._open_device(_TEST_DEVICE) as dev:
            began = time.monotonic()
            interrupter.start()
            with pytest.raises(expected):
                sane_backend._scan_adf_pages(
                    dev,
                    page_sink,
                    _UNCROPPED,
                    sane_backend_mod._PageBudget(timeout=30.0),
                )
            elapsed = time.monotonic() - began
            interrupter.join(_READER_JOIN_SECONDS)

            assert elapsed < _INTERRUPT_BOUND_SECONDS

            assert fake_device.cancel_calls == 1
            assert fake_device.close_while_blocked is False
            assert fake_device.close_calls == 0

        assert fake_device.close_calls == 1
        assert fake_device.close_while_blocked is False
        assert page_sink.records == ()

    def test_a_timed_out_feeder_page_sends_one_cancel_in_all(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        After a feeder page times out, the only cancel is the one the timeout sent.

        Neither the feeder iterator's finaliser nor the device context's routine
        cancel before close sends another.  On the ``net`` backend each cancel is
        a request to a host that may have stopped answering, and those two would
        run on the worker thread with no bound.  Counted after the context has
        exited and after a collection, so an iterator released late by a
        reference cycle would still be counted if it cancelled an open handle.
        """
        fake_device.block_read(ReadBlockMode.PARTIAL)

        # The device context is the outer one, so the timeout is caught, and
        # its traceback let go of, while the handle is still open.
        with (
            sane_backend._open_device(_TEST_DEVICE) as dev,
            pytest.raises(ScanError, match="timed out"),
        ):
            sane_backend._scan_adf_pages(
                dev, page_sink, _UNCROPPED, sane_backend_mod._PageBudget(timeout=0.05)
            )
        gc.collect()

        assert fake_device.cancel_calls == 1
        assert fake_device.close_calls == 1
        assert fake_device.close_while_blocked is False

    def test_a_timed_out_flatbed_page_sends_one_cancel_in_all(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        After a flatbed page times out, the device context sends no second cancel.

        The flatbed has no iterator, so the one extra cancel it could send is
        the routine one before close.
        """
        fake_device.block_read(ReadBlockMode.PARTIAL)

        with (
            sane_backend._open_device(_TEST_DEVICE) as dev,
            pytest.raises(ScanError, match="timed out"),
        ):
            sane_backend_mod._snap_flatbed(
                dev,
                _TEST_DEVICE,
                page_sink,
                _UNCROPPED,
                sane_backend_mod._PageBudget(timeout=0.05),
            )
        gc.collect()

        assert fake_device.cancel_calls == 1
        assert fake_device.close_calls == 1
        assert fake_device.close_while_blocked is False

    @pytest.mark.parametrize(
        "interruption",
        [
            pytest.param(KeyboardInterrupt(), id="ctrl-c"),
            pytest.param(
                ScanInterrupted("Interrupted by SIGTERM", signum=signal.SIGTERM),
                id="sigterm",
            ),
        ],
    )
    def test_an_interrupt_before_the_reader_starts_wedges_nothing(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        monkeypatch: pytest.MonkeyPatch,
        interruption: BaseException,
    ) -> None:
        """
        Ctrl-C landing before the reader thread exists neither cancels nor wedges.

        Parametrised over a signal's ``ScanInterrupted`` too, which takes the same
        settle path.  With no reader, a cancel would hit a handle holding no read
        and the grace would be waited out on an event nothing sets; the wedge then
        recorded could never clear, because only a reader's ``finally`` releases
        it, and every later scan would refuse.

        Only the *reader* thread is interrupted, so the cancel thread would start
        normally if the handler reached for it; ``cancel_calls`` is therefore a
        real assertion.  The elapsed bound pins "did not wait out the grace".
        """
        reader_prefix = sane_backend_mod._READER_THREAD_PREFIX

        class _InterruptTheReaderBeforeItStarts(threading.Thread):
            """A Thread whose reader never gets as far as running."""

            def start(self) -> None:
                """Raise instead of starting, but only for the reader."""
                if self.name.startswith(reader_prefix):
                    raise interruption
                super().start()

        monkeypatch.setattr(
            sane_backend_mod.threading, "Thread", _InterruptTheReaderBeforeItStarts
        )
        grace = 5.0

        # The assertions are inside the device context because _open_device's
        # own finally issues a routine cancel() on the clean path, which would
        # make cancel_calls 1 for a reason that has nothing to do with this.
        with sane_backend._open_device(_TEST_DEVICE) as dev:
            began = time.monotonic()
            with pytest.raises(type(interruption)):
                sane_backend_mod._acquire_with_timeout(
                    dev,
                    fake_device.snap,
                    page_budget_mod._page_label(0),
                    sane_backend_mod._PageBudget(timeout=5.0, grace=grace),
                )
            elapsed = time.monotonic() - began

            assert fake_device.cancel_calls == 0
            assert sane_backend_mod._WEDGE.stuck is False

        assert elapsed < grace

    def test_a_read_that_settles_in_the_grace_leaves_no_wedge(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        The wedge written before the cancel is cleared once the read returns.

        The record goes in before the cancel fires, so a read that comes back
        inside the grace has to take it out again.  Left behind, it would
        refuse every later scan for a device that answered, and the device
        context would leave a perfectly good handle open.
        """
        fake_device.block_read(ReadBlockMode.PARTIAL)

        with sane_backend._open_device(_TEST_DEVICE) as dev:
            with pytest.raises(ScanError, match="timed out") as raised:
                sane_backend._scan_adf_pages(
                    dev,
                    page_sink,
                    _UNCROPPED,
                    sane_backend_mod._PageBudget(timeout=0.05),
                )
            record = sane_backend_mod._WEDGE
            assert record.stuck is False
            assert record.settling is False
            assert record.outstanding == set()
            assert fake_device.close_calls == 0

        assert "did not answer the cancel" not in str(raised.value)

    def test_any_error_from_the_wait_still_settles_a_started_read(
        self, sane_backend: SaneBackend, fake_device: FakeSaneDev
    ) -> None:
        """
        An exception that is not an interrupt still cancels and waits out a read.

        A wait too long for ``threading.Event.wait`` raises ``OverflowError``
        once the reader is running. Left unsettled, the device context would
        cancel and close the handle while the read was still inside it.
        """
        fake_device.block_read(ReadBlockMode.PARTIAL)

        with sane_backend._open_device(_TEST_DEVICE) as dev:
            with pytest.raises(OverflowError):
                sane_backend_mod._acquire_with_timeout(
                    dev,
                    fake_device.snap,
                    page_budget_mod._page_label(0),
                    sane_backend_mod._PageBudget(timeout=1e300),
                )

            assert fake_device.cancel_calls == 1
            assert fake_device.read_is_blocked() is False
            assert sane_backend_mod._WEDGE.stuck is False

        assert fake_device.close_while_blocked is False

    def test_an_interrupt_while_the_cancel_is_prepared_leaves_the_wedge(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
    ) -> None:
        """
        Ctrl-C landing as the cancel thread is built finds the wedge recorded.

        The record has to come before any other work of the cancel tail. An
        interrupt landing before it would leave no wedge, and the device
        context would then cancel and close the handle while the read was
        still inside it. With the record standing, the handle is left alone
        until the read returns, and the reader closes it then.
        """

        class _InterruptTheCancelAsItIsBuilt(threading.Thread):
            """A Thread whose construction is interrupted for the canceller."""

            def __init__(
                self,
                *,
                target: Callable[..., object],
                name: str,
                daemon: bool,
                args: tuple[object, ...] = (),
            ) -> None:
                """Raise for the cancel thread, construct any other."""
                if name == _CANCEL_THREAD_NAME:
                    raise KeyboardInterrupt
                super().__init__(target=target, name=name, daemon=daemon, args=args)

        fake_device.block_read(ReadBlockMode.NEVER)
        monkeypatch.setattr(
            sane_backend_mod.threading, "Thread", _InterruptTheCancelAsItIsBuilt
        )

        with sane_backend._open_device(_TEST_DEVICE) as dev:
            with pytest.raises(KeyboardInterrupt):
                sane_backend_mod._acquire_with_timeout(
                    dev,
                    fake_device.snap,
                    page_budget_mod._page_label(0),
                    sane_backend_mod._PageBudget(timeout=0.05),
                )

            assert _wedged()

        assert fake_device.close_while_blocked is False
        assert fake_device.close_calls == 0

        fake_device.release_read()
        _join_sane_reader_threads()

        assert not _wedged()
        assert fake_device.close_calls == 1
        assert fake_device.close_calls == 1
        assert fake_device.close_while_cancelling is False

    def test_a_slow_cancel_reply_holds_the_worker_no_longer_than_the_grace(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
        second_pass_sink: SpooledPageSink,
    ) -> None:
        """
        A cancel still in flight at the end of the grace leaves the handle wedged.

        On ``net`` a cancel is a request to the host, and a host slow to reply
        holds the thread that sent it.  Here the read comes back at once but
        the cancel does not: the worker gives it the grace and no more, sends
        no second cancel, and closes nothing, because closing a handle while
        SANE is still inside a cancel on it is as unsafe as closing it under a
        read.  The cancel thread closes the handle itself when the reply
        finally arrives, and the wedge clears.

        The margin on the elapsed time is generous on purpose: what it pins is
        "about the grace", not a scheduling latency.
        """
        release = threading.Event()
        fake_device.block_read(ReadBlockMode.PARTIAL)
        fake_device.block_cancel(release)
        grace = 1.0
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")

        try:
            began = time.monotonic()
            with (
                pytest.raises(ScanError, match="timed out") as raised,
                sane_backend._open_device(_TEST_DEVICE) as dev,
            ):
                sane_backend._scan_adf_pages(
                    dev,
                    page_sink,
                    _UNCROPPED,
                    sane_backend_mod._PageBudget(timeout=0.05, grace=grace),
                )
            elapsed = time.monotonic() - began

            assert elapsed < grace + 1.0
            assert "did not answer the cancel" in str(raised.value)
            assert fake_device.cancel_calls == 1
            assert fake_device.cancels_in_flight_max == 1
            # No close at all, so none under the cancel either.
            assert fake_device.close_calls == 0
            assert _wedged() is True
            with pytest.raises(ScanError, match="Restart saneless"):
                sane_backend.scan_pages(_TEST_DEVICE, settings, second_pass_sink)
        finally:
            release.set()
        _join_sane_cancel_threads()

        assert fake_device.close_calls == 1
        assert fake_device.close_while_cancelling is False
        assert fake_device.close_while_blocked is False
        assert fake_device.cancels_in_flight_max == 1
        assert _wedged() is False

    @pytest.mark.parametrize(
        ("signum", "expected"),
        [
            pytest.param(signal.SIGINT, KeyboardInterrupt, id="ctrl-c"),
            pytest.param(signal.SIGTERM, ScanInterrupted, id="sigterm"),
        ],
    )
    def test_a_second_interrupt_during_the_grace_still_records_the_wedge(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
        signum: signal.Signals,
        expected: type[BaseException],
    ) -> None:
        """
        Ctrl-C pressed twice leaves the stuck read recorded, not closed under.

        The first interrupt lands during the read and starts the cancel; the
        second lands while the worker waits out the grace for a read that ignores
        the cancel.  The wedge is written before the cancel fires, so the second
        interrupt cannot skip it: the device context leaves the handle alone, the
        next scan is refused, and the reader closes the handle when it returns.

        Both signals go to the main thread with ``pthread_kill``, where the wait
        they interrupt runs.  Each waits on the fake's event for the moment it is
        meant to hit, so there is no sleep.
        """
        fake_device.block_read(ReadBlockMode.NEVER)
        main_thread = threading.main_thread().ident
        assert main_thread is not None
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")

        def interrupt_twice() -> None:
            if not fake_device.read_started.wait(_READER_JOIN_SECONDS):
                return
            signal.pthread_kill(main_thread, signum)
            if fake_device.cancel_started.wait(_READER_JOIN_SECONDS):
                signal.pthread_kill(main_thread, signum)

        interrupter = threading.Thread(
            target=interrupt_twice, name="interrupter", daemon=True
        )
        handling = (
            _signal_raises_scan_interrupted(signum)
            if expected is ScanInterrupted
            else contextlib.nullcontext()
        )

        # Started before the scan: it does nothing until the read has begun.
        interrupter.start()
        with (
            handling,
            pytest.raises(expected),
            sane_backend._open_device(_TEST_DEVICE) as dev,
        ):
            sane_backend._scan_adf_pages(
                dev,
                page_sink,
                _UNCROPPED,
                sane_backend_mod._PageBudget(timeout=20.0, grace=20.0),
            )
        interrupter.join(_READER_JOIN_SECONDS)

        assert _wedged() is True
        assert fake_device.cancel_calls == 1
        # No close at all, so none under the read either.
        assert fake_device.close_calls == 0
        with pytest.raises(ScanError, match="Restart saneless"):
            sane_backend.scan_pages(_TEST_DEVICE, settings, page_sink)

        fake_device.release_read()
        _join_sane_reader_threads()

        assert fake_device.close_calls == 1
        assert fake_device.close_while_blocked is False
        assert _wedged() is False


class TestBeginSettle:
    """Recording a wedge before a cancel leaves a read that has returned alone."""

    def test_an_already_finished_read_is_not_marked_wedged(
        self, sane_backend: SaneBackend
    ) -> None:
        """
        A read whose done event is already set is not recorded as wedged.

        The reader can return in the instant between the page timeout and the
        settle.  Recorded then, the wedge would refuse every later scan over a
        handle nothing is blocked in.
        """
        done = threading.Event()
        done.set()

        with sane_backend._open_device(_TEST_DEVICE) as dev:
            try:
                settling = sane_backend_mod._begin_settle(dev, done, "Page 1")
                stuck = _wedged()
            finally:
                with sane_backend_mod._WEDGE_LOCK:
                    sane_backend_mod._clear_wedge()

        assert settling is False
        assert stuck is False


class TestReinitialise:
    """
    The per-job SANE restart, and the two states in which it must not run.

    After a saned restart, an open in this process keeps failing with an I/O
    error until SANE is restarted, so each scan job starts by restarting it.
    ``sane_exit`` closes every open handle with a close request that waits for
    a reply, and on a host that has silently vanished that wait can last
    longer than 40 seconds.  So the restart is refused, with no SANE
    call at all, while a read is stuck or any handle is open.  "No SANE call"
    is asserted on the fake's own counters, because a refusal that came after
    ``sane_exit`` would already be too late.
    """

    @pytest.fixture(autouse=True)
    def _release_reads(self, fake_device: FakeSaneDev) -> Iterator[None]:
        """
        Let any blocked read return after each test, and clear the wedge.

        The release goes through the production path first, so the reader
        closes its own handle and clears the record.  Whatever is left is then
        cleared by hand, as ``TestSaneBackendCancelSequence`` does.

        Args:
            fake_device: The one handle every test here drives.

        Yields:
            None, before the release and the clear.

        """
        yield
        fake_device.release_read()
        _join_sane_reader_threads()
        record = sane_backend_mod._WEDGE
        record.stuck = False
        record.done = None
        record.device = None
        record.device_id = ""
        record.page_label = ""

    @staticmethod
    def _sane_calls(module: FakeSaneModule) -> tuple[int, int, int]:
        """
        Snapshot the process-level SANE calls the fake has counted.

        Args:
            module: The patched fake.

        Returns:
            The exit, init and device-listing call counts, in that order.

        """
        return (
            module.exit_call_count,
            module.init_call_count,
            module.get_devices_call_count,
        )

    def test_reinitialise_exits_then_initialises_once(
        self, fake_sane_module: FakeSaneModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One restart is one ``sane_exit`` and one ``sane_init``, host kept."""
        monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
        backend = SaneBackend(host="scanbox.lan")
        assert fake_sane_module.init_call_count == 1
        assert fake_sane_module.exit_call_count == 0

        backend.reinitialise()

        assert fake_sane_module.exit_call_count == 1
        assert fake_sane_module.init_call_count == 2
        assert sane_backend_mod._INIT.done is True
        assert sane_backend_mod._INIT.host == "scanbox.lan"
        assert os.environ["SANE_NET_HOSTS"] == "scanbox.lan"

    def test_reinitialise_calls_exit_before_init(
        self, fake_sane_module: FakeSaneModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The old SANE is shut down before the new one starts, never after."""
        backend = SaneBackend()
        order: list[str] = []
        real_exit = fake_sane_module.exit
        real_init = fake_sane_module.init

        def recording_exit() -> None:
            """Record the shutdown, then make it."""
            order.append("exit")
            real_exit()

        def recording_init() -> tuple[int, int, int, int]:
            """
            Record the start, then make it.

            Returns:
                The version the fake reports.

            """
            order.append("init")
            return real_init()

        monkeypatch.setattr(fake_sane_module, "exit", recording_exit)
        monkeypatch.setattr(fake_sane_module, "init", recording_init)

        backend.reinitialise()

        assert order[-2:] == ["exit", "init"]

    def test_reinitialise_is_refused_while_a_read_is_stuck(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
    ) -> None:
        """A stuck read refuses the restart before any SANE call is made."""
        TestSaneBackendCancelSequence._wedge(sane_backend, fake_device, page_sink)
        calls_before = self._sane_calls(fake_sane_module)
        device_calls_before = list(fake_device.calls)

        with pytest.raises(ScanError, match="Restart saneless"):
            sane_backend.reinitialise()

        assert self._sane_calls(fake_sane_module) == calls_before
        assert fake_device.calls == device_calls_before
        assert fake_sane_module.exit_while_blocked is False

    def test_reinitialise_is_refused_while_a_handle_is_open(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        fake_device: FakeSaneDev,
    ) -> None:
        """An open handle refuses the restart; closing it allows it again."""
        with sane_backend._open_device(_TEST_DEVICE):
            calls_before = self._sane_calls(fake_sane_module)
            device_calls_before = list(fake_device.calls)

            with pytest.raises(ScanError, match="still open"):
                sane_backend.reinitialise()

            assert self._sane_calls(fake_sane_module) == calls_before
            assert fake_device.calls == device_calls_before

        exits_before = fake_sane_module.exit_call_count
        sane_backend.reinitialise()
        assert fake_sane_module.exit_call_count == exits_before + 1

    def test_the_handle_count_follows_opens_and_closes_for_reinitialise(
        self, sane_backend: SaneBackend
    ) -> None:
        """The count is one inside the device block and zero on either exit."""
        assert sane_backend_mod._handles_open() == 0
        with sane_backend._open_device(_TEST_DEVICE):
            assert sane_backend_mod._handles_open() == 1
        assert sane_backend_mod._handles_open() == 0

        failure = "scan went wrong"
        with (
            pytest.raises(RuntimeError, match=failure),
            sane_backend._open_device(_TEST_DEVICE),
        ):
            raise RuntimeError(failure)
        assert sane_backend_mod._handles_open() == 0

    def test_a_failed_open_is_not_counted_for_reinitialise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A handle that never opened is never counted, so never blocks a job."""
        module = FakeSaneModule(open_error=FakeSaneError("Invalid argument"))
        monkeypatch.setattr(scan_session_mod, "sane", module)
        backend = SaneBackend()

        with contextlib.ExitStack() as stack, pytest.raises(ScanError):
            stack.enter_context(backend._open_device(_TEST_DEVICE))

        assert sane_backend_mod._handles_open() == 0

    def test_a_stuck_handle_stays_counted_until_released_for_reinitialise(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        A handle left open by a stuck read is counted until the reader closes it.

        The device block exits without closing the handle, so the count must
        not drop there.  It drops when the late read returns and the reader
        closes the handle itself.
        """
        TestSaneBackendCancelSequence._wedge(sane_backend, fake_device, page_sink)
        assert sane_backend_mod._handles_open() == 1

        fake_device.release_read()
        _join_sane_reader_threads()

        assert fake_device.close_calls == 1
        assert sane_backend_mod._handles_open() == 0

    def test_closing_one_handle_twice_leaves_another_counted(self) -> None:
        """
        A second close of the same handle does not count a different one closed.

        A bare count would drop to zero here and let SANE restart with the
        other handle still open, which ``sane_exit`` would then try to close.
        """
        first = FakeSaneDev()
        second = FakeSaneDev()
        sane_backend_mod._handle_opened(first)
        sane_backend_mod._handle_opened(second)

        sane_backend_mod._handle_closed(first)
        sane_backend_mod._handle_closed(first)

        assert sane_backend_mod._handles_open() == 1
        sane_backend_mod._handle_closed(second)
        assert sane_backend_mod._handles_open() == 0

    def test_reinitialise_logs_one_info_line(
        self,
        fake_sane_module: FakeSaneModule,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A job adds one INFO line; the shutdown and start lines are DEBUG."""
        _ = fake_sane_module
        monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
        backend = SaneBackend(host="scanbox.lan")
        caplog.set_level(logging.DEBUG, logger=_BACKEND_LOGGER)
        caplog.clear()

        backend.reinitialise()

        records = [r for r in caplog.records if r.name == _BACKEND_LOGGER]
        loud = [r.getMessage() for r in records if r.levelno >= logging.INFO]
        assert loud == ["SANE re-initialised before this scan"]
        quiet = [r.getMessage() for r in records if r.levelno == logging.DEBUG]
        assert "SANE shut down" in quiet
        assert any(m.startswith("SANE initialized") for m in quiet)
        assert any(m.startswith("SANE net host discovery") for m in quiet)

    def test_the_base_reinitialise_does_nothing(self) -> None:
        """A backend holding no process-global library has nothing to restart."""
        assert StubScannerBackend().reinitialise() is None

    def test_a_failed_start_during_reinitialise_is_reported_and_retried(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A SANE that will not start again fails the job and is tried next time.

        The guard stays unset, so the next job's restart calls ``sane_init``
        rather than assuming a SANE that is not there.
        """

        def failing_init() -> tuple[int, int, int, int]:
            """
            Count the call and fail, as a SANE that cannot start does.

            Raises:
                FakeSaneError: Always.

            """
            fake_sane_module.init_call_count += 1
            reason = "no backend could start"
            raise FakeSaneError(reason)

        real_init = fake_sane_module.init
        monkeypatch.setattr(fake_sane_module, "init", failing_init)

        with pytest.raises(ScanError) as failure:
            sane_backend.reinitialise()

        assert str(failure.value).startswith("Could not initialise SANE: ")
        assert sane_backend_mod._INIT.done is False

        monkeypatch.setattr(fake_sane_module, "init", real_init)
        inits_before = fake_sane_module.init_call_count
        sane_backend.reinitialise()
        assert fake_sane_module.init_call_count == inits_before + 1
        # The version, not ``done``: a type checker still holds ``done`` at the
        # False asserted above.  Only a successful init records a version.
        assert sane_backend_mod._INIT.version == (16777248, 1, 0, 32)


# The child process the exit proof runs, and the bound it is given.
#
# It blocks in ``os.read`` on a pipe nobody writes to.  That is the honest
# stand-in for ``sane_read``: a real blocking syscall that releases the GIL,
# which a wait on a ``threading.Event`` would only pretend to be -- and the
# whole claim under test is about what the interpreter does at exit with a
# thread parked in exactly such a call.
#
# Twenty seconds is not a duration the proof expects to use: a passing run
# exits in well under a second.  It is the bound that turns the failure this
# test exists to catch -- a process that never exits -- into a reported
# failure instead of a hung session.  A ``TimeoutExpired`` is therefore left
# to raise rather than caught.
_CHILD_EXIT_SECONDS = 20.0
_STUCK_READ_CHILD = '''\
"""Block a daemon reader in a real syscall, then leave."""

import os

from saneless.exceptions import ScanError
from saneless.scanner.sane_backend import _acquire_with_timeout, _PageBudget


class StuckScanner:
    """A device whose read never returns and whose cancel changes nothing."""

    def __init__(self) -> None:
        self.cancels = 0
        self.closes = 0

    def cancel(self) -> None:
        self.cancels += 1

    def close(self) -> None:
        self.closes += 1


read_fd, _write_fd = os.pipe()
device = StuckScanner()
returned = True


def never_returns() -> object:
    """Block forever in a GIL-releasing syscall, as sane_read does."""
    return os.read(read_fd, 1)


try:
    _acquire_with_timeout(
        device, never_returns, "Page 1", _PageBudget(timeout=0.2, grace=0.2)
    )
except ScanError as exc:
    returned = "did not answer the cancel" not in str(exc)

print(f"returned={returned} cancels={device.cancels} closes={device.closes}")
'''


class TestStuckReadDoesNotBlockProcessExit:
    """
    A process with a permanently stuck SANE read still exits on its own.

    No in-process test can show that the interpreter would exit, so a real
    child is started, wedged in a real blocking read, and required to exit.
    """

    def test_process_exits(self, tmp_path: Path) -> None:
        """
        A child with a permanently stuck reader still exits, and exits 0.

        The printed line carries the whole cancel sequence in one assertion:
        the read did not return, exactly one cancel was fired, and no close
        was issued on a handle a read is still inside.

        Args:
            tmp_path: Where the child script is written.

        """
        child = tmp_path / "stuck_read.py"
        child.write_text(_STUCK_READ_CHILD, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(child)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_CHILD_EXIT_SECONDS,
        )

        assert result.returncode == 0, result.stderr
        assert "returned=False cancels=1 closes=0" in result.stdout


class TestSaneBackendADFCleanup:
    """A feeder scan cancels and closes the device, however it ends."""

    def test_cancel_called_after_adf_scan(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """Both cancel() and close() run after an ADF multi_scan completes."""
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        sane_backend.scan_pages("test:device:001", settings, page_sink)
        mock_dev = fake_sane_module.device
        assert mock_dev.cancel_calls
        assert mock_dev.close_calls

    def test_cancel_called_on_adf_error(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """dev.cancel() and dev.close() run even when the ADF scan errors."""
        # The fault lands on the second sheet, so a page has already been
        # acquired when it arrives.
        dev = FakeSaneDev(
            pages=5,
            start_error=FakeSaneError("hardware error"),
            start_error_page=1,
        )
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Automatic Document Feeder", resolution=300, mode="Color"
        )

        # A fault after the first page is translated to ScanError
        # carrying the device's own text, rather than propagating raw.
        with pytest.raises(ScanError, match="hardware error"):
            backend.scan_pages("test:0", settings, page_sink)

        assert dev.cancel_calls
        assert dev.close_calls


class TestFakeFeederStartOrdering:
    """
    The fake's ``start()`` checks the armed error before the page budget.

    An error armed at the index one past the last page -- the end-of-feed
    probe -- has to fire, or the test silently becomes a clean-feed test
    instead of failing loudly as a misconfiguration.
    """

    def test_an_error_armed_at_the_probe_index_is_reachable(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        A jam on the probe surfaces instead of reading as a clean end of feed.

        With the budget checked first, this device would return three pages
        and no error at all, and the arming would be a silent no-op.
        """
        dev = FakeSaneDev(
            pages=3,
            start_error=FakeSaneError("Document feeder jammed"),
            start_error_page=3,
        )
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Automatic Document Feeder", resolution=300, mode="Color"
        )

        with pytest.raises(ScanError, match="jammed"):
            backend.scan_pages("test:0", settings, page_sink)


# ---------------------------------------------------------------------------
# Paper size geometry and crop fallback tests
# ---------------------------------------------------------------------------


# The hyphenated spelling get_options() reports, which the geometry presence
# check reads.  Assignment uses underscores (dev.tl_x); see fake_sane._GEOMETRY_NAMES.
_GEOMETRY_OPTION_NAMES = ("tl-x", "tl-y", "br-x", "br-y")

# A4 is 210 x 297 mm; at 300 dpi that is 2480.3 x 3507.87 px, which rounds to
# 2480 x 3508.
_A4_AT_300_DPI = (2480, 3508)


def _warning_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """
    Return the WARNING messages captured so far.

    Args:
        caplog: The pytest log-capture fixture.

    Returns:
        One string per captured WARNING record.

    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]


def _geometry_less_device(pages: int = 1) -> FakeSaneDev:
    """
    Build a device whose option list does not mention the geometry options.

    The real ``SaneDev.__setattr__`` *stores* an unknown name, so such a
    device accepts ``dev.br_y`` without complaint -- which is exactly why the
    presence check, and not an exception, is what makes the crop fallback
    reachable.

    Args:
        pages: How many sheets the feeder holds.

    Returns:
        A device reporting source, mode and resolution but no scan-area options,
        whose pages are larger than A4 at 300 dpi so a crop is observable.

    """
    dev = FakeSaneDev(
        options=build_option_table(omit=_GEOMETRY_OPTION_NAMES),
        pages=pages,
    )
    dev.set_page_size(3000, 4000)
    return dev


class TestPaperSizeGeometry:
    """A paper size is written to the device's scan-area options."""

    def test_a4_sets_geometry(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """When paper_size='a4', dev.br_x=210.0 and dev.br_y=297.0."""
        mock_dev = fake_sane_module.device
        mock_dev.load_feeder([Image.new("RGB", (2500, 3600), "white")])
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )
        sane_backend.scan_pages("test:device:001", settings, page_sink)
        assert mock_dev.br_x == 210.0
        assert mock_dev.br_y == 297.0
        assert mock_dev.tl_x == 0.0
        assert mock_dev.tl_y == 0.0

    def test_full_no_geometry(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """When paper_size='full', no geometry option is assigned at all."""
        mock_dev = fake_sane_module.device
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="full"
        )

        sane_backend.scan_pages("test:device:001", settings, page_sink)

        # Asserted against the device's own assignment log rather than by
        # writing a sentinel and reading it back: a real device clamps to the
        # option's range, so a sentinel such as -1.0 comes back 0.0 and the
        # read-back proves nothing.
        geometry = [
            name for name in mock_dev.assignments if name.startswith(("tl_", "br_"))
        ]
        assert geometry == []

    def test_letter_sets_geometry(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        When paper_size='letter', 215.9 x 279.4 mm is written to the device.

        This asserts the quantised value.  The default table's geometry range
        has a 1 mm step, as real devices' ranges do, so the device stores the
        nearest whole millimetre: 216.0 x 279.0.  The fixed-point tolerance
        with no step at all is proven by ``TestClampedScanArea``.
        """
        mock_dev = fake_sane_module.device
        mock_dev.load_feeder([Image.new("RGB", (2600, 3400), "white")])
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="letter"
        )
        sane_backend.scan_pages("test:device:001", settings, page_sink)
        assert mock_dev.br_x == 216.0
        assert mock_dev.br_y == 279.0

    # A geometry-less device's scan is asserted, crop size included, by
    # TestPaperSizeCropFallback.test_crop_fallback_when_geometry_fails below.


class TestPaperSizeCropFallback:
    """Without scan-area options, the page is cropped to size by Pillow."""

    def test_crop_fallback_when_geometry_fails(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """When geometry is absent, the scanned image is cropped to A4."""
        backend = _backend_with(_geometry_less_device(), monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )
        pages = backend.scan_pages("test:0", settings, page_sink).pages
        assert len(pages) == 1
        assert pages[0].size == _A4_AT_300_DPI

    def test_full_no_crop(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """When paper_size='full', the page is spooled at its original size."""
        mock_dev = fake_sane_module.device
        original_img = Image.new("RGB", (5000, 6000), "white")
        mock_dev.load_feeder([original_img])
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="full"
        )
        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages
        assert len(pages) == 1
        assert pages[0].size == (5000, 6000)

    def test_adf_pages_are_not_cropped_without_page_size_options(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        A fed page keeps the full window when the feeder cannot centre it.

        Where a feeder puts the sheet is unknown without ``page-width``: a
        top-left crop cuts the right edge off every page of a feeder that
        centres the sheet.  So the page stays whole, larger but complete.
        """
        backend = _backend_with(_geometry_less_device(pages=2), monkeypatch)
        settings = ScanSettings(
            source="Automatic Document Feeder",
            resolution=300,
            mode="Color",
            paper_size="a4",
        )
        pages = backend.scan_pages("test:0", settings, page_sink).pages
        assert [page.size for page in pages] == [(3000, 4000), (3000, 4000)]


class TestFakeDeviceAreaMatchesTheLibrary:
    """
    The fake's ``area`` raises what python-sane raises on a geometry-less device.

    The real library composes ``area`` from attribute reads
    (``SaneDev.__getattr__``) and raises ``AttributeError("No such attribute:
    tl_x")`` when the option table omits the geometry options.
    ``_set_geometry`` checks presence first, but the fake must not diverge
    from the library it stands in for.
    """

    def test_a_geometry_less_device_raises_attribute_error(self) -> None:
        """A device without geometry options raises AttributeError, not KeyError."""
        dev = FakeSaneDev(options=build_option_table(omit=_GEOMETRY_OPTION_NAMES))

        with pytest.raises(AttributeError, match="No such attribute"):
            _ = dev.area

    def test_a_device_reporting_the_options_still_returns_its_box(self) -> None:
        """A device reporting the geometry options returns its box."""
        dev = FakeSaneDev()
        dev.tl_x = 5.0
        dev.br_x = 100.0

        (tl_x, _tl_y), (br_x, _br_y) = dev.area

        assert tl_x == pytest.approx(5.0)
        assert br_x == pytest.approx(100.0)


class TestGeometryPresenceCheck:
    """
    Geometry is written only on a device that reports the options.

    The real ``SaneDev.__setattr__`` stores an unrecognised option name in
    ``__dict__`` and returns -- no device call, no validation, no raise.  So
    assigning ``dev.br_y`` on a device that has no geometry options
    *succeeds*, and only the presence check lets the Pillow crop fallback run.

    Every test here drives a fake that stores silently, as the real library
    does, so each one fails if the presence check is removed.
    """

    def test_a_device_without_geometry_options_stores_br_y_silently(self) -> None:
        """
        A device without geometry options stores ``br_y`` without complaint.

        The whole fallback rests on this.  If it raises, the fake models a
        library that does not exist and every test below it proves nothing.
        """
        dev = FakeSaneDev(options=build_option_table(omit=_GEOMETRY_OPTION_NAMES))

        dev.br_y = 297.0

        assert dev.br_y == 297.0
        # Stored with no device call at all, so it is not a device assignment.
        assert dev.assignments == []

    def test_the_crop_fallback_produces_a_correctly_sized_page(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        The crop fallback runs and the page comes back at A4's pixel size.

        A return-value assertion alone would not show this -- the fallback has
        to actually produce a correctly sized page, from a 3000x4000 scan.
        """
        backend = _backend_with(_geometry_less_device(), monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        pages = backend.scan_pages("test:0", settings, page_sink).pages

        assert pages[0].size == _A4_AT_300_DPI

    def test_the_missing_option_is_named_in_the_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """One absent option is enough, and the warning says which one."""
        dev = FakeSaneDev(options=build_option_table(omit=("br-y",)), pages=1)
        dev.set_page_size(3000, 4000)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        with caplog.at_level(logging.WARNING, logger=_SESSION_LOGGER):
            pages = backend.scan_pages("test:0", settings, page_sink).pages

        assert [m for m in _warning_messages(caplog) if "br-y" in m]
        assert pages[0].size == _A4_AT_300_DPI

    def test_a_rejected_geometry_assignment_logs_the_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        A rejected geometry assignment is logged with the exception it raised.

        Here the options are reported but marked not software-settable, so the
        assignment raises for a structural reason rather than being stored.
        """
        dev = FakeSaneDev(options=build_option_table(geometry_settable=False), pages=1)
        dev.set_page_size(3000, 4000)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        with caplog.at_level(logging.WARNING, logger=_SESSION_LOGGER):
            pages = backend.scan_pages("test:0", settings, page_sink).pages

        assert [m for m in _warning_messages(caplog) if "can't be set by software" in m]
        assert pages[0].size == _A4_AT_300_DPI


# The two units saneless can turn into a scan-area box.  Every other SANE unit
# is declined and the page is cropped by Pillow instead.
_CONVERTIBLE_UNITS = frozenset({GeometryUnit.UNIT_MM, GeometryUnit.UNIT_PIXEL})

# Wide enough that a pixel-denominated A4 box at 1200 dpi fits without the
# device clamping it, which would confuse a unit test with a range test.
_ROOMY_GEOMETRY_RANGE = (0.0, 20000.0, 1.0)


def _device_reporting_unit(
    unit: int,
    geometry_range: tuple[float, float, float] = _ROOMY_GEOMETRY_RANGE,
) -> FakeSaneDev:
    """
    Build a device whose geometry options report a given SANE unit.

    Args:
        unit: The unit code to report at index 5 of the geometry options.
        geometry_range: The ``(min, max, step)`` constraint for those options.

    Returns:
        A single-sheet device whose pages are larger than A4 at 300 dpi, so a
        crop is observable.

    """
    dev = FakeSaneDev(
        options=build_option_table(geometry_unit=unit, geometry_range=geometry_range),
        pages=1,
    )
    dev.set_page_size(3000, 4000)
    return dev


class TestGeometryUnit:
    """
    The scan-area scale factor comes from the unit the device reports.

    The unit lives at index 5 of the option tuple.  Writing A4's 210 mm as-is
    to a device reporting ``UNIT_PIXEL`` would hand it 210 *pixels* -- about
    18 mm at 300 dpi -- and return a sliver of the page with no error.

    There is no ``UNIT_CM`` and no ``UNIT_INCH``: the complete set is the seven
    codes below, confirmed against the ``_sane`` extension itself.
    """

    def test_every_sane_unit_code_is_a_member(self) -> None:
        """All seven codes, and only those seven."""
        assert sorted(unit.value for unit in GeometryUnit) == [0, 1, 2, 3, 4, 5, 6]

    @pytest.mark.parametrize("unit", list(GeometryUnit))
    def test_every_unit_is_either_converted_or_declined(
        self, unit: GeometryUnit
    ) -> None:
        """
        Every SANE unit is either converted to a scale factor or declined.

        Parametrised over the enum itself, so a new member is covered for free:
        one added without a matching ``match`` arm leaves the scale unbound and
        fails here at runtime, as well as failing ``assert_never`` under both
        type checkers.
        """
        scale = scan_session_mod._units_per_mm(unit, 300, fallback="will crop")

        if unit in _CONVERTIBLE_UNITS:
            assert scale is not None
            assert scale > 0
        else:
            assert scale is None

    def test_millimetres_are_written_directly(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """A device reporting UNIT_MM gets A4's millimetres unchanged."""
        dev = _device_reporting_unit(GeometryUnit.UNIT_MM, (0.0, 300.0, 1.0))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        pages = backend.scan_pages("test:0", settings, page_sink).pages

        assert dev.br_x == 210.0
        assert dev.br_y == 297.0
        # Geometry was set on the device, so the page is not cropped as well.
        assert pages[0].size == (3000, 4000)

    def test_pixels_use_the_resolution_read_back_from_the_device(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        The pixel conversion follows the dpi the device chose, not the request.

        5000 dpi is clamped by the device to 1200.  Converting with the
        requested value would size the area for a resolution the device is not
        using.

        A scan area in pixels is an integer option on a real device, so the
        device is given whole pixels: A4 at 1200 dpi is 9921.26 x 14031.50
        pixels, written as 9921 x 14031.  At the requested 5000 dpi it would
        have been over 41,000 pixels wide.
        """
        dev = FakeSaneDev(
            options=build_option_table(
                geometry_type=TYPE_INT,
                geometry_unit=GeometryUnit.UNIT_PIXEL,
                geometry_range=_ROOMY_GEOMETRY_RANGE,
            ),
            pages=1,
        )
        dev.set_page_size(3000, 4000)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=5000, mode="Color", paper_size="a4"
        )

        pages = backend.scan_pages("test:0", settings, page_sink).pages

        assert dev.br_x == 9921
        assert dev.br_y == 14031
        # The area was set on the device, so saneless does not crop as well.
        assert pages[0].size == (3000, 4000)

    @pytest.mark.parametrize("unit", sorted(set(GeometryUnit) - _CONVERTIBLE_UNITS))
    def test_an_unconvertible_unit_falls_through_to_the_crop(
        self,
        unit: GeometryUnit,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """The other five units warn by name and leave the crop to Pillow."""
        dev = _device_reporting_unit(unit)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        with caplog.at_level(logging.WARNING, logger=_SESSION_LOGGER):
            pages = backend.scan_pages("test:0", settings, page_sink).pages

        assert [m for m in _warning_messages(caplog) if unit.name in m]
        assert pages[0].size == _A4_AT_300_DPI

    def test_a_unit_code_outside_sane_does_not_crash_the_scan(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        The option tuple is device-supplied, so a bad code must not be fatal.

        A conforming backend cannot report 99, but nothing in the protocol
        stops a broken one, and a garbage scale factor would silently mis-size
        the page.  It is treated as unconvertible instead.
        """
        dev = _device_reporting_unit(99)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        with caplog.at_level(logging.WARNING, logger=_SESSION_LOGGER):
            pages = backend.scan_pages("test:0", settings, page_sink).pages

        assert [m for m in _warning_messages(caplog) if "99" in m]
        assert pages[0].size == _A4_AT_300_DPI


class TestNonPositiveGeometryScale:
    """
    A scale of zero is declined, not written as a zero-size box.

    ``_units_per_mm`` returns ``resolution / 25.4`` for UNIT_PIXEL, so a device
    whose read-back resolution truncates to 0 yields ``0.0``.  That value is
    not ``None``, so a ``None`` check alone would write a ``(0.0, 0.0)`` box,
    and the clamp check would agree with itself inside a tolerance that is
    also 0.  A zero-area scan reported as success is worse than the crop.
    """

    def test_a_sub_one_dpi_read_back_yields_a_zero_scale(self) -> None:
        """A read-back resolution below 1 dpi yields a scale factor of 0.0."""
        assert (
            scan_session_mod._units_per_mm(
                GeometryUnit.UNIT_PIXEL, 0, fallback="will crop"
            )
            == 0.0
        )

    def test_a_zero_scale_falls_back_to_the_crop(self) -> None:
        """A zero scale makes ``_set_geometry`` decline, so the crop runs."""
        dev = _device_reporting_unit(GeometryUnit.UNIT_PIXEL)

        assert scan_session_mod._set_geometry(dev, "a4", dev.get_options(), 0) is False

    def test_no_zero_size_box_reaches_the_device(self) -> None:
        """No corner is assigned when the scale is zero."""
        dev = _device_reporting_unit(GeometryUnit.UNIT_PIXEL)

        scan_session_mod._set_geometry(dev, "a4", dev.get_options(), 0)

        assert "br_x" not in dev.assignments
        assert "br_y" not in dev.assignments


class TestClampedScanArea:
    """
    A scan area the device quietly shrank is caught on read-back.

    The real ``test`` backend, asked for A4's 210 mm on a ``br-x`` range of
    ``(0.0, 200.0, 1.0)``, stores 200.0 with no error and no ``INFO_INEXACT``
    the caller can see.  Without the read-back, ``_set_geometry`` would report
    an area that is not the one requested, and the page would come out
    quietly wrong.
    """

    def test_a_clamped_area_falls_through_to_the_crop(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """A 200 mm device asked for A4 warns with both values and crops."""
        dev = _device_reporting_unit(GeometryUnit.UNIT_MM, (0.0, 200.0, 1.0))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        with caplog.at_level(logging.WARNING, logger=_SESSION_LOGGER):
            pages = backend.scan_pages("test:0", settings, page_sink).pages

        # The warning has to name what was asked for AND what was got, or the
        # operator cannot tell which of the two is wrong.
        assert [m for m in _warning_messages(caplog) if "210" in m and "200" in m]
        assert pages[0].size == _A4_AT_300_DPI

    def test_a_clamped_top_left_falls_through_to_the_crop(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        A device whose geometry range starts above 0 clamps ``tl``, not ``br``.

        ``dev.tl_x = 0.0`` is clamped up to the range minimum while ``br`` is
        accepted verbatim, so comparing the far corner alone would report
        success over an area short by the whole minimum on each axis: an A4
        request would yield a 200 x 287 mm page, with no warning, no crop, and
        a PDF MediaBox disagreeing with its own content.
        """
        dev = _device_reporting_unit(GeometryUnit.UNIT_MM, (10.0, 300.0, 1.0))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        with caplog.at_level(logging.WARNING, logger=_SESSION_LOGGER):
            pages = backend.scan_pages("test:0", settings, page_sink).pages

        # br really was honoured -- only tl moved, which is what makes the far
        # corner alone an insufficient test rather than a redundant one.
        assert dev.tl_x == pytest.approx(10.0)
        assert dev.br_x == pytest.approx(210.0)
        # 210 requested, 200 of box actually obtained.
        assert [m for m in _warning_messages(caplog) if "210" in m and "200" in m]
        assert pages[0].size == _A4_AT_300_DPI

    def test_a_comfortable_range_is_not_reported_as_clamped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """A device that honours the area sets it and does not crop as well."""
        dev = _device_reporting_unit(GeometryUnit.UNIT_MM, (0.0, 300.0, 1.0))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        with caplog.at_level(logging.WARNING, logger=_SESSION_LOGGER):
            pages = backend.scan_pages("test:0", settings, page_sink).pages

        assert not [m for m in _warning_messages(caplog) if "clamped" in m.lower()]
        assert pages[0].size == (3000, 4000)

    def test_a_fixed_point_round_trip_is_within_tolerance(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        A near-equal read-back must not trigger the fallback.

        Letter's 215.9 mm is not representable in SANE's 16.16 fixed point, so
        it reads back inexact on a device that clamped nothing.  Comparing for
        equality would send this perfectly good scan down the crop path.

        The device's range has no step (quant 0), so nothing is quantised and
        the fixed-point representation is the only difference left to see.
        """
        dev = _device_reporting_unit(GeometryUnit.UNIT_MM, (0.0, 300.0, 0.0))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="letter"
        )

        with caplog.at_level(logging.WARNING, logger=_SESSION_LOGGER):
            pages = backend.scan_pages("test:0", settings, page_sink).pages

        # The round trip really is inexact -- this is what makes the tolerance
        # load-bearing rather than decorative.
        assert dev.br_x != 215.9
        assert dev.br_x == pytest.approx(215.9, abs=1e-4)
        assert not [m for m in _warning_messages(caplog) if "clamped" in m.lower()]
        assert pages[0].size == (3000, 4000)

    def test_the_crop_uses_the_resolution_the_device_chose(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        A clamped dpi must not yield a crop box computed at the requested dpi.

        The crop arithmetic and the device's actual sampling have to agree, or
        a clamped resolution produces a cut-off page even when the fallback
        runs correctly.
        """
        dev = _geometry_less_device()
        # Selecting the source reloads the descriptors and reveals a 75 dpi
        # ceiling, so the 300 requested is clamped to 75.
        dev.narrow_resolution_for_source("Flatbed", (1.0, 75.0, 1.0))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        pages = backend.scan_pages("test:0", settings, page_sink).pages

        # A4 at the 75 dpi the device settled on, 620.1 x 876.97 px, not at
        # the 300 asked for, which would have been 2480x3508.
        assert pages[0].size == (620, 877)


# The options a paper size is written to, in the underscore spelling attribute
# access and the fake's assignment log use.
_PAPER_ATTRIBUTES = frozenset(
    {"page_width", "page_height", "tl_x", "tl_y", "br_x", "br_y"}
)

# A5 is 148 x 210 mm; at 300 dpi that is 1748.03 x 2480.31 px.
_A5_AT_300_DPI = (1748, 2480)


def _feeder_device(*, page_size_options: bool, pages: int = 1) -> FakeSaneDev:
    """
    Build a device with a flatbed, a feeder and an Auto source.

    Args:
        page_size_options: Whether the device offers ``page-width`` and
            ``page-height``, which are active only off the flatbed.
        pages: How many sheets the feeder holds.

    Returns:
        A device whose pages are larger than A4 at 300 dpi, so a crop is
        observable.

    """
    dev = FakeSaneDev(pages=pages)
    dev.report_sources(["Flatbed", "ADF", "Auto"])
    if page_size_options:
        dev.offer_page_size_options()
    dev.set_page_size(3000, 4000)
    return dev


def _paper_assignments(dev: FakeSaneDev) -> list[tuple[str, object]]:
    """
    List the paper-size and scan-area assignments the device received.

    Args:
        dev: The fake device after a scan.

    Returns:
        ``(name, value)`` pairs in the order the options were assigned, each
        with the value the device stored.

    """
    return [
        (name, getattr(dev, name))
        for name in dev.assignments
        if name in _PAPER_ATTRIBUTES
    ]


def _info_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """
    Return the INFO messages captured so far.

    Args:
        caplog: The pytest log-capture fixture.

    Returns:
        One string per captured INFO record.

    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    ]


class TestFeederPaperSize:
    """
    ``paper_size`` on a feeder uses the device's own centring, or nothing.

    A feeder may guide a sheet into the middle of its window rather than
    against one side, and saneless cannot see which.  A top-left scan area or
    crop then cuts the right edge off every page.  Where the device offers
    ``page-width`` and ``page-height`` it is told the paper size and centres
    its own window, and the scan area is set inside that window.  Where it does
    not, the paper size is not applied at all: the page is the full window,
    larger but complete.  The flatbed keeps its top-left area and crop, since a
    sheet on the glass sits in the corner.
    """

    def test_the_page_size_is_set_before_the_scan_area_on_a_feeder(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """Page width and height first, then the area, and no crop."""
        dev = _feeder_device(page_size_options=True)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="ADF", resolution=300, mode="Color", paper_size="a5"
        )

        pages = backend.scan_pages("test:0", settings, page_sink).pages

        assert _paper_assignments(dev) == [
            ("page_width", 148.0),
            ("page_height", 210.0),
            ("tl_x", 0.0),
            ("tl_y", 0.0),
            ("br_x", 148.0),
            ("br_y", 210.0),
        ]
        assert [page.size for page in pages] == [(3000, 4000)]

    def test_the_page_size_is_not_set_on_the_flatbed(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """The options are inactive on the flatbed, which keeps its own area."""
        dev = _feeder_device(page_size_options=True)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a5"
        )

        backend.scan_pages("test:0", settings, page_sink)

        assert _paper_assignments(dev) == [
            ("tl_x", 0.0),
            ("tl_y", 0.0),
            ("br_x", 148.0),
            ("br_y", 210.0),
        ]

    def test_a_rejected_scan_area_inside_the_centred_window_is_cropped(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        Once the device has centred its window, the top-left crop is right.

        The window starts at the sheet's left edge, so cutting it down to the
        paper size from its top-left corner loses nothing.
        """
        dev = FakeSaneDev(
            options=build_option_table(omit=_GEOMETRY_OPTION_NAMES), pages=1
        )
        dev.report_sources(["Flatbed", "ADF"])
        dev.offer_page_size_options()
        dev.set_page_size(3000, 4000)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="ADF", resolution=300, mode="Color", paper_size="a5"
        )

        pages = backend.scan_pages("test:0", settings, page_sink).pages

        assert _paper_assignments(dev) == [
            ("page_width", 148.0),
            ("page_height", 210.0),
        ]
        assert [page.size for page in pages] == [_A5_AT_300_DPI]

    @pytest.mark.parametrize(
        ("source", "auto_source_mode"),
        [("ADF", "flatbed"), ("Auto", "adf")],
        ids=["named-feeder", "auto-routed-to-the-feeder"],
    )
    def test_a_feeder_without_page_size_options_is_left_whole(
        self,
        source: str,
        auto_source_mode: Literal["flatbed", "adf"],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        No area, no crop, and one INFO line saying why.

        The routing decides, not the source's name: an Auto source sent
        through the feeder has the same unknown registration as a named one.
        Nothing is lost, so it is a log line and not a warning.
        """
        dev = _feeder_device(page_size_options=False, pages=2)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source=source,
            resolution=300,
            mode="Color",
            paper_size="a4",
            auto_source_mode=auto_source_mode,
        )

        with caplog.at_level(logging.INFO, logger=_SESSION_LOGGER):
            pages = backend.scan_pages("test:0", settings, page_sink).pages

        assert _paper_assignments(dev) == []
        assert [page.size for page in pages] == [(3000, 4000), (3000, 4000)]
        skipped = [m for m in _info_messages(caplog) if "a4" in m and "page-width" in m]
        assert len(skipped) == 1
        paper_warnings = [
            m
            for m in _warning_messages(caplog)
            if "a4" in m or "scan area" in m.lower() or "crop" in m.lower()
        ]
        assert paper_warnings == []

    def test_an_auto_source_kept_on_the_glass_keeps_its_scan_area(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """An Auto source scanned as a flatbed is framed as a flatbed."""
        dev = _feeder_device(page_size_options=False)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Auto",
            resolution=300,
            mode="Color",
            paper_size="a5",
            auto_source_mode="flatbed",
        )

        backend.scan_pages("test:0", settings, page_sink)

        assert _paper_assignments(dev) == [
            ("tl_x", 0.0),
            ("tl_y", 0.0),
            ("br_x", 148.0),
            ("br_y", 210.0),
        ]

    def test_integer_page_size_options_are_given_whole_numbers(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """Integer page-size options get whole numbers, as a float fails the scan."""
        dev = _feeder_device(page_size_options=False)
        dev.offer_page_size_options(
            value_type=TYPE_INT,
            unit=GeometryUnit.UNIT_PIXEL,
            span=_ROOMY_GEOMETRY_RANGE,
        )
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="ADF", resolution=300, mode="Color", paper_size="a5"
        )

        backend.scan_pages("test:0", settings, page_sink)

        # 148 mm and 210 mm at 300 dpi, rounded to the nearest pixel.
        assert dev.page_width == round(148 / 25.4 * 300) == 1748
        assert dev.page_height == round(210 / 25.4 * 300) == 2480

    def test_an_unconvertible_page_size_unit_says_the_full_window_is_scanned(
        self,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        The WARNING names what happens on this path, not the scan-area one.

        A feeder whose page-size options are in a unit saneless cannot convert
        is left to scan its full window, uncropped, so a WARNING saying the
        page will be cropped would describe something that does not happen.
        """
        dev = _feeder_device(page_size_options=False)
        dev.offer_page_size_options(unit=GeometryUnit.UNIT_DPI)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="ADF", resolution=300, mode="Color", paper_size="a5"
        )
        caplog.set_level(logging.WARNING, logger=_SESSION_LOGGER)

        backend.scan_pages("test:0", settings, page_sink)

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == _SESSION_LOGGER and "UNIT_DPI" in record.getMessage()
        ]
        assert messages
        assert all("will scan the full window" in message for message in messages)
        assert not any("will crop" in message for message in messages)


class TestIntegerGeometry:
    """
    A scan area of integer options is written as whole numbers.

    python-sane refuses a float for an integer option, even a whole one, so
    writing ``0.0`` to a pixel scan area would raise, and the page would fall
    back to a crop that the device could have done itself.
    """

    @pytest.mark.parametrize(
        ("unit", "expected"),
        [
            (
                GeometryUnit.UNIT_PIXEL,
                (round(210 / 25.4 * 300), round(297 / 25.4 * 300)),
            ),
            (GeometryUnit.UNIT_MM, (210, 297)),
        ],
        ids=["pixels", "millimetres"],
    )
    def test_the_area_is_written_as_integers_and_accepted(
        self,
        unit: GeometryUnit,
        expected: tuple[int, int],
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
    ) -> None:
        """The device keeps the area, so nothing is cropped afterwards."""
        dev = FakeSaneDev(
            options=build_option_table(
                geometry_type=TYPE_INT,
                geometry_unit=unit,
                geometry_range=_ROOMY_GEOMETRY_RANGE,
            ),
            pages=1,
        )
        dev.set_page_size(3000, 4000)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        pages = backend.scan_pages("test:0", settings, page_sink).pages

        area = [dev.tl_x, dev.tl_y, dev.br_x, dev.br_y]
        assert [type(value) for value in area] == [int, int, int, int]
        assert (dev.br_x, dev.br_y) == expected
        assert pages[0].size == (3000, 4000)


# ---------------------------------------------------------------------------
# The shared fake and the python-sane contract it models
# ---------------------------------------------------------------------------


# SANE's SANE_CAP_INACTIVE bit, index 7 of an option tuple.
_CAP_INACTIVE = 32


class TestFakeSaneOptionReload:
    """The fake's assignment log and its source-triggered option reload."""

    def test_assignments_are_recorded_in_order(self) -> None:
        """Every assignment the device really received, in the order given."""
        dev = FakeSaneDev()
        dev.mode = "Gray"
        dev.resolution = 200
        dev.source = "Flatbed"
        assert dev.assignments == ["mode", "resolution", "source"]

    def test_an_unknown_option_is_not_recorded_as_a_device_call(self) -> None:
        """An unrecognised name is stored silently, so it is not a device call."""
        dev = FakeSaneDev()
        dev.not_an_option = 1
        assert dev.assignments == []

    def test_a_source_change_narrows_the_resolution_constraint(self) -> None:
        """Selecting the armed source swaps in its narrower range."""
        dev = FakeSaneDev()
        dev.narrow_resolution_for_source("ADF Duplex", (1.0, 600.0, 1.0))
        dev.source = "ADF Duplex"

        constraints = {opt[1]: opt[8] for opt in dev.get_options()}
        assert constraints["resolution"] == (1.0, 600.0, 1.0)

        dev.resolution = 1000
        assert dev.resolution == 600.0

    def test_a_value_set_before_the_reload_is_not_re_validated(self) -> None:
        """A value set before a reload is not re-validated against the new range."""
        dev = FakeSaneDev()
        dev.narrow_resolution_for_source("ADF Duplex", (1.0, 600.0, 1.0))
        dev.resolution = 1000
        dev.source = "ADF Duplex"
        assert dev.resolution == 1000.0

    @pytest.mark.parametrize(
        ("activates", "active_on_the_feeder"),
        [
            pytest.param(True, True, id="duplex-hardware"),
            pytest.param(False, False, id="no-duplex-hardware"),
        ],
    )
    def test_adf_mode_follows_the_source_when_it_activates(
        self, *, activates: bool, active_on_the_feeder: bool
    ) -> None:
        """``adf-mode`` is inactive on the flatbed, and on the feeder if armed so."""
        dev = FakeSaneDev()
        dev.offer_adf_mode(activates=activates)

        def inactive() -> bool:
            caps = {opt[1]: opt[7] for opt in dev.get_options()}
            return bool(caps["adf-mode"] & _CAP_INACTIVE)

        assert inactive()
        dev.source = "Automatic Document Feeder"
        assert inactive() is not active_on_the_feeder
        dev.source = "Flatbed"
        assert inactive()


# ---------------------------------------------------------------------------
# Source-first option ordering and the resolution read-back
# ---------------------------------------------------------------------------


class TestDeviceOptionOrdering:
    """Options are assigned source-first, so a reload cannot strand them."""

    def test_options_are_assigned_source_first(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """The order the device observes is source, then mode, then resolution."""
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        backend.scan_pages("test:0", settings, page_sink)

        assert dev.assignments == ["source", "mode", "resolution"]

    def test_a_device_without_a_source_option_is_still_configured(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """No source option means no source assignment, but mode and res still land."""
        dev = FakeSaneDev(
            options=[
                (2, "mode", "Scan mode", "Mode desc", 3, 0, 1, 5, ["Color"]),
                (
                    3,
                    "resolution",
                    "Resolution",
                    "Res desc",
                    2,
                    4,
                    4,
                    5,
                    (1.0, 1200.0, 1.0),
                ),
            ],
            pages=1,
        )
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        backend.scan_pages("test:0", settings, page_sink)

        assert dev.assignments == ["mode", "resolution"]
        assert dev.resolution == 300.0

    def test_source_first_keeps_resolution_inside_a_narrowed_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        A feeder ceiling of 600 is respected because the source is set first.

        In mode/resolution/source order the 1000 dpi request would be validated
        against the platen's 1200 dpi range and then stranded there when
        selecting the feeder reloads the descriptors, leaving the device holding
        a value its active constraint does not permit.
        """
        feeder = "Automatic Document Feeder"
        dev = FakeSaneDev(pages=1)
        dev.narrow_resolution_for_source(feeder, (1.0, 600.0, 1.0))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source=feeder, resolution=1000, mode="Color")

        backend.scan_pages("test:0", settings, page_sink)

        # ``resolution`` is declared on the fake with the type the real device
        # hands back, so this isinstance is a runtime assertion rather than a
        # static narrowing: it pins that the device really reports a float.
        resolution = dev.resolution
        assert isinstance(resolution, float)
        assert 1.0 <= resolution <= 600.0


# ---------------------------------------------------------------------------
# Hardware duplex through a separate ADF-mode option
# ---------------------------------------------------------------------------

# The option python-sane reaches through ``getattr``, in its underscore
# spelling; ``get_options()`` reports it as ``adf-mode``.
_ADF_MODE = "adf_mode"

_ADF_SOURCE = "ADF"


def _adf_mode_device(*, activates: bool = True) -> FakeSaneDev:
    """
    Build a device with a flatbed, a feeder and an ``adf-mode`` option.

    Args:
        activates: Whether selecting the feeder makes ``adf-mode`` active, as
            it does on an epson2 whose feeder can duplex.

    Returns:
        A one-sheet device that selects duplex by ``adf-mode``, not by a
        source name.

    """
    dev = FakeSaneDev(pages=1)
    dev.report_sources(["Flatbed", _ADF_SOURCE])
    dev.offer_adf_mode(activates=activates)
    return dev


def _backend_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """
    Return the WARNING messages the scanner backend logged.

    Args:
        caplog: The pytest log-capture fixture.

    Returns:
        One string per WARNING record from either of the backend's loggers.

    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.name in _SCANNER_LOGGERS and record.levelno == logging.WARNING
    ]


class TestAdfMode:
    """
    Hardware duplex on a scanner that selects it with ``adf-mode``.

    epson2, kodakaio and magicolor offer one feeder source and a separate
    ``adf-mode`` string list, ``["Simplex", "Duplex"]``, instead of a both-sides
    source name. So ``duplex = "hardware"`` has to set that option, right after
    the source, or the feeder scans one side while the profile says two. An
    ``adf-mode`` the device reports inactive -- epson2 when the feeder cannot
    duplex -- refuses the scan before paper moves. Option values persist
    across handles, so any other scan on an active ``adf-mode`` sets
    ``Simplex`` explicitly.
    """

    def _scan(
        self,
        dev: FakeSaneDev,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        *,
        source: str,
        duplex: Literal["none", "hardware", "manual"],
    ) -> ScanBatch:
        """
        Scan once from ``dev`` in colour at 300 dpi.

        Args:
            dev: The device to scan from.
            monkeypatch: Fixture used to wire the device into the backend.
            page_sink: Where the pages go.
            source: The profile's source.
            duplex: The profile's duplex setting.

        Returns:
            The batch the scan returned.

        """
        settings = ScanSettings(
            source=source, resolution=300, mode="Color", duplex=duplex
        )
        return _backend_with(dev, monkeypatch).scan_pages(
            _TEST_DEVICE, settings, page_sink
        )

    def test_hardware_duplex_sets_duplex_right_after_the_source(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """Source, then adf_mode = 'Duplex', then mode and resolution."""
        dev = _adf_mode_device()

        batch = self._scan(
            dev, monkeypatch, page_sink, source=_ADF_SOURCE, duplex="hardware"
        )

        assert dev.assignments == ["source", _ADF_MODE, "mode", "resolution"]
        assert getattr(dev, _ADF_MODE) == "Duplex"
        assert len(batch.pages) == 1

    def test_an_inactive_adf_mode_refuses_hardware_duplex_before_start(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        An epson2 that cannot duplex keeps ``adf-mode`` inactive on the feeder.

        Scanning one side of every sheet under a profile that says both would
        be a silent loss of every back page, so the scan is refused, naming
        the option and the value, before any ``start()``.
        """
        dev = _adf_mode_device(activates=False)

        with pytest.raises(ScanError) as excinfo:
            self._scan(
                dev, monkeypatch, page_sink, source=_ADF_SOURCE, duplex="hardware"
            )

        message = str(excinfo.value)
        assert _ADF_MODE in message
        assert "'Duplex'" in message
        assert "start" not in dev.calls

    def test_a_simplex_feeder_scan_sets_simplex_explicitly(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """A 'Duplex' left behind by an earlier handle is put back to 'Simplex'."""
        dev = _adf_mode_device()
        dev.source = _ADF_SOURCE
        setattr(dev, _ADF_MODE, "Duplex")
        dev.assignments.clear()

        batch = self._scan(
            dev, monkeypatch, page_sink, source=_ADF_SOURCE, duplex="none"
        )

        assert dev.assignments == ["source", _ADF_MODE, "mode", "resolution"]
        assert getattr(dev, _ADF_MODE) == "Simplex"
        assert len(batch.pages) == 1

    def test_an_inactive_adf_mode_is_left_alone_on_the_flatbed(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """On the flatbed ``adf-mode`` is inactive, and nothing is written to it."""
        dev = _adf_mode_device()

        self._scan(dev, monkeypatch, page_sink, source="Flatbed", duplex="none")

        assert dev.assignments == ["source", "mode", "resolution"]

    def test_hardware_duplex_with_no_way_to_select_it_warns_and_scans(
        self,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A single-sided feeder name and no ``adf-mode``: one side, said aloud.

        Nothing the device reports selects duplex, so the scan goes ahead, and a
        WARNING says only one side of each sheet is scanned.
        """
        dev = FakeSaneDev(pages=1)
        dev.report_sources(["Flatbed", _ADF_SOURCE])
        caplog.set_level(logging.WARNING, logger=_SESSION_LOGGER)

        batch = self._scan(
            dev, monkeypatch, page_sink, source=_ADF_SOURCE, duplex="hardware"
        )

        assert _ADF_MODE not in dev.assignments
        assert any("one side" in message for message in _backend_warnings(caplog))
        assert len(batch.pages) == 1

    def test_a_duplex_source_name_needs_no_adf_mode_and_no_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A both-sides source name selects duplex by itself."""
        dev = FakeSaneDev(pages=2)
        dev.report_sources(["Flatbed", _ADF_SOURCE, "ADF Duplex"])
        caplog.set_level(logging.WARNING, logger=_SESSION_LOGGER)

        self._scan(dev, monkeypatch, page_sink, source="ADF Duplex", duplex="hardware")

        assert _ADF_MODE not in dev.assignments
        assert _backend_warnings(caplog) == []

    def test_each_manual_duplex_pass_sets_simplex(
        self,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        second_pass_sink: SpooledPageSink,
    ) -> None:
        """
        Both passes of a manual duplex job scan one side, whatever came before.

        The device is left at 'Duplex' before each pass, as another profile's
        hardware-duplex scan would leave it, and each pass puts it back.
        """
        dev = _adf_mode_device()
        dev.source = _ADF_SOURCE
        passes = []
        for sink in (page_sink, second_pass_sink):
            setattr(dev, _ADF_MODE, "Duplex")
            dev.assignments.clear()
            dev.load_feeder([_make_content_image()])
            self._scan(dev, monkeypatch, sink, source=_ADF_SOURCE, duplex="manual")
            passes.append((list(dev.assignments), getattr(dev, _ADF_MODE)))

        expected = (["source", _ADF_MODE, "mode", "resolution"], "Simplex")
        assert passes == [expected, expected]


# The nine-element option tuple, as get_options() reports it:
# (index, name, title, desc, type, unit, size, cap, constraint).  These are the
# two value-type codes and the one capability code the tables below vary.
_STRING_OPTION = 3
_FIXED_OPTION = 2
_SETTABLE = 5


def _option(index: int, name: str, value_type: int, constraint: object) -> tuple:
    """
    Build one nine-element SANE option tuple carrying a given constraint.

    Args:
        index: The option's position in the device's option list.
        name: The hyphenated option name, as ``get_options()`` reports it.
        value_type: The SANE value type code.
        constraint: What the device reports for the option -- a word list, a
            ``(min, max, step)`` triple, or None for an unconstrained option.

    Returns:
        The option tuple.

    """
    return (
        index,
        name,
        name.title(),
        f"{name} description",
        value_type,
        0,
        1,
        _SETTABLE,
        constraint,
    )


def _capabilities_for(
    constraint: object, monkeypatch: pytest.MonkeyPatch
) -> DeviceCapabilities:
    """
    Read capabilities from a device reporting a given resolution constraint.

    Args:
        constraint: What the device reports for its ``resolution`` option.
        monkeypatch: Fixture used to patch the module-level ``sane`` name.

    Returns:
        The capabilities parsed from that device.

    """
    dev = FakeSaneDev(
        options=[
            _option(1, "source", _STRING_OPTION, ["Flatbed", "ADF Duplex"]),
            _option(2, "resolution", _FIXED_OPTION, constraint),
            _option(3, "mode", _STRING_OPTION, ["Color", "Gray"]),
        ]
    )
    return _backend_with(dev, monkeypatch).get_capabilities("test:0")


class TestResolutionConstraintShapes:
    """
    All three documented constraint shapes are read, and none is guessed at.

    A SANE device reports its resolution support as *either* a word list *or* a
    ``(min, max, step)`` range, never both.  So ``resolutions`` and
    ``resolution_range`` are two different facts rather than two spellings of
    one, and neither is derived from the other: expanding a range into a list
    would print saneless's invention rather than the device's answer, and
    inferring a range from a list would claim support for values between the
    listed ones.  At most one of the two is ever populated.
    """

    @pytest.mark.parametrize(
        ("constraint", "expected_list", "expected_range"),
        [
            ([75, 150, 300, 600], [75, 150, 300, 600], None),
            ((1.0, 1200.0, 1.0), [], (1.0, 1200.0, 1.0)),
            (None, [], None),
        ],
        ids=["word-list", "range", "unconstrained"],
    )
    def test_each_shape_populates_only_the_field_it_describes(
        self,
        constraint: object,
        expected_list: list[int],
        expected_range: tuple[float, float, float] | None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A list fills resolutions, a range fills the range, None fills neither."""
        caps = _capabilities_for(constraint, monkeypatch)

        assert caps.resolutions == expected_list
        assert caps.resolution_range == expected_range
        # Whatever the device said, the two cannot both be populated and so
        # cannot disagree with each other.
        assert not (caps.resolutions and caps.resolution_range)

    def test_the_range_is_kept_as_the_device_reported_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The members stay floats; coercion belongs at the point of use."""
        caps = _capabilities_for((1.0, 1200.0, 1.0), monkeypatch)

        assert caps.resolution_range is not None
        assert all(isinstance(member, float) for member in caps.resolution_range)

    def test_sources_and_modes_still_come_from_their_list_constraints(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sources and modes come from their word lists beside a range resolution."""
        caps = _capabilities_for((1.0, 1200.0, 1.0), monkeypatch)

        assert caps.sources == ["Flatbed", "ADF Duplex"]
        assert caps.modes == ["Color", "Gray"]

    @pytest.mark.parametrize(
        "constraint",
        [(1.0, 1200.0), (1.0, 1200.0, 1.0, 1.0)],
        ids=["too-short", "too-long"],
    )
    def test_a_tuple_of_the_wrong_arity_is_not_guessed_at(
        self, constraint: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A device-supplied tuple of the wrong shape yields neither field.

        The constraint object comes from the device, so nothing guarantees it is
        one of the three documented shapes.  An unrecognised one must leave both
        fields alone rather than produce a guessed value, and must not raise out
        of ``get_capabilities``.
        """
        caps = _capabilities_for(constraint, monkeypatch)

        assert caps.resolutions == []
        assert caps.resolution_range is None

    def test_a_range_whose_members_are_not_numbers_is_not_guessed_at(self) -> None:
        """
        A range whose members are not numbers yields neither field.

        Driven straight through ``_constraint``: the shared fake coerces a
        range's members with ``float()``, as no real SANE backend can report a
        range of strings, so this guard against a malformed device is asserted
        at the function that does the hardening.
        """
        found = options_mod._constraint(
            [_option(2, "resolution", _FIXED_OPTION, ("low", "high", "step"))],
            "resolution",
        )

        assert found.present is True
        assert found.values is None
        assert found.span is None

    def test_a_short_option_tuple_is_skipped_without_raising(self) -> None:
        """An option too short to carry a constraint is ignored, never fatal."""
        found = options_mod._constraint([(1, "resolution")], "resolution")

        assert found.present is False


# The SANE value-type code for an INT option, beside the two above.
_INT_OPTION = 1

# The option python-sane reaches through ``getattr``, as the protocol does not
# declare it.
_DEPTH = "depth"

_FEEDER_SOURCE = "Automatic Document Feeder"


def _device_with_depth(value_type: int, constraint: object) -> FakeSaneDev:
    """
    Build the default device with a ``depth`` option of a given shape.

    Args:
        value_type: The SANE value type code for the option.
        constraint: The word list or ``(min, max, step)`` the device reports.

    Returns:
        A one-sheet device whose table ends with that ``depth`` option.

    """
    return FakeSaneDev(
        options=[*build_option_table(), _option(12, "depth", value_type, constraint)],
        pages=1,
    )


class TestSixteenBitDepth:
    """
    A scan runs at 8 bits per sample, or is refused before paper moves.

    python-sane misreads a 16-bit frame -- the page comes back twice as tall --
    and saneless writes 8-bit output anyway. So ``depth`` is set to 8 whenever
    the device offers 8, silently, and any frame still reported as 16-bit is
    refused before ``start()``.
    """

    def _scan(
        self,
        dev: FakeSaneDev,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        source: str = "Flatbed",
    ) -> ScanBatch:
        """
        Scan once from ``dev`` in colour at 300 dpi.

        Args:
            dev: The device to scan from.
            monkeypatch: Fixture used to wire the device into the backend.
            page_sink: Where the pages go.
            source: The profile's source.

        Returns:
            The batch the scan returned.

        """
        settings = ScanSettings(source=source, resolution=300, mode="Color")
        return _backend_with(dev, monkeypatch).scan_pages(
            _TEST_DEVICE, settings, page_sink
        )

    def test_depth_is_set_to_eight_after_mode_and_before_resolution(
        self,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A device offering [1, 8, 16] is set to the int 8, and nothing is logged.

        After ``mode``, because a mode change can reload the depth constraint;
        before ``resolution``, which is read back afterwards. An int, because
        ``depth`` is an INT option and python-sane refuses ``8.0`` for one.
        """
        dev = FakeSaneDev(pages=1)
        dev.offer_depth()
        caplog.set_level(logging.WARNING, logger=_BACKEND_LOGGER)

        batch = self._scan(dev, monkeypatch, page_sink)

        assert dev.assignments == ["source", "mode", "depth", "resolution"]
        depth = getattr(dev, _DEPTH)
        assert depth == 8
        assert type(depth) is int
        assert len(batch.pages) == 1
        assert _guard_warnings(caplog) == []

    @pytest.mark.parametrize(
        ("value_type", "constraint", "expected"),
        [
            pytest.param(_INT_OPTION, (1, 16, 1), 8, id="int-range"),
            pytest.param(_INT_OPTION, (0, 16, 4), 8, id="int-range-on-grid"),
            pytest.param(_FIXED_OPTION, (1.0, 16.0, 1.0), 8.0, id="fixed-range"),
            pytest.param(_FIXED_OPTION, (1.0, 16.0, 0.0), 8.0, id="fixed-no-step"),
        ],
    )
    def test_a_range_that_includes_eight_is_set_to_eight(
        self,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        value_type: int,
        constraint: tuple[float, float, float],
        expected: float,
    ) -> None:
        """
        A range includes 8 when 8 lies within it on its step grid.

        The value is written in the option's own type: an int for INT, since
        python-sane refuses a float there, and a float for FIXED.
        """
        dev = _device_with_depth(value_type, constraint)

        self._scan(dev, monkeypatch, page_sink)

        assert "depth" in dev.assignments
        depth = getattr(dev, _DEPTH)
        assert depth == expected
        assert type(depth) is type(expected)

    @pytest.mark.parametrize(
        "constraint",
        [
            pytest.param([1, 16], id="list-without-eight"),
            pytest.param((1, 16, 5), id="range-grid-misses-eight"),
            pytest.param((10, 16, 1), id="range-above-eight"),
        ],
    )
    def test_a_constraint_without_eight_is_left_alone(
        self,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        constraint: object,
    ) -> None:
        """
        No depth is written when 8 is not one the device can take.

        The device keeps its own depth; none of these starts at 16, so the scan
        goes ahead.
        """
        dev = _device_with_depth(_INT_OPTION, constraint)

        batch = self._scan(dev, monkeypatch, page_sink)

        assert dev.assignments == ["source", "mode", "resolution"]
        assert len(batch.pages) == 1

    def test_an_inactive_depth_option_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        A ``depth`` the device has switched off is not written, and the scan runs.

        Writing an inactive option raises, so a device that lists ``depth`` but
        disables it in the chosen mode would otherwise fail every scan.
        """
        inactive_settable = 32 | _SETTABLE
        depth = _option(12, "depth", _INT_OPTION, [1, 8, 16])
        dev = FakeSaneDev(
            options=[*build_option_table(), (*depth[:7], inactive_settable, depth[8])],
            pages=1,
        )

        batch = self._scan(dev, monkeypatch, page_sink)

        assert dev.assignments == ["source", "mode", "resolution"]
        assert len(batch.pages) == 1

    def test_a_depth_the_mode_switches_off_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        A ``depth`` that goes inactive when ``Lineart`` is set is not written.

        epson2 switches ``depth`` off for its 1-bit modes. The list read after
        the source still reports it active and offering 8, but the mode change
        reloads the descriptors, so writing 8 then would fail the scan before
        any page. The decision has to come from the list read after ``mode``.
        """
        dev = FakeSaneDev(pages=1)
        dev.offer_depth()
        dev.deactivate_depth_in_modes(("Lineart",))
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Lineart")

        batch = _backend_with(dev, monkeypatch).scan_pages(
            _TEST_DEVICE, settings, page_sink
        )

        assert dev.assignments == ["source", "mode", "resolution"]
        assert len(batch.pages) == 1

    def test_a_depth_the_mode_leaves_on_is_still_set_to_eight(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        The same device in ``Color`` keeps ``depth`` active, and it is set to 8.

        Deciding from the list read after ``mode`` must not lose the depth on a
        mode that leaves it on.
        """
        dev = FakeSaneDev(pages=1)
        dev.offer_depth()
        dev.deactivate_depth_in_modes(("Lineart",))

        self._scan(dev, monkeypatch, page_sink)

        assert dev.assignments == ["source", "mode", "depth", "resolution"]
        assert getattr(dev, _DEPTH) == 8

    def test_a_device_offering_only_sixteen_is_refused_before_start(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        With 16 the only depth, nothing is assigned and the frame is refused.

        The refusal comes from the device's own parameters, read after the
        options are set, and names the device; nothing is started.
        """
        dev = FakeSaneDev(pages=1)
        dev.offer_depth([16])

        with pytest.raises(ScanError) as excinfo:
            self._scan(dev, monkeypatch, page_sink)

        assert dev.assignments == ["source", "mode", "resolution"]
        assert dev.get_parameters()[3] == 16
        assert "start" not in dev.calls
        assert _TEST_DEVICE in str(excinfo.value)
        assert "16 bits" in str(excinfo.value)

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param("Flatbed", id="flatbed"),
            pytest.param(_FEEDER_SOURCE, id="feeder"),
        ],
    )
    def test_a_mode_implying_sixteen_bits_is_refused_before_start(
        self,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        source: str,
    ) -> None:
        """
        A device with no ``depth`` option can still report a 16-bit frame.

        A mode such as "Color (48 bits)" implies 16 bits per sample without
        any depth option to set, so the parameters are what is checked, and on
        both acquisition paths.
        """
        dev = FakeSaneDev(pages=1)
        dev.set_parameters(depth=16)

        with pytest.raises(ScanError) as excinfo:
            self._scan(dev, monkeypatch, page_sink, source=source)

        assert "start" not in dev.calls
        assert _TEST_DEVICE in str(excinfo.value)

    def test_a_default_device_is_configured_as_before_and_scans(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """No ``depth`` option means no depth assignment, and an 8-bit scan."""
        dev = FakeSaneDev(pages=1)

        batch = self._scan(dev, monkeypatch, page_sink)

        assert dev.assignments == ["source", "mode", "resolution"]
        assert len(batch.pages) == 1
        assert dev.get_parameters_calls == 1

    def test_a_failed_parameter_read_is_a_scan_error_before_start(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """A device that cannot report its parameters is refused, naming it."""
        dev = FakeSaneDev(pages=1)
        dev.fail_call("get_parameters", FakeSaneError("I/O error"))

        with pytest.raises(ScanError) as excinfo:
            self._scan(dev, monkeypatch, page_sink)

        assert "start" not in dev.calls
        assert _TEST_DEVICE in str(excinfo.value)
        assert "I/O error" in str(excinfo.value)

    def test_the_options_are_read_again_after_the_source_and_the_mode(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        A source or mode change reloads the descriptors, so they are read thrice.

        Once to match the source, once after assigning it so that ``adf-mode``
        is decided for the source actually selected, and once after ``mode``
        so that ``depth`` -- and everything decided after configuration -- is
        decided from the list that describes the configured device.
        """
        dev = FakeSaneDev(pages=1)

        self._scan(dev, monkeypatch, page_sink)

        assert dev.get_options_calls == 3

    def test_depth_is_decided_from_the_list_read_after_the_source(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        A depth option that appears only once the source is set is still used.

        The first read offers no ``depth``; the device adds it when the
        source is assigned, as a reload would. Deciding from the first list
        would skip it and leave the device at 16.
        """
        dev = FakeSaneDev(pages=1)
        original = dev.get_options

        def reveal_depth_after_source() -> list[tuple]:
            if "source" in dev.assignments and _DEPTH not in dev.opt:
                dev.offer_depth([16, 8])
            return original()

        # The device stores a name that is not one of its options on itself,
        # as python-sane does, so this shadows get_options for this device.
        monkeypatch.setattr(dev, "get_options", reveal_depth_after_source)

        self._scan(dev, monkeypatch, page_sink)

        assert dev.assignments == ["source", "mode", "depth", "resolution"]
        assert getattr(dev, _DEPTH) == 8


# An A4 sheet at 1200 dpi, as a device reports it: 9921 x 14031 pixels, with
# three bytes a pixel in colour and one in grey.
_A4_1200_WIDTH = 9921
_A4_1200_LINES = 14031


def _parameters(
    frame_format: str, pixels_per_line: int, lines: int, bytes_per_line: int
) -> page_budget_mod._ScanParameters:
    """
    Build the parameters a device reports for an 8-bit frame.

    Args:
        frame_format: SANE's frame format, such as ``"color"``.
        pixels_per_line: The width in pixels.
        lines: The height in lines, or -1 for a length not known in advance.
        bytes_per_line: The length of one line of image data.

    Returns:
        The parameters.

    """
    return page_budget_mod._ScanParameters(
        frame_format=frame_format,
        last_frame=True,
        pixels_per_line=pixels_per_line,
        lines=lines,
        depth=8,
        bytes_per_line=bytes_per_line,
    )


class TestPageBudget:
    """
    Each page's timeout scales with the page the device agreed to send.

    A fixed limit would cut off honest high-resolution colour scans on slow
    links and blame the network.  The budget is twice an estimate anchored on
    one minute for an A4 colour page at 600 dpi, scaled by how many bytes the
    negotiated page holds, and never below two minutes.  There is no setting:
    the device's own parameters decide it.
    """

    @pytest.mark.parametrize(
        ("parameters", "resolution", "expected"),
        [
            pytest.param(
                _parameters("color", 4961, 7016, 4961 * 3),
                600,
                120.0,
                id="a4-600-colour",
            ),
            pytest.param(
                _parameters(
                    "color", _A4_1200_WIDTH, _A4_1200_LINES, _A4_1200_WIDTH * 3
                ),
                1200,
                pytest.approx(480, rel=0.01),
                id="a4-1200-colour",
            ),
            pytest.param(
                _parameters("gray", _A4_1200_WIDTH, _A4_1200_LINES, _A4_1200_WIDTH),
                1200,
                pytest.approx(160, rel=0.01),
                id="a4-1200-grey",
            ),
            pytest.param(
                _parameters("gray", 2480, 3508, 2480), 300, 120.0, id="a4-300-grey"
            ),
            pytest.param(
                _parameters("color", 2480, 3508, 2480 * 3),
                300,
                120.0,
                id="a4-300-colour",
            ),
            pytest.param(
                _parameters("red", _A4_1200_WIDTH, _A4_1200_LINES, _A4_1200_WIDTH),
                1200,
                pytest.approx(480, rel=0.01),
                id="three-pass-counts-three-frames",
            ),
            pytest.param(
                _parameters("color", _A4_1200_WIDTH, _A4_1200_LINES, -5),
                1200,
                120.0,
                id="negative-line-length-is-the-floor",
            ),
        ],
    )
    def test_the_budget_scales_with_the_negotiated_page(
        self,
        parameters: page_budget_mod._ScanParameters,
        resolution: int,
        expected: object,
    ) -> None:
        """Twice the page's share of a minute per A4 colour page at 600 dpi."""
        assert page_budget_mod._page_budget_seconds(parameters, resolution) == expected

    def test_a_page_of_unknown_length_is_budgeted_as_legal_length(self) -> None:
        """
        SANE's "length not known in advance" is budgeted as a legal sheet.

        A legal sheet is 355.6 mm, 16,800 lines at 1200 dpi, longer than A4,
        so the budget is more than A4's and follows from that length.
        """
        unknown = _parameters("color", _A4_1200_WIDTH, -1, _A4_1200_WIDTH * 3)
        legal = _parameters("color", _A4_1200_WIDTH, 16800, _A4_1200_WIDTH * 3)

        budget = page_budget_mod._page_budget_seconds(unknown, 1200)

        assert budget > 480
        assert budget == pytest.approx(
            page_budget_mod._page_budget_seconds(legal, 1200), rel=0.001
        )

    @pytest.mark.parametrize(
        ("parameters", "resolution"),
        [
            pytest.param(
                _parameters("color", 1, -1, 2**31 - 1),
                1200,
                id="huge-line-of-unknown-length",
            ),
            pytest.param(
                _parameters("red", 1, 2**31 - 1, 2**31 - 1),
                1200,
                id="huge-three-pass-frame",
            ),
        ],
    )
    def test_a_nonsense_frame_is_capped_at_an_hour(
        self, parameters: page_budget_mod._ScanParameters, resolution: int
    ) -> None:
        """
        The device's numbers cannot make one page's limit unbounded.

        Uncapped, the first budget is months and the second is too large for
        ``threading.Event.wait`` to accept at all.
        """
        budget = page_budget_mod._page_budget_seconds(parameters, resolution)

        assert budget == 3600.0
        assert budget == page_budget_mod._PAGE_TIMEOUT_CEILING_SECONDS
        assert budget < threading.TIMEOUT_MAX

    def test_the_floor_is_two_minutes_with_no_setting(self) -> None:
        """The floor is two minutes, and a default budget uses it."""
        assert page_budget_mod._PAGE_TIMEOUT_FLOOR_SECONDS == 120.0
        assert sane_backend_mod._PageBudget().timeout == 120.0
        assert sane_backend_mod._PageBudget().page is None

    @staticmethod
    def _spy_on_budgets(
        monkeypatch: pytest.MonkeyPatch,
    ) -> list[sane_backend_mod._PageBudget]:
        """
        Record the budget every acquisition is given, then acquire as usual.

        Args:
            monkeypatch: Fixture used to wrap the acquisition.

        Returns:
            The list the budgets are appended to, in acquisition order.

        """
        budgets: list[sane_backend_mod._PageBudget] = []
        real = sane_backend_mod._acquire_with_timeout

        def spy(
            dev: scan_session_mod.SaneDevice,
            work: Callable[[], object],
            page_label: str,
            budget: sane_backend_mod._PageBudget,
        ) -> Image.Image:
            budgets.append(budget)
            return real(dev, work, page_label, budget)

        monkeypatch.setattr(sane_backend_mod, "_acquire_with_timeout", spy)
        return budgets

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param("Flatbed", id="flatbed"),
            pytest.param(_FEEDER_SOURCE, id="feeder"),
        ],
    )
    def test_both_paths_get_the_budget_for_the_negotiated_page(
        self,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        source: str,
    ) -> None:
        """A 1200 dpi A4 colour page gets 480 s on the glass and in the feeder."""
        dev = FakeSaneDev(pages=1)
        dev.set_parameters(
            frame_format="color",
            pixels_per_line=_A4_1200_WIDTH,
            lines=_A4_1200_LINES,
            bytes_per_line=_A4_1200_WIDTH * 3,
        )
        budgets = self._spy_on_budgets(monkeypatch)
        settings = ScanSettings(source=source, resolution=1200, mode="Color")

        batch = _backend_with(dev, monkeypatch).scan_pages(
            _TEST_DEVICE, settings, page_sink
        )

        assert len(batch.pages) == 1
        assert budgets
        for budget in budgets:
            assert budget.timeout == pytest.approx(480, rel=0.01)
            assert budget.page == scan_page_description(
                _A4_1200_WIDTH, _A4_1200_LINES, colour=True, dpi=1200
            )

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param("Flatbed", id="flatbed"),
            pytest.param(_FEEDER_SOURCE, id="feeder"),
        ],
    )
    def test_a_timeout_names_the_budget_and_the_page(
        self,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        source: str,
    ) -> None:
        """
        The error says how long the page was given, and for what page.

        The floor is lowered so the read that never finishes times out at
        once; the read still returns after the cancel, as a cooperative
        scanner's does, so only the limit is named, not a stuck read.
        """
        monkeypatch.setattr(page_budget_mod, "_PAGE_TIMEOUT_FLOOR_SECONDS", 0.05)
        dev = FakeSaneDev(pages=1)
        dev.set_parameters(
            frame_format="gray", pixels_per_line=236, lines=295, bytes_per_line=236
        )
        dev.block_read(ReadBlockMode.PARTIAL)
        settings = ScanSettings(source=source, resolution=300, mode="Gray")

        try:
            with pytest.raises(ScanError) as raised:
                _backend_with(dev, monkeypatch).scan_pages(
                    _TEST_DEVICE, settings, page_sink
                )
        finally:
            dev.release_read()
            _join_sane_reader_threads()

        message = str(raised.value)
        assert message.startswith("Page 1 timed out after 0s, the limit for ")
        assert scan_page_description(236, 295, colour=False, dpi=300) in message
        assert "at 300 dpi" in message
        assert "did not answer the cancel" not in message


class TestSourceOptionPresence:
    """
    Whether a device HAS a source option is a different fact from its constraint.

    ``_resolve_source`` records presence independently of whether the constraint
    can be read as a word list, and the assignment in ``_configure_device``
    depends on that flag, so saneless still sets the source on a device whose
    constraint it cannot read.
    """

    def test_a_device_with_no_source_option_is_left_alone(self) -> None:
        """No source option means nothing to assign and nothing to validate."""
        choice = scan_session_mod._resolve_source([], "Flatbed")

        assert choice.has_option is False
        assert choice.effective == "Flatbed"

    @pytest.mark.parametrize(
        "constraint",
        [None, (0.0, 1.0, 1.0), "Flatbed"],
        ids=["unconstrained", "range", "bare-string"],
    )
    def test_a_non_list_source_constraint_is_still_a_source_option(
        self, constraint: object
    ) -> None:
        """
        An unreadable constraint still means a source to assign.

        saneless cannot check the name against a list it cannot read, so it
        hands the requested name over, trimmed, and leaves the device to accept
        or refuse it. Refusing here instead would stop every scan on such a
        device, and not assigning at all would scan from whatever source the
        device happened to be left on.
        """
        choice = scan_session_mod._resolve_source(
            [_option(1, "source", _STRING_OPTION, constraint)], " ADF "
        )

        assert choice.has_option is True
        assert choice.effective == "ADF"
        assert choice.substituted_from is None


class TestAutoSourceFallbackIsAudible:
    """
    Only a flatbed request may become 'Auto', and never silently.

    ``scan_pages`` classifies the *effective* source, so ``Auto`` is routed by
    ``auto_source_mode``, which defaults to "flatbed". A feeder request swapped
    for ``Auto`` would therefore bring a whole stack back as one page, so that
    swap is refused. A flatbed request swapped for ``Auto`` on a device with no
    ``Flatbed`` entry scans the glass as asked, so it is kept and logged.
    """

    def _flatbed_and_auto(self) -> list[tuple]:
        """Build an option table whose source list offers no feeder."""
        return [_option(1, "source", _STRING_OPTION, ["Flatbed", "Auto"])]

    def _auto_and_adf(self) -> list[tuple]:
        """Build an option table whose source list offers no flatbed."""
        return [_option(1, "source", _STRING_OPTION, ["Auto", "ADF"])]

    def test_a_feeder_request_is_refused_rather_than_substituted(self) -> None:
        """'ADF Duplex' on a Flatbed-and-Auto device refuses, naming the sources."""
        with pytest.raises(ScanError) as excinfo:
            scan_session_mod._resolve_source(self._flatbed_and_auto(), "ADF Duplex")

        message = str(excinfo.value)
        assert all(name in message for name in ("'ADF Duplex'", "'Flatbed'", "'Auto'"))

    def test_a_flatbed_request_is_still_substituted(self) -> None:
        """A flatbed request becomes the device's Auto, and the record says so."""
        choice = scan_session_mod._resolve_source(self._auto_and_adf(), "Flatbed")

        assert choice.effective == "Auto"
        assert choice.has_option is True
        assert choice.substituted_from == "Flatbed"

    def test_a_flatbed_listed_under_another_name_is_not_swapped_for_auto(
        self,
    ) -> None:
        """
        A device with its own flatbed gets a refusal naming it, not ``Auto``.

        ``Auto`` stands in only for a flatbed the device does not have. Here
        it has one, under a name the request does not match, so choosing
        ``Auto`` would be a guess.
        """
        raw = [_option(1, "source", _STRING_OPTION, ["Flatbed Scanner", "Auto", "ADF"])]

        with pytest.raises(ScanError) as excinfo:
            scan_session_mod._resolve_source(raw, "Flatbed")

        message = str(excinfo.value)
        assert all(name in message for name in ("'Flatbed'", "'Flatbed Scanner'"))

    def test_the_device_spelling_of_auto_is_the_one_assigned(self) -> None:
        """Auto is recognised by the classifier, so a lowercase 'auto' is used."""
        raw = [_option(1, "source", _STRING_OPTION, ["auto", "ADF"])]

        choice = scan_session_mod._resolve_source(raw, "Flatbed")

        assert choice.effective == "auto"
        assert choice.substituted_from == "Flatbed"

    def test_the_substitution_is_logged_where_routing_is_known(
        self,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A substitution that keeps the glass is an INFO line naming both sources.

        Resolution cannot know whether ``Auto`` will feed, so the line is
        written once routing is decided, and at INFO because nothing is lost.
        """
        dev = FakeSaneDev(pages=1)
        dev.report_sources(["Auto", "ADF"])
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        with caplog.at_level(logging.INFO, logger=_SESSION_LOGGER):
            backend.scan_pages(_TEST_DEVICE, settings, page_sink)

        assert [
            record
            for record in caplog.records
            if record.name == _SESSION_LOGGER
            and record.levelno == logging.INFO
            and "'Flatbed'" in record.getMessage()
            and "'Auto'" in record.getMessage()
        ]


class TestSourceMatching:
    """
    A profile's source is matched as an operator means it, or refused.

    Case and surrounding whitespace are ignored and the device's own spelling
    is assigned. Nothing is matched by prefix, two entries that differ only in
    case are never guessed between, and a name the device does not offer is
    refused before anything is started -- except a flatbed request, which may
    fall back to the device's Auto source.
    """

    def _scan(
        self,
        sources: list[str],
        settings: ScanSettings,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
    ) -> tuple[FakeSaneDev, ScanBatch]:
        """
        Scan once from a device offering ``sources``.

        Args:
            sources: The source names the device reports.
            settings: The scan settings to run.
            monkeypatch: Fixture used to wire the device into the backend.
            page_sink: Where the pages go.

        Returns:
            The device, for inspection, and the batch the scan returned.

        """
        dev = FakeSaneDev()
        dev.report_sources(sources)
        batch = _backend_with(dev, monkeypatch).scan_pages(
            _TEST_DEVICE, settings, page_sink
        )
        return dev, batch

    def _refused(
        self,
        sources: list[str],
        requested: str,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
    ) -> tuple[FakeSaneDev, str]:
        """
        Scan from a device that must refuse ``requested``.

        ``auto_source_mode`` is "adf", so an ``Auto`` substituted in its place
        would feed: a refusal cannot be mistaken for a harmless platen snapshot.

        Args:
            sources: The source names the device reports.
            requested: The profile's source.
            monkeypatch: Fixture used to wire the device into the backend.
            page_sink: Where the pages would go.

        Returns:
            The device, for inspection, and the refusal's message.

        """
        dev = FakeSaneDev()
        dev.report_sources(sources)
        settings = ScanSettings(
            source=requested, resolution=300, mode="Color", auto_source_mode="adf"
        )
        with pytest.raises(ScanError) as excinfo:
            _backend_with(dev, monkeypatch).scan_pages(
                _TEST_DEVICE, settings, page_sink
            )
        return dev, str(excinfo.value)

    def test_a_lowercase_feeder_name_scans_the_device_feeder(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """'adf' selects the device's 'ADF' and drains the stack, not Auto."""
        settings = ScanSettings(source="adf", resolution=300, mode="Color")

        dev, batch = self._scan(
            ["Auto", "Flatbed", "ADF"], settings, monkeypatch, page_sink
        )

        assert dev.source == "ADF"
        assert len(batch.pages) == 3
        assert dev.calls == _THREE_SHEET_FEEDER_CALLS
        assert batch.substituted_source is None

    def test_surrounding_whitespace_and_case_are_ignored(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        '  flatbed ' selects 'Flatbed'.

        The device's spelling is what is assigned: libsane strips nothing, so
        handing it the padded name would be refused.
        """
        settings = ScanSettings(source="  flatbed ", resolution=300, mode="Color")

        dev, batch = self._scan(["Flatbed", "ADF"], settings, monkeypatch, page_sink)

        assert dev.source == "Flatbed"
        assert len(batch.pages) == 1
        assert dev.calls == ["start", "snap"]

    def test_the_device_spelling_is_the_value_assigned(self) -> None:
        """The resolved name is the device's entry, not the request."""
        raw = _options_reporting(["Auto", "Flatbed", "ADF"])

        choice = scan_session_mod._resolve_source(raw, " adf")

        assert choice.effective == "ADF"
        assert choice.substituted_from is None

    @pytest.mark.parametrize(
        ("requested", "sources"),
        [
            ("Nope", ["Auto", "Flatbed", "ADF"]),
            ("ADF Duplex", ["Flatbed", "Auto"]),
            ("Tray 2", ["Flatbed", "Auto"]),
            ("Flatbed", ["ADF"]),
        ],
        ids=["unknown-name", "duplex-feeder", "unclassified", "flatbed-without-auto"],
    )
    def test_an_unmatched_source_is_refused_before_any_page(
        self,
        requested: str,
        sources: list[str],
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
    ) -> None:
        """Nothing is started, the source is never assigned, and both are named."""
        dev, message = self._refused(sources, requested, monkeypatch, page_sink)

        assert dev.calls == []
        assert "source" not in dev.assignments
        assert repr(requested) in message
        assert all(repr(name) in message for name in sources)

    def test_two_entries_differing_only_in_case_are_not_guessed_between(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """'Adf' against 'ADF' and 'adf' refuses, naming both."""
        dev, message = self._refused(
            ["Auto", "ADF", "adf"], "Adf", monkeypatch, page_sink
        )

        assert dev.calls == []
        assert "'Adf'" in message
        assert "'ADF'" in message
        assert "'adf'" in message

    def test_an_exact_entry_wins_over_its_case_twin(self) -> None:
        """'adf' against 'ADF' and 'adf' is not ambiguous: it is listed exactly."""
        raw = _options_reporting(["Auto", "ADF", "adf"])

        choice = scan_session_mod._resolve_source(raw, "adf")

        assert choice.effective == "adf"

    @pytest.mark.parametrize(
        "sources", [["ADF"], ["Auto", "ADF"]], ids=["feeder-only", "with-auto"]
    )
    def test_a_prefix_is_not_a_match(
        self,
        sources: list[str],
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        'AD' is refused, although libsane itself would take it as 'ADF'.

        A prefix is a guess: 'ADF' is also a prefix of 'ADF Duplex'.
        """
        dev, message = self._refused(sources, "AD", monkeypatch, page_sink)

        assert dev.calls == []
        assert "'AD'" in message

    def test_a_flatbed_request_on_auto_keeps_the_glass_quietly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        Flatbed becomes Auto, scans one page, and leaves nothing on the batch.

        This is the built-in default profile on a scanner that lists only Auto
        and ADF: the glass is scanned as asked, so it is an INFO line and not
        a warning.
        """
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", auto_source_mode="flatbed"
        )

        with caplog.at_level(logging.INFO, logger=_SESSION_LOGGER):
            dev, batch = self._scan(["Auto", "ADF"], settings, monkeypatch, page_sink)

        assert dev.source == "Auto"
        assert len(batch.pages) == 1
        assert batch.substituted_source is None
        backend_records = [r for r in caplog.records if r.name in _SCANNER_LOGGERS]
        assert [
            r
            for r in backend_records
            if r.levelno == logging.INFO
            and "'Flatbed'" in r.getMessage()
            and "'Auto'" in r.getMessage()
        ]
        assert not [
            r
            for r in backend_records
            if r.levelno >= logging.WARNING and "Flatbed" in r.getMessage()
        ]

    def test_a_flatbed_request_routed_to_the_feeder_is_recorded(
        self,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        Flatbed becomes Auto, auto_source_mode sends it to the feeder: a fact.

        The operator asked for the glass and got a stack, so the batch names
        the requested source and the log warns.
        """
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", auto_source_mode="adf"
        )

        with caplog.at_level(logging.INFO, logger=_SESSION_LOGGER):
            dev, batch = self._scan(["Auto", "ADF"], settings, monkeypatch, page_sink)

        assert dev.source == "Auto"
        assert len(batch.pages) == 3
        assert dev.calls == _THREE_SHEET_FEEDER_CALLS
        assert batch.substituted_source == "Flatbed"
        assert [m for m in _warning_messages(caplog) if "'Flatbed'" in m]

    def test_an_unreadable_constraint_assigns_the_trimmed_request(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        A source option whose constraint is not a list is given ' ADF ' trimmed.

        The device validates it; saneless routes on it, so the stack is fed.
        """
        table = [
            (*option[:8], None) if option[1] == "source" else option
            for option in build_option_table()
        ]
        dev = FakeSaneDev(options=table)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source=" ADF ", resolution=300, mode="Color")

        batch = backend.scan_pages(_TEST_DEVICE, settings, page_sink)

        assert dev.source == "ADF"
        assert len(batch.pages) == 3
        assert dev.calls == _THREE_SHEET_FEEDER_CALLS

    def test_manual_duplex_matches_the_named_feeder_the_same_way(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """'adf' picks the device's 'ADF', not merely its first feeder."""
        settings = ScanSettings(
            source="adf", resolution=300, mode="Color", duplex="manual"
        )

        dev, batch = self._scan(
            ["Flatbed", "Automatic Document Feeder", "ADF"],
            settings,
            monkeypatch,
            page_sink,
        )

        assert dev.source == "ADF"
        assert len(batch.pages) == 3


class TestScannerInterfaceShape:
    """The backend-agnostic value objects are keyword-only, and settings frozen."""

    def test_scan_settings_cannot_be_changed_after_construction(self) -> None:
        """Assigning a field raises, so settings cannot drift mid-scan."""
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        # Through setattr with the name in a variable, because a plain
        # ``settings.source = "ADF"`` is a static error both type checkers
        # report once the class is frozen, and the point is the runtime refusal.
        attribute = "source"

        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(settings, attribute, "ADF")

        assert settings.source == "Flatbed"

    def test_scan_settings_refuses_positional_construction(self) -> None:
        """Every field is named at the call site."""
        construct = cast("Callable[..., object]", ScanSettings)

        with pytest.raises(TypeError):
            construct("Flatbed", 300, "Color")

    def test_device_capabilities_takes_option_names(self) -> None:
        """The option names are a tuple of strings, named at the call site."""
        caps = DeviceCapabilities(
            sources=["Flatbed"], resolutions=[], modes=[], option_names=("source",)
        )

        assert caps.option_names == ("source",)
        assert (
            DeviceCapabilities(sources=[], resolutions=[], modes=[]).option_names == ()
        )

    def test_device_capabilities_refuses_positional_construction(self) -> None:
        """Capabilities are built by keyword, so field order is nobody's dependency."""
        construct = cast("Callable[..., object]", DeviceCapabilities)

        with pytest.raises(TypeError):
            construct(["Flatbed"], [], [])


class TestResolutionReadBack:
    """The resolution the device actually chose is read back."""

    @staticmethod
    def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
        """Return the WARNING messages captured so far."""
        return [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]

    def test_a_substituted_resolution_warns_naming_both_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """Requesting 5000 on a 1200 dpi device names both numbers."""
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=5000, mode="Color")

        with caplog.at_level(logging.WARNING, logger=_SESSION_LOGGER):
            backend.scan_pages("test:0", settings, page_sink)

        assert [m for m in self._warnings(caplog) if "5000" in m and "1200" in m]

    def test_the_read_back_value_is_an_int_not_the_device_float(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """The device returns 1200.0; what the backend carries is 1200."""
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=5000, mode="Color")

        with caplog.at_level(logging.WARNING, logger=_SESSION_LOGGER):
            backend.scan_pages("test:0", settings, page_sink)

        warnings = self._warnings(caplog)
        assert [m for m in warnings if "1200" in m]
        assert not [m for m in warnings if "1200.0" in m]
        assert isinstance(dev.resolution, float)

    def test_an_honoured_resolution_logs_no_mismatch_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """A resolution the device accepts unchanged is not worth a warning."""
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        with caplog.at_level(logging.WARNING, logger=_SESSION_LOGGER):
            backend.scan_pages("test:0", settings, page_sink)

        assert not [m for m in self._warnings(caplog) if "resolution" in m.lower()]


class TestScanBatch:
    """
    One batch object carries what the device actually did out of the backend.

    The resolution the device settled on and how many fed sheets it could not
    read travel on the ``ScanBatch`` that ``scan_pages`` returns, since a
    generator's return value is discarded by ``list()``.
    """

    def test_the_batch_carries_exactly_five_fields(self) -> None:
        """
        Five fields, in order, and the per-page detail lives in ``pages``.

        Anything measured about one page belongs on its ``PageRecord``.  The
        last two fields are about the pass as a whole -- the
        source the device's Auto stood in for when it fed, and the cap the
        pass reached -- and so are not a second channel for a per-page fact.
        """
        assert [field.name for field in dataclasses.fields(ScanBatch)] == [
            "pages",
            "actual_resolution",
            "pages_rejected",
            "substituted_source",
            "cap_reached",
        ]

    def test_the_substitution_defaults_to_none(self) -> None:
        """A batch built without the substitution or cap facts reports neither."""
        batch = ScanBatch(pages=(), actual_resolution=300, pages_rejected=0)

        assert batch.substituted_source is None
        assert batch.cap_reached is None

    def test_the_cap_fact_is_frozen(self) -> None:
        """What the pass reached is a report, and cannot be rewritten."""
        fact = PassCapReached(cap=50, sheet_not_kept=51, auto_source=True)
        attribute = "sheet_not_kept"

        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(fact, attribute, 52)

        assert fact.sheet_not_kept == 51

    def test_the_stub_helpers_pass_the_cap_fact_through(
        self, page_sink: SpooledPageSink
    ) -> None:
        """``scan_batch`` and ``spooling`` carry the fact a test hands them."""
        fact = PassCapReached(cap=500, sheet_not_kept=501, auto_source=False)
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")

        built = scan_batch([], cap_reached=fact)
        spooled = spooling([_make_content_image()], cap_reached=fact)(
            _TEST_DEVICE, settings, page_sink
        )

        assert built.cap_reached == fact
        assert spooled.cap_reached == fact
        assert len(spooled.pages) == 1
        assert scan_batch([]).cap_reached is None

    def test_the_batch_is_not_an_iterator(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """What comes back is a record, not something to call ``list()`` on."""
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        batch = backend.scan_pages("test:0", settings, page_sink)

        assert isinstance(batch, ScanBatch)
        assert not hasattr(batch, "__next__")

    def test_a_clean_five_page_stack_rejects_nothing(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """Five readable sheets are five pages and a zero rejection count."""
        mock_dev = fake_sane_module.device
        mock_dev.load_feeder([_make_content_image() for _ in range(5)])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        batch = backend.scan_pages("test:device:001", settings, page_sink)

        assert len(batch.pages) == 5
        assert batch.pages_rejected == 0

    def test_an_unreadable_sheet_is_counted_rather_than_vanishing(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """
        Five sheets with one corrupt page return four pages and a count of one.

        This is the count's whole purpose.  The pipeline's ``pages_scanned`` is
        ``len(images)``, which already excludes a skipped sheet, so a stack with
        one unreadable page reports one fewer and nobody learns a page was lost.
        """
        pages = [_make_content_image() for _ in range(5)]
        # Far below _MIN_PAGE_BYTES: a 10x10 RGB page is 300 bytes.
        pages[2] = Image.new("RGB", (10, 10), "white")
        mock_dev = fake_sane_module.device
        mock_dev.load_feeder(pages)

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        batch = backend.scan_pages("test:device:001", settings, page_sink)

        assert len(batch.pages) == 4
        assert batch.pages_rejected == 1

    def test_the_batch_reports_the_resolution_the_device_chose(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """A device clamping 5000 to 1200 reports 1200, as a whole number."""
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=5000, mode="Color")

        batch = backend.scan_pages("test:0", settings, page_sink)

        assert batch.actual_resolution == 1200
        assert isinstance(batch.actual_resolution, int)
        # The page itself is recorded at the read-back value, not the 5000
        # asked for: it is what every PDF lays the page out at.
        assert [record.dpi for record in batch.pages] == [1200]

    def test_every_fed_page_is_recorded_at_the_resolution_the_device_chose(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """The feeder path hands the sink the read-back dpi for every sheet."""
        mock_dev = fake_sane_module.device
        mock_dev.load_feeder([_make_content_image() for _ in range(3)])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=5000, mode="Color")
        batch = backend.scan_pages("test:device:001", settings, page_sink)

        assert batch.actual_resolution == 1200
        assert [record.dpi for record in batch.pages] == [1200, 1200, 1200]

    def test_a_flatbed_scan_carries_both_facts_too(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """The single-page path populates the same two facts as the feeder."""
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        batch = backend.scan_pages("test:0", settings, page_sink)

        assert len(batch.pages) == 1
        assert batch.actual_resolution == 300
        assert batch.pages_rejected == 0

    def test_the_device_is_closed_by_the_time_the_batch_returns(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        Acquisition is eager, so the handle is released on return.

        Nothing holds the device open until the result is drained or
        garbage-collected.
        """
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        batch = backend.scan_pages("test:0", settings, page_sink)

        assert dev.close_calls == 1
        assert len(batch.pages) == 1


# ---------------------------------------------------------------------------
# The peak-memory bound, and the one-page-per-sink-call rule
# ---------------------------------------------------------------------------


class _RecordingSink(PageSink):
    """
    A sink that records every ``add`` and delegates to a real one.

    Subclasses ``PageSink`` rather than being one of ``unittest.mock``'s
    auto-specced doubles, for the reason ``tests/conftest.py``'s own stubs
    record: a mock returns whatever it is given, so a contract change slips
    past it, while a subclass is a type error in both checkers the moment
    ``add`` stops matching.  The delegate is a real ``SpooledPageSink``, so the
    pages this test asserts about were genuinely written and can be read back.
    """

    def __init__(self, delegate: SpooledPageSink) -> None:
        """
        Wrap a real sink.

        Args:
            delegate: The sink that actually writes and measures each page.

        """
        self._delegate = delegate
        self.added: list[Image.Image] = []
        self.returned: list[PageRecord] = []

    def add(self, image: Image.Image, *, dpi: int) -> PageRecord:
        """
        Record the page, pass it to the delegate, and record what came back.

        Args:
            image: The one page the backend handed over.
            dpi: The resolution the backend read back, passed on unchanged.

        Returns:
            The delegate's record, unchanged.

        """
        self.added.append(image)
        record = self._delegate.add(image, dpi=dpi)
        self.returned.append(record)
        return record


def _feeder_of(pages: int, monkeypatch: pytest.MonkeyPatch) -> FakeSaneDev:
    """
    Build a fresh feeder of ``pages`` sheets and wire it into the backend.

    Args:
        pages: How many sheets the feeder holds.
        monkeypatch: Fixture used to patch the module-level ``sane`` name.

    Returns:
        The device the next ``SaneBackend()`` will open.

    """
    dev = FakeSaneDev(pages=pages)
    _backend_with(dev, monkeypatch)
    return dev


class TestPeakPageMemory:
    """
    A scan holds at most two page images at once, however long the stack.

    Pages are counted live with a ``weakref`` each: ``tracemalloc`` cannot see
    Pillow's C-allocated pixels, and RSS is too noisy.  The bound is 2, not 1,
    because ``_acquire_pages``' loop variable still references page *k-1*
    while page *k* is read.  Equal peaks for a 3-page and a 12-page run prove
    independence from page count, which ``<= 2`` alone would not.  The fake
    generates its own distinct pages: ``load_feeder()`` keeps a strong
    reference to every page it was given, so the counter would report the
    test's retention instead of the backend's.
    """

    def test_live_page_images_stays_bounded(
        self,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
        second_pass_sink: SpooledPageSink,
    ) -> None:
        """A 12-page scan holds at most 2 pages at once, and 3 holds the same."""
        settings = _feeder_settings()

        long_run = _feeder_of(12, monkeypatch)
        batch = SaneBackend().scan_pages("test:0", settings, page_sink)

        assert len(batch.pages) == 12
        assert long_run.high_water_live_pages <= 2

        short_run = _feeder_of(3, monkeypatch)
        short_batch = SaneBackend().scan_pages("test:0", settings, second_pass_sink)

        assert len(short_batch.pages) == 3
        # The bound does not grow with the stack: four times the pages, the
        # same peak.  This is the assertion that fails if anything downstream
        # starts accumulating.
        assert long_run.high_water_live_pages == short_run.high_water_live_pages

    def test_live_page_images_falls_as_pages_are_released(self) -> None:
        """The counter tracks releases, so a steady reading means retention."""
        dev = FakeSaneDev(pages=3)
        issued = list(dev.multi_scan())

        assert len(issued) == 3
        assert dev.live_page_images() == 3
        assert dev.high_water_live_pages == 3

        issued.clear()
        gc.collect()

        assert dev.live_page_images() == 0
        # The high-water mark is a maximum, so it never falls back.
        assert dev.high_water_live_pages == 3

    def test_sink_receives_one_image_per_page(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``add`` runs once per page, with one image, and its records are the batch."""
        dev = _feeder_of(12, monkeypatch)
        recording = _RecordingSink(_page_sink_for(tmp_path))

        batch = SaneBackend().scan_pages("test:0", _feeder_settings(), recording)

        assert len(recording.added) == 12
        assert dev.calls.count("snap") == 12
        assert all(isinstance(page, Image.Image) for page in recording.added)
        # The batch is exactly what the sink handed back, in the order it did:
        # the backend keeps no pages of its own to assemble a different answer
        # from.
        assert batch.pages == tuple(recording.returned)

    def test_a_twelve_page_scan_numbers_its_records_one_to_twelve(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """Sequences run 1..12 with no gap and no repeat, and pages are distinct."""
        _feeder_of(12, monkeypatch)

        batch = SaneBackend().scan_pages("test:0", _feeder_settings(), page_sink)

        # Compared as a whole list rather than page by page, so a failure
        # prints the sequence that was actually produced.
        assert [record.sequence for record in batch.pages] == list(range(1, 13))
        spooled = [image.tobytes() for image in images_of(batch)]
        assert len(set(spooled)) == 12


# ---------------------------------------------------------------------------
# SANE module boundary
# ---------------------------------------------------------------------------

_LIBSANE_MISSING = (
    "libsane.so.1: cannot open shared object file: No such file or directory"
)


def _flatbed_settings() -> ScanSettings:
    """
    Build settings that take the flatbed start()/snap() path.

    Returns:
        Flatbed settings the shared fake accepts.

    """
    return ScanSettings(source="Flatbed", resolution=300, mode="Color")


class _AssignmentFailure(NamedTuple):
    """
    One refused option assignment, and the message it has to produce.

    A named record rather than four parametrize columns, for the reason
    ``DuplexMismatch`` gives in ``saneless.duplex``: the four facts belong
    together, and a four-column table makes every reader remember an order.
    Bundled, they also keep the test within ruff's ``PLR0913`` argument limit.

    Attributes:
        option: The underscore-spelled option whose assignment is armed to
            fail.
        settings: The settings that provoke that assignment.
        error: The exception the device raises from it.
        expected: The exact ``ScanError`` message the backend must produce.

    """

    option: str
    settings: ScanSettings
    error: BaseException
    expected: str


class TestSaneBoundary:
    """
    Every python-sane failure leaves the backend as a saneless type.

    python-sane raises ``_sane.error``, ``RuntimeError`` and ``AttributeError``
    with no shared base.  Each call site in ``scan_pages``, ``get_capabilities``
    and ``get_devices`` re-raises as ``ScanError`` naming the device, and the
    option where there is one, with the original message and ``__cause__``
    kept, so the CLI prints no traceback and the web layer does not classify
    the job as UNKNOWN.  A missing python-sane is a setup problem, so
    ``require_sane()`` raises ``ConfigError`` with an install hint instead.
    """

    # -- require_sane -------------------------------------------------------

    def test_require_sane_missing_module_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing python-sane is a one-line ConfigError with the install hint."""
        monkeypatch.setattr(scan_session_mod, "sane", None)
        monkeypatch.setitem(sys.modules, "sane", None)

        with pytest.raises(ConfigError) as exc_info:
            sane_backend_mod.require_sane()

        message = str(exc_info.value)
        assert "python-sane" in message
        assert "import of sane halted" in message
        assert "libsane-dev" in message
        assert "sane-backends-devel" in message
        assert "Install on Bare Metal" in message
        assert "\n" not in message
        assert isinstance(exc_info.value.__cause__, ModuleNotFoundError)

    def test_require_sane_missing_shared_library_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain ImportError (libsane.so missing) is translated the same way."""
        original = ImportError(_LIBSANE_MISSING)

        def _fail() -> None:
            raise original

        monkeypatch.setattr(sane_backend_mod, "_ensure_sane", _fail)

        with pytest.raises(ConfigError, match=r"libsane\.so\.1") as exc_info:
            sane_backend_mod.require_sane()

        assert exc_info.value.__cause__ is original

    def test_require_sane_next_step_says_install_not_reconfigure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The next step is the install the message names, not a config fix.

        A missing python-sane or libsane is filed under the configuration
        category, whose general advice cannot know the fix.  This raise site
        does, so it carries it: nothing in the config file or a restart
        brings the library back.
        """

        def _fail() -> None:
            raise ImportError(_LIBSANE_MISSING)

        monkeypatch.setattr(sane_backend_mod, "_ensure_sane", _fail)

        with pytest.raises(ConfigError) as exc_info:
            sane_backend_mod.require_sane()

        next_step = exc_info.value.next_step
        assert next_step is not None
        assert "install" in next_step.lower()
        assert "configuration file" not in next_step
        assert "config file" not in next_step
        assert "restart" not in next_step.lower()

    def test_require_sane_keeps_an_already_loaded_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With sane already bound, require_sane() returns and replaces nothing."""
        module = FakeSaneModule()
        monkeypatch.setattr(scan_session_mod, "sane", module)

        assert sane_backend_mod.require_sane() is None
        assert scan_session_mod.sane is module

    def test_ensure_sane_returns_the_patched_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A module patched into ``sane`` is what every SANE call goes through."""
        module = FakeSaneModule()
        monkeypatch.setattr(scan_session_mod, "sane", module)

        assert scan_session_mod._ensure_sane() is module

    def test_ensure_sane_imports_on_every_call_without_binding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        With nothing patched in, each call imports afresh and binds no name.

        Not caching is load-bearing: a test that evicts python-sane from
        ``sys.modules`` after another test imported it must see the eviction,
        and a remembered module would hide it.
        """
        first = FakeSaneModule()
        second = FakeSaneModule()
        monkeypatch.setattr(scan_session_mod, "sane", None)
        monkeypatch.setitem(sys.modules, "sane", first)

        assert scan_session_mod._ensure_sane() is first
        assert scan_session_mod.sane is None

        monkeypatch.setitem(sys.modules, "sane", second)

        assert scan_session_mod._ensure_sane() is second

    def test_scanner_package_does_not_offer_the_backend(self) -> None:
        """
        The package exports nothing, and never the SANE backend.

        Callers import ``saneless.scanner.base`` and
        ``saneless.scanner.sane_backend`` by name, so the package itself never
        has a reason to reach python-sane.
        """
        assert "SaneBackend" not in scanner_pkg.__all__
        assert not hasattr(scanner_pkg, "SaneBackend")

    def test_require_sane_is_not_run_at_import(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Importing the backend does not import sane, so ``--help`` stays sane-free.

        The backend is imported afresh with python-sane evicted from
        ``sys.modules``, because another test has very likely imported it
        already.  monkeypatch restores both entries, and the package attribute,
        afterwards, so the rest of the session keeps the original module.
        """
        monkeypatch.delitem(sys.modules, "sane", raising=False)
        monkeypatch.delitem(sys.modules, "_sane", raising=False)
        monkeypatch.delitem(sys.modules, sane_backend_mod.__name__)
        monkeypatch.delitem(sys.modules, scan_session_mod.__name__)
        monkeypatch.setattr(scanner_pkg, "sane_backend", sane_backend_mod)
        monkeypatch.setattr(scanner_pkg, "scan_session", scan_session_mod)

        fresh = importlib.import_module(sane_backend_mod.__name__)
        fresh_session = sys.modules[scan_session_mod.__name__]

        assert fresh is not sane_backend_mod
        assert fresh_session is not scan_session_mod
        assert fresh_session.sane is None
        assert "sane" not in sys.modules

    # -- init / open / get_devices / close ----------------------------------

    def test_init_failure_raises_scan_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing sane.init() is a ScanError chained to the SANE error."""
        original = FakeSaneError("Access to resource has been denied")
        monkeypatch.setattr(
            scan_session_mod, "sane", FakeSaneModule(init_error=original)
        )

        with pytest.raises(
            ScanError,
            match=r"^Could not initialise SANE: Access to resource has been denied$",
        ) as exc_info:
            SaneBackend()

        assert exc_info.value.__cause__ is original

    def test_open_failure_in_get_capabilities_raises_scan_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        sane.open() failing names the device and keeps SANE's message.

        The open runs in a listing child, so only the failure's text comes
        back: there is no exception in this process to chain to.
        """
        original = FakeSaneError("Invalid argument")
        monkeypatch.setattr(
            scan_session_mod, "sane", FakeSaneModule(open_error=original)
        )
        backend = SaneBackend()

        with pytest.raises(ScanError) as exc_info:
            backend.get_capabilities("epson2:libusb:001:004")

        assert (
            str(exc_info.value)
            == "Could not open scanner epson2:libusb:001:004: Invalid argument"
        )

    def test_open_failure_in_scan_pages_raises_scan_error(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """The same translation applies on the scan path."""
        original = FakeSaneError("Invalid argument")
        monkeypatch.setattr(
            scan_session_mod, "sane", FakeSaneModule(open_error=original)
        )
        backend = SaneBackend()

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("epson2:libusb:001:004", _flatbed_settings(), page_sink)

        assert (
            str(exc_info.value)
            == "Could not open scanner epson2:libusb:001:004: Invalid argument"
        )
        assert exc_info.value.__cause__ is original

    def test_open_failure_with_empty_message_names_the_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty SANE message falls back to the exception's class name."""
        monkeypatch.setattr(
            scan_session_mod,
            "sane",
            FakeSaneModule(open_error=FakeSaneError("")),
        )
        backend = SaneBackend()

        with pytest.raises(ScanError) as exc_info:
            backend.get_capabilities("test:0")

        assert str(exc_info.value) == "Could not open scanner test:0: FakeSaneError"

    def test_get_devices_failure_raises_scan_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        sane.get_devices() failing is a ScanError carrying the original's text.

        The listing runs in a child process, so the original exception object
        stays there: only its type name and message come back, and there is
        nothing in this process to chain to.
        """
        original = FakeSaneError("Out of memory")
        monkeypatch.setattr(
            scan_session_mod, "sane", FakeSaneModule(get_devices_error=original)
        )
        backend = SaneBackend()

        with pytest.raises(ScanError) as exc_info:
            backend.get_devices()

        assert str(exc_info.value) == "Could not list scanners: Out of memory"
        assert exc_info.value.__cause__ is None

    def test_close_failure_does_not_mask_the_scan_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """The scan's own error propagates; the close failure is only logged."""
        dev = FakeSaneDev()
        dev.fail_call("close", FakeSaneError("close failed"))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="NonExistentSource", resolution=300, mode="Color"
        )

        with (
            caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"),
            pytest.raises(ScanError, match="NonExistentSource"),
        ):
            backend.scan_pages("test:0", settings, page_sink)

        assert dev.close_calls == 1
        records = [
            r
            for r in caplog.records
            if r.name == "saneless.scanner.sane_backend"
            and r.levelno == logging.WARNING
            and "test:0" in r.getMessage()
        ]
        assert len(records) == 1
        assert records[0].exc_info is not None

    def test_close_failure_after_a_good_scan_returns_the_batch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """A close failure after a successful scan is logged and the result kept."""
        dev = FakeSaneDev()
        dev.fail_call("close", FakeSaneError("close failed"))
        backend = _backend_with(dev, monkeypatch)

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
            batch = backend.scan_pages("test:0", _flatbed_settings(), page_sink)

        assert len(batch.pages) == 1
        records = [
            r
            for r in caplog.records
            if r.name == "saneless.scanner.sane_backend"
            and "Could not close scanner test:0" in r.getMessage()
        ]
        assert len(records) == 1
        assert records[0].exc_info is not None

    def test_the_check_open_logs_no_device_id_and_no_exception_text(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        The Scanner check's open of an unlisted device names nothing in the log.

        A ``net:`` device id is a LAN address, and the exception a failed
        close raises is free text that can repeat it.  The check's open runs
        in the listing child, which swallows a failed close, since the open
        already showed the device can be reached, so the backend logs nothing
        about it -- unlike a scan's close failure, whose log names the device
        and carries the traceback.  The check's own enumeration is what is
        run here, over a real ``SaneBackend`` whose listing the suite's seam
        serves in this process, so every log line the check can cause through
        the backend and the child's logic is captured.
        """
        device_id = "net:scanbox.lan:brother5:bus0;dev1"
        dev = FakeSaneDev()
        dev.fail_call("close", FakeSaneError(f"close failed on {device_id}"))
        backend = _backend_with(dev, monkeypatch)

        with caplog.at_level(logging.DEBUG):
            enumeration = checks._scanner_enumeration(
                backend, device_id, may_open=True, abort=None
            )

        assert enumeration.configured_opened is True
        assert dev.close_calls == 1
        backend_warnings = [
            r
            for r in caplog.records
            if r.name in _SCANNER_LOGGERS and r.levelno >= logging.WARNING
        ]
        assert backend_warnings == []
        for record in caplog.records:
            message = record.getMessage()
            assert "scanbox" not in message
            assert "net:" not in message
            assert "close failed" not in message
            assert record.exc_info is None

    # -- option assignment and read-back ------------------------------------

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(
                _AssignmentFailure(
                    option="source",
                    settings=ScanSettings(
                        source="Flatbed", resolution=300, mode="Color"
                    ),
                    error=FakeSaneError("Invalid argument"),
                    expected=(
                        "Could not set source to 'Flatbed' on test:0: Invalid argument"
                    ),
                ),
                id="source-invalid-argument",
            ),
            pytest.param(
                _AssignmentFailure(
                    option="mode",
                    settings=ScanSettings(
                        source="Flatbed", resolution=300, mode="Lineart"
                    ),
                    error=FakeSaneError("Invalid argument"),
                    expected=(
                        "Could not set mode to 'Lineart' on test:0: Invalid argument"
                    ),
                ),
                id="mode-invalid-argument",
            ),
            pytest.param(
                _AssignmentFailure(
                    option="mode",
                    settings=ScanSettings(
                        source="Flatbed", resolution=300, mode="Color"
                    ),
                    error=AttributeError("Inactive option: mode"),
                    expected=(
                        "Could not set mode to 'Color' on test:0: Inactive option: mode"
                    ),
                ),
                id="mode-inactive-option",
            ),
            pytest.param(
                _AssignmentFailure(
                    option="resolution",
                    settings=ScanSettings(
                        source="Flatbed", resolution=300, mode="Color"
                    ),
                    error=FakeSaneError("Invalid argument"),
                    expected=(
                        "Could not set resolution to 300 on test:0: Invalid argument"
                    ),
                ),
                id="resolution-invalid-argument",
            ),
        ],
    )
    def test_option_assignment_failure_names_option_and_value(
        self,
        case: _AssignmentFailure,
        monkeypatch: pytest.MonkeyPatch,
        page_sink: SpooledPageSink,
    ) -> None:
        """A refused assignment names the option, the value and the device."""
        dev = FakeSaneDev()
        dev.fail_assignment(case.option, case.error)
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", case.settings, page_sink)

        assert str(exc_info.value) == case.expected
        assert exc_info.value.__cause__ is case.error
        assert dev.cancel_calls
        assert dev.close_calls

    def test_resolution_read_back_failure_raises_scan_error(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """Reading the resolution back after setting it is wrapped too."""
        original = AttributeError("Inactive option: resolution")
        dev = FakeSaneDev()
        dev.fail_read("resolution", original)
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", _flatbed_settings(), page_sink)

        assert str(exc_info.value) == (
            "Could not read back resolution from test:0: Inactive option: resolution"
        )
        assert exc_info.value.__cause__ is original

    # -- get_options ---------------------------------------------------------

    def test_get_options_failure_in_scan_pages_raises_scan_error(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """get_options() failing on the scan path names the device."""
        original = FakeSaneError("Error during device I/O")
        dev = FakeSaneDev()
        dev.fail_call("get_options", original)
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", _flatbed_settings(), page_sink)

        assert str(exc_info.value) == (
            "Could not read options from test:0: Error during device I/O"
        )
        assert exc_info.value.__cause__ is original
        assert dev.close_calls

    def test_get_options_failure_in_get_capabilities_raises_scan_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        get_options() failing while reading capabilities names the device.

        The read runs in a listing child, so only the failure's text comes
        back, with nothing in this process to chain to.
        """
        original = FakeSaneError("Error during device I/O")
        dev = FakeSaneDev()
        dev.fail_call("get_options", original)
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.get_capabilities("test:0")

        assert str(exc_info.value) == (
            "Could not read options from test:0: Error during device I/O"
        )

    # -- flatbed start / snap ------------------------------------------------

    def test_flatbed_start_failure_raises_scan_error(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """A flatbed start() failure names the device and chains the original."""
        original = FakeSaneError("Scanner cover is open")
        dev = FakeSaneDev(start_error=original)
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", _flatbed_settings(), page_sink)

        assert str(exc_info.value) == "Scanner error on test:0: Scanner cover is open"
        assert exc_info.value.__cause__ is original
        assert not isinstance(exc_info.value, FeederEmptyError)

    def test_flatbed_snap_failure_raises_scan_error(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """snap()'s RuntimeError for an empty read is a ScanError with cause."""
        original = RuntimeError("Scanner returned no data")
        dev = FakeSaneDev()
        dev.fail_call("snap", original)
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", _flatbed_settings(), page_sink)

        assert "Scanner returned no data" in str(exc_info.value)
        assert exc_info.value.__cause__ is original
        assert dev.close_calls

    def test_flatbed_out_of_documents_is_feeder_empty(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """The one end-of-feed message maps to FeederEmptyError."""
        original = FakeSaneError(_OUT_OF_DOCUMENTS)
        dev = FakeSaneDev(start_error=original)
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(FeederEmptyError) as exc_info:
            backend.scan_pages("test:0", _flatbed_settings(), page_sink)

        assert str(exc_info.value) == "No paper detected in feeder"
        assert exc_info.value.__cause__ is original

    def test_adf_empty_message_fault_names_the_type(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """A mid-batch fault with no text still says what went wrong."""
        dev = FakeSaneDev(pages=5, start_error=FakeSaneError(""), start_error_page=1)
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", _feeder_settings(), page_sink)

        assert str(exc_info.value) == "Scanner error on page 2: FakeSaneError"


# A device name as LAN discovery could report it: an OSC that retitles the
# terminal window, then a CSI that turns the text red.
_HOSTILE_DEVICE = "net:evil\x1b]0;owned\x07\x1b[31m:0"
_SHOWN_DEVICE = "net:evil\\x1b]0;owned\\x07\\x1b[31m:0"


class TestDeviceIdsInScanErrorsAreNeutralised:
    """A device id from discovery reaches every ScanError message defused."""

    @staticmethod
    def _assert_defused(exc: ScanError) -> None:
        """Assert the message names the device with its controls escaped."""
        message = str(exc)
        assert _SHOWN_DEVICE in message, message
        assert not has_control_characters(message)

    def test_open_failure(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """The open failure names the device with its controls escaped."""
        monkeypatch.setattr(
            scan_session_mod,
            "sane",
            FakeSaneModule(open_error=FakeSaneError("Invalid argument")),
        )
        backend = SaneBackend()

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages(_HOSTILE_DEVICE, _flatbed_settings(), page_sink)

        self._assert_defused(exc_info.value)

    def test_scanner_error(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """A flatbed scan failure names the device with its controls escaped."""
        dev = FakeSaneDev(start_error=FakeSaneError("scan failed"))
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages(_HOSTILE_DEVICE, _flatbed_settings(), page_sink)

        self._assert_defused(exc_info.value)

    def test_option_set_failure(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """A refused option names the device with its controls escaped."""
        dev = FakeSaneDev()
        dev.fail_assignment("resolution", FakeSaneError("Invalid argument"))
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages(_HOSTILE_DEVICE, _flatbed_settings(), page_sink)

        self._assert_defused(exc_info.value)

    def test_resolution_read_back_failure(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """A failed read-back names the device with its controls escaped."""
        dev = FakeSaneDev()
        dev.fail_read("resolution", FakeSaneError("I/O error"))
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages(_HOSTILE_DEVICE, _flatbed_settings(), page_sink)

        self._assert_defused(exc_info.value)

    def test_options_read_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed option listing names the device with its controls escaped."""
        dev = FakeSaneDev()
        dev.fail_call("get_options", FakeSaneError("I/O error"))
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.get_capabilities(_HOSTILE_DEVICE)

        self._assert_defused(exc_info.value)

    def test_capabilities_open_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A capability read's failed open names the device with controls escaped."""
        monkeypatch.setattr(
            scan_session_mod,
            "sane",
            FakeSaneModule(open_error=FakeSaneError("Invalid\x1b[31m argument")),
        )
        backend = SaneBackend()

        with pytest.raises(ScanError) as exc_info:
            backend.get_capabilities(_HOSTILE_DEVICE)

        self._assert_defused(exc_info.value)
        assert str(exc_info.value).startswith(
            f"Could not open scanner {_SHOWN_DEVICE}: "
        )

    def test_wedged_refusal(
        self,
        fake_sane_module: FakeSaneModule,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
    ) -> None:
        """The wedge refusal names both devices with their controls escaped."""
        backend = SaneBackend()
        _ = fake_sane_module
        record = sane_backend_mod._WEDGE
        record.stuck = True
        record.done = threading.Event()
        record.device = fake_device
        record.device_id = _HOSTILE_DEVICE
        record.page_label = "Page 1"

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages(_HOSTILE_DEVICE, _flatbed_settings(), page_sink)

        self._assert_defused(exc_info.value)
        assert str(exc_info.value).count(_SHOWN_DEVICE) == 2
