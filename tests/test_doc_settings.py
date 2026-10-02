"""
One page owns where saneless reads its settings and which source wins.

The configuration reference has a single section that lists the config files
saneless searches, in order, and the rule that a set environment variable
overrides the loaded file, which overrides the built-in defaults. Every other
page links to that section instead of restating it.

The checks below derive the expected list from the loader itself:
``config_search_paths()`` gives the files and their order, and the settings
model gives the environment variable prefix and nesting delimiter. A path
added, dropped or reordered in the code, or a prefix renamed, fails here
rather than leaving a reader with a list that no longer matches what runs.
Each checker returns its offences as strings, and a seeded bad section proves
it can fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from saneless.config import (
    CONFIG_FILENAME,
    Settings,
    config_search_paths,
    load_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_REFERENCE = REPO_ROOT / "docs" / "reference" / "configuration.md"
ENV_REFERENCE = REPO_ROOT / "docs" / "reference" / "environment-variables.md"

CANONICAL_HEADING = "## Where saneless reads settings"

# The explicit flag always comes first: it bypasses the search entirely.
EXPLICIT_FLAG = "`--config PATH`"

# How the page spells the per-user location: the variable, not a home path.
XDG_SPELLING = "$XDG_CONFIG_HOME"

_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_NUMBERED_ITEM = re.compile(r"^\d+\.\s+(.*)$")
_LEADING_CODE_SPAN = re.compile(r"^`([^`]+)`")
_CODE_SPAN = re.compile(r"`([^`]+)`")


def _mkdocs_slug(heading: str) -> str:
    """Slug a heading the way Python-Markdown's default ``toc`` does."""
    text = re.sub(r"[^\w\s-]", "", heading).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def _env_prefix() -> str:
    """Return the environment variable prefix the settings model reads."""
    prefix = Settings.model_config.get("env_prefix")
    assert isinstance(prefix, str)
    assert prefix
    return prefix


def _env_delimiter() -> str:
    """Return the nesting delimiter the settings model splits variable names on."""
    delimiter = Settings.model_config.get("env_nested_delimiter")
    assert isinstance(delimiter, str)
    assert delimiter
    return delimiter


def _settings_section(text: str, name: str = str(CONFIG_REFERENCE)) -> str:
    """
    Return the body under the canonical heading, up to the next ``## `` heading.

    The heading must be a whole line, so a longer heading that merely starts
    with it does not count.
    """
    match = re.search(rf"^{re.escape(CANONICAL_HEADING)}$", text, re.MULTILINE)
    assert match, f"{name} has no {CANONICAL_HEADING!r} section"
    return text[match.end() :].split("\n## ", 1)[0]


def _numbered_lists(text: str) -> list[list[str]]:
    """
    Return each numbered Markdown list in ``text`` as its items' text.

    Fenced code is skipped. Blank lines do not end a list; any other line
    that is not a numbered item does.
    """
    lists: list[list[str]] = []
    current: list[str] = []
    for line in _FENCE.sub("", text).splitlines():
        item = _NUMBERED_ITEM.match(line)
        if item:
            current.append(item.group(1))
        elif line.strip() and current:
            lists.append(current)
            current = []
    if current:
        lists.append(current)
    return lists


def _documented_spellings(xdg_home: Path) -> list[str]:
    """
    Map the loader's search paths to the spellings the page uses.

    ``xdg_home`` is the sentinel ``$XDG_CONFIG_HOME`` the paths were rendered
    under, so a path beneath it is written with the variable. A relative path
    is written from the working directory.
    """
    spellings: list[str] = []
    for path in config_search_paths():
        if not path.is_absolute():
            spellings.append(f"./{path.as_posix()}")
        elif path.is_relative_to(xdg_home):
            spellings.append(f"{XDG_SPELLING}/{path.relative_to(xdg_home).as_posix()}")
        else:
            spellings.append(path.as_posix())
    return spellings


def _search_path_offenders(section: str, spellings: list[str]) -> list[str]:
    """
    Report where the section's search list differs from the loader's.

    The list is the numbered list whose first item is the explicit flag. Its
    remaining items must name ``spellings`` as leading code spans, all of
    them, in the same order, and nothing else.
    """
    search = [
        items
        for items in _numbered_lists(section)
        if items[0].startswith(EXPLICIT_FLAG)
    ]
    if not search:
        return [f"no numbered list whose first item is {EXPLICIT_FLAG}"]
    documented: list[str] = []
    for item in search[0][1:]:
        span = _LEADING_CODE_SPAN.match(item)
        documented.append(span.group(1) if span else item)
    offenders = [
        f"{spelling} is not in the search list"
        for spelling in spellings
        if spelling not in documented
    ]
    offenders.extend(
        f"{entry} is listed but the loader does not search it"
        for entry in documented
        if entry not in spellings
    )
    listed = [entry for entry in documented if entry in spellings]
    expected = [spelling for spelling in spellings if spelling in documented]
    if listed != expected:
        offenders.append(
            f"search list order is {listed}, the loader searches {expected}"
        )
    return offenders


