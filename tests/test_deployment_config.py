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

Plain-text assertions only: the contract is what an operator copies, not what a
YAML parser makes of it.
"""

from __future__ import annotations

import re
from pathlib import Path

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
    With no ``config.toml``, the doc says the write lands in ``/app`` (WR-06).

    The CLI writes ``./saneless.toml`` when no config file was loaded, which in
    the image is ``/app/saneless.toml``: in the container layer, and first in
    the search order. The how-to used to imply the write lands in ``./config``.
    """
    text = DEPLOY_HOWTO.read_text(encoding="utf-8")
    name = DEPLOY_HOWTO.relative_to(REPO_ROOT)
    assert "/app/saneless.toml" in text, (
        f"{name} does not say auto-profiles writes /app/saneless.toml "
        "when config.toml is missing"
    )
    assert "touch config/config.toml" in text, (
        f"{name} does not tell container users to create config/config.toml first"
    )


def test_cli_reference_says_where_auto_profiles_writes_in_the_image() -> None:
    """The CLI reference names ``/app/saneless.toml`` for the image (WR-06)."""
    text = CLI_REFERENCE.read_text(encoding="utf-8")
    name = CLI_REFERENCE.relative_to(REPO_ROOT)
    assert "/app/saneless.toml" in text, (
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
