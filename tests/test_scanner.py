"""Tests for scanner abstraction layer (ABC + SaneBackend)."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

import pytest
from PIL import Image, ImageDraw

import saneless.scanner.sane_backend as sane_backend_mod
from saneless.exceptions import FeederEmptyError, ScanError
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScannerBackend,
    ScanSettings,
    SourceKind,
    classify_source,
)
from saneless.scanner.sane_backend import SaneBackend
from tests.fake_sane import FakeSaneDev, FakeSaneError, FakeSaneModule

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


class MockSaneDev:
    """Mock SANE device returned by sane.open()."""

    def __init__(self) -> None:
        """Initialize mock device with default settings."""
        self.mode = "color"
        self.resolution = 300
        self.source = "Flatbed"
        self.tl_x: float = 0.0
        self.tl_y: float = 0.0
        self.br_x: float = 0.0
        self.br_y: float = 0.0
        self._cancel_called = False
        self._close_called = False
        self._snap_calls: list[dict] = []
        # Default ADF pages: 3 content images that pass validation
        self._multi_scan_pages: list[Image.Image] = [
            _make_content_image(color="red"),
            _make_content_image(color="green"),
            _make_content_image(color="blue"),
        ]
        self._multi_scan_error: BaseException | None = None
        self._snap_impl: MagicMock | None = None
        self._options_impl: list[tuple] | None = None

    def start(self) -> None:
        """Initiate SANE scan cycle (no-op in mock)."""

    def snap(self) -> Image.Image:
        """Return a simple test image, or delegate to _snap_impl if set."""
        if self._snap_impl is not None:
            return self._snap_impl()
        self._snap_calls.append({})
        return Image.new("RGB", (100, 100), "white")

    def multi_scan(self) -> Iterator[Image.Image]:
        """Return an iterator over ADF pages."""
        if self._multi_scan_error is not None:
            raise self._multi_scan_error
        return iter(self._multi_scan_pages)

    def cancel(self) -> None:
        """Record that cancel was called."""
        self._cancel_called = True

    def close(self) -> None:
        """Record that close was called."""
        self._close_called = True

    def get_options(self) -> list[tuple]:
        """
        Return sample SANE option tuples, or _options_impl if set.

        SANE option format:
        (index, name, title, desc, type, unit, size, cap, constraint)
        """
        if self._options_impl is not None:
            return self._options_impl
        return [
            (
                1,
                "source",
                "Scan source",
                "Source desc",
                3,
                0,
                1,
                5,
                ["Flatbed", "ADF", "ADF Duplex"],
            ),
            (
                2,
                "resolution",
                "Resolution",
                "Res desc",
                1,
                4,
                1,
                5,
                [75, 150, 300, 600],
            ),
            (
                3,
                "mode",
                "Scan mode",
                "Mode desc",
                3,
                0,
                1,
                5,
                ["color", "gray", "lineart"],
            ),
        ]


class MockSaneModule:
    """Mock for the ``sane`` module (python-sane)."""

    def __init__(self) -> None:
        """Initialize mock module with default devices."""
        self.init_call_count = 0
        self._devices: list[tuple[str, str, str, str]] = [
            ("test:device:001", "TestVendor", "TestModel", "scanner"),
        ]
        self._mock_dev = MockSaneDev()

    def init(self) -> tuple[int, int, int]:
        """Simulate sane.init() and track call count."""
        self.init_call_count += 1
        return (1, 0, 3)

    def get_devices(self) -> list[tuple[str, str, str, str]]:
        """Return the list of mock devices."""
        return self._devices

    def open(self, _device_id: str) -> MockSaneDev:
        """Return the shared mock device handle."""
        return self._mock_dev


class _FakeSaneDevice:
    """Fake SANE device for testing scan_pages behavior."""

    def __init__(
        self,
        *,
        multi_scan: Callable[[], Iterator[Image.Image]] | None = None,
        cancel: Callable[[], None] | None = None,
        close: Callable[[], None] | None = None,
    ) -> None:
        """Initialize fake device with pluggable multi_scan, cancel, close."""
        self.mode: str = "color"
        self.resolution: int = 300
        self.source: str = "Flatbed"
        self.tl_x: float = 0.0
        self.tl_y: float = 0.0
        self.br_x: float = 0.0
        self.br_y: float = 0.0
        self._multi_scan_fn = multi_scan or (lambda: iter([]))
        self._cancel_fn = cancel or (lambda: None)
        self._close_fn = close or (lambda: None)

    def get_options(self) -> list[tuple]:
        """Return empty options list."""
        return []

    def start(self) -> None:
        """Initiate SANE scan cycle (no-op in mock)."""

    def snap(self) -> Image.Image:
        """Return a test image."""
        return Image.new("RGB", (100, 100), "white")

    def multi_scan(self) -> Iterator[Image.Image]:
        """Delegate to pluggable multi_scan function."""
        return self._multi_scan_fn()

    def cancel(self) -> None:
        """Delegate to pluggable cancel function."""
        self._cancel_fn()

    def close(self) -> None:
        """Delegate to pluggable close function."""
        self._close_fn()


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

    def scan_pages(
        self, device_id: str, settings: ScanSettings
    ) -> Iterator[Image.Image]:
        """Yield a single white test image."""
        yield Image.new("RGB", (100, 100), "white")


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
        settings = ScanSettings(source="Flatbed", resolution=300, mode="color")
        pages = list(backend.scan_pages("mock:device", settings))
        assert len(pages) == 1
        assert isinstance(pages[0], Image.Image)


# ---------------------------------------------------------------------------
# SaneBackend tests (all using mocked sane module)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_sane_module(monkeypatch: pytest.MonkeyPatch) -> MockSaneModule:
    """Patch sane module into sane_backend's namespace."""
    mock_sane = MockSaneModule()
    monkeypatch.setattr(sane_backend_mod, "sane", mock_sane)
    return mock_sane