def _precedence_list(section: str) -> list[str] | None:
    """Return the numbered list whose first item names the env prefix, if any."""
    prefix = _env_prefix()
    for items in _numbered_lists(section):
        if prefix in items[0]:
            return items
    return None


def _precedence_offenders(section: str) -> list[str]:
    """
    Report where the section's precedence list differs from the loader's rule.

    The rule is an ordered list of three sources: an environment variable,
    named with the model's prefix and nesting delimiter, then the loaded
    config file, then the built-in defaults.
    """
    prefix = _env_prefix()
    delimiter = _env_delimiter()
    items = _precedence_list(section)
    if items is None:
        return [f"no numbered list whose first item names {prefix}"]
    offenders: list[str] = []
    if len(items) != 3:
        offenders.append(f"the precedence list has {len(items)} items, not 3")
    if not any(
        span.startswith(prefix) and delimiter in span
        for span in _CODE_SPAN.findall(items[0])
    ):
        offenders.append(
            f"item 1 does not show a variable as {prefix}<SECTION>{delimiter}<FIELD>"
        )
    if len(items) > 1 and CONFIG_FILENAME not in items[1]:
        offenders.append(f"item 2 is not the loaded {CONFIG_FILENAME}")
    if len(items) > 2 and "default" not in items[2].lower():
        offenders.append("item 3 is not the built-in defaults")
    return offenders


def _restated_precedence_offenders(text: str) -> list[str]:
    """Report a page that carries its own copy of the precedence list."""
    items = _precedence_list(text)
    if items is None:
        return []
    return [f"restates the precedence list: {items}"]


def _canonical_link_offenders(text: str) -> list[str]:
    """Report a page that does not link the canonical section by its slug."""
    target = f"configuration.md#{_mkdocs_slug(CANONICAL_HEADING.lstrip('# '))}"
    if f"({target})" in text:
        return []
    return [f"no link to {target}"]


@pytest.fixture
def spellings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Render the loader's search list under a sentinel ``$XDG_CONFIG_HOME``."""
    xdg_home = tmp_path / "sentinel-xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))
    return _documented_spellings(xdg_home)


def test_spellings_cover_every_search_path(spellings: list[str]) -> None:
    """
    Each searched path maps to a distinct spelling, with the XDG one by name.

    Guards the mapping itself: a path that fell through to its sentinel
    rendering would be compared against nothing the page could say.
    """
    assert len(spellings) == len(config_search_paths())
    assert len(set(spellings)) == len(spellings)
    assert any(spelling.startswith(f"{XDG_SPELLING}/") for spelling in spellings)
    assert all("sentinel-xdg-config" not in spelling for spelling in spellings)


def test_configuration_reference_lists_the_loader_search_order(
    spellings: list[str],
) -> None:
    """The canonical section lists the files the loader searches, in its order."""
    section = _settings_section(CONFIG_REFERENCE.read_text(encoding="utf-8"))
    offenders = _search_path_offenders(section, spellings)
    assert not offenders, "\n".join(offenders)


def test_configuration_reference_states_the_precedence_rule() -> None:
    """The canonical section says environment beats file beats defaults."""
    section = _settings_section(CONFIG_REFERENCE.read_text(encoding="utf-8"))
    offenders = _precedence_offenders(section)
    assert not offenders, "\n".join(offenders)


