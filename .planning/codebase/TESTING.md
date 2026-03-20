# Testing Patterns

**Analysis Date:** 2026-03-20

## Test Framework

**Runner:**
- pytest 9.0.2+
- Config: `pyproject.toml` with `[tool.pytest.ini_options]` section
- Test discovery: Searches `tests/` directory by default (configured in pyproject.toml)

**Assertion Library:**
- pytest's built-in assertion introspection (no separate library required)
- Use standard `assert` statements; pytest rewrites them for detailed error messages

**Run Commands:**
```bash
# Run all tests
uv run pytest

# Run specific test file
uv run pytest tests/test_scanner.py

# Run specific test function
uv run pytest tests/test_scanner.py::test_connect

# Run with verbose output
uv run pytest -v

# Watch mode (requires pytest-watch, not currently installed)
# uv run ptw

# Generate coverage report
uv run pytest --cov=src/saneless --cov-report=html

# Run tests excluding slow tests
uv run pytest -m "not slow"
```

## Test File Organization

**Location:**
- Separate `tests/` directory (not co-located with source)
- Tests are NOT included in the package distribution
- Config path: `testpaths = ["tests"]` in pyproject.toml

**Naming:**
- Test files: `test_*.py` or `*_test.py` prefix/suffix
- Test functions: `def test_*()` prefix
- Test classes: `class Test*:` (for grouping related tests)

**Structure:**
```
tests/
├── test_scanner.py         # Tests for scanner module
├── test_paperless.py       # Tests for paperless integration
├── conftest.py             # Shared fixtures
├── fixtures/               # Fixture data (JSON, images, etc.)
│   ├── sample_scan.pdf
│   └── scanner_responses.json
└── integration/            # Integration tests
    └── test_e2e_scan.py
```

## Test Structure

**Suite Organization:**
```python
"""Test scanner connection logic."""

import pytest

from saneless.scanner import ScannerConnection, ScannerError


class TestScannerConnection:
    """Test suite for ScannerConnection class."""

    def test_connect_successful(self) -> None:
        """Test successful connection to available scanner."""
        conn = ScannerConnection("localhost")
        assert conn.is_connected is True

    def test_connect_timeout(self) -> None:
        """Test connection timeout when device unreachable."""
        with pytest.raises(ConnectionError, match="timeout"):
            ScannerConnection("192.168.1.999", timeout=1)

    def test_disconnect(self) -> None:
        """Test graceful disconnect."""
        conn = ScannerConnection("localhost")
        conn.disconnect()
        assert conn.is_connected is False


class TestScannerError:
    """Test exception handling."""

    def test_scanner_error_message(self) -> None:
        """Test custom error message."""
        error = ScannerError("Device offline")
        assert str(error) == "Device offline"
```

**Patterns:**
- **Setup (arrange):** Create fixtures and test data at the top of the test
- **Teardown (cleanup):** Use pytest fixtures with yield or autouse cleanup
- **Assertion (act & assert):** Minimal per test; one logical assertion per test when possible

## Mocking

**Framework:** `unittest.mock` (built into Python standard library)

**Patterns:**
```python
"""Test with mocks for external dependencies."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from saneless.paperless import PaperlessClient


def test_ingest_document() -> None:
    """Test document ingestion without calling real Paperless API."""
    mock_response = Mock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"id": 42}

    with patch("requests.post", return_value=mock_response) as mock_post:
        client = PaperlessClient("http://localhost:8000")
        doc_id = client.ingest_pdf(b"fake pdf data")

        mock_post.assert_called_once()
        assert doc_id == 42


@pytest.fixture
def mock_sane_device() -> MagicMock:
    """Provide a mock SANE device."""
    device = MagicMock()
    device.scan.return_value = [b"page1", b"page2"]
    return device
```

**What to Mock:**
- External API calls (Paperless, SANE backend)
- File I/O operations
- Network calls
- Time-dependent operations (use `freezegun` for datetime if needed)

