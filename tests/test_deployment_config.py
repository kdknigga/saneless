"""
Static text tests for the documented Docker deployment (CFG-09, M-30, D-09).

saneless rewrites ``config.toml`` atomically: it writes a temp file beside the
config and renames it over the original. That rename only works when the
container sees the config *directory*. A single-file bind mount makes the kernel
refuse the rename with EBUSY, so every profile write fails on the deployment the
docs used to recommend. These tests hold the compose example and every doc page
to the read-write directory mount ``./config:/etc/saneless``, and they keep out
the old, false claim that the container fails to start without ``config.toml``.

The later tests hold the configuration, environment-variable and CLI references,
the scripting how-to, the empty-page explanation and ``saneless.toml.example``
to the Phase 27 behaviour: strict keys and ``SANELESS_*`` names, XDG paths,
validated log levels, ``-v``, the literal profile title, and the optional
``--title`` (CFG-01..CFG-08, CFG-10, CFG-11).

The Phase 28 tests pin the exit-code tables in the scripting how-to and the CLI
reference, and the troubleshooting how-to, to ``ExitCode``: every documented
code is a real one, and every real one is documented (D-07, D-13).

The Phase 29 test pins the architecture page's "Memory, disk and timeouts"
subsection, and the absence of the two claims that phase falsified (D-20).

Plain-text assertions only: the contract is what an operator copies, not what a
YAML parser makes of it.
"""

from __future__ import annotations

import re
from pathlib import Path

from saneless.vocabulary import ExitCode

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker-compose.yml"
DOCS_DIR = REPO_ROOT / "docs"

DIRECTORY_MOUNT = "./config:/etc/saneless"
SINGLE_FILE_MOUNT = "config.toml:/etc/saneless/config.toml"

DEPLOY_HOWTO = DOCS_DIR / "how-to" / "deploy-docker-compose.md"
DOCKER_REFERENCE = DOCS_DIR / "reference" / "docker.md"
QUICK_START = DOCS_DIR / "getting-started" / "quick-start.md"
PROFILE_HOWTO = DOCS_DIR / "how-to" / "configure-scan-profiles.md"

CONFIG_REFERENCE = DOCS_DIR / "reference" / "configuration.md"
ENV_REFERENCE = DOCS_DIR / "reference" / "environment-variables.md"
ARCHITECTURE = DOCS_DIR / "explanation" / "architecture.md"
FIRST_CLI_SCAN = DOCS_DIR / "getting-started" / "first-cli-scan.md"
INSTALL_BARE_METAL = DOCS_DIR / "how-to" / "install-bare-metal.md"
TOML_EXAMPLE = REPO_ROOT / "saneless.toml.example"
CLI_REFERENCE = DOCS_DIR / "reference" / "cli-commands.md"
CLI_SCRIPTING = DOCS_DIR / "how-to" / "cli-scripting.md"


def _doc_pages() -> list[Path]:
    """Return every Markdown page under ``docs/``, asserting there is at least one."""
    pages = sorted(DOCS_DIR.rglob("*.md"))
    assert pages, f"no Markdown pages found under {DOCS_DIR}"
    return pages


def _deployment_files() -> list[Path]:
    """Return the compose example plus every doc page."""
    return [COMPOSE, *_doc_pages()]


def test_compose_mounts_the_config_directory() -> None:
    """The compose example mounts ``./config`` read-write at ``/etc/saneless``."""
    lines = [line.strip() for line in COMPOSE.read_text(encoding="utf-8").splitlines()]
    assert f"- {DIRECTORY_MOUNT}" in lines, (
        f"{COMPOSE.name} has no volume line exactly '- {DIRECTORY_MOUNT}'"
    )


