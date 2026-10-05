"""
One page defines the exit codes; every other page names only real ones.

The CLI reference's ``## Exit codes`` table is the full definition, pinned to
``ExitCode`` elsewhere. Other pages keep advice: troubleshooting maps each code
to where to look, the scripting guide lists what its example script does on
each code it branches on, and the multi-page guide maps its own events to
codes. None of them restates what a code means; each links the reference
instead.

The checks here are derived rather than phrase matches. Every exit code a page
mentions in prose, and every code in a table under an exit-code heading, must
be an ``ExitCode`` member, so a code that is renumbered or dropped fails the
sweep wherever a page still names it. The scripting guide's table must list
exactly the codes its example's ``case`` statement branches on, read from the
example itself. Each checker returns its offences as strings, and a seeded
bad page proves it can fail.
"""

import re
from pathlib import Path

from saneless.vocabulary import ExitCode

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
DOCS_DIR = REPO_ROOT / "docs"
CLI_SCRIPTING = DOCS_DIR / "how-to" / "cli-scripting.md"
TROUBLESHOOTING = DOCS_DIR / "how-to" / "troubleshoot-a-failed-scan.md"
MULTI_PAGE_HOWTO = DOCS_DIR / "how-to" / "scan-a-multi-page-document.md"

EXIT_CODES = frozenset(int(code) for code in ExitCode)

# The canonical definition, as a link from a page under docs/how-to/.
CANONICAL_LINK = "../reference/cli-commands.md#exit-codes"

# A code, or a list of codes joined by commas, "and" or "or".
_CODE_LIST = r"\d+(?:(?:\s*,\s*(?:(?:and|or)\s+)?|\s+(?:and|or)\s+)\d+)*"

# "exit 2", "exits 2", "exited 0", "exit code 2", "exit codes 6 and 7",
# "exits with code 2", "exits with 143" and "(exit 9)", across a line wrap.
_MENTION = re.compile(
    rf"\bexit(?:s|ed)?(?:\s+with)?(?:\s+codes?)?\s+(?P<codes>{_CODE_LIST})\b",
    re.IGNORECASE,
)
_FENCE = re.compile(r"^\s*```")
_HEADING = re.compile(r"^#{1,6}\s+(?P<title>.+)$")
# A table that follows a bold marker rather than a heading, as each command's
# own table in the CLI reference does.
_EXIT_MARKER = re.compile(r"^\*\*Exit codes?:\*\*", re.IGNORECASE)
# A first cell that holds only codes: "9", or "129, 143".
_CODE_CELL = re.compile(rf"`?(?P<codes>{_CODE_LIST})`?")


def _doc_pages() -> list[Path]:
    """Return README.md and every Markdown page under ``docs/``."""
    pages = sorted(DOCS_DIR.rglob("*.md"))
    assert pages, f"no Markdown pages found under {DOCS_DIR}"
    return [README, *pages]


def _without_fences(text: str) -> list[str]:
    """
    Return the page's lines with every fenced block's lines blanked.

    A fenced block is a shell script or program output: its ``exit 1`` is the
    script's own exit and its ``case`` labels are what the script handles, not
    a claim about saneless. Blanking rather than dropping the lines keeps the
    line numbers an offence reports.
    """
    lines: list[str] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            lines.append("")
        else:
            lines.append("" if fenced else line)
    return lines


def _codes(listed: str) -> list[int]:
    """Return the integers in a matched code list such as ``129 and 143``."""
    return [int(number) for number in re.findall(r"\d+", listed)]


def _prose_mentions(lines: list[str]) -> list[tuple[int, int]]:
    """Return ``(line number, code)`` for every exit code the prose names."""
    text = "\n".join(lines)
    return [
        (text.count("\n", 0, match.start("codes") + number.start()) + 1, int(number[0]))
        for match in _MENTION.finditer(text)
        for number in re.finditer(r"\d+", match["codes"])
    ]


def _first_cell(row: str) -> str:
    """Return the first cell of a Markdown table row, stripped."""
    return row.strip().strip("|").split("|", 1)[0].strip()


