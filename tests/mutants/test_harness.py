"""
The mutated-copy harness copies, edits and runs exactly what it is told to.

A gap mutant is only evidence when the harness under it is honest: the copy
holds the project, an edit lands in exactly one place, and the mutant checks
stay out of an ordinary run until they are asked for.
"""

from __future__ import annotations

import re
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from tests.mutants.harness import (
    PLANNING_DIRECTORY,
    REPO_ROOT,
    Edit,
    apply_edits,
    check_mutant,
    copy_repository,
)

if TYPE_CHECKING:
    from pathlib import Path

_SELF_TEST = "tests/mutants/test_harness.py::test_check_mutant_sees_a_killed_mutant"

_CHILD_SECONDS = 120
"""How long a child pytest may take to collect this module."""


def _collect(*, with_mutants: bool) -> subprocess.CompletedProcess[str]:
    """
    Collect this module in a child pytest run from the repository root.

    Args:
        with_mutants: Whether the child is given ``--mutants``.

    Returns:
        The finished child, with its output captured.

    """
    options = ["--mutants"] if with_mutants else []
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "-q",
            *options,
            "tests/mutants/test_harness.py",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=_CHILD_SECONDS,
    )


def test_apply_edits_replaces_an_anchor_that_occurs_once(tmp_path: Path) -> None:
    """An anchor found exactly once is replaced, and nothing else changes."""
    target = tmp_path / "pkg" / "module.py"
    target.parent.mkdir()
    target.write_text("first = 1\nsecond = 2\n", encoding="utf-8")

    apply_edits(tmp_path, [Edit("pkg/module.py", "second = 2", "second = 3")])

    assert target.read_text(encoding="utf-8") == "first = 1\nsecond = 3\n"


def test_apply_edits_refuses_a_missing_anchor(tmp_path: Path) -> None:
    """An anchor that is not there fails loudly instead of testing nothing."""
    target = tmp_path / "module.py"
    target.write_text("first = 1\n", encoding="utf-8")

    with pytest.raises(AssertionError, match=r"module\.py: .* 0 times"):
        apply_edits(tmp_path, [Edit("module.py", "absent = 0", "absent = 1")])

    assert target.read_text(encoding="utf-8") == "first = 1\n"


def test_apply_edits_refuses_a_repeated_anchor(tmp_path: Path) -> None:
    """An anchor found twice fails, because the mutant would be ambiguous."""
    target = tmp_path / "module.py"
    target.write_text("value = 1\nvalue = 1\n", encoding="utf-8")

    with pytest.raises(AssertionError, match=r"module\.py: .* 2 times"):
        apply_edits(tmp_path, [Edit("module.py", "value = 1", "value = 2")])

    assert target.read_text(encoding="utf-8") == "value = 1\nvalue = 1\n"


def test_copy_repository_holds_the_project_without_the_planning_notes(
    tmp_path: Path,
) -> None:
    """The copy has the package, the suite and the project file, and no notes."""
    copy = copy_repository(tmp_path / "repo")

    assert copy == tmp_path / "repo"
    assert (copy / "pyproject.toml").is_file()
    assert (copy / "src" / "saneless" / "__init__.py").is_file()
    assert (copy / "tests" / "conftest.py").is_file()
    assert not (copy / PLANNING_DIRECTORY).exists()


def test_mutant_tests_are_deselected_without_the_option() -> None:
    """An ordinary run deselects the mutant checks rather than running them."""
    result = _collect(with_mutants=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _SELF_TEST not in result.stdout
    assert "1 deselected" in result.stdout


def test_mutant_tests_are_collected_with_the_option() -> None:
    """``--mutants`` collects the mutant checks alongside everything else."""
    result = _collect(with_mutants=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _SELF_TEST in result.stdout
    assert re.search(r"\d+ deselected", result.stdout) is None


@pytest.mark.mutant
def test_check_mutant_sees_a_killed_mutant(tmp_path: Path) -> None:
    """The module-entry test fails when ``python -m saneless`` exits early."""
    check_mutant(
        tmp_path,
        "tests/test_main_module.py::test_python_dash_m_saneless_runs_the_cli",
        [
            Edit(
                "src/saneless/__main__.py",
                'if __name__ == "__main__":\n    main()',
                'if __name__ == "__main__":\n    raise SystemExit(3)',
            )
        ],
    )