def test_no_single_file_config_mount_anywhere() -> None:
    """No compose file or doc page bind-mounts ``config.toml`` as a single file."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for path in _deployment_files()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if SINGLE_FILE_MOUNT in line
    ]
    assert not offenders, "single-file config.toml mount found:\n" + "\n".join(
        offenders
    )


def test_no_fail_to_start_claim() -> None:
    """No page claims the container fails to start when the config is missing."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for path in _deployment_files()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if "fail to start" in line.lower() and "config" in line.lower()
    ]
    assert not offenders, "false 'fail to start' config claim found:\n" + "\n".join(
        offenders
    )


def test_deploy_docs_use_the_directory_mount() -> None:
    """Each Docker deployment page shows the directory mount."""
    for page in (DEPLOY_HOWTO, DOCKER_REFERENCE, QUICK_START):
        text = page.read_text(encoding="utf-8")
        assert DIRECTORY_MOUNT in text, (
            f"{page.relative_to(REPO_ROOT)} does not show '{DIRECTORY_MOUNT}'"
        )


def test_deploy_doc_explains_missing_config_and_migration() -> None:
    """The compose how-to explains a missing file, EBUSY, and the migration."""
    text = DEPLOY_HOWTO.read_text(encoding="utf-8")
    name = DEPLOY_HOWTO.relative_to(REPO_ROOT)
    assert "defaults" in text, f"{name} does not say a missing file means defaults"
    assert "environment variables" in text, (
        f"{name} does not mention environment variables for a missing file"
    )
    assert "EBUSY" in text, f"{name} does not explain the EBUSY rename failure"
    migration = [
        line
        for line in text.splitlines()
        if "config.toml" in line and "config/" in line and "mv" in line
    ]
    assert migration, f"{name} has no migration step moving config.toml into ./config/"


def test_deploy_doc_says_where_auto_profiles_writes_without_config_toml() -> None:
    """
    With no ``config.toml``, the doc says the write lands at ``/saneless.toml`` (WR-06).

    The CLI writes ``./saneless.toml`` when no config file was loaded, which in
    the image is ``/saneless.toml`` (its runtime stage sets no WORKDIR, so the
    working directory is ``/``): in the container layer, and first in
    the search order. The how-to used to imply the write lands in ``./config``.
    """
    text = DEPLOY_HOWTO.read_text(encoding="utf-8")
    name = DEPLOY_HOWTO.relative_to(REPO_ROOT)
    assert "`/saneless.toml`" in text, (
        f"{name} does not say auto-profiles writes /saneless.toml "
        "when config.toml is missing"
    )
    assert "touch config/config.toml" in text, (
        f"{name} does not tell container users to create config/config.toml first"
    )


def test_cli_reference_says_where_auto_profiles_writes_in_the_image() -> None:
    """The CLI reference names ``/saneless.toml`` for the image (WR-06)."""
    text = CLI_REFERENCE.read_text(encoding="utf-8")
    name = CLI_REFERENCE.relative_to(REPO_ROOT)
    assert "`/saneless.toml`" in text, (
        f"{name} does not say where the image writes with no config file loaded"
    )


def test_profile_howto_describes_force_as_a_merge() -> None:
    """The profile how-to describes ``--force`` as a merge, not an overwrite."""
    text = PROFILE_HOWTO.read_text(encoding="utf-8")
    name = PROFILE_HOWTO.relative_to(REPO_ROOT)
    for needle in (
        "auto_generated",
        "default_tags",
        "Refreshed",
        "Skipped (not auto-generated)",
    ):
        assert needle in text, f"{name} does not mention {needle!r}"
    assert "to overwrite existing auto-generated profiles" not in text, (
        f"{name} still describes --force as an overwrite"
    )


def test_profile_howto_title_is_literal() -> None:
    """The profile ``title`` is documented as a literal default, not a template."""
    text = PROFILE_HOWTO.read_text(encoding="utf-8")
    name = PROFILE_HOWTO.relative_to(REPO_ROOT)
    assert "title template" not in text.lower(), f"{name} still calls title a template"
    assert "Scan <" in text, f"{name} does not document the 'Scan <time>' fallback"


