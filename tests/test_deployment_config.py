"""
Static text tests for the documented Docker deployment (CFG-09, M-30, D-09).

saneless rewrites ``saneless.toml`` atomically: it writes a temp file beside the
config and renames it over the original. That rename only works when the
container sees the config *directory*. A single-file bind mount makes the kernel
refuse the rename with EBUSY, so every profile write fails on the deployment the
docs used to recommend. These tests hold the compose example and every doc page
to the read-write directory mount ``./config:/etc/saneless``, and they keep out
the old, false claim that the container fails to start without ``saneless.toml``.

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
``./config/saneless.toml`` -- and to APPL-11's consume-directory mount and
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
``src/``, and every release script under ``scripts/``, to comments that give
their own reasons, because the planning records they might otherwise point at
do not ship with the product.

The Phase 35 tests hold the declared ``>=`` floors to the versions ``uv.lock``
resolves, and hold the ``anyio`` ceiling to its declaration. The container
builds its environment with ``uv sync --locked`` in the builder stage, so the
floors no longer decide what ships; the published wheel's metadata still carries them, so they
remain the only thing a downstream non-lock install obeys (DEP-12, DEP-13,
D-09, D-10, D-11, D-17).

Plain-text assertions, with two stated exceptions: the contract is what an
operator copies, not what a YAML parser makes of it. The first is the
floor-to-lock guard at the foot of this file, which parses ``uv.lock`` with
``tomllib`` because that file is machine-generated, is copied by nobody, and
hides the one failure a line scanner cannot see -- two ``[[package]]`` entries
for a single declared name. The second is ``packaging``, used wherever a guard
has to decide what a version or a version range means: the published-image
guard reads ``project.version`` to tell a release candidate from a final
release, the uv guard asks whether each pinned uv lies inside the declared
``required-version`` range, and the interpreter guard asks whether
``.python-version`` satisfies ``requires-python``. PEP 440 ordering and range
membership are not things to re-implement with a regular expression.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
import subprocess
import sys

# One of the two parsing imports this module's plain-text rule allows. It
# serves the floor-to-lock guard at the foot of the file, where the reason is
# set out in full: `uv.lock` is machine-generated TOML that no operator
# copies, and a line scanner cannot see two `[[package]]` entries for one
# name. It also reads `project.version` for the published-image guard.
import tomllib
from pathlib import Path
from typing import Any

import pytest

# The other parsing import: the published-image guard parses the project
# version with it to tell a release candidate from a final release, and the
# uv and interpreter guards test version-range membership with it.
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from saneless import worker as worker_module
from saneless.checks import CheckKey, check_name
from saneless.config import (
    OutputConfig,
    ProfileConfig,
    WebConfig,
    is_placeholder_token,
)
from saneless.pages import (
    EDGE_TRIM,
    INK_DELTA,
    PAPER_PERCENTILE,
    PAPER_WHITE_FLOOR,
    filter_blank_pages,
    is_blank,
)
from saneless.scanner.base import PageRecord
from saneless.vocabulary import (
    HIDDEN_JOB_TITLE,
    ExitCode,
    JobState,
    RequestRejection,
    job_label,
    rejection_message,
    rejection_status_code,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker-compose.yml"
README = REPO_ROOT / "README.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"
DOCS_DIR = REPO_ROOT / "docs"

DIRECTORY_MOUNT = "./config:/etc/saneless"
# The same directory at the same container path, spelled for a bind-mount flag,
# where the host side has to be absolute and quoted (row 31).
ABSOLUTE_DIRECTORY_MOUNT = '"$(pwd)/config:/etc/saneless"'
DIRECTORY_MOUNT_FORMS = (DIRECTORY_MOUNT, ABSOLUTE_DIRECTORY_MOUNT)

# The one config filename saneless reads, and the one it used to read. The
# legacy name is assembled from named parts in an f-string rather than written
# as a single literal, because the repository sweep guard forbids that literal
# in every shipped file -- this one included -- and a literal here would make
# the guard report its own definition. It is the ``FORBIDDEN_OWNER_SLUG``
# idiom, and for the same reason: ruff's FLY002 rewrites a ``"".join`` over an
# inline sequence straight back into the literal, and suppression is forbidden.
CONFIG_NAME = "saneless.toml"
_LEGACY_STEM = "config"
_TOML_EXT = "toml"
LEGACY_CONFIG_NAME = f"{_LEGACY_STEM}.{_TOML_EXT}"

# Both spellings of the single-file bind mount that breaks the atomic rename.
# The legacy spelling stays: an operator upgrading from it copies the old line
# out of an old guide, so dropping it would make this guard vacuous for exactly
# the deployment it exists to catch.
SINGLE_FILE_MOUNTS = (
    f"{LEGACY_CONFIG_NAME}:/etc/saneless/{LEGACY_CONFIG_NAME}",
    f"{CONFIG_NAME}:/etc/saneless/{CONFIG_NAME}",
)

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


def _is_single_file_mount(line: str) -> bool:
    """Say whether a line bind-mounts the config file itself, in either spelling."""
    return any(mount in line for mount in SINGLE_FILE_MOUNTS)


def test_no_single_file_config_mount_anywhere() -> None:
    """No compose file or doc page bind-mounts the config file on its own."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for path in _deployment_files()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if _is_single_file_mount(line)
    ]
    assert not offenders, "single-file config mount found:\n" + "\n".join(offenders)


def test_the_single_file_mount_guard_fires_for_both_spellings() -> None:
    """
    The guard above can fail, for the old filename and for the new one.

    The rename is what makes this worth asserting. A guard written around one
    literal filename goes quietly vacuous the moment the file is renamed: it
    keeps passing, on every page, forever, while the mount it was written to
    catch sails through under the other name. Feeding one synthetic line per
    spelling through the same predicate the guard uses is the cheapest proof
    that neither spelling is a blind spot.
    """
    for mount in SINGLE_FILE_MOUNTS:
        line = f"      - ./{mount}:ro"
        assert _is_single_file_mount(line), (
            f"the single-file mount guard does not fire for {line.strip()!r}, "
            f"so a page could carry that mount unnoticed"
        )
    assert not _is_single_file_mount(f"      - {DIRECTORY_MOUNT}"), (
        "the single-file mount guard fires for the directory mount the docs "
        "are supposed to recommend, so it would fail on correct pages"
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
        if f"config/{CONFIG_NAME}" in line and "mv" in line
    ]
    assert migration, (
        f"{name} has no migration step moving an old single-file config to "
        f"./config/{CONFIG_NAME}"
    )


