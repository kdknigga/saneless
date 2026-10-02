"""
The CLI samples in the docs are what the CLI prints for the documented device.

A hand-typed device table drifts the moment the column sizing changes, and a
hand-typed ``auto-profiles`` run drifts the moment profile naming does. These
tests hold only the inputs as literals -- the device each page documents and
the capabilities it reports -- and render every sample through the real
command, so a doc block that no longer matches the program fails here.

They also hold First CLI Scan to the order a reader can succeed with:
profiles are generated after the config file exists, so they land in the file
the next command loads, and before the first scan, so a scanner with no glass
is never asked for a source it does not have.
"""

from __future__ import annotations

import itertools
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from saneless.auto_profiles import generate_profiles, write_profiles_to_config
from saneless.cli import cli
from saneless.config import CONFIG_FILENAME, load_settings
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    SourceKind,
    classify_source,
)
from tests.conftest import StubScannerBackend

if TYPE_CHECKING:
    from collections.abc import Sequence

    from saneless.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
TUTORIAL = REPO_ROOT / "docs" / "getting-started" / "first-cli-scan.md"
CLI_REFERENCE = REPO_ROOT / "docs" / "reference" / "cli-commands.md"

# The main example: an all-in-one on the hpaio backend, reached through saned.
# hpaio names its sources only "Auto" and "ADF"; the glass is behind "Auto".
HP_3030 = DeviceInfo(
    "net:192.168.1.50:hpaio:/usb/hp_LaserJet_3030?serial=00MXBM121742",
    "Hewlett-Packard",
    "hp_LaserJet_3030",
    "all-in-one",
)
HP_3030_CAPS = DeviceCapabilities(
    sources=["Auto", "ADF"],
    resolutions=[75, 100, 150, 200, 300, 600],
    modes=["Lineart", "Gray", "Color"],
)

# The aside's example: a sheet-fed scanner with no glass at all.
FI_7160 = DeviceInfo(
    "net:192.168.1.50:fujitsu:fi-7160:12345",
    "FUJITSU",
    "fi-7160",
    "scanner",
)
FI_7160_CAPS = DeviceCapabilities(
    sources=["ADF Front", "ADF Back", "ADF Duplex"],
    resolutions=[50, 100, 150, 200, 300, 400, 600],
    modes=["Lineart", "Halftone", "Gray", "Color"],
)

# The width the samples are rendered for; the table sizes itself to it.
COLUMNS = "80"

# The admonition that holds the sheet-fed example in First CLI Scan.
NO_GLASS_ASIDE = '!!! note "If your scanner has no glass"'
# The lines that introduce the reference page's samples.
DEVICES_TABLE_MARKER = "**Example output (table):**"
AUTO_PROFILES_MARKER = "**Example output (first run):**"

# The service the shipped compose file defines, and the program in the image.
COMPOSE_EXEC = "docker compose exec saneless saneless"

TAB_LINE = re.compile(r'^=== "(?P<label>[^"]+)"\s*$')
PROFILES_IN = re.compile(r"^\s*Profiles in (?P<path>\S+):$", re.MULTILINE)
PROFILE_OPTION = re.compile(r"--profile(?:=|\s+)(?P<name>\S+)")


def _scanner_for(device: DeviceInfo, caps: DeviceCapabilities) -> type:
    """Build a scanner double that reports one device and its capabilities."""

    class _DocumentedScanner(StubScannerBackend):
        """The scanner the doc page describes."""

        def __init__(self, host: str = "") -> None:
            """Accept the host the CLI passes."""

        def get_devices(self) -> list[DeviceInfo]:
            """Report the documented device."""
            return [device]

        def get_capabilities(self, device_id: str) -> DeviceCapabilities:
            """Report the documented device's capabilities."""
            return caps

    return _DocumentedScanner


def _write_config() -> Path:
    """
    Write the tutorial's minimal config in the working directory.

    The autouse ``hermetic_env`` fixture makes the working directory the
    test's own, so this is the file the real loader finds first.

    Returns:
        The absolute path of the file written.

    """
    path = Path.cwd() / CONFIG_FILENAME
    path.write_text(
        '[scanner]\nhost = "192.168.1.50"\n\n'
        '[paperless]\nurl = "http://192.168.1.50:8000"\ntoken = "doc-sample"\n',
        encoding="utf-8",
    )
    return path.resolve()