def test_profile_howto_unwritable_example_is_current() -> None:
    """The unwritable-config example no longer blames the Docker Compose examples."""
    text = PROFILE_HOWTO.read_text(encoding="utf-8")
    assert "as in the Docker Compose examples" not in text, (
        f"{PROFILE_HOWTO.relative_to(REPO_ROOT)} still says the compose examples "
        "mount the config read-only"
    )


def _read(path: Path) -> tuple[str, Path]:
    """Return a file's text and its repo-relative name for failure messages."""
    return path.read_text(encoding="utf-8"), path.relative_to(REPO_ROOT)


def test_configuration_reference_documents_xdg_and_levels() -> None:
    """The configuration reference names the XDG bases, every level and ``~``."""
    text, name = _read(CONFIG_REFERENCE)
    for needle in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "CRITICAL", "~", "expanded"):
        assert needle in text, f"{name} does not mention {needle!r}"


def test_configuration_reference_title_is_literal() -> None:
    """The configuration reference no longer calls the profile title a template."""
    text, name = _read(CONFIG_REFERENCE)
    assert "title template" not in text.lower(), f"{name} still calls title a template"


def test_configuration_reference_documents_unknown_key_rejection() -> None:
    """The configuration reference says unknown keys are rejected with valid keys."""
    text, name = _read(CONFIG_REFERENCE)
    for needle in ("unknown key", "valid keys"):
        assert needle in text, f"{name} does not mention {needle!r}"


def test_environment_reference_documents_unknown_variables() -> None:
    """The environment reference documents rejection and profile variables."""
    text, name = _read(ENV_REFERENCE)
    assert "SANELESS_PAPERLES__TOKEN" in text, (
        f"{name} has no misspelt-variable example (SANELESS_PAPERLES__TOKEN)"
    )
    assert "exit" in text, f"{name} does not give the exit code for unknown variables"
    assert "Profile fields cannot be set via environment variables" not in text, (
        f"{name} still claims profile fields cannot be set from the environment"
    )
    assert "SANELESS_PROFILES__" in text, (
        f"{name} does not show the SANELESS_PROFILES__<NAME>__<FIELD> form"
    )


def test_search_path_lists_name_xdg_config_home() -> None:
    """Every page listing the per-user config path names ``$XDG_CONFIG_HOME``."""
    offenders = []
    for page in (
        CONFIG_REFERENCE,
        ARCHITECTURE,
        FIRST_CLI_SCAN,
        INSTALL_BARE_METAL,
        QUICK_START,
    ):
        text, name = _read(page)
        if ".config/saneless" in text and "XDG_CONFIG_HOME" not in text:
            offenders.append(str(name))
    assert not offenders, (
        "pages list ~/.config/saneless without $XDG_CONFIG_HOME: "
        + ", ".join(offenders)
    )


def test_toml_example_title_is_literal() -> None:
    """The example config describes ``title`` as a literal title for a blank one."""
    text, name = _read(TOML_EXAMPLE)
    title_lines = [
        line for line in text.splitlines() if line.lstrip("# ").startswith("title =")
    ]
    assert title_lines, f"{name} has no title line"
    for line in title_lines:
        comment = line.lstrip("# ").partition("#")[2].lower()
        assert "blank" in comment, f"{name}: title comment does not mention blank"
        assert "template" not in comment, f"{name}: title comment says template"


def _table_row(text: str, first_cell: str) -> str:
    """Return the Markdown table row whose first cell is ``first_cell``."""
    rows = [line for line in text.splitlines() if line.startswith(f"| {first_cell} |")]
    assert rows, f"no table row starting with {first_cell}"
    return rows[0]


def test_cli_reference_verbose_is_saneless_debug() -> None:
    """``-v`` is documented as saneless's own debug detail, mirrored to stderr."""
    text, name = _read(CLI_REFERENCE)
    assert "sets log level to DEBUG" not in text, (
        f"{name} still says -v sets the log level to DEBUG"
    )
    row = _table_row(text, "`-v, --verbose`")
    assert "stderr" in row, f"{name}: the -v row does not mention stderr"