def test_deploy_doc_says_where_auto_profiles_writes_without_a_config_file() -> None:
    """
    With no ``saneless.toml``, the doc names the real write path (WR-06).

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
        f"{AUTO_PROFILES_CONTAINER_PATH} when the config file is missing"
    )
    assert f"touch config/{CONFIG_NAME}" in text, (
        f"{name} does not tell container users to create config/{CONFIG_NAME} first"
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
    never touches SANE, so it has no 1.  Only ``scan`` delivers a document, so
    only ``scan`` has 6 (saved to the consume folder) and 7 (uploaded with a
    warning).  ``serve``'s 3 is a TLS trust store it cannot read when it
    builds the Paperless client; a malformed Paperless URL never gets that
    far, because the config load refuses it with a 2.

    ``doctor`` has the same four codes as ``jobs``, for three separate reasons.
    No 1: it never fails on SANE at all -- Amendment A-1 turns a missing
    python-sane into a ``FAIL`` row rather than a refusal, and the scanner
    check reports an unreachable device instead of raising. No 3: it does
    construct a Paperless client, but ``test_connection`` returns a status
    rather than raising, and a URL the client cannot be built from becomes the
    "not found at that URL" row instead of a ``PaperlessError``. No 4: it
    assembles nothing.

    Only ``scan`` judges pages blank, so only ``scan`` has 8.  Every one-shot
    command but ``serve`` installs handlers for SIGHUP and SIGTERM, so each of
    them has 129 and 143 (128 + the signal number): an interruption that keeps
    the pages already scanned, unlike the cancel's 130.  ``serve`` keeps
    uvicorn's own handlers, for which a SIGTERM is a graceful stop, exit 0.
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
    assert _documented_codes(tables["scan"]) == {
        0,
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        129,
        130,
        143,
    }
    assert _documented_codes(tables["devices"]) == {0, 1, 2, 5, 129, 130, 143}
    assert _documented_codes(tables["jobs"]) == {0, 2, 5, 129, 130, 143}
    assert _documented_codes(tables["serve"]) == {0, 2, 3, 5, 130}
    assert _documented_codes(tables["auto-profiles"]) == {0, 1, 2, 5, 129, 130, 143}
    assert _documented_codes(tables["doctor"]) == {0, 2, 5, 129, 130, 143}
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
        "blank",
        "Interrupted",
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
# line here silently overrides ``./config/saneless.toml`` -- the U-01 finding.
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
        "./config/saneless.toml (D-17, U-01):\n" + "\n".join(offenders)
    )


def test_compose_says_the_environment_block_overrides_the_config_file() -> None:
    """The commented block explains the override and the upgrade action."""
    text = COMPOSE.read_text(encoding="utf-8").lower()
    for needle in ("override", CONFIG_NAME):
        assert needle in text, (
            f"{COMPOSE.name} does not contain {needle!r}, so it does not "
            f"say that an environment line overrides {CONFIG_NAME} (D-17)"
        )
    assert "delete" in text or "remove" in text, (
        f"{COMPOSE.name} does not tell an operator who copied an earlier "
        "version to remove their own token line, so their real saneless.toml "
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


# A compose duration saneless ships: whole minutes and seconds, such as "90s".
_COMPOSE_DURATION = re.compile(r"^(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$")

# Room above the two join bounds for uvicorn to stop serving and for the
# lifespan's own closes, before Docker's SIGKILL.
_GRACE_MARGIN_SECONDS = 10.0

# The container's scratch directory named as a path, not as part of a longer
# word or path.  A pattern rather than a plain needle, which would read as a
# hard-coded temporary directory.
_TMP_PATH = re.compile(r"(?<![\w./])/tmp\b")

# The heading of the deploy guide's section on stopping the container.
_STOPPING_HEADING = "## Stopping and restarting"


def _grace_period_lines(path: Path) -> list[tuple[int, str]]:
    """Return every live ``stop_grace_period:`` line in ``path``, numbered."""
    return [
        (number, line)
        for number, line in _numbered(path)
        if line.strip().startswith("stop_grace_period:")
    ]


def _grace_seconds(line: str, name: str) -> float:
    """
    Parse one ``stop_grace_period:`` line's value into seconds.

    Args:
        line: The whole line.
        name: The file it came from, for the failure message.

    Returns:
        The grace period, in seconds.

    """
    value = line.split(":", 1)[1].strip().strip("\"'")
    assert value, f"{name}: stop_grace_period has no value"
    match = _COMPOSE_DURATION.match(value)
    assert match is not None, (
        f"{name}: stop_grace_period {value!r} is not a duration like '90s'"
    )
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return float(minutes * 60 + seconds)


def _preservation_budget() -> float:
    """Return the longest a stopping server may need: both joins and a margin."""
    return (
        worker_module.STOP_JOIN_SECONDS
        + worker_module.PRESERVATION_JOIN_SECONDS
        + _GRACE_MARGIN_SECONDS
    )


def test_compose_gives_a_stopping_scan_time_to_keep_its_pages() -> None:
    """
    ``stop_grace_period`` covers the worker's join and its preservation extension.

    Docker's default grace is 10 s, then SIGKILL.  A scan stopped at the flip
    prompt keeps its fronts by copying them from ``/tmp`` to the data volume,
    a different filesystem, and ``docker compose up -d`` recreates the
    container and discards ``/tmp``, so a copy cut short is lost for good.
    The setting must sit on the saneless service, be at least both join
    bounds plus a margin, and carry a comment saying why.
    """
    lines = _numbered(COMPOSE)
    graces = [
        index
        for index, (_, line) in enumerate(lines)
        if line.strip().startswith("stop_grace_period:")
    ]
    assert len(graces) == 1, (
        f"{COMPOSE.name}: expected exactly one live stop_grace_period line, "
        f"found {len(graces)}"
    )
    index = graces[0]
    line = lines[index][1]
    assert line.startswith("    stop_grace_period:"), (
        f"{COMPOSE.name}:{lines[index][0]}: stop_grace_period is not indented "
        "as a key of services.saneless"
    )
    service = next(
        position for position, (_, text) in enumerate(lines) if text == "  saneless:"
    )
    service_end = next(
        (
            position
            for position, (_, text) in enumerate(lines)
            if position > service and text and not text.startswith((" ", "#"))
        ),
        len(lines),
    )
    assert service < index < service_end, (
        f"{COMPOSE.name}:{lines[index][0]}: stop_grace_period is outside "
        "services.saneless"
    )
    seconds = _grace_seconds(line, COMPOSE.name)
    assert seconds >= _preservation_budget(), (
        f"{COMPOSE.name}: stop_grace_period is {seconds:g} s, less than the "
        f"worker's {_preservation_budget():g} s worst case"
    )
    above = _comment_lines_above(lines, index)
    assert above >= 1, f"{COMPOSE.name}: stop_grace_period has no comment above it"
    comment = " ".join(text for _, text in lines[index - above : index])
    assert _TMP_PATH.search(comment), (
        f"{COMPOSE.name}: the comment above stop_grace_period does not name "
        "the container's /tmp, so it does not say why the grace is needed"
    )
    assert "failed/" in comment, (
        f"{COMPOSE.name}: the comment above stop_grace_period does not name "
        "failed/, so it does not say what the grace protects"
    )


def test_the_deploy_guide_explains_the_stop_grace_period() -> None:
    """
    The guide says why a stop needs time, and its compose example sets it.

    Operators copy the guide's compose block as well as the shipped file, so
    the example carries the same grace period.  The explanation names the two
    facts that make a cut-short copy unrecoverable: ``/tmp`` and the data
    volume are different filesystems, and ``docker compose up -d`` recreates
    the container.  It also covers ``docker run``, whose flag has another name.
    """
    text = DEPLOY_HOWTO.read_text(encoding="utf-8")
    shipped = _grace_period_lines(COMPOSE)
    assert len(shipped) == 1
    example = _grace_period_lines(DEPLOY_HOWTO)
    assert len(example) == 1, (
        f"{DEPLOY_HOWTO.name}: expected the compose example to set "
        f"stop_grace_period once, found {len(example)}"
    )
    assert _grace_seconds(example[0][1], DEPLOY_HOWTO.name) == _grace_seconds(
        shipped[0][1], COMPOSE.name
    )
    section = _section(text, _STOPPING_HEADING, DEPLOY_HOWTO)
    assert _TMP_PATH.search(section), (
        f"{DEPLOY_HOWTO.name}: the {_STOPPING_HEADING!r} section does not "
        "name the container's /tmp"
    )
    for needle in (
        "stop_grace_period",
        "failed/",
        "docker compose up -d",
        "--stop-timeout",
    ):
        assert needle in section, (
            f"{DEPLOY_HOWTO.name}: the {_STOPPING_HEADING!r} section does not "
            f"mention {needle!r}"
        )
    # The stop is bounded: only a scan at the flip wait is interrupted, and
    # the rest is recovered by the next start of the same container only.
    prose = " ".join(section.split())
    for needle in ("is not interrupted", "docker compose restart"):
        assert needle in prose, (
            f"{DEPLOY_HOWTO.name}: the {_STOPPING_HEADING!r} section does not "
            f"say {needle!r}"
        )


def test_the_signal_exit_codes_do_not_promise_a_kept_file() -> None:
    """
    After 129 or 143 a script is told to look where the line points, not blindly.

    Every command but ``serve`` exits 129 or 143 on a signal, and most of them
    -- or a scan stopped before its first page -- keep nothing, so the pages
    say that a line naming no path kept nothing.
    """
    for path in (CLI_SCRIPTING, CLI_REFERENCE, TROUBLESHOOTING):
        text, name = _read(path)
        prose = " ".join(text.split())
        assert "names no path" in prose or "No path on that line" in prose, (
            f"{name} does not say what an Interrupted: line with no path means"
        )


def test_the_signal_exit_codes_admit_a_settled_outcome_and_failed_keeping() -> None:
    """
    129 and 143 are not promised for every signal, nor a kept file for each.

    A signal that arrives once a scan's outcome is settled leaves the command
    its own exit code, and a scan interrupted while ``failed/`` cannot be
    written keeps nothing there: every page that documents the codes says so,
    and no 129 or 143 table row claims the pages were kept unconditionally.
    """
    for path in (CLI_SCRIPTING, CLI_REFERENCE, TROUBLESHOOTING):
        text, name = _read(path)
        prose = " ".join(text.split())
        assert "outcome's own code" in prose, (
            f"{name} does not say a settled outcome keeps its own exit code"
        )
    overclaims = [
        f"{path.name}:{number}: {line.strip()}"
        for path in (CLI_SCRIPTING, CLI_REFERENCE, TROUBLESHOOTING)
        for number, line in _numbered(path)
        if line.startswith(("| 129 |", "| 143 |"))
        and "kept in `failed/`" in line
        and "when they could be" not in line
    ]
    assert not overclaims, (
        "a 129/143 row says the pages were kept whether or not they could be:\n"
        + "\n".join(overclaims)
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
    for needle in ("override", CONFIG_NAME):
        assert needle in lowered, (
            f"{name} does not contain {needle!r}, so it does not explain that "
            f"an environment line overrides {CONFIG_NAME} (D-17)"
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

    This needs its own assertion because nothing else catches it. Neither the
    build backend nor ``twine check`` errors when the classifier ships beside a
    PEP 639 ``License-Expression`` -- both were measured doing exactly that --
    so no build or publish step would fail if it came back.
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
        f"{PUBLISHED_IMAGE}:\n" + "\n".join(offenders)
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
# The directories whose files ship with the repository and are read by people
# who have no copy of the planning records: the package itself, and the
# release scripts the workflows run. The hook's ``files:`` pattern below says
# the same thing in regex form, and a test keeps the two in step.
_SOURCE_PREFIXES = ("src/", "scripts/")
CITATION_HOOK_FILES = r"^(src|scripts)/"
_SOURCE_SUFFIXES = frozenset({".py", ".html", ".css", ".js"})
# The vendored htmx and Pico files are upstream bytes pinned by an integrity
# hash, so they are neither ours to comment nor ours to edit.
_VENDOR_PREFIX = "src/saneless/web/static/vendor/"
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"


def _shipped_source_files() -> list[str]:
    """
    Return the tracked source and script files under src/ and scripts/.

    Returns:
        Repo-relative path names, vendored assets excluded.

    """
    return [
        name
        for name in _shipped_files()
        if name.startswith(_SOURCE_PREFIXES)
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
    No shipped source or script file points at the planning records.

    A comment that says only "see decision so-and-so" tells a reader of the
    product nothing, because the planning directory does not ship with it.
    Each comment in src/ and scripts/ has to carry its own reason in plain
    words: the release scripts ship with the repository just as the package
    does, and a workflow reader lands in them first.

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
        "a file under src/ or scripts/ cites a planning artefact or could not "
        "be read. "
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


def test_the_citation_hook_covers_src_and_scripts() -> None:
    """
    The commit hook reads the same directories the guard above reads.

    The pattern is only half of what the two share. A hook scoped to src/
    alone would let a citation into a release script at commit, merge and
    push, and only CI would say so. The regex and the prefix list are written
    once each, and this test holds them to the same directories.
    """
    lines = _significant_lines(PRE_COMMIT_CONFIG)
    starts = [
        index
        for index, (_number, line) in enumerate(lines)
        if line == "- id: no-planning-citations"
    ]
    assert len(starts) == 1, (
        f"{PRE_COMMIT_CONFIG.name} does not declare exactly one "
        "no-planning-citations hook, so there is no hook scope to check"
    )
    window: list[str] = []
    for _number, line in lines[starts[0] + 1 :]:
        if line.startswith("- "):
            break
        window.append(line)
    assert f"files: {CITATION_HOOK_FILES}" in window, (
        f"the no-planning-citations hook is not scoped to {CITATION_HOOK_FILES}, "
        "so it checks a different set of directories from the guard test. "
        f"Its lines read: {window}"
    )
    files = re.compile(CITATION_HOOK_FILES)
    assert all(files.match(prefix) for prefix in _SOURCE_PREFIXES)
    assert files.match("tests/test_deployment_config.py") is None


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
# Published image tags and install lines
# ---------------------------------------------------------------------------

# The published image name is assembled at runtime, the FORBIDDEN_OWNER_SLUG
# idiom again: the sweep below reads every shipped file, this one included,
# and a literal here would be a reference carrying no tag. An f-string over
# named constants is the form ruff's FLY002 leaves alone.
_IMAGE_REGISTRY = "ghcr.io"
_IMAGE_OWNER = "kdknigga"
_PROJECT_NAME = "saneless"
PUBLISHED_IMAGE = f"{_IMAGE_REGISTRY}/{_IMAGE_OWNER}/{_PROJECT_NAME}"

# One reference to the published image, with its tag if it has one. The
# lookahead stops a longer repository name that merely starts with ours from
# counting as an untagged reference to this image.
IMAGE_REFERENCE = re.compile(
    re.escape(PUBLISHED_IMAGE) + r"(?![\w-])(?::(?P<tag>[0-9A-Za-z._-]+))?"
)

# The release workflow names the image without a tag on purpose in two
# places: its ``images:`` key is the input the tagging action expands into
# every tag a release publishes, and its ``subject-name:`` keys name the
# repository an attestation is stored against, with the digest supplied
# beside it. Those lines, in that one file, are the only references allowed
# to go untagged.
_RELEASE_WORKFLOW_NAME = ".github/workflows/release.yml"
_IMAGES_KEY = "images:"
_SUBJECT_NAME_KEY = "subject-name:"
_UNTAGGED_RELEASE_KEYS = (_IMAGES_KEY, _SUBJECT_NAME_KEY)

# What follows the image name in a digest reference. A digest names one
# immutable set of bytes, which is stricter than any tag, so a reference in
# that form is never stale and never resolves to ``latest``.
_DIGEST_SEPARATOR = "@"

# The install-from-the-package-index command, assembled from fragments for the
# same self-scan reason. Every spelling that resolves the name on the index
# counts: ``pip``, ``pip3``, ``pip3.14``, ``python -m pip`` and ``uv pip``
# with ``install``; ``pipx`` and ``uv tool`` with ``install`` or ``run``; and
# ``uvx``. Flags may sit between the command and the name (``-U``,
# ``--upgrade``, ``--force``), the name may be quoted, and it must be the whole
# name -- not a longer name that starts with it. Case is ignored, because the
# package index ignores it. An install from the repository names ``git+...``
# where the project name would be, so it never matches.
_PIP = "pip"
_UV = "uv"
_INSTALL = "install"
PYPI_INSTALL = re.compile(
    rf"(?<![\w.-])(?:"
    rf"(?:{_PIP}(?:3(?:\.\d+)?)?|python3?\s+-m\s+{_PIP}|{_UV}\s+{_PIP})"
    rf"\s+{_INSTALL}"
    rf"|(?:{_PIP}x|{_UV}\s+tool)\s+(?:{_INSTALL}|run)"
    rf"|{_UV}x"
    rf")(?:\s+-{{1,2}}[\w-]+(?:=\S+)?)*\s+['\"]?{_PROJECT_NAME}(?![\w-])",
    re.IGNORECASE,
)
_PYPI_INSTALL_LINE = f"{_PIP} {_INSTALL} {_PROJECT_NAME}"
_PIPX_INSTALL_LINE = f"{_PIP}x {_INSTALL} {_PROJECT_NAME}"

# A tag no published image could carry. Measuring the real tree against it
# turns every reference into an offender, which counts how many the sweep saw.
_IMPOSSIBLE_TAG = "never a published tag"


def _declared_version() -> str:
    """
    Return ``project.version`` exactly as ``pyproject.toml`` spells it.

    Returns:
        The declared version string, unnormalised.

    """
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return pyproject["project"]["version"]


def _expected_image_tag(declared: str) -> str:
    """
    Return the image tag the documentation must pin for a declared version.

    A release candidate publishes only its own exact tag, so that is the only
    tag a reader can pull. The tag is the version as ``pyproject.toml`` spells
    it, which is the spelling the release tag and so the image tag carry. A
    final release also publishes a ``major.minor`` tag that later patch
    releases move forward, and that is the one the documentation pins.

    Args:
        declared: ``project.version`` as written in ``pyproject.toml``.

    Returns:
        The declared string for a pre-release, else ``major.minor``.

    """
    version = Version(declared)
    if version.is_prerelease:
        return declared
    return f"{version.major}.{version.minor}"


def _read_or_report(root: Path, name: str, offenders: list[str]) -> str | None:
    """
    Return a file's text, or record it as an offender and return ``None``.

    Args:
        root: The directory the name is relative to.
        name: Repo-relative file name to read.
        offenders: The list an unreadable file is reported into.

    Returns:
        The file's text, or ``None`` when it could not be read.

    """
    # Two clauses rather than one tuple, for the reason given in the owner
    # guard above.
    try:
        return (root / name).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        offenders.append(f"{name}: not UTF-8, so it was not checked: {exc}")
    except OSError as exc:
        offenders.append(f"{name}: unreadable, so it was not checked: {exc}")
    return None


def _image_tag_offenders(names: list[str], root: Path, expected: str) -> list[str]:
    """
    Return every image reference that does not carry the expected tag.

    An untagged reference is an offender like any other: the registry resolves
    it to ``latest``, which a release candidate never receives. A reference in
    digest form is not, because it names exact bytes rather than a movable
    tag. A file the guard cannot read is an offender too, since it has not been
    checked.

    Args:
        names: Repo-relative file names to check.
        root: The directory the names are relative to.
        expected: The one tag every reference must carry.

    Returns:
        One ``name:line: text`` entry per wrong or missing tag, and one
        ``name: reason`` entry per file that could not be read.

    """
    offenders: list[str] = []
    for name in names:
        text = _read_or_report(root, name, offenders)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if name == _RELEASE_WORKFLOW_NAME and line.strip().startswith(
                _UNTAGGED_RELEASE_KEYS
            ):
                continue
            offenders.extend(
                f"{name}:{number}: {line.strip()}"
                for match in IMAGE_REFERENCE.finditer(line)
                if match.group("tag") != expected
                and not line.startswith(_DIGEST_SEPARATOR, match.end())
            )
    return offenders


def _pip_install_offenders(names: list[str], root: Path) -> list[str]:
    """
    Return every line that installs the project from the package index.

    Args:
        names: Repo-relative file names to check.
        root: The directory the names are relative to.

    Returns:
        One ``name:line: text`` entry per index install, and one
        ``name: reason`` entry per file that could not be read.

    """
    offenders: list[str] = []
    for name in names:
        text = _read_or_report(root, name, offenders)
        if text is None:
            continue
        offenders.extend(
            f"{name}:{number}: {line.strip()}"
            for number, line in enumerate(text.splitlines(), start=1)
            if PYPI_INSTALL.search(line)
        )
    return offenders


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("0.2.0-rc.6", "0.2.0-rc.6"),
        ("0.2.0", "0.2"),
        ("1.4.3", "1.4"),
    ],
)
def test_the_expected_image_tag_follows_the_kind_of_release(
    declared: str, expected: str
) -> None:
    """A release candidate pins its exact tag; a final release pins major.minor."""
    assert _expected_image_tag(declared) == expected


def test_every_shipped_image_reference_carries_the_published_image_tag() -> None:
    """
    Every shipped reference to the image names a tag the registry holds.

    The expected tag is derived from ``project.version``, so the commit that
    makes the version final is the commit that has to move every example from
    the release-candidate tag to ``major.minor``. Until then an example
    pulling ``latest``, or no tag at all, fails with "manifest unknown".
    """
    names = _shipped_files()
    expected = _expected_image_tag(_declared_version())

    examined = _image_tag_offenders(names, REPO_ROOT, _IMPOSSIBLE_TAG)
    assert len(examined) >= 10, (
        f"the sweep found only {len(examined)} references to {PUBLISHED_IMAGE} "
        "in the shipped files, so it is no longer looking where the "
        "deployment examples live:\n" + "\n".join(examined)
    )

    offenders = _image_tag_offenders(names, REPO_ROOT, expected)
    assert not offenders, (
        f"a shipped reference to {PUBLISHED_IMAGE} does not carry the tag "
        f"{expected!r} that project.version publishes, or a file could not be "
        "read. A release candidate publishes only its exact tag and never "
        "latest; a final release pins major.minor:\n" + "\n".join(offenders)
    )


def test_the_image_tag_guard_reports_wrong_missing_and_unread_tags(
    tmp_path: Path,
) -> None:
    """
    Wrong and missing tags are reported, and so is a file that was not read.

    The release workflow's untagged ``images:`` and ``subject-name:`` lines
    are exempt there and nowhere else, and a digest reference is exempt
    everywhere; a ``latest`` pull in the release workflow is still reported.
    """
    expected = "0.2.0-rc.6"
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "release.yml").write_text(
        f"          {_IMAGES_KEY} {PUBLISHED_IMAGE}\n"
        f"          run: docker pull {PUBLISHED_IMAGE}\n"
        f"          {_SUBJECT_NAME_KEY} {PUBLISHED_IMAGE}\n"
        f"          image: {PUBLISHED_IMAGE}{_DIGEST_SEPARATOR}sha256:0123abcd\n"
        f"          run: docker pull {PUBLISHED_IMAGE}:latest\n",
        encoding="utf-8",
    )
    seeded = {
        "latest.yml": f"    image: {PUBLISHED_IMAGE}:latest\n",
        "untagged.md": f"docker run -p 8080:8080 {PUBLISHED_IMAGE}\n",
        "older.md": f"docker pull {PUBLISHED_IMAGE}:0.2.0-rc.5\n",
        "elsewhere.yml": f"{_IMAGES_KEY} {PUBLISHED_IMAGE}\n",
        "pinned.md": f"docker pull {PUBLISHED_IMAGE}:{expected}\n",
        "longer-name.md": f"docker pull {PUBLISHED_IMAGE}-other:latest\n",
        "subject.yml": f"    {_SUBJECT_NAME_KEY} {PUBLISHED_IMAGE}\n",
        "digest.md": (
            f"docker pull {PUBLISHED_IMAGE}{_DIGEST_SEPARATOR}sha256:0123abcd\n"
        ),
    }
    for name, text in seeded.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    (tmp_path / "latin1.md").write_bytes(b"caf\xe9\n")

    offenders = _image_tag_offenders(
        [_RELEASE_WORKFLOW_NAME, *seeded, "latin1.md", "gone.md"],
        tmp_path,
        expected,
    )

    assert [entry.split(": ", 1)[0] for entry in offenders] == [
        f"{_RELEASE_WORKFLOW_NAME}:2",
        f"{_RELEASE_WORKFLOW_NAME}:5",
        "latest.yml:1",
        "untagged.md:1",
        "older.md:1",
        "elsewhere.yml:1",
        "subject.yml:1",
        "latin1.md",
        "gone.md",
    ]
    assert "not UTF-8" in offenders[-2]
    assert "unreadable" in offenders[-1]


def test_the_image_tag_guard_moves_to_major_minor_at_a_final_release(
    tmp_path: Path,
) -> None:
    """Once the version is final, the release-candidate tag is stale."""
    (tmp_path / "rc.md").write_text(
        f"docker pull {PUBLISHED_IMAGE}:0.2.0-rc.6\n", encoding="utf-8"
    )
    (tmp_path / "final.md").write_text(
        f"docker pull {PUBLISHED_IMAGE}:0.2\n", encoding="utf-8"
    )

    offenders = _image_tag_offenders(
        ["rc.md", "final.md"], tmp_path, _expected_image_tag("0.2.0")
    )

    assert [entry.split(":", 1)[0] for entry in offenders] == ["rc.md"]


def test_no_shipped_pip_install_line_uses_the_package_index_before_final() -> None:
    """
    While the version is a pre-release, nothing installs from the package index.

    The project name is not yet held on the package index, so an install line
    naming it there resolves to whoever registers it first. Install lines
    point at the project's own repository instead until a final release has
    claimed the name. Once the version is final this guard stands down,
    because no offline test can know whether that claim has happened.
    """
    declared = _declared_version()
    if not Version(declared).is_prerelease:
        pytest.skip(
            f"project.version {declared} is final, so installing from the "
            "package index is allowed again"
        )
    offenders = _pip_install_offenders(_shipped_files(), REPO_ROOT)
    assert not offenders, (
        f"a shipped file installs {_PROJECT_NAME} from the package index "
        f"while project.version is the pre-release {declared}, or a file "
        "could not be read. Until a final release claims the name, install "
        "from git+https://github.com/kdknigga/saneless:\n" + "\n".join(offenders)
    )


def test_the_pip_install_guard_tells_the_index_from_the_repository(
    tmp_path: Path,
) -> None:
    """
    Index installs and unread files are reported; repository installs are not.

    Every command that resolves the name on the index counts, however it is
    spelled: another pip executable, flags before the name, a quoted name, or
    one of the uv and pipx commands that run a package without installing it.
    """
    name = _PROJECT_NAME
    repository = f"git+https://github.com/{_IMAGE_OWNER}/{name}"
    index_installs = {
        "pip.md": f"    {_PYPI_INSTALL_LINE}\n",
        "pipx.md": f"    {_PIPX_INSTALL_LINE}\n",
        "prose.md": f"then retry `{_PYPI_INSTALL_LINE}`.\n",
        "pip3.md": f"{_PIP}3 {_INSTALL} {name}\n",
        "pip3-14.md": f"{_PIP}3.14 {_INSTALL} {name}\n",
        "upgrade-short.md": f"{_PIP} {_INSTALL} -U {name}\n",
        "upgrade-long.md": f"{_PIP} {_INSTALL} --upgrade '{name}'\n",
        "pipx-force.md": f"{_PIP}x {_INSTALL} --force {name}\n",
        "pipx-run.md": f"{_PIP}x run {name} serve\n",
        "python-m.md": f"python3 -m {_PIP} {_INSTALL} {name}[web]\n",
        "uv-pip.md": f"{_UV} {_PIP} {_INSTALL} {name}==0.2.0\n",
        "uv-tool.md": f"{_UV} tool {_INSTALL} {name}\n",
        "uv-tool-run.md": f"{_UV} tool run {name}\n",
        "uvx.md": f"{_UV}x {name} --help\n",
    }
    repository_installs = {
        "git-pipx.md": f"{_PIP}x {_INSTALL} {repository}\n",
        "git-pip.md": f"{_PIP} {_INSTALL} {repository}\n",
        "git-upgrade.md": f"{_PIP} {_INSTALL} --upgrade {repository}\n",
        "git-uv-tool.md": f"{_UV} tool {_INSTALL} {repository}\n",
        "git-uvx.md": f"{_UV}x --from {repository} {name}\n",
        "other.md": f"{_PIPX_INSTALL_LINE}-plugin\n",
        "longer-command.md": f"my{_PIP} {_INSTALL} {name}\n",
    }
    seeded = {**index_installs, **repository_installs}
    for file_name, text in seeded.items():
        (tmp_path / file_name).write_text(text, encoding="utf-8")
    (tmp_path / "latin1.md").write_bytes(b"caf\xe9\n")

    offenders = _pip_install_offenders([*seeded, "latin1.md", "gone.md"], tmp_path)

    assert [entry.split(":", 1)[0] for entry in offenders] == [
        *index_installs,
        "latin1.md",
        "gone.md",
    ]


# ---------------------------------------------------------------------------
# Phase 31: the container build context (D-29, D-30, D-33, DLVR-06, DLVR-09)
# ---------------------------------------------------------------------------

DOCKERIGNORE = REPO_ROOT / ".dockerignore"
GITIGNORE = REPO_ROOT / ".gitignore"

# Path fragments that must never appear on a ``!`` re-include line. ``config``
# and ``saneless.toml`` are where a live paperless-ngx token lives; ``tests``
# and ``.planning`` are bulk the image has no use for, and ``.planning`` in
# particular carries the phase audit artifacts. The legacy filename stays on
# this list although saneless no longer reads it: a file left behind under the
# old name still holds whatever token its owner put there, so it must never
# reach the daemon either.
FORBIDDEN_CONTEXT_PATHS = (
    ".env",
    ".git",
    ".planning",
    "config",
    LEGACY_CONFIG_NAME,
    CONFIG_NAME,
    "tests",
)

# Every entry the image build reads. ``uv.lock`` is a contract, not a
# convenience: the builder stage runs ``uv sync --locked``, which fails outright
# when the lock is absent from the context.
REQUIRED_CONTEXT_PATHS = (
    "!src/",
    "!pyproject.toml",
    "!uv.lock",
    "!README.md",
    "!LICENSE",
)


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
    ``!`` line below re-includes one path the image build needs. That only
    holds while ``*`` comes first. Move it down, or drop it, and every ``!``
    line below it becomes decoration while the working tree -- including a real
    ``saneless.toml`` with a live paperless-ngx token -- rides into a build layer
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


def test_dockerignore_reincludes_everything_the_build_needs() -> None:
    """
    The allow-list re-includes every input the image build actually reads.

    None of these is a convenience. The builder stage runs
    ``uv sync --locked``, which refuses to run without ``uv.lock``.
    ``pyproject.toml`` declares ``readme = "README.md"`` and the PEP 639
    ``license-files = ["LICENSE"]``; excluding either file makes the project
    build **fail**, not merely produce a thinner package. An over-tightened
    allow-list therefore breaks the image build rather than degrading it
    quietly, which is the better failure -- but only if it is caught here
    first.
    """
    lines = {line for _, line in _significant_lines(DOCKERIGNORE)}
    name = DOCKERIGNORE.relative_to(REPO_ROOT)
    missing = [entry for entry in REQUIRED_CONTEXT_PATHS if entry not in lines]
    assert not missing, (
        f"{name} does not re-include {missing}. None of them is optional: "
        "`uv sync --locked` fails without uv.lock, and pyproject.toml's readme "
        "and license-files keys each make the project build fail when the "
        "named file is absent from the context"
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

BUILD_INPUTS = ("pyproject.toml", "uv.lock", "README.md", "LICENSE")


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


def test_dockerfile_copies_only_the_build_inputs() -> None:
    """
    The builder stage copies named paths, never the whole working tree (D-29).

    ``uv sync --locked`` reads exactly these: the project metadata, the lock it
    verifies every artifact against, the two files the metadata names, and the
    package sources.

    This is the second of the two independent build-context gates, the first
    being the ``.dockerignore`` allow-list. Copying the entire context sweeps
    whatever the daemon was sent into a layer -- which, before this phase,
    included a real ``saneless.toml`` holding a live paperless-ngx token. Naming
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
    missing = [entry for entry in BUILD_INPUTS if entry not in copied]
    assert not missing, (
        f"{name} has no COPY line naming {missing}; `uv sync --locked` in the "
        "builder stage cannot build the project without them"
    )
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


