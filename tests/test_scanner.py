"""Tests for scanner abstraction layer (ABC + SaneBackend)."""

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from PIL import Image

from saneless.exceptions import ScanError
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScannerBackend,
    ScanSettings,
)

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


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

    def snap(self) -> Image.Image:
        """Return a simple test image (no progress callback)."""
        self._snap_calls.append({})
        return Image.new("RGB", (100, 100), "white")

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
            (1, "source", "Scan source", "Source desc", 3, 0, 1, 5, ["Flatbed", "ADF"]),
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

    def get_capabilities(self, _device_id: str) -> DeviceCapabilities:
        """Return minimal capabilities for the mock device."""
        return DeviceCapabilities(
            sources=["Flatbed"],
            resolutions=[300],
            modes=["color"],
            raw_options=[],
        )

    def scan_pages(
        self, _device_id: str, _settings: ScanSettings
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


# ---------------------------------------------------------------------------
# ABC contract tests
# ---------------------------------------------------------------------------


class TestScannerBackendABC:
    """Scanner backend ABC tests."""

    def test_scanner_backend_is_abstract(self) -> None:
        """ScannerBackend cannot be instantiated directly."""
        with pytest.raises(TypeError, match="abstract"):
            ScannerBackend()  # type: ignore[abstract]


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
def mock_sane_module(monkeypatch):
    """Patch sane module into sane_backend's namespace."""
    import saneless.scanner.sane_backend as sane_backend_mod

    mock_sane = MockSaneModule()
    monkeypatch.setattr(sane_backend_mod, "sane", mock_sane)
    return mock_sane


@pytest.fixture
def sane_backend(mock_sane_module):
    """Create a SaneBackend with mocked sane module."""
    _ = mock_sane_module  # side-effect: patches the sane module
    from saneless.scanner.sane_backend import SaneBackend

    return SaneBackend()


class TestSaneBackendInit:
    """SaneBackend initialization tests."""

    def test_sane_backend_init_calls_sane_init(self, mock_sane_module) -> None:
        """SaneBackend constructor calls sane.init() exactly once."""
        from saneless.scanner.sane_backend import SaneBackend

        assert mock_sane_module.init_call_count == 0
        SaneBackend()
        assert mock_sane_module.init_call_count == 1

    def test_sane_backend_init_calls_sane_init_exactly_once(
        self, mock_sane_module
    ) -> None:
        """A single SaneBackend instance only triggers one init call."""
        from saneless.scanner.sane_backend import SaneBackend

        SaneBackend()
        assert mock_sane_module.init_call_count == 1


class TestSaneBackendGetDevices:
    """SaneBackend device enumeration tests."""

    def test_sane_backend_get_devices(self, sane_backend) -> None:
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
        self, sane_backend, mock_sane_module
    ) -> None:
        """scan_pages opens device, yields image, then closes device."""
        settings = ScanSettings(source="Flatbed", resolution=300, mode="color")
        pages = list(sane_backend.scan_pages("test:device:001", settings))
        assert len(pages) == 1
        assert isinstance(pages[0], Image.Image)
        mock_dev = mock_sane_module._mock_dev
        assert mock_dev._close_called

    def test_sane_backend_cancel_before_close(
        self, sane_backend, mock_sane_module
    ) -> None:
        """Device cancel() is called before close() on normal exit."""
        settings = ScanSettings(source="Flatbed", resolution=300, mode="color")
        list(sane_backend.scan_pages("test:device:001", settings))
        mock_dev = mock_sane_module._mock_dev
        assert mock_dev._cancel_called
        assert mock_dev._close_called

    def test_sane_backend_cancel_before_close_on_error(self, mock_sane_module) -> None:
        """Device cancel() and close() are called even when snap() raises."""
        from saneless.scanner.sane_backend import SaneBackend

        # Make snap raise an error
        mock_dev = mock_sane_module._mock_dev
        mock_dev.snap = MagicMock(side_effect=RuntimeError("scan failed"))

        backend = SaneBackend()
        settings = ScanSettings(source="Flatbed", resolution=300, mode="color")

        with pytest.raises(RuntimeError, match="scan failed"):
            list(backend.scan_pages("test:device:001", settings))

        assert mock_dev._cancel_called
        assert mock_dev._close_called

    def test_sane_backend_no_progress_callback(
        self, sane_backend, mock_sane_module
    ) -> None:
        """Verify snap() is called without progress argument (Pitfall #2)."""
        mock_dev = mock_sane_module._mock_dev
        mock_dev.snap = MagicMock(return_value=Image.new("RGB", (100, 100), "white"))

        settings = ScanSettings(source="Flatbed", resolution=300, mode="color")
        list(sane_backend.scan_pages("test:device:001", settings))

        # snap() should be called with no arguments (no progress callback)
        mock_dev.snap.assert_called_once_with()

    @pytest.mark.usefixtures("mock_sane_module")
    def test_sane_backend_validates_source_option(self) -> None:
        """Requesting an unsupported source raises ScanError."""
        from saneless.scanner.sane_backend import SaneBackend

        backend = SaneBackend()
        settings = ScanSettings(
            source="NonExistentSource", resolution=300, mode="color"
        )

        with pytest.raises(ScanError, match="NonExistentSource"):
            list(backend.scan_pages("test:device:001", settings))


class TestSaneBackendGetCapabilities:
    """SaneBackend capability query tests."""

    def test_sane_backend_get_capabilities(self, sane_backend) -> None:
        """get_capabilities returns parsed sources, resolutions, and modes."""
        caps = sane_backend.get_capabilities("test:device:001")
        assert isinstance(caps, DeviceCapabilities)
        assert "Flatbed" in caps.sources
        assert "ADF" in caps.sources
        assert 300 in caps.resolutions
        assert 600 in caps.resolutions
        assert "color" in caps.modes
        assert "gray" in caps.modes
        assert len(caps.raw_options) > 0
