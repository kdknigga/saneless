"""
The suite runs in a fake home and its own working directory.

A test that reaches the developer's real home can read their live config --
Paperless URL and token included -- and write job state beside their real one.
These tests check the isolation from inside an ordinary test, so a change to the
autouse fixture that loosens it fails here rather than silently.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from saneless.config import OutputConfig

# Captured at import, before any fixture runs, so it is the real home.
_REAL_HOME = Path.home()
_TESTS = Path(__file__).resolve().parent
_REPOSITORY = _TESTS.parent

_XDG_BASES = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")


def test_home_is_neither_the_real_home_nor_in_the_repository() -> None:
    """HOME is a throwaway directory, away from both real homes of the code."""
    home = Path(os.environ["HOME"]).resolve()
    assert home != _REAL_HOME.resolve()
    assert not home.is_relative_to(_REPOSITORY)
    assert Path.home().resolve() == home


@pytest.mark.parametrize("variable", _XDG_BASES)
def test_each_xdg_base_is_under_the_fake_home(variable: str) -> None:
    """Every XDG base directory resolves inside the fake HOME."""
    home = Path(os.environ["HOME"])
    assert Path(os.environ[variable]).is_relative_to(home)


def test_xdg_runtime_dir_is_unset() -> None:
    """The per-session runtime directory of the real user is not visible."""
    assert "XDG_RUNTIME_DIR" not in os.environ


def test_working_directory_is_the_tests_own(tmp_path: Path) -> None:
    """The working directory is tmp_path, so no ./saneless.toml is reachable."""
    assert Path.cwd() == tmp_path
    assert not (Path.cwd() / "saneless.toml").exists()


def test_no_saneless_variable_leaks_in() -> None:
    """No SANELESS_* setting from the developer's shell reaches a test."""
    leaked = sorted(key for key in os.environ if key.startswith("SANELESS_"))
    assert leaked == []


def test_playwright_browsers_path_is_pinned() -> None:
    """The browser tests still find Chromium once HOME is a fake directory."""
    assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH")


def test_no_test_module_names_a_fixed_temp_path() -> None:
    """No test builds its settings on one shared, process-wide temp directory."""
    fixed = "saneless" + "-test"
    offenders = sorted(
        path.name
        for path in _TESTS.glob("*.py")
        if fixed in path.read_text(encoding="utf-8")
    )
    assert offenders == []


def test_hermetic_temp_dir_is_a_fresh_private_directory() -> None:
    """``tempfile.tempdir`` is pinned to a new directory only this user can use."""
    fake = Path(tempfile.gettempdir())
    assert tempfile.tempdir == str(fake)
    assert fake.is_dir()
    assert list(fake.iterdir()) == []
    assert fake.stat().st_mode & 0o077 == 0


def test_hermetic_default_tmp_dir_is_under_the_fake_temp_dir() -> None:
    """The default scratch directory lands in the test's temp dir, not in /tmp."""
    fake = Path(tempfile.gettempdir())
    assert OutputConfig().tmp_dir == fake / f"saneless-{os.getuid()}"


def test_hermetic_import_of_config_touches_no_temp_dir() -> None:
    """
    Importing ``saneless.config`` does not resolve the temp directory.

    ``tempfile.gettempdir()`` probes the file system on its first call and
    caches the answer in ``tempfile.tempdir``, so a module that computes its
    default at import leaves it set. A fresh interpreter shows it still unset.
    """
    result = subprocess.run(
        ["/bin/sh", "-c", 'exec "$SANELESS_TEST_PYTHON" -c "$SANELESS_TEST_CODE"'],
        env={
            **os.environ,
            "SANELESS_TEST_PYTHON": sys.executable,
            "SANELESS_TEST_CODE": (
                "import tempfile, saneless.config; print(tempfile.tempdir)"
            ),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "None"