def test_dockerfile_makes_the_data_dir_owner_only_before_declaring_the_volume() -> None:
    """
    The data directory is ``chmod 700`` in the image, **before** ``VOLUME``.

    A fresh named or anonymous volume takes the mode of the image's directory
    at that path, just as it takes the owner, so this line is what makes a new
    deployment's job database and preserved scans unreadable to other local
    users from the first start. saneless itself makes ``data_dir`` owner-only
    only when it has to create it, and in the image the directory always
    exists. An existing volume keeps its mode; the upgrade notes give the
    command for that. The order matters for the same reason as the ``chown``.
    """
    name = DOCKERFILE.relative_to(REPO_ROOT)
    lines = _significant_lines(DOCKERFILE)
    chmods = [
        number
        for number, line in lines
        if f"chmod 700 {DATA_DIR}" in line or f"chmod 0700 {DATA_DIR}" in line
    ]
    volumes = [number for number, line in lines if line.upper().startswith("VOLUME")]
    assert chmods, f"{name} never makes {DATA_DIR} owner-only with chmod 700"
    assert volumes, f"{name} has no VOLUME instruction"
    assert max(chmods) < min(volumes), (
        f"{name} sets the mode of {DATA_DIR} at line {max(chmods)}, after the "
        f"VOLUME instruction at line {min(volumes)}, where a builder may discard it"
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


# The stage that compiles, the finished environment it hands over, and the
# one command that both hash-checks and installs every dependency.
BUILDER_STAGE = "builder"
VENV_DIR = "/opt/venv"
LOCKED_SYNC = "uv sync --locked"

# Build tooling the runtime stage must never carry. ``/uv`` is the binary's
# path on a copy line; ``uv sync`` is the command itself.
RUNTIME_BUILD_TOOLS = ("gcc", "libc6-dev", "libsane-dev", "/uv", "uv sync")

# Tokens that must appear nowhere in the file's instructions. The HTTP client
# existed only for the healthcheck, and the data-dir variable overrode an
# operator's own ``[output] data_dir``.
FORBIDDEN_DOCKERFILE_TOKENS = ("curl", "SANELESS_OUTPUT__DATA_DIR")

# The SANE backends the image enables, in the order dll.conf lists them.
IMAGE_SANE_BACKENDS = ["net", "escl"]
DLL_CONF_WRITE = re.compile(r"printf\s+'(?P<body>[^']*)'\s*>\s*/etc/sane\.d/dll\.conf")

# What the runtime stage must do, each paired with the words an offender
# message uses when it does not.
RUNTIME_REQUIREMENTS = (
    (
        re.compile(rf"^COPY\s+--from={BUILDER_STAGE}\s+{VENV_DIR}\s"),
        f"copy the finished {VENV_DIR} from the {BUILDER_STAGE} stage",
    ),
    (
        re.compile(r"^ENV\b.*\bXDG_STATE_HOME=/var/lib(?:\s|$)"),
        "set `ENV XDG_STATE_HOME=/var/lib`",
    ),
    (re.compile(r"^RUN\b.*\bpip uninstall\b"), "uninstall pip"),
    (
        re.compile(r"^RUN\b.*\brm\b.*\s/etc/sane\.d/dll\.d/"),
        "empty /etc/sane.d/dll.d/, where a package could re-add backends",
    ),
    (
        re.compile(r'^HEALTHCHECK\b.*\s--start-period=\S+.*\sCMD\s+\["python",'),
        "run an exec-form `python` HEALTHCHECK with --start-period",
    ),
)


def _dockerfile_stages(
    lines: list[tuple[int, str]],
) -> list[tuple[str | None, list[tuple[int, str]]]]:
    """
    Split Dockerfile lines into stages of whole instructions.

    Continuation lines are folded into the instruction they continue, so a
    ``RUN`` or ``HEALTHCHECK`` wrapped with trailing backslashes is read as
    one piece of text, numbered by its first line.

    Args:
        lines: ``(line number, stripped line)`` pairs with comments removed,
            as ``_significant_lines`` returns them.

    Returns:
        One ``(stage name, instructions)`` pair per ``FROM``, in file order.
        The name is ``None`` for a stage with no ``AS``.

    """
    instructions: list[tuple[int, str]] = []
    for number, line in lines:
        if instructions and instructions[-1][1].endswith("\\"):
            start, text = instructions[-1]
            instructions[-1] = (start, f"{text} {line}")
        else:
            instructions.append((number, line))

    stages: list[tuple[str | None, list[tuple[int, str]]]] = []
    for number, text in instructions:
        if FROM_LINE.match(text) is not None:
            named = FROM_STAGE.match(text)
            stages.append((named.group("stage") if named else None, []))
        if stages:
            stages[-1][1].append((number, text))
    return stages


def _runtime_stage_offenders(runtime: list[tuple[int, str]]) -> list[str]:
    """
    Return every way the final stage falls short of a curated runtime.

    Args:
        runtime: The final stage's instructions, as ``_dockerfile_stages``
            returns them.

    Returns:
        One message per missing requirement or leaked build tool.

    """
    offenders = [
        f"the runtime stage does not {what}"
        for pattern, what in RUNTIME_REQUIREMENTS
        if not any(pattern.search(text) for _, text in runtime)
    ]
    offenders.extend(
        f"line {number}: the runtime stage carries build tooling {tool!r}: {text}"
        for number, text in runtime
        for tool in RUNTIME_BUILD_TOOLS
        if tool in text
    )
    writes = [
        (number, match.group("body"))
        for number, text in runtime
        for match in [DLL_CONF_WRITE.search(text)]
        if match is not None
    ]
    if not writes:
        offenders.append("the runtime stage never writes /etc/sane.d/dll.conf")
    for number, body in writes:
        backends = [entry.strip() for entry in body.split("\\n") if entry.strip()]
        if backends != IMAGE_SANE_BACKENDS:
            offenders.append(
                f"line {number}: dll.conf enables {backends}, not exactly "
                f"{IMAGE_SANE_BACKENDS}"
            )
    return offenders


def _builder_stage_offenders(builder: list[tuple[int, str]]) -> list[str]:
    """
    Return every way the builder stage falls short of a locked, finished venv.

    Args:
        builder: The builder stage's instructions.

    Returns:
        One message per missing or malformed ``uv sync --locked``.

    """
    syncs = [
        (number, text)
        for number, text in builder
        if text.startswith("RUN") and LOCKED_SYNC in text
    ]
    offenders: list[str] = []
    if len(syncs) < 2:
        offenders.append(
            f"the {BUILDER_STAGE} stage runs `{LOCKED_SYNC}` {len(syncs)} time(s), "
            "not twice (build backend first, then the project)"
        )
    if not any("--group build" in text for _, text in syncs):
        offenders.append(
            f"no `{LOCKED_SYNC}` in the {BUILDER_STAGE} stage installs "
            "`--group build`, so python-sane has no hash-checked backend to "
            "build against"
        )
    offenders.extend(
        f"line {number}: `{LOCKED_SYNC}` without --no-editable leaves the venv "
        f"pointing back into the {BUILDER_STAGE} stage's source tree: {text}"
        for number, text in syncs
        if "--no-editable" not in text
    )
    # Without it, uv adds the default groups to whatever the command names:
    # the whole dev toolchain, and the build group, would ship in /opt/venv.
    offenders.extend(
        f"line {number}: `{LOCKED_SYNC}` without --no-default-groups installs "
        f"the default dependency groups into the shipped venv: {text}"
        for number, text in syncs
        if "--no-default-groups" not in text
    )
    return offenders


def _dockerfile_runtime_offenders(lines: list[tuple[int, str]]) -> list[str]:
    """
    Return every departure from a builder-built venv and a curated runtime.

    Args:
        lines: ``(line number, stripped line)`` pairs with comments removed.

    Returns:
        One message per offence; empty when the file holds every invariant.

    """
    stages = _dockerfile_stages(lines)
    offenders = [
        f"line {number}: {token!r} is back in the image: {text}"
        for _, stage in stages
        for number, text in stage
        for token in FORBIDDEN_DOCKERFILE_TOKENS
        if token in text
    ]
    builders = [stage for name, stage in stages if name == BUILDER_STAGE]
    if not builders:
        offenders.append(f"there is no `FROM ... AS {BUILDER_STAGE}` stage")
    if len(stages) < 2 or stages[-1][0] is not None:
        offenders.append("there is no unnamed final runtime stage")
        return offenders
    for builder in builders:
        offenders.extend(_builder_stage_offenders(builder))
    offenders.extend(_runtime_stage_offenders(stages[-1][1]))
    return offenders


def test_dockerfile_runtime_stage_receives_a_finished_locked_venv() -> None:
    """
    The runtime image is a finished venv on a curated base, and nothing more.

    Every dependency byte is hash-checked by ``uv sync --locked`` in the
    builder stage, python-sane's build backend included, and compilation
    happens only there. The runtime stage copies the finished ``/opt/venv``
    and carries no compiler, headers, uv, pip or HTTP client that a
    ``docker exec`` could turn into an installer. It enables exactly the
    ``net`` and ``escl`` SANE backends, because the stock list probes about
    eighty and the first device enumeration paid for every one of them. It
    sets ``XDG_STATE_HOME`` rather than the data-dir variable, so a mounted
    ``saneless.toml``'s ``[output] data_dir`` takes effect, and its
    healthcheck is a Python probe with a start period.

    This reads the file. That the built image holds these properties is
    proven separately, by building and running it.
    """
    name = DOCKERFILE.relative_to(REPO_ROOT)
    lines = _significant_lines(DOCKERFILE)
    assert lines, f"{name} has no meaningful lines at all"
    offenders = _dockerfile_runtime_offenders(lines)
    assert not offenders, (
        f"{name} departs from the builder-built venv and curated runtime:\n"
        + "\n".join(offenders)
    )


# The target shape in miniature, so each seeded break below is one edit away
# from a file the guard accepts.
_SEEDED_DOCKERFILE = """\
FROM uv-image AS uv
FROM base AS builder
RUN apt-get update && apt-get install -y gcc libc6-dev libsane-dev
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-default-groups --group build --no-install-project --no-editable
COPY src ./src
RUN uv sync --locked --no-default-groups --no-editable
FROM base
RUN apt-get install -y libsane1 \\
    && printf 'net\\nescl\\n' > /etc/sane.d/dll.conf \\
    && rm -rf /etc/sane.d/dll.d/* \\
    && python -m pip uninstall -y pip
COPY --from=builder /opt/venv /opt/venv
ENV XDG_STATE_HOME=/var/lib
HEALTHCHECK --interval=30s --start-period=30s \\
    CMD ["python", "-c", "probe"]
"""


def test_the_runtime_guard_accepts_the_target_shape(tmp_path: Path) -> None:
    """The runtime-invariant guard passes a file holding every invariant."""
    seeded = tmp_path / "Dockerfile"
    seeded.write_text(_SEEDED_DOCKERFILE, encoding="utf-8")
    assert _dockerfile_runtime_offenders(_significant_lines(seeded)) == []


@pytest.mark.parametrize(
    ("before", "after", "named"),
    [
        (
            "ENV XDG_STATE_HOME=/var/lib",
            "ENV SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless",
            "XDG_STATE_HOME",
        ),
        (
            "ENV XDG_STATE_HOME=/var/lib",
            "ENV SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless",
            "SANELESS_OUTPUT__DATA_DIR",
        ),
        (
            'CMD ["python", "-c", "probe"]',
            "CMD curl -f http://localhost:8080/health || exit 1",
            "curl",
        ),
        ("--interval=30s --start-period=30s", "--interval=30s", "--start-period"),
        ("'net\\nescl\\n'", "'net\\nescl\\nepson2\\n'", "dll.conf"),
        ("install -y libsane1", "install -y libsane1 gcc", "gcc"),
        ("&& rm -rf /etc/sane.d/dll.d/* ", "", "dll.d"),
        ("&& python -m pip uninstall -y pip", "&& true", "pip"),
        (
            "--from=builder /opt/venv /opt/venv",
            "--from=builder /dist /dist",
            "/opt/venv",
        ),
        ("--group build --no-install-project", "--no-install-project", "--group build"),
        ("--no-default-groups --no-editable", "--no-default-groups", "--no-editable"),
        (
            "--locked --no-default-groups --group build",
            "--locked --group build",
            "--no-default-groups",
        ),
        (
            "--locked --no-default-groups --no-editable",
            "--locked --no-editable",
            "--no-default-groups",
        ),
    ],
)
def test_the_runtime_guard_reports_a_seeded_break(
    tmp_path: Path, before: str, after: str, named: str
) -> None:
    """Each broken invariant yields an offender naming what broke."""
    assert before in _SEEDED_DOCKERFILE, f"the seed no longer contains {before!r}"
    seeded = tmp_path / "Dockerfile"
    seeded.write_text(_SEEDED_DOCKERFILE.replace(before, after), encoding="utf-8")
    offenders = _dockerfile_runtime_offenders(_significant_lines(seeded))
    assert any(named in offender for offender in offenders), offenders


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
    the example into a mounted ``saneless.toml`` moved the server off the port
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
    the saneless container. A container reaches a scanner over the network --
    ``saned`` over SANE's own protocol, including a ``saned`` on its own host,
    or an eSCL device directly -- and never over the USB bus.

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

# An ``empty_page_coverage_threshold`` assignment inside a TOML example.
EMPTY_PAGE_THRESHOLD = re.compile(
    r"^empty_page_coverage_threshold = (?P<value>[0-9.]+)$",
    re.MULTILINE,
)

# The phrasings that get the coverage rule backwards. ``pages.is_blank`` is
# ``coverage <= threshold`` (on light paper), so raising the threshold admits
# MORE pages to the blank set, never fewer.
INVERTED_TUNING_PHRASES = (
    "raise the threshold to keep",
    "raising it keeps",
    "lower the threshold to remove",
)

# The explanation page the blank-page rule is described on.
EMPTY_PAGE_EXPLANATION = DOCS_DIR / "explanation" / "empty-page-detection.md"

# The headings the explanation must keep: other pages link to their anchors,
# and each is one of the things the page exists to say.
EMPTY_PAGE_HEADINGS = (
    "## Tuning the threshold",
    "## Removed pages",
    "## When every page is blank",
)

# The profile keys the coverage rule replaced, with no alias: a config that
# still sets either fails to load, so no page may still tell a reader to.
REMOVED_THRESHOLD_KEYS = ("empty_page_mean_threshold", "empty_page_stddev_threshold")

# The per-page log line the explanation quotes, rebuilt from these facts by
# the real filter, so the quoted line cannot drift from what the log says.
EXAMPLE_LOG_POSITION = 3
EXAMPLE_LOG_PAGES = 12
EXAMPLE_LOG_COVERAGE = 0.00034
EXAMPLE_LOG_PAPER_WHITE = 249

# The bullet that lists the words the history table's Status column shows.
HISTORY_STATUS_WORDS = re.compile(r"Current state of the job \((?P<words>[^)]*)\)")

# The trailing hedge in that bullet, which is prose rather than a label.
HISTORY_STATUS_HEDGE = "and so on"


def test_profile_howto_tuning_advice_matches_the_empty_page_rule() -> None:
    """
    The how-to's threshold example moves the threshold the way that keeps pages.

    ``pages.is_blank`` removes a page whose coverage is at or below the
    threshold.  Keeping a faint page therefore means **lowering** the
    threshold; raising it does the opposite of what the page promises, which
    is the inversion the review caught as row 10 under the old rule.  The
    direction is asserted three ways: the prose says to lower it, the example
    sets a value below ``ProfileConfig``'s own default (so a default change
    cannot leave a stale literal passing), and the real rule keeps a page
    measured between the two at the example's value while removing it at
    the default.
    """
    text, name = _read(PROFILE_HOWTO)
    body = _section(text, PROFILE_TUNING_HEADING, name)
    lowered = body.lower()
    for phrase in INVERTED_TUNING_PHRASES:
        assert phrase not in lowered, (
            f"{name}'s tuning advice says {phrase!r}. Raising the coverage "
            "threshold makes MORE pages count as empty, so this tells the "
            "reader to do the opposite of what the sentence promises (row 10)"
        )
    assert "lower the threshold" in lowered, (
        f"{name} does not tell the reader to lower the threshold to keep "
        "faint pages, which is the direction is_blank actually rewards"
    )
    assert "raising it removes more pages" in lowered, (
        f"{name} does not say that raising the threshold removes more pages"
    )

    default = ProfileConfig(source="Flatbed").empty_page_coverage_threshold
    found = [
        float(match.group("value")) for match in EMPTY_PAGE_THRESHOLD.finditer(body)
    ]
    assert len(found) == 1, (
        f"{name}'s tuning section should set empty_page_coverage_threshold "
        f"once in its example; found {found}"
    )
    (example,) = found
    assert example < default, (
        f"{name}'s example sets empty_page_coverage_threshold to {example}, "
        f"at or above the default {default}. That removes more pages, not fewer"
    )
    between = (example + default) / 2
    assert is_blank(between, PAPER_WHITE_FLOOR, coverage_threshold=default)
    assert not is_blank(between, PAPER_WHITE_FLOOR, coverage_threshold=example)


def _example_log_line(caplog: pytest.LogCaptureFixture) -> str:
    """
    Run the real blank-page filter over made-up records and return one log line.

    Args:
        caplog: pytest's log capture.

    Returns:
        The line the filter logged for the page the explanation quotes.

    """
    threshold = ProfileConfig().empty_page_coverage_threshold
    records = [
        PageRecord(
            sequence=position,
            path=Path(f"a-{position:04d}.png"),
            size=(2480, 3508),
            mode="L",
            dpi=300,
            ink_coverage=(
                EXAMPLE_LOG_COVERAGE if position == EXAMPLE_LOG_POSITION else 1.0
            ),
            paper_white=EXAMPLE_LOG_PAPER_WHITE,
        )
        for position in range(1, EXAMPLE_LOG_PAGES + 1)
    ]
    caplog.set_level(logging.INFO, logger="saneless.pages")
    filter_blank_pages(records, coverage_threshold=threshold)
    lines = [
        entry.getMessage()
        for entry in caplog.records
        if entry.name == "saneless.pages"
        and f"page {EXAMPLE_LOG_POSITION} of " in entry.getMessage()
    ]
    assert len(lines) == 1, lines
    return lines[0]


def test_empty_page_explanation_matches_the_detector(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    The explanation states the rule, the knob and the outcomes the code has.

    Every number the page gives about the rule is derived from the code here:
    the key and its default from ``ProfileConfig``, the margin, the
    paper-white percentile, the ink margin and the paper floor from
    ``saneless.pages``, and the quoted log line from the real filter.  A change
    to any of them fails this test until the page is rewritten to match.
    """
    text, name = _read(EMPTY_PAGE_EXPLANATION)
    default = ProfileConfig().empty_page_coverage_threshold

    assert "`empty_page_coverage_threshold`" in text, name
    assert f"`{default!r}`" in text, (
        f"{name} does not give the default threshold {default!r}"
    )
    assert f"{round(EDGE_TRIM * 100)}%" in text, (
        f"{name} does not give the {EDGE_TRIM:.0%} margin"
    )
    assert f"brightest {round((1 - PAPER_PERCENTILE) * 100)}%" in text, name
    assert f"more than {INK_DELTA} levels darker" in text, name
    assert f"at least {PAPER_WHITE_FLOOR}" in text, name
    for heading in EMPTY_PAGE_HEADINGS:
        assert re.search(rf"^{re.escape(heading)}$", text, re.MULTILINE), (
            f"{name} has no {heading!r} section"
        )
    assert "exits 8" in text, f"{name} does not say the all-blank scan exits 8"
    for key in REMOVED_THRESHOLD_KEYS:
        assert key not in text, f"{name} still names the removed key {key}"
    assert _example_log_line(caplog) in text, (
        f"{name}'s example log line is not the line the filter writes"
    )


def test_no_doc_page_names_a_removed_threshold_key() -> None:
    """
    No page tells a reader to set a key that now fails the config load.

    The mean and stddev thresholds were removed with no alias, so a profile
    that still sets either is refused by name.  A page still showing one
    would hand the reader a config that cannot load.
    """
    offenders = [
        f"{page.relative_to(REPO_ROOT)}: {key}"
        for page in _doc_pages()
        for key in REMOVED_THRESHOLD_KEYS
        if key in page.read_text(encoding="utf-8")
    ]
    assert not offenders, "\n".join(offenders)


def test_no_doc_page_describes_the_removed_blank_page_rule() -> None:
    """
    No page still calls blank-page detection the old two-threshold rule.

    The mean and standard-deviation rule was replaced by ink coverage, so a
    page describing detection by its old name sends the reader looking for
    two thresholds that no longer exist.
    """
    offenders = [
        str(page.relative_to(REPO_ROOT))
        for page in _doc_pages()
        if "dual-threshold" in page.read_text(encoding="utf-8").lower()
    ]
    assert not offenders, "\n".join(offenders)


def test_first_web_ui_scan_names_real_history_labels() -> None:
    """
    Every word the walkthrough gives for the history Status column is a real label.

    The history table renders ``job_label(job.state, job.warning)``; the status
    area above it spells the same terminal state differently -- ``DONE`` is "Complete" in
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
    real = {
        job_label(state, warning)
        for state in JobState
        for warning in (None, "a warning")
    }
    unreal = [word for word in listed if word not in real]
    assert not unreal, (
        f"{name} says the history table shows {unreal}, but job_label never "
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


# ---------------------------------------------------------------------------
# The runtime-evaluated route annotations, asked of ruff rather than read out
# of the config file
# ---------------------------------------------------------------------------

_TC002_TARGET = "src/saneless/web/routes.py"
# Generous, because the assertion is about the answer and not the latency. The
# measured run is well under a second: it invokes the ruff executable in this
# environment directly, so there is no dependency resolution step in front of
# it.
_TC002_SECONDS = 120


def test_ruff_still_exempts_the_runtime_evaluated_route_annotations() -> None:
    """
    Ruff leaves the runtime ``Response`` import in routes.py where it is.

    FastAPI resolves a route's return annotation when the decorator runs, so a
    ``Response`` it cannot resolve becomes a response model that makes
    ``app.openapi()`` raise. ``runtime-evaluated-decorators`` in pyproject.toml
    is what stops ruff's TC002 moving that import into a typing-only block.

    The guard asks ruff rather than reading the config, so it also catches the
    failure mode a config-shape assertion is blind to: ruff resolves a
    decorator's receiver only through an assignment in the same module, so
    constructing the ``APIRouter`` somewhere else stops the exemption applying
    while the config still looks exactly right.
    """
    env = {
        **os.environ,
        "SANELESS_TEST_RUFF": str(Path(sys.executable).with_name("ruff")),
        "SANELESS_TEST_FILE": _TC002_TARGET,
    }

    # Every argv element is a literal and the per-run paths travel in the
    # environment, double-quoted so the shell never re-splits them -- the
    # shape the other child-process tests in this suite established. The two
    # obvious alternatives are both rejected by this project's own lint rules:
    # a bare command name relying on PATH trips ruff S607, and putting the
    # resolved executable path in argv[0] trips S603, because argv[0] stops
    # being a literal. Suppression is forbidden, so the shell indirection is
    # what is left. The repository path travels in ``cwd``, never as a -C
    # argument, for the reason the git helper above gives. The command string
    # stays inline rather than moving to a named constant: S603 only accepts
    # an argv whose elements are literals at the call site.
    result = subprocess.run(
        [
            "/bin/sh",
            "-c",
            'exec "$SANELESS_TEST_RUFF" check --no-fix --select TC002 "$SANELESS_TEST_FILE"',
        ],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=_TC002_SECONDS,
    )

    # The exit code is the whole contract. Nothing asserts on the child's
    # output, because tooling around it can write chatter to either stream
    # without that meaning ruff found anything.
    assert result.returncode == 0, (
        f"ruff now wants to move a runtime import out of {_TC002_TARGET}. "
        "FastAPI resolves each route's return annotation when the decorator "
        "runs, so a Response moved into a typing-only block becomes a "
        "response model FastAPI cannot resolve, and app.openapi() raises. "
        "Restore runtime-evaluated-decorators in pyproject.toml, and check "
        "the APIRouter is still constructed in the module that decorates "
        f"with it:\n{result.stdout}\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# The declared floors against the versions uv.lock resolves (D-09, D-10, D-11)
# ---------------------------------------------------------------------------

# This is the one guard in this file that parses rather than matching plain
# text, and the departure is deliberate. Everywhere else the contract is what
# an operator copies, so a parser would assert something no reader ever sees.
# ``uv.lock`` is the opposite: machine-generated TOML that nobody copies, and
# the failure that most needs catching here -- one declared name resolved into
# two ``[[package]]`` entries split by an environment marker -- is invisible to
# a line scanner, because both entries are well formed and neither is wrong on
# its own. ``tomllib`` is stdlib, so the parse costs no dependency and no
# subprocess.

# A declared requirement: a name, optional bracketed extras, then a single
# ``>=`` floor. The floor stops at a comma, a semicolon or whitespace, so a
# spec carrying an environment marker or a second bound does not match -- and a
# spec that does not match is a reported offender, never a silent skip.
_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[(?P<extras>[^\]]*)\])?"
    r">=(?P<floor>[^,;\s]+)$"
)

# The leading distribution name of any requirement string, whatever follows
# it. A ``[tool.uv]`` constraint carries an upper bound rather than a floor and
# so cannot use the pattern above, and the duplicate-declaration guard needs
# the name of every requirement, floor or not.
_CONSTRAINT_NAME = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)")

