"""Tests for scanner abstraction layer (ABC + SaneBackend)."""

from __future__ import annotations

import ast
import dataclasses
import gc
import importlib
import inspect
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, NamedTuple

import pytest
from PIL import Image, ImageDraw

import saneless.cli as cli_module
import saneless.scanner as scanner_pkg
import saneless.scanner.sane_backend as sane_backend_mod
import saneless.web.app as app_module
from saneless.exceptions import ConfigError, FeederEmptyError, ScanError
from saneless.pipeline import _SPOOL_LABEL_A, _SPOOL_LABEL_B
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    PageRecord,
    PageSink,
    ScanBatch,
    ScannerBackend,
    ScanSettings,
    SourceKind,
    classify_source,
)
from saneless.scanner.sane_backend import GeometryUnit, SaneBackend
from saneless.spool import SpooledPageSink
from tests.conftest import images_of, reset_sane_process_state
from tests.fake_sane import (
    FakeSaneDev,
    FakeSaneError,
    FakeSaneModule,
    ReadBlockMode,
    build_option_table,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import ModuleType

# The backend module's own logger, for the tests that read what it reported.
_BACKEND_LOGGER = "saneless.scanner.sane_backend"

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
    test can tell a real page apart from a uniformly blank one.  The backend no
    longer judges content, so variance is no longer required to survive a scan.
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


# D-17 completed: MockSaneDev, MockSaneModule and _FakeSaneDevice used to live
# here.  All three modelled a python-sane that does not exist -- most sharply
# the geometry-less one, which RAISED on an unknown option name where the real
# library stores it silently -- and each disagreement let a shipped defect earn
# a green test (M-32).  There is now exactly one definition of what python-sane
# does, in tests/fake_sane.py, and both this module and test_pipeline.py are
# written against it.


# ---------------------------------------------------------------------------
# Dataclass field tests
# ---------------------------------------------------------------------------


class TestDeviceInfo:
    """DeviceInfo dataclass tests."""

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


class TestScanSettings:
    """ScanSettings dataclass tests."""

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
# Source classification tests (CTR-04)
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
            # them down the single-page branch, and restores the C-06 defect.
            ("Automatic Document Feeder", SourceKind.FEEDER),
            ("Automatic Document Feeder(left aligned)", SourceKind.FEEDER),
            ("Automatic Document Feeder(centrally aligned)", SourceKind.FEEDER),
            ("Document Feeder", SourceKind.FEEDER),
            ("ADF", SourceKind.FEEDER),
            # "ADF Back" is a feeder for ROUTING purposes even though it must
            # not share a profile slug with "ADF Front" (N-09).
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
            # Ambiguity B -- "Manual Duplex" is this project's own pseudo-source
            # (docs/how-to/set-up-adf-duplex.md).  Its exposure is narrow and
            # Phase 25 deletes the source-overloading entirely, so no special
            # case is built for it here.
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
        """Harvested real-world SANE source names classify correctly (CTR-04)."""
        assert classify_source(source) is expected

    def test_uses_feeder_true_for_feeder_kinds(self) -> None:
        """FEEDER and FEEDER_DUPLEX feed a stack of sheets (CTR-04)."""
        assert SourceKind.FEEDER.uses_feeder
        assert SourceKind.FEEDER_DUPLEX.uses_feeder

    def test_uses_feeder_false_for_single_page_kinds(self) -> None:
        """FLATBED, AUTO, and UNKNOWN take the single-page path (CTR-04)."""
        assert not SourceKind.FLATBED.uses_feeder
        assert not SourceKind.AUTO.uses_feeder
        assert not SourceKind.UNKNOWN.uses_feeder


# ---------------------------------------------------------------------------
# ABC contract tests
# ---------------------------------------------------------------------------


class TestScannerBackendABC:
    """Scanner backend ABC tests."""

    def test_scanner_backend_is_abstract(self) -> None:
        """ScannerBackend cannot be instantiated directly."""
        cls: type = ScannerBackend
        with pytest.raises(TypeError, match="abstract"):
            cls()


# ---------------------------------------------------------------------------
# SaneBackend tests (all using mocked sane module)
# ---------------------------------------------------------------------------


_TEST_DEVICE = "test:device:001"

# The call sequence a three-sheet feeder produces, start to finish.
#
# The real ADF iterator calls start() then snap() ONCE PER SHEET, so "snap was
# never called" does not distinguish the feeder path from the flatbed one --
# that was an artefact of the deleted double, whose multi_scan() handed back
# iter(list) and touched neither method. What actually distinguishes the feeder
# is this repeated per-page probe, ending in one final start() that reports the
# feeder empty. A flatbed scan is exactly ["start", "snap"], so comparing the
# whole sequence tells the two paths apart without asserting a falsehood about
# the library.
_THREE_SHEET_FEEDER_CALLS = ["start", "snap"] * 3 + ["start"]

# The free-space reserve the sinks in this module keep beyond the page being
# written.  Zero, deliberately: these tests are about what the backend does
# with a page, not about D-07's shortfall arithmetic, and any positive reserve
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


def _join_sane_reader_threads(timeout: float = _READER_JOIN_SECONDS) -> None:
    """
    Wait for every acquisition thread the backend started to finish.

    Args:
        timeout: The bound each join waits under.

    """
    for thread in threading.enumerate():
        if thread.name.startswith(_READER_THREAD_PREFIX):
            thread.join(timeout)


def _uncropped(image: Image.Image) -> Image.Image:
    """
    Hand a page on unchanged, as the crop closure for a direct acquisition call.

    ``_acquire_pages`` takes the per-page crop as a closure because
    ``scan_pages`` binds the paper size, the resolution the device chose and
    whether the scan area was set on the device into one callable.  A test
    driving acquisition directly has none of those, and is not about geometry,
    so it supplies the identity.

    Args:
        image: The page the device produced.

    Returns:
        That same page.

    """
    return image


@pytest.fixture
def page_sink(tmp_path: Path) -> SpooledPageSink:
    """
    Return the sink this test's ``scan_pages`` call spools into.

    ``scan_pages`` takes a sink as its third argument now, and it is the
    pipeline that owns one in production, so every test here supplies its own.

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
    Patch the one shared fake into sane_backend's module-level ``sane`` name.

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
    monkeypatch.setattr(sane_backend_mod, "sane", module)
    return module


@pytest.fixture
def fake_device(fake_sane_module: FakeSaneModule) -> FakeSaneDev:
    """Return the one device handle the patched module hands out."""
    return fake_sane_module.open(_TEST_DEVICE)


@pytest.fixture
def sane_backend(fake_sane_module: FakeSaneModule) -> SaneBackend:
    """Create a SaneBackend over the shared fake."""
    _ = fake_sane_module  # side-effect: patches the sane module
    return SaneBackend()


class TestTheSuiteResetsTheProcessGlobalSaneState:
    """
    The suite-wide reset really re-arms the init guard, in both its branches.

    The guard is process state (D-17), so a module that builds a
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
        outstanding, because ``sane_exit()`` closes every open handle (D-13,
        D-18).  Left at that, a test that wedged the backend would set
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
    """SaneBackend initialization tests."""

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


def _guard_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """
    Collect the backend module's WARNING messages.

    Args:
        caplog: The capturing fixture, already set to WARNING for the module.

    Returns:
        One string per WARNING the backend logged during the call phase.

    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _BACKEND_LOGGER and record.levelno == logging.WARNING
    ]


class TestSaneInitGuard:
    """
    SANE is initialised once per process, behind a guard (HARD-05, D-17).

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
        """Repeating the configured host is the normal case, not a warning."""
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
            threading.Thread(target=build, name=f"init-race-{i}")
            for i in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        assert failures == []
        assert fake_sane_module.init_call_count == 1

    def test_init_once_is_not_recorded_when_init_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed init raises and leaves the guard unset, so a retry runs."""
        failing = FakeSaneModule(init_error=FakeSaneError("no SANE here"))
        monkeypatch.setattr(sane_backend_mod, "sane", failing)
        with pytest.raises(ScanError, match="Could not initialise SANE: no SANE here"):
            SaneBackend()
        assert failing.init_call_count == 1

        working = FakeSaneModule()
        monkeypatch.setattr(sane_backend_mod, "sane", working)
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
    SANE is shut down at an entry point, once, and never over an error (D-18).

    ``shutdown()`` is the other half of the init guard: the entry point that
    owns the process calls it when the process is ending, and nothing else
    calls it at all.  ``atexit`` is not used and must not be, because it runs
    while a daemon reader thread may still be inside ``sane_read``.
    """

    def test_shutdown_calls_sane_exit_once_however_often_it_is_called(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """Repeated shutdowns are the normal case for several backends."""
        SaneBackend()
        sane_backend_mod.shutdown()
        sane_backend_mod.shutdown()
        assert fake_sane_module.exit_call_count == 1

    def test_shutdown_calls_nothing_when_sane_was_never_initialised(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """With no init there is nothing to undo, and sane_exit is undefined."""
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
        """A failed exit must not leave the process unable to initialise again."""
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
        """A raise from a close callback would replace the operator's error."""
        backend = SaneBackend()
        monkeypatch.setattr(fake_sane_module, "exit", _failing_exit)
        with caplog.at_level(logging.WARNING, logger=sane_backend_mod.__name__):
            backend.close()

        _assert_shutdown_failure_logged(caplog)

    @pytest.mark.parametrize(
        "module",
        [sane_backend_mod, cli_module, app_module],
        ids=["sane_backend", "cli", "web.app"],
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
        is structural rather than textual on purpose -- the backend's own
        docstrings explain at length why ``concurrent.futures``' interpreter-
        exit join made a pooled reader unsurvivable, and prose recording a
        rejected design is not the design.
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
    """SaneBackend device enumeration tests."""

    def test_sane_backend_get_devices(self, sane_backend: SaneBackend) -> None:
        """get_devices returns DeviceInfo objects from sane.get_devices()."""
        devices = sane_backend.get_devices()
        assert len(devices) == 1
        assert isinstance(devices[0], DeviceInfo)
        assert devices[0].name == "test:device:001"
        assert devices[0].vendor == "TestVendor"
        assert devices[0].model == "TestModel"
        assert devices[0].device_type == "scanner"


class TestSaneBackendScanPages:
    """SaneBackend scan page acquisition tests."""

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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        assert mock_dev.cancel_calls
        assert mock_dev.close_calls

    def test_sane_backend_cancel_before_close_on_error(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """Device cancel() and close() are called even when the scan raises."""
        # The fault is armed on the device rather than by swapping out its snap
        # method: start() is where a flatbed scan first touches the hardware,
        # and the fake raises from there with the library's own error type.
        # Since EXC-01 the library's error leaves the backend as a ScanError
        # naming the device, with the original kept as its cause.
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
        """Verify snap() is called without progress argument (Pitfall #2)."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)

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


class TestSaneBackendGetCapabilities:
    """SaneBackend capability query tests."""

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
        # real SANE ``test`` backend does and what the deleted double did not.
        # The word list stays empty because the device reported no word list --
        # the two are different facts and neither is derived from the other.
        assert caps.resolution_range == (1.0, 1200.0, 1.0)
        assert caps.resolutions == []
        assert len(caps.raw_options) > 0


# ---------------------------------------------------------------------------
# ADF scan tests
# ---------------------------------------------------------------------------


class TestSaneBackendADFScan:
    """ADF simplex scan tests."""

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
        # Order is the records' own, never the directory's (D-02).
        assert [page.sequence for page in pages] == [1, 2, 3]
        assert all(page.path.exists() for page in pages)

    def test_adf_scan_uses_multi_scan_not_snap(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """ADF scan calls multi_scan(), not snap()."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)

        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        sane_backend.scan_pages("test:device:001", settings, page_sink)

        assert mock_dev.calls == _THREE_SHEET_FEEDER_CALLS

    def test_flatbed_still_uses_snap(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """Flatbed scan still uses snap(), not multi_scan()."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)

        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages

        assert len(pages) == 1
        assert mock_dev.calls.count("snap") == 1


class TestSaneBackendAutomaticDocumentFeeder:
    """Routing for feeder names that contain no "adf" token (C-06 / D-11)."""

    def test_automatic_document_feeder_yields_all_pages(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        Automatic Document Feeder uses multi_scan and returns every page (CTR-04).

        The SANE ``test`` backend names its feeder "Automatic Document Feeder",
        with no "adf" token anywhere in the string. The deleted string-sniffing
        rule in ``sane_backend`` returned False for it, so the flatbed
        ``start()``/``snap()`` branch ran and a ten-page stack produced exactly
        one page. This asserts that ``multi_scan()`` is used instead -- that all
        3 fake pages are spooled rather than 1, and that ``snap()`` is never
        called. This is the C-06 fix and the phase's one authorised behaviour
        change (D-11); it could not have passed before Phase 21.
        """
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.report_sources(["Flatbed", "Automatic Document Feeder"])

        settings = ScanSettings(
            source="Automatic Document Feeder", resolution=300, mode="Color"
        )
        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages

        assert len(pages) == 3
        assert [page.sequence for page in pages] == [1, 2, 3]
        assert mock_dev.calls == _THREE_SHEET_FEEDER_CALLS


class TestSaneBackendDuplex:
    """ADF Duplex scan tests."""

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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)

        settings = ScanSettings(source="ADF Duplex", resolution=300, mode="Color")
        sane_backend.scan_pages("test:device:001", settings, page_sink)

        assert mock_dev.calls == _THREE_SHEET_FEEDER_CALLS


class TestSaneBackendEmptyFeeder:
    """Empty ADF feeder detection tests."""

    # ``test_empty_feeder_out_of_documents_error`` was deleted here by D-03.
    # It drove ``multi_scan()`` itself into raising and asserted the result was
    # FeederEmptyError, but the real method is a one-line
    # ``return _SaneIterator(self)`` that cannot raise, so it pinned the
    # behaviour of provably unreachable code.  A fault arriving from the
    # iterator is now covered honestly by TestAdfPageErrorsAreTruthful, and the
    # zero-page path it nominally tested is covered below.

    def test_empty_feeder_stop_iteration(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """Multi_scan that yields zero pages raises FeederEmptyError."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")

        with pytest.raises(FeederEmptyError, match="No paper detected in feeder"):
            backend.scan_pages("test:device:001", settings, page_sink)


# The four first-page faults measured against the real SANE ``test`` backend
# with ``read_return_value`` set to the matching status (RESEARCH Finding 3).
# Every one of them reached the operator as "No paper detected in feeder"
# before D-03.
_MEASURED_SANE_FAULTS = [
    "Error during device I/O",
    "Document feeder jammed",
    "Scanner cover is open",
    "Device busy",
]

# The one message python-sane converts to StopIteration (sane.py:130).
_OUT_OF_DOCUMENTS = "Document feeder out of documents"


def _feeder_settings() -> ScanSettings:
    """
    Build settings that route to the ADF through the shared fake's options.

    The mode is ``"Color"`` and not ``"color"`` because the fake carries the
    real device's list constraint, which rejects an unlisted value.

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
    monkeypatch.setattr(sane_backend_mod, "sane", FakeSaneModule(device=dev))
    return SaneBackend()


class TestAdfPageErrorsAreTruthful:
    """
    A real SANE fault is reported as itself, never as an empty feeder (D-03).

    ``sane_backend`` used to convert *every* exception raised while acquiring
    page 0 into ``FeederEmptyError("No paper detected in feeder")``, so a jam,
    an open cover, a busy device and an I/O error all told the operator to
    load paper (M-11).  The genuine empty-feeder signal never reached that
    branch anyway: python-sane converts exactly one message to
    ``StopIteration``, and a zero-page feeder is the only honest source of
    "No paper detected in feeder".
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
    The ADF loop is bounded, so non-feeder hardware cannot spin forever (D-04).

    python-sane's ``_SaneIterator.__next__`` stops only on one exact message,
    so on hardware that is not a feeder ``start()``/``snap()`` keep succeeding
    and the loop never terminates -- reproduced live during research with a
    Flatbed source that yielded page after page and would not stop.  The
    per-page timeout is no help: a scan that succeeds satisfies it every
    single iteration.  This is Phase 21's W-01, discharged here.
    """

    def test_an_endless_feeder_is_cut_off_at_the_cap(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """A device that never reports end-of-feed raises ScanError at the cap."""
        cap = sane_backend_mod._MAX_ADF_PAGES
        # Far more sheets than the cap, so the fake never reports end-of-feed
        # and the loop has to be stopped by the cap rather than by the device.
        dev = FakeSaneDev(pages=cap * 100)
        backend = _backend_with(dev, monkeypatch)

        started = time.monotonic()
        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", _feeder_settings(), page_sink)
        elapsed = time.monotonic() - started

        assert str(cap) in str(exc_info.value)
        assert not isinstance(exc_info.value, FeederEmptyError)
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

        pages = backend.scan_pages("test:0", _feeder_settings(), page_sink).pages

        assert len(pages) == cap


class TestSaneBackendPageValidation:
    """Inline page validation tests."""

    def test_zero_dimension_page_skipped(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """Page with zero dimensions is skipped with warning."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
        page is exactly mean 255.0 / stddev 0.0 -- which is precisely what the
        deleted scanner-level check keyed on.  Those statistics are still how a
        blank page is recognised; what changed is *where*.  The decision now
        belongs to ``pipeline._drop_empty_pages``, under the profile's
        ``enable_empty_page_detection`` toggle, where the user can see it and
        turn it off (M-14, D-05).
        """
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        white_img = Image.new("RGB", (200, 300), (255, 255, 255))
        normal_img = _make_content_image()
        mock_dev.load_feeder([white_img, normal_img])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        pages = backend.scan_pages("test:device:001", settings, page_sink).pages
        assert len(pages) == 2
        # The blank itself survived, rather than the content page arriving
        # twice.  Asserted from the record's own statistics, which are the
        # exact two numbers named above and are measured once, at spool time:
        # a stddev of 0 means every greyscale pixel is equal, and a mean of 255
        # says which value they all are.
        assert pages[0].mean == 255.0
        assert pages[0].stddev == 0.0

    def test_pure_black_page_survives(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """
        A uniformly black page reaches the caller instead of being discarded.

        This is the defect the real SANE ``test`` backend exposed end to end:
        its default picture is solid black, so every page of a ten-sheet stack
        was destroyed by the backend before the pipeline ever saw it.  Judging
        content is not the scanner layer's job (M-14, D-05).
        """
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        black_img = Image.new("RGB", (200, 300), (0, 0, 0))
        normal_img = _make_content_image()
        mock_dev.load_feeder([black_img, normal_img])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        pages = backend.scan_pages("test:device:001", settings, page_sink).pages
        assert len(pages) == 2
        # Mean 0.0 with stddev 0.0 is solid black, which is precisely the
        # statistic the deleted content policy keyed on.
        assert pages[0].mean == 0.0
        assert pages[0].stddev == 0.0

    def test_normal_content_page_passes(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """Image with mixed content passes all validation checks."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        page = _with_orientation_exif(Image.new("RGB", (100, 100), "white"))
        mock_dev.load_feeder([page])

        backend = SaneBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        batch = backend.scan_pages("test:device:001", settings, page_sink)

        assert len(batch.pages) == 1
        _assert_no_exif_on_disk(batch.pages[0])


class TestFlatbedIntegrityChecks:
    """
    The single-sheet path runs the same two checks as the feeder (WR-03).

    The feeder validated every page and counted rejections; the flatbed path
    did neither.  The reason given -- that the caller sees any failure as an
    exception -- describes the case these checks are not for: an unreadable
    image returned *successfully*, which flowed into ``_maybe_crop`` and then
    into ``assemble_pdf``, where ``img.save()`` on a 0x0 image was the first
    thing to notice.  The identical page arriving from a feeder was skipped,
    counted and reported.

    A failure is fatal here rather than counted, because a flatbed exposes one
    sheet at a time and there is no next page to carry on to.
    """

    def test_a_zero_dimension_sheet_raises(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """A 0x0 image is not a page, however successfully it was returned."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([Image.new("RGB", (0, 0))])

        backend = SaneBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        with pytest.raises(ScanError, match="unreadable"):
            backend.scan_pages("test:device:001", settings, page_sink)

    def test_a_sheet_below_the_byte_floor_raises(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """A 10x10 RGB page is 300 bytes, far below the floor."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([Image.new("RGB", (10, 10), "white")])

        backend = SaneBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        with pytest.raises(ScanError, match="unreadable"):
            backend.scan_pages("test:device:001", settings, page_sink)

    def test_a_readable_sheet_still_reports_no_rejections(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """The check must not start counting good flatbed pages as rejects."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([_make_content_image()])

        backend = SaneBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        batch = backend.scan_pages("test:device:001", settings, page_sink)

        assert len(batch.pages) == 1
        assert batch.pages_rejected == 0


class TestIntegrityFailuresAreSkippedAndCounted:
    """
    One unreadable page costs one page; a wholly unreadable batch raises.

    Raising on the first integrity failure was rejected in D-06: it would make
    "every returned page is readable" true by construction, but it fails a
    fifty-sheet job over one bad sheet, and partial-result recovery belongs to
    Phase 29's HARD-02.

    A batch in which *every* page was rejected must not return an empty list
    either.  The pipeline would hand that straight to ``assemble_pdf([])`` and
    record a job that produced nothing as a success, which is M-14's third
    consequence and the reason it prescribes a raise.
    """

    def test_a_mid_stack_integrity_failure_costs_exactly_one_page(
        self,
        fake_sane_module: FakeSaneModule,
        caplog: pytest.LogCaptureFixture,
        page_sink: SpooledPageSink,
    ) -> None:
        """A zero-dimension third sheet is skipped and named; four survive."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([_make_content_image() for _ in range(5)])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        with caplog.at_level(logging.WARNING):
            pages = backend.scan_pages("test:device:001", settings, page_sink).pages

        assert len(pages) == 5
        assert not [r for r in caplog.records if "skipping" in r.getMessage()]


class TestAutoSourceRouting:
    """Auto source conditional routing via auto_source_mode."""

    def test_auto_source_adf_routes_to_adf_path(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """scan_pages with source='Auto' and auto_source_mode='adf' uses ADF path."""
        # Add "Auto" to available sources
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", auto_source_mode="adf"
        )
        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages
        # Flatbed always uses flatbed path regardless of auto_source_mode
        assert len(pages) == 1
        assert mock_dev.calls.count("snap") == 1


class TestAutoSourceRecognition:
    """The Auto source is recognised by the classifier, not by == (Q8)."""

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

        ``effective_source == "Auto"`` was case- and whitespace-sensitive, so a
        device reporting lowercase ``auto`` classified as AUTO, took the
        single-page path and skipped the override entirely -- silently ignoring
        ``auto_source_mode = "adf"`` and returning one page from a whole stack.
        """
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
        """It is a feeder by classification, so auto_source_mode is irrelevant."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
        D-01, asserted so that reversing it flips a test rather than passing quietly.

        UNKNOWN keeps today's single-page routing.  C-06's safer default --
        treat anything that is not the flatbed entry as multi-page -- was
        DECLINED, not deferred.  ``auto_source_mode`` is set to ``"adf"`` here
        precisely to show an unrecognised name does not reach the Auto
        override either.
        """
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
    Manual duplex resolves a real feeder from the device's own list (D-02, C-01).

    ``"Manual Duplex"`` used to reach ``_resolve_source`` verbatim, where it
    either raised or -- on a device offering ``Auto`` -- was silently swapped for
    ``Auto``.  With ``auto_source_mode`` defaulting to ``"flatbed"``, that swap
    took one platen snapshot per pass and reported a green Complete.  These
    tests pin the feeder branch, and above all that the ``Auto`` substitution is
    unreachable from it.

    No expected feeder name here is a plain ``"ADF"``: real consumer feeders
    report ``"Automatic Document Feeder"``, and a hardcoded ``"ADF"`` (the code
    review's suggestion, declined by D-02) would fail on exactly that hardware.
    """

    def test_selects_the_first_source_that_feeds(self) -> None:
        """The device's own first feeder is chosen, not a guessed name."""
        raw = _options_reporting(["Flatbed", "Automatic Document Feeder", "ADF Duplex"])

        effective, has_source_option = sane_backend_mod._resolve_source(
            raw, "Flatbed", resolve_feeder=True
        )

        assert effective == "Automatic Document Feeder"
        assert has_source_option is True

    def test_a_single_sided_feeder_the_operator_named_is_honoured(self) -> None:
        """An operator who picked one of two feeders gets that one (T-25-28)."""
        raw = _options_reporting(["Flatbed", "ADF Front", "Automatic Document Feeder"])

        effective, _ = sane_backend_mod._resolve_source(
            raw, "Automatic Document Feeder", resolve_feeder=True
        )

        assert effective == "Automatic Document Feeder"

    def test_a_named_both_sides_feeder_is_overridden_loudly(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        A named ``"ADF Duplex"`` gives way to the single-sided feeder (WR-02).

        A both-sides source returns 2N pages per pass, the two passes' counts
        agree, and interleaving them scrambles 4N pages into a green DONE. The
        operator's choice is therefore overridden -- with a WARNING naming both
        sources, so the override is never silent.
        """
        raw = _options_reporting(["Flatbed", "Automatic Document Feeder", "ADF Duplex"])

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
            effective, _ = sane_backend_mod._resolve_source(
                raw, "ADF Duplex", resolve_feeder=True
            )

        assert effective == "Automatic Document Feeder"
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.name == "saneless.scanner.sane_backend"
            and record.levelno == logging.WARNING
        ]
        assert any(
            "'ADF Duplex'" in message and "'Automatic Document Feeder'" in message
            for message in warnings
        )

    def test_a_single_sided_feeder_wins_over_an_earlier_both_sides_one(
        self,
    ) -> None:
        """The first *single-sided* feeder is chosen, not the first feeder (WR-02)."""
        raw = _options_reporting(["Flatbed", "ADF Duplex", "Automatic Document Feeder"])

        effective, _ = sane_backend_mod._resolve_source(
            raw, "Flatbed", resolve_feeder=True
        )

        assert effective == "Automatic Document Feeder"

    def test_a_device_whose_only_feeder_scans_both_sides_is_refused(self) -> None:
        """
        Only a both-sides feeder means refusal before any page (WR-02).

        Using it with a WARNING would still end in a scrambled document beside
        a green DONE on an unattended appliance, so the operator is pointed at
        hardware duplex instead.
        """
        raw = _options_reporting(["Flatbed", "ADF Duplex"])

        with pytest.raises(ScanError, match="scans both sides") as excinfo:
            sane_backend_mod._resolve_source(raw, "ADF", resolve_feeder=True)

        message = str(excinfo.value)
        assert 'duplex = "hardware"' in message
        assert "['Flatbed', 'ADF Duplex']" in message

    def test_an_unreported_source_falls_back_to_a_reported_feeder(self) -> None:
        """``source = "ADF"`` on a device that says "Automatic Document Feeder"."""
        raw = _options_reporting(["Flatbed", "Automatic Document Feeder"])

        effective, _ = sane_backend_mod._resolve_source(raw, "ADF", resolve_feeder=True)

        assert effective == "Automatic Document Feeder"

    def test_a_flatbed_only_device_is_refused_naming_its_sources(self) -> None:
        """No feeder means no manual duplex, said loudly and before any scan."""
        raw = _options_reporting(["Flatbed"])

        with pytest.raises(ScanError, match="feeder") as excinfo:
            sane_backend_mod._resolve_source(raw, "ADF", resolve_feeder=True)

        assert "['Flatbed']" in str(excinfo.value)

    def test_auto_is_never_substituted_for_manual_duplex(self) -> None:
        """
        C-01's mechanism, closed: ``Auto`` on offer still ends in a refusal.

        Substituting ``Auto`` here is how a flatbed-only device used to take two
        platen snapshots and report success.
        """
        raw = _options_reporting(["Flatbed", "Auto"])

        with pytest.raises(ScanError, match="feeder") as excinfo:
            sane_backend_mod._resolve_source(raw, "ADF", resolve_feeder=True)

        assert "['Flatbed', 'Auto']" in str(excinfo.value)

    def test_no_source_option_trusts_a_configured_feeder_name(self) -> None:
        """
        A sheet-fed device with no ``source`` option feeds without being told (WR-03).

        The simplex path already trusts the classifier on the configured name
        for such a device, so manual duplex does the same and nothing is
        assigned.
        """
        raw = build_option_table(omit=("source",))

        assert sane_backend_mod._resolve_source(raw, "ADF", resolve_feeder=True) == (
            "ADF",
            False,
        )

    def test_no_source_option_accepts_the_legacy_manual_duplex_name(self) -> None:
        """The pre-phase ``source = "Manual Duplex"`` profile runs again (WR-03)."""
        raw = build_option_table(omit=("source",))

        assert sane_backend_mod._resolve_source(
            raw, "Manual Duplex", resolve_feeder=True
        ) == ("Manual Duplex", False)

    def test_no_source_option_refuses_a_non_feeder_name(self) -> None:
        """A configured flatbed on a device with no source option is refused (WR-03)."""
        raw = build_option_table(omit=("source",))

        with pytest.raises(ScanError, match="exposes no source option") as excinfo:
            sane_backend_mod._resolve_source(raw, "Flatbed", resolve_feeder=True)

        assert "'Flatbed'" in str(excinfo.value)

    def test_without_the_flag_auto_is_still_substituted(self) -> None:
        """The simplex path's validate-or-substitute behaviour is unchanged."""
        raw = _options_reporting(["Flatbed", "Auto"])

        assert sane_backend_mod._resolve_source(raw, "ADF", resolve_feeder=False) == (
            "Auto",
            True,
        )

    def test_without_the_flag_an_unsupported_source_still_raises(self) -> None:
        """The simplex path's refusal message keeps its existing shape."""
        raw = _options_reporting(["Flatbed"])

        with pytest.raises(ScanError, match="Device does not support source 'ADF'"):
            sane_backend_mod._resolve_source(raw, "ADF", resolve_feeder=False)

    def test_without_the_flag_a_reported_flatbed_is_kept(self) -> None:
        """Only manual duplex looks for a feeder; a simplex flatbed stays put."""
        raw = _options_reporting(["Flatbed", "Automatic Document Feeder"])

        assert sane_backend_mod._resolve_source(
            raw, "Flatbed", resolve_feeder=False
        ) == ("Flatbed", True)

    def test_scan_settings_does_not_resolve_a_feeder_by_default(self) -> None:
        """Every existing simplex construction keeps today's behaviour."""
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        assert settings.resolve_feeder_source is False

    def test_scan_pages_feeds_through_the_resolved_feeder(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """The flag reaches the backend, which assigns and drives the real feeder."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.report_sources(["Flatbed", "Automatic Document Feeder"])
        settings = ScanSettings(
            source="ADF",
            resolution=300,
            mode="Color",
            resolve_feeder_source=True,
        )

        pages = SaneBackend().scan_pages(_TEST_DEVICE, settings, page_sink).pages

        assert mock_dev.source == "Automatic Document Feeder"
        assert len(pages) == 3
        assert mock_dev.calls == _THREE_SHEET_FEEDER_CALLS

    def test_scan_pages_feeds_a_device_with_no_source_option(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """Manual duplex on a no-source-option device feeds the stack (WR-03)."""
        dev = FakeSaneDev(options=build_option_table(omit=("source",)))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="ADF",
            resolution=300,
            mode="Color",
            resolve_feeder_source=True,
        )

        pages = backend.scan_pages(_TEST_DEVICE, settings, page_sink).pages

        assert len(pages) == 3
        assert dev.calls == _THREE_SHEET_FEEDER_CALLS
        assert "source" not in dev.assignments

    def test_scan_pages_refuses_before_touching_the_platen(
        self, fake_sane_module: FakeSaneModule, page_sink: SpooledPageSink
    ) -> None:
        """A flatbed-plus-Auto device takes no snapshot at all for manual duplex."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.report_sources(["Flatbed", "Auto"])
        settings = ScanSettings(
            source="ADF",
            resolution=300,
            mode="Color",
            auto_source_mode="flatbed",
            resolve_feeder_source=True,
        )

        with pytest.raises(ScanError, match="feeder"):
            SaneBackend().scan_pages(_TEST_DEVICE, settings, page_sink)

        assert mock_dev.calls == []
        assert "source" not in mock_dev.assignments


class TestSaneBackendPerPageTimeout:
    """Per-page timeout tests."""

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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.block_read(ReadBlockMode.PARTIAL)

        backend = SaneBackend()

        try:
            with pytest.raises(ScanError, match="timed out after"):
                backend._scan_adf_pages(
                    mock_dev, page_sink, _uncropped, timeout_per_page=0.05
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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        assert isinstance(mock_dev, FakeSaneDev)

        backend = SaneBackend()
        records, _ = backend._scan_adf_pages(
            mock_dev, page_sink, _uncropped, timeout_per_page=5.0
        )
        assert len(records) == 3
        assert [record.sequence for record in records] == [1, 2, 3]


class TestSaneBackendCancelSequence:
    """
    HARD-03 and HARD-04: cancel, wait, then close only if the read returned.

    Every test here arms the shared fake's Event-gated read, so nothing waits
    out a sleep: the block is ended by setting an ``Event``, and the only
    bounded waits are the backend's own timeout and grace (TEST-02, D-16).

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

        The record is module state by design -- D-13 needs the *next*
        ``scan_pages`` on a fresh backend object to refuse -- so a test that
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
        record = sane_backend_mod._WEDGE
        record.stuck = False
        record.done = None
        record.device = None
        record.iterator = None
        record.device_id = ""
        record.page_label = ""

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
                dev, sink, _uncropped, timeout_per_page=0.05, grace=0.05
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
                    dev, page_sink, _uncropped, timeout_per_page=0.05
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

        The grace is injected the same way ``timeout_per_page`` is, so the
        test does not wait out the module's real ten seconds.
        """
        fake_device.block_read(ReadBlockMode.NEVER)

        with (
            caplog.at_level(logging.CRITICAL),
            pytest.raises(ScanError) as raised,
            sane_backend._open_device(_TEST_DEVICE) as dev,
        ):
            sane_backend._scan_adf_pages(
                dev, page_sink, _uncropped, timeout_per_page=0.05, grace=0.05
            )

        message = str(raised.value)
        assert "timed out" in message
        assert "did not respond" in message
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
        PDF than the error message claims (Pitfall 1, D-12).
        """
        fake_device.block_read(ReadBlockMode.PARTIAL)

        with (
            pytest.raises(ScanError, match="timed out"),
            sane_backend._open_device(_TEST_DEVICE) as dev,
        ):
            sane_backend._scan_adf_pages(
                dev, page_sink, _uncropped, timeout_per_page=0.05
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
        D-13: every public entry point refuses, touching nothing.

        "No SANE call" is asserted as the device's own call log being
        byte-for-byte what it was, because the refusal has to happen before
        ``sane.open()``.  Refusing after opening would be the very operation
        the SANE standard forbids while a read is outstanding.

        ``get_devices`` is included because it was the one entry point the
        refusal missed, and the scan path reaches it: ``_resolve_device`` calls
        it whenever ``scanner.device`` is empty -- the documented
        auto-detection default -- and does so *before* ``scan_pages``, so the
        refusal that would have stopped the job came too late.  On the ``net``
        backend ``sane_get_devices`` is an RPC on the same control wire the
        stuck read is on (WR-04).
        """
        self._wedge(sane_backend, fake_device, page_sink)
        calls_before = list(fake_device.calls)
        enumerations_before = fake_sane_module.get_devices_call_count
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")

        with pytest.raises(ScanError, match="Restart saneless"):
            sane_backend.scan_pages(_TEST_DEVICE, settings, second_pass_sink)
        with pytest.raises(ScanError, match="Restart saneless"):
            sane_backend.get_capabilities(_TEST_DEVICE)
        with pytest.raises(ScanError, match="Restart saneless") as refusal:
            sane_backend.get_devices()

        assert fake_device.calls == calls_before
        assert fake_sane_module.get_devices_call_count == enumerations_before
        assert fake_device.close_calls == 0
        assert fake_device.cancel_calls == 1
        # The refusal names the wedged device even though the caller had none
        # to name: enumeration is the call that finds out which devices exist.
        assert _TEST_DEVICE in str(refusal.value)

    def test_shutdown_leaves_sane_up_while_a_read_is_outstanding(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A wedged backend is not shut down, and the refusal is logged (D-18).

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
        HARD-04: one flatbed sheet is bounded exactly as one fed sheet is.

        The defaults are asserted alongside the behaviour because D-14's claim
        is not merely "the flatbed times out" but "with the same constant, and
        no new config key".  A second timeout that happened to be equal today
        would satisfy the first half and quietly drift apart later.  The grace
        is pinned for the same reason, and because it is the parameter that
        makes the unresponsive-cancel path testable at all (WR-10).
        """
        budget = (
            inspect.signature(sane_backend_mod._snap_flatbed)
            .parameters["budget"]
            .default
        )
        assert budget.timeout == sane_backend_mod._DEFAULT_PAGE_TIMEOUT_SECONDS
        assert budget.grace == sane_backend_mod._CANCEL_GRACE_SECONDS
        fake_device.block_read(ReadBlockMode.PARTIAL)

        with sane_backend._open_device(_TEST_DEVICE) as dev:
            with pytest.raises(ScanError, match="timed out"):
                sane_backend_mod._snap_flatbed(
                    dev,
                    _TEST_DEVICE,
                    page_sink,
                    _uncropped,
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
        The platen equivalent of ``test_did_not_respond_to_cancel`` (WR-10).

        D-14's claim is "one sheet is one sheet, whichever way it was
        presented", and until the grace became injectable here there was no
        flatbed version of this proof that did not wait out the module's real
        ten seconds -- so the half of the claim that matters most, what happens
        when the scanner ignores the cancel, was asserted only for the feeder.
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
                _uncropped,
                sane_backend_mod._PageBudget(timeout=0.05, grace=0.05),
            )

        # The injected grace was really used, not the module's ten seconds --
        # which is the whole difference this parameter makes.
        assert time.monotonic() - began < 1.0
        message = str(raised.value)
        assert "timed out" in message
        assert "did not respond" in message
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
        HARD-04's other half: the shared validation, fatal on the platen.

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

    def test_keyboard_interrupt_mid_read(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        D-15: Ctrl-C during a read takes the same device-safe path, then re-raises.

        This closes the "safe device cancel on Ctrl-C mid-read" Phase 28
        deferred to this phase.  Nothing about the operator-facing behaviour
        moves: the ``KeyboardInterrupt`` is re-raised rather than swallowed or
        translated into a ``ScanError``, so the CLI's exit 130 and its one-line
        message are exactly what they were (Phase 28 D-07).  What changes is
        the state the device is left in on the way out.

        The interrupt is delivered deterministically, without a sleep and
        without polling.  ``read_started`` is set by the reader thread from
        inside the blocked read, so by the time the signal is raised the
        waiting thread is provably past ``reader.start()`` and inside the
        block that handles the interrupt.
        """
        fake_device.block_read(ReadBlockMode.PARTIAL)

        def interrupt_once_the_read_blocks() -> None:
            if fake_device.read_started.wait(_READER_JOIN_SECONDS):
                signal.raise_signal(signal.SIGINT)

        interrupter = threading.Thread(
            target=interrupt_once_the_read_blocks, name="ctrl-c", daemon=True
        )

        with sane_backend._open_device(_TEST_DEVICE) as dev:
            interrupter.start()
            with pytest.raises(KeyboardInterrupt):
                sane_backend._scan_adf_pages(
                    dev, page_sink, _uncropped, timeout_per_page=5.0
                )
            interrupter.join(_READER_JOIN_SECONDS)

            assert fake_device.cancel_calls == 1
            assert fake_device.close_while_blocked is False
            assert fake_device.close_calls == 0

        assert fake_device.close_calls == 1
        assert fake_device.close_while_blocked is False
        assert page_sink.records == ()

    def test_an_interrupt_before_the_reader_starts_wedges_nothing(
        self,
        sane_backend: SaneBackend,
        fake_device: FakeSaneDev,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        WR-05: Ctrl-C landing before the thread exists must not cancel or wedge.

        ``reader.start()`` sits inside the guarded block so that an interrupt
        arriving once the thread exists cannot abandon it uncancelled.  The
        other end of that window is the expensive one: with no reader, the D-12
        tail fired ``dev.cancel()`` on a handle holding no read, waited out the
        whole grace on an event nothing would ever set, and then recorded a
        wedge that could never be cleared -- ``_release_wedge`` runs only from a
        reader's ``finally``, and there is no reader.  Every later scan would
        have refused and ``shutdown()`` would have skipped ``sane.exit()`` for
        the rest of the process.

        Only the *reader* thread is interrupted, so the cancel thread would
        start normally if the handler reached for it; ``cancel_calls`` is
        therefore a real assertion and not one the stub satisfies by accident.
        The elapsed bound is what pins "did not wait out the grace".
        """
        reader_prefix = sane_backend_mod._READER_THREAD_PREFIX

        class _InterruptTheReaderBeforeItStarts(threading.Thread):
            """A Thread whose reader never gets as far as running."""

            def start(self) -> None:
                """Raise instead of starting, but only for the reader."""
                if self.name.startswith(reader_prefix):
                    raise KeyboardInterrupt
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
            with pytest.raises(KeyboardInterrupt):
                sane_backend_mod._acquire_with_timeout(
                    dev, fake_device.snap, sane_backend_mod._page_label(0), 5.0, grace
                )
            elapsed = time.monotonic() - began

            assert fake_device.cancel_calls == 0
            assert sane_backend_mod._WEDGE.stuck is False

        assert elapsed < grace


# The child process the exit proof runs, and the bound it is given.
#
# It blocks in ``os.read`` on a pipe nobody writes to.  That is the honest
# stand-in for ``sane_read``: a real blocking syscall that releases the GIL,
# which a ``threading.Event().wait()`` would only pretend to be -- and the
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
from saneless.scanner.sane_backend import _acquire_with_timeout


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
    _acquire_with_timeout(device, never_returns, "Page 1", 0.2, 0.2)
except ScanError as exc:
    returned = "did not respond" not in str(exc)

print(f"returned={returned} cancels={device.cancels} closes={device.closes}")
'''


class TestStuckReadDoesNotBlockProcessExit:
    """
    HARD-03's only honest proof, and the reason the executor had to go.

    It cannot be made in-process: a test asserting "the interpreter would have
    exited" is asserting about something that has not happened yet.  So a real
    child is started, wedged in a real blocking read, and required to exit on
    its own.
    """

    def test_process_exits(self, tmp_path: Path) -> None:
        """
        A child with a permanently stuck reader still exits, and exits 0.

        The printed line carries the whole D-12 sequence in one assertion:
        the read did not return, exactly one cancel was fired, and no close
        was issued on a handle a read is still inside.

        Args:
            tmp_path: Where the child script is written.

        """
        child = tmp_path / "stuck_read.py"
        child.write_text(_STUCK_READ_CHILD, encoding="utf-8")
        env = {
            **os.environ,
            "SANELESS_TEST_PYTHON": sys.executable,
            "SANELESS_TEST_CHILD": str(child),
        }

        # Every argv element is a literal and the per-run paths travel in the
        # environment, quoted so they are never re-split -- the shape
        # test_atomic_write.py established for the one other child-process
        # test in this suite.
        result = subprocess.run(
            ["/bin/sh", "-c", 'exec "$SANELESS_TEST_PYTHON" "$SANELESS_TEST_CHILD"'],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=_CHILD_EXIT_SECONDS,
        )

        assert result.returncode == 0, result.stderr
        assert "returned=False cancels=1 closes=0" in result.stdout


class TestSaneBackendADFCleanup:
    """ADF cleanup (cancel/close) tests."""

    def test_cancel_called_after_adf_scan(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """
        Both cancel() and close() run after an ADF multi_scan completes.

        ``test_iterator_deleted_before_cancel`` was deleted here by D-17. It
        built a TrackingIterator with a ``__del__`` probe and a device double to
        host it, but its own comment conceded the deletion "may or may not
        appear depending on GC" -- so the only thing it ever actually asserted
        was that cancel had run, which is asserted here. Nothing was lost but
        the double, and the ``del iterator`` it nominally guarded is still in
        the backend's own ``finally``.
        """
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        sane_backend.scan_pages("test:device:001", settings, page_sink)
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        assert mock_dev.cancel_calls
        assert mock_dev.close_calls

    def test_cancel_called_on_adf_error(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """dev.cancel() and dev.close() run even when the ADF scan errors."""
        # The fault lands on the second sheet, so a page has already been
        # acquired when it arrives -- the same shape the hand-rolled error
        # iterator modelled, now driven through the one shared fake.
        dev = FakeSaneDev(
            pages=5,
            start_error=FakeSaneError("hardware error"),
            start_error_page=1,
        )
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Automatic Document Feeder", resolution=300, mode="Color"
        )

        # D-03: a fault after the first page is translated to ScanError
        # carrying the device's own text, rather than propagating raw.
        with pytest.raises(ScanError, match="hardware error"):
            backend.scan_pages("test:0", settings, page_sink)

        assert dev.cancel_calls
        assert dev.close_calls


class TestFakeFeederStartOrdering:
    """
    ``start()`` checks the armed error before the page budget (WR-07).

    The budget check used to run first, so an error armed at the index one past
    the last page -- the end-of-feed probe -- could never fire: the test
    silently became a clean-feed test rather than failing loudly as a
    misconfiguration.
    """

    def test_an_error_armed_at_the_probe_index_is_reachable(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """
        A jam on the probe surfaces instead of reading as a clean end of feed.

        With the budget checked first this device returned three pages and no
        error at all, so the arming was a silent no-op.
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


# The hyphenated spelling get_options() reports, which D-09's presence check
# reads.  Assignment uses underscores (dev.tl_x); see fake_sane._GEOMETRY_NAMES.
_GEOMETRY_OPTION_NAMES = ("tl-x", "tl-y", "br-x", "br-y")

# A4 is 210 x 297 mm; at 300 dpi that is 2480 x 3507 px.
_A4_AT_300_DPI = (2480, 3507)


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

    This is the condition the old geometry-less double claimed to model and
    inverted.  The real ``SaneDev.__setattr__`` *stores* an unknown name
    (``sane.py:188``), so such a device accepts ``dev.br_y`` without complaint
    -- which is exactly why the presence check, and not an exception, is what
    makes the crop fallback reachable.

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
    """Paper size geometry option setting tests."""

    def test_a4_sets_geometry(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        page_sink: SpooledPageSink,
    ) -> None:
        """When paper_size='a4', dev.br_x=210.0 and dev.br_y=297.0."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="full"
        )

        sane_backend.scan_pages("test:device:001", settings, page_sink)

        # Asserted against the device's own assignment log rather than by
        # writing a sentinel and reading it back. The deleted double stored any
        # float verbatim, so a test could write -1.0 and see -1.0; a real device
        # clamps to the option's range, so that sentinel would have come back
        # 0.0 and the test would have passed only by coincidence.
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
        """When paper_size='letter', dev.br_x=215.9 and dev.br_y=279.4."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([Image.new("RGB", (2600, 3400), "white")])
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="letter"
        )
        sane_backend.scan_pages("test:device:001", settings, page_sink)
        # SANE_Fixed is a 16.16 fixed-point integer, so letter's 215.9 mm is not
        # exactly representable and reads back differing in the low bits without
        # the device having clamped anything. The deleted double stored floats
        # verbatim and hid that entirely -- which is precisely why D-19 compares
        # scan areas with a tolerance instead of for equality.
        assert mock_dev.br_x == pytest.approx(215.9, abs=1e-4)
        assert mock_dev.br_y == pytest.approx(279.4, abs=1e-4)

    # A geometry-less device's scan is asserted, crop size included, by
    # TestPaperSizeCropFallback.test_crop_fallback_when_geometry_fails below.


class TestPaperSizeCropFallback:
    """Pillow crop fallback when geometry options are unavailable."""

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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        original_img = Image.new("RGB", (5000, 6000), "white")
        mock_dev.load_feeder([original_img])
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="full"
        )
        pages = sane_backend.scan_pages("test:device:001", settings, page_sink).pages
        assert len(pages) == 1
        assert pages[0].size == (5000, 6000)

    def test_adf_crop_fallback(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """ADF pages are cropped when geometry options are unavailable."""
        backend = _backend_with(_geometry_less_device(pages=2), monkeypatch)
        settings = ScanSettings(
            source="Automatic Document Feeder",
            resolution=300,
            mode="Color",
            paper_size="a4",
        )
        pages = backend.scan_pages("test:0", settings, page_sink).pages
        assert len(pages) == 2
        for page in pages:
            assert page.size == _A4_AT_300_DPI


class TestFakeDeviceAreaMatchesTheLibrary:
    """
    ``area`` raises what python-sane raises on a geometry-less device (WR-06).

    The fake read ``_values`` directly, so a device whose option table omits
    the geometry options -- the exact table ``build_option_table(omit=...)``
    exists to produce -- raised ``KeyError``.  The real library composes
    ``area`` from attribute reads (``sane.py:220``) and raises
    ``AttributeError("No such attribute: tl_x")``.

    No production path reaches this today, because ``_set_geometry`` checks
    presence before reading ``area``.  But that ordering is a property of
    today's code, and the fake's whole premise is that it cannot quietly
    diverge from the library it stands in for.
    """

    def test_a_geometry_less_device_raises_attribute_error(self) -> None:
        """Not KeyError, which the real library never raises here."""
        dev = FakeSaneDev(options=build_option_table(omit=_GEOMETRY_OPTION_NAMES))

        with pytest.raises(AttributeError, match="No such attribute"):
            _ = dev.area

    def test_a_device_reporting_the_options_still_returns_its_box(self) -> None:
        """Composing from attribute reads must not change the happy path."""
        dev = FakeSaneDev()
        dev.tl_x = 5.0
        dev.br_x = 100.0

        (tl_x, _tl_y), (br_x, _br_y) = dev.area

        assert tl_x == pytest.approx(5.0)
        assert br_x == pytest.approx(100.0)


class TestGeometryPresenceCheck:
    """
    Geometry is written only on a device that reports the options (D-09).

    The real ``SaneDev.__setattr__`` stores an unrecognised option name in
    ``__dict__`` and returns -- no device call, no validation, no raise
    (``sane.py:188``).  So assigning ``dev.br_y`` on a device that has no
    geometry options *succeeds*, ``_set_geometry`` returned True, and the Pillow
    crop fallback it guarded could never run.  That is M-15, and it shipped
    green because the double it was tested against raised on exactly the
    assignment the real library stores.

    Every test here drives a fake that stores silently, as the real library
    does, so each one fails if the presence check is removed.
    """

    def test_a_device_without_geometry_options_stores_br_y_silently(self) -> None:
        """
        The premise the whole fallback rests on, asserted rather than assumed.

        If this ever raises, the fake has drifted back to modelling a library
        that does not exist and every test below it proves nothing.
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
        SCNR-04's reachability proof: the page comes back at A4's pixel size.

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

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
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
        The bare ``except Exception`` names what it swallowed (M-15's other half).

        Here the options are reported but marked not software-settable, so the
        assignment raises for a structural reason rather than being stored.
        """
        dev = FakeSaneDev(options=build_option_table(geometry_settable=False), pages=1)
        dev.set_page_size(3000, 4000)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
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
    The scan-area scale factor comes from the descriptor the device sent (D-10).

    ``_set_geometry`` wrote ``br_x = 210.0`` for A4 on every device, assuming
    millimetres, although the unit lives at index 5 of the option tuple (N-03).
    A device reporting ``UNIT_PIXEL`` was handed 210 *pixels* -- about 18 mm at
    300 dpi -- and returned a sliver of the page with no error.

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
        Parametrised over the enum itself, so a new member is covered for free.

        This is Phase 21's D-09.  A member added without a matching ``match``
        arm leaves the scale unbound and fails here at runtime, as well as
        failing ``assert_never`` under both type checkers at edit time.
        """
        scale = sane_backend_mod._units_per_mm(unit, 300)

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
        requested value would reintroduce the very substitution bug D-11 cures,
        one layer further down.
        """
        dev = _device_reporting_unit(GeometryUnit.UNIT_PIXEL)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=5000, mode="Color", paper_size="a4"
        )

        backend.scan_pages("test:0", settings, page_sink)

        assert dev.br_x == pytest.approx(210.0 * 1200 / 25.4)
        assert dev.br_y == pytest.approx(297.0 * 1200 / 25.4)

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

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
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

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
            pages = backend.scan_pages("test:0", settings, page_sink).pages

        assert [m for m in _warning_messages(caplog) if "99" in m]
        assert pages[0].size == _A4_AT_300_DPI


class TestNonPositiveGeometryScale:
    """
    A scale of zero is declined, not written as a zero-size box (WR-01).

    ``_units_per_mm`` returns ``resolution / 25.4`` for UNIT_PIXEL, so a device
    whose read-back resolution truncates to 0 yields ``0.0``.  That value is
    not ``None``, so it passed the only guard ``_set_geometry`` had: the
    expected box became ``(0.0, 0.0)``, the device stored it, and the clamp
    check agreed with itself inside a tolerance that was also 0.  A zero-area
    scan reported as success is worse than the crop it bypassed.
    """

    def test_a_sub_one_dpi_read_back_yields_a_zero_scale(self) -> None:
        """The arithmetic really does produce 0.0 -- the premise, measured."""
        assert sane_backend_mod._units_per_mm(GeometryUnit.UNIT_PIXEL, 0) == 0.0

    def test_a_zero_scale_falls_back_to_the_crop(self) -> None:
        """``_set_geometry`` declines, which is what makes the crop run."""
        dev = _device_reporting_unit(GeometryUnit.UNIT_PIXEL)

        assert sane_backend_mod._set_geometry(dev, "a4", dev.get_options(), 0) is False

    def test_no_zero_size_box_reaches_the_device(self) -> None:
        """Declining happens before any corner is assigned."""
        dev = _device_reporting_unit(GeometryUnit.UNIT_PIXEL)

        sane_backend_mod._set_geometry(dev, "a4", dev.get_options(), 0)

        assert "br_x" not in dev.assignments
        assert "br_y" not in dev.assignments


class TestClampedScanArea:
    """
    A scan area the device quietly shrank is caught on read-back (D-19).

    Measured against the real ``test`` backend: writing A4's 210 mm to a device
    whose ``br-x`` range is ``(0.0, 200.0, 1.0)`` yields 200.0, with no error
    and no ``INFO_INEXACT`` the caller can see.  ``_set_geometry`` could
    therefore return True having set an area that is not the one requested --
    the same silent-substitution shape M-16 cures for DPI, and the page comes
    out quietly wrong.
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

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
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
        accepted verbatim, so comparing the far corner alone reports success
        over an area short by the whole minimum on each axis.  The read-back
        already had the measured top-left in hand and threw it away: an A4
        request silently yielded a 200 x 287 mm page, with no warning, no crop,
        and a PDF MediaBox disagreeing with its own content.
        """
        dev = _device_reporting_unit(GeometryUnit.UNIT_MM, (10.0, 300.0, 1.0))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
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

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
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
        """
        dev = _device_reporting_unit(GeometryUnit.UNIT_MM, (0.0, 300.0, 1.0))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="letter"
        )

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
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
        a clamped resolution produces M-16's cut-off page even when the
        fallback runs correctly.
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

        # A4 at the 75 dpi the device settled on, not at the 300 asked for,
        # which would have been 2480x3507.
        assert pages[0].size == (620, 876)


# ---------------------------------------------------------------------------
# D-17: the shared fake and the measured python-sane contract
# ---------------------------------------------------------------------------


class TestFakeSaneContract:
    """
    The shared fake models python-sane 2.9.2's measured behaviour.

    The rows asserted here are RESEARCH.md Finding 7's contract table, which
    was executed against the real library rather than inferred.  Each of the
    three hand-written doubles in this module got at least one row wrong, and
    every wrong row let a shipped defect earn a green test -- most visibly the
    geometry-less double, which *raised* on an unknown option where the real
    library *stores* it, so the crop fallback it was written to prove could
    never actually be reached.  That double is deleted (D-09).

    The table is parametrised rather than written out one test per row so that
    a future row cannot be added to the fake without also being asserted here.
    """

    @pytest.mark.parametrize(
        ("option", "value", "expected", "fragment"),
        [
            # Row 2: a bad value for a known option with a list constraint.
            ("source", "Nope", FakeSaneError, "Invalid argument"),
            ("mode", "Sepia", FakeSaneError, "Invalid argument"),
            # Row 3: the wrong Python type, rejected by the C layer before
            # SANE ever sees it.  CONTEXT.md does not name this row.
            (
                "resolution",
                "banana",
                TypeError,
                "SANE_FIXED requires a floating point number",
            ),
            (
                "tl_x",
                "banana",
                TypeError,
                "SANE_FIXED requires a floating point number",
            ),
            # Row 4: read-only attributes, buttons, groups, inactive and
            # not-software-settable options.
            ("dev", 1, AttributeError, "Read-only attribute: dev"),
            (
                "area",
                ((0.0, 0.0), (1.0, 1.0)),
                AttributeError,
                "Read-only attribute: area",
            ),
            ("optlist", [], AttributeError, "Read-only attribute: optlist"),
            (
                "scan_button",
                1,
                AttributeError,
                "Buttons don't have values: scan_button",
            ),
            (
                "geometry_group",
                1,
                AttributeError,
                "Groups don't have values: geometry_group",
            ),
            ("inactive_opt", "x", AttributeError, "Inactive option: inactive_opt"),
            (
                "readonly_opt",
                "x",
                AttributeError,
                "Option can't be set by software: readonly_opt",
            ),
        ],
    )
    def test_setattr_follows_the_measured_contract(
        self,
        option: str,
        value: object,
        expected: type[BaseException],
        fragment: str,
    ) -> None:
        """Each assignment row raises its measured exception and message."""
        dev = FakeSaneDev()
        with pytest.raises(expected) as exc_info:
            setattr(dev, option, value)
        assert fragment in str(exc_info.value)

    def test_unknown_option_is_stored_silently(self) -> None:
        """
        Row 1, the inverse of the deleted geometry-less double.

        The real ``SaneDev.__setattr__`` stores an unrecognised name straight
        into ``__dict__`` and returns: no device call, no validation, no
        raise.  This is precisely why ``_set_geometry`` always returned True.
        """
        dev = FakeSaneDev()
        dev.no_such_option = 1
        assert dev.no_such_option == 1

    @pytest.mark.parametrize(
        ("name", "fragment"),
        [
            ("scan_button", "Buttons don't have values: scan_button"),
            ("geometry_group", "Groups don't have values: geometry_group"),
            ("inactive_opt", "Inactive option: inactive_opt"),
            ("definitely_absent", "No such attribute: definitely_absent"),
        ],
    )
    def test_getattr_follows_the_measured_contract(
        self, name: str, fragment: str
    ) -> None:
        """Reading a button, group, inactive or absent name raises."""
        dev = FakeSaneDev()
        with pytest.raises(AttributeError) as exc_info:
            getattr(dev, name)
        assert fragment in str(exc_info.value)

    def test_multi_scan_returns_an_iterator(self) -> None:
        """``multi_scan()`` hands back something with ``__next__``."""
        dev = FakeSaneDev(pages=3)
        assert hasattr(dev.multi_scan(), "__next__")

    def test_multi_scan_cannot_raise(self) -> None:
        """
        ``multi_scan()`` only constructs the iterator, so it never raises.

        The real method is a one-line ``return _SaneIterator(self)``.  A
        double that raises here makes the backend's ``try``/``except`` around
        the call look meaningful when it is in fact unreachable.
        """
        dev = FakeSaneDev(pages=3, start_error=FakeSaneError("Document feeder jammed"))
        iterator = dev.multi_scan()
        with pytest.raises(FakeSaneError) as exc_info:
            next(iterator)
        assert "Document feeder jammed" in str(exc_info.value)

    def test_iterator_calls_start_then_snap_once_per_page(self) -> None:
        """The iterator drives start()/snap() per page, not ``iter(list)``."""
        dev = FakeSaneDev(pages=2)
        pages = list(dev.multi_scan())
        assert len(pages) == 2
        # The trailing "start" is not an off-by-one: the real iterator learns
        # the feeder is empty only by calling start() one more time and
        # catching the message it raises.  A double built on iter(list) hides
        # that probe, and with it the only place D-03's message handling runs.
        assert dev.calls == ["start", "snap", "start", "snap", "start"]

    def test_page_budget_is_honoured(self) -> None:
        """A three-sheet feeder yields exactly three pages."""
        dev = FakeSaneDev(pages=3)
        assert len(list(dev.multi_scan())) == 3

    def test_out_of_documents_becomes_stop_iteration(self) -> None:
        """The one message the iterator converts to StopIteration."""
        dev = FakeSaneDev(
            pages=5,
            start_error=FakeSaneError("Document feeder out of documents"),
        )
        assert list(dev.multi_scan()) == []

    def test_an_empty_feeder_stops_immediately(self) -> None:
        """A zero-sheet feeder stops without yielding."""
        dev = FakeSaneDev(pages=0)
        assert list(dev.multi_scan()) == []

    def test_a_jam_propagates_out_of_next(self) -> None:
        """Any other message propagates rather than ending the scan."""
        dev = FakeSaneDev(pages=5, start_error=FakeSaneError("Document feeder jammed"))
        with pytest.raises(FakeSaneError):
            list(dev.multi_scan())

    def test_get_options_reports_a_realistic_feeder_name(self) -> None:
        """The default table carries the long real-world feeder name (C-06)."""
        dev = FakeSaneDev()
        constraints = {opt[1]: opt[8] for opt in dev.get_options()}
        assert "Automatic Document Feeder" in constraints["source"]

    def test_get_options_reports_resolution_as_a_range(self) -> None:
        """Resolution is a ``(min, max, step)`` range, never a list (N-01)."""
        dev = FakeSaneDev()
        constraints = {opt[1]: opt[8] for opt in dev.get_options()}
        assert constraints["resolution"] == (1.0, 1200.0, 1.0)

    def test_option_names_use_hyphens_but_attributes_use_underscores(self) -> None:
        """D-09's presence check reads ``tl-x``; assignment writes ``tl_x``."""
        dev = FakeSaneDev()
        assert "tl-x" in [opt[1] for opt in dev.get_options()]
        dev.tl_x = 5.0
        assert dev.tl_x == 5.0

    def test_resolution_reads_back_as_float(self) -> None:
        """The real device returns float, which the Protocol now declares (D-11)."""
        dev = FakeSaneDev()
        dev.resolution = 300
        assert isinstance(dev.resolution, float)
        assert dev.resolution == 300.0

    @pytest.mark.parametrize(
        ("requested", "expected"),
        [(5000, 1200.0), (0, 1.0), (150, 150.0)],
    )
    def test_resolution_clamps_silently(self, requested: int, expected: float) -> None:
        """A range constraint clamps and reports success -- it never raises."""
        dev = FakeSaneDev()
        dev.resolution = requested
        assert dev.resolution == expected

    def test_a_fixed_point_value_cannot_be_stored_exactly(self) -> None:
        """
        SANE_Fixed is 16.16, so letter's 215.9 mm is not representable.

        The value comes back differing in the low bits without the device
        having clamped anything, which is why D-19 uses a tolerance.
        """
        dev = FakeSaneDev()

        dev.br_x = 215.9

        assert dev.br_x != 215.9
        assert dev.br_x == pytest.approx(215.9, abs=1e-4)

    def test_area_reflects_the_geometry_clamp(self) -> None:
        """An A4 box against a 200mm device clamps, exactly as measured."""
        dev = FakeSaneDev(geometry_range=(0.0, 200.0, 1.0))
        dev.tl_x = 0.0
        dev.tl_y = 0.0
        dev.br_x = 210.0
        dev.br_y = 297.0
        assert dev.area == ((0.0, 0.0), (200.0, 200.0))

    def test_a_narrowed_constraint_rejects_a_previously_legal_value(self) -> None:
        """The option table is injectable, so a plan can narrow a constraint."""
        dev = FakeSaneDev(
            options=[(1, "mode", "Scan mode", "Mode desc", 3, 0, 1, 5, ["Color"])]
        )
        with pytest.raises(FakeSaneError) as exc_info:
            dev.mode = "Lineart"
        assert "Invalid argument" in str(exc_info.value)

    def test_error_type_mirrors_the_real_error_mro(self) -> None:
        """``_sane.error`` subclasses Exception directly, not OSError."""
        assert issubclass(FakeSaneError, Exception)
        assert not issubclass(FakeSaneError, OSError)

    def test_module_open_returns_the_shared_device(self) -> None:
        """``open()`` hands back one device, so a test can configure it."""
        module = FakeSaneModule()
        assert module.init() == (1, 0, 3)
        assert module.init_call_count == 1
        assert module.open("test:0") is module.open("test:0")

    def test_module_reports_four_element_device_tuples(self) -> None:
        """``get_devices()`` matches the real ``(name, vendor, model, type)``."""
        devices = FakeSaneModule().get_devices()
        assert devices
        for entry in devices:
            assert len(entry) == 4


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
        """The stranded value is the hazard that makes ordering observable."""
        dev = FakeSaneDev()
        dev.narrow_resolution_for_source("ADF Duplex", (1.0, 600.0, 1.0))
        dev.resolution = 1000
        dev.source = "ADF Duplex"
        assert dev.resolution == 1000.0


# ---------------------------------------------------------------------------
# D-11: source-first option ordering and the resolution read-back
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
        A feeder ceiling of 600 is respected because the source was set first.

        Under the old mode/resolution/source order the 1000 dpi request was
        validated against the platen's 1200 dpi range and then stranded there
        when selecting the feeder reloaded the descriptors, so the device was
        left holding a value its active constraint no longer permits.
        """
        feeder = "Automatic Document Feeder"
        dev = FakeSaneDev(pages=1)
        dev.narrow_resolution_for_source(feeder, (1.0, 600.0, 1.0))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source=feeder, resolution=1000, mode="Color")

        backend.scan_pages("test:0", settings, page_sink)

        # ``resolution`` is declared on the fake with the type the real device
        # hands back, so this isinstance is a runtime assertion rather than a
        # static narrowing: it pins that the device really reports a float,
        # which is what the Protocol quietly misdeclared for three phases.
        resolution = dev.resolution
        assert isinstance(resolution, float)
        assert 1.0 <= resolution <= 600.0


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
    one, and neither is derived from the other (Q6): expanding a range into a
    list would print saneless's invention rather than the device's answer, and
    inferring a range from a list would claim support for values between the
    listed ones.  At most one of the two is ever populated.

    ``get_capabilities`` used to read only the word-list shape, so the SANE
    ``test`` backend's measured ``(1.0, 1200.0, 1.0)`` arrived as an empty
    list: the CLI printed a label with nothing after it and auto-profiles fell
    back to 300 regardless of what the device actually supported (N-01).
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
        """Reading the range shape did not disturb the two list-shaped options."""
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
        A device-supplied tuple of the wrong shape yields neither field (T-24-27).

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
        The same guard for non-numeric members, asserted where it protects.

        This case is driven straight through ``_constraint`` rather than through
        a device, and deliberately so: the shared fake coerces a range's members
        with ``float()`` when it builds a device's starting values, so it cannot
        hold this table at all.  That refusal is correct -- no real SANE backend
        can report a range of strings -- and widening the fake to accept one
        would make it model a library that does not exist, which is the very
        drift D-17 exists to stop.  The guard is defensive hardening against a
        malformed device, so the honest place to assert it is the function that
        does the hardening.
        """
        found = sane_backend_mod._constraint(
            [_option(2, "resolution", _FIXED_OPTION, ("low", "high", "step"))],
            "resolution",
        )

        assert found.present is True
        assert found.values is None
        assert found.span is None

    def test_a_short_option_tuple_is_skipped_without_raising(self) -> None:
        """An option too short to carry a constraint is ignored, never fatal."""
        found = sane_backend_mod._constraint([(1, "resolution")], "resolution")

        assert found.present is False


class TestSourceOptionPresence:
    """
    Whether a device HAS a source option is a different fact from its constraint.

    ``_resolve_source`` records presence independently of whether the constraint
    can be read as a word list, and the assignment in ``_configure_device``
    depends on that flag.  Collapsing the two -- reporting only the parsed
    constraint -- would silently stop saneless setting the source on a device
    whose constraint it cannot read, which is a behaviour change disguised as a
    refactor.
    """

    def test_a_device_with_no_source_option_is_left_alone(self) -> None:
        """No source option means nothing to assign and nothing to validate."""
        effective, has_source_option = sane_backend_mod._resolve_source([], "Flatbed")

        assert has_source_option is False
        assert effective == "Flatbed"

    @pytest.mark.parametrize(
        "constraint",
        [None, (0.0, 1.0, 1.0), "Flatbed"],
        ids=["unconstrained", "range", "bare-string"],
    )
    def test_a_non_list_source_constraint_is_still_a_source_option(
        self, constraint: object
    ) -> None:
        """
        Presence is detected even when the constraint cannot be read as a list.

        This is a regression guard rather than a RED assertion: it already holds,
        and it is precisely the behaviour most at risk from the dedup.  The raise
        is what witnesses presence -- a device whose source option went
        unnoticed would return cleanly here instead, and saneless would scan
        from whatever source the device happened to be left on.
        """
        with pytest.raises(ScanError):
            sane_backend_mod._resolve_source(
                [_option(1, "source", _STRING_OPTION, constraint)], "Flatbed"
            )


class TestAutoSourceFallbackIsAudible:
    """
    Substituting 'Auto' for a missing source can change the page count (WR-02).

    ``scan_pages`` classifies the *effective* source, so a profile asking for
    "ADF Duplex" on a device offering only Flatbed and Auto is routed by
    ``auto_source_mode``, which defaults to "flatbed" -- one page out of a
    whole stack.  That was announced at INFO, while the comparable resolution
    substitution has warned since M-16.
    """

    def _flatbed_and_auto(self) -> list[tuple]:
        """Build an option table whose source list offers no feeder."""
        return [_option(1, "source", _STRING_OPTION, ["Flatbed", "Auto"])]

    def test_the_substitution_still_happens(self) -> None:
        """Raising the level must not change which source is chosen."""
        effective, has_source_option = sane_backend_mod._resolve_source(
            self._flatbed_and_auto(), "ADF Duplex"
        )

        assert effective == "Auto"
        assert has_source_option is True

    def test_the_substitution_is_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The requested source is named, at a level the operator sees."""
        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
            sane_backend_mod._resolve_source(self._flatbed_and_auto(), "ADF Duplex")

        assert [m for m in _warning_messages(caplog) if "ADF Duplex" in m]

    def test_the_warning_names_the_routing_consequence(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        The page-count risk is the point, not the substitution alone.

        "Falling back to Auto" reads as harmless; "may not be multi-page" is
        the part that explains a one-page PDF from a twenty-sheet stack.
        """
        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
            sane_backend_mod._resolve_source(self._flatbed_and_auto(), "ADF Duplex")

        assert [m for m in _warning_messages(caplog) if "auto_source_mode" in m]


class TestDeviceCapabilitiesShape:
    """The value object's field order, which existing call sites depend on."""

    def test_resolution_range_is_defaulted_and_sits_after_the_required_fields(
        self,
    ) -> None:
        """
        A defaulted field must stay in the defaulted block.

        ``PipelineRequest`` records the same trap in its own docstring: moving a
        defaulted field up into the non-default block reorders the dataclass and
        breaks positional construction, which call sites across the suite rely
        on.
        """
        fields = dataclasses.fields(DeviceCapabilities)
        defaulted = [
            f.name
            for f in fields
            if f.default is not dataclasses.MISSING
            or f.default_factory is not dataclasses.MISSING
        ]
        required = [f.name for f in fields if f.name not in defaulted]

        assert required == ["sources", "resolutions", "modes"]
        assert defaulted == ["raw_options", "resolution_range"]


class TestResolutionReadBack:
    """The resolution the device actually chose is read back (D-11, M-16)."""

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

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
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

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
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

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
            backend.scan_pages("test:0", settings, page_sink)

        assert not [m for m in self._warnings(caplog) if "resolution" in m.lower()]


class TestScanBatch:
    """
    One object carries what the device actually did, out of the backend (D-12).

    ``scan_pages`` used to yield ``Image`` only, so two facts the backend had
    already measured -- the resolution the device settled on, and how many fed
    sheets it could not read -- had no way out of it.  A generator's return
    value is discarded by ``list()``, which is what every pipeline call site
    does, so carrying them out meant changing the ABC rather than smuggling
    them past it.
    """

    def test_the_batch_carries_exactly_three_fields(self) -> None:
        """
        Three fields, in order, and the per-page detail lives in ``pages``.

        The object is still deliberately minimal.  HARD-01's ordered per-page
        record design landed as ``pages: tuple[PageRecord, ...]`` rather than as
        extra fields here, so anything measured about one page belongs on that
        record and a fourth field on the batch would be a second channel for it.
        """
        assert [field.name for field in dataclasses.fields(ScanBatch)] == [
            "pages",
            "actual_resolution",
            "pages_rejected",
        ]

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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
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

        This is a consequence, not a goal: the generator held the device open
        until it was drained or garbage-collected.  Close-while-reading and
        cancel semantics are Phase 29's HARD-03/HARD-04 and are deliberately
        not folded in here.
        """
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        batch = backend.scan_pages("test:0", settings, page_sink)

        assert dev.close_calls == 1
        assert len(batch.pages) == 1


# ---------------------------------------------------------------------------
# HARD-01: the peak-memory bound, and the one-page-per-sink-call rule
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

    def add(self, image: Image.Image) -> PageRecord:
        """
        Record the page, pass it to the delegate, and record what came back.

        Args:
            image: The one page the backend handed over.

        Returns:
            The delegate's record, unchanged.

        """
        self.added.append(image)
        record = self._delegate.add(image)
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
    HARD-01's bound, proven by counting live page images (D-08).

    The instrument is a ``weakref`` per page the fake hands out, and the three
    obvious alternatives were all measured and rejected:

    - ``tracemalloc`` cannot see this memory at all.  A 26 MB Pillow image adds
      **460 bytes** to the traced total, because the pixels are malloc'd in C
      rather than through the Python allocator.
    - RSS is far too noisy for CI, and answers about the whole process rather
      than about the pages.
    - A hash-based weak set cannot hold Pillow images: ``Image`` defines
      ``__eq__`` without ``__hash__``, so building one raises ``TypeError``.

    The honest high-water mark is **2**, not 1, and the reason is structural:
    ``_acquire_pages``' loop variable still references page *k-1* while page
    *k* is being read.  A ``del`` that made the number 1 would exist only to
    satisfy a test, and it was declined (29-RESEARCH.md Finding 4).

    Which is why the bound is asserted two ways.  ``<= 2`` alone would survive
    a regression that grew the constant; equality between a 3-page run and a
    12-page one is what actually proves independence from page count, and that
    independence -- not the constant -- is the claim HARD-01 makes.

    One trap, recorded because it silently inverts the measurement:
    ``load_feeder()`` makes the device keep a strong reference to every page it
    was loaded with, so the counter would report the *test's* retention rather
    than the backend's, and reads 12 for a 12-page run.  These tests therefore
    let the fake generate its pages, which are already distinguishable by page
    index, and assert that distinctness rather than assuming it.
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
# SANE module boundary (EXC-01)
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
    ``_DuplexMismatch`` gives in ``pipeline.py``: the four facts belong
    together, and a four-column table makes every reader remember an order.
    The immediate cause is ruff's ``PLR0913`` -- the test already takes
    ``monkeypatch`` and now a sink as well, which is six with the columns
    spread out and five with them bundled.

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
    Every python-sane failure leaves the backend as a saneless type (EXC-01).

    python-sane raises ``_sane.error``, ``RuntimeError`` and ``AttributeError``
    with no shared base, and before Phase 28 all three escaped ``scan_pages``,
    ``get_capabilities`` and ``get_devices`` raw, so the CLI printed a traceback
    and the web layer classified the job as UNKNOWN (M-17).  Each call site now
    re-raises as ``ScanError`` naming the device, and the option where there is
    one, with the original message and ``__cause__`` kept (D-08).  A missing
    python-sane is a setup problem, so ``require_sane()`` raises ``ConfigError``
    with an install hint instead (D-05).
    """

    # -- require_sane (D-05) ------------------------------------------------

    def test_require_sane_missing_module_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing python-sane is a one-line ConfigError with the install hint."""
        monkeypatch.setattr(sane_backend_mod, "sane", None)
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

    def test_require_sane_keeps_an_already_loaded_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With sane already bound, require_sane() returns and replaces nothing."""
        module = FakeSaneModule()
        monkeypatch.setattr(sane_backend_mod, "sane", module)

        assert sane_backend_mod.require_sane() is None
        assert sane_backend_mod.sane is module

    def test_ensure_sane_returns_the_patched_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A module patched into ``sane`` is what every SANE call goes through."""
        module = FakeSaneModule()
        monkeypatch.setattr(sane_backend_mod, "sane", module)

        assert sane_backend_mod._ensure_sane() is module

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
        monkeypatch.setattr(sane_backend_mod, "sane", None)
        monkeypatch.setitem(sys.modules, "sane", first)

        assert sane_backend_mod._ensure_sane() is first
        assert sane_backend_mod.sane is None

        monkeypatch.setitem(sys.modules, "sane", second)

        assert sane_backend_mod._ensure_sane() is second

    def test_scanner_package_does_not_offer_the_backend(self) -> None:
        """
        The package exports the abstraction only, never the SANE backend.

        Callers import ``saneless.scanner.sane_backend`` by name, so the
        package itself never has a reason to reach python-sane.
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
        monkeypatch.setattr(scanner_pkg, "sane_backend", sane_backend_mod)

        fresh = importlib.import_module(sane_backend_mod.__name__)

        assert fresh is not sane_backend_mod
        assert fresh.sane is None
        assert "sane" not in sys.modules

    # -- init / open / get_devices / close ----------------------------------

    def test_init_failure_raises_scan_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing sane.init() is a ScanError chained to the SANE error."""
        original = FakeSaneError("Access to resource has been denied")
        monkeypatch.setattr(
            sane_backend_mod, "sane", FakeSaneModule(init_error=original)
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
        """sane.open() failing names the device and chains the original."""
        original = FakeSaneError("Invalid argument")
        monkeypatch.setattr(
            sane_backend_mod, "sane", FakeSaneModule(open_error=original)
        )
        backend = SaneBackend()

        with pytest.raises(ScanError) as exc_info:
            backend.get_capabilities("epson2:libusb:001:004")

        assert (
            str(exc_info.value)
            == "Could not open scanner epson2:libusb:001:004: Invalid argument"
        )
        assert exc_info.value.__cause__ is original

    def test_open_failure_in_scan_pages_raises_scan_error(
        self, monkeypatch: pytest.MonkeyPatch, page_sink: SpooledPageSink
    ) -> None:
        """The same translation applies on the scan path."""
        original = FakeSaneError("Invalid argument")
        monkeypatch.setattr(
            sane_backend_mod, "sane", FakeSaneModule(open_error=original)
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
            sane_backend_mod,
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
        """sane.get_devices() failing is a ScanError chained to the original."""
        original = FakeSaneError("Out of memory")
        monkeypatch.setattr(
            sane_backend_mod, "sane", FakeSaneModule(get_devices_error=original)
        )
        backend = SaneBackend()

        with pytest.raises(ScanError) as exc_info:
            backend.get_devices()

        assert str(exc_info.value) == "Could not list scanners: Out of memory"
        assert exc_info.value.__cause__ is original

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

    # -- option assignment and read-back (D-08) -----------------------------

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
        """get_options() failing while reading capabilities names the device."""
        original = FakeSaneError("Error during device I/O")
        dev = FakeSaneDev()
        dev.fail_call("get_options", original)
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.get_capabilities("test:0")

        assert str(exc_info.value) == (
            "Could not read options from test:0: Error during device I/O"
        )
        assert exc_info.value.__cause__ is original

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
        """The one end-of-feed message maps to FeederEmptyError (Phase 24 D-03)."""
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