def test_cli_reference_title_is_optional() -> None:
    """The scan synopsis and ``--title`` row show the title as optional."""
    text, name = _read(CLI_REFERENCE)
    assert "scan [--title TEXT]" in text, (
        f"{name}: scan synopsis still requires --title"
    )
    row = _table_row(text, "`--title`")
    assert "(required)" not in row, f"{name}: the --title row still says required"


def test_cli_reference_force_is_a_merge() -> None:
    """``auto-profiles --force`` is documented as a merge that can fail on EBUSY."""
    text, name = _read(CLI_REFERENCE)
    assert "Overwrite existing profiles" not in text, (
        f"{name} still describes --force as an overwrite"
    )
    assert "auto_generated" in text, f"{name} does not mention auto_generated"
    assert "EBUSY" in text or "single file" in text, (
        f"{name} does not explain the single-file bind mount failure"
    )


def test_cli_reference_help_needs_no_config() -> None:
    """The CLI reference says ``--help`` works without a valid config."""
    text, name = _read(CLI_REFERENCE)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    assert any("--help" in s and "config" in s for s in sentences), (
        f"{name} does not say --help works without a config"
    )


def test_no_log_level_option_documented() -> None:
    """No doc page (other than the historical PRD) cites a ``--log-level`` option."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for path in _doc_pages()
        if path != DOCS_DIR / "PRD.md"
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if "--log-level" in line
    ]
    assert not offenders, "nonexistent --log-level option cited:\n" + "\n".join(
        offenders
    )


def test_scripting_exit_code_two_examples() -> None:
    """The scripting how-to's exit-code section lists the new exit-2 causes."""
    text, name = _read(CLI_SCRIPTING)
    _, heading, rest = text.partition("## Exit codes")
    assert heading, f"{name} has no '## Exit codes' section"
    section = rest.split("\n## ", 1)[0]
    for needle in ("unknown config key", "SANELESS_"):
        assert needle in section, f"{name}: exit-code section lacks {needle!r}"


EXIT_CODES = frozenset(int(code) for code in ExitCode)
_CODE_ROW = re.compile(r"^\| (\d+) \|", re.MULTILINE)
_COMMAND_HEADING = re.compile(r"^## `saneless ([a-z-]+)`$", re.MULTILINE)

OLD_SCRIPTING_ABORT = (
    "aborted at the flip prompt or not confirmed within `flip_timeout_seconds` "
    "exits with code 1"
)
OLD_REFERENCE_ABORT = "manual duplex aborted at the flip prompt or flip wait timed out"


def _documented_codes(section: str) -> set[int]:
    """Return the integer first cells of the Markdown table rows in ``section``."""
    return {int(match) for match in _CODE_ROW.findall(section)}


def _section(text: str, heading: str, name: Path) -> str:
    """
    Return the body of the ``heading`` section, up to the next ``## `` heading.

    The heading must be a whole line, so ``## Exit codes`` never matches a
    longer heading that merely starts with it.
    """
    match = re.search(rf"^{re.escape(heading)}$", text, re.MULTILINE)
    assert match, f"{name} has no {heading!r} section"
    return text[match.end() :].split("\n## ", 1)[0]


def _command_exit_tables(text: str, name: Path) -> dict[str, str]:
    """
    Return each ``saneless <cmd>`` section's ``**Exit codes:**`` table text.

    The table is the run of lines starting with ``|`` after the marker, so the
    prose that follows it is never read as a row.
    """
    tables: dict[str, str] = {}
    headings = list(_COMMAND_HEADING.finditer(text))
    assert headings, f"{name} has no '## `saneless <cmd>`' sections"
    for heading in headings:
        section = text[heading.end() :].split("\n## ", 1)[0]
        _, marker, rest = section.partition("**Exit codes:**")
        assert marker, f"{name}: `saneless {heading[1]}` has no exit-code table"
        rows: list[str] = []
        for line in rest.strip("\n").splitlines():
            if not line.startswith("|"):
                break
            rows.append(line)
        tables[heading[1]] = "\n".join(rows)
    return tables