# The anyio ceiling, written once and spelled from the same pair, so loosening
# the declaration and loosening the comparison cannot drift apart.
_ANYIO_CEILING = (4, 15)
_ANYIO_CEILING_SPEC = f"<{_ANYIO_CEILING[0]}.{_ANYIO_CEILING[1]}"


def _canonical_name(name: str) -> str:
    """
    Return ``name`` in the canonical form both files can be compared on.

    ``pyproject.toml`` and ``uv.lock`` are each free to spell a distribution
    with either separator and either case, so neither side is authoritative
    about punctuation.

    Args:
        name: A distribution name as either file happens to spell it.

    Returns:
        The name lower-cased, with every run of ``-``, ``_`` and ``.``
        collapsed to a single ``-``.

    """
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked_versions() -> dict[str, list[str]]:
    """
    Return every ``[[package]]`` version in ``uv.lock``, keyed by canonical name.

    The value is a list and not a scalar on purpose: detecting a name that
    resolved to more than one entry is half of what the guard below is for,
    and a dict of scalars would silently keep whichever entry came last.

    Returns:
        Canonical distribution name -> every version the lock holds for it.

    """
    lock = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    versions: dict[str, list[str]] = {}
    for package in lock["package"]:
        versions.setdefault(_canonical_name(package["name"]), []).append(
            package["version"]
        )
    return versions


def _declared_requirements() -> list[tuple[str, str]]:
    """
    Return every declared requirement paired with the table it was declared in.

    Every dependency group is read, not a named one: a group added later
    carries floors into the lock just as ``dev`` does, and a guard that listed
    its groups by name would pass over a new one without a word.

    Returns:
        ``(where, spec)`` pairs covering ``[project].dependencies`` and then
        each ``[dependency-groups]`` table in file order, each labelled
        ``[dependency-groups].<group>`` and listed in its declared order.

    """
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = [
        ("[project].dependencies", spec)
        for spec in pyproject["project"]["dependencies"]
    ]
    groups = [
        (f"[dependency-groups].{group}", spec)
        for group, specs in pyproject["dependency-groups"].items()
        for spec in specs
    ]
    return project + groups


def test_every_declared_floor_equals_the_version_uv_lock_resolves() -> None:
    """
    Every declared ``>=`` floor equals the version ``uv.lock`` resolves for it.

    The container is no longer the surface at risk here -- its builder stage
    runs ``uv sync --locked`` -- but the published wheel's metadata
    carries these floors verbatim, so they are what a downstream installer
    resolves against. A floor left below the locked
    version therefore admits into a fresh install the very tree this project
    upgraded away from, and nothing else in the repository would notice. The
    rule is equality, not satisfaction, so the comparison is plain string
    equality over the lock's ``version`` field and needs no PEP 440 parsing: a
    post-release floor compares like any other string.

    The check runs in both directions: every declared name is resolved by the
    lock, and every floor equals what the lock resolved. One further
    requirement makes the second half meaningful -- a declared name must have
    exactly one ``[[package]]`` entry. A name absent from the lock fails, and a
    name split across two entries by an environment marker fails with both
    versions named, because which one a wheel install would land on is a
    decision and not something a guard may guess.

    Scope is ``[project].dependencies`` and every ``[dependency-groups]``
    table. ``[tool.uv].constraint-dependencies`` is deliberately outside that scope:
    it holds ceilings rather than floors, so there is no floor in it to compare
    for equality, and the pattern above would reject its one entry as
    uncomparable. That entry is covered instead by
    ``test_the_anyio_ceiling_is_declared_and_the_lock_obeys_it`` below. The
    exclusion is stated here so no reader has to infer it from a regex that
    happens not to match.
    """
    locked = _locked_versions()
    offenders: list[str] = []
    corrections: list[str] = []

    for where, spec in _declared_requirements():
        match = _REQUIREMENT.match(spec)
        if match is None:
            offenders.append(
                f'"{spec}" in {where} is not a simple ">=" floor; this guard '
                "cannot compare it"
            )
            continue

        name = match.group("name")
        extras = match.group("extras")
        floor = match.group("floor")
        entries = locked.get(_canonical_name(name), [])

        if not entries:
            offenders.append(
                f"{name} is declared in {where} but has no [[package]] entry "
                f"in {UV_LOCK.name}"
            )
            continue

        if len(entries) > 1:
            found = ", ".join(sorted(entries))
            offenders.append(
                f"{UV_LOCK.name} has {len(entries)} [[package]] entries for "
                f"{name} ({found}); a marker-split resolution needs a "
                "decision, not a guessed winner"
            )
            continue

        resolved = entries[0]
        if floor != resolved:
            offenders.append(
                f"{name} floors at {floor} in {where}, but {UV_LOCK.name} "
                f"resolves {resolved}"
            )
            spelled = f"{name}[{extras}]" if extras else name
            corrections.append(f"{spelled}>={resolved}")

    remedy = ""
    if corrections:
        remedy = (
            f"\n\nReplace these lines in {PYPROJECT.name}, extras included, in "
            "the same commit as the lock move that caused this:\n"
        ) + "\n".join(corrections)

    assert not offenders, (
        f"a declared floor and {UV_LOCK.name} disagree. The container runs "
        "`uv sync --locked` in its builder stage, but the published wheel's metadata "
        "carries these floors, so a floor below the locked version is the only "
        "thing standing between a downstream fresh install and the tree this "
        "project already upgraded away from:\n" + "\n".join(offenders) + remedy
    )


# python-sane publishes an sdist and no wheel, so installing it runs a build
# backend. These name the three links that make that backend come from the
# lock: isolation off for the package, the group holding the backend, and the
# backend itself.
SDIST_ONLY_PACKAGE = "python-sane"
BUILD_GROUP = "build"
BUILD_BACKEND = "setuptools"


def _build_backend_offenders(
    pyproject: dict[str, Any], lock: dict[str, Any]
) -> list[str]:
    """
    Return every broken link between python-sane and a hash-checked backend.

    ``uv.lock`` does not lock build dependencies, and a build constraint pins
    a version without checking a hash. The backend is therefore declared as a
    dependency group, installed from the lock like any other package, and
    python-sane is built against that installed copy with isolation off.

    Args:
        pyproject: ``pyproject.toml``, parsed.
        lock: ``uv.lock``, parsed.

    Returns:
        One message per missing link; empty when the chain is whole.

    """
    uv_settings = pyproject.get("tool", {}).get("uv", {})
    offenders: list[str] = []
    if SDIST_ONLY_PACKAGE not in uv_settings.get("no-build-isolation-package", []):
        offenders.append(
            f"[tool.uv].no-build-isolation-package does not list "
            f"{SDIST_ONLY_PACKAGE}, so uv builds it in an isolated environment "
            f"whose {BUILD_BACKEND} is resolved at build time and never "
            "hash-checked"
        )
    if BUILD_GROUP not in uv_settings.get("default-groups", []):
        offenders.append(
            f"[tool.uv].default-groups does not include {BUILD_GROUP!r}, so a "
            f"plain `uv sync` has no {BUILD_BACKEND} to build "
            f"{SDIST_ONLY_PACKAGE} against"
        )
    declared = pyproject.get("dependency-groups", {}).get(BUILD_GROUP, [])
    names = {
        _canonical_name(match.group("name"))
        for spec in declared
        if isinstance(spec, str)
        for match in [_CONSTRAINT_NAME.match(spec)]
        if match is not None
    }
    if BUILD_BACKEND not in names:
        offenders.append(
            f"[dependency-groups].{BUILD_GROUP} does not declare {BUILD_BACKEND}"
        )
    entries = [
        package
        for package in lock.get("package", [])
        if _canonical_name(package.get("name", "")) == BUILD_BACKEND
    ]
    if len(entries) != 1:
        offenders.append(
            f"uv.lock has {len(entries)} [[package]] entries for {BUILD_BACKEND}, "
            "not exactly one"
        )
    for entry in entries:
        sdist_hash = entry.get("sdist", {}).get("hash", "")
        wheel_hashes = [wheel.get("hash", "") for wheel in entry.get("wheels", [])]
        if not sdist_hash.startswith("sha256:"):
            offenders.append(f"uv.lock records no sdist sha256 for {BUILD_BACKEND}")
        if not wheel_hashes or not all(
            digest.startswith("sha256:") for digest in wheel_hashes
        ):
            offenders.append(
                f"uv.lock records no sha256 for every {BUILD_BACKEND} wheel"
            )
    return offenders


def test_python_sanes_build_backend_comes_from_the_lock_hash_checked() -> None:
    """
    python-sane's build backend comes from the lock, hash-checked.

    python-sane ships only an sdist, so every install compiles it, and the
    backend that compiles it is code run with the builder's privileges. Left
    to build isolation, uv resolves that backend fresh at build time from an
    open-ended requirement and checks no hash. The chain that closes the gap
    has four links -- isolation off for python-sane, a ``build`` group in the
    default groups, that group declaring setuptools, and a setuptools entry in
    ``uv.lock`` with sha256 digests -- and removing any one reopens it.
    """
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    lock = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    offenders = _build_backend_offenders(pyproject, lock)
    assert not offenders, (
        f"{SDIST_ONLY_PACKAGE}'s build backend is not locked and hash-checked:\n"
        + "\n".join(offenders)
    )


def _seeded_build_backend_chain() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a parsed pyproject and lock holding every link of the chain."""
    pyproject: dict[str, Any] = {
        "tool": {
            "uv": {
                "no-build-isolation-package": [SDIST_ONLY_PACKAGE],
                "default-groups": ["dev", BUILD_GROUP],
            }
        },
        "dependency-groups": {BUILD_GROUP: [f"{BUILD_BACKEND}>=84.0.0"]},
    }
    lock: dict[str, Any] = {
        "package": [
            {
                "name": BUILD_BACKEND,
                "version": "84.0.0",
                "sdist": {"hash": "sha256:" + "0" * 64},
                "wheels": [{"hash": "sha256:" + "1" * 64}],
            }
        ]
    }
    return pyproject, lock


def test_the_build_backend_guard_accepts_the_whole_chain() -> None:
    """The build-backend guard passes a chain with every link in place."""
    pyproject, lock = _seeded_build_backend_chain()
    assert _build_backend_offenders(pyproject, lock) == []


def test_the_build_backend_guard_reports_isolation_left_on() -> None:
    """Dropping python-sane from the no-isolation list is reported."""
    pyproject, lock = _seeded_build_backend_chain()
    pyproject["tool"]["uv"]["no-build-isolation-package"] = []
    offenders = _build_backend_offenders(pyproject, lock)
    assert any("no-build-isolation-package" in item for item in offenders), offenders


def test_the_build_backend_guard_reports_a_backend_without_hashes() -> None:
    """A setuptools lock entry stripped of its digests is reported twice."""
    pyproject, lock = _seeded_build_backend_chain()
    entry = lock["package"][0]
    entry["sdist"] = {}
    entry["wheels"] = [{"url": "https://example.invalid/setuptools.whl"}]
    offenders = _build_backend_offenders(pyproject, lock)
    assert any("sdist sha256" in item for item in offenders), offenders
    assert any("wheel" in item for item in offenders), offenders


def test_the_build_backend_guard_reports_a_missing_group() -> None:
    """A build group left out of the defaults, and left empty, is reported."""
    pyproject, lock = _seeded_build_backend_chain()
    pyproject["tool"]["uv"]["default-groups"] = ["dev"]
    pyproject["dependency-groups"][BUILD_GROUP] = []
    offenders = _build_backend_offenders(pyproject, lock)
    assert any("default-groups" in item for item in offenders), offenders
    assert any(f"does not declare {BUILD_BACKEND}" in item for item in offenders), (
        offenders
    )


def test_the_anyio_ceiling_is_declared_and_the_lock_obeys_it() -> None:
    """
    The ``anyio`` ceiling is still declared, and the locked anyio obeys it.

    anyio 4.15.0 turned ``anyio.abc.BlockingPortal`` into a deprecated alias,
    and starlette's ``testclient`` module evaluates that name at module scope.
    This project runs pytest under ``filterwarnings = ["error"]``, so the
    deprecation is raised rather than printed and every module that imports
    ``TestClient`` fails during collection -- seven of them here. Nothing in
    this repository can fix that, because the deprecated name is starlette's
    own and is reached before any saneless code runs. The ceiling should be
    removed only once starlette stops using the alias.

    anyio is a transitive this project never imports, so it is declared in
    neither dependency list and the floor guard above cannot see it: a ceiling
    is not a floor, and putting it in either list would export a workaround for
    an upstream bug into the published wheel's metadata. This guard is what
    covers it, and it fails if the declaration is deleted, if its bound is
    loosened, or if the lock stops obeying it.
    """
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    uv_table = pyproject.get("tool", {}).get("uv", {})
    constraints: list[str] = uv_table.get("constraint-dependencies", [])
    declared = [
        constraint
        for constraint in constraints
        if (match := _CONSTRAINT_NAME.match(constraint)) is not None
        and _canonical_name(match.group("name")) == "anyio"
    ]

    assert len(declared) == 1, (
        f"{PYPROJECT.name} declares {len(declared)} anyio entries in "
        "[tool.uv].constraint-dependencies; exactly one is expected. Without "
        "the ceiling, resolution is free to select anyio 4.15 or later, where "
        "anyio.abc.BlockingPortal became a deprecated alias that starlette's "
        "testclient module evaluates at module scope. Under "
        'filterwarnings = ["error"] that deprecation is an error, so every '
        "module importing TestClient fails to collect. Restore "
        f'"anyio{_ANYIO_CEILING_SPEC}" there, and remove it only once '
        "starlette stops using the alias"
    )
    ceiling = declared[0]
    assert _ANYIO_CEILING_SPEC in ceiling, (
        f"{PYPROJECT.name} constrains anyio as {ceiling!r}, which no longer "
        f"carries the {_ANYIO_CEILING_SPEC} upper bound. anyio 4.15.0 turned "
        "anyio.abc.BlockingPortal into a deprecated alias that starlette's "
        "testclient module evaluates at module scope, and this project raises "
        "deprecations as errors, so loosening the bound reopens seven "
        "collection failures. Loosen it only once starlette migrates"
    )

    versions = _locked_versions().get("anyio", [])
    assert len(versions) == 1, (
        f"{UV_LOCK.name} holds {len(versions)} [[package]] entries for anyio "
        f"({', '.join(sorted(versions))}); exactly one is expected, and the "
        "ceiling cannot be checked against a split resolution"
    )
    parts = versions[0].split(".")
    resolved = (int(parts[0]), int(parts[1]))
    assert resolved < _ANYIO_CEILING, (
        f"{UV_LOCK.name} resolves anyio {versions[0]}, which does not obey the "
        f"declared {_ANYIO_CEILING_SPEC} ceiling. anyio 4.15.0 turned "
        "anyio.abc.BlockingPortal into a deprecated alias, starlette's "
        "testclient module evaluates it at module scope, and this project "
        'runs under filterwarnings = ["error"], so every module importing '
        "TestClient fails during collection. Re-lock with the constraint in "
        "place; lift the ceiling only once starlette stops using the alias"
    )


def test_no_requirement_is_declared_twice() -> None:
    """
    No distribution is declared in more than one dependency table.

    A runtime dependency is already installed in every environment that
    installs the project, so declaring it again in a group adds nothing but
    a second floor. Two floors for one name drift independently: a bump that
    moves one leaves the other behind, and which of them a reader believes
    depends on which table they happened to open. Names are compared in
    canonical form, so a second spelling with a different separator, case or
    extras list is the same distribution and is caught.
    """
    requirements = _declared_requirements()
    assert requirements, (
        f"{PYPROJECT.name} declares no requirements in any table, so this "
        "guard has nothing to compare"
    )

    tables: dict[str, list[str]] = {}
    unnamed: list[str] = []
    for where, spec in requirements:
        match = _CONSTRAINT_NAME.match(spec)
        if match is None:
            unnamed.append(f'"{spec}" in {where}')
            continue
        tables.setdefault(_canonical_name(match.group("name")), []).append(where)

    assert not unnamed, (
        "these requirements do not start with a distribution name, so this "
        "guard cannot tell whether they repeat another:\n" + "\n".join(unnamed)
    )
    offenders = [
        f"{name}: {', '.join(where)}"
        for name, where in sorted(tables.items())
        if len(where) > 1
    ]
    assert not offenders, (
        f"{PYPROJECT.name} declares a distribution in more than one table. "
        "Each extra declaration is a second floor that moves independently of "
        "the first; keep the one in the table that needs it and delete the "
        "rest:\n" + "\n".join(offenders)
    )


def test_pyproject_anchors_ty_to_this_tree() -> None:
    """
    ``pyproject.toml`` carries a ``[tool.ty]`` table, even an empty one.

    ty ignores a ``pyproject.toml`` without that table and keeps searching
    parent directories for one that has it. A git worktree nested inside
    another checkout then anchors ty on the outer checkout, and ``ty check``
    passes over the outer tree's source while never reading this one. The
    table looks like dead configuration when empty, which is why it needs a
    guard rather than only a comment.
    """
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert "ty" in pyproject.get("tool", {}), (
        f"{PYPROJECT.name} has no [tool.ty] table. ty skips such a file and "
        "anchors on the nearest parent directory's pyproject.toml that has one, "
        "so in a worktree nested inside another checkout it type-checks the "
        "outer tree and passes. Restore the table, empty if nothing needs "
        "configuring"
    )


PYTHON_VERSION_FILE = REPO_ROOT / ".python-version"


def test_python_version_names_one_interpreter_the_project_supports() -> None:
    """
    ``.python-version`` names exactly one interpreter, and the project accepts it.

    uv, setup-uv and pyenv all read this file to choose an interpreter. A
    second line is not a fallback any of them agree on: uv takes the first,
    while pyenv puts every listed version on ``PATH``, so the file stops
    answering "which Python does this repository run on". A version outside
    ``requires-python`` is worse, because the environment it selects cannot
    install the project at all. The bound is read from ``requires-python``
    rather than written here, so raising the project's floor without updating
    this file fails the suite instead of passing against a stale copy.
    """
    name = PYTHON_VERSION_FILE.relative_to(REPO_ROOT)
    lines = [
        line.strip()
        for line in PYTHON_VERSION_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 1, (
        f"{name} lists {len(lines)} interpreter versions ({', '.join(lines)}); "
        "exactly one is expected. uv uses the first line and pyenv exposes "
        "every line, so more than one leaves the tools disagreeing about "
        "which Python this repository runs on"
    )

    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    supported = SpecifierSet(pyproject["project"]["requires-python"])
    assert Version(lines[0]) in supported, (
        f"{name} selects Python {lines[0]}, which {PYPROJECT.name}'s "
        f'requires-python = "{supported}" rejects, so the interpreter it '
        "picks cannot install the project"
    )


# ---------------------------------------------------------------------------
# Phase 36: pinned artifacts and advisory coverage (PIN-02, PIN-05, SEC-01,
# SEC-03)
# ---------------------------------------------------------------------------

WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
DEPENDABOT_CONFIG = REPO_ROOT / ".github" / "dependabot.yml"

# The build backend's name, as ``[build-system].requires`` spells it.
UV_BUILD_NAME = "uv_build"

# A step reference to the action that installs uv on a runner, as the step's
# first key (``- uses:``) or as a later one (``uses:`` below ``- name:``).
SETUP_UV_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*astral-sh/setup-uv@")
# The list item that opens a step, and the column its keys sit at.
STEP_ITEM = re.compile(r"^(?P<dash>\s*)-\s+")
# The ``with:`` inputs that override the uv version setup-uv would otherwise
# read from ``pyproject.toml``.
SETUP_UV_VERSION_INPUTS = frozenset({"version", "version-file"})
# The setup-uv steps the workflows carry today. Fewer found means the step
# scanner stopped recognising them, not that the pins went away.
MINIMUM_SETUP_UV_STEPS = 7
# The build-backend requirement as the Dockerfile's uv-stage comment quotes it.
QUOTED_REQUIRES = re.compile(r'requires = \["(?P<spec>uv_build[^"]+)"\]')
# A build-backend requirement split into its floor and its ceiling.
UV_BUILD_RANGE = re.compile(r"^uv_build>=(?P<floor>[^,]+),<(?P<ceiling>\S+)$")


def _workflow_files() -> list[Path]:
    """
    Return every GitHub Actions workflow file, sorted by path.

    This is the suite's first reader of ``.github/workflows``. Several guards
    need the same input set, and sorting is what keeps their failure messages
    in a stable order rather than whatever order the filesystem returns.

    Both spellings are collected because GitHub loads both. Every workflow
    here happens to be ``.yml`` today, so globbing one extension would pass
    and keep passing -- right up until someone adds a ``.yaml`` file, which
    would then carry an unpinned ``uses:`` or an ``--ignore`` flag past every
    guard that reads this list. The guards are supply-chain gates, so their
    input set has to be what GitHub runs, not what the repository happens to
    contain.

    Returns:
        The workflow files under ``.github/workflows``, in path order.

    """
    return sorted(
        path for suffix in ("*.yml", "*.yaml") for path in WORKFLOW_DIR.glob(suffix)
    )


def _dockerfile_uv_version() -> str:
    """
    Return the uv version the Dockerfile's tool stage pins, read off its tag.

    One side of every comparison below is derived rather than written down a
    second time, so no guard here can end up agreeing with a copy of itself.

    Returns:
        The tag half of the ``ghcr.io/astral-sh/uv`` reference.

    """
    name = DOCKERFILE.relative_to(REPO_ROOT)
    tags = [
        match.group("ref").partition("@")[0].removeprefix(f"{UV_IMAGE}:")
        for _number, line in _significant_lines(DOCKERFILE)
        for match in [FROM_LINE.match(line)]
        if match is not None and match.group("ref").startswith(f"{UV_IMAGE}:")
    ]
    assert len(tags) == 1, (
        f"{name} names {UV_IMAGE} on {len(tags)} FROM lines; exactly one is "
        "expected, and the canonical uv version cannot be derived from any "
        "other number of them"
    )
    return tags[0]


def _declared_uv_build_specifier() -> str:
    """
    Return ``pyproject.toml``'s build-backend requirement, verbatim.

    Returns:
        The single ``[build-system].requires`` entry naming the backend.

    """
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    specifiers = [
        spec
        for spec in pyproject["build-system"]["requires"]
        if spec.startswith(UV_BUILD_NAME)
    ]
    assert len(specifiers) == 1, (
        f"{PYPROJECT.name} declares {len(specifiers)} [build-system].requires "
        f"entries for {UV_BUILD_NAME}; exactly one is expected"
    )
    return specifiers[0]


def test_every_repeated_image_tag_in_the_dockerfile_shares_one_digest() -> None:
    """
    A tag on more than one ``FROM`` line carries the same digest at each (D-07).

    ``python:3.14-slim`` is the base of both the builder and the runtime stage.
    The defect this catches is not a missing pin -- the shape guard further up
    already refuses those -- but a re-resolution that updated one line and
    missed the other. That builds the application against different bytes than
    it ships on, and nothing about it looks wrong in review.
    """
    name = DOCKERFILE.relative_to(REPO_ROOT)
    references = [
        (number, match.group("ref"))
        for number, line in _significant_lines(DOCKERFILE)
        for match in [FROM_LINE.match(line)]
        if match is not None
    ]
    assert references, f"{name} has no FROM instructions at all"

    by_tag: dict[str, list[tuple[int, str]]] = {}
    for number, reference in references:
        image, _, digest = reference.partition("@")
        by_tag.setdefault(image, []).append((number, digest))

    repeated = {image: seen for image, seen in by_tag.items() if len(seen) > 1}
    assert repeated, (
        f"{name} no longer uses any image tag on more than one FROM line, so "
        "this guard has nothing left to compare. It was written for the two "
        "python:3.14-slim stages; if the file really has collapsed to one "
        "stage per tag, delete the guard as a decision rather than leaving it "
        "passing over an empty comparison"
    )

    offenders = [
        f"{name}:{number}: {image}@{digest}"
        for image, seen in sorted(repeated.items())
        if len({digest for _number, digest in seen}) > 1
        for number, digest in seen
    ]
    assert not offenders, (
        "one image tag is pinned to two different digests. A re-resolution "
        "updated one FROM line and missed the other, so the stages below no "
        "longer start from the same bytes:\n" + "\n".join(offenders)
    )


def _pinned_uv_offenders(required: SpecifierSet) -> list[str]:
    """
    Return every uv pin that cannot read ``required-version`` and escapes it.

    Args:
        required: ``[tool.uv] required-version``, parsed.

    Returns:
        One line per pin outside the range, naming the file and the pin.

    """
    ceilings = [spec.version for spec in required if spec.operator == "<"]
    assert len(ceilings) == 1, (
        f"{PYPROJECT.name} declares required-version = {str(required)!r}, "
        "which does not carry exactly one '<' upper bound. The ceiling is what "
        "keeps a new uv minor series from reaching the runners without review, "
        "and the build backend's ceiling is compared to it"
    )
    ceiling = Version(ceilings[0])
    offenders: list[str] = []

    tool_stage = _dockerfile_uv_version()
    if Version(tool_stage) not in required:
        offenders.append(
            f"{DOCKERFILE.relative_to(REPO_ROOT)}: the {UV_IMAGE} tool stage "
            f"pins {tool_stage}"
        )

    locked = _locked_versions().get("uv", [])
    if len(locked) != 1:
        offenders.append(
            f"{UV_LOCK.name}: holds {len(locked)} [[package]] entries for uv; "
            "exactly one is expected"
        )
    elif Version(locked[0]) not in required:
        offenders.append(f"{UV_LOCK.name}: resolves uv {locked[0]}")

    specifier = _declared_uv_build_specifier()
    match = UV_BUILD_RANGE.match(specifier)
    assert match is not None, (
        f"{PYPROJECT.name} declares the build backend as {specifier!r}, which "
        "is not the floor-and-ceiling shape this guard compares. Both bounds "
        "are load-bearing: the floor must lie inside required-version and the "
        "ceiling must equal its upper bound"
    )
    if Version(match.group("floor")) not in required:
        offenders.append(
            f"{PYPROJECT.name}: [build-system].requires floors {UV_BUILD_NAME} "
            f"at {match.group('floor')}"
        )
    if Version(match.group("ceiling")) != ceiling:
        offenders.append(
            f"{PYPROJECT.name}: [build-system].requires caps {UV_BUILD_NAME} at "
            f"{match.group('ceiling')}, but required-version caps uv at {ceiling}"
        )
    return offenders


def _key_column(line: str) -> int:
    """Return the column a line's key starts at, past any list-item dash."""
    return len(line) - len(line.lstrip(" -"))


