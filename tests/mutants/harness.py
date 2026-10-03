"""
Run one named test against a mutated copy of the repository.

A hand-written mutant is a list of exact text edits. The harness copies the
repository into a temporary directory, checks the named test passes on the
clean copy, applies the edits and checks the same test now fails. Each edit's
anchor must occur exactly once, so a mutant whose anchor has moved or been
duplicated fails loudly instead of silently testing nothing.

The copy runs with its own ``src/`` first on ``PYTHONPATH`` and its own root
as pytest's rootdir, so both ``saneless`` and ``tests`` resolve inside it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]

PLANNING_DIRECTORY = ".planning"
"""The project's planning notes: not part of the product, so never copied."""

_LIST_TIMEOUT = 60
"""Seconds the file listing may take."""


class Edit(NamedTuple):
    """One exact text replacement in a file of the copied repository."""

    path: str
    """The file, relative to the repository root."""
    old: str
    """The text to replace; it must occur exactly once."""
    new: str
    """The replacement."""


def _listed_paths() -> list[str]:
    """
    List every tracked or untracked-but-not-ignored path in the repository.

    Returns:
        Repository-relative paths, as git prints them.

    """
    listing = subprocess.run(
        [
            "/usr/bin/git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        timeout=_LIST_TIMEOUT,
    )
    return sorted({name for name in listing.stdout.decode("utf-8").split("\0") if name})


def copy_repository(destination: Path) -> Path:
    """
    Copy the repository's files, without the planning notes, into ``destination``.

    Paths git still lists but that are gone from disk are skipped. File modes
    are kept, and symbolic links are copied as links.

    Args:
        destination: The directory to create the copy in.

    Returns:
        ``destination``.

    """
    for relative in _listed_paths():
        if relative.split("/", 1)[0] == PLANNING_DIRECTORY:
            continue
        source = REPO_ROOT / relative
        if not source.is_symlink() and not source.exists():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(source.readlink())
        else:
            shutil.copy2(source, target)
    return destination


def apply_edits(root: Path, edits: Sequence[Edit]) -> None:
    """
    Apply each edit to the tree at ``root``.

    Args:
        root: The copied repository.
        edits: The replacements, applied in order.

    Raises:
        AssertionError: If an edit's anchor does not occur exactly once.

    """
    for edit in edits:
        target = root / edit.path
        text = target.read_text(encoding="utf-8")
        count = text.count(edit.old)
        assert count == 1, (
            f"{edit.path}: the mutant's anchor occurs {count} times, expected once"
        )
        target.write_text(text.replace(edit.old, edit.new), encoding="utf-8")


def run_test(
    copy: Path, nodeid: str, *, timeout: float = 300.0
) -> subprocess.CompletedProcess[str]:
    """
    Run one test inside a copied repository.

    The interpreter and node id travel in the environment so the argv stays a
    literal, which keeps the call on ruff's subprocess allow-list.

    Args:
        copy: The copied repository to run in.
        nodeid: The pytest node id, relative to the repository root.
        timeout: Seconds the child pytest may take.

    Returns:
        The finished child, with its output captured.

    """
    return subprocess.run(
        [
            "/bin/sh",
            "-c",
            'exec "$SANELESS_TEST_PYTHON" -m pytest -p no:cacheprovider -q '
            '"$SANELESS_TEST_NODEID"',
        ],
        cwd=copy,
        env={
            **os.environ,
            "PYTHONPATH": str(copy / "src"),
            "SANELESS_TEST_PYTHON": sys.executable,
            "SANELESS_TEST_NODEID": nodeid,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    """
    Format a child run's output for an assertion message.

    Args:
        result: The finished child.

    Returns:
        Its exit status, stdout and stderr.

    """
    return (
        f"exit status {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def assert_test_passes(result: subprocess.CompletedProcess[str], nodeid: str) -> None:
    """
    Assert the run passed ``nodeid`` and failed nothing.

    Args:
        result: The finished child.
        nodeid: The test it ran.

    """
    assert result.returncode == 0, f"{nodeid} did not pass\n{_output(result)}"
    assert "passed" in result.stdout, f"{nodeid} did not pass\n{_output(result)}"
    assert "failed" not in result.stdout, f"{nodeid} failed\n{_output(result)}"


def assert_test_fails(result: subprocess.CompletedProcess[str], nodeid: str) -> None:
    """
    Assert the run failed ``nodeid`` as a test failure, not a crash.

    Args:
        result: The finished child.
        nodeid: The test it ran.

    """
    message = f"{nodeid} did not fail under the mutant\n{_output(result)}"
    assert result.returncode == 1, message
    assert f"FAILED {nodeid}" in result.stdout, message


def assert_test_skipped(result: subprocess.CompletedProcess[str], nodeid: str) -> None:
    """
    Assert the run skipped ``nodeid`` and passed nothing.

    Args:
        result: The finished child.
        nodeid: The test it ran.

    """
    message = f"{nodeid} was not skipped under the mutant\n{_output(result)}"
    assert result.returncode == 0, message
    assert "skipped" in result.stdout, message
    assert "passed" not in result.stdout, message


def _clean_then_mutated(
    tmp_path: Path, nodeid: str, edits: Sequence[Edit]
) -> subprocess.CompletedProcess[str]:
    """
    Copy the repository, check ``nodeid`` passes, apply the edits and rerun it.

    Args:
        tmp_path: A fresh directory for the copy.
        nodeid: The test to run.
        edits: The mutant.

    Returns:
        The run against the mutated copy.

    """
    copy = copy_repository(tmp_path / "repo")
    assert_test_passes(run_test(copy, nodeid), nodeid)
    apply_edits(copy, edits)
    return run_test(copy, nodeid)


def check_mutant(tmp_path: Path, nodeid: str, edits: Sequence[Edit]) -> None:
    """
    Assert ``nodeid`` passes on a clean copy and fails under ``edits``.

    Args:
        tmp_path: A fresh directory for the copy.
        nodeid: The test that must kill the mutant.
        edits: The mutant.

    """
    assert_test_fails(_clean_then_mutated(tmp_path, nodeid, edits), nodeid)


def check_mutant_skips(tmp_path: Path, nodeid: str, edits: Sequence[Edit]) -> None:
    """
    Assert ``nodeid`` passes on a clean copy and is skipped under ``edits``.

    Args:
        tmp_path: A fresh directory for the copy.
        nodeid: The test whose skip the mutant must trigger.
        edits: The mutant.

    """
    assert_test_skipped(_clean_then_mutated(tmp_path, nodeid, edits), nodeid)
