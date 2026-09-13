"""Tests for scanner abstraction layer (ABC + SaneBackend)."""

from __future__ import annotations

import dataclasses
import logging
import os
import time

import pytest
from PIL import Image, ImageDraw

import saneless.scanner.sane_backend as sane_backend_mod
from saneless.exceptions import FeederEmptyError, ScanError
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScanBatch,
    ScannerBackend,
    ScanSettings,
    SourceKind,
    classify_source,
)
from saneless.scanner.sane_backend import GeometryUnit, SaneBackend
from tests.fake_sane import (
    FakeSaneDev,
    FakeSaneError,
    FakeSaneModule,
    build_option_table,
)

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


# D-17 completed: MockSaneDev, MockSaneModule and _FakeSaneDevice used to live
# here.  All three modelled a python-sane that does not exist -- most sharply
# the geometry-less one, which RAISED on an unknown option name where the real
# library stores it silently -- and each disagreement let a shipped defect earn
# a green test (M-32).  There is now exactly one definition of what python-sane
# does, in tests/fake_sane.py, and both this module and test_pipeline.py are
# written against it.
#
# MockBackend below is deliberately NOT one of them: it implements the
# ScannerBackend ABC, which is saneless's own interface, and models no part of
# the sane module or a device handle.


class MockBackend(ScannerBackend):
    """Concrete mock backend to test the ABC contract."""

    def get_devices(self) -> list[DeviceInfo]:
        """Return a single mock device."""
        return [
            DeviceInfo(
                name="mock:device",
                vendor="Mock",
                model="Scanner",
                device_type="scanner",
            ),
        ]

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """Return minimal capabilities for the mock device."""
        return DeviceCapabilities(
            sources=["Flatbed"],
            resolutions=[300],
            modes=["color"],
            raw_options=[],
        )

    def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
        """Return a batch holding a single white test image."""
        return ScanBatch(
            pages=[Image.new("RGB", (100, 100), "white")],
            actual_resolution=settings.resolution,
            pages_rejected=0,
        )


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


class TestMockBackend:
    """Mock backend implementation tests."""

    def test_mock_backend_get_devices(self) -> None:
        """MockBackend.get_devices returns DeviceInfo list."""
        backend = MockBackend()
        devices = backend.get_devices()
        assert len(devices) == 1
        assert isinstance(devices[0], DeviceInfo)

    def test_mock_backend_scan_pages(self) -> None:
        """MockBackend.scan_pages yields PIL Images."""
        backend = MockBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        pages = backend.scan_pages("mock:device", settings).pages
        assert len(pages) == 1
        assert isinstance(pages[0], Image.Image)


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