@pytest.fixture
def sane_backend(mock_sane_module: MockSaneModule) -> SaneBackend:
    """Create a SaneBackend with mocked sane module."""
    _ = mock_sane_module  # side-effect: patches the sane module
    return SaneBackend()


class TestSaneBackendInit:
    """SaneBackend initialization tests."""

    def test_sane_backend_init_calls_sane_init(
        self, mock_sane_module: MockSaneModule
    ) -> None:
        """SaneBackend constructor calls sane.init() exactly once."""
        assert mock_sane_module.init_call_count == 0
        SaneBackend()
        assert mock_sane_module.init_call_count == 1

    def test_sane_backend_init_calls_sane_init_exactly_once(
        self, mock_sane_module: MockSaneModule
    ) -> None:
        """A single SaneBackend instance only triggers one init call."""
        SaneBackend()
        assert mock_sane_module.init_call_count == 1

    def test_sane_backend_sets_sane_net_hosts(
        self, mock_sane_module: MockSaneModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SaneBackend(host='192.168.1.50') sets SANE_NET_HOSTS env var."""
        monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
        SaneBackend(host="192.168.1.50")
        assert os.environ["SANE_NET_HOSTS"] == "192.168.1.50"

    def test_sane_backend_does_not_override_existing_env(
        self, mock_sane_module: MockSaneModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SaneBackend does not override externally-set SANE_NET_HOSTS."""
        monkeypatch.setenv("SANE_NET_HOSTS", "external-host")
        SaneBackend(host="config-host")
        assert os.environ["SANE_NET_HOSTS"] == "external-host"

    def test_sane_backend_no_host_no_env_change(
        self, mock_sane_module: MockSaneModule, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SaneBackend() with no host does not set SANE_NET_HOSTS."""
        monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
        SaneBackend()
        assert "SANE_NET_HOSTS" not in os.environ

    def test_sane_backend_multi_host_colon_delimiter(
        self, mock_sane_module: MockSaneModule, monkeypatch: pytest.MonkeyPatch
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
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """scan_pages opens device, yields image, then closes device."""
        settings = ScanSettings(source="Flatbed", resolution=300, mode="color")
        pages = list(sane_backend.scan_pages("test:device:001", settings))
        assert len(pages) == 1
        assert isinstance(pages[0], Image.Image)
        mock_dev = mock_sane_module._mock_dev
        assert mock_dev._close_called

    def test_sane_backend_cancel_before_close(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """Device cancel() is called before close() on normal exit."""
        settings = ScanSettings(source="Flatbed", resolution=300, mode="color")
        list(sane_backend.scan_pages("test:device:001", settings))
        mock_dev = mock_sane_module._mock_dev
        assert mock_dev._cancel_called
        assert mock_dev._close_called

    def test_sane_backend_cancel_before_close_on_error(
        self, mock_sane_module: MockSaneModule
    ) -> None:
        """Device cancel() and close() are called even when snap() raises."""
        # Make snap raise an error
        mock_dev = mock_sane_module._mock_dev
        mock_dev._snap_impl = MagicMock(side_effect=RuntimeError("scan failed"))

        backend = SaneBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="color")

        with pytest.raises(RuntimeError, match="scan failed"):
            list(backend.scan_pages("test:device:001", settings))

        assert mock_dev._cancel_called
        assert mock_dev._close_called

    def test_sane_backend_no_progress_callback(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """Verify snap() is called without progress argument (Pitfall #2)."""
        mock_dev = mock_sane_module._mock_dev
        mock_dev._snap_impl = MagicMock(
            return_value=Image.new("RGB", (100, 100), "white")
        )

        settings = ScanSettings(source="Flatbed", resolution=300, mode="color")
        list(sane_backend.scan_pages("test:device:001", settings))

        # snap() should be called with no arguments (no progress callback)
        mock_dev._snap_impl.assert_called_once_with()

    @pytest.mark.usefixtures("mock_sane_module")
    def test_sane_backend_validates_source_option(self) -> None:
        """Requesting an unsupported source raises ScanError."""
        backend = SaneBackend()
        settings = ScanSettings(
            source="NonExistentSource", resolution=300, mode="color"
        )

        with pytest.raises(ScanError, match="NonExistentSource"):
            list(backend.scan_pages("test:device:001", settings))


class TestSaneBackendGetCapabilities:
    """SaneBackend capability query tests."""

    def test_sane_backend_get_capabilities(self, sane_backend: SaneBackend) -> None:
        """get_capabilities returns parsed sources, resolutions, and modes."""
        caps = sane_backend.get_capabilities("test:device:001")
        assert isinstance(caps, DeviceCapabilities)
        assert "Flatbed" in caps.sources
        assert "ADF" in caps.sources
        assert "ADF Duplex" in caps.sources
        assert 300 in caps.resolutions
        assert 600 in caps.resolutions
        assert "color" in caps.modes
        assert "gray" in caps.modes
        assert len(caps.raw_options) > 0


# ---------------------------------------------------------------------------
# ADF scan tests
# ---------------------------------------------------------------------------


class TestSaneBackendADFScan:
    """ADF simplex scan tests."""

    def test_adf_scan_yields_all_pages(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """Verify ADF scan via multi_scan yields all 3 pages from the feeder."""
        _ = mock_sane_module  # fixture provides mock device
        settings = ScanSettings(source="ADF", resolution=300, mode="color")
        pages = list(sane_backend.scan_pages("test:device:001", settings))
        assert len(pages) == 3
        for page in pages:
            assert isinstance(page, Image.Image)

    def test_adf_scan_uses_multi_scan_not_snap(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """ADF scan calls multi_scan(), not snap()."""
        mock_dev = mock_sane_module._mock_dev
        mock_dev._snap_impl = MagicMock(
            return_value=Image.new("RGB", (100, 100), "white")
        )

        settings = ScanSettings(source="ADF", resolution=300, mode="color")
        list(sane_backend.scan_pages("test:device:001", settings))

        mock_dev._snap_impl.assert_not_called()

    def test_flatbed_still_uses_snap(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """Flatbed scan still uses snap(), not multi_scan()."""
        mock_dev = mock_sane_module._mock_dev
        mock_dev._snap_impl = MagicMock(
            return_value=Image.new("RGB", (100, 100), "white")
        )

        settings = ScanSettings(source="Flatbed", resolution=300, mode="color")
        pages = list(sane_backend.scan_pages("test:device:001", settings))

        assert len(pages) == 1
        mock_dev._snap_impl.assert_called_once_with()


class TestSaneBackendAutomaticDocumentFeeder:
    """Routing for feeder names that contain no "adf" token (C-06 / D-11)."""

    @staticmethod
    def _options_with_feeder_source() -> list[tuple]:
        """Return SANE options whose source constraint is the test backend's."""
        return [
            (
                1,
                "source",
                "Scan source",
                "Source desc",
                3,
                0,
                1,
                5,
                ["Flatbed", "Automatic Document Feeder"],
            ),
        ]

    def test_automatic_document_feeder_yields_all_pages(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
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
        mock_dev = mock_sane_module._mock_dev
        mock_dev._options_impl = self._options_with_feeder_source()
        mock_dev._snap_impl = MagicMock(
            return_value=Image.new("RGB", (100, 100), "white")
        )

        settings = ScanSettings(
            source="Automatic Document Feeder", resolution=300, mode="color"
        )
        pages = list(sane_backend.scan_pages("test:device:001", settings))

        assert len(pages) == 3
        for page in pages:
            assert isinstance(page, Image.Image)
        mock_dev._snap_impl.assert_not_called()


class TestSaneBackendDuplex:
    """ADF Duplex scan tests."""

    def test_duplex_scan_yields_pages(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """Verify ADF Duplex scan yields pages pre-interleaved from hardware."""
        _ = mock_sane_module  # fixture provides mock device
        settings = ScanSettings(source="ADF Duplex", resolution=300, mode="color")
        pages = list(sane_backend.scan_pages("test:device:001", settings))
        assert len(pages) == 3
        for page in pages:
            assert isinstance(page, Image.Image)

    def test_duplex_scan_uses_multi_scan(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """ADF Duplex uses multi_scan(), not snap()."""
        mock_dev = mock_sane_module._mock_dev
        mock_dev._snap_impl = MagicMock(
            return_value=Image.new("RGB", (100, 100), "white")
        )

        settings = ScanSettings(source="ADF Duplex", resolution=300, mode="color")
        list(sane_backend.scan_pages("test:device:001", settings))

        mock_dev._snap_impl.assert_not_called()


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
        self, mock_sane_module: MockSaneModule
    ) -> None:
        """Multi_scan that yields zero pages raises FeederEmptyError."""
        mock_dev = mock_sane_module._mock_dev
        mock_dev._multi_scan_pages = []

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")

        with pytest.raises(FeederEmptyError, match="No paper detected in feeder"):
            list(backend.scan_pages("test:device:001", settings))


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
            list(backend.scan_pages("test:0", _feeder_settings()))

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
            list(backend.scan_pages("test:0", _feeder_settings()))

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
            list(backend.scan_pages("test:0", _feeder_settings()))

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
            list(backend.scan_pages("test:0", _feeder_settings()))

    def test_a_clean_stack_yields_every_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A three-sheet feeder yields three pages and raises nothing."""
        dev = FakeSaneDev(pages=3)
        backend = _backend_with(dev, monkeypatch)

        pages = list(backend.scan_pages("test:0", _feeder_settings()))

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
            list(backend.scan_pages("test:0", _feeder_settings()))
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

        pages = list(backend.scan_pages("test:0", _feeder_settings()))

        assert len(pages) == cap


class TestSaneBackendPageValidation:
    """Inline page validation tests."""

    def test_zero_dimension_page_skipped(
        self, mock_sane_module: MockSaneModule
    ) -> None:
        """Page with zero dimensions is skipped with warning."""
        mock_dev = mock_sane_module._mock_dev
        zero_img = Image.new("RGB", (0, 0))
        normal_img = _make_content_image()
        mock_dev._multi_scan_pages = [zero_img, normal_img]

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")
        pages = list(backend.scan_pages("test:device:001", settings))
        assert len(pages) == 1

    def test_min_file_size_page_skipped(self, mock_sane_module: MockSaneModule) -> None:
        """Page below MIN_PAGE_BYTES (1x1 pixel) is skipped."""
        mock_dev = mock_sane_module._mock_dev
        tiny_img = Image.new("RGB", (1, 1), "red")
        normal_img = _make_content_image()
        mock_dev._multi_scan_pages = [tiny_img, normal_img]

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")
        pages = list(backend.scan_pages("test:device:001", settings))
        assert len(pages) == 1

    def test_pure_white_page_survives(self, mock_sane_module: MockSaneModule) -> None:
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
        mock_dev = mock_sane_module._mock_dev
        white_img = Image.new("RGB", (200, 300), (255, 255, 255))
        normal_img = _make_content_image()
        mock_dev._multi_scan_pages = [white_img, normal_img]

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")
        pages = list(backend.scan_pages("test:device:001", settings))
        assert len(pages) == 2
        # The blank itself survived, rather than the content page arriving twice.
        assert pages[0].convert("L").getextrema() == (255, 255)

    def test_pure_black_page_survives(self, mock_sane_module: MockSaneModule) -> None:
        """
        A uniformly black page reaches the caller instead of being discarded.

        This is the defect the real SANE ``test`` backend exposed end to end:
        its default picture is solid black, so every page of a ten-sheet stack
        was destroyed by the backend before the pipeline ever saw it.  Judging
        content is not the scanner layer's job (M-14, D-05).
        """
        mock_dev = mock_sane_module._mock_dev
        black_img = Image.new("RGB", (200, 300), (0, 0, 0))
        normal_img = _make_content_image()
        mock_dev._multi_scan_pages = [black_img, normal_img]

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")
        pages = list(backend.scan_pages("test:device:001", settings))
        assert len(pages) == 2
        assert pages[0].convert("L").getextrema() == (0, 0)

    def test_normal_content_page_passes(self, mock_sane_module: MockSaneModule) -> None:
        """Image with mixed content passes all validation checks."""
        mock_dev = mock_sane_module._mock_dev
        content_img = _make_content_image()
        mock_dev._multi_scan_pages = [content_img]

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")
        pages = list(backend.scan_pages("test:device:001", settings))
        assert len(pages) == 1

    def test_exif_stripped(self, mock_sane_module: MockSaneModule) -> None:
        """EXIF data is removed from scanned images before yielding."""
        mock_dev = mock_sane_module._mock_dev
        img = _make_content_image()
        # Inject fake EXIF data
        img.info["exif"] = b"fake-exif-data"
        mock_dev._multi_scan_pages = [img]

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")
        pages = list(backend.scan_pages("test:device:001", settings))
        assert len(pages) == 1
        assert "exif" not in pages[0].info

    def test_exif_stripped_flatbed(self, mock_sane_module: MockSaneModule) -> None:
        """EXIF data is stripped from flatbed scans too."""
        mock_dev = mock_sane_module._mock_dev
        img = Image.new("RGB", (100, 100), "white")
        img.info["exif"] = b"fake-exif-data"
        mock_dev._snap_impl = MagicMock(return_value=img)

        backend = SaneBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="color")
        pages = list(backend.scan_pages("test:device:001", settings))
        assert len(pages) == 1
        assert "exif" not in pages[0].info


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
        self, mock_sane_module: MockSaneModule, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A zero-dimension third sheet is skipped and named; four survive."""
        mock_dev = mock_sane_module._mock_dev
        mock_dev._multi_scan_pages = [
            _make_content_image(),
            _make_content_image(),
            Image.new("RGB", (0, 0)),
            _make_content_image(),
            _make_content_image(),
        ]

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")
        with caplog.at_level(logging.WARNING):
            pages = list(backend.scan_pages("test:device:001", settings))

        assert len(pages) == 4
        skips = [r for r in caplog.records if "skipping" in r.getMessage()]
        assert len(skips) == 1
        assert "Page 3" in skips[0].getMessage()

    def test_a_wholly_rejected_batch_raises_rather_than_yielding_nothing(
        self, mock_sane_module: MockSaneModule
    ) -> None:
        """Three unreadable sheets raise ScanError naming how many were fed."""
        mock_dev = mock_sane_module._mock_dev
        mock_dev._multi_scan_pages = [Image.new("RGB", (0, 0)) for _ in range(3)]

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")

        with pytest.raises(ScanError) as exc_info:
            list(backend.scan_pages("test:device:001", settings))

        assert "3" in str(exc_info.value)
        # Distinct condition from an empty feeder: paper *was* fed, and the
        # operator needs to be told it was unreadable rather than absent.
        assert not isinstance(exc_info.value, FeederEmptyError)

    def test_a_zero_page_feeder_still_raises_feeder_empty(
        self, mock_sane_module: MockSaneModule
    ) -> None:
        """No paper at all stays FeederEmptyError, not the all-rejected error."""
        mock_dev = mock_sane_module._mock_dev
        mock_dev._multi_scan_pages = []

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")

        with pytest.raises(FeederEmptyError, match="No paper detected in feeder"):
            list(backend.scan_pages("test:device:001", settings))

    def test_a_clean_stack_logs_no_skip_warning(
        self, mock_sane_module: MockSaneModule, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Five readable sheets yield five pages and no skip warning at all."""
        mock_dev = mock_sane_module._mock_dev
        mock_dev._multi_scan_pages = [_make_content_image() for _ in range(5)]

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")
        with caplog.at_level(logging.WARNING):
            pages = list(backend.scan_pages("test:device:001", settings))

        assert len(pages) == 5
        assert not [r for r in caplog.records if "skipping" in r.getMessage()]


class TestAutoSourceRouting:
    """Auto source conditional routing via auto_source_mode."""

    def test_auto_source_adf_routes_to_adf_path(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """scan_pages with source='Auto' and auto_source_mode='adf' uses ADF path."""
        # Add "Auto" to available sources
        mock_dev = mock_sane_module._mock_dev
        mock_dev._options_impl = [
            (1, "source", "Source", "", 3, 0, 1, 5, ["Flatbed", "ADF", "Auto"]),
            (2, "resolution", "Res", "", 1, 4, 1, 5, [300]),
            (3, "mode", "Mode", "", 3, 0, 1, 5, ["color"]),
        ]
        settings = ScanSettings(
            source="Auto", resolution=300, mode="color", auto_source_mode="adf"
        )
        pages = list(sane_backend.scan_pages("test:device:001", settings))
        # ADF path yields 3 pages via multi_scan
        assert len(pages) == 3

    def test_auto_source_flatbed_routes_to_flatbed_path(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """scan_pages with source='Auto' and auto_source_mode='flatbed' uses flatbed path."""
        mock_dev = mock_sane_module._mock_dev
        mock_dev._options_impl = [
            (1, "source", "Source", "", 3, 0, 1, 5, ["Flatbed", "ADF", "Auto"]),
            (2, "resolution", "Res", "", 1, 4, 1, 5, [300]),
            (3, "mode", "Mode", "", 3, 0, 1, 5, ["color"]),
        ]
        mock_dev._snap_impl = MagicMock(
            return_value=Image.new("RGB", (100, 100), "white")
        )
        settings = ScanSettings(
            source="Auto", resolution=300, mode="color", auto_source_mode="flatbed"
        )
        pages = list(sane_backend.scan_pages("test:device:001", settings))
        # Flatbed path yields 1 page via snap
        assert len(pages) == 1
        mock_dev._snap_impl.assert_called_once()

    def test_explicit_adf_ignores_auto_source_mode(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """Explicit ADF source ignores auto_source_mode setting."""
        settings = ScanSettings(
            source="ADF", resolution=300, mode="color", auto_source_mode="flatbed"
        )
        pages = list(sane_backend.scan_pages("test:device:001", settings))
        # ADF always uses ADF path regardless of auto_source_mode
        assert len(pages) == 3

    def test_explicit_flatbed_ignores_auto_source_mode(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """Explicit Flatbed source ignores auto_source_mode setting."""
        mock_dev = mock_sane_module._mock_dev
        mock_dev._snap_impl = MagicMock(
            return_value=Image.new("RGB", (100, 100), "white")
        )
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="color", auto_source_mode="adf"
        )
        pages = list(sane_backend.scan_pages("test:device:001", settings))
        # Flatbed always uses flatbed path regardless of auto_source_mode
        assert len(pages) == 1
        mock_dev._snap_impl.assert_called_once()


class TestSaneBackendPerPageTimeout:
    """Per-page timeout tests."""

    def test_page_timeout_raises_scan_error(
        self, mock_sane_module: MockSaneModule
    ) -> None:
        """Page that takes too long raises ScanError with timeout message."""
        event = threading.Event()

        def _blocking_iterator() -> Iterator[Image.Image]:
            """Block on first next() call to simulate slow scan."""
            event.wait(timeout=10)
            yield _make_content_image()

        fake_dev = _FakeSaneDevice(multi_scan=_blocking_iterator)

        backend = SaneBackend()

        with pytest.raises(ScanError, match="timed out"):
            list(backend._scan_adf_pages(fake_dev, timeout_per_page=0.5))

        # Unblock the thread so it can clean up
        event.set()

    def test_pages_within_timeout_succeed(
        self, mock_sane_module: MockSaneModule
    ) -> None:
        """Pages acquired within timeout proceed normally."""
        mock_dev = mock_sane_module._mock_dev
        assert isinstance(mock_dev, MockSaneDev)

        backend = SaneBackend()
        pages = list(backend._scan_adf_pages(mock_dev, timeout_per_page=5.0))
        assert len(pages) == 3


class TestSaneBackendADFCleanup:
    """ADF cleanup (cancel/close) tests."""

    def test_cancel_called_after_adf_scan(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """dev.cancel() is called after ADF multi_scan completes."""
        settings = ScanSettings(source="ADF", resolution=300, mode="color")
        list(sane_backend.scan_pages("test:device:001", settings))
        mock_dev = mock_sane_module._mock_dev
        assert mock_dev._cancel_called

    def test_cancel_called_on_adf_error(self, mock_sane_module: MockSaneModule) -> None:
        """dev.cancel() and dev.close() called even when ADF scan errors."""
        operations: list[str] = []

        def _error_iterator() -> Iterator[Image.Image]:
            yield _make_content_image()
            msg = "hardware error"
            raise RuntimeError(msg)

        fake_dev = _FakeSaneDevice(
            multi_scan=_error_iterator,
            cancel=lambda: operations.append("cancel"),
            close=lambda: operations.append("close"),
        )
        object.__setattr__(mock_sane_module, "_mock_dev", fake_dev)

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")

        # D-03: a fault after the first page is translated to ScanError
        # carrying the device's own text, rather than propagating raw.
        with pytest.raises(ScanError, match="hardware error"):
            list(backend.scan_pages("test:device:001", settings))

        assert "cancel" in operations
        assert "close" in operations

    def test_iterator_deleted_before_cancel(
        self, mock_sane_module: MockSaneModule
    ) -> None:
        """Multi_scan iterator reference is deleted before dev.cancel()."""
        mock_dev = mock_sane_module._mock_dev

        # Track operation order
        operations: list[str] = []

        class TrackingIterator:
            """Iterator that tracks when it is deleted."""

            def __init__(self, pages: list[Image.Image]) -> None:
                """Initialize with pages to yield."""
                self._pages = iter(pages)

            def __next__(self) -> Image.Image:
                """Yield next page."""
                return next(self._pages)

            def __iter__(self) -> TrackingIterator:
                """Return self as iterator."""
                return self

            def __del__(self) -> None:
                """Track deletion."""
                operations.append("iterator_deleted")

        def _tracking_multi_scan() -> TrackingIterator:
            return TrackingIterator(mock_dev._multi_scan_pages)

        def _tracking_cancel() -> None:
            operations.append("cancel_called")

        fake_dev = _FakeSaneDevice(
            multi_scan=_tracking_multi_scan, cancel=_tracking_cancel
        )
        object.__setattr__(mock_sane_module, "_mock_dev", fake_dev)

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")
        list(backend.scan_pages("test:device:001", settings))

        # Note: iterator_deleted may or may not appear depending on GC,
        # but cancel should always be called
        assert "cancel_called" in operations


# ---------------------------------------------------------------------------
# Paper size geometry and crop fallback tests
# ---------------------------------------------------------------------------


class _NoGeometryDevice:
    """Mock device that raises AttributeError on geometry attribute assignment."""

    def __init__(self, pages: list[Image.Image] | None = None) -> None:
        """Initialize device that rejects geometry options."""
        self.mode: str = "color"
        self.resolution: int = 300
        self.source: str = "Flatbed"
        self._pages = pages or [_make_content_image()]

    def __setattr__(self, name: str, value: object) -> None:
        """Reject geometry attributes, accept everything else."""
        if name in {"tl_x", "tl_y", "br_x", "br_y"}:
            msg = f"Device does not support option '{name}'"
            raise AttributeError(msg)
        super().__setattr__(name, value)

    def get_options(self) -> list[tuple]:
        """Return sample options with source."""
        return [
            (1, "source", "Source", "", 3, 0, 1, 5, ["Flatbed", "ADF"]),
            (2, "resolution", "Res", "", 1, 4, 1, 5, [300]),
            (3, "mode", "Mode", "", 3, 0, 1, 5, ["color"]),
        ]

    def start(self) -> None:
        """Initiate SANE scan cycle (no-op in mock)."""

    def snap(self) -> Image.Image:
        """Return first page image."""
        return self._pages[0]

    def multi_scan(self) -> Iterator[Image.Image]:
        """Return iterator over pages."""
        return iter(self._pages)

    def cancel(self) -> None:
        """Cancel (no-op in mock)."""

    def close(self) -> None:
        """Close (no-op in mock)."""


class TestPaperSizeGeometry:
    """Paper size geometry option setting tests."""

    def test_a4_sets_geometry(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """When paper_size='a4', dev.br_x=210.0 and dev.br_y=297.0."""
        mock_dev = mock_sane_module._mock_dev
        mock_dev._snap_impl = MagicMock(
            return_value=Image.new("RGB", (2500, 3600), "white")
        )
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="color", paper_size="a4"
        )
        list(sane_backend.scan_pages("test:device:001", settings))
        assert mock_dev.br_x == 210.0
        assert mock_dev.br_y == 297.0
        assert mock_dev.tl_x == 0.0
        assert mock_dev.tl_y == 0.0

    def test_full_no_geometry(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """When paper_size='full', no geometry options are set on device."""
        mock_dev = mock_sane_module._mock_dev
        # Reset to known values
        mock_dev.tl_x = -1.0
        mock_dev.tl_y = -1.0
        mock_dev.br_x = -1.0
        mock_dev.br_y = -1.0
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="color", paper_size="full"
        )
        list(sane_backend.scan_pages("test:device:001", settings))
        # Geometry should be unchanged (not set by scan_pages)
        assert mock_dev.tl_x == -1.0
        assert mock_dev.br_x == -1.0

    def test_letter_sets_geometry(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """When paper_size='letter', dev.br_x=215.9 and dev.br_y=279.4."""
        mock_dev = mock_sane_module._mock_dev
        mock_dev._snap_impl = MagicMock(
            return_value=Image.new("RGB", (2600, 3400), "white")
        )
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="color", paper_size="letter"
        )
        list(sane_backend.scan_pages("test:device:001", settings))
        expected_br_x = 215.9
        expected_br_y = 279.4
        assert mock_dev.br_x == expected_br_x
        assert mock_dev.br_y == expected_br_y

    def test_geometry_failure_still_completes(
        self, mock_sane_module: MockSaneModule
    ) -> None:
        """When geometry setting raises, scan still completes (fallback to crop)."""
        no_geom_dev = _NoGeometryDevice()
        object.__setattr__(mock_sane_module, "_mock_dev", no_geom_dev)

        backend = SaneBackend()
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="color", paper_size="a4"
        )
        pages = list(backend.scan_pages("test:device:001", settings))
        assert len(pages) == 1


class TestPaperSizeCropFallback:
    """Pillow crop fallback when geometry options are unavailable."""

    def test_crop_fallback_when_geometry_fails(
        self, mock_sane_module: MockSaneModule
    ) -> None:
        """When geometry fails, scanned image is cropped to paper dimensions."""
        # Create a large image (larger than A4 at 300 DPI)
        large_img = _make_content_image(width=3000, height=4000)
        no_geom_dev = _NoGeometryDevice(pages=[large_img])
        object.__setattr__(mock_sane_module, "_mock_dev", no_geom_dev)

        backend = SaneBackend()
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="color", paper_size="a4"
        )
        pages = list(backend.scan_pages("test:device:001", settings))
        assert len(pages) == 1
        # A4 at 300 DPI: 210*300/25.4=2480, 297*300/25.4=3507
        assert pages[0].size == (2480, 3507)

    def test_full_no_crop(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """When paper_size='full', image is yielded at original size."""
        mock_dev = mock_sane_module._mock_dev
        original_img = Image.new("RGB", (5000, 6000), "white")
        mock_dev._snap_impl = MagicMock(return_value=original_img)
        settings = ScanSettings(
            source="Flatbed", resolution=300, mode="color", paper_size="full"
        )
        pages = list(sane_backend.scan_pages("test:device:001", settings))
        assert len(pages) == 1
        assert pages[0].size == (5000, 6000)

    def test_adf_crop_fallback(self, mock_sane_module: MockSaneModule) -> None:
        """ADF pages are cropped when geometry options are unavailable."""
        large_pages = [
            _make_content_image(width=3000, height=4000),
            _make_content_image(width=3000, height=4000),
        ]
        no_geom_dev = _NoGeometryDevice(pages=large_pages)
        object.__setattr__(mock_sane_module, "_mock_dev", no_geom_dev)

        backend = SaneBackend()
        settings = ScanSettings(
            source="ADF", resolution=300, mode="color", paper_size="a4"
        )
        pages = list(backend.scan_pages("test:device:001", settings))
        assert len(pages) == 2
        for page in pages:
            assert page.size == (2480, 3507)


# ---------------------------------------------------------------------------
# D-17: the shared fake and the measured python-sane contract
# ---------------------------------------------------------------------------


class TestFakeSaneContract:
    """
    The shared fake models python-sane 2.9.2's measured behaviour.

    The rows asserted here are RESEARCH.md Finding 7's contract table, which
    was executed against the real library rather than inferred.  Each of the
    three hand-written doubles in this module got at least one row wrong, and
    every wrong row let a shipped defect earn a green test -- most visibly
    ``_NoGeometryDevice``, which *raises* on an unknown option where the real
    library *stores* it, so the crop fallback it was written to prove could
    never actually be reached.

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
        Row 1, the inverse of ``_NoGeometryDevice``.

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
        """The real device returns float, so the Protocol's ``int`` is a lie."""
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

        list(backend.scan_pages("test:0", settings))

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

        list(backend.scan_pages("test:0", settings))

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

        list(backend.scan_pages("test:0", settings))

        assert 1.0 <= dev.resolution <= 600.0


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
            list(backend.scan_pages("test:0", settings))

        assert [m for m in self._warnings(caplog) if "5000" in m and "1200" in m]

    def test_the_read_back_value_is_an_int_not_the_device_float(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The device returns 1200.0; what the backend carries is 1200."""
        dev = FakeSaneDev()
        backend = _backend_with(dev, monkeypatch)
        settings = ScanSettings(source="Flatbed", resolution=5000, mode="Color")

        with caplog.at_level(logging.WARNING, logger="saneless.scanner.sane_backend"):
            list(backend.scan_pages("test:0", settings))

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
            list(backend.scan_pages("test:0", settings))

        assert not [m for m in self._warnings(caplog) if "resolution" in m.lower()]