def _render(
    device: DeviceInfo,
    caps: DeviceCapabilities,
    args: Sequence[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    shown_path: str = "",
) -> tuple[str, str]:
    """
    Run the real CLI against the documented device and return its output.

    Only what a test cannot have is replaced: the python-sane check, the log
    handler and SANE itself. The config is a real file loaded by the real
    loader, so ``auto-profiles`` writes where it would for the reader.

    Args:
        device: The device the scanner double reports.
        caps: The capabilities the scanner double reports.
        args: The command line after ``saneless``.
        monkeypatch: Replaces the names in ``saneless.cli``.
        shown_path: The path the doc prints in place of the real config path.

    Returns:
        The command's stdout and stderr, in that order.

    """
    if not (Path.cwd() / CONFIG_FILENAME).exists():
        _write_config()
    config_path = (Path.cwd() / CONFIG_FILENAME).resolve()

    def configure_logging(*_args: object, **_kwargs: object) -> bool:
        """Attach no handler; report that no log file was attached."""
        return False

    def require_sane() -> None:
        """Skip the python-sane import check; no hardware is involved."""

    monkeypatch.setattr("saneless.cli.require_sane", require_sane)
    monkeypatch.setattr("saneless.cli.configure_logging", configure_logging)
    monkeypatch.setattr("saneless.cli.SaneBackend", _scanner_for(device, caps))

    result = CliRunner().invoke(cli, list(args), env={"COLUMNS": COLUMNS})

    assert result.exit_code == 0, (result.stdout, result.stderr)
    stdout = result.stdout
    if shown_path:
        stdout = stdout.replace(str(config_path), shown_path)
    return stdout, result.stderr


def _fenced_blocks_after(
    text: str, marker: str, *, lang: str | None = None
) -> list[str]:
    """
    Return the fenced blocks after the line that reads ``marker``, in order.

    A fence may be indented, as it is inside a tab or an admonition; its own
    indentation is removed from every line of the body.

    Args:
        text: The page.
        marker: The whole stripped text of the line to start after.
        lang: The fence's info string to keep; every fence when ``None``.

    Returns:
        Each kept block's body, without the fences.

    """
    lines = text.splitlines()
    starts = [n for n, line in enumerate(lines) if line.strip() == marker]
    assert starts, f"no line reading {marker!r}"
    return [
        "\n".join(row.removeprefix(indent) for row in body)
        for _, info, indent, body in _fences(lines[starts[0] + 1 :])
        if lang is None or info == lang
    ]


def _fenced_after(text: str, marker: str, *, lang: str | None = None) -> str:
    """Return the first fenced block after the line that reads ``marker``."""
    blocks = _fenced_blocks_after(text, marker, lang=lang)
    assert blocks, f"no {lang or 'fenced'} block after {marker!r}"
    return blocks[0]


def _placeholder_path() -> str:
    """
    Return the one path the docs print in place of the real config path.

    Every ``Profiles in …`` sample on the two pages must use the same
    placeholder, so a reader comparing them sees one file.

    """
    paths = {
        match["path"]
        for page in (TUTORIAL, CLI_REFERENCE)
        for match in PROFILES_IN.finditer(page.read_text(encoding="utf-8"))
    }
    assert len(paths) == 1, f"the samples name different config paths: {paths}"
    (path,) = paths
    assert path.startswith("/"), path
    assert path.endswith(f"/{CONFIG_FILENAME}"), path
    return path


def _reader_paths(text: str) -> dict[str, list[str]]:
    """
    Split a tabbed page into what a reader of each tab reads, in order.

    A reader of the "Docker" tabs reads every untabbed line and the content of
    every tab labelled "Docker", and none of the other tabs. A page without
    tabs has one reader, keyed by the empty string.

    Returns:
        Each tab label's lines, with the tab indentation removed.

    """
    tagged: list[tuple[str | None, str]] = []
    current: str | None = None
    for line in text.splitlines():
        tab = TAB_LINE.match(line)
        if tab:
            current = tab["label"]
            continue
        if current is not None and (not line.strip() or line.startswith("    ")):
            tagged.append((current, line.removeprefix("    ")))
            continue
        current = None
        tagged.append((None, line))
    labels = {label for label, _ in tagged if label is not None}
    if not labels:
        return {"": [line for _, line in tagged]}
    return {
        label: [line for owner, line in tagged if owner in {None, label}]
        for label in sorted(labels)
    }


def _fences(lines: list[str]) -> list[tuple[int, str, str, list[str]]]:
    """Return each fenced block as (opening index, info, indent, raw body)."""
    found = []
    number = 0
    while number < len(lines):
        line = lines[number]
        if line.strip().startswith("```"):
            closes = [
                n for n in range(number + 1, len(lines)) if lines[n].strip() == "```"
            ]
            assert closes, f"unclosed fence: {line.strip()}"
            indent = line[: len(line) - len(line.lstrip())]
            body = lines[number + 1 : closes[0]]
            found.append((number, line.strip()[3:].strip(), indent, body))
            number = closes[0] + 1
            continue
        number += 1
    return found


def _commands(lines: list[str]) -> list[tuple[int, str]]:
    """Return every shell command line in the reader's path, with its index."""
    return [
        (start, line.strip())
        for start, info, _, body in _fences(lines)
        if info in {"bash", "sh", "shell", "console"}
        for line in body
        if line.strip() and not line.strip().startswith("#")
    ]


def _runs_saneless(command: str) -> bool:
    """Whether a command line runs one of the saneless subcommands."""
    words = shlex.split(command)
    return any(
        word == "saneless" and following in cli.commands
        for word, following in itertools.pairwise(words)
    )


def _glass_profiles(settings: Settings) -> set[str]:
    """Return the profiles that scan one page from the glass."""
    glass = set()
    for name, profile in settings.profiles.items():
        kind = classify_source(profile.source)
        if kind is SourceKind.FLATBED or (
            kind is SourceKind.AUTO and profile.auto_source_mode == "flatbed"
        ):
            glass.add(name)
    return glass


def _assert_devices_block(block: str, stdout: str, stderr: str) -> None:
    """Assert the block is the stderr status line above the stdout table."""
    assert stderr.splitlines(), "devices printed no status line on stderr"
    assert block.splitlines() == stderr.splitlines() + stdout.splitlines(), (
        "the devices sample is not what the CLI prints:\n"
        + stderr
        + stdout
        + "\n---- doc ----\n"
        + block
    )


def test_tutorial_devices_sample_is_the_cli_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First CLI Scan's devices sample is the HP 3030 as the CLI lists it."""
    stdout, stderr = _render(HP_3030, HP_3030_CAPS, ["devices"], monkeypatch)
    text = TUTORIAL.read_text(encoding="utf-8")

    block = _fenced_after(text, "saneless devices", lang="text")

    _assert_devices_block(block, stdout, stderr)


def test_reference_devices_sample_is_the_cli_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI reference's devices table is the HP 3030 as the CLI lists it."""
    stdout, stderr = _render(HP_3030, HP_3030_CAPS, ["devices"], monkeypatch)
    text = CLI_REFERENCE.read_text(encoding="utf-8")

    block = _fenced_after(text, DEVICES_TABLE_MARKER)

    _assert_devices_block(block, stdout, stderr)


@pytest.mark.parametrize(
    ("page", "marker"),
    [(TUTORIAL, "saneless auto-profiles"), (CLI_REFERENCE, AUTO_PROFILES_MARKER)],
    ids=["tutorial", "reference"],
)
def test_auto_profiles_sample_is_the_cli_output(
    monkeypatch: pytest.MonkeyPatch, page: Path, marker: str
) -> None:
    """The HP 3030's auto-profiles sample is the CLI's first run, whole."""
    stdout, stderr = _render(
        HP_3030,
        HP_3030_CAPS,
        ["auto-profiles"],
        monkeypatch,
        shown_path=_placeholder_path(),
    )

    block = _fenced_after(page.read_text(encoding="utf-8"), marker, lang="text")

    assert stderr == ""
    assert block.splitlines() == stdout.splitlines(), (
        f"{page.name}: the auto-profiles sample is not what the CLI prints:\n" + stdout
    )


def test_no_glass_aside_is_the_cli_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sheet-fed aside shows the fi-7160's devices row and its profiles."""
    text = TUTORIAL.read_text(encoding="utf-8")
    blocks = _fenced_blocks_after(text, NO_GLASS_ASIDE, lang="text")
    assert len(blocks) >= 2, "the aside has no devices and auto-profiles samples"
    devices_block, profiles_block = blocks[:2]

    table, _ = _render(FI_7160, FI_7160_CAPS, ["devices"], monkeypatch)
    profiles, _ = _render(
        FI_7160,
        FI_7160_CAPS,
        ["auto-profiles"],
        monkeypatch,
        shown_path=_placeholder_path(),
    )

    assert devices_block.splitlines() == table.splitlines(), table
    assert profiles_block.splitlines() == profiles.splitlines(), profiles


def test_no_glass_aside_names_the_feeder_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The aside names the source the sheet-fed scanner's default feeds from."""
    _render(FI_7160, FI_7160_CAPS, ["auto-profiles"], monkeypatch)
    settings = load_settings(str(Path.cwd() / CONFIG_FILENAME))
    default_source = settings.profiles["default"].source
    text = TUTORIAL.read_text(encoding="utf-8")
    assert NO_GLASS_ASIDE in text, "the tutorial has no sheet-fed aside"
    aside = text.split(NO_GLASS_ASIDE, 1)[1].split("\n## ", 1)[0]

    assert classify_source(default_source).uses_feeder, default_source
    assert not _glass_profiles(settings), settings.profiles
    assert f"`{default_source}`" in aside


def test_tutorial_scans_from_the_glass_with_a_glass_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The profile the tutorial scans with reads the HP 3030's glass.

    The hpaio backend offers no "Flatbed" source, so the glass is reached
    through "Auto" scanned as a single page; the profile the scan step uses
    and the one its glass sentence names must be one of those.
    """
    _render(HP_3030, HP_3030_CAPS, ["auto-profiles"], monkeypatch)
    settings = load_settings(str(Path.cwd() / CONFIG_FILENAME))
    glass = _glass_profiles(settings)
    text = TUTORIAL.read_text(encoding="utf-8")

    scans = [
        command
        for lines in _reader_paths(text).values()
        for _, command in _commands(lines)
        if _runs_saneless(command) and re.search(r"\bsaneless scan\b", command)
    ]
    assert scans, "the tutorial runs no scan"
    for command in scans:
        option = PROFILE_OPTION.search(command)
        used = option["name"] if option else "default"
        assert used in glass, f"{command!r} scans with {used!r}, not {glass}"

    glass_lines = [
        line
        for line in text.splitlines()
        if "glass" in line and any(f"`{name}`" in line for name in glass)
    ]
    assert glass_lines, f"no sentence names {sorted(glass)} as the glass profile"


def test_tutorial_generates_profiles_between_config_and_first_scan() -> None:
    """
    In every tab, auto-profiles runs after the config exists and before a scan.

    Run first, it would write a per-user file that the tutorial's own config
    file then shadows, and the profiles would silently vanish; run after the
    scan, the scan would use the bare default on a scanner that may have no
    glass.
    """
    paths = _reader_paths(TUTORIAL.read_text(encoding="utf-8"))
    assert "Docker" in paths, sorted(paths)

    for label, lines in paths.items():
        configs = [
            start
            for start, info, _, body in _fences(lines)
            if info == "toml" and any(row.strip() == "[paperless]" for row in body)
        ]
        commands = _commands(lines)
        generate = [
            n for n, c in commands if _runs_saneless(c) and "auto-profiles" in c
        ]
        scan = [
            n
            for n, c in commands
            if _runs_saneless(c) and re.search(r"\bsaneless scan\b", c)
        ]
        assert configs, f"{label}: no step creates the config file"
        assert generate, f"{label}: no auto-profiles step"
        assert scan, f"{label}: no scan step"
        assert configs[0] < generate[0] < scan[0], (
            f"{label}: auto-profiles must come after the config file "
            "and before the first scan"
        )


def test_docker_tab_runs_every_command_through_compose() -> None:
    """A reader of the Docker tabs runs saneless inside the compose service."""
    paths = _reader_paths(TUTORIAL.read_text(encoding="utf-8"))
    commands = [command for _, command in _commands(paths["Docker"])]

    offenders = [
        command
        for command in commands
        if _runs_saneless(command) and not command.startswith(f"{COMPOSE_EXEC} ")
    ]

    assert commands, "the Docker tabs run no command"
    assert not offenders, "Docker tab commands outside compose:\n" + "\n".join(
        offenders
    )


def test_docker_tab_shows_the_run_after_startup_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The Docker tab shows what auto-profiles prints after the server started.

    In the compose deployment the server generates and saves profiles when it
    starts with a config holding only the bare default, so the reader's run
    finds them already there.
    """
    config_path = _write_config()
    write_profiles_to_config(
        config_path, generate_profiles(HP_3030_CAPS, HP_3030.device_type)
    )
    stdout, _ = _render(HP_3030, HP_3030_CAPS, ["auto-profiles"], monkeypatch)
    skipped = [line for line in stdout.splitlines() if line.startswith("Skipped")]
    docker = "\n".join(_reader_paths(TUTORIAL.read_text(encoding="utf-8"))["Docker"])

    assert len(skipped) == 1, stdout
    assert skipped[0] in docker, skipped[0]