@pytest.fixture
def fake_sane_module(monkeypatch: pytest.MonkeyPatch) -> FakeSaneModule:
    """
    Patch the one shared fake into sane_backend's module-level ``sane`` name.

    ``_ensure_sane()`` leaves that name None until first use, which is the seam
    that makes the whole approach work; it is kept exactly as it was.

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


class TestSaneBackendInit:
    """SaneBackend initialization tests."""

    def test_sane_backend_init_calls_sane_init(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """SaneBackend constructor calls sane.init() exactly once."""
        assert fake_sane_module.init_call_count == 0
        SaneBackend()
        assert fake_sane_module.init_call_count == 1

    def test_sane_backend_init_calls_sane_init_exactly_once(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """A single SaneBackend instance only triggers one init call."""
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
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """scan_pages opens device, yields image, then closes device."""
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        pages = sane_backend.scan_pages("test:device:001", settings).pages
        assert len(pages) == 1
        assert isinstance(pages[0], Image.Image)
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        assert mock_dev.close_calls

    def test_sane_backend_cancel_before_close(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """Device cancel() is called before close() on normal exit."""
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        sane_backend.scan_pages("test:device:001", settings)
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        assert mock_dev.cancel_calls
        assert mock_dev.close_calls

    def test_sane_backend_cancel_before_close_on_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Device cancel() and close() are called even when the scan raises."""
        # The fault is armed on the device rather than by swapping out its snap
        # method: start() is where a flatbed scan first touches the hardware,
        # and the fake raises from there with the library's own error type.
        dev = FakeSaneDev(start_error=FakeSaneError("scan failed"))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        with pytest.raises(FakeSaneError, match="scan failed"):
            backend.scan_pages("test:0", settings)

        assert dev.cancel_calls
        assert dev.close_calls

    def test_sane_backend_no_progress_callback(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """Verify snap() is called without progress argument (Pitfall #2)."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)

        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        sane_backend.scan_pages("test:device:001", settings)

        # snap() should be called with no arguments (no progress callback)
        assert mock_dev.calls.count("snap") == 1

    @pytest.mark.usefixtures("fake_sane_module")
    def test_sane_backend_validates_source_option(self) -> None:
        """Requesting an unsupported source raises ScanError."""
        backend = SaneBackend()
        settings = ScanSettings(
            source="NonExistentSource", resolution=300, mode="Color"
        )

        with pytest.raises(ScanError, match="NonExistentSource"):
            backend.scan_pages("test:device:001", settings)


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
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """Verify ADF scan via multi_scan yields all 3 pages from the feeder."""
        _ = fake_sane_module  # fixture provides mock device
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        pages = sane_backend.scan_pages("test:device:001", settings).pages
        assert len(pages) == 3
        for page in pages:
            assert isinstance(page, Image.Image)

    def test_adf_scan_uses_multi_scan_not_snap(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """ADF scan calls multi_scan(), not snap()."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)

        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        sane_backend.scan_pages("test:device:001", settings)

        assert mock_dev.calls == _THREE_SHEET_FEEDER_CALLS

    def test_flatbed_still_uses_snap(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """Flatbed scan still uses snap(), not multi_scan()."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)

        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        pages = sane_backend.scan_pages("test:device:001", settings).pages

        assert len(pages) == 1
        assert mock_dev.calls.count("snap") == 1


class TestSaneBackendAutomaticDocumentFeeder:
    """Routing for feeder names that contain no "adf" token (C-06 / D-11)."""

    def test_automatic_document_feeder_yields_all_pages(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """
        Automatic Document Feeder uses multi_scan and returns every page (CTR-04).

        The SANE ``test`` backend names its feeder "Automatic Document Feeder",
        with no "adf" token anywhere in the string. The deleted string-sniffing
        rule in ``sane_backend`` returned False for it, so the flatbed
        ``start()``/``snap()`` branch ran and a ten-page stack produced exactly
        one page. This asserts that ``multi_scan()`` is used instead -- that all
        3 fake pages come back rather than 1, and that ``snap()`` is never
        called. This is the C-06 fix and the phase's one authorised behaviour
        change (D-11); it could not have passed before Phase 21.
        """
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.report_sources(["Flatbed", "Automatic Document Feeder"])

        settings = ScanSettings(
            source="Automatic Document Feeder", resolution=300, mode="Color"
        )
        pages = sane_backend.scan_pages("test:device:001", settings).pages

        assert len(pages) == 3
        for page in pages:
            assert isinstance(page, Image.Image)
        assert mock_dev.calls == _THREE_SHEET_FEEDER_CALLS


class TestSaneBackendDuplex:
    """ADF Duplex scan tests."""

    def test_duplex_scan_yields_pages(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """Verify ADF Duplex scan yields pages pre-interleaved from hardware."""
        _ = fake_sane_module  # fixture provides mock device
        settings = ScanSettings(source="ADF Duplex", resolution=300, mode="Color")
        pages = sane_backend.scan_pages("test:device:001", settings).pages
        assert len(pages) == 3
        for page in pages:
            assert isinstance(page, Image.Image)

    def test_duplex_scan_uses_multi_scan(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """ADF Duplex uses multi_scan(), not snap()."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)

        settings = ScanSettings(source="ADF Duplex", resolution=300, mode="Color")
        sane_backend.scan_pages("test:device:001", settings)

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
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """Multi_scan that yields zero pages raises FeederEmptyError."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")

        with pytest.raises(FeederEmptyError, match="No paper detected in feeder"):
            backend.scan_pages("test:device:001", settings)


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
        self, message: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each measured page-0 fault raises ScanError carrying the SANE text."""
        dev = FakeSaneDev(pages=5, start_error=FakeSaneError(message))
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", _feeder_settings())

        assert message in str(exc_info.value)
        assert "page 1" in str(exc_info.value)
        assert not isinstance(exc_info.value, FeederEmptyError)

    def test_first_page_fault_keeps_the_original_as_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The translated ScanError chains the exception SANE actually raised."""
        original = FakeSaneError("Document feeder jammed")
        dev = FakeSaneDev(pages=5, start_error=original)
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", _feeder_settings())

        assert exc_info.value.__cause__ is original

    def test_mid_stack_jam_names_the_one_based_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A jam on the fourth sheet names page 4, not page 0."""
        dev = FakeSaneDev(
            pages=10,
            start_error=FakeSaneError("Document feeder jammed"),
            start_error_page=3,
        )
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", _feeder_settings())

        assert "Document feeder jammed" in str(exc_info.value)
        assert "page 4" in str(exc_info.value)
        assert not isinstance(exc_info.value, FeederEmptyError)

    def test_out_of_documents_still_means_an_empty_feeder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one converted message is the only path to the feeder message."""
        dev = FakeSaneDev(pages=5, start_error=FakeSaneError(_OUT_OF_DOCUMENTS))
        backend = _backend_with(dev, monkeypatch)

        with pytest.raises(FeederEmptyError, match="No paper detected in feeder"):
            backend.scan_pages("test:0", _feeder_settings())

    def test_a_clean_stack_yields_every_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A three-sheet feeder yields three pages and raises nothing."""
        dev = FakeSaneDev(pages=3)
        backend = _backend_with(dev, monkeypatch)

        pages = backend.scan_pages("test:0", _feeder_settings()).pages

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
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A device that never reports end-of-feed raises ScanError at the cap."""
        cap = sane_backend_mod._MAX_ADF_PAGES
        # Far more sheets than the cap, so the fake never reports end-of-feed
        # and the loop has to be stopped by the cap rather than by the device.
        dev = FakeSaneDev(pages=cap * 100)
        backend = _backend_with(dev, monkeypatch)

        started = time.monotonic()
        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:0", _feeder_settings())
        elapsed = time.monotonic() - started

        assert str(cap) in str(exc_info.value)
        assert not isinstance(exc_info.value, FeederEmptyError)
        # The fake's pages are tiny, so the cap must be reached quickly -- a
        # slow run here would mean the bound is not what stopped the loop.
        assert elapsed < 10.0

    def test_a_maximal_stack_is_not_off_by_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exactly _MAX_ADF_PAGES sheets complete normally and yield in full."""
        cap = sane_backend_mod._MAX_ADF_PAGES
        dev = FakeSaneDev(pages=cap)
        backend = _backend_with(dev, monkeypatch)

        pages = backend.scan_pages("test:0", _feeder_settings()).pages

        assert len(pages) == cap


class TestSaneBackendPageValidation:
    """Inline page validation tests."""

    def test_zero_dimension_page_skipped(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """Page with zero dimensions is skipped with warning."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        zero_img = Image.new("RGB", (0, 0))
        normal_img = _make_content_image()
        mock_dev.load_feeder([zero_img, normal_img])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        pages = backend.scan_pages("test:device:001", settings).pages
        assert len(pages) == 1

    def test_min_file_size_page_skipped(self, fake_sane_module: FakeSaneModule) -> None:
        """Page below MIN_PAGE_BYTES (1x1 pixel) is skipped."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        tiny_img = Image.new("RGB", (1, 1), "red")
        normal_img = _make_content_image()
        mock_dev.load_feeder([tiny_img, normal_img])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        pages = backend.scan_pages("test:device:001", settings).pages
        assert len(pages) == 1

    def test_pure_white_page_survives(self, fake_sane_module: FakeSaneModule) -> None:
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
        pages = backend.scan_pages("test:device:001", settings).pages
        assert len(pages) == 2
        # The blank itself survived, rather than the content page arriving twice.
        assert pages[0].convert("L").getextrema() == (255, 255)

    def test_pure_black_page_survives(self, fake_sane_module: FakeSaneModule) -> None:
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
        pages = backend.scan_pages("test:device:001", settings).pages
        assert len(pages) == 2
        assert pages[0].convert("L").getextrema() == (0, 0)

    def test_normal_content_page_passes(self, fake_sane_module: FakeSaneModule) -> None:
        """Image with mixed content passes all validation checks."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        content_img = _make_content_image()
        mock_dev.load_feeder([content_img])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        pages = backend.scan_pages("test:device:001", settings).pages
        assert len(pages) == 1

    def test_exif_stripped(self, fake_sane_module: FakeSaneModule) -> None:
        """EXIF data is removed from scanned images before yielding."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        img = _make_content_image()
        # Inject fake EXIF data
        img.info["exif"] = b"fake-exif-data"
        mock_dev.load_feeder([img])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        pages = backend.scan_pages("test:device:001", settings).pages
        assert len(pages) == 1
        assert "exif" not in pages[0].info

    def test_exif_stripped_flatbed(self, fake_sane_module: FakeSaneModule) -> None:
        """EXIF data is stripped from flatbed scans too."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        img = Image.new("RGB", (100, 100), "white")
        img.info["exif"] = b"fake-exif-data"
        mock_dev.load_feeder([img])

        backend = SaneBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        pages = backend.scan_pages("test:device:001", settings).pages
        assert len(pages) == 1
        assert "exif" not in pages[0].info


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
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """A 0x0 image is not a page, however successfully it was returned."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([Image.new("RGB", (0, 0))])

        backend = SaneBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        with pytest.raises(ScanError, match="unreadable"):
            backend.scan_pages("test:device:001", settings)

    def test_a_sheet_below_the_byte_floor_raises(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """A 10x10 RGB page is 300 bytes, far below the floor."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([Image.new("RGB", (10, 10), "white")])

        backend = SaneBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        with pytest.raises(ScanError, match="unreadable"):
            backend.scan_pages("test:device:001", settings)

    def test_a_readable_sheet_still_reports_no_rejections(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """The check must not start counting good flatbed pages as rejects."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([_make_content_image()])

        backend = SaneBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")
        batch = backend.scan_pages("test:device:001", settings)

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
        self, fake_sane_module: FakeSaneModule, caplog: pytest.LogCaptureFixture
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
            pages = backend.scan_pages("test:device:001", settings).pages

        assert len(pages) == 4
        skips = [r for r in caplog.records if "skipping" in r.getMessage()]
        assert len(skips) == 1
        assert "Page 3" in skips[0].getMessage()

    def test_a_wholly_rejected_batch_raises_rather_than_yielding_nothing(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """Three unreadable sheets raise ScanError naming how many were fed."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([Image.new("RGB", (0, 0)) for _ in range(3)])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")

        with pytest.raises(ScanError) as exc_info:
            backend.scan_pages("test:device:001", settings)

        assert "3" in str(exc_info.value)
        # Distinct condition from an empty feeder: paper *was* fed, and the
        # operator needs to be told it was unreadable rather than absent.
        assert not isinstance(exc_info.value, FeederEmptyError)

    def test_a_zero_page_feeder_still_raises_feeder_empty(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """No paper at all stays FeederEmptyError, not the all-rejected error."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")

        with pytest.raises(FeederEmptyError, match="No paper detected in feeder"):
            backend.scan_pages("test:device:001", settings)

    def test_a_clean_stack_logs_no_skip_warning(
        self, fake_sane_module: FakeSaneModule, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Five readable sheets yield five pages and no skip warning at all."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([_make_content_image() for _ in range(5)])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        with caplog.at_level(logging.WARNING):
            pages = backend.scan_pages("test:device:001", settings).pages

        assert len(pages) == 5
        assert not [r for r in caplog.records if "skipping" in r.getMessage()]


class TestAutoSourceRouting:
    """Auto source conditional routing via auto_source_mode."""

    def test_auto_source_adf_routes_to_adf_path(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """scan_pages with source='Auto' and auto_source_mode='adf' uses ADF path."""
        # Add "Auto" to available sources
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.report_sources(["Flatbed", "ADF", "Auto"])
        settings = ScanSettings(
            source="Auto", resolution=300, mode="Color", auto_source_mode="adf"
        )
        pages = sane_backend.scan_pages("test:device:001", settings).pages
        # ADF path yields 3 pages via multi_scan
        assert len(pages) == 3

    def test_auto_source_flatbed_routes_to_flatbed_path(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """scan_pages with source='Auto' and auto_source_mode='flatbed' uses flatbed path."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.report_sources(["Flatbed", "ADF", "Auto"])
        settings = ScanSettings(
            source="Auto", resolution=300, mode="Color", auto_source_mode="flatbed"
        )
        pages = sane_backend.scan_pages("test:device:001", settings).pages
        # Flatbed path yields 1 page via snap
        assert len(pages) == 1
        assert mock_dev.calls.count("snap") == 1

    def test_explicit_adf_ignores_auto_source_mode(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """Explicit ADF source ignores auto_source_mode setting."""
        settings = ScanSettings(
            source="ADF", resolution=300, mode="Color", auto_source_mode="flatbed"
        )
        pages = sane_backend.scan_pages("test:device:001", settings).pages
        # ADF always uses ADF path regardless of auto_source_mode
        assert len(pages) == 3

    def test_explicit_flatbed_ignores_auto_source_mode(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """Explicit Flatbed source ignores auto_source_mode setting."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", auto_source_mode="adf"
        )
        pages = sane_backend.scan_pages("test:device:001", settings).pages
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

        pages = sane_backend.scan_pages("test:device:001", settings).pages

        assert classify_source(reported) is SourceKind.AUTO
        assert len(pages) == 3
        assert mock_dev.calls == _THREE_SHEET_FEEDER_CALLS

    @pytest.mark.parametrize("reported", ["Auto", "auto", "  AUTO  "])
    def test_auto_still_honours_flatbed_routing(
        self,
        sane_backend: SaneBackend,
        fake_sane_module: FakeSaneModule,
        reported: str,
    ) -> None:
        """The override is consulted, not merely coincidentally agreed with."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.report_sources(["Flatbed", "ADF", reported])
        settings = ScanSettings(
            source=reported, resolution=300, mode="Color", auto_source_mode="flatbed"
        )

        pages = sane_backend.scan_pages("test:device:001", settings).pages

        assert len(pages) == 1
        assert mock_dev.calls.count("snap") == 1

    def test_a_long_feeder_name_never_takes_the_auto_override(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
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

        pages = sane_backend.scan_pages("test:device:001", settings).pages

        assert classify_source("Automatic Document Feeder") is SourceKind.FEEDER
        assert len(pages) == 3
        assert mock_dev.calls == _THREE_SHEET_FEEDER_CALLS

    def test_an_unrecognised_source_takes_the_single_page_path(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
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

        pages = sane_backend.scan_pages("test:device:001", settings).pages

        assert classify_source("Mystery Tray") is SourceKind.UNKNOWN
        assert len(pages) == 1
        assert mock_dev.calls.count("snap") == 1


class TestSaneBackendPerPageTimeout:
    """Per-page timeout tests."""

    def test_page_timeout_raises_scan_error(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """A page that takes too long raises ScanError naming the timeout."""
        # A merely slow scanner is a real condition and the per-page timeout
        # exists for exactly it, so the delay is armed on the one shared device
        # rather than by a bespoke blocking iterator -- which was a device
        # double of its own, and is what D-17 leaves only one of.
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.set_page_delay(2.0)

        backend = SaneBackend()

        with pytest.raises(ScanError, match="timed out"):
            backend._scan_adf_pages(mock_dev, timeout_per_page=0.1)

    def test_pages_within_timeout_succeed(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """Pages acquired within timeout proceed normally."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        assert isinstance(mock_dev, FakeSaneDev)

        backend = SaneBackend()
        pages, _ = backend._scan_adf_pages(mock_dev, timeout_per_page=5.0)
        assert len(pages) == 3


class TestSaneBackendADFCleanup:
    """ADF cleanup (cancel/close) tests."""

    def test_cancel_called_after_adf_scan(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
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
        sane_backend.scan_pages("test:device:001", settings)
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        assert mock_dev.cancel_calls
        assert mock_dev.close_calls

    def test_cancel_called_on_adf_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
            backend.scan_pages("test:0", settings)

        assert dev.cancel_calls
        assert dev.close_calls


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
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """When paper_size='a4', dev.br_x=210.0 and dev.br_y=297.0."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([Image.new("RGB", (2500, 3600), "white")])
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )
        sane_backend.scan_pages("test:device:001", settings)
        assert mock_dev.br_x == 210.0
        assert mock_dev.br_y == 297.0
        assert mock_dev.tl_x == 0.0
        assert mock_dev.tl_y == 0.0

    def test_full_no_geometry(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """When paper_size='full', no geometry option is assigned at all."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="full"
        )

        sane_backend.scan_pages("test:device:001", settings)

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
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """When paper_size='letter', dev.br_x=215.9 and dev.br_y=279.4."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([Image.new("RGB", (2600, 3400), "white")])
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="letter"
        )
        sane_backend.scan_pages("test:device:001", settings)
        # SANE_Fixed is a 16.16 fixed-point integer, so letter's 215.9 mm is not
        # exactly representable and reads back differing in the low bits without
        # the device having clamped anything. The deleted double stored floats
        # verbatim and hid that entirely -- which is precisely why D-19 compares
        # scan areas with a tolerance instead of for equality.
        assert mock_dev.br_x == pytest.approx(215.9, abs=1e-4)
        assert mock_dev.br_y == pytest.approx(279.4, abs=1e-4)

    def test_geometry_failure_still_completes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A device without geometry options still completes the scan."""
        backend = _backend_with(_geometry_less_device(), monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )
        pages = backend.scan_pages("test:0", settings).pages
        assert len(pages) == 1


class TestPaperSizeCropFallback:
    """Pillow crop fallback when geometry options are unavailable."""

    def test_crop_fallback_when_geometry_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When geometry is absent, the scanned image is cropped to A4."""
        backend = _backend_with(_geometry_less_device(), monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )
        pages = backend.scan_pages("test:0", settings).pages
        assert len(pages) == 1
        assert pages[0].size == _A4_AT_300_DPI

    def test_full_no_crop(
        self, sane_backend: SaneBackend, fake_sane_module: FakeSaneModule
    ) -> None:
        """When paper_size='full', image is yielded at original size."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        original_img = Image.new("RGB", (5000, 6000), "white")
        mock_dev.load_feeder([original_img])
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="full"
        )
        pages = sane_backend.scan_pages("test:device:001", settings).pages
        assert len(pages) == 1
        assert pages[0].size == (5000, 6000)

    def test_adf_crop_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ADF pages are cropped when geometry options are unavailable."""
        backend = _backend_with(_geometry_less_device(pages=2), monkeypatch)
        settings = ScanSettings(
            source="Automatic Document Feeder",
            resolution=300,
            mode="Color",
            paper_size="a4",
        )
        pages = backend.scan_pages("test:0", settings).pages
        assert len(pages) == 2
        for page in pages:
            assert page.size == _A4_AT_300_DPI


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
        self, monkeypatch: pytest.MonkeyPatch
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

        pages = backend.scan_pages("test:0", settings).pages

        assert pages[0].size == _A4_AT_300_DPI

    def test_the_missing_option_is_named_in_the_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One absent option is enough, and the warning says which one."""
        dev = FakeSaneDev(options=build_option_table(omit=("br-y",)), pages=1)
        dev.set_page_size(3000, 4000)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
            pages = backend.scan_pages("test:0", settings).pages

        assert [m for m in _warning_messages(caplog) if "br-y" in m]
        assert pages[0].size == _A4_AT_300_DPI

    def test_a_rejected_geometry_assignment_logs_the_exception(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
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
            pages = backend.scan_pages("test:0", settings).pages

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
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A device reporting UNIT_MM gets A4's millimetres unchanged."""
        dev = _device_reporting_unit(GeometryUnit.UNIT_MM, (0.0, 300.0, 1.0))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        pages = backend.scan_pages("test:0", settings).pages

        assert dev.br_x == 210.0
        assert dev.br_y == 297.0
        # Geometry was set on the device, so the page is not cropped as well.
        assert pages[0].size == (3000, 4000)

    def test_pixels_use_the_resolution_read_back_from_the_device(
        self, monkeypatch: pytest.MonkeyPatch
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

        backend.scan_pages("test:0", settings)

        assert dev.br_x == pytest.approx(210.0 * 1200 / 25.4)
        assert dev.br_y == pytest.approx(297.0 * 1200 / 25.4)

    @pytest.mark.parametrize("unit", sorted(set(GeometryUnit) - _CONVERTIBLE_UNITS))
    def test_an_unconvertible_unit_falls_through_to_the_crop(
        self,
        unit: GeometryUnit,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The other five units warn by name and leave the crop to Pillow."""
        dev = _device_reporting_unit(unit)
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
            pages = backend.scan_pages("test:0", settings).pages

        assert [m for m in _warning_messages(caplog) if unit.name in m]
        assert pages[0].size == _A4_AT_300_DPI

    def test_a_unit_code_outside_sane_does_not_crash_the_scan(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
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
            pages = backend.scan_pages("test:0", settings).pages

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
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A 200 mm device asked for A4 warns with both values and crops."""
        dev = _device_reporting_unit(GeometryUnit.UNIT_MM, (0.0, 200.0, 1.0))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
            pages = backend.scan_pages("test:0", settings).pages

        # The warning has to name what was asked for AND what was got, or the
        # operator cannot tell which of the two is wrong.
        assert [m for m in _warning_messages(caplog) if "210" in m and "200" in m]
        assert pages[0].size == _A4_AT_300_DPI

    def test_a_clamped_top_left_falls_through_to_the_crop(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
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
            pages = backend.scan_pages("test:0", settings).pages

        # br really was honoured -- only tl moved, which is what makes the far
        # corner alone an insufficient test rather than a redundant one.
        assert dev.tl_x == pytest.approx(10.0)
        assert dev.br_x == pytest.approx(210.0)
        # 210 requested, 200 of box actually obtained.
        assert [m for m in _warning_messages(caplog) if "210" in m and "200" in m]
        assert pages[0].size == _A4_AT_300_DPI

    def test_a_comfortable_range_is_not_reported_as_clamped(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A device that honours the area sets it and does not crop as well."""
        dev = _device_reporting_unit(GeometryUnit.UNIT_MM, (0.0, 300.0, 1.0))
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="Color", paper_size="a4"
        )

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
            pages = backend.scan_pages("test:0", settings).pages

        assert not [m for m in _warning_messages(caplog) if "clamped" in m.lower()]
        assert pages[0].size == (3000, 4000)

    def test_a_fixed_point_round_trip_is_within_tolerance(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
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
            pages = backend.scan_pages("test:0", settings).pages

        # The round trip really is inexact -- this is what makes the tolerance
        # load-bearing rather than decorative.
        assert dev.br_x != 215.9
        assert dev.br_x == pytest.approx(215.9, abs=1e-4)
        assert not [m for m in _warning_messages(caplog) if "clamped" in m.lower()]
        assert pages[0].size == (3000, 4000)

    def test_the_crop_uses_the_resolution_the_device_chose(
        self, monkeypatch: pytest.MonkeyPatch
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

        pages = backend.scan_pages("test:0", settings).pages

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
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The order the device observes is source, then mode, then resolution."""
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        backend.scan_pages("test:0", settings)

        assert dev.assignments == ["source", "mode", "resolution"]

    def test_a_device_without_a_source_option_is_still_configured(
        self, monkeypatch: pytest.MonkeyPatch
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

        backend.scan_pages("test:0", settings)

        assert dev.assignments == ["mode", "resolution"]
        assert dev.resolution == 300.0

    def test_source_first_keeps_resolution_inside_a_narrowed_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
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

        backend.scan_pages("test:0", settings)

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
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Requesting 5000 on a 1200 dpi device names both numbers."""
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=5000, mode="Color")

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
            backend.scan_pages("test:0", settings)

        assert [m for m in self._warnings(caplog) if "5000" in m and "1200" in m]

    def test_the_read_back_value_is_an_int_not_the_device_float(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The device returns 1200.0; what the backend carries is 1200."""
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=5000, mode="Color")

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
            backend.scan_pages("test:0", settings)

        warnings = self._warnings(caplog)
        assert [m for m in warnings if "1200" in m]
        assert not [m for m in warnings if "1200.0" in m]
        assert isinstance(dev.resolution, float)

    def test_an_honoured_resolution_logs_no_mismatch_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A resolution the device accepts unchanged is not worth a warning."""
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
            backend.scan_pages("test:0", settings)

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
        Three fields, in order, and no per-page structure.

        The object is deliberately minimal.  Phase 29's HARD-01 owns the
        ordered per-page record design, and a batch that grew page-level detail
        here would quietly pre-empt it.
        """
        assert [field.name for field in dataclasses.fields(ScanBatch)] == [
            "pages",
            "actual_resolution",
            "pages_rejected",
        ]

    def test_the_batch_is_not_an_iterator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What comes back is a record, not something to call ``list()`` on."""
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        batch = backend.scan_pages("test:0", settings)

        assert isinstance(batch, ScanBatch)
        assert not hasattr(batch, "__next__")

    def test_a_clean_five_page_stack_rejects_nothing(
        self, fake_sane_module: FakeSaneModule
    ) -> None:
        """Five readable sheets are five pages and a zero rejection count."""
        mock_dev = fake_sane_module.open(_TEST_DEVICE)
        mock_dev.load_feeder([_make_content_image() for _ in range(5)])

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="Color")
        batch = backend.scan_pages("test:device:001", settings)

        assert len(batch.pages) == 5
        assert batch.pages_rejected == 0

    def test_an_unreadable_sheet_is_counted_rather_than_vanishing(
        self, fake_sane_module: FakeSaneModule
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
        batch = backend.scan_pages("test:device:001", settings)

        assert len(batch.pages) == 4
        assert batch.pages_rejected == 1

    def test_the_batch_reports_the_resolution_the_device_chose(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A device clamping 5000 to 1200 reports 1200, as a whole number."""
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=5000, mode="Color")

        batch = backend.scan_pages("test:0", settings)

        assert batch.actual_resolution == 1200
        assert isinstance(batch.actual_resolution, int)

    def test_a_flatbed_scan_carries_both_facts_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The single-page path populates the same two facts as the feeder."""
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=300, mode="Color")

        batch = backend.scan_pages("test:0", settings)

        assert len(batch.pages) == 1
        assert batch.actual_resolution == 300
        assert batch.pages_rejected == 0

    def test_the_device_is_closed_by_the_time_the_batch_returns(
        self, monkeypatch: pytest.MonkeyPatch
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

        batch = backend.scan_pages("test:0", settings)

        assert dev.close_calls == 1
        assert len(batch.pages) == 1
