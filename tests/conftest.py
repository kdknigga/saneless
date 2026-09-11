"""Shared test fixtures and helpers for all test modules."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from PIL import Image, ImageDraw

from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.paperless import UploadResult
from saneless.scanner.base import ScannerBackend

if TYPE_CHECKING:
    from collections.abc import Callable

    from saneless.job import Job, JobStore
    from saneless.vocabulary import JobState

_TEST_TMP = str(Path(tempfile.gettempdir()) / "saneless-test")
# Keeps the suite out of the developer's real ~/.local/state/saneless, which is
# where data_dir would otherwise resolve once a test builds a JobStore.
_TEST_DATA = str(Path(tempfile.gettempdir()) / "saneless-test" / "data")
_TEST_LOG = str(Path(tempfile.gettempdir()) / "saneless-test" / "saneless.log")

_POLL_INTERVAL = 0.02


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """Create a temporary directory for config files."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return config_dir


@pytest.fixture
def sample_toml(tmp_config_dir: Path) -> Path:
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
def sample_pil_image() -> Image.Image:
    """Return a 100x100 white RGB PIL Image."""
    return Image.new("RGB", (100, 100), "white")


@pytest.fixture
def sample_pil_images() -> list[Image.Image]:
    """Return a list of 3 sample PIL Images of varying sizes and colors."""
    return [
        Image.new("RGB", (100, 100), "white"),
        Image.new("RGB", (200, 200), "red"),
        Image.new("RGB", (150, 150), "blue"),
    ]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all SANELESS_* env vars before each test."""
    for key in list(os.environ):
        if key.startswith("SANELESS_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def default_settings() -> Settings:
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
            data_dir=_TEST_DATA,
            log_file=_TEST_LOG,
        ),
        profiles={"default": ProfileConfig()},
    )


@pytest.fixture
def mock_scanner() -> MagicMock:
    """Return a mock ScannerBackend that yields a single image with content."""
    scanner = MagicMock(spec=ScannerBackend)
    img = Image.new("RGB", (100, 100), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 90, 90], fill="black")
    scanner.scan_pages.return_value = iter([img])
    return scanner


@pytest.fixture
def multi_page_images() -> list[Image.Image]:
    """Return 5 distinct page images simulating an ADF scan."""
    colors = ["white", "red", "blue", "green", "yellow"]
    return [Image.new("RGB", (200, 300), c) for c in colors]


@pytest.fixture
def empty_page_image() -> Image.Image:
    """Return a nearly-white image that should be detected as empty."""
    return Image.new("RGB", (200, 300), (253, 253, 253))


@pytest.fixture
def content_page_image() -> Image.Image:
    """Return an image with content (not empty)."""
    img = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 180, 280], fill="black")
    return img


@pytest.fixture
def mock_paperless() -> MagicMock:
    """Return a mock PaperlessClient that succeeds."""
    paperless = MagicMock()
    paperless.upload_document.return_value = UploadResult(
        delivered_to_api=True, task_uuid="mock-task-uuid"
    )
    # poll_task's return value is not part of its contract: after OUTC-01 a
    # successful poll is "it returned" and a failed one is "it raised", so a
    # stub that hands back a status dict would encode a contract that no longer
    # exists.  None keeps this fixture honest about what success means.
    paperless.poll_task.return_value = None
    return paperless


def wait_for_state(
    store: JobStore,
    job_id: str,
    state: JobState | frozenset[JobState],
    timeout: float = 2.0,
) -> Job:
    """
    Block until a job reaches an awaited state, then return it.

    The worker runs on its own thread, so tests that drive it have to wait for
    a state rather than read one.  Passing a frozenset lets a caller wait on a
    whole class of states -- ``TERMINAL_STATES`` in particular -- without
    knowing which member the run will land on.

    The default budget is deliberately far below pytest-timeout's 60 s SIGALRM:
    a wait that hits this ceiling raises a message naming the job, the awaited
    state and the state actually observed, which a SIGALRM traceback would not.

    Args:
        store: The job store to poll.
        job_id: The id of the job to wait on.
        state: A single awaited state, or a set of acceptable states.
        timeout: Seconds to wait before giving up.

    Returns:
        The job, in one of the awaited states.

    Raises:
        RuntimeError: If the job does not reach the awaited state in time.

    """
    wanted = state if isinstance(state, frozenset) else frozenset([state])
    observed = "<no such job>"
    found: Job | None = None
    for _ in range(max(1, int(timeout / _POLL_INTERVAL))):
        job = store.get_job(job_id)
        if job is not None:
            observed = job.state.value
            if job.state in wanted:
                found = job
                break
        time.sleep(_POLL_INTERVAL)
    else:
        names = ", ".join(sorted(s.value for s in wanted))
        msg = (
            f"Job {job_id} did not reach {names} within {timeout}s "
            f"(last observed: {observed})"
        )
        raise RuntimeError(msg)
    assert found is not None
    return found


@pytest.fixture(name="wait_for_state")
def _wait_for_state_fixture() -> Callable[..., Job]:
    """
    Hand the wait_for_state helper to a test module.

    pytest 9 imports test modules in ``importlib`` mode, so ``tests/`` never
    lands on ``sys.path`` and ``from conftest import wait_for_state`` raises
    ``ModuleNotFoundError``.  A fixture is the supported route for a conftest
    helper, and it is the only one that also keeps ty and pyrefly happy.

    Returns:
        The wait_for_state function itself, uncalled.

    """
    return wait_for_state
