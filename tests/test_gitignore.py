"""
The ignore file hides secrets, caches and generated output, and nothing else.

A pattern copied from a generic template can match a path this project might
well create: a ``tags`` rule swallows a ``templates/tags/`` directory, a
``lib/`` rule swallows a ``lib`` package, and a vim swap-file glob matches
``.svg``.  Git gives no signal when that happens: ``git add`` of a new file
under such a path simply does nothing, and the file is missing from the next
clone.

These checks ask git itself which paths it would ignore, so they test the
patterns as git reads them rather than as text.  They run in both directions:
plausible project paths must not be ignored, and the files that hold a live
paperless-ngx token, the tool caches and the generated site must be.  An ignore
file emptied by mistake fails the second direction.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GITIGNORE = REPO_ROOT / ".gitignore"

# Paths a contributor could reasonably add to the project.  None may be
# ignored, or ``git add`` silently drops them.
PROJECT_PATHS = (
    "src/saneless/web/templates/tags/list.html",
    "src/saneless/lib/helpers.py",
    "src/saneless/web/static/_logo.svg",
    "src/saneless/build/x.py",
    "tests/fixtures/saneless.toml",
    "tests/fixtures/scan.log",
    "docs/var/x.md",
)

# Paths that hold a secret, a cache or generated output.  Each must stay
# ignored: the first three can hold a live paperless-ngx token.
IGNORED_PATHS = (
    "saneless.toml",
    "config/saneless.toml",
    ".env",
    ".venv/x",
    "src/saneless/__pycache__/x.pyc",
    ".pytest_cache/x",
    ".ruff_cache/x",
    ".coverage",
    "site/index.html",
    "test-results/x",
    ".serena/x",
    ".claude/worktrees/x/y",
)

# Whitespace then ``#``: the shape of a trailing comment, which an ignore file
# does not support.  Git reads the whole line, comment included, as a pattern.
_TRAILING_COMMENT = re.compile(r"\s#")


def _ignored(paths: tuple[str, ...]) -> set[str]:
    """
    Return the subset of ``paths`` that the repository's ignore rules match.

    ``--no-index`` makes git answer from the patterns alone, so a tracked path
    is still reported when a rule matches it.  Exit status 1 with no output
    means git ignores none of the paths; any other failure is reported.

    Args:
        paths: Repository-relative paths, which need not exist.

    Returns:
        The paths git reports as ignored.

    """
    # Every argv element is a literal and the repository travels in ``cwd``;
    # the paths go over stdin, so no variable data reaches the argv.
    result = subprocess.run(
        ["/usr/bin/git", "check-ignore", "--no-index", "--stdin"],
        cwd=REPO_ROOT,
        input="\n".join(paths),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode in {0, 1}, (
        f"git check-ignore failed with exit {result.returncode}: {result.stderr}"
    )
    return set(result.stdout.splitlines())


def _trailing_comment_offenders(text: str) -> list[str]:
    """
    Return the pattern lines of an ignore file that carry a trailing comment.

    Args:
        text: The ignore file's contents.

    Returns:
        One ``line number: line`` string per offending line.

    """
    return [
        f"{number}: {line}"
        for number, line in enumerate(text.splitlines(), start=1)
        if line.strip()
        and not line.lstrip().startswith("#")
        and _TRAILING_COMMENT.search(line)
    ]


@pytest.mark.parametrize("path", PROJECT_PATHS)
def test_a_project_path_is_not_ignored(path: str) -> None:
    """A path a contributor could add is not swallowed by an ignore rule."""
    assert path not in _ignored((path,)), (
        f"{path} is ignored. `git add` would silently drop it; run "
        f"`git check-ignore -v --no-index {path}` to find the rule"
    )


@pytest.mark.parametrize("path", IGNORED_PATHS)
def test_a_secret_cache_or_generated_path_is_ignored(path: str) -> None:
    """A token file, a tool cache or generated output stays out of git."""
    assert path in _ignored((path,)), (
        f"{path} is no longer ignored, so a commit can pick it up"
    )


def test_every_ignore_line_is_a_bare_pattern() -> None:
    """No pattern line carries a trailing comment, which git would match."""
    offenders = _trailing_comment_offenders(GITIGNORE.read_text(encoding="utf-8"))
    assert not offenders, (
        "a comment after a pattern is part of the pattern, so the line matches "
        "nothing. Put the comment on its own line:\n" + "\n".join(offenders)
    )


def test_the_trailing_comment_check_reports_a_seeded_line() -> None:
    """A pattern followed by a comment is reported; an escaped hash is not."""
    seeded = "# own-line comment\n*.pyc\n!*.svg  # keep icons\n\\#*\\#\n"
    assert _trailing_comment_offenders(seeded) == ["3: !*.svg  # keep icons"]
