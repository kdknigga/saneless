"""
The suite runs in a fake home and its own working directory.

A test that reaches the developer's real home can read their live config --
Paperless URL and token included -- and write job state beside their real one.
These tests check the isolation from inside an ordinary test, so a change to the
autouse fixture that loosens it fails here rather than silently.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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