def _enclosing_step(lines: list[tuple[int, str]], index: int) -> list[tuple[int, str]]:
    """
    Return the step holding ``lines[index]``, its list-item dash blanked.

    The step opens at the nearest list item, at or above the line, whose keys
    sit in the same column as the line's key, and runs until the next line at
    or left of that item's dash. The dash is replaced by a space so every key
    of the step, the first one included, sits at the same indent and can be
    read with ``_key_mapping``.

    Args:
        lines: Significant raw lines of one workflow, indentation kept.
        index: Position of a line inside a step.

    Returns:
        The step's lines, in order.

    """
    column = _key_column(lines[index][1])
    start = index
    while start > 0:
        item = STEP_ITEM.match(lines[start][1])
        if item is not None and _key_column(lines[start][1]) == column:
            break
        start -= 1
    number, opener = lines[start]
    dash = len(opener) - len(opener.lstrip(" "))
    step = [(number, f"{opener[:dash]} {opener[dash + 1 :]}")]
    for later_number, later_line in lines[start + 1 :]:
        if _indent(later_line) <= dash:
            break
        step.append((later_number, later_line))
    return step


def _setup_uv_version_overrides(
    paths: list[Path] | None = None,
) -> tuple[int, list[str]]:
    """
    Scan every setup-uv step for an input that overrides ``required-version``.

    A step is found whichever key comes first in it, and its ``with:`` inputs
    are read in both spellings the workflows use: a block of lines, or a flow
    mapping on the key's own line.

    Args:
        paths: Workflow files to scan; every workflow in the repository when
            omitted.

    Returns:
        How many setup-uv steps were found, and one line per ``version:`` or
        ``version-file:`` input, naming its file and the step's line.

    """
    sites = 0
    offenders: list[str] = []
    for path in _workflow_files() if paths is None else paths:
        name = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        lines = [
            (number, line)
            for number, line in _numbered(path)
            if line.strip() and not _is_comment(line)
        ]
        for index, (number, line) in enumerate(lines):
            if SETUP_UV_USES.match(line) is None:
                continue
            sites += 1
            step = _enclosing_step(lines, index)
            inputs = _key_mapping(step, "with", _key_column(line)) or {}
            offenders.extend(
                f"{name}:{number}: the setup-uv step overrides required-version "
                f"with {key}: {inputs[key]}"
                for key in sorted(SETUP_UV_VERSION_INPUTS & inputs.keys())
            )
    return sites, offenders


def test_every_uv_surface_sits_inside_the_required_version_range() -> None:
    """
    Every surface that pins uv lies inside ``pyproject.toml``'s declared range.

    ``[tool.uv] required-version`` is the one place the uv version is
    declared. setup-uv reads it on every runner when a step names no version
    of its own, and local uv refuses to run outside it, so a ``version:`` or
    ``version-file:`` input on a setup-uv step is a second source that
    silently wins on that runner. Those inputs are refused outright.

    Two surfaces cannot read the range and stay pinned: the Dockerfile's uv
    tool stage and the ``uv_build`` build-backend requirement. The lock pins a
    third, the ``uv`` the dev environment installs. Each is held inside the
    range, and the build backend's ceiling must equal the range's, so the
    backend cannot admit a uv series the rest of the toolchain refuses. Pins
    are compared to the range rather than to each other: two surfaces that
    each move to a newer patch release inside it are both correct, even when
    their updates land in separate commits.
    """
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    declared_range = pyproject.get("tool", {}).get("uv", {}).get("required-version")
    if declared_range is None:
        offenders = [
            f"{PYPROJECT.name}: [tool.uv] declares no required-version, so "
            "setup-uv has no version to read and local uv enforces none"
        ]
    else:
        offenders = _pinned_uv_offenders(SpecifierSet(declared_range))

    sites, overrides = _setup_uv_version_overrides()
    offenders.extend(overrides)

    assert sites >= MINIMUM_SETUP_UV_STEPS, (
        f"only {sites} astral-sh/setup-uv steps were found across the "
        f"workflows, fewer than the {MINIMUM_SETUP_UV_STEPS} they carry. "
        "Either the action was replaced, the step scanner stopped matching it, "
        f"or {WORKFLOW_DIR.relative_to(REPO_ROOT)} is no longer where the "
        "workflows live"
    )
    assert not offenders, (
        f"a uv surface escapes {PYPROJECT.name}'s [tool.uv] required-version. "
        "That range is the one declaration of the uv this project builds, "
        "tests and ships with; a pin outside it, or a setup-uv step that "
        "overrides it, is a toolchain that differs between the image, CI and "
        "local work:\n" + "\n".join(offenders)
    )


_SEEDED_SETUP_UV = """\
name: Seeded
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000 # v1.0.0
        with: {persist-credentials: false}
      - name: Set up uv, name first
        uses: astral-sh/setup-uv@0000000000000000000000000000000000000000 # v1.0.0
        with:
          version: "0.13.0"
      - uses: astral-sh/setup-uv@0000000000000000000000000000000000000000 # v1.0.0
        with: {enable-cache: false, version: "0.13.0"}
      - uses: astral-sh/setup-uv@0000000000000000000000000000000000000000 # v1.0.0
        with:
          version-file: uv.lock
      - with: {version: "0.13.0"}
        uses: astral-sh/setup-uv@0000000000000000000000000000000000000000 # v1.0.0
      - name: Set up uv, reading pyproject.toml
        # version: "0.13.0" in a comment is not an input
        uses: astral-sh/setup-uv@0000000000000000000000000000000000000000 # v1.0.0
        with:
          enable-cache: false
      - run: echo version: 0.13.0
"""


def test_the_setup_uv_scanner_sees_every_step_and_input_spelling(
    tmp_path: Path,
) -> None:
    """
    A version override is found whichever way the step or its inputs are written.

    A step may open with ``name:`` rather than ``uses:``, or even with
    ``with:``, and ``with:`` may be a flow mapping on one line. Each of those
    spellings already appears in these workflows, so a scanner that knew only
    ``- uses:`` followed by a block ``with:`` would let a later step override
    the one declared uv range without any guard noticing.
    """
    seeded = tmp_path / "seeded.yml"
    seeded.write_text(_SEEDED_SETUP_UV, encoding="utf-8")

    sites, offenders = _setup_uv_version_overrides([seeded])

    assert sites == 5, f"expected five setup-uv steps, found {sites}"
    assert [offender.split(": ", 1)[0] for offender in offenders] == [
        f"{seeded}:9",
        f"{seeded}:12",
        f"{seeded}:14",
        f"{seeded}:18",
    ], offenders
    assert "version-file: uv.lock" in offenders[2], offenders


def test_the_dockerfile_comment_quotes_the_declared_build_specifier() -> None:
    """
    The uv-stage comment quotes the specifier ``pyproject.toml`` declares (D-22).

    That comment explains why the uv tool image and the build backend are
    pinned together, and it argues the case by quoting the specifier. Quoted
    text goes stale silently: nothing about editing ``pyproject.toml`` touches
    the Dockerfile, so the comment would go on explaining a real coupling in
    terms of a range that no longer exists -- misstating a design decision
    rather than merely reading untidily. The raw lines are needed here because
    ``_significant_lines`` drops whole-line comments by design.
    """
    name = DOCKERFILE.relative_to(REPO_ROOT)
    quoted = [
        (number, match.group("spec"))
        for number, line in _numbered(DOCKERFILE)
        for match in [QUOTED_REQUIRES.search(line)]
        if match is not None
    ]
    assert quoted, (
        f"{name} no longer quotes a {UV_BUILD_NAME} requirement anywhere, so "
        "this guard has nothing to compare. That comment is what explains why "
        "the tool image and the build backend move together; if it went away "
        "deliberately, remove this guard in the same change"
    )
    declared = _declared_uv_build_specifier()
    offenders = [
        f"{name}:{number}: {spec}" for number, spec in quoted if spec != declared
    ]
    assert not offenders, (
        f"a Dockerfile comment quotes a {UV_BUILD_NAME} requirement that "
        f"{PYPROJECT.name} does not declare -- it now reads {declared!r}. The "
        "comment explains a deliberate coupling, so a stale specifier there "
        "misstates a design decision:\n" + "\n".join(offenders)
    )


# The start of an `updates:` entry, and the settling period each must carry.
DEPENDABOT_ENTRY = re.compile(r"^-\s+package-ecosystem:")
COOLDOWN_KEY = "cooldown:"
DEFAULT_DAYS = re.compile(r"^default-days:\s*(?P<days>\d+)$")
MINIMUM_COOLDOWN_DAYS = 7


def test_every_dependabot_entry_settles_for_a_week_before_opening_a_pr() -> None:
    """
    Every ``updates:`` entry carries a cooldown of at least a week (D-15).

    Measured against zizmor 1.30.1 on its default persona: the
    ``dependabot-cooldown`` check is ecosystem-gated. A cooldown-less ``pip``
    or ``github-actions`` entry is a finding, but a cooldown-less ``uv`` entry
    produces none and the audit exits 0 -- so for the ecosystem carrying this
    project's Python pins, the blocking CI step proves nothing. Without this
    guard the settling period the file's own header demands would be policy
    with nothing behind it, which is worse than an absent rule because it
    reads like an enforced one. The window between one entry and the next is
    what scopes the check, because ``_significant_lines`` has already stripped
    the indentation that would otherwise delimit it.
    """
    name = DEPENDABOT_CONFIG.relative_to(REPO_ROOT)
    lines = _significant_lines(DEPENDABOT_CONFIG)
    starts = [
        index
        for index, (_number, line) in enumerate(lines)
        if DEPENDABOT_ENTRY.match(line) is not None
    ]
    assert starts, (
        f"{name} declares no updates: entry at all, so this guard has nothing "
        "to check. Either the file stopped configuring Dependabot or the entry "
        "spelling changed under it"
    )

    offenders: list[str] = []
    for position, index in enumerate(starts):
        number, line = lines[index]
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        window = [entry_line for _entry_number, entry_line in lines[index:end]]
        days = [
            match.group("days")
            for entry_line in window
            for match in [DEFAULT_DAYS.match(entry_line)]
            if match is not None
        ]
        if COOLDOWN_KEY not in window or len(days) != 1:
            offenders.append(f"{name}:{number}: {line}")
        elif int(days[0]) < MINIMUM_COOLDOWN_DAYS:
            offenders.append(
                f"{name}:{number}: {line} -- settles for only {days[0]} days"
            )
    assert not offenders, (
        "a Dependabot entry can open a pull request before the release it "
        f"proposes has settled for {MINIMUM_COOLDOWN_DAYS} days. That window "
        "is the only thing between an upstream account compromised today and "
        "a merge-ready bump today, and zizmor does not enforce it for every "
        "ecosystem, so nothing else here would catch this:\n" + "\n".join(offenders)
    )


# The seventh banned suppression spelling, assembled from the same fragments
# as the six above for the same reason -- and kept apart from them because
# their guard scans Python files, which is where this one cannot look.
_FLAG_LEAD = "--"

# The rest are ways a workflow or hook step goes green over a failure without
# a checker being told anything: an environment variable that makes prek skip
# a hook by id, prek's own skip option, the step-level setting that marks a
# failed step as passed, a shell fallback that swallows the exit status, and
# ruff's option to report findings and exit 0. Plain literals are safe here,
# because no guard in this file scans Python source for them.
SUPPRESSION_FLAGS = (
    f"{_FLAG_LEAD}{_SILENCE_RULE}",
    "SKIP=",
    "--skip",
    "continue-on-error",
    "|| true",
    "--exit-zero",
)


def _suppression_flag_offenders(name: str, lines: list[tuple[int, str]]) -> list[str]:
    """
    Return every significant line that carries a banned suppression spelling.

    Args:
        name: The file name to report the lines under.
        lines: ``(line number, line)`` pairs, comments already dropped.

    Returns:
        One ``name:line: text`` entry per offending line.

    """
    return [
        f"{name}:{number}: {line}"
        for number, line in lines
        if any(flag in line for flag in SUPPRESSION_FLAGS)
    ]


def test_the_suppression_flag_scan_reports_each_banned_spelling() -> None:
    """Each banned spelling is reported on a line, and a clean step is not."""
    seeded = [
        (1, "continue-on-error: true"),
        (2, "- run: uv run prek run --all-files || true"),
        (3, "- run: SKIP=zizmor uv run prek run --all-files"),
        (4, "- run: uv run prek run --all-files --skip zizmor"),
        (5, "entry: uv run ruff check --exit-zero ."),
        (6, f"- run: uv audit {_FLAG_LEAD}{_SILENCE_RULE} GHSA-0000"),
        (7, "- run: uv run prek run --all-files --show-diff-on-failure"),
    ]

    offenders = _suppression_flag_offenders("seeded.yml", seeded)

    assert [entry.split(":")[1] for entry in offenders] == [
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
    ]