def test_loader_applies_the_documented_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The loader really does put a set variable over the file over the default.

    The page's ordered list is only true while this holds, so a change in
    source order fails here as well as in the page check.
    """
    variable = f"{_env_prefix()}SCANNER{_env_delimiter()}HOST"
    monkeypatch.delenv(variable, raising=False)
    bare = tmp_path / "bare.toml"
    bare.write_text("", encoding="utf-8")
    default = load_settings(str(bare)).scanner.host
    config = tmp_path / CONFIG_FILENAME
    config.write_text('[scanner]\nhost = "from-file"\n', encoding="utf-8")
    assert default != "from-file"
    assert load_settings(str(config)).scanner.host == "from-file"
    monkeypatch.setenv(variable, "from-env")
    assert load_settings(str(config)).scanner.host == "from-env"


def test_environment_reference_links_the_canonical_section() -> None:
    """
    The environment reference links the rule rather than restating it.

    A second copy of the precedence list is one more place for it to go stale.
    """
    text = ENV_REFERENCE.read_text(encoding="utf-8")
    offenders = _canonical_link_offenders(text) + _restated_precedence_offenders(text)
    assert not offenders, "\n".join(offenders)


# The real page's shape, with one thing wrong in each.
_SEEDED_SEARCH_SECTIONS = {
    "out of order": (
        "1. `--config PATH` -- explicit\n"
        "2. `/etc/saneless/saneless.toml` -- system\n"
        "3. `./saneless.toml` -- working directory\n"
        "4. `$XDG_CONFIG_HOME/saneless/saneless.toml` -- per user\n"
    ),
    "path dropped": (
        "1. `--config PATH` -- explicit\n"
        "2. `./saneless.toml` -- working directory\n"
        "3. `/etc/saneless/saneless.toml` -- system\n"
    ),
    "path the loader does not search": (
        "1. `--config PATH` -- explicit\n"
        "2. `./saneless.toml` -- working directory\n"
        "3. `$XDG_CONFIG_HOME/saneless/saneless.toml` -- per user\n"
        "4. `/etc/saneless/saneless.toml` -- system\n"
        "5. `/usr/local/etc/saneless.toml` -- another\n"
    ),
    "flag not first": (
        "1. `./saneless.toml` -- working directory\n"
        "2. `$XDG_CONFIG_HOME/saneless/saneless.toml` -- per user\n"
        "3. `/etc/saneless/saneless.toml` -- system\n"
    ),
}


@pytest.mark.parametrize(
    "section", _SEEDED_SEARCH_SECTIONS.values(), ids=list(_SEEDED_SEARCH_SECTIONS)
)
def test_seeded_search_list_is_reported(section: str, spellings: list[str]) -> None:
    """A search list that drops, adds or reorders a path is an offence."""
    assert _search_path_offenders(section, spellings)


def test_seeded_correct_search_list_passes(spellings: list[str]) -> None:
    """The checker accepts the loader's own list, so its failures mean something."""
    section = "\n".join(
        [f"1. {EXPLICIT_FLAG} -- explicit"]
        + [
            f"{index}. `{spelling}` -- searched"
            for index, spelling in enumerate(spellings, start=2)
        ]
    )
    assert not _search_path_offenders(section, spellings)


_SEEDED_PRECEDENCE_SECTIONS = {
    "prefix missing": (
        "1. Environment variables (`APP_<SECTION>__<FIELD>`)\n"
        "2. The loaded `saneless.toml`\n"
        "3. Built-in defaults\n"
    ),
    "delimiter missing": (
        "1. Environment variables (`SANELESS_<SECTION>.<FIELD>`)\n"
        "2. The loaded `saneless.toml`\n"
        "3. Built-in defaults\n"
    ),
    "file over environment": (
        "1. The loaded `saneless.toml` (not `SANELESS_*`)\n"
        "2. Environment variables (`SANELESS_<SECTION>__<FIELD>`)\n"
        "3. Built-in defaults\n"
    ),
    "defaults over file": (
        "1. Environment variables (`SANELESS_<SECTION>__<FIELD>`)\n"
        "2. Built-in defaults\n"
        "3. The loaded `saneless.toml`\n"
    ),
}


@pytest.mark.parametrize(
    "section",
    _SEEDED_PRECEDENCE_SECTIONS.values(),
    ids=list(_SEEDED_PRECEDENCE_SECTIONS),
)
def test_seeded_precedence_list_is_reported(section: str) -> None:
    """A precedence list without the prefix, or in the wrong order, is an offence."""
    assert _precedence_offenders(section)


def test_seeded_correct_precedence_list_passes() -> None:
    """The checker accepts a list in the loader's order."""
    prefix = _env_prefix()
    delimiter = _env_delimiter()
    section = (
        f"1. Environment variables (`{prefix}<SECTION>{delimiter}<FIELD>`)\n"
        f"2. The loaded `{CONFIG_FILENAME}`\n"
        "3. Built-in defaults\n"
    )
    assert not _precedence_offenders(section)


def test_seeded_restated_precedence_is_reported() -> None:
    """A page with its own precedence list and no link is reported twice."""
    text = (
        "## Priority\n\n"
        "1. Environment variables (`SANELESS_*`)\n"
        "2. TOML config file\n"
        "3. Built-in defaults\n"
    )
    assert _restated_precedence_offenders(text)
    assert _canonical_link_offenders(text)


def test_seeded_missing_heading_is_reported() -> None:
    """A page without the canonical heading fails loudly, naming the page."""
    with pytest.raises(AssertionError, match="Where saneless reads settings"):
        _settings_section("# Configuration\n\n## Config File Search Path\n", "page")