def test_scripting_exit_code_table_matches_exit_code_enum() -> None:
    """The scripting how-to's exit-code table lists exactly the ExitCode values."""
    text, name = _read(CLI_SCRIPTING)
    section = _section(text, "## Exit codes", name)
    assert _documented_codes(section) == EXIT_CODES, (
        f"{name}: exit-code table {sorted(_documented_codes(section))} "
        f"!= ExitCode {sorted(EXIT_CODES)}"
    )


def test_cli_reference_global_exit_code_table_matches_exit_code_enum() -> None:
    """The CLI reference's global exit-code table lists exactly the ExitCode values."""
    text, name = _read(CLI_REFERENCE)
    section = _section(text, "## Exit codes", name)
    assert _documented_codes(section) == EXIT_CODES, (
        f"{name}: global exit-code table {sorted(_documented_codes(section))} "
        f"!= ExitCode {sorted(EXIT_CODES)}"
    )


def test_cli_reference_command_exit_codes_are_real() -> None:
    """
    Every command's table lists exactly the codes that command can exit with.

    ``serve`` has no 1: it scans nothing itself, and SANE failing to initialise
    is a start-up failure, exit 2 (D-07 amendment, WR-07). It does have 130:
    Ctrl-C before uvicorn has started is a cancel through the CLI guard, while
    Ctrl-C once uvicorn is running is its graceful stop, exit 0 (D-03). No
    command but ``scan`` builds a PDF, so only ``scan`` has 4; only ``scan``
    and ``serve`` construct a Paperless client, so only they have 3; ``jobs``
    never touches SANE, so it has no 1.
    """
    text, name = _read(CLI_REFERENCE)
    tables = _command_exit_tables(text, name)
    for command, table in tables.items():
        codes = _documented_codes(table)
        assert codes, f"{name}: `saneless {command}` exit-code table is empty"
        assert codes <= EXIT_CODES, (
            f"{name}: `saneless {command}` documents unknown codes "
            f"{sorted(codes - EXIT_CODES)}"
        )
    assert _documented_codes(tables["scan"]) == {0, 1, 2, 3, 4, 5, 130}
    assert _documented_codes(tables["devices"]) == {0, 1, 2, 5, 130}
    assert _documented_codes(tables["jobs"]) == {0, 2, 5, 130}
    assert _documented_codes(tables["serve"]) == {0, 2, 3, 5, 130}
    assert _documented_codes(tables["auto-profiles"]) == {0, 1, 2, 5, 130}
    assert "abort" not in _table_row(tables["scan"], "1").lower(), (
        f"{name}: scan's exit-1 row still describes a flip-prompt abort"
    )
    # D-07: "No scanner found" is exit 2 on every command where it occurs (WR-06).
    for command in ("scan", "auto-profiles"):
        assert "no scanner" in _table_row(tables[command], "2").lower(), (
            f"{name}: `saneless {command}` does not document no scanner under 2"
        )
        assert "no scanner" not in _table_row(tables[command], "1").lower(), (
            f"{name}: `saneless {command}` documents no scanner under 1"
        )