def test_no_workflow_or_hook_file_silences_a_checker_with_a_flag() -> None:
    """
    No workflow or hook step passes a flag or setting that drops a failure (D-05).

    The advisory gate in ``ci.yml`` brought with it a suppression surface the
    existing ban never reached: that guard scans tracked Python files, and
    does so deliberately, because the prose stating the rule lives in Markdown
    and YAML. An advisory waved through by a flag is the same defect as a
    silenced type error -- the report stops, the vulnerable version stays in
    the lock, and the build goes green over it. The match is on the bare flag
    rather than on the audit step, because a line continuation would evade a
    narrower rule and because the flag would be a suppression on any other
    tool in these files too. Now that CI runs the hook file itself, a skipped
    hook or a swallowed exit status is the same defect one level up, so those
    spellings are banned alongside it. The scan runs over significant lines,
    so the comment in ``ci.yml`` stating this ban is not read as the ban being
    broken, and the audit flag is built at runtime for the reason the owner
    guard gives further up.
    """
    scanned = 0
    offenders: list[str] = []
    for path in [*_workflow_files(), PRE_COMMIT_CONFIG]:
        lines = _significant_lines(path)
        scanned += len(lines)
        offenders.extend(
            _suppression_flag_offenders(str(path.relative_to(REPO_ROOT)), lines)
        )
    assert scanned, (
        "no workflow or hook file yielded a single significant line, so this "
        "guard has nothing to scan. Either the workflows moved or the hook "
        "config was renamed, and either way the ban is no longer enforced"
    )
    assert not offenders, (
        "a workflow or hook step tells a checker to drop findings, skips a "
        "hook, or marks a failed step as passed, instead of fixing what was "
        "reported. For the advisory gate that means shipping a package whose "
        "vulnerability is known and recorded, with a green build over it. Fix "
        "the finding, or upgrade past it:\n" + "\n".join(offenders)
    )


# A step's ``uses:`` key as ``_significant_lines`` leaves it: the indentation
# is gone and the list dash may be, but a trailing comment survives intact,
# which is what makes the comment half of the guard below possible.
USES_LINE = re.compile(r"^(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<trailer>.*)$")
# A pinned third-party reference: ``owner/repo`` at a full-length commit SHA.
# Lowercase is load-bearing rather than cosmetic -- a mixed-case SHA names the
# same commit but compares unequal, which would defeat the cross-file check.
PINNED_USES = re.compile(r"^(?P<action>[^@\s]+)@(?P<sha>[0-9a-f]{40})$")
# The trailing comment both workflow headers promise every pin carries.
VERSION_COMMENT = re.compile(r"^#\s*(?P<version>v\d+\.\d+\.\d+)$")
# A call to a workflow in this same repository. ``$/`` is GitHub's
# self-repository reference and is the spelling these files actually use;
# ``./`` is the workspace-relative one, accepted too so that a deliberate
# switch would not be reported as a malformed pin. Neither can collide with
# an ``owner/repo@sha`` form.
LOCAL_WORKFLOW_PREFIXES = ("$/", "./")


def test_every_workflow_uses_reference_is_a_commented_lowercase_sha() -> None:
    """
    Every ``uses:`` ref is a full lowercase SHA carrying its version (D-05).

    A tag or branch ref is mutable: whoever controls the action can repoint it,
    and different bytes then run with this workflow's permissions. The trailing
    comment is the half a reviewer actually reads, and ``ci.yml:1`` and
    ``release.yml:1`` already declare -- in identical words -- that comments
    must carry the full version, while nothing until now parsed that claim.
    The cross-file check catches the one-site-updated-one-missed case, which
    has real duplication to work with: ``actions/checkout`` appears six times
    and ``astral-sh/setup-uv`` five.

    Classification is total on purpose. A ``uses:`` line matching neither the
    exempt nor the pinned form is an offender rather than a silent skip, because
    a guard that quietly passes over what it cannot parse is worse than none.

    What this deliberately does not do: assert that a SHA is the commit its
    comment names. That is a registry lookup, it would break the suite's
    offline guarantee, and it would need a token for rate limits. Dependabot
    rewrites a SHA and its comment together, so ongoing agreement is the bot's
    job; the "wrong from day one" residual was closed once by hand under D-06.
    """
    exempt: list[str] = []
    pinned: list[tuple[str, str, str, str]] = []
    shape_offenders: list[str] = []
    comment_offenders: list[str] = []

    for path in _workflow_files():
        name = path.relative_to(REPO_ROOT)
        for number, line in _significant_lines(path):
            if "uses:" not in line:
                continue
            site = f"{name}:{number}"
            match = USES_LINE.match(line)
            if match is None:
                shape_offenders.append(
                    f"{site}: {line} -- carries a uses: key in a shape this "
                    "guard cannot read, so it was checked by nothing"
                )
                continue
            reference = match.group("ref")
            if reference.startswith(LOCAL_WORKFLOW_PREFIXES) and "@" not in reference:
                exempt.append(site)
                continue
            pin = PINNED_USES.match(reference)
            if pin is None:
                shape_offenders.append(f"{site}: {reference}")
                continue
            comment = VERSION_COMMENT.match(match.group("trailer").strip())
            if comment is None:
                comment_offenders.append(f"{site}: {reference}")
                continue
            pinned.append(
                (site, pin.group("action"), pin.group("sha"), comment.group("version"))
            )

    assert pinned, (
        "no pinned uses: reference was collected from any workflow file, so "
        "this guard would pass over nothing at all. Either "
        f"{WORKFLOW_DIR.relative_to(REPO_ROOT)} is no longer where the "
        "workflows live, or every step was rewritten into a form this guard "
        "cannot read"
    )
    assert exempt, (
        "no uses: reference was exempted as a local reusable-workflow call. "
        "release.yml calls ci.yml through GitHub's self-repository reference, "
        "spelled $/ -- see release.yml:24-31 for why that spelling and not the "
        "workspace-relative ./ one. If the exemption in this guard was rewritten "
        "to ./ then it no longer matches the tree and release.yml's call is "
        "about to be reported as a malformed pin; if release.yml simply stopped "
        "calling ci.yml, delete the exemption as a decision rather than leaving "
        "it matching nothing"
    )
    assert not shape_offenders, (
        "a workflow uses: reference is not pinned to a full 40-character "
        "lowercase commit SHA. A tag, a branch or a short ref is mutable, so "
        "the bytes that run in CI are whatever the action's owner -- or whoever "
        "compromises them -- points it at:\n" + "\n".join(shape_offenders)
    )
    assert not comment_offenders, (
        "a pinned uses: reference carries no trailing # vX.Y.Z comment. A bare "
        "SHA tells a reviewer nothing about which version they are approving, "
        "and both workflow headers state that the comment is there:\n"
        + "\n".join(comment_offenders)
    )

    by_action: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for site, action, sha, version in pinned:
        by_action.setdefault((action, version), []).append((site, sha))

    inconsistent = [
        f"{action} {version}: "
        + ", ".join(f"{site} -> {sha}" for site, sha in sorted(seen))
        for (action, version), seen in sorted(by_action.items())
        if len({sha for _site, sha in seen}) > 1
    ]
    assert not inconsistent, (
        "one action at one version is pinned to two different SHAs. An update "
        "landed at some of its sites and missed others, so the same named "
        "version now runs different bytes depending on which workflow invoked "
        "it:\n" + "\n".join(inconsistent)
    )


def test_the_workflow_reader_collects_both_extensions_github_loads() -> None:
    """
    ``_workflow_files`` reads ``.yaml`` as well as ``.yml`` (PR #13 review).

    GitHub loads both spellings out of ``.github/workflows``. Every workflow
    in this repository happens to be ``.yml``, so a reader that globbed one
    extension passed today and would have kept passing -- while a ``.yaml``
    file added later carried its ``uses:`` refs and any ``--ignore`` flag
    past every guard built on this list. That is the whole failure this
    module exists to prevent, arriving through the guard's own input set.

    The check is on the glob patterns rather than on a fixture file, because
    the defect is what the reader *would* miss, and no ``.yaml`` file exists
    to observe. Asserting the reader finds a file that is not there could
    only be written as a tautology.
    """
    source = inspect.getsource(_workflow_files)
    for suffix in ("*.yml", "*.yaml"):
        assert suffix in source, (
            f"_workflow_files does not glob {suffix!r}. GitHub loads both "
            "spellings, so a workflow using the other one would bypass every "
            "guard that reads this list -- the uses: pin check and the "
            "suppression-flag check both take their input from here."
        )

    found = {path.name for path in _workflow_files()}
    on_disk = {
        path.name
        for path in WORKFLOW_DIR.iterdir()
        if path.suffix in {".yml", ".yaml"} and path.is_file()
    }
    assert found == on_disk, (
        "the reader disagrees with the directory. Every workflow file GitHub "
        f"would load must reach the guards: {sorted(on_disk - found)} missing."
    )


# ---------------------------------------------------------------------------
# The release pipeline's order, approval and provenance
# ---------------------------------------------------------------------------

RELEASE_WORKFLOW = WORKFLOW_DIR / "release.yml"
CI_WORKFLOW = WORKFLOW_DIR / "ci.yml"

# The top-level key that opens the job table, and one job's key beneath it at
# the two-space indent every workflow here uses. Same shape as the compose
# service key above: a name alone on its line, ending in a colon.
JOBS_KEY = "jobs:"
JOB_KEY = re.compile(r"^  (?P<job>[a-z0-9_-]+):\s*$")
# The start of one step in a job's ``steps:`` list, at the six-space indent
# every job here uses.
STEP_START = re.compile(r"^      -\s")
# A ``run:`` key, bare or as a step's first key, with whatever follows it.
RUN_KEY = re.compile(r"^(?P<indent>\s*)(?:-\s+)?run:\s*(?P<value>.*)$")
# A block-scalar indicator: the script is on the lines below the key.
BLOCK_SCALAR = re.compile(r"^[|>][+-]?$")
# The word ``latest`` as a value rather than inside a longer name such as the
# ``ubuntu-latest`` runner label.
LATEST_WORD = re.compile(r"(?<![\w-])latest(?![\w-])")

# The exact expressions that route a release by the gate's one output, which
# it derives from the parsed version, not from the tag's text. Only the literal answer ``false`` selects the
# real index, so an empty or missing gate output falls through to TestPyPI,
# where an upload can be abandoned, never to PyPI, where it cannot be undone.
# Pinned whole rather than by substring: swapping the two arms keeps every
# substring and sends each release candidate to the real index.
GATE_ENVIRONMENT = (
    "${{ needs.gate.outputs.is_prerelease == 'false' && 'pypi' || 'testpypi' }}"
)
GATE_INDEX_URL = (
    "${{ needs.gate.outputs.is_prerelease == 'false' && "
    "'https://upload.pypi.org/legacy/' || 'https://test.pypi.org/legacy/' }}"
)
# The gate's command, and the install it runs after.
GATE_SCRIPT = "scripts/release_gate.py"
GATE_INSTALL = "uv sync --locked --only-group release --no-install-project"
# Ways a workflow has routed a release by reading the tag's text for a hyphen.
# Each is a string test, which is exactly what the version gate replaced.
HYPHEN_ROUTING = ("contains(github.ref_name", "*-*")
# An expression opener. The tag name is attacker-chosen text, so a ``run:``
# script reads it from the environment rather than having it pasted in.
EXPRESSION_OPEN = "${{"

# What each publishing job's token may do, and nothing more.
PUBLISH_DOCKER_PERMISSIONS = {
    "contents": "read",
    "packages": "write",
    "id-token": "write",
    "attestations": "write",
}
PUBLISH_PYPI_PERMISSIONS = {"contents": "read", "id-token": "write"}

# The published image's tag patterns: the exact version on every release,
# and ``major.minor`` from final releases.
SEMVER_VERSION_TAG = "type=semver,pattern={{version}}"
SEMVER_MINOR_TAG = "type=semver,pattern={{major}}.{{minor}}"

ATTEST_ACTION = "actions/attest@"
SBOM_ACTION = "anchore/sbom-action@"
# One exact syft release tag, the form the SBOM action installs from.
SYFT_RELEASE_TAG = re.compile(r"v\d+\.\d+\.\d+")
BUILD_PUSH_ACTION = "docker/build-push-action@"
METADATA_ACTION = "docker/metadata-action@"
BUILD_DIGEST = "steps.build.outputs.digest"
SMOKE_SCRIPT = "scripts/smoke_image.py"


def _strip_trailing_comment(value: str) -> str:
    """Return a YAML scalar without a trailing `` # comment``."""
    return value.split(" #", 1)[0].strip()


def _job_block(lines: list[tuple[int, str]], job: str) -> list[tuple[int, str]]:
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
        significant = line.strip() and not _is_comment(line)
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


def _indent(line: str) -> int:
    """Return the number of leading spaces on a line."""
    return len(line) - len(line.lstrip(" "))


def _key_mapping(
    lines: list[tuple[int, str]], key: str, indent: int
) -> dict[str, str] | None:
    """
    Return the flat mapping held by ``key`` at exactly ``indent`` spaces.

    Both spellings the workflows use are read: a flow mapping on the key's own
    line (``key: {a: b, c: d}``) and a block of ``name: value`` lines nested
    one level deeper. Comments are dropped. A scalar value comes back under
    the empty-string key, so it can never compare equal to a real mapping.

    Args:
        lines: The lines to search, raw.
        key: The key name, without its colon.
        indent: The key's indentation in spaces.

    Returns:
        The mapping, or ``None`` when the key is absent.

    """
    opener = re.compile(rf"^ {{{indent}}}{re.escape(key)}:\s*(?P<value>.*)$")
    for index, (_, line) in enumerate(lines):
        match = opener.match(line)
        if match is None:
            continue
        value = _strip_trailing_comment(match.group("value"))
        if value.startswith("{") and value.endswith("}"):
            pairs = [part.partition(":") for part in value[1:-1].split(",")]
            return {name.strip(): rest.strip() for name, _, rest in pairs if rest}
        if value:
            return {"": value}
        mapping: dict[str, str] = {}
        for _, nested in lines[index + 1 :]:
            if not nested.strip() or _is_comment(nested):
                continue
            if _indent(nested) <= indent:
                break
            name, _, rest = nested.strip().partition(":")
            mapping[name] = _strip_trailing_comment(rest)
        return mapping
    return None


def _job_needs(block: list[tuple[int, str]]) -> set[str]:
    """
    Return the job ids a job's ``needs:`` names.

    Args:
        block: The job's lines, as ``_job_block`` returns them.

    Returns:
        Every job named, whether as a scalar, a flow list or a block list.

    """
    for index, (_, line) in enumerate(block):
        match = re.match(r"^    needs:\s*(?P<value>.*)$", line)
        if match is None:
            continue
        value = _strip_trailing_comment(match.group("value"))
        if value.startswith("["):
            return {part.strip() for part in value.strip("[]").split(",") if part}
        if value:
            return {value}
        needs: set[str] = set()
        for _, nested in block[index + 1 :]:
            if not nested.strip() or _is_comment(nested):
                continue
            if _indent(nested) <= _indent(line):
                break
            needs.add(_strip_trailing_comment(nested.strip().removeprefix("-")))
        return needs
    return set()


def _job_steps(block: list[tuple[int, str]]) -> list[list[str]]:
    """
    Split a job into its steps, each as stripped significant lines.

    Args:
        block: The job's lines, as ``_job_block`` returns them.

    Returns:
        One list per step, in order. Lines before the first step are dropped.

    """
    steps: list[list[str]] = []
    for _, line in block:
        if STEP_START.match(line):
            steps.append([])
        if steps and line.strip() and not _is_comment(line):
            steps[-1].append(line.strip())
    return steps


def _steps_using(block: list[tuple[int, str]], action: str) -> list[list[str]]:
    """Return every step of a job whose ``uses:`` names ``action``."""
    return [
        step
        for step in _job_steps(block)
        if any(
            re.match(r"^(?:-\s+)?uses:\s*", line) and action in line for line in step
        )
    ]


def _step_value(step: list[str], key: str) -> str | None:
    """Return a step key's value, comment stripped, or ``None`` when absent."""
    for line in step:
        name, separator, rest = line.removeprefix("- ").partition(":")
        if separator and name.strip() == key:
            return _strip_trailing_comment(rest)
    return None


