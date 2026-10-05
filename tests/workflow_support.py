"""
Line readers for the GitHub Actions workflows under ``.github/workflows/``.

The suite checks the workflows as plain text rather than parsing them with a
YAML library: every workflow here uses the same two-space layout, so a job is
the lines under ``  <job>:`` and a step is the lines from one ``      - `` to
the next.  These readers find a job, split it into steps, read a step's keys
and collect the shell a workflow runs.  More than one test module derives its
checks from the workflows, so the readers live here once.

``tests/`` is a package, so pytest's default prepend mode imports it as
``tests.*``, and the helper is imported by that package name.  Import it as
``from tests.workflow_support import ...``.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "BLOCK_SCALAR",
    "CI_WORKFLOW",
    "DOCS_WORKFLOW",
    "JOBS_KEY",
    "JOB_KEY",
    "RUN_KEY",
    "STEP_START",
    "WORKFLOW_DIR",
    "indent",
    "is_comment",
    "job_block",
    "job_needs",
    "job_steps",
    "key_mapping",
    "numbered",
    "run_scripts",
    "step_value",
    "steps_using",
    "strip_trailing_comment",
]

_REPO_ROOT = Path(__file__).resolve().parents[1]

WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOW_DIR / "ci.yml"
DOCS_WORKFLOW = WORKFLOW_DIR / "docs.yml"

# The top-level key that opens the job table, and one job's key beneath it at
# the two-space indent every workflow here uses. Same shape as a compose
# service key: a name alone on its line, ending in a colon.
JOBS_KEY = "jobs:"
JOB_KEY = re.compile(r"^  (?P<job>[a-z0-9_-]+):\s*$")
# The start of one step in a job's ``steps:`` list, at the six-space indent
# every job here uses.
STEP_START = re.compile(r"^      -\s")
# A ``run:`` key, bare or as a step's first key, with whatever follows it.
RUN_KEY = re.compile(r"^(?P<indent>\s*)(?:-\s+)?run:\s*(?P<value>.*)$")
# A block-scalar indicator: the script is on the lines below the key.
BLOCK_SCALAR = re.compile(r"^[|>][+-]?$")


def is_comment(line: str) -> bool:
    """Say whether a YAML (or fenced-YAML) line is commented out."""
    return line.lstrip().startswith("#")


def numbered(path: Path) -> list[tuple[int, str]]:
    """Return ``(line number, line)`` pairs for a file, 1-based."""
    return list(enumerate(path.read_text(encoding="utf-8").splitlines(), start=1))


def strip_trailing_comment(value: str) -> str:
    """Return a YAML scalar without a trailing `` # comment``."""
    return value.split(" #", 1)[0].strip()


def job_block(lines: list[tuple[int, str]], job: str) -> list[tuple[int, str]]:
    """
    Return the raw lines of one job, key line excluded.

    The block runs from the line after ``  <job>:`` under the top-level
    ``jobs:`` key to the line before the next job key, or the next top-level
    key, or the end of the file. Lines are returned as read, indentation and
    comments included, so a caller can tell nesting apart.

    Args:
        lines: ``(line number, line)`` pairs for the whole workflow.
        job: The job id to find.

    Returns:
        The job's lines in file order, or an empty list when no such job is
        declared under ``jobs:``.

    """
    in_jobs = False
    block: list[tuple[int, str]] | None = None
    for number, line in lines:
        significant = line.strip() and not is_comment(line)
        if significant and not line[0].isspace():
            if block is not None:
                break
            in_jobs = line.rstrip() == JOBS_KEY
            continue
        if not in_jobs:
            continue
        match = JOB_KEY.match(line)
        if match is not None:
            if block is not None:
                break
            if match.group("job") == job:
                block = []
            continue
        if block is not None:
            block.append((number, line))
    return block or []


def indent(line: str) -> int:
    """Return the number of leading spaces on a line."""
    return len(line) - len(line.lstrip(" "))


