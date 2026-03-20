"""Shared test fixtures for all test modules."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.scanner.base import ScannerBackend

_TEST_TMP = str(Path(tempfile.gettempdir()) / "saneless-test")
_TEST_LOG = str(Path(tempfile.gettempdir()) / "saneless-test" / "saneless.log")


@pytest.fixture
def tmp_config_dir(tmp_path):
    """Create a temporary directory for config files."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return config_dir


@pytest.fixture
def sample_toml(tmp_config_dir):
    """Write a minimal valid TOML config to tmp_config_dir/saneless.toml."""
    toml_content = """\
[scanner]
host = "192.168.1.50"

[paperless]
url = "http://paperless:8000"
token = "abc123"

[output]
tmp_dir = "/tmp/saneless"
log_level = "INFO"

[profiles.default]
source = "Flatbed"
resolution = 300
mode = "color"
"""
    config_file = tmp_config_dir / "saneless.toml"
    config_file.write_text(toml_content)
    return config_file


@pytest.fixture
def sample_pil_image():
    """Return a 100x100 white RGB PIL Image."""
    return Image.new("RGB", (100, 100), "white")


@pytest.fixture
def sample_pil_images():
    """Return a list of 3 sample PIL Images of varying sizes and colors."""
    return [
        Image.new("RGB", (100, 100), "white"),
        Image.new("RGB", (200, 200), "red"),
        Image.new("RGB", (150, 150), "blue"),
    ]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Remove all SANELESS_* env vars before each test."""
    for key in list(os.environ):
        if key.startswith("SANELESS_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def default_settings():
    """Return a Settings instance with test-safe defaults."""
    auth = "test-token"
    return Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(
            url="http://localhost:8000",
            token=auth,
        ),
        output=OutputConfig(
            tmp_dir=_TEST_TMP,
            log_file=_TEST_LOG,
        ),
        profiles={"default": ProfileConfig()},
    )


@pytest.fixture
def mock_scanner():
    """Return a mock ScannerBackend that yields a single white image."""
    scanner = MagicMock(spec=ScannerBackend)
    scanner.scan_pages.return_value = iter([Image.new("RGB", (100, 100), "white")])
    return scanner


@pytest.fixture
def mock_paperless():
    """Return a mock PaperlessClient that succeeds."""
    paperless = MagicMock()
    paperless.upload_document.return_value = "mock-task-uuid"
    paperless.poll_task.return_value = {"status": "SUCCESS"}
    return paperless