**What NOT to Mock:**
- Internal function calls within the module being tested
- Business logic (you want to test the actual logic, not a mock)
- Built-in functions like `len()`, `dict()` unless specifically testing error handling

## Fixtures and Factories

**Test Data:**
```python
"""Reusable test fixtures."""

import pytest


@pytest.fixture
def sample_pdf() -> bytes:
    """Provide sample PDF binary data for tests."""
    # Minimal valid PDF structure
    return b"%PDF-1.4\n%EOF"


@pytest.fixture
def scan_config() -> dict[str, str]:
    """Provide a typical scan configuration."""
    return {
        "device": "epson:libusb:001:002",
        "resolution": "300",
        "mode": "Color",
        "source": "Flatbed",
    }


@pytest.fixture
def mock_saned_server(monkeypatch) -> str:
    """Mock SANE server on localhost."""
    # Replace SANE connection code to use test socket
    monkeypatch.setenv("SANED_HOST", "127.0.0.1")
    monkeypatch.setenv("SANED_PORT", "6566")
    return "127.0.0.1"
```

**Location:**
- Shared fixtures: `tests/conftest.py` (auto-discovered by pytest)
- Module-specific fixtures: In the test file itself or in a fixture submodule like `tests/fixtures/`
- Complex fixtures: Separate file `tests/fixtures.py` if shared by many tests

## Coverage

**Requirements:** Not enforced (no coverage target set)

**View Coverage:**
```bash
# Generate coverage report
uv run pytest --cov=src/saneless --cov-report=term-missing

# HTML report
uv run pytest --cov=src/saneless --cov-report=html
# Open htmlcov/index.html in browser
```

## Test Types

**Unit Tests:**
- Scope: Single function or class method
- Approach: Isolated, minimal dependencies, mock external calls
- Location: `tests/test_*.py` (main test files)
- Example: Testing `ScannerConnection.connect()` with mocked socket

**Integration Tests:**
- Scope: Multiple modules working together (but not full system)
- Approach: Use real sub-components where feasible; mock only external services
- Location: `tests/integration/` (separate subdirectory for clarity)
- Example: Testing scanner discovery → device connection → PDF assembly without hitting Paperless

**E2E Tests:**
- Scope: Full system from UI/CLI through scanner to Paperless
- Approach: Use playwright for web UI automation
- Location: `tests/e2e/` (if E2E tests added)
- Framework: Playwright 1.58.0+ (available in dev dependencies)
- Current status: Not yet implemented (infrastructure available)

## Common Patterns

**Async Testing:**
```python
"""Test async functions (if used)."""

import pytest


@pytest.mark.asyncio
async def test_async_scan() -> None:
    """Test asynchronous scan operation."""
    result = await scanner.scan_async()
    assert result.pages > 0
```

**Error Testing:**
```python
"""Test error conditions."""

import pytest

from saneless.scanner import ScannerError


def test_scan_device_offline() -> None:
    """Test error when device goes offline during scan."""
    with pytest.raises(ScannerError, match="offline"):
        # Code that triggers ScannerError
        raise ScannerError("Device offline")


def test_invalid_resolution() -> None:
    """Test validation of invalid resolution."""
    with pytest.raises(ValueError, match="resolution"):
        # Code that validates resolution
        validate_resolution(-1)
```

**Parameterized Tests:**
```python
"""Test multiple inputs with one test function."""

import pytest


@pytest.mark.parametrize(
    "resolution,expected_quality",
    [
        (150, "draft"),
        (300, "standard"),
        (600, "high"),
    ],
)
def test_scan_resolution_quality(
    resolution: int,
    expected_quality: str,
) -> None:
    """Test that different resolutions produce expected quality."""
    scan = perform_scan(resolution=resolution)
    assert scan.quality == expected_quality
```

---

*Testing analysis: 2026-03-20*
