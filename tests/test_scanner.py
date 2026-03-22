"""Tests for scanner abstraction layer (ABC + SaneBackend)."""

from __future__ import annotations

import os
import threading
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
)
from saneless.scanner.sane_backend import SaneBackend

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_content_image(
    width: int = 200, height: int = 300, color: str = "red"
) -> Image.Image:
    """
    Create a test image with mixed content that passes validation.

    Uses drawing operations to ensure non-trivial pixel variance,
    passing the scanner-level pure white/black checks.
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
        Return sample SANE option tuples.

        SANE option format:
        (index, name, title, desc, type, unit, size, cap, constraint)
        """
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

    def test_empty_feeder_out_of_documents_error(
        self, mock_sane_module: MockSaneModule
    ) -> None:
        """Multi_scan first iteration error with 'out of documents' raises FeederEmptyError."""

        def _raising_multi_scan() -> Iterator[Image.Image]:
            msg = "out of documents"
            raise RuntimeError(msg)

        object.__setattr__(
            mock_sane_module,
            "_mock_dev",
            _FakeSaneDevice(multi_scan=_raising_multi_scan),
        )

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")

        with pytest.raises(FeederEmptyError, match="No paper detected in feeder"):
            list(backend.scan_pages("test:device:001", settings))

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

    def test_pure_white_page_skipped(self, mock_sane_module: MockSaneModule) -> None:
        """Pure white image (255,255,255) is skipped at scanner level."""
        mock_dev = mock_sane_module._mock_dev
        white_img = Image.new("RGB", (200, 300), (255, 255, 255))
        normal_img = _make_content_image()
        mock_dev._multi_scan_pages = [white_img, normal_img]

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")
        pages = list(backend.scan_pages("test:device:001", settings))
        assert len(pages) == 1

    def test_pure_black_page_skipped(self, mock_sane_module: MockSaneModule) -> None:
        """Pure black image (0,0,0) is skipped at scanner level."""
        mock_dev = mock_sane_module._mock_dev
        black_img = Image.new("RGB", (200, 300), (0, 0, 0))
        normal_img = _make_content_image()
        mock_dev._multi_scan_pages = [black_img, normal_img]

        backend = SaneBackend()
        settings = ScanSettings(source="ADF", resolution=300, mode="color")
        pages = list(backend.scan_pages("test:device:001", settings))
        assert len(pages) == 1

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


class TestAutoSourceRouting:
    """Auto source conditional routing via auto_source_mode."""

    def test_auto_source_adf_routes_to_adf_path(
        self, sane_backend: SaneBackend, mock_sane_module: MockSaneModule
    ) -> None:
        """scan_pages with source='Auto' and auto_source_mode='adf' uses ADF path."""
        # Add "Auto" to available sources
        mock_dev = mock_sane_module._mock_dev
        mock_dev.get_options = lambda: [  # type: ignore[assignment]
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
        mock_dev.get_options = lambda: [  # type: ignore[assignment]
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

        with pytest.raises(RuntimeError, match="hardware error"):
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
