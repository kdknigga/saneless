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
        self._cancel_called = True

    def close(self) -> None:
        self._close_called = True

    def get_options(self) -> list[tuple]:
        """Return sample SANE option tuples.

        SANE option format:
        (index, name, title, desc, type, unit, size, cap, constraint)
        """
        return [
            (1, "source", "Scan source", "Source desc", 3, 0, 1, 5, ["Flatbed", "ADF"]),
            (2, "resolution", "Resolution", "Res desc", 1, 4, 1, 5, [75, 150, 300, 600]),
            (3, "mode", "Scan mode", "Mode desc", 3, 0, 1, 5, ["color", "gray", "lineart"]),
        ]


class MockSaneModule:
    """Mock for the ``sane`` module (python-sane)."""

    def __init__(self) -> None:
        self.init_call_count = 0
        self._devices: list[tuple[str, str, str, str]] = [
            ("test:device:001", "TestVendor", "TestModel", "scanner"),
        ]
        self._mock_dev = MockSaneDev()

    def init(self) -> tuple[int, int, int]:
        self.init_call_count += 1
        return (1, 0, 3)

    def get_devices(self) -> list[tuple[str, str, str, str]]:
        return self._devices

    def open(self, device_id: str) -> MockSaneDev:
        return self._mock_dev


class MockBackend(ScannerBackend):
    """Concrete mock backend to test the ABC contract."""

    def get_devices(self) -> list[DeviceInfo]:
        return [
            DeviceInfo(
                name="mock:device",
                vendor="Mock",
                model="Scanner",
                device_type="scanner",
            ),
        ]

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        return DeviceCapabilities(
            sources=["Flatbed"],
            resolutions=[300],
            modes=["color"],
            raw_options=[],
        )

    def scan_pages(
        self, device_id: str, settings: ScanSettings
    ) -> Iterator[Image.Image]:
        yield Image.new("RGB", (100, 100), "white")


# ---------------------------------------------------------------------------
# Dataclass field tests
# ---------------------------------------------------------------------------


class TestDeviceInfo:
    def test_device_info_fields(self) -> None:
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
    def test_scan_settings_fields(self) -> None:
        settings = ScanSettings(source="Flatbed", resolution=300, mode="color")
        assert settings.source == "Flatbed"
        assert settings.resolution == 300
        assert settings.mode == "color"


# ---------------------------------------------------------------------------
# ABC contract tests
# ---------------------------------------------------------------------------


class TestScannerBackendABC:
    def test_scanner_backend_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            ScannerBackend()  # type: ignore[abstract]


class TestMockBackend:
    def test_mock_backend_get_devices(self) -> None:
        backend = MockBackend()
        devices = backend.get_devices()
        assert len(devices) == 1
        assert isinstance(devices[0], DeviceInfo)

    def test_mock_backend_scan_pages(self) -> None:
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
    from saneless.scanner.sane_backend import SaneBackend

    return SaneBackend()


class TestSaneBackendInit:
    def test_sane_backend_init_calls_sane_init(self, mock_sane_module) -> None:
        from saneless.scanner.sane_backend import SaneBackend

        assert mock_sane_module.init_call_count == 0
        SaneBackend()
        assert mock_sane_module.init_call_count == 1

    def test_sane_backend_init_calls_sane_init_exactly_once(
        self, mock_sane_module
    ) -> None:
        from saneless.scanner.sane_backend import SaneBackend

        SaneBackend()
        # Creating a second instance calls init again (once per instance)
        # but a single instance only calls init once
        assert mock_sane_module.init_call_count == 1


class TestSaneBackendGetDevices:
    def test_sane_backend_get_devices(self, sane_backend, mock_sane_module) -> None:
        devices = sane_backend.get_devices()
        assert len(devices) == 1
        assert isinstance(devices[0], DeviceInfo)
        assert devices[0].name == "test:device:001"
        assert devices[0].vendor == "TestVendor"
        assert devices[0].model == "TestModel"
        assert devices[0].device_type == "scanner"


class TestSaneBackendScanPages:
    def test_sane_backend_scan_pages_opens_and_closes_device(
        self, sane_backend, mock_sane_module
    ) -> None:
        settings = ScanSettings(source="Flatbed", resolution=300, mode="color")
        pages = list(sane_backend.scan_pages("test:device:001", settings))
        assert len(pages) == 1
        assert isinstance(pages[0], Image.Image)
        mock_dev = mock_sane_module._mock_dev
        assert mock_dev._close_called

    def test_sane_backend_cancel_before_close(
        self, sane_backend, mock_sane_module
    ) -> None:
        settings = ScanSettings(source="Flatbed", resolution=300, mode="color")
        list(sane_backend.scan_pages("test:device:001", settings))
        mock_dev = mock_sane_module._mock_dev
        assert mock_dev._cancel_called
        assert mock_dev._close_called

    def test_sane_backend_cancel_before_close_on_error(
        self, mock_sane_module
    ) -> None:
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

    def test_sane_backend_validates_source_option(
        self, mock_sane_module
    ) -> None:
        from saneless.scanner.sane_backend import SaneBackend

        backend = SaneBackend()
        settings = ScanSettings(
            source="NonExistentSource", resolution=300, mode="color"
        )

        with pytest.raises(ScanError, match="NonExistentSource"):
            list(backend.scan_pages("test:device:001", settings))


class TestSaneBackendGetCapabilities:
    def test_sane_backend_get_capabilities(
        self, sane_backend, mock_sane_module
    ) -> None:
        caps = sane_backend.get_capabilities("test:device:001")
        assert isinstance(caps, DeviceCapabilities)
        assert "Flatbed" in caps.sources
        assert "ADF" in caps.sources
        assert 300 in caps.resolutions
        assert 600 in caps.resolutions
        assert "color" in caps.modes
        assert "gray" in caps.modes
        assert len(caps.raw_options) > 0