def _table_mentions(lines: list[str]) -> list[tuple[int, int]]:
    """
    Return ``(line number, code)`` for each row of an exit-code table.

    A table counts when it sits under a heading that contains "exit code", or
    after a bold ``**Exit codes:**`` marker, up to the next heading. Only a
    first cell made of codes is read, so the header and separator rows are
    skipped.
    """
    mentions: list[tuple[int, int]] = []
    in_exit_section = False
    for number, line in enumerate(lines, start=1):
        heading = _HEADING.match(line)
        if heading:
            in_exit_section = "exit code" in heading["title"].lower()
            continue
        if _EXIT_MARKER.match(line):
            in_exit_section = True
            continue
        if not in_exit_section or not line.startswith("|"):
            continue
        cell = _CODE_CELL.fullmatch(_first_cell(line))
        if cell:
            mentions.extend((number, code) for code in _codes(cell["codes"]))
    return mentions


def _unknown_code_offences(text: str, name: str) -> list[str]:
    """Return ``name:line: code`` for every code ``text`` names that is not real."""
    lines = _without_fences(text)
    return [
        f"{name}:{number}: {code}"
        for number, code in sorted({*_prose_mentions(lines), *_table_mentions(lines)})
        if code not in EXIT_CODES
    ]


def _section(text: str, heading: str, name: str) -> str:
    """Return the body of ``heading`` up to the next heading of any level."""
    match = re.search(rf"^{re.escape(heading)}$", text, re.MULTILINE)
    assert match, f"{name} has no {heading!r} section"
    return re.split(r"^#{1,6} ", text[match.end() :], maxsplit=1, flags=re.MULTILINE)[0]


def _table_rows(text: str) -> list[str]:
    """Return the first run of Markdown table lines in ``text``."""
    rows: list[str] = []
    for line in text.splitlines():
        if line.startswith("|"):
            rows.append(line)
        elif rows:
            break
    return rows


def _table_codes(rows: list[str]) -> set[int]:
    """Return the codes in the first cells of ``rows``, skipping the header."""
    return {
        code
        for row in rows
        if (cell := _CODE_CELL.fullmatch(_first_cell(row)))
        for code in _codes(cell["codes"])
    }