def test_job_database_documented_under_exit_code_two() -> None:
    """
    A job database saneless cannot use is exit 2, never exit 5 (D-07 amendment).

    ``StorageError`` is a setup problem; exit 5 is only for an exception that is
    not a saneless type.
    """
    scripting, scripting_name = _read(CLI_SCRIPTING)
    reference, reference_name = _read(CLI_REFERENCE)
    tables = _command_exit_tables(reference, reference_name)
    sections = {
        f"{scripting_name} ## Exit codes": _section(
            scripting, "## Exit codes", scripting_name
        ),
        f"{reference_name} ## Exit codes": _section(
            reference, "## Exit codes", reference_name
        ),
        f"{reference_name} saneless jobs": tables["jobs"],
        f"{reference_name} saneless serve": tables["serve"],
    }
    for label, section in sections.items():
        assert "job database" in _table_row(section, "2").lower(), (
            f"{label}: the exit-2 row does not mention the job database"
        )
        assert "job database" not in _table_row(section, "5").lower(), (
            f"{label}: the exit-5 row mentions the job database"
        )


def test_flip_prompt_abort_documented_as_cancelled() -> None:
    """An abort at the flip prompt is documented as exit 130, not 1 (D-02, D-03)."""
    for path, old in (
        (CLI_SCRIPTING, OLD_SCRIPTING_ABORT),
        (CLI_REFERENCE, OLD_REFERENCE_ABORT),
    ):
        text, name = _read(path)
        flat = " ".join(text.split())
        assert old not in flat, f"{name} still documents the abort as exit 1"
        sentences = re.split(r"(?<=[.!?])\s+", flat)
        assert any("flip prompt" in s and "130" in s for s in sentences), (
            f"{name} has no sentence saying a flip-prompt abort exits 130"
        )


TROUBLESHOOTING = DOCS_DIR / "how-to" / "troubleshoot-a-failed-scan.md"
SCANNER_HOST_DISCOVERY = DOCS_DIR / "how-to" / "scanner-host-discovery.md"
MKDOCS = REPO_ROOT / "mkdocs.yml"


def _heading_section(text: str, word: str, name: Path) -> str:
    """Return the body of the one ``## `` section whose heading contains ``word``."""
    matches = [
        match
        for match in re.finditer(r"^## (.+)$", text, re.MULTILINE)
        if word in match[1]
    ]
    assert len(matches) == 1, f"{name}: expected one '## ' heading with {word!r}"
    return text[matches[0].end() :].split("\n## ", 1)[0]


def _first_table(text: str) -> str:
    """Return the first run of Markdown table lines in ``text``."""
    rows: list[str] = []
    for line in text.splitlines():
        if line.startswith("|"):
            rows.append(line)
        elif rows:
            break
    return "\n".join(rows)


def test_troubleshooting_page_is_linked_and_covers_every_exit_code() -> None:
    """
    The troubleshooting how-to exists, is navigable, and covers every code (D-13).

    Its opening table lists exactly the ExitCode values, it has a section for
    each kind of failure, and the pages with their own Troubleshooting section
    link to it. A job database problem is a setup problem (exit 2), so it is
    described under configuration and never under unexpected errors (D-07
    amendment). The unexpected-error section says what to attach to a bug
    report and warns about the Paperless token in a DEBUG log (T-28-51).
    """
    assert TROUBLESHOOTING.is_file(), f"{TROUBLESHOOTING} does not exist"
    text, name = _read(TROUBLESHOOTING)
    table_codes = _documented_codes(_first_table(text))
    assert table_codes == EXIT_CODES, (
        f"{name}: first table {sorted(table_codes)} != ExitCode {sorted(EXIT_CODES)}"
    )
    nav, _ = _read(MKDOCS)
    assert "how-to/troubleshoot-a-failed-scan.md" in nav, (
        "mkdocs.yml nav does not list the troubleshooting how-to"
    )
    headings = re.findall(r"^#+ (.+)$", text, re.MULTILINE)
    for word in (
        "Scanner",
        "Paperless",
        "PDF",
        "Configuration",
        "python-sane",
        "Cancelled",
        "Unexpected",
    ):
        assert any(word in heading for heading in headings), (
            f"{name} has no heading containing {word!r}"
        )
    unexpected = _heading_section(text, "Unexpected", name).lower()
    for needle in ("log file", "bug", "token"):
        assert needle in unexpected, (
            f"{name}: the unexpected-error section does not mention {needle!r}"
        )
    assert "job database" not in unexpected, (
        f"{name}: the unexpected-error section mentions the job database"
    )
    configuration = _heading_section(text, "Configuration", name).lower()
    assert "job database" in configuration, (
        f"{name}: the configuration section does not mention the job database"
    )
    for page in (INSTALL_BARE_METAL, SCANNER_HOST_DISCOVERY):
        page_text, page_name = _read(page)
        assert "troubleshoot-a-failed-scan.md" in page_text, (
            f"{page_name} does not link to the troubleshooting how-to"
        )


