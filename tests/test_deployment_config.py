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

The Phase 30 tests hold the shipped compose template to D-17 -- the paperless
connection is commented out, because a live line there silently overrides
``./config/config.toml`` -- and to APPL-11's consume-directory mount and
APPL-12's ``TZ``. They derive the documentation's expectations from the
source: the route decorators, the ``WebConfig`` fields and the
``RequestRejection`` members, so a future addition cannot ship undocumented
(APPL-05, APPL-07, APPL-10, APPL-11, APPL-12).

The Phase 31 tests pin the packaging identity ``pyproject.toml`` publishes:
the 0.2.0 series, the PEP 639 license keys, the Alpha maturity classifier, the
absence of the deprecated ``License ::`` classifier -- which nothing in the
build or publish toolchain rejects, so this is the only thing that does -- and
the unchanged ``saneless`` distribution name (D-01, D-02, D-05, DLVR-08).

The Phase 31 identity guard then holds every tracked file outside
``.planning/`` to the current GitHub owner, so a stale project URL cannot
reach a reader or a registry (CI-02, DLVR-01, D-08..D-12). Its README tests
hold the front page's own examples to the source and to the filesystem: the
scan example shows ``--title``, every ``source`` value is the spelling the
profile model defaults to, and every documentation deep link names a page
that exists (DOCS-02, D-45).

The citation guard holds every source, template, style and script file under
``src/`` to comments that give their own reasons, because the planning records
they might otherwise point at do not ship with the product.