def _run_scripts(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
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
            if _indent(line) > block_indent:
                scripts.append((number, line.strip()))
                continue
            block_indent = None
        match = RUN_KEY.match(line)
        if match is None or _is_comment(line):
            continue
        value = match.group("value").strip()
        if BLOCK_SCALAR.match(value):
            block_indent = len(match.group("indent"))
        else:
            scripts.append((number, value))
    return scripts


def _release_job(job: str) -> list[tuple[int, str]]:
    """Return one release job's block, asserting that the job exists."""
    block = _job_block(_numbered(RELEASE_WORKFLOW), job)
    assert block, (
        f"release.yml declares no {job!r} job under jobs:, so none of the "
        "guards on it can check anything"
    )
    return block


_SEEDED_WORKFLOW = """\
name: Seeded

permissions:
  contents: read

jobs:
  # a comment at the job indent is not a job key
  first:
    needs: [ci, gate]  # trailing reason
    permissions:
      contents: read  # reason
      id-token: write
    environment:
      name: ${{ needs.gate.outputs.is_prerelease == 'true' && 'a' || 'b' }}
    steps:
      - uses: example/one@0000000000000000000000000000000000000000 # v1.0.0
        with:
          push: true
      - run: |
          echo "${GITHUB_REF_NAME}"
          echo done
  second:
    needs:
      - first
    permissions: {contents: read, packages: write}
    steps:
      - run: echo ${{ github.ref_name }}

concurrency:
  group: release
"""


def test_the_job_reader_finds_one_release_job_and_stops_at_the_next(
    tmp_path: Path,
) -> None:
    """
    The job reader returns one job's lines and nothing from its neighbours.

    The release guards below read ``needs:``, ``permissions:`` and steps out of
    a named job without a YAML parser, so the reader has to be right about
    where a job ends, or one job's keys would be credited to another.
    """
    seeded = tmp_path / "seeded.yml"
    seeded.write_text(_SEEDED_WORKFLOW, encoding="utf-8")
    lines = _numbered(seeded)

    first = _job_block(lines, "first")
    second = _job_block(lines, "second")

    assert _job_needs(first) == {"ci", "gate"}
    assert _job_needs(second) == {"first"}
    assert _key_mapping(first, "permissions", 4) == {
        "contents": "read",
        "id-token": "write",
    }
    assert _key_mapping(second, "permissions", 4) == {
        "contents": "read",
        "packages": "write",
    }
    assert _key_mapping(lines, "concurrency", 0) == {"group": "release"}
    assert _key_mapping(first, "environment", 4) == {
        "name": "${{ needs.gate.outputs.is_prerelease == 'true' && 'a' || 'b' }}"
    }
    assert not any("second" in line or "- first" in line for _, line in first)
    assert not any("group:" in line for _, line in second)
    assert _job_block(lines, "absent") == []
    assert len(_job_steps(first)) == 2
    assert _step_value(_steps_using(first, "example/one@")[0], "push") == "true"
    assert [line for _, line in _run_scripts(lines)] == [
        'echo "${GITHUB_REF_NAME}"',
        "echo done",
        "echo ${{ github.ref_name }}",
    ]


def test_release_publishes_only_after_ci_and_the_version_gate() -> None:
    """
    Nothing publishes until CI and the version gate pass, and PyPI goes last.

    A container registry tag can be deleted and pushed again; a package index
    upload cannot. So the image publish needs CI (which builds and smoke-tests
    the image) and the gate, and the index upload needs all three. A release
    candidate once uploaded to the index in parallel with an image build that
    then failed, which is the half-publish this order rules out.
    """
    docker_needs = _job_needs(_release_job("publish-docker"))
    pypi_needs = _job_needs(_release_job("publish-pypi"))

    assert {"ci", "gate"} <= docker_needs, (
        f"publish-docker needs {sorted(docker_needs)}; it must wait for both "
        "ci and gate"
    )
    assert {"ci", "gate", "publish-docker"} <= pypi_needs, (
        f"publish-pypi needs {sorted(pypi_needs)}; the irreversible upload "
        "must wait for ci, gate and the image publish"
    )


def test_release_gate_job_runs_the_gate_script_and_outputs_the_routing() -> None:
    """
    The gate job runs the version gate from the lock and exposes its answer.

    The tag and ``pyproject.toml`` must name the same version or nothing
    publishes, and the parsed version decides whether the release is a
    pre-release. The gate's only dependency is installed hash-checked from the
    lock, never fetched ad hoc into a publishing workflow.
    """
    block = _release_job("gate")
    scripts = [line for _, line in _run_scripts(block)]

    assert any(GATE_INSTALL in line for line in scripts), (
        f"the gate job does not run {GATE_INSTALL!r}; its dependency must "
        f"come from the lock. Run lines: {scripts}"
    )
    assert any(GATE_SCRIPT in line for line in scripts), (
        f"the gate job does not run {GATE_SCRIPT}. Run lines: {scripts}"
    )
    outputs = _key_mapping(block, "outputs", 4) or {}
    assert outputs.get("is_prerelease") == "${{ steps.gate.outputs.is_prerelease }}", (
        "the gate job does not output is_prerelease from its gate step, so "
        f"nothing downstream can route by it. Outputs: {outputs}"
    )
    # The output names a step by id, and a renamed id leaves it empty without
    # any error. So the step it names must exist and must be the gate itself.
    gate_steps = [
        step for step in _job_steps(block) if _step_value(step, "id") == "gate"
    ]
    assert len(gate_steps) == 1, (
        "the gate job has no single step with `id: gate`, so the is_prerelease "
        "output it declares is always empty"
    )
    assert GATE_SCRIPT in (_step_value(gate_steps[0], "run") or ""), (
        f"the `id: gate` step does not run {GATE_SCRIPT}, so the routing output "
        f"is not the version gate's answer: {gate_steps[0]}"
    )


def test_both_publish_jobs_route_by_the_gate_not_by_a_hyphen_in_the_tag() -> None:
    """
    Environment and index both follow the gate's parsed-version answer.

    A hyphen test on the tag's text sends ``v0.2.0rc7`` to the real index,
    because PEP 440 needs no hyphen to spell a pre-release. Both publish jobs
    select their environment with the same expression, so a final release is
    approved before the image push as well as before the upload, and the
    upload URL follows the same answer.

    The direction is pinned, not just the output's presence: only the literal
    answer ``false`` may select the real index, so a missing answer, or arms
    swapped by an edit, cannot send a release candidate there.
    """
    names = {
        job: (_key_mapping(_release_job(job), "environment", 4) or {}).get("name", "")
        for job in ("publish-docker", "publish-pypi")
    }
    for job, name in names.items():
        assert name == GATE_ENVIRONMENT, (
            f"{job}'s environment is {name!r}; it must be exactly "
            f"{GATE_ENVIRONMENT!r}, which picks pypi only on an explicit "
            "`false` from the gate and TestPyPI on anything else"
        )

    pypi_steps = _steps_using(
        _release_job("publish-pypi"), "pypa/gh-action-pypi-publish@"
    )
    assert len(pypi_steps) == 1, "publish-pypi has no single upload step"
    url = _step_value(pypi_steps[0], "repository-url") or ""
    assert url == GATE_INDEX_URL, (
        f"the upload's repository-url is {url!r}; it must be exactly "
        f"{GATE_INDEX_URL!r}, which picks the real index only on an explicit "
        "`false` from the gate"
    )

    offenders = [
        f"release.yml:{number}: {line}"
        for number, line in _significant_lines(RELEASE_WORKFLOW)
        if any(needle in line for needle in HYPHEN_ROUTING)
    ]
    assert not offenders, (
        "release.yml still routes a release by testing the tag's text for a "
        "hyphen:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        ("publish-docker", PUBLISH_DOCKER_PERMISSIONS),
        ("publish-pypi", PUBLISH_PYPI_PERMISSIONS),
    ],
)
def test_publish_jobs_hold_exactly_the_permissions_they_use(
    job: str, expected: dict[str, str]
) -> None:
    """
    Each publish job's token can do what that job publishes with, no more.

    The image job pushes to the registry and signs and stores attestations;
    the upload job only needs its OIDC identity. A scope beyond that is a
    scope a compromised step could use.
    """
    permissions = _key_mapping(_release_job(job), "permissions", 4)
    assert permissions == expected, (
        f"{job} holds {permissions}, expected exactly {expected}"
    )


def test_release_runs_do_not_overlap_in_the_release_concurrency_group() -> None:
    """
    Two tags pushed close together publish one after the other.

    Out of order, the older release could finish last and leave ``latest`` on
    it. An in-flight publish is never cancelled either, since cancelling one
    between the image push and the upload would be a half-publish. A waiting
    publish is not dropped: the default queue holds one pending run and
    cancels it when a newer one enters the group, so a third tag pushed while
    a final release waits for approval would lose the second release silently.
    """
    lines = _numbered(RELEASE_WORKFLOW)
    concurrency = _key_mapping(lines, "concurrency", 0)
    assert concurrency is not None, "release.yml has no workflow-level concurrency"
    assert concurrency.get("group") == "release", (
        f"release.yml's concurrency group is {concurrency.get('group')!r}, "
        "expected 'release'"
    )
    assert concurrency.get("cancel-in-progress") == "false", (
        f"release.yml's concurrency must not cancel an in-flight release: {concurrency}"
    )
    assert concurrency.get("queue") == "max", (
        "release.yml's concurrency keeps only one pending release, so a third "
        f"tag cancels the second without a failure: {concurrency}"
    )


def test_publish_docker_attests_signed_provenance_and_an_sbom() -> None:
    """
    The pushed image gets a signed provenance and a signed SBOM attestation.

    Both are made for the digest the build step pushed and are pushed to the
    registry beside it. BuildKit's own provenance is switched off explicitly:
    left at its default on a public repository it attaches an unsigned
    manifest, which the signed attestations replace.
    """
    block = _release_job("publish-docker")
    builds = _steps_using(block, BUILD_PUSH_ACTION)
    assert len(builds) == 1, "publish-docker has no single build-push step"
    build = builds[0]
    assert _step_value(build, "id") == "build", (
        "the build step is not `id: build`, so its digest is not the one the "
        "attestation steps name"
    )
    assert _step_value(build, "push") == "true"
    assert _step_value(build, "provenance") == "false", (
        "the build step does not set provenance: false, so BuildKit attaches "
        "an unsigned provenance manifest by default"
    )

    attests = _steps_using(block, ATTEST_ACTION)
    assert len(attests) == 2, (
        f"publish-docker has {len(attests)} {ATTEST_ACTION} steps, expected "
        "two: provenance and SBOM"
    )
    for step in attests:
        assert _step_value(step, "push-to-registry") == "true", step
        assert BUILD_DIGEST in (_step_value(step, "subject-digest") or ""), step
        assert _step_value(step, "create-storage-record") == "false", step
    with_sbom = [step for step in attests if _step_value(step, "sbom-path")]
    assert len(with_sbom) == 1, (
        "exactly one attest step must carry sbom-path (the SBOM); the other "
        "is the provenance attestation"
    )

    sboms = _steps_using(block, SBOM_ACTION)
    assert len(sboms) == 1, f"publish-docker has no single {SBOM_ACTION} step"
    assert BUILD_DIGEST in (_step_value(sboms[0], "image") or ""), (
        "the SBOM is not generated from the pushed digest"
    )
    steps = _job_steps(block)
    assert steps.index(sboms[0]) < steps.index(with_sbom[0]), (
        "the SBOM is attested before it is generated"
    )


def test_the_sbom_step_pins_the_syft_it_runs_and_withholds_the_token() -> None:
    """
    The SBOM step names the syft release it runs and passes it no token.

    The action's SHA pin covers the action's own code, not the syft binary it
    downloads at run time from a release tag. Naming an exact release tag
    (one that was checked to be immutable when it was chosen) fixes those
    bytes; leaving the input out hands the choice to whatever default the
    action ships. syft inherits the action's environment, inputs included,
    and this step uploads nothing, so it gets no GitHub token.
    """
    sboms = _steps_using(_release_job("publish-docker"), SBOM_ACTION)
    assert len(sboms) == 1, f"publish-docker has no single {SBOM_ACTION} step"
    version = _step_value(sboms[0], "syft-version") or ""
    assert SYFT_RELEASE_TAG.fullmatch(version), (
        f"the SBOM step's syft-version is {version!r}; it must name one exact "
        "syft release tag such as v1.51.1, not a branch, a range or the "
        "action's default"
    )
    token = _step_value(sboms[0], "github-token")
    assert token in {'""', "''"}, (
        f"the SBOM step's github-token is {token!r}; it must be set to an empty "
        "string, or syft inherits the job's token through the action's inputs"
    )


def test_release_image_tags_follow_semver_and_leave_latest_to_the_default() -> None:
    """
    Every release gets its exact tag, finals also ``major.minor``, no raw latest.

    The tagging action skips partial patterns for pre-releases and applies
    ``latest`` to finals only by default, so writing ``latest`` anywhere in the
    image job could only move it onto a release candidate.
    """
    block = _release_job("publish-docker")
    metas = _steps_using(block, METADATA_ACTION)
    assert len(metas) == 1, "publish-docker has no single metadata step"
    meta = metas[0]
    for pattern in (SEMVER_VERSION_TAG, SEMVER_MINOR_TAG):
        assert pattern in meta, f"the metadata step's tags lack {pattern!r}"

    offenders = [
        f"release.yml:{number}: {line.strip()}"
        for number, line in block
        if not _is_comment(line) and LATEST_WORD.search(line)
    ]
    assert not offenders, (
        "publish-docker names latest; leave it to the tagging action's "
        "default, which withholds it from pre-releases:\n" + "\n".join(offenders)
    )


def test_release_run_scripts_never_paste_an_expression_into_the_shell() -> None:
    """
    No release ``run:`` script has an expression expanded into its text.

    The tag name is chosen by whoever pushes it, and an expression is
    substituted into the script before the shell parses it, so a crafted tag
    would become shell. The gate reads the tag from the environment instead.
    """
    scripts = _run_scripts(_numbered(RELEASE_WORKFLOW))
    assert scripts, "release.yml has no run: script, so this checked nothing"
    offenders = [
        f"release.yml:{number}: {line}"
        for number, line in scripts
        if EXPRESSION_OPEN in line
    ]
    assert not offenders, (
        "a release run: script expands an expression into shell text:\n"
        + "\n".join(offenders)
    )


def test_the_ci_docker_job_builds_and_smoke_tests_the_image() -> None:
    """
    Every CI run builds the image locally and runs the smoke script on it.

    The build is loaded into the runner and never pushed, and the checks live
    in the script rather than in inline YAML, so every caller runs the same
    ones. Because the release workflow calls CI, publishing waits for this.
    """
    block = _job_block(_numbered(CI_WORKFLOW), "docker")
    assert block, "ci.yml declares no docker job"
    builds = _steps_using(block, BUILD_PUSH_ACTION)
    assert len(builds) == 1, "the docker job has no single build-push step"
    build = builds[0]
    assert _step_value(build, "push") == "false"
    assert _step_value(build, "load") == "true"
    tag = _step_value(build, "tags")
    assert tag, "the docker job's build names no tag for the smoke run to use"

    scripts = [line for _, line in _run_scripts(block)]
    assert scripts, "the docker job runs nothing after the build"
    inline = [line for line in scripts if SMOKE_SCRIPT not in line]
    assert not inline, (
        f"the docker job runs checks outside {SMOKE_SCRIPT}; keep them in the "
        f"script so every caller runs the same ones: {inline}"
    )
    assert any(line.endswith(f"{SMOKE_SCRIPT} {tag}") for line in scripts), (
        f"the smoke script is not run against the image the build tagged {tag}: "
        f"{scripts}"
    )


# ---------------------------------------------------------------------------
# The hook file as the one definition of the gate
# ---------------------------------------------------------------------------

DOCS_WORKFLOW = WORKFLOW_DIR / "docs.yml"

# One entry in the hook file's ``repos:`` list as ``_significant_lines`` leaves
# it, and the two repository names that are never fetched from anywhere.
HOOK_REPO = re.compile(r"^-\s+repo:\s*(?P<repo>\S+)$")
IN_TREE_HOOK_REPOS = frozenset({"local", "meta"})
# A remote hook pin: a full-length lowercase commit SHA, then the release it
# was resolved from, in the comment form ``prek autoupdate --freeze`` writes
# and Dependabot updates together with the SHA. A tag is a name the upstream
# owner can move to other code; a commit SHA is not.
REV_LINE = re.compile(r"^rev:\s*(?P<value>.*)$")
FROZEN_REV = re.compile(r"^[0-9a-f]{40}\s+#\s*frozen:\s*v\d+\.\d+\.\d+$")
# The hook that rewrites a frozen remote rev back to the tag in its comment.
SYNCING_HOOK_REPO = "sync-with-uv"

# The commands the CI lint job runs, each as its own step: the hook file at
# its commit stage and at its push stage, both over the whole tree, then the
# advisory audit, which reaches the network and so is deliberately no hook.
LINT_COMMANDS = (
    "uv run prek run --all-files",
    "uv run prek run --stage pre-push --all-files",
    "uv audit --preview-features audit-command",
)
# A checker the hook file already runs, invoked by name from a workflow step.
# A second spelling of a gate in the workflow is a second definition of it,
# and the two drift.
DIRECT_CHECKER = re.compile(r"(?<![\w-])(?:ruff|ty\s+check|pyrefly|zizmor)(?![\w-])")

# The condition that keeps a docs step or job off pull requests.
PUSH_ONLY = "github.event_name == 'push'"
UPLOAD_PAGES_ACTION = "actions/upload-pages-artifact@"
MKDOCS_BUILD = "mkdocs build"
STRICT_FLAG = "--strict"
QUIET_FLAG = "--quiet"
# A job-level ``if:`` key, at the four-space indent every job key here uses.
JOB_IF = re.compile(r"^    if:\s*(?P<value>.*)$")


def _top_level_block(
    lines: list[tuple[int, str]], key: str
) -> tuple[str, list[tuple[int, str]]] | None:
    """
    Return a top-level key's inline value and the raw lines nested under it.

    Args:
        lines: ``(line number, line)`` pairs for the whole file, raw.
        key: The top-level key, without its colon.

    Returns:
        The value written on the key's own line (empty for a block) and the
        lines up to the next top-level key, or ``None`` when the key is absent.

    """
    value: str | None = None
    block: list[tuple[int, str]] = []
    for number, line in lines:
        significant = line.strip() and not _is_comment(line)
        if significant and not line[0].isspace():
            if value is not None:
                break
            name, separator, rest = line.partition(":")
            if separator and name == key:
                value = _strip_trailing_comment(rest)
            continue
        if value is not None:
            block.append((number, line))
    return None if value is None else (value, block)


def _workflow_triggers(lines: list[tuple[int, str]]) -> set[str]:
    """Return the event names a workflow's ``on:`` key lists, in either form."""
    found = _top_level_block(lines, "on")
    if found is None:
        return set()
    value, block = found
    if value:
        return {part.strip() for part in value.strip("[]").split(",") if part.strip()}
    return {
        line.strip().partition(":")[0]
        for _, line in block
        if line.strip() and not _is_comment(line) and _indent(line) == 2
    }


def _job_ids(lines: list[tuple[int, str]]) -> list[str]:
    """Return the ids of the jobs declared under the top-level ``jobs:`` key."""
    found = _top_level_block(lines, "jobs")
    if found is None:
        return []
    return [
        match.group("job")
        for _, line in found[1]
        for match in [JOB_KEY.match(line)]
        if match is not None
    ]


def _condition(value: str | None) -> str:
    """Return an ``if:`` value without the optional ``${{ }}`` wrapper."""
    text = (value or "").strip()
    if text.startswith(EXPRESSION_OPEN) and text.endswith("}}"):
        text = text.removeprefix(EXPRESSION_OPEN).removesuffix("}}")
    return text.strip()


def _hook_repos(lines: list[tuple[int, str]]) -> list[tuple[int, str, list[str]]]:
    """
    Split the hook file's significant lines into one window per repo entry.

    Args:
        lines: ``_significant_lines`` output for the hook file.

    Returns:
        ``(line number, repo, lines)`` per entry, where the lines run from the
        one after ``- repo:`` to the one before the next entry.

    """
    starts = [
        (index, match.group("repo"))
        for index, (_number, line) in enumerate(lines)
        for match in [HOOK_REPO.match(line)]
        if match is not None
    ]
    repos: list[tuple[int, str, list[str]]] = []
    for position, (index, repo) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        window = [line for _number, line in lines[index + 1 : end]]
        repos.append((lines[index][0], repo, window))
    return repos


def _unfrozen_hook_repos(lines: list[tuple[int, str]]) -> tuple[int, list[str]]:
    """
    Return how many remote hook repos there are, and each one not frozen.

    Args:
        lines: ``_significant_lines`` output for the hook file.

    Returns:
        The number of remote repo entries, and one ``line: repo -- reason``
        entry per repo whose ``rev:`` is not a frozen commit SHA.

    """
    remote = 0
    offenders: list[str] = []
    for number, repo, window in _hook_repos(lines):
        if repo in IN_TREE_HOOK_REPOS:
            continue
        remote += 1
        revs = [
            match.group("value")
            for line in window
            for match in [REV_LINE.match(line)]
            if match is not None
        ]
        if len(revs) != 1:
            offenders.append(f"{number}: {repo} -- {len(revs)} rev: lines")
        elif FROZEN_REV.match(revs[0]) is None:
            offenders.append(f"{number}: {repo} -- rev: {revs[0]}")
    return remote, offenders


def test_every_remote_hook_repo_is_pinned_to_a_frozen_commit() -> None:
    """
    Every hook fetched from elsewhere is pinned to a commit, with its version.

    prek clones a remote hook repo at its ``rev:`` and runs the code there
    with the developer's or the runner's privileges. A tag is a name its owner
    can move, so a compromised upstream account could hand every checkout new
    code under the old version. A full commit SHA cannot be moved, and the
    ``# frozen: vX.Y.Z`` comment keeps the pin readable and is what Dependabot
    rewrites together with the SHA.
    """
    remote, offenders = _unfrozen_hook_repos(_significant_lines(PRE_COMMIT_CONFIG))
    assert remote, (
        f"{PRE_COMMIT_CONFIG.name} declares no remote hook repo at all, so this "
        "guard has nothing to check. Either every hook became local or the "
        "repo entry spelling changed under the scan"
    )
    assert not offenders, (
        "a remote hook repo is pinned to a movable name rather than a commit. "
        "Run `uv run prek autoupdate --freeze` (or resolve the tag's commit by "
        "hand) so each reads `rev: <40-hex sha>  # frozen: vX.Y.Z`:\n"
        + "\n".join(f"{PRE_COMMIT_CONFIG.name}:{entry}" for entry in offenders)
    )


def test_the_frozen_rev_scan_reports_a_tag_a_bare_sha_and_a_missing_rev() -> None:
    """A tag, an uncommented SHA, a mixed-case SHA and no rev are all reported."""
    sha = "3e8a8703264a2f4a69428a0aa4dcb512790b2c8c"
    seeded = list(
        enumerate(
            [
                "repos:",
                "- repo: https://example.invalid/frozen",
                f"rev: {sha}  # frozen: v6.0.0",
                "hooks:",
                "- id: check-ast",
                "- repo: https://example.invalid/tag",
                "rev: v6.0.0",
                "- repo: https://example.invalid/bare",
                f"rev: {sha}",
                "- repo: https://example.invalid/upper",
                f"rev: {sha.upper()}  # frozen: v6.0.0",
                "- repo: https://example.invalid/none",
                "hooks:",
                "- repo: local",
                "hooks:",
                "- id: ty-checker",
                "- repo: meta",
            ],
            start=1,
        )
    )

    remote, offenders = _unfrozen_hook_repos(seeded)

    assert remote == 5
    assert [entry.split(" -- ")[0] for entry in offenders] == [
        "6: https://example.invalid/tag",
        "8: https://example.invalid/bare",
        "10: https://example.invalid/upper",
        "12: https://example.invalid/none",
    ]


def test_the_hook_file_has_no_sync_with_uv_hook() -> None:
    """
    No hook rewrites a frozen remote rev back to a tag.

    ``sync-with-uv`` sets each remote hook's rev to the version ``uv.lock``
    holds. Run against a frozen pin it replaced the SHA with the tag and left
    the ``# frozen:`` comment behind, so the file claimed a pin it no longer
    had. ruff, the one tool it kept in step, now runs from the lock through
    local hooks, so the syncing hook has nothing left to do.
    """
    repos = [
        repo
        for _number, repo, _window in _hook_repos(_significant_lines(PRE_COMMIT_CONFIG))
    ]
    assert repos, f"{PRE_COMMIT_CONFIG.name} declares no hook repo at all"
    offenders = [repo for repo in repos if SYNCING_HOOK_REPO in repo]
    assert not offenders, (
        f"{PRE_COMMIT_CONFIG.name} still carries {SYNCING_HOOK_REPO}, which "
        "rewrites a frozen commit SHA back to the tag in its comment and so "
        f"silently undoes the pin: {offenders}"
    )