ARCHITECTURE_MEMORY_HEADING = "### Memory, disk and timeouts"

# The Phase 29 claims the architecture page now makes, each with the substrings
# that carry it.
#
# Substrings rather than whole sentences, deliberately: rewording the page for
# clarity should not fail this test, but dropping a guarantee should. Each entry
# is one thing an operator decides on -- how much RAM a long scan needs, what
# `min_free_space_mb` is for, whether a hung scanner can be waited out or has to
# be restarted -- so an assertion firing here means the page stopped answering a
# question someone actually asks it.
#
# 29-RESEARCH.md Finding 11 enumerated fourteen doc sentences this phase
# falsified and found that no doc-truth test pinned a single one of them, which
# is why nothing would have caught a page left stale. This is that test.
ARCHITECTURE_MEMORY_CLAIMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "roughly one decoded page is held in memory while scanning",
        ("one decoded page",),
    ),
    (
        "disk is checked per page, against that page plus the reserve",
        ("per page", "min_free_space_mb"),
    ),
    (
        "one per-page timeout bounds the feeder and the flatbed alike",
        ("120", "feeder", "flatbed"),
    ),
    (
        "a timeout cancels the read and waits for it before closing the device",
        ("cancel", "before closing"),
    ),
    (
        "a read that never returns does not block shutdown",
        ("daemon", "docker stop"),
    ),
    (
        "a wedged scanner refuses the next scan and asks for a restart",
        ("refus", "restart saneless"),
    ),
)


def _subsection(text: str, heading: str, name: Path) -> str:
    """Return the body of the one ``### `` section headed exactly ``heading``."""
    marker = f"\n{heading}\n"
    assert text.count(marker) == 1, f"{name}: expected one {heading!r} heading"
    body = text.split(marker, 1)[1]
    for following in ("\n## ", "\n### "):
        body = body.split(following, 1)[0]
    return body


def test_architecture_page_states_the_memory_disk_and_timeout_rules() -> None:
    """The architecture page pins Phase 29's memory, disk and timeout claims."""
    text, name = _read(ARCHITECTURE)
    assert ARCHITECTURE_MEMORY_HEADING in text, (
        f"{name} has no {ARCHITECTURE_MEMORY_HEADING!r} subsection"
    )
    body = _subsection(text, ARCHITECTURE_MEMORY_HEADING, name).lower()
    for claim, needles in ARCHITECTURE_MEMORY_CLAIMS:
        for needle in needles:
            assert needle in body, (
                f"{name}: the memory, disk and timeouts subsection no longer "
                f"says that {claim} (looked for {needle!r})"
            )

    # The two claims the page used to make and must not make again. A PNG
    # re-encode is lossless, but the bytes in the PDF are not the bytes the
    # scanner sent, and pages are no longer carried through the pipeline as
    # in-memory images. Reverting either correction fails here.
    lowered = text.lower()
    assert "byte-for-byte" not in lowered, (
        f"{name} claims the embedded image data is byte-for-byte identical to "
        "what the scanner produced; it is lossless, not byte-identical"
    )
    assert "pil images" not in lowered, (
        f"{name}'s pipeline diagram still carries pages as PIL Images; they are "
        "spooled to disk as they arrive"
    )
    assert "lossless" in lowered, (
        f"{name} no longer says the PNG-to-PDF embed is lossless"
    )