def _case_labels(text: str) -> set[int]:
    """
    Return the integer labels of the ``case $exit_code in … esac`` block.

    A label may join codes with ``|``; the ``*)`` fallback is not a code.
    """
    match = re.search(
        r"^\s*case \"?\$exit_code\"? in\n(?P<body>.*?)^\s*esac\b",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, "no `case $exit_code in ... esac` block found"
    labels: set[int] = set()
    for label in re.findall(r"^\s*([^\s)#][^)]*)\)", match["body"], re.MULTILINE):
        labels.update(int(part) for part in label.split("|") if part.strip().isdigit())
    return labels


def _read(path: Path) -> tuple[str, str]:
    """Return a page's text and its repo-relative name."""
    return path.read_text(encoding="utf-8"), str(path.relative_to(REPO_ROOT))


# ---------------------------------------------------------------------------
# Seeded pages: each checker can fail
# ---------------------------------------------------------------------------


def test_seeded_prose_mention_of_an_unknown_code_is_reported() -> None:
    """A sentence naming a code ``ExitCode`` lacks is one offence."""
    assert 11 not in EXIT_CODES
    offences = _unknown_code_offences(
        "# Page\n\nIf the disk fills, the command exits 11.\n", "seeded.md"
    )
    assert offences == ["seeded.md:3: 11"]


def test_seeded_mention_across_a_line_wrap_and_in_a_list_is_reported() -> None:
    """A code list split over a line is still read, code by code."""
    offences = _unknown_code_offences(
        "Interrupted runs (exit 129 and\n42) keep their pages; see exit\ncode 2.\n",
        "seeded.md",
    )
    assert offences == ["seeded.md:2: 42"]


def test_seeded_table_row_under_an_exit_code_heading_is_reported() -> None:
    """A row for an unknown code under an exit-code heading is one offence."""
    text = (
        "## Exit codes\n\n"
        "| Code | What to do |\n"
        "|---|---|\n"
        "| 0 | Nothing |\n"
        "| 42 | Panic |\n\n"
        "## Something else\n\n"
        "| 42 | Not an exit code table |\n"
    )
    assert _unknown_code_offences(text, "seeded.md") == ["seeded.md:6: 42"]


def test_seeded_table_after_a_bold_exit_code_marker_is_reported() -> None:
    """A per-command table after ``**Exit codes:**`` is swept like a heading's."""
    text = "## `saneless jobs`\n\n**Exit codes:**\n\n| Code | Meaning |\n|-|-|\n| 77 | x |\n"
    assert _unknown_code_offences(text, "seeded.md") == ["seeded.md:7: 77"]


def test_seeded_fenced_case_labels_and_script_exits_are_not_mentions() -> None:
    """A script's own ``exit`` and its ``case`` labels are not claims."""
    text = (
        "## Exit codes\n\n"
        "```bash\n"
        "case $exit_code in\n"
        "  42) echo odd ;;\n"
        "esac\n"
        "exit 99\n"
        "```\n"
    )
    assert _unknown_code_offences(text, "seeded.md") == []


def test_seeded_case_labels_are_read_from_the_example() -> None:
    """Joined labels are split, and the fallback is not a code."""
    text = (
        "```bash\n"
        "case $exit_code in\n"
        "  0) echo ok ;;\n"
        "  6|7) echo delivered ;;\n"
        "  130) echo cancelled ;;\n"
        '  *) echo "other: $exit_code" ;;\n'
        "esac\n"
        "```\n"
    )
    assert _case_labels(text) == {0, 6, 7, 130}


# ---------------------------------------------------------------------------
# The real pages
# ---------------------------------------------------------------------------


def test_every_exit_code_the_docs_name_is_real() -> None:
    """README.md and every docs page name only codes ``ExitCode`` defines."""
    offences = [
        offence
        for path in _doc_pages()
        for offence in _unknown_code_offences(*_read(path))
    ]
    assert not offences, "these pages name an exit code saneless does not have:\n" + (
        "\n".join(offences)
    )


def test_scripting_table_lists_exactly_the_codes_its_example_handles() -> None:
    """
    The scripting guide's table is the example's ``case`` labels, and links on.

    A row the example does not branch on is a restated definition, and a
    branch with no row leaves a script author without the reason for it. The
    full definitions, including the codes the example leaves to its fallback,
    are one link away.
    """
    text, name = _read(CLI_SCRIPTING)
    example = _section(text, "### Basic scan with error handling", name)
    labels = _case_labels(example)
    assert labels <= EXIT_CODES, (
        f"{name}: the example branches on codes saneless does not have: "
        f"{sorted(labels - EXIT_CODES)}"
    )
    section = _section(text, "## Exit codes", name)
    documented = _table_codes(_table_rows(section))
    assert documented == labels, (
        f"{name}: the exit-code table lists {sorted(documented)}, but the example "
        f"branches on {sorted(labels)}"
    )
    assert CANONICAL_LINK in section, (
        f"{name}: the exit-code section does not link {CANONICAL_LINK}"
    )


def test_troubleshooting_table_is_advice_linked_to_the_definitions() -> None:
    """
    The troubleshooting table says where to look, not what a code means.

    What each code means is defined once, in the CLI reference, and the
    sentence after the table links there.
    """
    text, name = _read(TROUBLESHOOTING)
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("|"))
    end = next(
        index for index in range(start, len(lines)) if not lines[index].startswith("|")
    )
    header = [cell.strip() for cell in lines[start].strip().strip("|").split("|")]
    assert header == ["Exit code", "Where to look"], (
        f"{name}: the first table's columns are {header}, not a code and where to look"
    )
    after = "\n".join(lines[end : end + 5])
    assert CANONICAL_LINK in after, (
        f"{name}: the sentence after the first table does not link {CANONICAL_LINK}"
    )


def test_multi_page_exit_code_table_links_the_definitions() -> None:
    """The multi-page guide's event table links the codes' definitions."""
    text, name = _read(MULTI_PAGE_HOWTO)
    section = _section(text, "## Exit codes at a glance", name)
    assert _table_codes(_table_rows(section)), f"{name}: the section has no table"
    assert CANONICAL_LINK in section, (
        f"{name}: 'Exit codes at a glance' does not link {CANONICAL_LINK}"
    )
