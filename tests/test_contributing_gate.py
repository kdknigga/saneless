"""
CONTRIBUTING's description of the CI gate is derived from the workflows.

The contributor guide lists every command CI runs, in a table of workflow, job
and command, and repeats them as a block a contributor can paste to reproduce
the gate locally.  Both copies drift as soon as a step is added, renamed or
reordered in ``.github/workflows/ci.yml`` or ``docs.yml``: a new check that the
guide never mentions is one a contributor cannot run before pushing.

So the checks below read the workflows and derive the gate from them: every
``run:`` line of every job, minus the setup lines (the apt install, ``uv
sync`` and the Playwright browser download), plus a ``docker build`` for each
image the workflows build with ``docker/build-push-action``.  The table and the
block must name exactly those commands, the block in CI's order, and every
``playwright install`` the guide shows must fetch the browsers CI fetches.
Each checker returns its offences as strings, and a seeded bad guide proves it
can fail.
"""

from __future__ import annotations

import re
import textwrap
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from tests.workflow_support import (
    CI_WORKFLOW,
    DOCS_WORKFLOW,
    JOB_KEY,
    JOBS_KEY,
    is_comment,
    job_block,
    numbered,
    run_scripts,
    step_value,
    steps_using,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

# The section that holds the gate table and the "reproduce the gate" block.
GATE_HEADING = "## The checks CI runs"

BUILD_PUSH_ACTION = "docker/build-push-action@"
PLAYWRIGHT_INSTALL = "uv run playwright install"
# Script lines that prepare a runner rather than check anything.  A
# contributor's machine already has the headers, the environment and the
# browsers by the time they reproduce the gate.
SETUP_PREFIXES = ("sudo ", "apt", "uv sync", PLAYWRIGHT_INSTALL)

# One table row: workflow and job as code spans, then the command cell.
_TABLE_ROW = re.compile(
    r"^\| `(?P<workflow>[^`]+)` \| `(?P<job>[^`]+)` \| (?P<cell>.*) \|$",
    re.MULTILINE,
)
_CODE_SPAN = re.compile(r"`([^`]+)`")
_BASH_FENCE = re.compile(r"^```bash\n(?P<body>.*?)^```", re.MULTILINE | re.DOTALL)
_ANY_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
# A shell comment after a command: whitespace, then ``#`` to the end.
_SHELL_COMMENT = re.compile(r"\s+#.*$")
# ``playwright install`` and the words after it.  It is matched inside one
# code-block line or one inline code span at a time, so it never runs on into
# the next command, and a span wrapped across a line break is read whole.
_PLAYWRIGHT_MENTION = re.compile(r"playwright install(?P<args>(?:\s+[\w-]+)+)")


class Gate(NamedTuple):
    """One command CI runs to decide whether a change may merge."""

    workflow: str
    job: str
    command: str


def _job_names(lines: list[tuple[int, str]]) -> list[str]:
    """
    Return the job ids declared under a workflow's ``jobs:`` key, in order.

    Args:
        lines: ``(line number, line)`` pairs for the whole workflow.

    Returns:
        Every job id, in file order.

    """
    names: list[str] = []
    in_jobs = False
    for _, line in lines:
        if line.strip() and not is_comment(line) and not line[0].isspace():
            in_jobs = line.rstrip() == JOBS_KEY
            continue
        match = JOB_KEY.match(line) if in_jobs else None
        if match is not None:
            names.append(match.group("job"))
    return names


def _image_builds(block: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """
    Return a ``docker build`` command for each image a job builds.

    The image is built by an action rather than a ``run:`` line, so the
    command a contributor runs is derived from the step's ``tags:`` value.

    Args:
        block: The job's lines, as ``job_block`` returns them.

    Returns:
        ``(line number of the uses: line, command)`` pairs, in step order.

    """
    uses_lines = [
        number
        for number, line in block
        if re.match(r"^\s*(?:-\s+)?uses:\s*", line) and BUILD_PUSH_ACTION in line
    ]
    builds = steps_using(block, BUILD_PUSH_ACTION)
    return [
        (number, f"docker build -t {step_value(step, 'tags')} .")
        for number, step in zip(uses_lines, builds, strict=True)
    ]


def _gate_commands_in(workflow: str, lines: list[tuple[int, str]]) -> list[Gate]:
    """
    Return the gate commands of one workflow, in the order it runs them.

    Args:
        workflow: The workflow's file name, as the guide's table spells it.
        lines: ``(line number, line)`` pairs for the whole workflow.

    Returns:
        One ``Gate`` per checking command, setup lines excluded.

    """
    gates: list[Gate] = []
    for job in _job_names(lines):
        block = job_block(lines, job)
        commands = sorted([*run_scripts(block), *_image_builds(block)])
        gates.extend(
            Gate(workflow, job, command)
            for _, command in commands
            if not command.startswith(SETUP_PREFIXES)
        )
    return gates


def _gate_commands() -> list[Gate]:
    """Return every gate command of ci.yml, then docs.yml, in CI order."""
    return [
        gate
        for path in (CI_WORKFLOW, DOCS_WORKFLOW)
        for gate in _gate_commands_in(path.name, numbered(path))
    ]


def _ci_browsers() -> tuple[str, ...]:
    """Return the browsers CI's Playwright install step fetches."""
    installs = [
        command
        for _, command in run_scripts(numbered(CI_WORKFLOW))
        if command.startswith(PLAYWRIGHT_INSTALL)
    ]
    assert len(installs) == 1, f"ci.yml has no single Playwright install: {installs}"
    return _browsers(installs[0].removeprefix("uv run playwright install"))


def _browsers(args: str) -> tuple[str, ...]:
    """Return the browser names among ``playwright install``'s arguments."""
    return tuple(word for word in args.split() if not word.startswith("-"))


def _gate_section(text: str) -> str:
    """Return the guide's gate section, asserting that it exists."""
    match = re.search(rf"^{re.escape(GATE_HEADING)}$", text, re.MULTILINE)
    assert match, f"the guide has no {GATE_HEADING!r} section"
    return text[match.end() :].split("\n## ", 1)[0]


def _accepted_spans(gate: Gate) -> set[str]:
    """Return the code spans that name a gate in a table cell."""
    if gate.command.startswith("docker build -t "):
        tag = gate.command.removeprefix("docker build -t ").removesuffix(" .")
        return {gate.command, tag}
    return {gate.command}


def _table_offenders(text: str, gates: Sequence[Gate]) -> list[str]:
    """
    Compare the guide's gate table with the workflows' gate commands.

    A gate is covered by a row naming its workflow and job whose command cell
    has the command as a code span.  An image build is also covered by its
    tag as a code span, which is how a prose cell describes it.  Every code
    span in a row's command cell must be one of that job's commands or tags.

    Args:
        text: The guide's text.
        gates: The gate commands, as ``_gate_commands`` returns them.

    Returns:
        One string per missing or stale row entry.

    """
    rows = [
        (match["workflow"], match["job"], _CODE_SPAN.findall(match["cell"]))
        for match in _TABLE_ROW.finditer(_gate_section(text))
    ]
    offenders = [
        f"no table row for {gate.workflow} {gate.job}: `{gate.command}`"
        for gate in gates
        if not any(
            (workflow, job) == (gate.workflow, gate.job)
            and _accepted_spans(gate) & set(spans)
            for workflow, job, spans in rows
        )
    ]
    for workflow, job, spans in rows:
        known = {
            name
            for gate in gates
            if (gate.workflow, gate.job) == (workflow, job)
            for name in _accepted_spans(gate)
        }
        offenders.extend(
            f"stale table row: {workflow} {job} does not run `{span}`"
            for span in spans
            if span not in known
        )
    return offenders


def _block_lines(text: str) -> list[str]:
    """Return the reproduce block's commands, shell comments removed."""
    match = _BASH_FENCE.search(_gate_section(text))
    assert match, "the gate section has no bash block to reproduce the gate"
    return [
        _SHELL_COMMENT.sub("", line).strip()
        for line in match["body"].splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _block_offenders(
    text: str, gates: Sequence[Gate], browsers: tuple[str, ...]
) -> list[str]:
    """
    Compare the guide's reproduce block with the workflows' gate commands.

    Args:
        text: The guide's text.
        gates: The gate commands, in CI order.
        browsers: The browsers CI's Playwright install fetches.

    Returns:
        One string per missing, extra or misordered command, and per browser
        install that fetches other browsers than CI.

    """
    lines = _block_lines(text)
    commands = [gate.command for gate in gates]
    offenders = [
        f"the block does not run `{command}`"
        for command in commands
        if command not in lines
    ]
    installs = [line for line in lines if line.startswith(PLAYWRIGHT_INSTALL)]
    offenders.extend(
        f"the block runs `{line}`, which CI does not"
        for line in lines
        if line not in commands and line not in installs
    )
    present = [command for command in commands if command in lines]
    offenders.extend(
        f"the block runs `{later}` before `{earlier}`; CI runs `{earlier}` first"
        for earlier, later in pairwise(present)
        if lines.index(later) < lines.index(earlier)
    )
    if browsers and not installs:
        offenders.append(f"the block installs no browsers; CI installs {browsers}")
    offenders.extend(
        f"the block's `{line}` installs {found}; CI installs {browsers}"
        for line in installs
        if (found := _browsers(line.removeprefix(PLAYWRIGHT_INSTALL))) != browsers
    )
    return offenders


def _browser_mention_offenders(text: str, browsers: tuple[str, ...]) -> list[str]:
    """
    Return every ``playwright install`` in the guide that differs from CI's.

    Args:
        text: The guide's text.
        browsers: The browsers CI's Playwright install fetches.

    Returns:
        One ``line number: browsers`` string per differing mention.

    """
    units: list[tuple[int, str]] = []
    for fence in _ANY_FENCE.finditer(text):
        first = text.count("\n", 0, fence.start()) + 1
        units.extend(
            (first + offset, _SHELL_COMMENT.sub("", line))
            for offset, line in enumerate(fence.group().splitlines())
        )
    # Blank the fences out line for line, so span line numbers stay true.
    prose = _ANY_FENCE.sub(lambda fence: "\n" * fence.group().count("\n"), text)
    units.extend(
        (prose.count("\n", 0, span.start()) + 1, span[1])
        for span in _CODE_SPAN.finditer(prose)
    )
    return [
        f"{number}: installs {found}, CI installs {browsers}"
        for number, unit in sorted(units)
        for match in _PLAYWRIGHT_MENTION.finditer(unit)
        if (found := _browsers(match["args"])) != browsers
    ]


# ---------------------------------------------------------------------------
# The real guide and workflows
# ---------------------------------------------------------------------------


def test_the_workflows_yield_gate_commands() -> None:
    """The derivation finds CI's commands and the docs build, so it checks."""
    gates = _gate_commands()
    assert {gate.workflow for gate in gates} == {"ci.yml", "docs.yml"}, gates
    assert len(gates) == len(set(gates)), f"a gate command repeats: {gates}"


def test_the_gate_table_lists_every_ci_command() -> None:
    """Every gate command has a table row, and every row is a gate command."""
    offenders = _table_offenders(
        CONTRIBUTING.read_text(encoding="utf-8"), _gate_commands()
    )
    assert not offenders, (
        "CONTRIBUTING.md's gate table does not match the workflows:\n"
        + "\n".join(offenders)
    )


def test_the_reproduce_block_runs_the_gate_in_ci_order() -> None:
    """The block runs every gate command, nothing else, in CI's order."""
    offenders = _block_offenders(
        CONTRIBUTING.read_text(encoding="utf-8"), _gate_commands(), _ci_browsers()
    )
    assert not offenders, (
        "CONTRIBUTING.md's reproduce-the-gate block does not match the "
        "workflows:\n" + "\n".join(offenders)
    )


def test_every_playwright_install_fetches_the_browsers_ci_fetches() -> None:
    """A browser test that needs Firefox cannot pass on a Chromium-only setup."""
    offenders = _browser_mention_offenders(
        CONTRIBUTING.read_text(encoding="utf-8"), _ci_browsers()
    )
    assert not offenders, (
        "CONTRIBUTING.md installs other browsers than CI:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# Seeded inputs: each checker reports exactly the offence planted
# ---------------------------------------------------------------------------

_SEEDED_WORKFLOW = """\
name: Seeded

jobs:
  lint:
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000 # v1.0.0
      - name: Install headers
        run: |
          sudo apt-get update
          sudo apt-get install -y libsane-dev
      - run: uv sync --locked
      - run: uv run prek run --all-files
      - run: uv audit
  browser:
    steps:
      - run: uv run playwright install --with-deps chromium firefox
      - name: Browser tests
        run: uv run pytest -m browser
  docker:
    steps:
      - uses: docker/build-push-action@0000000000000000000000000000000000000000 # v1.0.0
        with:
          tags: saneless:seeded
      - run: uv run --no-project python scripts/smoke_image.py saneless:seeded
"""

SEEDED_GATES = (
    Gate("ci.yml", "lint", "uv run prek run --all-files"),
    Gate("ci.yml", "lint", "uv audit"),
    Gate("ci.yml", "browser", "uv run pytest -m browser"),
    Gate("ci.yml", "docker", "docker build -t saneless:seeded ."),
    Gate(
        "ci.yml",
        "docker",
        "uv run --no-project python scripts/smoke_image.py saneless:seeded",
    ),
    Gate("docs.yml", "build", "uv run --no-sync mkdocs build --strict"),
)
SEEDED_BROWSERS = ("chromium", "firefox")

_SEEDED_ROWS = {
    "prek": "| `ci.yml` | `lint` | `uv run prek run --all-files` |",
    "audit": "| `ci.yml` | `lint` | `uv audit` |",
    "browser": "| `ci.yml` | `browser` | `uv run pytest -m browser` |",
    "docker": (
        "| `ci.yml` | `docker` | builds the image as `saneless:seeded`, then "
        "`uv run --no-project python scripts/smoke_image.py saneless:seeded` |"
    ),
    "docs": "| `docs.yml` | `build` | `uv run --no-sync mkdocs build --strict` |",
}
_SEEDED_BLOCK = [
    "uv run prek run --all-files",
    "uv audit",
    "uv run playwright install chromium firefox   # once, to fetch the browsers",
    "uv run pytest -m browser",
    "docker build -t saneless:seeded .",
    "uv run --no-project python scripts/smoke_image.py saneless:seeded",
    "uv run --no-sync mkdocs build --strict",
]


def _seeded_guide(
    rows: Sequence[str] | None = None, block: Sequence[str] | None = None
) -> str:
    """Return a guide with the given (or the good) table rows and block."""
    table = "\n".join(
        [
            "| Workflow | Job | Command |",
            "|----------|-----|---------|",
            *(_SEEDED_ROWS.values() if rows is None else rows),
        ]
    )
    commands = "\n".join(_SEEDED_BLOCK if block is None else block)
    return (
        f"# Contributing\n\n{GATE_HEADING}\n\n{table}\n\n"
        f"You can reproduce the gate with:\n\n```bash\n{commands}\n```\n\n"
        "## Local pre-flight\n\n```bash\nuv run prek run --all-files\n```\n"
    )


def test_seeded_workflow_yields_the_gate_without_setup_lines(tmp_path: Path) -> None:
    """Apt, ``uv sync`` and the browser download are setup; the rest is gate."""
    workflow = tmp_path / "ci.yml"
    workflow.write_text(_SEEDED_WORKFLOW, encoding="utf-8")
    docs = tmp_path / "docs.yml"
    docs.write_text(
        textwrap.dedent(
            """\
            jobs:
              build:
                steps:
                  - run: uv sync --locked --only-group dev --no-install-project
                  - run: uv run --no-sync mkdocs build --strict
            """
        ),
        encoding="utf-8",
    )
    gates = [
        *_gate_commands_in("ci.yml", numbered(workflow)),
        *_gate_commands_in("docs.yml", numbered(docs)),
    ]
    assert gates == list(SEEDED_GATES)


def test_seeded_good_guide_reports_nothing() -> None:
    """A guide that matches the seeded gate passes every checker."""
    guide = _seeded_guide()
    assert _table_offenders(guide, SEEDED_GATES) == []
    assert _block_offenders(guide, SEEDED_GATES, SEEDED_BROWSERS) == []
    assert _browser_mention_offenders(guide, SEEDED_BROWSERS) == []


def test_seeded_table_missing_the_audit_row_is_reported() -> None:
    """A gate command with no table row is reported by name."""
    rows = [row for key, row in _SEEDED_ROWS.items() if key != "audit"]
    assert _table_offenders(_seeded_guide(rows=rows), SEEDED_GATES) == [
        "no table row for ci.yml lint: `uv audit`"
    ]


def test_seeded_table_with_a_stale_row_is_reported() -> None:
    """A row naming a command CI does not run is reported as stale."""
    rows = [*_SEEDED_ROWS.values(), "| `ci.yml` | `lint` | `uv run ruff check .` |"]
    assert _table_offenders(_seeded_guide(rows=rows), SEEDED_GATES) == [
        "stale table row: ci.yml lint does not run `uv run ruff check .`"
    ]


def test_seeded_block_with_two_commands_swapped_is_reported() -> None:
    """A block that runs two gate commands out of CI's order is reported."""
    block = list(_SEEDED_BLOCK)
    block[0], block[1] = block[1], block[0]
    assert _block_offenders(
        _seeded_guide(block=block), SEEDED_GATES, SEEDED_BROWSERS
    ) == [
        "the block runs `uv audit` before `uv run prek run --all-files`; "
        "CI runs `uv run prek run --all-files` first"
    ]


def test_seeded_block_missing_and_extra_commands_are_reported() -> None:
    """A dropped gate command and a command CI never runs are both reported."""
    block = [line for line in _SEEDED_BLOCK if line != "uv audit"]
    block.append("uv run pytest")
    assert _block_offenders(
        _seeded_guide(block=block), SEEDED_GATES, SEEDED_BROWSERS
    ) == [
        "the block does not run `uv audit`",
        "the block runs `uv run pytest`, which CI does not",
    ]


def test_seeded_chromium_only_install_is_reported() -> None:
    """Installing Chromium alone, when CI also installs Firefox, is reported."""
    block = [
        "uv run playwright install chromium" if "playwright" in line else line
        for line in _SEEDED_BLOCK
    ]
    guide = _seeded_guide(block=block)
    assert _block_offenders(guide, SEEDED_GATES, SEEDED_BROWSERS) == [
        "the block's `uv run playwright install chromium` installs ('chromium',); "
        "CI installs ('chromium', 'firefox')"
    ]
    prose = (
        "with Chromium installed (`uv run playwright install --with-deps\nchromium`)."
    )
    assert _browser_mention_offenders(prose, SEEDED_BROWSERS) == [
        "1: installs ('chromium',), CI installs ('chromium', 'firefox')"
    ]