Plain-text assertions only: the contract is what an operator copies, not what a
YAML parser makes of it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from saneless.config import (
    OutputConfig,
    ProfileConfig,
    WebConfig,
    is_placeholder_token,
)
from saneless.vocabulary import (
    ExitCode,
    JobState,
    RequestRejection,
    rejection_message,
    rejection_status_code,
    state_label,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker-compose.yml"
README = REPO_ROOT / "README.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"
DOCS_DIR = REPO_ROOT / "docs"

DIRECTORY_MOUNT = "./config:/etc/saneless"
# The same directory at the same container path, spelled for a bind-mount flag,
# where the host side has to be absolute and quoted (row 31).
ABSOLUTE_DIRECTORY_MOUNT = '"$(pwd)/config:/etc/saneless"'
DIRECTORY_MOUNT_FORMS = (DIRECTORY_MOUNT, ABSOLUTE_DIRECTORY_MOUNT)
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

# Where `saneless auto-profiles` writes inside the image when no config
# file was loaded. The CLI writes `./saneless.toml`, and the runtime
# stage's WORKDIR is what decides where that resolves to.
AUTO_PROFILES_CONTAINER_PATH = "`/var/lib/saneless/saneless.toml`"


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
    """
    Each Docker deployment page mounts the configuration *directory*.

    Two spellings satisfy this, and which one is correct depends on the syntax:
    a compose ``volumes:`` entry names the directory relative to the compose
    file, while a bind-mount flag needs the absolute form (row 31). Both name
    the same directory at the same container path, which is the thing Phase 27
    D-09 requires; the single-file mount is banned separately.
    """
    for page in (DEPLOY_HOWTO, DOCKER_REFERENCE, QUICK_START):
        text = page.read_text(encoding="utf-8")
        assert any(form in text for form in DIRECTORY_MOUNT_FORMS), (
            f"{page.relative_to(REPO_ROOT)} shows the configuration directory "
            f"mounted in neither of these forms: {list(DIRECTORY_MOUNT_FORMS)}"
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
    With no ``config.toml``, the doc names the real write path (WR-06).

    The CLI writes ``./saneless.toml`` when no config file was loaded. The
    image's runtime stage sets ``WORKDIR /var/lib/saneless``, so that resolves
    to ``/var/lib/saneless/saneless.toml`` -- inside the declared data volume,
    where it outlives the container, and still ahead of ``/etc/saneless`` in
    the config search order. The how-to used to imply the write lands in
    ``./config``, and before the WORKDIR existed it landed at ``/`` instead.
    """
    text = DEPLOY_HOWTO.read_text(encoding="utf-8")
    name = DEPLOY_HOWTO.relative_to(REPO_ROOT)
    assert AUTO_PROFILES_CONTAINER_PATH in text, (
        f"{name} does not say auto-profiles writes "
        f"{AUTO_PROFILES_CONTAINER_PATH} when config.toml is missing"
    )
    assert "touch config/config.toml" in text, (
        f"{name} does not tell container users to create config/config.toml first"
    )


def test_cli_reference_says_where_auto_profiles_writes_in_the_image() -> None:
    """The CLI reference names the image's real write path (WR-06)."""
    text = CLI_REFERENCE.read_text(encoding="utf-8")
    name = CLI_REFERENCE.relative_to(REPO_ROOT)
    assert AUTO_PROFILES_CONTAINER_PATH in text, (
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

# The sentence at the top of the CLI reference that counts the commands, and
# the number words it may be written with.  The count is compared against the
# sections actually present rather than against a literal, so the seventh
# command is caught by the same test as the sixth.
_COUNT_SENTENCE = re.compile(r"^saneless provides (\w+) commands\b", re.MULTILINE)
_NUMBER_WORDS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

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

    ``doctor`` has the same four codes as ``jobs``, for three separate reasons.
    No 1: it never fails on SANE at all -- Amendment A-1 turns a missing
    python-sane into a ``FAIL`` row rather than a refusal, and the scanner
    check reports an unreachable device instead of raising. No 3: it does
    construct a Paperless client, but ``test_connection`` returns a status
    rather than raising, and a URL the client cannot be built from becomes the
    "not found at that URL" row instead of a ``PaperlessError``. No 4: it
    assembles nothing.
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
    assert _documented_codes(tables["doctor"]) == {0, 2, 5, 130}
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


def test_cli_reference_command_count_matches_its_sections() -> None:
    """
    The opening sentence's command count equals the number of command sections.

    Derived rather than pinned to a literal: the sentence said "five" for as
    long as there were five commands and would have gone on saying it for the
    sixth, the seventh and the eighth. Comparing it against the sections that
    are actually present is the only form of this test that keeps working.
    """
    text, name = _read(CLI_REFERENCE)
    match = _COUNT_SENTENCE.search(text)
    assert match, f"{name} no longer states how many commands saneless provides"
    stated = _NUMBER_WORDS.get(match[1])
    assert stated is not None, (
        f"{name}: {match[1]!r} is not a number word this test knows; "
        f"add it to _NUMBER_WORDS"
    )
    documented = len(_COMMAND_HEADING.findall(text))
    assert stated == documented, (
        f"{name} says saneless provides {match[1]} commands but documents {documented}"
    )


def test_doctor_promises_no_json_mode() -> None:
    """
    Neither document offers ``doctor --json``, because it does not ship.

    Research found no consumer for one -- not in the docs, the tests, the
    Dockerfile or the compose file -- and CONTEXT says a JSON mode ships only
    with a reader. A document that promises one would be a wire contract
    somebody parses before anybody implements it, so the decision is pinned
    here rather than left to be re-litigated.
    """
    for path in (CLI_REFERENCE, CLI_SCRIPTING):
        text, name = _read(path)
        assert "doctor --json" not in text, (
            f"{name} promises a `doctor --json` that does not exist"
        )


def test_scripting_does_not_claim_every_read_command_has_json() -> None:
    """
    The JSON section names the commands that have ``--json``, rather than all.

    ``doctor`` is a read command with no ``--json``, so the old blanket claim
    became false the moment it shipped. This is the assertion that would have
    caught it.
    """
    text, name = _read(CLI_SCRIPTING)
    assert "All read commands support" not in text, (
        f"{name} still claims every read command supports --json"
    )
    assert "doctor" in text, f"{name} does not mention doctor's exit semantics"


def test_scripting_documents_jobs_json_created_at_as_utc() -> None:
    """
    The documented ``created_at`` example carries the ``+00:00`` offset.

    ``jobs --json`` is a machine contract: APPL-12 localised the human table
    and deliberately left the JSON in UTC, so the worked example a script
    author copies has to show the offset. Without this assertion a later plan
    could localise the contract and the document would agree with it.
    """
    text, name = _read(CLI_SCRIPTING)
    examples = re.findall(r'"created_at": "([^"]+)"', text)
    assert examples, f"{name} shows no created_at example to check"
    for value in examples:
        assert value.endswith("+00:00"), (
            f"{name} documents created_at as {value!r}, which is not UTC ISO-8601"
        )
    assert "UTC" in text, f"{name} does not say the JSON timestamps stay UTC"


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


# ---------------------------------------------------------------------------
# Phase 30: the shipped deployment template (D-17, APPL-07, APPL-11, APPL-12)
# ---------------------------------------------------------------------------

# Every environment variable that carries the paperless-ngx connection. A live
# line here silently overrides ``./config/config.toml`` -- the U-01 finding.
OVERRIDING_ENV_KEYS = ("SANELESS_PAPERLESS__URL", "SANELESS_PAPERLESS__TOKEN")

_TOKEN_ASSIGNMENT = re.compile(r"SANELESS_PAPERLESS__TOKEN=(\S*)")

CONSUME_MOUNT_SUBSTRING = ":/consume"


def _is_comment(line: str) -> bool:
    """Say whether a YAML (or fenced-YAML) line is commented out."""
    return line.lstrip().startswith("#")


def _numbered(path: Path) -> list[tuple[int, str]]:
    """Return ``(line number, line)`` pairs for a file, 1-based."""
    return list(enumerate(path.read_text(encoding="utf-8").splitlines(), start=1))


def _comment_lines_above(lines: list[tuple[int, str]], index: int) -> int:
    """Count the unbroken run of comment lines immediately above ``index``."""
    count = 0
    for _, line in reversed(lines[:index]):
        if not _is_comment(line):
            break
        count += 1
    return count


def test_compose_ships_no_live_paperless_environment_line() -> None:
    """No live ``environment:`` line sets the paperless URL or token (D-17)."""
    offenders = [
        f"{COMPOSE.name}:{number}: {line.strip()}"
        for number, line in _numbered(COMPOSE)
        if not _is_comment(line)
        if any(f"{key}=" in line for key in OVERRIDING_ENV_KEYS)
    ]
    assert not offenders, (
        "the shipped compose template still sets the paperless connection in "
        "its environment: block, which silently overrides "
        "./config/config.toml (D-17, U-01):\n" + "\n".join(offenders)
    )


def test_compose_says_the_environment_block_overrides_the_config_file() -> None:
    """The commented block explains the override and the upgrade action."""
    text = COMPOSE.read_text(encoding="utf-8").lower()
    for needle in ("override", "config.toml"):
        assert needle in text, (
            f"{COMPOSE.name} does not say that an environment line "
            f"{needle}s the config file (D-17)"
        )
    assert "delete" in text or "remove" in text, (
        f"{COMPOSE.name} does not tell an operator who copied an earlier "
        "version to remove their own token line, so their real config.toml "
        "stays overridden and the status strip stays red"
    )


def test_compose_ships_the_consume_directory_mount_with_its_explanation() -> None:
    """The consume-directory mount is present with two lines of why (APPL-11)."""
    lines = _numbered(COMPOSE)
    mounts = [
        index
        for index, (_, line) in enumerate(lines)
        if CONSUME_MOUNT_SUBSTRING in line
    ]
    assert len(mounts) == 1, (
        f"{COMPOSE.name}: expected exactly one consume-directory bind mount "
        f"line containing {CONSUME_MOUNT_SUBSTRING!r}, found {len(mounts)}"
    )
    explanation = _comment_lines_above(lines, mounts[0])
    assert explanation >= 2, (
        f"{COMPOSE.name}: the consume-directory mount has {explanation} "
        "comment lines above it; APPL-11 asks for a two-line explanation"
    )
    text = COMPOSE.read_text(encoding="utf-8").lower()
    assert "consume_dir" in text, (
        f"{COMPOSE.name} does not say that paperless.consume_dir must name the "
        "same path, so an operator who uncomments the mount gets nothing"
    )


def test_compose_sets_the_timezone_with_an_explanation() -> None:
    """The compose template sets ``TZ`` and says why (APPL-12, Pitfall 10)."""
    lines = _numbered(COMPOSE)
    settings = [index for index, (_, line) in enumerate(lines) if "TZ=" in line]
    assert len(settings) == 1, (
        f"{COMPOSE.name}: expected exactly one TZ line, found {len(settings)}"
    )
    index = settings[0]
    assert not _is_comment(lines[index][1]), (
        f"{COMPOSE.name}: the TZ line is commented out, so the shipped "
        "template still reports UTC for every timestamp saneless displays"
    )
    assert _comment_lines_above(lines, index) >= 1, (
        f"{COMPOSE.name}: the TZ line has no comment above it explaining that "
        "a container reports UTC by default"
    )
    assert "utc" in COMPOSE.read_text(encoding="utf-8").lower(), (
        f"{COMPOSE.name} does not mention UTC, so the TZ line reads as noise"
    )


def test_no_shipped_example_token_is_a_detected_placeholder() -> None:
    """
    No live ``SANELESS_PAPERLESS__TOKEN=`` example is a detected placeholder.

    ``changeme`` used to be the shipped value in both the compose template and
    the Docker reference. This release detects it, shows the status strip red
    and refuses scans (APPL-07, D-14), so presenting it as a working example
    hands the reader a deployment that cannot scan. The expectation is derived
    from ``is_placeholder_token`` rather than a hard-coded word list, so
    widening that set cannot leave a stale example behind (T-30-86).
    """
    offenders: list[str] = []
    for path in (COMPOSE, DOCKER_REFERENCE, DEPLOY_HOWTO):
        for number, line in _numbered(path):
            if _is_comment(line):
                continue
            match = _TOKEN_ASSIGNMENT.search(line)
            if match is not None and is_placeholder_token(match.group(1)):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
                )
    assert not offenders, (
        "a shipped example sets the paperless token to a value this release "
        "detects as a placeholder and refuses scans for:\n" + "\n".join(offenders)
    )


def test_deploy_howto_tells_existing_operators_to_remove_their_token_line() -> None:
    """The compose how-to states the upgrade action and its consequence."""
    text, name = _read(DEPLOY_HOWTO)
    lowered = text.lower()
    assert "SANELESS_PAPERLESS__TOKEN" in text, (
        f"{name} no longer names the variable an existing operator has to remove"
    )
    for needle in ("override", "config.toml"):
        assert needle in lowered, (
            f"{name} does not explain that an environment line {needle}s the "
            "config file (D-17)"
        )
    assert "red" in lowered, (
        f"{name} does not state the consequence -- the status strip stays red "
        "-- for an operator who leaves their own token line in place"
    )


def test_docker_reference_documents_the_consume_mount_and_the_timezone() -> None:
    """The Docker reference documents ``/consume``, ``TZ`` and placeholder refusal."""
    text, name = _read(DOCKER_REFERENCE)
    assert "/consume" in text, f"{name} does not document the consume mount"
    assert "`TZ`" in text, (
        f"{name} does not document TZ, which is what makes every displayed "
        "timestamp local rather than UTC (APPL-12)"
    )
    assert "placeholder" in text.lower(), (
        f"{name} does not explain that placeholder token values are detected "
        "and refuse scans (APPL-07)"
    )


# ---------------------------------------------------------------------------
# Phase 30: the reference documents, derived from the source (T-30-89)
# ---------------------------------------------------------------------------

WEB_API_REFERENCE = DOCS_DIR / "reference" / "web-api.md"
FIRST_WEB_UI_SCAN = DOCS_DIR / "getting-started" / "first-web-ui-scan.md"
ROUTES_MODULE = REPO_ROOT / "src" / "saneless" / "web" / "routes.py"

_ROUTE_DECORATOR = re.compile(
    r"^@router\.(get|post|put|patch|delete)\(\s*\"([^\"]+)\"", re.MULTILINE
)

# The stale sentence the reserve-columns paragraph used to end on. Phase 23
# filled the page counters in and Phase 30 renders them; Phase 30 also writes
# owner_token. Reverting the correction fails here (T-30-87).
ARCHITECTURE_STALE_RESERVE_CLAIM = "nothing writes any of them yet"


def _declared_routes() -> list[tuple[str, str]]:
    """Return ``(METHOD, path)`` for every route declared in ``routes.py``."""
    source = ROUTES_MODULE.read_text(encoding="utf-8")
    routes = [
        (method.upper(), path) for method, path in _ROUTE_DECORATOR.findall(source)
    ]
    assert routes, f"no route decorators found in {ROUTES_MODULE.name}"
    return routes


def test_every_route_is_documented_in_the_web_api_reference() -> None:
    """
    Every route in ``routes.py`` has its own heading in ``web-api.md``.

    The expectation is derived from the decorators rather than a hard-coded
    list, so a route added in a later phase cannot ship undocumented: adding it
    fails this test until the reference gains its section (T-30-89).
    """
    text, name = _read(WEB_API_REFERENCE)
    missing = [
        f"{method} {path}"
        for method, path in _declared_routes()
        if f"`{method} {path}`" not in text
    ]
    assert not missing, (
        f"{name} has no `METHOD /path` heading for these routes:\n" + "\n".join(missing)
    )


def test_every_rejection_member_is_documented_with_its_message() -> None:
    """Every ``RequestRejection`` member, status and sentence is in the reference."""
    text, name = _read(WEB_API_REFERENCE)
    missing = [
        f"{member.name} ({rejection_status_code(member)}): {rejection_message(member)}"
        for member in RequestRejection
        if member.name not in text or rejection_message(member) not in text
    ]
    assert not missing, (
        f"{name} does not document these rejections, with the member name and "
        "the exact sentence saneless renders:\n" + "\n".join(missing)
    )


def test_every_web_config_field_is_documented() -> None:
    """
    Each ``[web]`` key is in the config reference with a ``SANELESS_WEB__`` row.

    Derived from ``WebConfig.model_fields``: a key added to the section later
    cannot ship without both references gaining it (APPL-10, T-30-89).
    """
    config_text, config_name = _read(CONFIG_REFERENCE)
    env_text, env_name = _read(ENV_REFERENCE)
    assert "[web]" in config_text, (
        f"{config_name} has no [web] section, so the form-shape keys are undocumented"
    )
    for field in WebConfig.model_fields:
        assert field in config_text, f"{config_name} does not document [web] {field}"
        variable = f"SANELESS_WEB__{field.upper()}"
        assert variable in env_text, f"{env_name} has no {variable} row"


def test_config_reference_says_where_the_bind_address_lives() -> None:
    """The ``[web]`` section admits ``web_host``/``web_port`` stayed in ``[output]``."""
    text, name = _read(CONFIG_REFERENCE)
    section = text.split("## `[web]`", 1)
    assert len(section) == 2, f"{name} has no `[web]` section heading"
    body = section[1].split("\n## ", 1)[0]
    for needle in ("web_host", "web_port", "[output]"):
        assert needle in body, (
            f"{name}'s [web] section does not mention {needle}, so a reader who "
            "looks there for the bind address finds nothing and no explanation"
        )


def test_architecture_no_longer_calls_the_job_columns_unwritten() -> None:
    """The reserve-columns paragraph stops claiming nothing writes them."""
    text, name = _read(ARCHITECTURE)
    assert ARCHITECTURE_STALE_RESERVE_CLAIM not in text, (
        f"{name} still says {ARCHITECTURE_STALE_RESERVE_CLAIM!r} about the job "
        "columns. pages_scanned, pages_removed and pages_uploaded are written "
        "by the worker's success path and displayed in the status area and the "
        "history Title cell; owner_token is written at submit and gates the "
        "flip prompt (T-30-87)"
    )
    for written in ("pages_scanned", "owner_token"):
        assert written in text, (
            f"{name} no longer names {written}; the correction should say what "
            "is true now rather than delete the paragraph"
        )


def test_first_web_ui_scan_walks_the_current_form() -> None:
    """The getting-started walkthrough describes the shipped page, not the old one."""
    text, name = _read(FIRST_WEB_UI_SCAN)
    for element, needle in (
        ("the status strip", "System status"),
        ("the Check again button", "Check again"),
        ("the tag checkbox list", "checkbox"),
        ("the tag filter", "filter"),
        ("the profile description line", "description"),
    ):
        assert needle in text, f"{name} does not walk {element} (looked for {needle!r})"
    assert "multi-select" not in text.lower(), (
        f"{name} still calls the tag picker a multi-select dropdown; it is a "
        "checkbox list (D-30, D-31)"
    )


# ---------------------------------------------------------------------------
# Phase 31: packaging identity (D-01, D-02, D-05, DLVR-08)
# ---------------------------------------------------------------------------

# The top-level ``version = "..."`` assignment. Anchored at the start of a line
# so ``target-version = "py314"`` under [tool.ruff] cannot match it.
VERSION_LINE = re.compile(r'^version = "([^"]*)"$', re.MULTILINE)

# The 0.2.0 series: the release version itself, or one of its release
# candidates.
ZERO_TWO_SERIES = re.compile(r"^0\.2\.0(-rc\.\d+)?$")

LEGACY_LICENSE_CLASSIFIER = "License :: OSI Approved :: MIT License"


def test_pyproject_declares_the_0_2_0_series() -> None:
    """
    The declared package version is the 0.2.0 series (D-01).

    The pattern admits ``0.2.0-rc.1`` deliberately. The TestPyPI release
    rehearsal sets exactly that string for the duration of the upload and
    restores ``0.2.0`` afterwards; a bare equality assertion would go red for
    the length of the rehearsal, and the pressure then would be to weaken it.
    Admitting the RC suffix up front is the narrower accommodation.
    """
    text, name = _read(PYPROJECT)
    match = VERSION_LINE.search(text)
    assert match is not None, f"{name} has no top-level version assignment"
    declared = match.group(1)
    assert ZERO_TWO_SERIES.match(declared), (
        f"{name} declares version {declared!r}; this phase ships the 0.2.0 "
        "series (0.2.0, or 0.2.0-rc.N during the release rehearsal)"
    )


def test_pyproject_uses_the_pep_639_license_keys() -> None:
    """The license is the PEP 639 SPDX string plus ``license-files`` (D-05)."""
    text, name = _read(PYPROJECT)
    assert 'license = "MIT"' in text, (
        f'{name} does not declare the PEP 639 SPDX expression license = "MIT"'
    )
    assert 'license-files = ["LICENSE"]' in text, (
        f'{name} does not declare license-files = ["LICENSE"], so the built '
        "wheel carries no dist-info/licenses/LICENSE (DLVR-08)"
    )
    assert "license = {text =" not in text, (
        f"{name} still uses the deprecated license table form, which PEP 639 "
        "replaced with the SPDX expression"
    )


def test_pyproject_has_no_legacy_license_classifier() -> None:
    """
    The deprecated ``License ::`` classifier is gone (D-05).

    This needs its own assertion because nothing else catches it. Neither
    ``uv_build`` 0.10.3 nor ``twine check`` errors when the classifier ships
    beside a PEP 639 ``License-Expression`` -- both were measured doing exactly
    that -- so no build or publish step would fail if it came back.
    """
    text, name = _read(PYPROJECT)
    assert LEGACY_LICENSE_CLASSIFIER not in text, (
        f"{name} still carries the {LEGACY_LICENSE_CLASSIFIER!r} classifier, "
        "which PEP 639 deprecates in favour of the license expression. No "
        "build step rejects it, so this test is the only thing that does"
    )


def test_pyproject_declares_alpha_maturity() -> None:
    """The maturity classifier is ``3 - Alpha``, not ``4 - Beta`` (D-02)."""
    text, name = _read(PYPROJECT)
    assert "Development Status :: 3 - Alpha" in text, (
        f"{name} does not classify saneless as Development Status :: 3 - Alpha"
    )
    assert "Development Status :: 4 - Beta" not in text, (
        f"{name} still claims Development Status :: 4 - Beta; the first "
        "published release is Alpha"
    )


def test_pyproject_distribution_name_is_saneless() -> None:
    """The PyPI distribution name stays ``saneless`` (DLVR-01)."""
    text, name = _read(PYPROJECT)
    assert 'name = "saneless"' in text, (
        f'{name} no longer declares name = "saneless". The owner rename '
        "changes the GitHub URLs only; the distribution name is published and "
        "must not drift to a squattable variant"
    )


# ---------------------------------------------------------------------------
# Phase 31: the identity guard (CI-02, DLVR-01, D-08..D-12)
# ---------------------------------------------------------------------------

# The forbidden owner slug is assembled at runtime from these two halves. The
# obvious spelling is a single literal, but the guard below scans *every*
# tracked file outside ``.planning/`` -- including this one -- so a literal here
# would make the guard report itself and go red with no real regression behind
# it. No file is exempt, which is exactly the point: there is nowhere a genuine
# stale reference could hide (D-11). The other obvious spelling, a ``"-".join``
# over an inline literal sequence, is no good either: ruff's FLY002 rewrites it
# straight back into the literal string, and suppressing the rule is forbidden.
# An f-string over named constants is the form FLY002 leaves alone. Fold this
# back into one string and the suite goes red on this very file.
_OWNER_GIVEN = "kris"
_OWNER_FAMILY = "knigga"
FORBIDDEN_OWNER_SLUG = f"{_OWNER_GIVEN}-{_OWNER_FAMILY}"

_EXCLUDED_PREFIX = ".planning/"


def _shipped_files() -> list[str]:
    """
    Return every tracked path outside ``.planning/`` (DLVR-01, D-10).

    "Shipped" is defined as ``git ls-files`` rather than a hand-maintained
    list, so a file added in a later phase is covered without anyone
    remembering to extend anything. ``site/`` is ignored by version control and
    therefore never reaches ``git ls-files``, so the built documentation site
    is excluded for free.

    Returns:
        Repo-relative path names, NUL-separated by the child process and split
        here.

    """
    # Every argv element is a literal and the repository path travels in the
    # ``cwd`` keyword -- the shape test_scanner.py and test_atomic_write.py
    # established for the other child-process tests in this suite. Passing
    # ``str(REPO_ROOT)`` as a ``-C`` argument instead trips ruff S603, and
    # suppression is forbidden.
    result = subprocess.run(
        ["/usr/bin/git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return [
        name
        for name in result.stdout.split("\0")
        if name and not name.startswith(_EXCLUDED_PREFIX)
    ]


def test_no_shipped_file_references_the_old_owner() -> None:
    """No tracked file outside ``.planning/`` names the old GitHub owner."""
    offenders: list[str] = []
    for name in _shipped_files():
        # Two clauses rather than one tuple: at this project's ruff
        # target-version, ruff format rewrites a parenthesised tuple into
        # PEP 758's bracketless form, which the pre-commit
        # debug-statements hook -- running on its own older interpreter --
        # cannot parse. Separate clauses read identically to both.
        try:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except OSError:
            continue
        offenders.extend(
            f"{name}:{number}: {line.strip()}"
            for number, line in enumerate(text.splitlines(), start=1)
            if FORBIDDEN_OWNER_SLUG in line
        )
    assert not offenders, (
        "a shipped file still references the old GitHub owner, which DLVR-01 "
        "renames. Every project URL must use the kdknigga forms -- "
        "github.com/kdknigga/saneless, kdknigga.github.io/saneless and "
        "ghcr.io/kdknigga/saneless:\n" + "\n".join(offenders)
    )


# The planning directory is not part of the product. Someone reading src/
# has no copy of it, and its identifiers mean nothing once its records move
# on, so a comment in shipped source states its reason in words instead of
# pointing there. This pattern matches the identifier shapes the planning
# records use: decision, finding and requirement IDs, threat IDs, phase and
# plan numbers, numbered research pitfalls and the planning file names. It
# leaves ordinary text alone: UTF-8, ISO-8601, SHA-384, A4, "N-1" and a bare
# PLAN (SQLite's EXPLAIN QUERY PLAN) do not match. The no-planning-citations
# hook in .pre-commit-config.yaml carries the same pattern, and the test after
# the guard keeps the two identical.
PLANNING_CITATION = re.compile(
    r"\b(R[0-9]+-)?(C|D|M|N|S|U|W|CR|IN|WR)-[0-9]{2,}\b|\b(A|"
    r"API|APPL|CFG|CTR|DARK|DLVR|DOCS|DPLX|EXC|HARD|OUTC|ROBU|"
    r"SCAN|SCNR|STOR|SWP|TEST)-[0-9]+\b|\bT-[0-9]+-[0-9]+\b|"
    r"\b[Pp]hase [0-9]+|\b[Pp]lan [0-9]+(\.[0-9]+)?-[0-9]+\b|"
    r"\bPitfall #?[0-9]+|UI-SPEC|\b(CONTEXT|RESEARCH)\b|\b(PLAN|"
    r"SUMMARY|VERIFICATION|REVIEW)\.md\b|Open Question|"
    r"[Pp]er user decision|\.planning/"
)
_SOURCE_PREFIX = "src/"
_SOURCE_SUFFIXES = frozenset({".py", ".html", ".css", ".js"})
# The vendored htmx and Pico files are upstream bytes pinned by an integrity
# hash, so they are neither ours to comment nor ours to edit.
_VENDOR_PREFIX = "src/saneless/web/static/vendor/"
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"


def _shipped_source_files() -> list[str]:
    """
    Return the tracked source, template, style and script files under src/.

    Returns:
        Repo-relative path names, vendored assets excluded.

    """
    return [
        name
        for name in _shipped_files()
        if name.startswith(_SOURCE_PREFIX)
        and Path(name).suffix in _SOURCE_SUFFIXES
        and not name.startswith(_VENDOR_PREFIX)
    ]


def _citation_offenders(names: list[str], root: Path) -> list[str]:
    """
    Return every cited planning artefact, and every file that could not be read.

    A file the guard cannot read is an offender too, not a pass: a source
    file that is not UTF-8, or that vanished between listing and reading,
    has not been checked, so it must not count as clean.

    Args:
        names: Repo-relative file names to check.
        root: The directory the names are relative to.

    Returns:
        One ``name:line: text`` entry per citation, and one ``name: reason``
        entry per file that could not be read.

    """
    offenders: list[str] = []
    for name in names:
        # Two clauses rather than one tuple, for the reason given in the
        # owner guard above.
        try:
            text = (root / name).read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            offenders.append(f"{name}: not UTF-8, so it was not checked: {exc}")
            continue
        except OSError as exc:
            offenders.append(f"{name}: unreadable, so it was not checked: {exc}")
            continue
        offenders.extend(
            f"{name}:{number}: {line.strip()}"
            for number, line in enumerate(text.splitlines(), start=1)
            if PLANNING_CITATION.search(line)
        )
    return offenders


def test_no_src_file_cites_a_planning_artefact() -> None:
    """
    No shipped source file points at the planning records for its reasons.

    A comment that says only "see decision so-and-so" tells a reader of the
    product nothing, because the planning directory does not ship with it.
    Each comment in src/ has to carry its own reason in plain words.

    This test and the no-planning-citations hook share one pattern, but not
    quite one file set: the test picks files by suffix (``.py``, ``.html``,
    ``.css``, ``.js``), while the hook picks them by the type ``identify``
    detects, which also takes in, for example, an extensionless script with
    a Python shebang or a ``.mjs`` file. src/ holds neither today, so the two
    check the same files; the drift is accepted rather than making the test
    depend on ``identify``.
    """
    offenders = _citation_offenders(_shipped_source_files(), REPO_ROOT)
    assert not offenders, (
        "a file under src/ cites a planning artefact or could not be read. "
        "Replace a reference with the reason it stood for, in words, or "
        "delete it where the sentence is complete without it:\n" + "\n".join(offenders)
    )


def test_the_citation_guard_reports_a_file_it_cannot_read(tmp_path: Path) -> None:
    """A non-UTF-8 or missing file is reported, never skipped as clean."""
    (tmp_path / "latin1.py").write_bytes(b"# caf\xe9\n")
    (tmp_path / "clean.py").write_text("# nothing to see\n", encoding="utf-8")

    offenders = _citation_offenders(["latin1.py", "clean.py", "gone.py"], tmp_path)

    assert [entry.split(":", 1)[0] for entry in offenders] == [
        "latin1.py",
        "gone.py",
    ]
    assert "not UTF-8" in offenders[0]
    assert "unreadable" in offenders[1]


def test_the_citation_hook_uses_the_guard_pattern() -> None:
    """The commit hook and the guard above match exactly the same text."""
    text, name = _read(PRE_COMMIT_CONFIG)
    entry = f"entry: '{PLANNING_CITATION.pattern}'"
    assert entry in text, (
        f"{name}'s no-planning-citations hook no longer carries the pattern "
        "the guard test uses, so the two can disagree about what a citation "
        "is. Copy PLANNING_CITATION.pattern into the hook's entry verbatim"
    )


# Only the documentation deep links: the site root has no path after
# ``saneless/`` and is not a page. The trailing ``/`` is greedy so a nested
# path such as ``reference/cli-commands`` is captured whole.
README_DOCS_LINK = re.compile(r"kdknigga\.github\.io/saneless/(?P<path>[^)\s]+)/")

SCAN_EXAMPLE = "saneless scan"
README_SOURCE_ASSIGNMENT = re.compile(r'source = "(?P<value>[^"]*)"')


def test_readme_scan_example_carries_a_title() -> None:
    """Every ``saneless scan`` line in the README shows ``--title`` (DOCS-02)."""
    offenders = [
        f"{number}: {line.strip()}"
        for number, line in _numbered(README)
        if SCAN_EXAMPLE in line and "--title" not in line
    ]
    assert not offenders, (
        "a README scan example omits --title. The flag is optional at runtime "
        "-- saneless resolves a default -- but DOCS-02 asks the front-page "
        "example to show the reader how a document gets its name:\n"
        + "\n".join(offenders)
    )


def test_readme_source_values_are_real_sane_spellings() -> None:
    """
    Every README ``source`` value is the spelling the profile model ships.

    saneless compares the configured source against the names the backend
    reports, and that comparison is case-sensitive: ``"flatbed"`` does not
    match the ``"Flatbed"`` every SANE backend returns, which is exactly why
    the lowercase spelling in the README was a real bug and not a typo. The
    expectation is read off ``ProfileConfig`` rather than written out here, so
    changing the default cannot leave the front page quietly wrong.
    """
    expected = ProfileConfig.model_fields["source"].default
    offenders = [
        f"{number}: {line.strip()}"
        for number, line in _numbered(README)
        for match in README_SOURCE_ASSIGNMENT.finditer(line)
        if match.group("value") != expected
    ]
    assert not offenders, (
        f"a README profile example sets source to something other than "
        f"{expected!r}, the spelling ProfileConfig defaults to and SANE "
        "reports. The comparison is case-sensitive, so a near-miss selects no "
        "source at all:\n" + "\n".join(offenders)
    )


def test_every_readme_docs_link_resolves_to_a_page() -> None:
    """Every README documentation deep link names a page that exists (DOCS-02)."""
    offenders = [
        f"{number}: {match.group(0)}"
        for number, line in _numbered(README)
        for match in README_DOCS_LINK.finditer(line)
        if not (DOCS_DIR / f"{match.group('path')}.md").is_file()
    ]
    assert not offenders, (
        "a README documentation link points at a path with no page behind it, "
        "so the reader lands on a 404. The expectation is the docs/ tree "
        "itself rather than a hard-coded list, so a page renamed in a later "
        "phase cannot leave a dead link on the front page:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# Phase 31: the container build context (D-29, D-30, D-33, DLVR-06, DLVR-09)
# ---------------------------------------------------------------------------

DOCKERIGNORE = REPO_ROOT / ".dockerignore"
GITIGNORE = REPO_ROOT / ".gitignore"

# Path fragments that must never appear on a ``!`` re-include line. ``config``
# and ``saneless.toml`` are where a live paperless-ngx token lives; ``tests``
# and ``.planning`` are bulk the image has no use for, and ``.planning`` in
# particular carries the phase audit artifacts.
FORBIDDEN_CONTEXT_PATHS = (
    ".env",
    ".git",
    ".planning",
    "config",
    "config.toml",
    "saneless.toml",
    "tests",
)

# Every entry the wheel build reads. ``uv.lock`` is deliberately absent: the
# build succeeds without it, so requiring it here would assert a convenience
# rather than a contract.
REQUIRED_CONTEXT_PATHS = ("!src/", "!pyproject.toml", "!README.md", "!LICENSE")


def _significant_lines(path: Path) -> list[tuple[int, str]]:
    """
    Return ``(line number, stripped line)`` for non-blank, non-comment lines.

    Filtering comments out rather than matching raw text is what stops these
    tests self-invalidating: a comment that quotes ``*.png`` to explain why the
    glob was removed would otherwise read as the glob itself.

    Args:
        path: The file to read.

    Returns:
        One pair per meaningful line, 1-based, in file order.

    """
    return [
        (number, line.strip())
        for number, line in _numbered(path)
        if line.strip() and not _is_comment(line)
    ]


def test_dockerignore_starts_with_a_deny_everything_line() -> None:
    """
    ``.dockerignore``'s first meaningful line is exactly ``*`` (D-29, D-30).

    The file is an allow-list: ``*`` excludes the whole working tree and each
    ``!`` line below re-includes one path the wheel build needs. That only
    holds while ``*`` comes first. Move it down, or drop it, and every ``!``
    line below it becomes decoration while the working tree -- including a real
    ``config.toml`` with a live paperless-ngx token -- rides into a build layer
    that is recoverable from the image.
    """
    lines = _significant_lines(DOCKERIGNORE)
    name = DOCKERIGNORE.relative_to(REPO_ROOT)
    assert lines, f"{name} has no meaningful lines at all"
    number, first = lines[0]
    assert first == "*", (
        f"{name}:{number} is {first!r}, not '*'. The allow-list's first "
        "meaningful line must exclude everything, or nothing below it is "
        "re-including anything -- the whole working tree reaches the daemon"
    )


def test_dockerignore_never_reincludes_a_secret_or_test_path() -> None:
    """
    No ``!`` line re-includes a config, a secret, ``.planning/`` or tests (D-30).

    Nothing else in CI would notice the allow-list being weakened: there is no
    docker-build-and-inspect step, and a leak is only visible by unpacking a
    layer. This test is the guard.
    """
    name = DOCKERIGNORE.relative_to(REPO_ROOT)
    offenders = [
        f"{name}:{number}: {line}"
        for number, line in _significant_lines(DOCKERIGNORE)
        if line.startswith("!")
        and any(needle in line for needle in FORBIDDEN_CONTEXT_PATHS)
    ]
    assert not offenders, (
        "a .dockerignore re-include line names a path the build context must "
        "never carry. The image needs the package sources and the two files "
        "the wheel metadata reads -- nothing else:\n" + "\n".join(offenders)
    )


def test_dockerignore_reincludes_everything_the_wheel_build_needs() -> None:
    """
    The allow-list re-includes every input ``uv build --wheel`` actually reads.

    Two of these are live traps rather than conveniences. ``pyproject.toml``
    declares ``readme = "README.md"`` and the PEP 639
    ``license-files = ["LICENSE"]``; excluding either file makes the wheel
    build **fail**, not merely produce a thinner wheel. An over-tightened
    allow-list therefore breaks the image build rather than degrading it
    quietly, which is the better failure -- but only if it is caught here
    first.
    """
    lines = {line for _, line in _significant_lines(DOCKERIGNORE)}
    name = DOCKERIGNORE.relative_to(REPO_ROOT)
    missing = [entry for entry in REQUIRED_CONTEXT_PATHS if entry not in lines]
    assert not missing, (
        f"{name} does not re-include {missing}. README.md and LICENSE are not "
        "optional: pyproject.toml's readme and license-files keys each make "
        "`uv build --wheel` fail when the named file is absent from the "
        "context"
    )


def test_gitignore_has_no_blanket_png_glob() -> None:
    """
    ``.gitignore`` no longer blanket-ignores every PNG in the tree (DLVR-09).

    A repository-wide ``*.png`` silently swallows an asset someone means to
    commit -- a documentation screenshot, a favicon -- and gives no signal at
    all when it does. Paths are ignored by path here instead.
    """
    name = GITIGNORE.relative_to(REPO_ROOT)
    offenders = [
        f"{name}:{number}: {line}"
        for number, line in _significant_lines(GITIGNORE)
        if line == "*.png"
    ]
    assert not offenders, (
        "the blanket PNG glob is back in .gitignore. Ignore the directory that "
        "produces the files instead, so committing an intentional image does "
        "not silently do nothing:\n" + "\n".join(offenders)
    )


def test_gitignore_ignores_the_playwright_artifact_directory() -> None:
    """
    ``/test-results/`` is ignored -- the one path the blanket glob covered.

    ``git check-ignore -v`` was run against every candidate PNG path in the
    repository. ``site/``, ``.planning/ui-reviews/`` and ``.playwright-mcp/``
    each have their own entry and survive the glob's removal; pytest-playwright's
    default artifact directory had nothing but ``*.png`` behind it. It holds
    failure videos and trace archives as well as screenshots, so the directory
    is the correct replacement rather than a narrower glob.
    """
    lines = {line for _, line in _significant_lines(GITIGNORE)}
    name = GITIGNORE.relative_to(REPO_ROOT)
    assert "/test-results/" in lines, (
        f"{name} does not ignore /test-results/. Removing the blanket *.png "
        "glob leaves pytest-playwright's failure screenshots, videos and trace "
        "archives untracked-but-visible in every `git status` after a failed "
        "browser test"
    )


# ---------------------------------------------------------------------------
# Phase 31: the container image itself (D-25, D-27, D-28, D-29, DLVR-07)
# ---------------------------------------------------------------------------

DOCKERFILE = REPO_ROOT / "Dockerfile"

DATA_DIR = "/var/lib/saneless"

# The reference on a ``FROM`` line: everything up to the first whitespace.
FROM_LINE = re.compile(r"^FROM\s+(?P<ref>\S+)", re.IGNORECASE)
FROM_STAGE = re.compile(r"^FROM\s+(?P<ref>\S+)\s+AS\s+(?P<stage>\S+)", re.IGNORECASE)
USER_LINE = re.compile(r"^USER\s+(?P<user>\S+)", re.IGNORECASE)
DIGEST_SUFFIX = re.compile(r"@sha256:[0-9a-f]{64}$")

UV_IMAGE = "ghcr.io/astral-sh/uv"

# Spellings of the superuser a ``USER`` instruction can carry.
ROOT_USERS = frozenset({"root", "0", "0:0", "root:root"})

WHEEL_BUILD_INPUTS = ("pyproject.toml", "uv.lock", "README.md", "LICENSE")


def test_dockerfile_pins_every_base_image_by_digest() -> None:
    """
    Every ``FROM`` reference carries both a tag and a ``sha256`` digest (D-27).

    The digest is what makes the build reproducible: a tag can be repointed at
    new content between two builds of the same source. The **tag** has to stay
    in the reference alongside it, not move into the trailing comment, because
    that is how Dependabot knows which stream the pin belongs to and what to
    bump it to. A bare digest is immutable and unmaintained -- it just rots.
    """
    name = DOCKERFILE.relative_to(REPO_ROOT)
    references = [
        (number, line, match.group("ref"))
        for number, line in _significant_lines(DOCKERFILE)
        for match in [FROM_LINE.match(line)]
        if match is not None
    ]
    assert references, f"{name} has no FROM instructions at all"
    offenders = [
        f"{name}:{number}: {line}"
        for number, line, reference in references
        if not DIGEST_SUFFIX.search(reference)
        or ":" not in reference.split("@")[0].rsplit("/", 1)[-1]
    ]
    assert not offenders, (
        "a FROM reference is not pinned as 'image:tag@sha256:<64 hex>'. Both "
        "halves are load-bearing: the digest pins the content, the tag tells "
        "Dependabot what stream to bump:\n" + "\n".join(offenders)
    )


def test_dockerfile_gets_uv_from_a_named_from_stage() -> None:
    """
    The uv tool image arrives through a named ``FROM`` stage, not a copy-from.

    Dependabot's Docker file parser iterates the file matching ``FROM``
    directives **only**, and deliberately excludes builder-stage references
    written as a ``COPY`` with a ``--from`` pointing at a registry image. A
    digest written straight onto such a line is invisible to it and would
    never be updated -- the pin would look maintained and quietly rot.
    Promoting uv to ``FROM ... AS uv`` costs one line and makes the third
    digest pin real.
    """
    name = DOCKERFILE.relative_to(REPO_ROOT)
    lines = _significant_lines(DOCKERFILE)
    uv_stages = [
        number
        for number, line in lines
        for match in [FROM_STAGE.match(line)]
        if match is not None and match.group("ref").startswith(UV_IMAGE)
    ]
    assert uv_stages, (
        f"{name} has no `FROM {UV_IMAGE}:<version>@sha256:... AS uv` stage, so "
        "the uv digest pin is one Dependabot cannot see or maintain"
    )
    offenders = [
        f"{name}:{number}: {line}"
        for number, line in lines
        if line.upper().startswith("COPY") and "--from=ghcr.io/" in line
    ]
    assert not offenders, (
        "a COPY names a registry image in its --from. Dependabot skips those "
        "lines, so a digest written there never gets bumped:\n" + "\n".join(offenders)
    )


def test_dockerfile_copies_only_the_wheel_build_inputs() -> None:
    """
    The builder stage copies named paths, never the whole working tree (D-29).

    This is the second of the two independent build-context gates, the first
    being the ``.dockerignore`` allow-list. Copying the entire context sweeps
    whatever the daemon was sent into a layer -- which, before this phase,
    included a real ``config.toml`` holding a live paperless-ngx token. Naming
    the inputs means a mistake in the allow-list alone cannot leak anything.
    """
    name = DOCKERFILE.relative_to(REPO_ROOT)
    lines = _significant_lines(DOCKERFILE)
    offenders = [
        f"{name}:{number}: {line}"
        for number, line in lines
        if line.upper().startswith("COPY") and line.split()[1:] == [".", "."]
    ]
    assert not offenders, (
        "the builder stage still copies the entire build context:\n"
        + "\n".join(offenders)
    )
    copied = " ".join(line for _, line in lines if line.upper().startswith("COPY"))
    missing = [entry for entry in WHEEL_BUILD_INPUTS if entry not in copied]
    assert not missing, f"{name} has no COPY line naming {missing}"
    assert "src" in copied.split(), (
        f"{name} has no COPY line naming the `src` package directory"
    )


def test_dockerfile_runs_as_a_non_root_user() -> None:
    """
    The image ends on a ``USER`` that is not root (D-25, DLVR-07).

    A root process in the container reaches much further into a bind-mounted
    host directory, and into the kernel, than UID 1000 does. The account is
    created at a **fixed** 1000 on purpose: on the single-user Linux host this
    appliance targets, the operator's own ``./config`` directory is already
    ``1000:1000``, so the read-write config mount works with no ``chown`` at
    all. A high UID such as 10001 can never match, which would put a fix-up on
    every deployment's happy path.

    This test reads the file. That the built image actually starts as UID 1000
    is proven separately, by running it.
    """
    name = DOCKERFILE.relative_to(REPO_ROOT)
    users = [
        (number, match.group("user"))
        for number, line in _significant_lines(DOCKERFILE)
        for match in [USER_LINE.match(line)]
        if match is not None
    ]
    assert users, f"{name} has no USER instruction, so the image runs as root"
    offenders = [
        f"{name}:{number}: USER {user}" for number, user in users if user in ROOT_USERS
    ]
    assert not offenders, "a USER instruction names the superuser:\n" + "\n".join(
        offenders
    )
    text, _ = _read(DOCKERFILE)
    for needle in ("groupadd", "useradd", "1000"):
        assert needle in text, (
            f"{name} does not create the account with {needle!r}; the USER "
            "instruction names an account that has to exist in the image"
        )


def test_dockerfile_chowns_the_data_dir_before_declaring_the_volume() -> None:
    """
    The data directory is created and chowned **before** ``VOLUME`` (D-28).

    Whether a build step that changes data inside a declared volume path
    *after* the ``VOLUME`` instruction survives depends on **which builder
    ran**: Docker's own reference says the legacy builder discards those
    changes and BuildKit keeps them. Ordering the ``mkdir`` and ``chown``
    before ``VOLUME`` is the one arrangement that is correct under both. Get
    it wrong and, on the builder that discards, a fresh named volume comes up
    owned by root, the non-root process cannot write the job database, and
    preserved scans have nowhere to go.

    Asserting the order statically rather than test-driving it is deliberate,
    for the same reason: buildah -- which is what ``docker`` is on the
    maintainer's host -- was measured keeping the change even with the wrong
    order, so a green local build is no evidence at all. This test does not
    depend on which builder ran.
    """
    name = DOCKERFILE.relative_to(REPO_ROOT)
    lines = _significant_lines(DOCKERFILE)
    chowns = [number for number, line in lines if "chown" in line and DATA_DIR in line]
    volumes = [number for number, line in lines if line.upper().startswith("VOLUME")]
    assert chowns, f"{name} never chowns {DATA_DIR}"
    assert volumes, f"{name} has no VOLUME instruction"
    assert max(chowns) < min(volumes), (
        f"{name} chowns {DATA_DIR} at line {max(chowns)}, after the VOLUME "
        f"instruction at line {min(volumes)}. Docker discards that change, so "
        "a fresh volume would come up owned by root and the non-root process "
        "could not write to it"
    )


def test_dockerfile_sets_a_workdir_in_the_runtime_stage() -> None:
    """
    The runtime stage anchors relative writes inside the durable volume (D-28).

    With no ``WORKDIR`` the working directory is ``/``, so a relative write --
    ``saneless auto-profiles`` with no config file loaded writes
    ``./saneless.toml`` -- lands in the container's own writable layer, first
    in the config search order and destroyed on the next recreation. Pointing
    ``WORKDIR`` at the declared volume puts it somewhere durable and owned by
    the app user instead.
    """
    name = DOCKERFILE.relative_to(REPO_ROOT)
    lines = _significant_lines(DOCKERFILE)
    froms = [number for number, line in lines if FROM_LINE.match(line) is not None]
    workdirs = [number for number, line in lines if line == f"WORKDIR {DATA_DIR}"]
    assert froms, f"{name} has no FROM instructions at all"
    assert workdirs, f"{name} does not set `WORKDIR {DATA_DIR}`"
    assert max(workdirs) > max(froms), (
        f"{name} sets `WORKDIR {DATA_DIR}` at line {max(workdirs)}, before the "
        f"runtime stage begins at line {max(froms)}, so the runtime stage "
        "still starts in /"
    )


# ---------------------------------------------------------------------------
# Phase 31: one port everywhere, and the non-1000 operator (D-26, D-31, D-32)
# ---------------------------------------------------------------------------

# A `-p` publish flag, with or without a bind address in front of it, and a
# quoted compose `ports:` entry. Both are matched narrowly rather than by a
# bare `\d+:\d+`, which would also read the minutes out of a log timestamp.
PUBLISH_FLAG = re.compile(
    r"-p\s+(?:\d+\.\d+\.\d+\.\d+:)?(?P<host>\d{2,5}):(?P<container>\d{2,5})"
)
QUOTED_MAPPING = re.compile(r'"(?P<host>\d{2,5}):(?P<container>\d{2,5})"')
# A `ports:` key and a YAML sequence item, each also matching the commented
# form. A quoted `number:number` only counts as a port mapping when it is an
# entry under `ports:` -- `user: "1000:1000"` has the same shape and is a
# UID and GID.
PORTS_KEY = re.compile(r"^\s*#?\s*ports:\s*$")
SEQUENCE_ITEM = re.compile(r"^\s*#?\s*-\s")
EXPOSE_PORT = re.compile(r"\bEXPOSE\s+(?P<port>\d+)")
HEALTH_URL_PORT = re.compile(r"localhost:(?P<port>\d+)/health")

# A `web_port` assignment that is live rather than commented out.
LIVE_WEB_PORT = re.compile(r"^\s*web_port\s*=")

UID_CHOWN_COMMAND = "chown -R 1000:1000 ./config"


# The service key that opens a block in a compose file, at the one indent
# level compose and every fenced example in the docs use for it.
COMPOSE_SERVICE_KEY = re.compile(r"^  (?P<name>[A-Za-z0-9_.-]+):\s*$")
IMAGE_KEY = re.compile(r"^\s*image:\s*(?P<reference>\S+)")

# Substring identifying this project's own image. `paperless-ngx` does not
# contain it, which is the distinction that matters in the side-by-side
# compose example.
OWN_IMAGE_MARKER = "saneless"


def _port_bearing_files() -> list[Path]:
    """Return every file that could name a container-side port."""
    return [DOCKERFILE, COMPOSE, README, *_doc_pages()]


def _shell_command_at(lines: list[str], index: int) -> str:
    """
    Return the whole shell command beginning at ``index``, continuations joined.

    A ``docker run`` in the documentation is usually wrapped over several
    lines with trailing backslashes, and the image it runs is on the last one.
    Reading only the line that carries the ``-p`` flag would leave the command
    unattributable.

    Args:
        lines: Every line of the file, in order.
        index: 0-based index of the line carrying the flag.

    Returns:
        The command text, continuation lines appended.

    """
    parts = [lines[index]]
    cursor = index
    while lines[cursor].rstrip().endswith("\\") and cursor + 1 < len(lines):
        cursor += 1
        parts.append(lines[cursor])
    return " ".join(parts)


def _is_ports_entry(lines: list[str], index: int) -> bool:
    """
    Say whether the line at ``index`` is an entry under a compose ``ports:`` key.

    Args:
        lines: Every line of the file, in order.
        index: 0-based index of the candidate line.

    Returns:
        True when the line is a sequence item whose nearest enclosing key is
        ``ports:``.

    """
    if not SEQUENCE_ITEM.match(lines[index]):
        return False
    for cursor in range(index - 1, -1, -1):
        line = lines[cursor]
        if not line.strip() or SEQUENCE_ITEM.match(line):
            continue
        return PORTS_KEY.match(line) is not None
    return False


def _compose_service_image(lines: list[str], index: int) -> str:
    """
    Return the image of the compose service whose block contains ``index``.

    The docs put saneless and paperless-ngx side by side in one compose
    example, so a ports entry says nothing on its own about which service it
    belongs to.

    Args:
        lines: Every line of the file, in order.
        index: 0-based index of the line inside the service block.

    Returns:
        The image reference, or the empty string when there is no enclosing
        service block or it declares no image.

    """
    start = None
    for cursor in range(index, -1, -1):
        if COMPOSE_SERVICE_KEY.match(lines[cursor]):
            start = cursor
            break
    if start is None:
        return ""
    for line in lines[start:]:
        if COMPOSE_SERVICE_KEY.match(line) and line is not lines[start]:
            break
        match = IMAGE_KEY.match(line)
        if match is not None:
            return match.group("reference")
    return ""


def test_the_example_config_does_not_ship_a_live_web_port() -> None:
    """
    ``saneless.toml.example`` does not set ``web_port`` live (D-32).

    The example shipped ``web_port = 8081``, which is wrong for every Docker
    reader: the image's ``EXPOSE`` and healthcheck are both 8080, so copying
    the example into a mounted ``config.toml`` moved the server off the port
    the healthcheck probes and the container went unhealthy with nothing on
    screen to say why. The line joins the commented pair below it instead, so
    it still documents the key without configuring anything.
    """
    name = TOML_EXAMPLE.relative_to(REPO_ROOT)
    offenders = [
        f"{name}:{number}: {line.strip()}"
        for number, line in _numbered(TOML_EXAMPLE)
        if LIVE_WEB_PORT.match(line)
    ]
    assert not offenders, (
        "the example config sets web_port live. In Docker the container port "
        "is fixed and you remap on the host, so a live value here can only "
        "move the server away from the port the image's healthcheck "
        "probes:\n" + "\n".join(offenders)
    )


def test_every_documented_container_port_matches_the_model_default() -> None:
    """
    Image, compose file and every doc page agree on one container port (D-31).

    The expectation is read off ``OutputConfig`` rather than written out here,
    so changing the default cannot leave the image and the documentation
    disagreeing without something going red. Only the *container* side of a
    ``-p`` mapping is checked: remapping on the host is exactly what operators
    are told to do when 8080 is taken.

    A mapping counts only when the image it belongs to is this project's. The
    compose how-to stands saneless next to paperless-ngx, whose own
    ``"8000:8000"`` is correct and none of this contract's business, so each
    mapping is attributed first -- to the enclosing compose service's image,
    or to the shell command the flag appears in, continuations included.
    """
    expected = str(OutputConfig.model_fields["web_port"].default)
    text, dockerfile_name = _read(DOCKERFILE)
    assert f"EXPOSE {expected}" in text, (
        f"{dockerfile_name} does not EXPOSE {expected}, the port "
        "OutputConfig.web_port defaults to"
    )
    assert f"localhost:{expected}/health" in text, (
        f"{dockerfile_name}'s healthcheck does not probe port {expected}"
    )
    offenders = []
    for path in _port_bearing_files():
        name = path.relative_to(REPO_ROOT)
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            found = [
                match.group("container")
                for match in PUBLISH_FLAG.finditer(line)
                if OWN_IMAGE_MARKER in _shell_command_at(lines, index)
            ]
            found.extend(
                match.group("container")
                for match in QUOTED_MAPPING.finditer(line)
                if _is_ports_entry(lines, index)
                and OWN_IMAGE_MARKER in _compose_service_image(lines, index)
            )
            found.extend(
                match.group("port")
                for pattern in (EXPOSE_PORT, HEALTH_URL_PORT)
                for match in pattern.finditer(line)
            )
            offenders.extend(
                f"{name}:{index + 1}: {port} in {line.strip()}"
                for port in found
                if port != expected
            )
    assert not offenders, (
        f"a container-side port other than {expected} is documented. The "
        "container's port is fixed; web_port is a bare-metal setting and the "
        "host side of the mapping is the half operators change:\n"
        + "\n".join(offenders)
    )


def test_compose_carries_a_commented_user_override() -> None:
    """
    The compose example ships the UID override commented out (D-26).

    The image already runs as 1000:1000, which is what the operator's own
    ``./config`` directory is on a single-user Linux host, so the override is
    for the minority case and must not be live. It is a literal rather than
    ``user: "${UID}:${GID}"`` on purpose: compose does not populate ``UID``
    unless the shell exports it, so that form silently falls back to an empty
    string and the service fails to start for a reason that looks like
    nothing.
    """
    name = COMPOSE.relative_to(REPO_ROOT)
    lines = _numbered(COMPOSE)
    live = [
        f"{name}:{number}: {line.strip()}"
        for number, line in lines
        if line.strip().startswith("user:")
    ]
    assert not live, (
        "docker-compose.yml sets `user:` live. The image already runs as "
        "1000:1000; a live override here is one more thing to get wrong on "
        "the happy path:\n" + "\n".join(live)
    )
    commented = [
        number
        for number, line in lines
        if _is_comment(line) and "user:" in line and "1000" in line
    ]
    assert commented, (
        f"{name} has no commented `user:` line naming 1000. An operator whose "
        "own UID is not 1000 needs the override where they are already "
        "reading, not only in the documentation"
    )


def test_the_uid_is_documented_where_operators_will_look() -> None:
    """
    Both Docker pages state the UID and give the ``chown`` command (D-26).

    A bind mount does not inherit the image directory's ownership the way a
    named volume does -- that was measured -- so an operator whose UID is not
    1000 has to fix the host directory themselves. This is handled with
    documentation rather than a runtime fix-up because any fix-up would have
    to start as root, which is the thing running as UID 1000 exists to stop.
    """
    for page in (DOCKER_REFERENCE, DEPLOY_HOWTO):
        text, name = _read(page)
        assert "1000" in text, f"{name} does not state the UID the image runs as"
        assert UID_CHOWN_COMMAND in text, (
            f"{name} does not give the `{UID_CHOWN_COMMAND}` command for an "
            "operator whose own UID is not 1000"
        )


# ---------------------------------------------------------------------------
# Phase 31: the deployment shapes and the one USB rule (D-47, D-48, DOCS-04)
# ---------------------------------------------------------------------------

# The host device tree a container would have to be handed in order to reach a
# scanner over the USB bus itself.
USB_BUS_PATH = "/dev/bus/usb"

SCANNER_HOST_ROW_MARKER = "| `host` |"
CONTAINER_CAVEAT_WORDS = ("container", "saned")


def test_no_doc_page_documents_usb_passthrough_into_a_container() -> None:
    """
    No page or deployment file hands the host USB bus to a container (D-48).

    PROJECT.md constrains this project to need no ``--privileged`` flag,
    because USB device access is handled by the ``saned`` server rather than by
    the saneless container. A container reaches a scanner over the SANE network
    protocol, always -- including a ``saned`` running on its own host.

    Documenting a device mapping as an "advanced" option would add a fourth
    deployment shape that contradicts that constraint, has never been tested
    here, and hands the container raw device access it has no use for. It was
    written once and deleted; this test is what stops it coming back.
    """
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for path in _deployment_files()
        for number, line in _numbered(path)
        if USB_BUS_PATH in line
    ]
    assert not offenders, (
        "a page or deployment file maps the host USB bus into the container. "
        "Only a bare-metal install enumerates a locally attached scanner; a "
        "container reaches one through `saned` over the network:\n"
        + "\n".join(offenders)
    )


def test_scanner_host_documentation_carries_the_container_caveat() -> None:
    """
    The ``scanner.host`` row states what an empty value means in a container.

    "Empty = local USB" is true, and only for a bare-metal install. Read inside
    a container it is the opposite of true: leaving the setting empty there
    gives the backend nothing to probe, and the operator gets an empty device
    list with nothing on screen to say why. The row therefore has to carry the
    container caveat next to the default, not three pages away.

    The assertion is on the presence of the caveat words rather than on a whole
    sentence, so a later rewording of the row cannot fail this for a reason
    that has nothing to do with the contract.
    """
    name = CONFIG_REFERENCE.relative_to(REPO_ROOT)
    rows = [
        line
        for _, line in _numbered(CONFIG_REFERENCE)
        if SCANNER_HOST_ROW_MARKER in line
    ]
    assert rows, f"{name} has no `{SCANNER_HOST_ROW_MARKER}` table row at all"
    row = " ".join(rows).lower()
    assert "empty" in row, (
        f"{name}'s scanner host row no longer says what an empty value means"
    )
    missing = [word for word in CONTAINER_CAVEAT_WORDS if word not in row]
    assert not missing, (
        f"{name}'s scanner host row does not mention {missing}. An operator "
        "reading the default inside a container needs to be told there that "
        "the container cannot see a local scanner and reaches one through "
        f"`saned`:\n{' '.join(rows).strip()}"
    )


# ---------------------------------------------------------------------------
# Phase 31: the setup chooser link and the no-auth note (D-47, D-50, DOCS-04,
# DOCS-05)
# ---------------------------------------------------------------------------

PREREQUISITES_HEADING = "## Prerequisites"
SETUP_CHOOSER_LINK_TARGET = "which-setup.md"

MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((?P<target>[^)\s]+)\)")

# Either of the two places the trust posture is already written down. D-50
# rejected a page of its own: a sentence that links to one of these is what
# criterion 5 asks for, and a third partial copy is what it does not.
TRUST_LINK_TARGETS = (
    DOCS_DIR / "how-to" / "deploy-docker-compose.md",
    DOCS_DIR / "reference" / "web-api.md",
)

NO_AUTH_PHRASES = ("no login", "no authentication")
ALL_INTERFACES_PHRASES = ("all network interfaces", "0.0.0.0")

TRUST_NOTE_SURFACES = (QUICK_START, DOCKER_REFERENCE)


def _section_lines(path: Path, heading: str) -> list[str]:
    """
    Return the lines under one ``##`` heading, up to the next one.

    Args:
        path: The page to read.
        heading: The heading line to start at, matched after stripping.

    Returns:
        The section's lines, heading excluded. Empty when the heading is absent.

    """
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = next(
            index for index, line in enumerate(lines) if line.strip() == heading
        )
    except StopIteration:
        return []
    rest = lines[start + 1 :]
    end = next(
        (index for index, line in enumerate(rest) if line.startswith(("## ", "# "))),
        len(rest),
    )
    return rest[:end]


def _resolved_link_targets(path: Path, lines: list[str]) -> list[tuple[str, Path]]:
    """
    Return each relative Markdown link in ``lines`` with the file it names.

    Anchors are stripped, and the target is resolved against the linking page's
    own directory, which is what MkDocs does. Absolute URLs are skipped: this
    exists to catch a dangling in-tree link, and nothing here can tell whether
    someone else's site still serves a path.

    Args:
        path: The page the links were read from, used as the resolution base.
        lines: The lines to scan.

    Returns:
        ``(target as written, resolved path)`` pairs, in order.

    """
    return [
        (target, (path.parent / target.split("#", 1)[0]).resolve())
        for line in lines
        for match in MARKDOWN_LINK.finditer(line)
        if not (target := match.group("target")).startswith(
            ("http://", "https://", "#")
        )
    ]


def test_quick_start_prerequisites_link_to_the_setup_chooser() -> None:
    """
    The quick-start prerequisites send an unsure reader to the chooser first.

    DOCS-04 puts the link here rather than further down on purpose. The three
    deployment shapes differ in whether a SANE daemon is needed and what the
    scanner host is set to, and a reader who guesses wrong does not find out
    until the device list comes back empty.

    The target's existence is read off the filesystem rather than hard-coded,
    so renaming the page in a later phase surfaces here instead of becoming a
    404 on the busiest page in the documentation.
    """
    name = QUICK_START.relative_to(REPO_ROOT)
    section = _section_lines(QUICK_START, PREREQUISITES_HEADING)
    assert section, f"{name} has no `{PREREQUISITES_HEADING}` section"
    targets = [
        (target, resolved)
        for target, resolved in _resolved_link_targets(QUICK_START, section)
        if SETUP_CHOOSER_LINK_TARGET in target
    ]
    assert targets, (
        f"{name}'s prerequisites do not link to `{SETUP_CHOOSER_LINK_TARGET}`. "
        "The reader decides which deployment shape they are in before they "
        "install, or they debug the wrong one"
    )
    dangling = [target for target, resolved in targets if not resolved.is_file()]
    assert not dangling, (
        f"{name} links to a setup chooser that does not exist: {dangling}"
    )


def test_the_no_auth_note_appears_on_both_entry_surfaces() -> None:
    """
    Both entry surfaces say there is no login, and link to the proxy option.

    The web UI has no authentication and binds every interface. Whether that is
    acceptable is the operator's call, and they can only make it if they are
    told -- so it is said on the two pages someone deploys from, the quick
    start and the Docker reference, rather than only on the API reference that
    a reader following either of them never opens.

    D-50 rejected a page of its own for this. The assertion is therefore that
    each surface carries the sentence and a link that resolves to one of the
    two places the posture is already written out in full, not that it repeats
    the posture a third time.
    """
    for page in TRUST_NOTE_SURFACES:
        text, name = _read(page)
        lowered = text.lower()
        assert any(phrase in lowered for phrase in NO_AUTH_PHRASES), (
            f"{name} does not say the web UI has no login. An operator who is "
            f"never told cannot decide whether that is safe on their network"
        )
        assert any(phrase in lowered for phrase in ALL_INTERFACES_PHRASES), (
            f"{name} does not say the server binds every network interface"
        )
        linked = [
            (target, resolved)
            for target, resolved in _resolved_link_targets(page, text.splitlines())
            if resolved in TRUST_LINK_TARGETS
        ]
        assert linked, (
            f"{name} states the posture but links to neither of the two pages "
            "that explain what to do about it, so the sentence is a dead end"
        )
        dangling = [target for target, resolved in linked if not resolved.is_file()]
        assert not dangling, (
            f"{name} links to trust-model material that does not exist: {dangling}"
        )


# ---------------------------------------------------------------------------
# Phase 31: the corrected examples (rows 30, 31) -- DOCS-01
# ---------------------------------------------------------------------------

# The flag form of a bind mount, with the host side captured. The host side
# runs up to the first colon, which is where the container path begins.
VOLUME_FLAG_HOST = re.compile(r"(?:^|\s)(?:-v|--volume)[= ]\s*\"?(?P<host>[^\"\s:]+):")

RELATIVE_PREFIXES = ("./", "../")

# The ``id`` field of a JSON object, with its value captured.
JSON_ID_FIELD = re.compile(r'"id":\s*"(?P<value>[^"]*)"')

# What ``str(uuid.uuid4())`` produces: 8-4-4-4-12 lowercase hex, with the
# version nibble and the variant nibble both pinned, so a hand-typed string of
# the right length but the wrong shape does not pass for one.
UUID4_SHAPE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


def test_no_docker_run_example_uses_a_relative_host_path() -> None:
    """
    No ``-v``/``--volume`` flag in any example names a relative host path.

    Docker Engine has historically rejected a host side that is not an absolute
    path, so a block copied off one of these pages fails outright on a real
    host rather than doing something subtly different (row 31). ``"$(pwd)/..."``
    is the form that works, quoted so a directory whose name contains a space
    is not re-split into two arguments.

    Compose ``volumes:`` entries are a different syntax under different rules --
    a path beginning with a dot is correct there, and is the mount Phase 27 D-09
    requires -- so this looks only at the flag form and leaves YAML list items
    alone.
    """
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for path in _deployment_files()
        for number, line in _numbered(path)
        for match in VOLUME_FLAG_HOST.finditer(line)
        if match.group("host").startswith(RELATIVE_PREFIXES)
    ]
    assert not offenders, (
        "a bind-mount flag names a host path relative to wherever the reader "
        "happens to be standing:\n" + "\n".join(offenders)
    )


def test_the_job_json_example_shows_a_real_id_shape() -> None:
    """
    The scripting guide's job JSON shows an id in the shape ids really have.

    ``saneless jobs --json`` echoes ``j.id`` straight out of the row, and a job
    id is ``str(uuid.uuid4())`` -- see ``job.py``. The example used to show a
    truncated eight-character string, so anything written against it (a script
    that slices an id, a column sized to fit one, a fixture built to look like
    one) was written against a shape the program never emits (row 30).
    """
    text, name = _read(CLI_SCRIPTING)
    values = [match.group("value") for match in JSON_ID_FIELD.finditer(text)]
    assert values, f"{name} no longer shows a job id in its JSON example"
    wrong = [value for value in values if not UUID4_SHAPE.fullmatch(value)]
    assert not wrong, (
        f"{name} shows a job id that no saneless release can produce: {wrong}. "
        "Job ids are UUID4 strings"
    )


# ---------------------------------------------------------------------------
# Phase 31: the deploy guide links rather than copies (row 32, DOCS-06)
# ---------------------------------------------------------------------------

# The one setting the guide's half-a-service used to declare.
PAPERLESS_SERVICE_MARKER = "PAPERLESS_" + "SECRET_KEY"

# An image line whose value names the other project.
PAPERLESS_IMAGE_LINE = re.compile(
    r"^\s*(?:#\s*)?image:\s*\S*paperless\S*", re.MULTILINE
)

# Any absolute link into the other project's own material -- its documentation
# site or its repository. Which of the two is a choice for the page, not a
# contract.
PAPERLESS_NGX_HOME = "paperless-ngx"


def test_the_deploy_guide_does_not_ship_a_partial_paperless_stack() -> None:
    """
    The compose guide points at the other project's own file, never copies it.

    The example used to define half a service for it: one setting, no message
    broker, no database. The stack it described could not come up, so a reader
    who followed the guide ended with a container that restarts forever and
    nothing saying why.

    Completing it was the other option and was rejected (D-49). A copy of
    someone else's stack is a claim this project would have to keep true
    forever, against requirements that change without notice -- the bundled
    files currently ship Valkey as the broker, which is not what the copy
    here would have said.
    """
    text, name = _read(DEPLOY_HOWTO)
    assert PAPERLESS_SERVICE_MARKER not in text, (
        f"{name} still declares the other project's service settings itself"
    )
    images = PAPERLESS_IMAGE_LINE.findall(text)
    assert not images, (
        f"{name} still names the other project's image in a compose example: "
        f"{images}. Link to its own compose file instead"
    )
    linked = [
        target
        for match in MARKDOWN_LINK.finditer(text)
        if (target := match.group("target")).startswith(("http://", "https://"))
        and PAPERLESS_NGX_HOME in target
    ]
    assert linked, (
        f"{name} removed the half-a-service but tells the reader nowhere to "
        "get a working one. Link to the official compose documentation"
    )


# ---------------------------------------------------------------------------
# Phase 31: nothing from the planning tree is published (row 34, DOCS-03)
# ---------------------------------------------------------------------------

# Names that belong to the planning tree rather than to a reader.
PLANNING_ARTIFACT_NAMES = ("PRD.md", "ROADMAP.md", "REQUIREMENTS.md", "STATE.md")


def test_no_planning_artifact_is_published_under_docs() -> None:
    """
    No planning artifact sits under ``docs/``, whatever the nav says.

    MkDocs builds and publishes every Markdown file under ``docs/`` whether or
    not the nav lists it, so a page absent from the nav is still served and
    still search-indexed -- it is only unreachable by clicking. That gap is how
    a v1.0 planning document, whose claims stopped matching the code releases
    ago, ended up on the public site with nobody aware it was there (row 34).

    The document itself was not wrong to exist; it is a record of what v1.0
    intended. It was in the wrong tree, and it now lives beside the other
    planning artifacts.
    """
    offenders = [
        str(page.relative_to(REPO_ROOT))
        for page in _doc_pages()
        if page.name in PLANNING_ARTIFACT_NAMES
    ]
    assert not offenders, (
        "a planning artifact is published as part of the documentation "
        "site:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# Phase 31: what the final audit itself found (rows 4 and 10, DOCS-01)
# ---------------------------------------------------------------------------

# The heading whose body carries the threshold-tuning advice and its example.
PROFILE_TUNING_HEADING = "## Empty page detection tuning"

# An ``empty_page_*_threshold`` assignment inside a TOML example.
EMPTY_PAGE_THRESHOLD = re.compile(
    r"^empty_page_(?P<key>mean|stddev)_threshold = (?P<value>[0-9.]+)$",
    re.MULTILINE,
)

# The phrasings that get the dual-threshold rule backwards. ``is_empty_page``
# is ``mean > mean_threshold and stddev < stddev_threshold``, so lowering the
# mean threshold admits MORE pages to the blank set, never fewer.
INVERTED_TUNING_PHRASES = (
    "lower the thresholds to detect",
    "lower the thresholds to keep",
)

# The bullet that lists the words the history table's Status column shows.
HISTORY_STATUS_WORDS = re.compile(r"Current state of the job \((?P<words>[^)]*)\)")

# The trailing hedge in that bullet, which is prose rather than a label.
HISTORY_STATUS_HEDGE = "and so on"


def test_profile_howto_tuning_advice_matches_the_empty_page_rule() -> None:
    """
    The how-to's threshold example moves each threshold the way that keeps pages.

    ``pages.is_empty_page`` is ``mean > mean_threshold and stddev <
    stddev_threshold``.  Keeping a faint page therefore means **raising** the
    mean threshold and **lowering** the stddev threshold; lowering the mean
    threshold does the opposite of what the page promises, and the review
    caught that inversion as row 10.  The numbers are checked against
    ``ProfileConfig``'s own defaults, so a default change cannot leave a stale
    literal here passing.
    """
    text, name = _read(PROFILE_HOWTO)
    body = _section(text, PROFILE_TUNING_HEADING, name)
    lowered = body.lower()
    for phrase in INVERTED_TUNING_PHRASES:
        assert phrase not in lowered, (
            f"{name}'s tuning advice says {phrase!r}. Lowering the mean "
            "threshold makes MORE pages count as empty, so this tells the "
            "reader to do the opposite of what the sentence promises (row 10)"
        )
    assert "raise the mean threshold" in lowered, (
        f"{name} does not tell the reader to raise the mean threshold to keep "
        "faint pages, which is the direction is_empty_page actually rewards"
    )

    defaults = ProfileConfig(source="Flatbed")
    found = {
        match.group("key"): float(match.group("value"))
        for match in EMPTY_PAGE_THRESHOLD.finditer(body)
    }
    assert set(found) == {"mean", "stddev"}, (
        f"{name}'s tuning section no longer sets both thresholds in its "
        f"example; found {sorted(found)}"
    )
    assert found["mean"] > defaults.empty_page_mean_threshold, (
        f"{name}'s example sets empty_page_mean_threshold to {found['mean']}, "
        f"at or below the default {defaults.empty_page_mean_threshold}. That "
        "discards more pages, not fewer"
    )
    assert found["stddev"] < defaults.empty_page_stddev_threshold, (
        f"{name}'s example sets empty_page_stddev_threshold to "
        f"{found['stddev']}, at or above the default "
        f"{defaults.empty_page_stddev_threshold}. That discards more pages, "
        "not fewer"
    )


def test_first_web_ui_scan_names_real_history_labels() -> None:
    """
    Every word the walkthrough gives for the history Status column is a real label.

    The history table renders ``state_label(job.state)``; the status area above
    it spells the same terminal state differently -- ``DONE`` is "Complete" in
    the table and "Done" in the status line.  Naming the status area's word in
    the description of the table sends a reader looking for a string the table
    never renders.  Derived from ``JobState`` so a relabelling cannot leave
    this passing (row 4).
    """
    text, name = _read(FIRST_WEB_UI_SCAN)
    match = HISTORY_STATUS_WORDS.search(text)
    assert match is not None, (
        f"{name} no longer describes what the history table's Status column "
        "shows, so nothing pins its wording to the labels the table renders"
    )
    listed = [
        stripped
        for word in match.group("words").split(",")
        if (stripped := word.strip()) and stripped != HISTORY_STATUS_HEDGE
    ]
    assert listed, f"{name} lists no example status words at all"
    real = {state_label(state) for state in JobState}
    unreal = [word for word in listed if word not in real]
    assert not unreal, (
        f"{name} says the history table shows {unreal}, but state_label never "
        f"returns those. The labels it can return are {sorted(real)}"
    )


# ---------------------------------------------------------------------------
# Hook interpreter pin (tooling hygiene, found during the Phase 31 rehearsal)
# ---------------------------------------------------------------------------

PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"


def _ruff_target_python() -> str:
    """
    Return ruff's ``target-version`` as an interpreter name, e.g. ``python3.14``.

    Derived from ``pyproject.toml`` rather than written down here, because
    ruff's target is what decides which syntax ``ruff format`` may EMIT, and
    that is precisely what the hook interpreters have to be able to parse.

    Returns:
        The ``pythonX.Y`` interpreter name matching ruff's target version.

    """
    text, _ = _read(PYPROJECT)
    match = re.search(r'^target-version\s*=\s*"py(\d)(\d+)"', text, re.MULTILINE)
    assert match, f"{PYPROJECT.name} has no [tool.ruff] target-version"
    return f"python{match.group(1)}.{match.group(2)}"


def test_hook_interpreter_is_pinned_to_the_project_python() -> None:
    """
    The prek hooks run on the interpreter ruff targets, not whatever is found.

    ``check-ast`` and ``debug-statements`` parse this project's source with
    their own interpreter. Unpinned, prek builds each hook environment with
    whichever Python it happens to locate -- environments at 3.12.4, 3.13.9 and
    3.14.2 all existed on one machine at once. Anything below ruff's target
    rejects syntax ruff itself produces: PEP 758's bracketless
    ``except ValueError, TypeError:`` is a SyntaxError before 3.14, and
    ``ruff format`` writes exactly that at ``target-version = "py314"``.
    """
    text, name = _read(PRE_COMMIT_CONFIG)
    expected = _ruff_target_python()
    match = re.search(
        r"^default_language_version:\s*\n\s+python:\s*(\S+)\s*$",
        text,
        re.MULTILINE,
    )
    assert match, (
        f"{name} does not pin default_language_version.python. Without it prek "
        f"picks any interpreter, and one older than {expected} cannot parse the "
        f"syntax ruff format emits at this project's target-version"
    )
    assert match.group(1) == expected, (
        f"{name} pins the hook interpreter to {match.group(1)}, but ruff targets "
        f"{expected}. A hook older than ruff's target rejects syntax ruff writes"
    )


# ---------------------------------------------------------------------------
# The suppression ban, enforced instead of merely written down
# ---------------------------------------------------------------------------

# CLAUDE.md and CONTRIBUTING.md both forbid silencing a checker rather than
# fixing what it found, but prose cannot fail a build. The guard below turns
# the rule into something falsifiable: it reads every tracked Python file and
# reports any line carrying one of the six comment forms that do the
# silencing. Those are, in words: the bare linter-suppression comment; the
# shared type-checker suppression comment; the named per-checker form for the
# linter and for each of the two type checkers in turn; and the leading-colon
# form, which is what the file-level spellings all end in -- those disable
# every rule in a whole module at once and share no text with the other five.
# Matching is plain substring containment, so each bracketed per-rule variant
# is caught by the same entry that catches its bare form.
#
# Every marker is assembled at runtime from the fragments below rather than
# spelled out, for the reason the owner guard gives further up: this file is
# in scope of its own scan, so a literal marker anywhere here would make the
# guard report itself and go red with no real regression behind it. A join
# over an inline sequence is no good either, because ruff's FLY002 rewrites it
# straight back into the literal and suppressing the rule is forbidden -- the
# irony of which is the whole point of this guard. An f-string over named
# constants is the form FLY002 leaves alone. Each fragment on its own is
# harmless; only the concatenations are the banned text, and no line in this
# file spells one of them out.
_MARKER_LEAD = "# "
_MARKER_SEP = ": "
_SILENCE_LINT = "noqa"
_SILENCE_RULE = "ignore"
_CHECKER_TYPE = "type"
_CHECKER_RUFF = "ruff"
_CHECKER_PYREFLY = "pyrefly"
_CHECKER_TY = "ty"

SUPPRESSION_MARKERS = (
    f"{_MARKER_LEAD}{_SILENCE_LINT}",
    f"{_MARKER_LEAD}{_CHECKER_TYPE}{_MARKER_SEP}{_SILENCE_RULE}",
    f"{_MARKER_LEAD}{_CHECKER_RUFF}{_MARKER_SEP}{_SILENCE_RULE}",
    f"{_MARKER_LEAD}{_CHECKER_PYREFLY}{_MARKER_SEP}{_SILENCE_RULE}",
    f"{_MARKER_LEAD}{_CHECKER_TY}{_MARKER_SEP}{_SILENCE_RULE}",
    f"{_MARKER_SEP}{_SILENCE_LINT}",
)


def _shipped_python_files() -> list[str]:
    """
    Return every tracked ``.py`` file outside ``.planning/``.

    The suffix filter is not an optimisation. ``_shipped_files`` also returns
    Markdown and YAML, and the ban is stated in prose in CLAUDE.md, in
    CONTRIBUTING.md and in the CI workflow; scanning those would make the
    guard fail on the very documents that define the rule.

    Returns:
        Repo-relative names of the tracked Python files in scope.

    """
    return [name for name in _shipped_files() if Path(name).suffix == ".py"]


def test_no_shipped_python_file_carries_a_suppression_comment() -> None:
    """
    No tracked Python file silences a checker instead of fixing what it found.

    Scope follows ``git ls-files``, so a Python file added in a later phase is
    covered without anyone remembering to extend a list. This file is in scope
    of its own scan, which is why the markers it looks for are assembled at
    runtime rather than written out.
    """
    offenders: list[str] = []
    for name in _shipped_python_files():
        # Two clauses rather than one tuple, for the reason given in the
        # owner guard above.
        try:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except OSError:
            continue
        offenders.extend(
            f"{name}:{number}: {line.strip()}"
            for number, line in enumerate(text.splitlines(), start=1)
            if any(marker in line for marker in SUPPRESSION_MARKERS)
        )
    assert not offenders, (
        "a tracked Python file silences a checker with a suppression comment, "
        "which CLAUDE.md and CONTRIBUTING.md both forbid. Fix what the checker "
        "reported, at source, or change the rule set deliberately in "
        "pyproject.toml where the whole project can see it. Remove the comment "
        "from each line below:\n" + "\n".join(offenders)
    )