def _ci_lint_offenders(lines: list[tuple[int, str]]) -> list[str]:
    """
    Return every way a CI workflow departs from running the hook file as its gate.

    Args:
        lines: ``(line number, line)`` pairs for the whole workflow, raw.

    Returns:
        One entry per lint command the ``lint`` job does not run as a step of
        its own, and one per ``run:`` line anywhere that calls a checker the
        hook file already runs.

    """
    block = _job_block(lines, "lint")
    if not block:
        return ["no lint job is declared under jobs:"]
    runs = [
        value
        for step in _job_steps(block)
        for value in [_step_value(step, "run")]
        if value is not None
    ]
    offenders = [
        f"the lint job has no step running {command!r}"
        for command in LINT_COMMANDS
        if not any(run == command or run.startswith(f"{command} ") for run in runs)
    ]
    offenders.extend(
        f"{number}: {line} -- calls a checker the hook file already runs"
        for number, line in _run_scripts(lines)
        if DIRECT_CHECKER.search(line)
    )
    return offenders


def test_ci_lint_job_runs_the_hook_file_at_both_stages_and_the_audit() -> None:
    """
    CI's lint job runs the hook file, and no workflow step repeats a checker.

    ``.pre-commit-config.yaml`` is the one definition of the gate. The lint job
    runs it at the commit stage, where a fixer that would change a file fails
    the job with the diff, and at the push stage, where the full type checks,
    the no-fix lint and format checks and the deployment-config tests run.
    The advisory audit stays its own step because it reaches the network. A
    checker called by name from a step would be a second definition, free to
    drift from the first.
    """
    offenders = _ci_lint_offenders(_numbered(CI_WORKFLOW))
    assert not offenders, (
        "ci.yml does not run the hook file as its lint gate. The lint job "
        f"must run each of {list(LINT_COMMANDS)} as its own step, and no step "
        "may call ruff, ty, pyrefly or zizmor directly:\n" + "\n".join(offenders)
    )


_SEEDED_CI = """\
name: Seeded

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000 # v1.0.0
      - run: uv sync --locked
{lint_steps}
  test:
    runs-on: ubuntu-latest
    steps:
      - run: {test_run}
"""
# The lint job as it was before the hook file became the gate: each checker
# its own step, the commit stage alone, and no push stage.
_SEEDED_OLD_LINT_STEPS = """\
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run ty check
      - run: uv run zizmor .
      - run: uv run prek run --all-files
      - run: uv audit --preview-features audit-command"""


def test_the_ci_lint_scan_reports_direct_checkers_and_missing_stages(
    tmp_path: Path,
) -> None:
    """The old separate-steps shape is reported; the hook-file shape is not."""
    old = tmp_path / "old.yml"
    old.write_text(
        _SEEDED_CI.format(
            lint_steps=_SEEDED_OLD_LINT_STEPS,
            test_run="uv run pyrefly check src tests scripts",
        ),
        encoding="utf-8",
    )
    new = tmp_path / "new.yml"
    new.write_text(
        _SEEDED_CI.format(
            lint_steps="\n".join(
                f"      - run: {command} --show-diff-on-failure"
                if "prek" in command
                else f"      - run: {command}"
                for command in LINT_COMMANDS
            ),
            test_run='uv run env HOME="$(mktemp -d)" pytest',
        ),
        encoding="utf-8",
    )

    offenders = _ci_lint_offenders(_numbered(old))

    assert offenders[0] == (
        "the lint job has no step running "
        "'uv run prek run --stage pre-push --all-files'"
    )
    assert [entry.split(":")[0] for entry in offenders[1:]] == [
        "9",
        "10",
        "11",
        "12",
        "18",
    ]
    assert _ci_lint_offenders(_numbered(new)) == []
    assert _ci_lint_offenders([(1, "jobs:")]) == ["no lint job is declared under jobs:"]


def _docs_workflow_offenders(lines: list[tuple[int, str]]) -> list[str]:
    """
    Return every way the docs workflow departs from build-on-PR, deploy-on-push.

    Args:
        lines: ``(line number, line)`` pairs for the whole workflow, raw.

    Returns:
        One entry per broken property: the pull-request trigger, the deploy
        job's push gate, each upload step's push gate, and each ``mkdocs
        build`` line that is not strict or that is quiet.

    """
    offenders: list[str] = []
    if "pull_request" not in _workflow_triggers(lines):
        offenders.append("on: has no pull_request trigger")
    deploy_if = [
        _condition(match.group("value"))
        for _, line in _job_block(lines, "deploy")
        for match in [JOB_IF.match(line)]
        if match is not None
    ]
    if deploy_if != [PUSH_ONLY]:
        offenders.append(f"the deploy job is not gated on push: {deploy_if}")
    uploads = [
        step
        for job in _job_ids(lines)
        for step in _steps_using(_job_block(lines, job), UPLOAD_PAGES_ACTION)
    ]
    if not uploads:
        offenders.append("no step uploads a Pages artifact")
    offenders.extend(
        f"a Pages upload step is not gated on push: {step}"
        for step in uploads
        if _condition(_step_value(step, "if")) != PUSH_ONLY
    )
    builds = [
        (number, line) for number, line in _run_scripts(lines) if MKDOCS_BUILD in line
    ]
    if not builds:
        offenders.append(f"no step runs {MKDOCS_BUILD}")
    for number, line in builds:
        flags = line.split(MKDOCS_BUILD, 1)[1].split()
        if STRICT_FLAG not in flags:
            offenders.append(f"{number}: {line} -- not {STRICT_FLAG}")
        quiet = [
            flag
            for flag in flags
            if flag == QUIET_FLAG
            or (flag.startswith("-") and not flag.startswith("--") and "q" in flag)
        ]
        if quiet:
            offenders.append(f"{number}: {line} -- {quiet} hides what strict counts")
    return offenders


def test_docs_workflow_builds_prs_strictly_and_deploys_only_on_push() -> None:
    """
    Every pull request builds the site strictly; only a push to master publishes.

    Strict mode fails the build on a warning, and quiet mode hides warnings,
    so a quiet strict build exits 0 over a dead anchor. The Pages upload and
    the deploy job run on push only, so a pull request can break the build but
    never the published site.
    """
    offenders = _docs_workflow_offenders(_numbered(DOCS_WORKFLOW))
    assert not offenders, (
        "docs.yml no longer builds every pull request with a strict, "
        "unquiet mkdocs build while publishing only on push:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("  pull_request:\n", "", "no pull_request trigger"),
        (
            "    if: github.event_name == 'push'\n    needs: build",
            "    needs: build",
            "deploy job is not gated",
        ),
        (
            "        if: github.event_name == 'push'\n        with:\n          path: site",
            "        with:\n          path: site",
            "upload step is not gated",
        ),
        ("mkdocs build --strict", "mkdocs build --strict -q", "hides what strict"),
        ("mkdocs build --strict", "mkdocs build --quiet --strict", "hides what"),
        ("mkdocs build --strict", "mkdocs build", "not --strict"),
    ],
)
def test_the_docs_workflow_scan_reports_each_seeded_break(
    old: str, new: str, expected: str
) -> None:
    """Each property of the real workflow, broken on its own, is reported."""
    text = DOCS_WORKFLOW.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"the seed {old!r} no longer matches docs.yml once"
    seeded = list(enumerate(text.replace(old, new).splitlines(), start=1))

    offenders = _docs_workflow_offenders(seeded)

    assert any(expected in entry for entry in offenders), offenders


def _anchor_validation(lines: list[tuple[int, str]]) -> str | None:
    """Return mkdocs.yml's ``validation.anchors`` level, or ``None`` if unset."""
    mapping = _key_mapping(lines, "validation", 0)
    return None if mapping is None else mapping.get("anchors")


def test_docs_workflow_build_validates_anchors_through_mkdocs_yml() -> None:
    """
    The strict docs build fails on a link to an anchor that does not exist.

    mkdocs ignores a dead ``page.md#anchor`` link unless ``validation.anchors``
    raises it to a warning, and only then does ``--strict`` turn it into a
    failed build.
    """
    level = _anchor_validation(_numbered(MKDOCS))
    assert level == "warn", (
        f"mkdocs.yml sets validation.anchors to {level!r}, so the strict build "
        "passes a link to an anchor that does not exist. Set it to warn"
    )


@pytest.mark.parametrize(
    "text",
    [
        "site_name: x\n",
        "validation:\n  anchors: info\n",
        "validation:\n  unrecognized_links: warn\n",
        "nav:\n  anchors: warn\n",
    ],
)
def test_the_anchor_validation_reader_reports_anything_but_warn(text: str) -> None:
    """An absent, lower or misplaced anchors level does not read as warn."""
    lines = list(enumerate(text.splitlines(), start=1))
    assert _anchor_validation(lines) != "warn"
    assert _anchor_validation([(1, "validation: {anchors: warn}")]) == "warn"


# ---------------------------------------------------------------------------
# Phase 37: the config filename rename (CFG-05, D-01, D-03, D-13, D-17)
# ---------------------------------------------------------------------------


def test_deploy_doc_tells_upgraders_to_rename_the_config_file() -> None:
    """
    The compose how-to gives the rename an upgrading operator has to perform.

    Every container deployed from this guide holds the old
    ``config/config.toml`` rather than ``config/saneless.toml``, because the
    old name is what the guide told its reader to create. saneless no longer
    reads it, so on the first pull after the rename the file goes
    silently unread -- the exact failure this phase exists to remove. The
    guide must therefore carry the rename as a command, on one line, naming
    both filenames, and must say what the reader sees until they run it: the
    status page's Configuration row.
    """
    text = DEPLOY_HOWTO.read_text(encoding="utf-8")
    name = DEPLOY_HOWTO.relative_to(REPO_ROOT)
    renames = [
        line
        for line in text.splitlines()
        if "mv" in line
        if f"config/{LEGACY_CONFIG_NAME}" in line
        if f"config/{CONFIG_NAME}" in line
    ]
    assert renames, (
        f"{name} has no line telling an upgrading operator to rename "
        f"config/{LEGACY_CONFIG_NAME} to config/{CONFIG_NAME}. Without it "
        "every container deployed from this guide loads no config at all "
        "after the upgrade, with nothing on the page to say why"
    )
    assert "Configuration" in text, (
        f"{name} does not name the Configuration row, which is what an "
        "operator who has not renamed the file sees turn red"
    )


def test_compose_tells_upgraders_to_rename_the_config_file() -> None:
    """
    The shipped compose template carries the rename in a comment.

    The compose file is the artifact an operator actually has open when they
    pull a new image, and their own copy of it still names the old path. One
    comment line naming both paths is what turns a silent no-config start into
    an instruction.
    """
    renames = [
        f"{COMPOSE.name}:{number}: {line.strip()}"
        for number, line in _numbered(COMPOSE)
        if _is_comment(line)
        if f"./config/{LEGACY_CONFIG_NAME}" in line
        if f"./config/{CONFIG_NAME}" in line
    ]
    assert renames, (
        f"{COMPOSE.name} has no comment line naming both ./config/"
        f"{LEGACY_CONFIG_NAME} and ./config/{CONFIG_NAME}, so an operator "
        "copying this template gets no notice that the old name stopped "
        "being read"
    )


# ---------------------------------------------------------------------------
# Phase 37: the repository-wide sweep guard (CFG-05, D-13)
# ---------------------------------------------------------------------------

# The lines allowed to name the superseded config filename without also naming
# the current one. Each entry is an exact ``(repo-relative path, stripped
# line)`` pair, so widening the exception to a neighbouring line, or letting it
# drift to another file, fails the guard rather than passing quietly. Every
# entry carries the reason it is here.
#
# The line texts are assembled from ``LEGACY_CONFIG_NAME`` for the reason given
# where that constant is defined: the guard below scans this file too, and a
# literal here would make it report its own allowlist.
_LEGACY_NAME_ALLOWED_LINES: frozenset[tuple[str, str]] = frozenset(
    {
        # The one place the superseded name is spelled in shipped source.
        # Detection needs the literal -- without it nothing could name a
        # leftover file -- and the constant exists precisely so that this is
        # the only line which has to carry it. Detecting the old name is not
        # supporting it: the file is stat-ed and never opened.
        (
            "src/saneless/config.py",
            f'LEGACY_CONFIG_FILENAME: Final = "{LEGACY_CONFIG_NAME}"',
        ),
    }
)


def test_no_shipped_file_names_the_legacy_config_file() -> None:
    """
    No tracked file outside ``.planning/`` names the old config file (D-13).

    The sweep is total because a half-swept tree is worse than an unswept one:
    a reader who meets the old name on one page and the new name on another
    has no way to tell which is stale. A line may still carry the old name
    when it also carries the new one, because such a line is a rename
    instruction rather than a leftover -- which is how the upgrade sections
    are written. Anything else is an offender unless it appears verbatim in
    ``_LEGACY_NAME_ALLOWED_LINES``.
    """
    offenders: list[str] = []
    for name in _shipped_files():
        # Two clauses rather than one tuple, for the reason set out on the
        # owner-slug guard above: ruff format rewrites a parenthesised tuple
        # into PEP 758's bracketless form, which the pre-commit
        # debug-statements hook cannot parse on its own older interpreter.
        try:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except OSError:
            continue
        offenders.extend(
            f"{name}:{number}: {line.strip()}"
            for number, line in enumerate(text.splitlines(), start=1)
            if LEGACY_CONFIG_NAME in line
            if CONFIG_NAME not in line
            if (name, line.strip()) not in _LEGACY_NAME_ALLOWED_LINES
        )
    assert not offenders, (
        f"a shipped file still names {LEGACY_CONFIG_NAME} as a saneless "
        f"config path. saneless reads {CONFIG_NAME} in every searched "
        "location and never reads the old name, so a page that still spells "
        "it sends its reader to create a file that is detected and ignored. "
        "Either rename the path, or name both files on the line so it reads "
        "as the rename it is:\n" + "\n".join(offenders)
    )


def test_legacy_name_allowlist_entries_still_exist() -> None:
    """
    Every allowlisted line is still present, verbatim, in the file it names.

    Without this the allowlist rots into a silent pass: the guard above only
    ever *subtracts*, so an entry whose line was reworded, moved or deleted
    goes on excusing something that is not there, and the next line to match
    its text inherits the exemption without anyone deciding to grant it.
    """
    missing: list[str] = []
    for name, expected in sorted(_LEGACY_NAME_ALLOWED_LINES):
        try:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            missing.append(f"{name}: is not UTF-8, so the line cannot be found")
            continue
        except OSError:
            missing.append(f"{name}: cannot be read")
            continue
        if expected not in [line.strip() for line in text.splitlines()]:
            missing.append(f"{name}: {expected}")
    assert not missing, (
        "an entry in _LEGACY_NAME_ALLOWED_LINES no longer matches a line in "
        "the file it exempts, so it excuses nothing while the next line to "
        "match its text would be exempted by accident. Remove the entry, or "
        "correct it to the line that is really there:\n" + "\n".join(missing)
    )


# Every page that enumerates the health checks for a reader. A page that lists
# some of them is worse than one that lists none: the check a reader cannot
# find is the one they conclude does not exist.
CHECK_LISTING_PAGES = (
    DOCS_DIR / "reference" / "cli-commands.md",
    DOCS_DIR / "reference" / "web-api.md",
    DOCS_DIR / "getting-started" / "first-web-ui-scan.md",
)

# Phrases that count the rows in prose. Each was true of the five-row strip and
# is now false, and none of them would be caught by the name check above --
# a page can name all six checks and still tell its reader there are five.
STALE_CHECK_COUNT_PHRASES = (
    "five checks",
    "five rows",
    "among five",
    "other four",
)


def test_docs_that_list_the_checks_name_every_check() -> None:
    """
    Every page listing the checks names all of them, and counts them right.

    The lists are derived from ``CheckKey`` rather than written down here, so
    a seventh check added in a later phase fails this test on every page that
    has not been updated -- which is the only reason the lists agree today.
    The prose count is asserted separately because naming a check and counting
    the checks are two different claims, and this phase falsified the second
    one on three pages while leaving the first one true on two of them.
    """
    expected = [check_name(key) for key in CheckKey]
    offenders: list[str] = []
    for page in CHECK_LISTING_PAGES:
        name = page.relative_to(REPO_ROOT)
        text = page.read_text(encoding="utf-8")
        offenders.extend(
            f"{name}: does not name the {label} check"
            for label in expected
            if label not in text
        )
        lowered = text.lower()
        offenders.extend(
            f"{name}: still says {phrase!r}"
            for phrase in STALE_CHECK_COUNT_PHRASES
            if phrase in lowered
        )
    assert not offenders, (
        "a documentation page disagrees with CheckKey about which checks "
        f"exist. There are {len(expected)} -- {', '.join(expected)} -- and "
        "every page that lists them must list all of them and must not count "
        "them as five:\n" + "\n".join(offenders)
    )


WEB_API_REFERENCE = DOCS_DIR / "reference" / "web-api.md"
FIRST_WEB_UI_SCAN = DOCS_DIR / "getting-started" / "first-web-ui-scan.md"


def test_web_api_reference_states_what_an_unauthenticated_client_can_read() -> None:
    """
    The API reference says what the LAN can read, and where the rest lives.

    The web UI has no login, so the reference is the one place a reader can
    learn what any device on the network is shown: the generic title for
    another browser's scan, and the server-side command that keeps the full
    text.  The generic title is read from ``vocabulary`` so the page and the
    code cannot name it differently.
    """
    text, name = _read(WEB_API_REFERENCE)
    for needle in (
        "What an unauthenticated client can read",
        HIDDEN_JOB_TITLE,
        "saneless jobs --json",
    ):
        assert needle in text, f"{name} does not mention {needle!r}"


def test_first_web_ui_scan_no_longer_says_every_viewer_sees_the_same() -> None:
    """The getting-started page no longer promises every browser the same view."""
    text, name = _read(FIRST_WEB_UI_SCAN)
    assert "is the same for both" not in text, (
        f"{name} still says the title and thumbnail are the same for every viewer"
    )
    assert HIDDEN_JOB_TITLE in text, f"{name} does not name the generic title"


def test_allowed_hosts_docs_warn_against_a_shared_suffix() -> None:
    """
    Both pages that explain ``allowed_hosts`` warn against a shared suffix.

    saneless accepts any suffix with two labels, and it cannot tell a domain
    the operator owns from one where anybody can register a name, such as a
    dynamic DNS service's.  A leading-dot entry for the latter trusts every
    name registered under it, which is DNS rebinding again, so the pages have
    to say so and point at the operator's own full name instead.
    """
    config_text, config_name = _read(CONFIG_REFERENCE)
    deploy_text, deploy_name = _read(DEPLOY_HOWTO)
    sections = {
        f"{config_name} ### Allowed host names": _subsection(
            config_text, "### Allowed host names", config_name
        ),
        f"{deploy_name} ## Running behind a reverse proxy": _section(
            deploy_text, "## Running behind a reverse proxy", deploy_name
        ),
    }
    offenders = [
        f"{where} does not mention {needle!r}"
        for where, body in sections.items()
        for needle in (".duckdns.org", "me.duckdns.org", "register")
        if needle not in body
    ]
    assert not offenders, "\n".join(offenders)


def test_proxy_docs_require_the_original_host_header() -> None:
    """
    The proxy guidance makes passing ``Host`` mandatory, not an alternative.

    saneless never trusts ``X-Forwarded-Host``.  A proxy that sets it and
    replaces ``Host`` with its upstream's name sends a name saneless always
    answers to, which turns the Host check off for every request through the
    proxy, so offering that header as an alternative is wrong advice.
    """
    deploy_text, deploy_name = _read(DEPLOY_HOWTO)
    api_text, api_name = _read(WEB_API_REFERENCE)
    sections = {
        f"{deploy_name} ## Running behind a reverse proxy": _section(
            deploy_text, "## Running behind a reverse proxy", deploy_name
        ),
        f"{api_name} ### Host check": _subsection(api_text, "### Host check", api_name),
        f"{api_name} ### Cross-site requests": _subsection(
            api_text, "### Cross-site requests", api_name
        ),
    }
    offenders = [
        f"{where} still offers X-Forwarded-Host in place of the original Host"
        for where, body in sections.items()
        if "or set `X-Forwarded-Host`" in body
    ]
    offenders.extend(
        f"{where} does not say a replaced Host turns the Host check off"
        for where in list(sections)[:2]
        if "turns the Host check off" not in sections[where]
    )
    assert not offenders, "\n".join(offenders)