def key_mapping(
    lines: list[tuple[int, str]], key: str, indent_width: int
) -> dict[str, str] | None:
    """
    Return the flat mapping held by ``key`` at exactly ``indent_width`` spaces.

    Both spellings the workflows use are read: a flow mapping on the key's own
    line (``key: {a: b, c: d}``) and a block of ``name: value`` lines nested
    one level deeper. Comments are dropped. A scalar value comes back under
    the empty-string key, so it can never compare equal to a real mapping.

    Args:
        lines: The lines to search, raw.
        key: The key name, without its colon.
        indent_width: The key's indentation in spaces.

    Returns:
        The mapping, or ``None`` when the key is absent.

    """
    opener = re.compile(rf"^ {{{indent_width}}}{re.escape(key)}:\s*(?P<value>.*)$")
    for index, (_, line) in enumerate(lines):
        match = opener.match(line)
        if match is None:
            continue
        value = strip_trailing_comment(match.group("value"))
        if value.startswith("{") and value.endswith("}"):
            pairs = [part.partition(":") for part in value[1:-1].split(",")]
            return {name.strip(): rest.strip() for name, _, rest in pairs if rest}
        if value:
            return {"": value}
        mapping: dict[str, str] = {}
        for _, nested in lines[index + 1 :]:
            if not nested.strip() or is_comment(nested):
                continue
            if indent(nested) <= indent_width:
                break
            name, _, rest = nested.strip().partition(":")
            mapping[name] = strip_trailing_comment(rest)
        return mapping
    return None


def job_needs(block: list[tuple[int, str]]) -> set[str]:
    """
    Return the job ids a job's ``needs:`` names.

    Args:
        block: The job's lines, as ``job_block`` returns them.

    Returns:
        Every job named, whether as a scalar, a flow list or a block list.

    """
    for index, (_, line) in enumerate(block):
        match = re.match(r"^    needs:\s*(?P<value>.*)$", line)
        if match is None:
            continue
        value = strip_trailing_comment(match.group("value"))
        if value.startswith("["):
            return {part.strip() for part in value.strip("[]").split(",") if part}
        if value:
            return {value}
        needs: set[str] = set()
        for _, nested in block[index + 1 :]:
            if not nested.strip() or is_comment(nested):
                continue
            if indent(nested) <= indent(line):
                break
            needs.add(strip_trailing_comment(nested.strip().removeprefix("-")))
        return needs
    return set()


def job_steps(block: list[tuple[int, str]]) -> list[list[str]]:
    """
    Split a job into its steps, each as stripped significant lines.

    Args:
        block: The job's lines, as ``job_block`` returns them.

    Returns:
        One list per step, in order. Lines before the first step are dropped.

    """
    steps: list[list[str]] = []
    for _, line in block:
        if STEP_START.match(line):
            steps.append([])
        if steps and line.strip() and not is_comment(line):
            steps[-1].append(line.strip())
    return steps


def steps_using(block: list[tuple[int, str]], action: str) -> list[list[str]]:
    """Return every step of a job whose ``uses:`` names ``action``."""
    return [
        step
        for step in job_steps(block)
        if any(
            re.match(r"^(?:-\s+)?uses:\s*", line) and action in line for line in step
        )
    ]


def step_value(step: list[str], key: str) -> str | None:
    """Return a step key's value, comment stripped, or ``None`` when absent."""
    for line in step:
        name, separator, rest = line.removeprefix("- ").partition(":")
        if separator and name.strip() == key:
            return strip_trailing_comment(rest)
    return None


def run_scripts(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """
    Return every line of shell a workflow runs, block scalars included.

    Args:
        lines: ``(line number, line)`` pairs for the whole workflow.

    Returns:
        The ``run:`` line itself for an inline script, and every line of the
        block for a ``run: |`` script.

    """
    scripts: list[tuple[int, str]] = []
    block_indent: int | None = None
    for number, line in lines:
        if block_indent is not None:
            if not line.strip():
                continue
            if indent(line) > block_indent:
                scripts.append((number, line.strip()))
                continue
            block_indent = None
        match = RUN_KEY.match(line)
        if match is None or is_comment(line):
            continue
        value = match.group("value").strip()
        if BLOCK_SCALAR.match(value):
            block_indent = len(match.group("indent"))
        else:
            scripts.append((number, value))
    return scripts
