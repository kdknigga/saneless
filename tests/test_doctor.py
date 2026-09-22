"""
Tests for ``saneless doctor``, the CLI half of the shared check registry.

This file exists because ``doctor`` is the command surface of
``saneless.checks``.  The index page shows the six checks to a household
member; ``doctor`` prints the same six, from the same registry and in the same
words, to a shell that can gate on them (APPL-01, D-02).  What each check
*decides* is ``tests/test_checks.py``'s subject.  What this file asserts is the
rendering and the exit code -- the two things that belong to the command.

Three rules get tests of their own, because each is easy to break and expensive
to notice afterwards:

* **D-01** -- a ``WARN`` must not fail a scripted health gate.  ``doctor``
  exits 2 (``ExitCode.CONFIG``) when any check fails and 0 for everything else.
  No new ``ExitCode`` member is added: ``tests/test_deployment_config.py`` pins
  the enum against the documented tables, so a sixth code would break two
  doc-truth tests and Phase 28's D-07 table at once.
* **Amendment A-1** -- ``doctor`` never calls ``require_sane()``.  A machine
  with no python-sane is precisely the machine whose owner needs a diagnosis,
  so the missing import is rendered as one ``FAIL`` row among six rather than
  as a refusal to run at all.  ``scan``, ``devices``, ``serve`` and
  ``auto-profiles`` all refuse; ``doctor`` is the one command that must not.
* **APPL-01/D-14** -- a placeholder paperless-ngx token makes ``doctor`` exit
  non-zero.  That one is exercised through the *real* registry rather than a
  stub, so the wiring between the command and ``run_checks`` is genuinely
  under test at least once.

The state-permutation tests stub ``saneless.cli.run_checks`` with hand-built
results, for the same reason ``tests/test_cli.py`` stubs the scanner backend:
arranging six real checks into a chosen set of states would test the registry,
which already has 91 tests of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from saneless.checks import (
    CheckContext,
    CheckKey,
    CheckResult,
    CheckState,
    check_name,
    configuration_check,
    run_checks,
)
from saneless.cli import (
    _MARKER_WIDTH,
    _SKIPPED_MARKER,
    _row_marker,
    _state_marker,
    cli,
)
from saneless.config import (
    CONFIG_FILENAME,
    LEGACY_CONFIG_FILENAME,
    ConfigDiscovery,
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
    discover_config,
    profile_storage_for_loaded,
)
from saneless.exceptions import ConfigError, PaperlessError
from saneless.job import JobStore
from saneless.scanner.base import DeviceInfo
from saneless.vocabulary import ConnectionStatus, ExitCode, ProfileStorage
from saneless.worker import ScanWorker
from tests.conftest import StubScannerBackend

if TYPE_CHECKING:
    import httpx2

# Every ExitCode member, written out rather than derived, so that adding a
# member to the enum fails here as well as in the two doc-truth tests.  D-01
# maps a red check onto the existing 2 and this is the assertion that says so.
_EXPECTED_EXIT_CODES = {0, 1, 2, 3, 4, 5, 130}

# Every call the CLI made to ``require_sane`` during one invocation.  Amendment
# A-1 is an assertion about a call that must *not* happen, and a silent no-op
# stand-in cannot tell no call from a call that did nothing.
_REQUIRE_SANE_CALLS: list[str] = []

# A configured value ``is_placeholder_token`` accepts, bound to a name with no
# password-ish word in it.  Ruff's S105/S106/S107 read the *name*, not the
# value, and S107 is not among the rules tests waive, so a literal default on a
# parameter called ``token`` is an error the project forbids silencing.
_NOT_A_PLACEHOLDER = "real-token-value"


def _record_require_sane() -> None:
    """Record that something called ``require_sane``, and otherwise do nothing."""
    _REQUIRE_SANE_CALLS.append("called")


class _ReadyScanner(StubScannerBackend):
    """A backend reporting one device, so the scanner check can come out OK."""

    def __init__(self, host: str = "") -> None:
        """Accept the host argument ``SaneBackend`` takes, and ignore it."""

    def get_devices(self) -> list[DeviceInfo]:
        """
        Report one flatbed.

        Returns:
            A single device, named the way a real backend names one.

        """
        return [DeviceInfo("epson:001", "Epson", "ET-4850", "flatbed scanner")]


class _ConnectedPaperless:
    """A Paperless client whose connection test always succeeds."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        """Accept and ignore every constructor argument."""

    def test_connection(
        self, *, timeout: httpx2.Timeout | None = None
    ) -> ConnectionStatus:
        """
        Report a healthy paperless-ngx.

        Args:
            timeout: Accepted because the registry passes its own bound.

        Returns:
            ``ConnectionStatus.CONNECTED``.

        """
        return ConnectionStatus.CONNECTED

    def close(self) -> None:
        """Nothing to close."""


def _make_settings(
    tmp_path: Path, token: str = _NOT_A_PLACEHOLDER, consume_dir: str = ""
) -> Settings:
    """
    Build settings whose directories exist and are writable.

    The data folder is created, because the data-folder check probes it with a
    real write and an absent directory is a legitimate red row that would mask
    whatever the test is actually about.

    Args:
        tmp_path: pytest's per-test directory.
        token: The paperless-ngx token to configure.
        consume_dir: The fallback folder, or "" for none.

    Returns:
        Settings ready for ``_patch_doctor``.

    """
    data_dir = tmp_path / "data"
    scratch_dir = tmp_path / "tmp"
    data_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(
            url="http://localhost:8000",
            token=token,
            consume_dir=consume_dir,
        ),
        output=OutputConfig(
            tmp_dir=str(scratch_dir),
            data_dir=str(data_dir),
            log_file=str(tmp_path / "saneless.log"),
        ),
        profiles={"default": ProfileConfig()},
    )


def _patch_doctor(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    scanner_cls: type | None = None,
    paperless_cls: type | None = None,
) -> CliRunner:
    """
    Patch the CLI's collaborators, following ``tests/test_cli.py``'s idiom.

    ``require_sane`` is replaced with a recorder rather than a no-op: this file
    has a test asserting ``doctor`` never reaches it, and a silent stand-in
    could not tell a call from no call.

    Args:
        monkeypatch: pytest's patcher.
        settings: What ``load_settings`` should return.
        scanner_cls: Stands in for ``SaneBackend``.
        paperless_cls: Stands in for ``PaperlessClient``.

    Returns:
        A runner ready to invoke ``cli``.

    """

    def _fake_load_settings(config_path: str | None = None) -> Settings:
        """Return the test settings, recording ``--config`` as the real one does."""
        if config_path is not None:
            # The real loader records the search alongside the path, and the
            # Configuration row and the resolution table both read the
            # recording.  A fake that set only the path would leave every
            # doctor run reporting a search it never ran.
            explicit = Path(config_path)
            settings._config_path = explicit
            settings._config_discovery = ConfigDiscovery(
                explicit=explicit,
                searched=(),
                found=(explicit,),
                loaded=explicit,
                stale=(),
            )
            return settings
        # No --config: a recording a test attached to these settings stands,
        # and the path follows it, which is the direction the real loader
        # derives them in.  Without one there is nothing to record but an
        # empty search.
        injected = settings.config_discovery
        if injected is None:
            settings._config_discovery = discover_config(())
            settings._config_path = None
        else:
            settings._config_path = injected.loaded
        return settings

    monkeypatch.setattr("saneless.cli.load_settings", _fake_load_settings)
    monkeypatch.setattr(
        "saneless.cli.configure_logging", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("saneless.cli.require_sane", _record_require_sane)
    monkeypatch.setattr("saneless.cli.SaneBackend", scanner_cls or _ReadyScanner)
    monkeypatch.setattr(
        "saneless.cli.PaperlessClient", paperless_cls or _ConnectedPaperless
    )
    _REQUIRE_SANE_CALLS.clear()
    return CliRunner()


def _row(key: CheckKey, state: CheckState, next_step: str = "") -> CheckResult:
    """
    Build one hand-made result.

    Args:
        key: Which check.
        state: How it came out.
        next_step: What to do about it.

    Returns:
        A result the stubbed registry can return.

    """
    return CheckResult(
        key=key,
        state=state,
        message=f"{check_name(key)} message.",
        next_step=next_step,
    )


def _all_ok() -> tuple[CheckResult, ...]:
    """
    Build one OK result per check.

    Returns:
        Six green rows in ``CheckKey`` order.

    """
    return tuple(_row(key, CheckState.OK) for key in CheckKey)


def _all_ok_except(
    key: CheckKey, state: CheckState, next_step: str
) -> tuple[CheckResult, ...]:
    """
    Build one row per check, green but for the one named.

    Generated from ``CheckKey`` rather than listed, so a member added to the
    enum lands in these rows in its own position instead of turning a
    hand-written tuple into an assertion about the wrong row.

    Args:
        key: The check that is not green.
        state: What that check came out as.
        next_step: What its row says to do about it.

    Returns:
        One result per check, in member order.

    """
    return tuple(
        _row(
            member,
            state if member is key else CheckState.OK,
            next_step=next_step if member is key else "",
        )
        for member in CheckKey
    )


def _one_skipped_scanner() -> tuple[CheckResult, ...]:
    """
    Build six rows of which the Scanner one was never probed.

    ``run_checks`` really can produce this -- ``_scanner_skipped`` and
    ``_scanner_busy`` both do -- but no ``doctor`` invocation reaches it today,
    because the command builds its context with ``skip_scanner`` defaulted and
    passes no scanner gate.  The stub is therefore how this row gets in front
    of the printer at all, which is exactly the half-wiring R3-WR-03 found:
    until something rendered it, nothing noticed it rendered wrong.

    Returns:
        Six rows in ``CheckKey`` order, the Scanner one skipped.

    """
    return tuple(
        CheckResult(
            key=key,
            state=CheckState.OK,
            message=f"{check_name(key)} message.",
            skipped=key is CheckKey.SCANNER,
        )
        for key in CheckKey
    )


def _stub_registry(
    monkeypatch: pytest.MonkeyPatch, results: tuple[CheckResult, ...]
) -> None:
    """
    Make ``doctor``'s registry call return ``results``.

    Args:
        monkeypatch: pytest's patcher.
        results: What ``run_checks`` should hand back.

    """
    monkeypatch.setattr("saneless.cli.run_checks", lambda _context: results)


def _lines(output: str) -> list[str]:
    """
    Split command output into non-empty lines.

    Args:
        output: The captured stdout.

    Returns:
        One entry per printed line.

    """
    return [line for line in output.splitlines() if line.strip()]


# The caption that separates the check rows from the config resolution table
# (D-12).  Written out rather than imported so that respelling it in ``cli.py``
# is a visible change here, the way the row wording is pinned in
# ``tests/test_checks.py``.
_TABLE_CAPTION = "Config files searched, in order:"


def _rows(output: str) -> list[str]:
    """
    Split off the check rows: everything printed before the resolution table.

    The rows used to be the whole of ``doctor``'s output, so the assertions
    that count them were written against every printed line.  They are scoped
    here instead of loosened, because "one line per check and nothing else" is
    still the contract for *that* section and the table is what follows it.

    Args:
        output: The captured command output.

    Returns:
        The non-empty lines above the caption.

    """
    lines = _lines(output)
    assert _TABLE_CAPTION in lines, output
    return lines[: lines.index(_TABLE_CAPTION)]


def _table(output: str) -> list[str]:
    """
    Return the resolution table's entries, caption excluded.

    Args:
        output: The captured command output.

    Returns:
        The non-empty lines below the caption.

    """
    lines = _lines(output)
    assert _TABLE_CAPTION in lines, output
    return [line.strip() for line in lines[lines.index(_TABLE_CAPTION) + 1 :]]


def _candidates(tmp_path: Path) -> tuple[Path, ...]:
    """
    Build three search candidates, in the shape ``config_search_paths`` returns.

    Three separate directories, because the superseded-name file is looked for
    beside each candidate and a shared directory would make "which candidate is
    this file beside" unanswerable.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        The candidate paths, in search order.

    """
    directories = (tmp_path / "cwd", tmp_path / "xdg", tmp_path / "etc")
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    return tuple(directory / CONFIG_FILENAME for directory in directories)


def _all_ok_but_real_configuration(settings: Settings) -> tuple[CheckResult, ...]:
    """
    Build green rows for every check but Configuration, which is derived.

    Lets an exit-code test say something about one row without the other five
    being able to decide the answer for it.

    Args:
        settings: The settings whose recorded search the row reports.

    Returns:
        One result per check, in member order.

    """
    return tuple(
        configuration_check(settings, absolute_paths=True)
        if key is CheckKey.CONFIGURATION
        else _row(key, CheckState.OK)
        for key in CheckKey
    )


class TestDoctorHelp:
    """``doctor --help`` is reachable with nothing else working."""

    def test_help_works_with_a_broken_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--help`` never loads settings, so a broken file cannot break it (CFG-10)."""

        def _explode(_config_path: str | None = None) -> Settings:
            msg = "Configuration error in /nope.toml: unreadable"
            raise ConfigError(msg)

        monkeypatch.setattr("saneless.cli.load_settings", _explode)
        result = CliRunner().invoke(cli, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "Check that saneless is ready to scan." in result.output

    def test_json_is_not_an_option(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``--json`` does not ship, so click refuses it (research: no consumer)."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        result = runner.invoke(cli, ["doctor", "--json"])
        assert result.exit_code != 0
        assert "No such option" in result.stderr


class TestDoctorExitCodes:
    """D-01's mapping, one test per state."""

    def test_every_check_ok_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """All green is a healthy appliance and a passing gate."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        _stub_registry(monkeypatch, _all_ok())
        result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0

    def test_a_warning_does_not_fail_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A WARN is a true statement about a deployment that still works (D-01)."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        # Six rows, Configuration included, so the stub mirrors what a real
        # run hands the printer rather than a registry one member short.
        results = (
            _row(CheckKey.CONFIGURATION, CheckState.OK),
            _row(CheckKey.SCANNER, CheckState.OK),
            _row(CheckKey.PAPERLESS, CheckState.OK),
            _row(CheckKey.PROFILES, CheckState.OK),
            _row(CheckKey.FALLBACK, CheckState.WARN, next_step="Set a folder."),
            _row(CheckKey.DATA_DIR, CheckState.OK),
        )
        _stub_registry(monkeypatch, results)
        result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0

    def test_a_failing_check_exits_two(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Any FAIL is "can't scan, fix your setup" -- the existing exit 2."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        results = (
            _row(CheckKey.CONFIGURATION, CheckState.OK),
            _row(CheckKey.SCANNER, CheckState.OK),
            _row(CheckKey.PAPERLESS, CheckState.FAIL, next_step="Fix the token."),
            _row(CheckKey.PROFILES, CheckState.WARN, next_step="Name them."),
            _row(CheckKey.FALLBACK, CheckState.OK),
            _row(CheckKey.DATA_DIR, CheckState.OK),
        )
        _stub_registry(monkeypatch, results)
        result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == ExitCode.CONFIG
        assert result.exit_code == 2

    def test_no_new_exit_code_member_was_added(self) -> None:
        """
        ``doctor`` reuses 2 rather than growing the enum.

        The two doc-truth tests at ``tests/test_deployment_config.py`` compare
        the documented global tables against every member, so a sixth code
        would be a documentation change in three files and a Phase 28 D-07
        revision, for a command that reports a list and can only carry one
        code out of one process anyway.
        """
        assert {int(code) for code in ExitCode} == _EXPECTED_EXIT_CODES


class TestDoctorOutput:
    """The printed table: six rows, in order, each with its marker."""

    def test_all_ok_prints_one_line_per_check(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        One line per check and nothing else above the table -- no header, no summary.

        The rows section is still exactly the checks; the config resolution
        table follows it, which is asserted here rather than left to the
        table's own tests, so this cannot go on passing after the table stops
        being printed at all.
        """
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        _stub_registry(monkeypatch, _all_ok())
        result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert len(_rows(result.output)) == len(CheckKey)
        assert _TABLE_CAPTION in result.output

    def test_rows_are_printed_in_check_key_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Member order is the contract both surfaces render in (D-02)."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        _stub_registry(monkeypatch, _all_ok())
        result = runner.invoke(cli, ["doctor"])
        printed = [line.split("]", 1)[1].split()[0] for line in _rows(result.output)]
        assert printed == [check_name(key).split()[0] for key in CheckKey]

    @pytest.mark.parametrize(
        ("state", "marker"),
        [
            (CheckState.OK, "[ OK ] "),
            (CheckState.WARN, "[WARN] "),
            (CheckState.FAIL, "[FAIL] "),
        ],
    )
    def test_each_row_carries_its_state_marker(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        state: CheckState,
        marker: str,
    ) -> None:
        """The bracketed markers are fixed-width, so the name column lines up."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        next_step = "" if state is CheckState.OK else "Do the thing."
        _stub_registry(
            monkeypatch,
            tuple(_row(key, state, next_step=next_step) for key in CheckKey),
        )
        result = runner.invoke(cli, ["doctor"])
        rows = [line for line in _rows(result.output) if line.startswith("[")]
        assert len(rows) == len(CheckKey)
        assert all(row.startswith(marker) for row in rows)

    def test_the_name_column_is_padded_to_the_longest_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Every message starts at the same column, derived from the longest name."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        _stub_registry(monkeypatch, _all_ok())
        result = runner.invoke(cli, ["doctor"])
        starts = {
            line.index(f"{check_name(key)} message.")
            for key, line in zip(CheckKey, _rows(result.output), strict=True)
        }
        assert len(starts) == 1

    def test_a_warn_row_is_followed_by_its_indented_next_step(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """APPL-04: a row nobody can act on is a row that only gets escalated."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        step = "Set a fallback folder in the saneless config."
        # Built from the enum rather than listed: a member added above
        # FALLBACK moves the row and its step line together.
        results = _all_ok_except(CheckKey.FALLBACK, CheckState.WARN, step)
        _stub_registry(monkeypatch, results)
        result = runner.invoke(cli, ["doctor"])
        lines = _rows(result.output)
        assert len(lines) == len(CheckKey) + 1
        warned = list(CheckKey).index(CheckKey.FALLBACK)
        row, step_line = lines[warned], lines[warned + 1]
        assert step_line.strip() == step
        assert step_line.index(step) == row.index("Fallback message.")

    def test_a_fail_row_is_followed_by_its_indented_next_step(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Red rows carry a remedy for the same reason amber ones do."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        step = "Put a real API token in the saneless config file."
        results = _all_ok_except(CheckKey.PAPERLESS, CheckState.FAIL, step)
        _stub_registry(monkeypatch, results)
        result = runner.invoke(cli, ["doctor"])
        lines = _rows(result.output)
        failed = list(CheckKey).index(CheckKey.PAPERLESS)
        assert lines[failed + 1].strip() == step

    def test_ok_rows_print_no_next_step_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Nothing to do means nothing printed; six green rows are six lines."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        _stub_registry(monkeypatch, _all_ok())
        result = runner.invoke(cli, ["doctor"])
        assert all(line.startswith("[") for line in _rows(result.output))


class TestDoctorConfigResolutionTable:
    """
    D-12: after the rows, where every config file the search looked at ended up.

    The Configuration row says *which situation* the appliance is in, in the
    same words the status strip uses.  This table says *which files*, by
    absolute path, which the row may not carry: the strip is reachable by
    anyone on the LAN and ``doctor`` is not, and the resolved path is the half
    an operator needs to act (D-14).

    ``doctor`` is what gets run on a machine where the log is not to hand, so
    the table is printed on every run and not only when something is wrong --
    a resolution that appears only on failure cannot be compared against a
    working machine's.
    """

    @staticmethod
    def _run(
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        discovery: ConfigDiscovery,
    ) -> list[str]:
        """
        Run ``doctor`` over a recorded search and hand back the table's lines.

        Args:
            monkeypatch: pytest's patcher.
            tmp_path: pytest's per-test directory.
            discovery: The recording to attach to the settings.

        Returns:
            The table entries, caption excluded and stripped.

        """
        settings = _make_settings(tmp_path)
        settings._config_discovery = discovery
        runner = _patch_doctor(monkeypatch, settings)
        _stub_registry(monkeypatch, _all_ok())
        return _table(runner.invoke(cli, ["doctor"]).output)

    def test_the_table_is_separated_from_the_rows_by_a_blank_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Two sections read as two sections, not as a seventh and eighth row."""
        settings = _make_settings(tmp_path)
        settings._config_discovery = discover_config(_candidates(tmp_path))
        runner = _patch_doctor(monkeypatch, settings)
        _stub_registry(monkeypatch, _all_ok())
        printed = runner.invoke(cli, ["doctor"]).output.splitlines()
        caption = printed.index(_TABLE_CAPTION)
        assert printed[caption - 1] == ""
        assert printed[caption - 2].startswith("[")

    def test_every_searched_candidate_is_listed_in_search_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Search order is the whole point: which file won is a question of order."""
        candidates = _candidates(tmp_path)
        lines = self._run(monkeypatch, tmp_path, discover_config(candidates))
        assert [line.split(maxsplit=2)[-1] for line in lines] == [
            str(candidate.absolute()) for candidate in candidates
        ]

    def test_a_candidate_that_does_not_exist_says_not_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A place that was looked at and held nothing is the fact that was missing."""
        candidates = _candidates(tmp_path)
        lines = self._run(monkeypatch, tmp_path, discover_config(candidates))
        assert all(line.startswith("not found ") for line in lines)

    def test_the_loaded_candidate_says_used_and_a_later_one_says_not_used(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Two files, one winner: the loser is present and says why it lost."""
        candidates = _candidates(tmp_path)
        candidates[0].write_text("# the winner\n")
        candidates[2].write_text("# also here\n")
        lines = self._run(monkeypatch, tmp_path, discover_config(candidates))
        assert lines[0] == f"used {candidates[0].absolute()}"
        assert lines[1] == f"not found {candidates[1].absolute()}"
        assert lines[2] == (
            f"not used {candidates[2].absolute()} (an earlier file won)"
        )

    def test_a_stale_only_search_lists_the_old_name_as_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The 2026-09-22 failure, named: the file is there and nothing read it."""
        candidates = _candidates(tmp_path)
        stale = candidates[2].with_name(LEGACY_CONFIG_FILENAME)
        stale.write_text("# left behind\n")
        lines = self._run(monkeypatch, tmp_path, discover_config(candidates))
        assert lines[-1] == (
            f"ignored {stale.absolute()} (old name; rename it to {CONFIG_FILENAME})"
        )
        assert len(lines) == len(candidates) + 1

    def test_a_leftover_beside_a_loaded_file_says_leftover(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        D-17's Docker shape: the new file loaded and the old one is still there.

        It is a different sentence from the stale-only one because it is a
        different situation -- nothing is broken, and the old file may still
        hold the only copy of the URL and token.
        """
        candidates = _candidates(tmp_path)
        candidates[0].write_text("# in use\n")
        stale = candidates[2].with_name(LEGACY_CONFIG_FILENAME)
        stale.write_text("# left behind\n")
        lines = self._run(monkeypatch, tmp_path, discover_config(candidates))
        assert lines[0] == f"used {candidates[0].absolute()}"
        assert lines[-1] == f"leftover {stale.absolute()} (old name; ignored)"

    def test_an_explicit_config_says_no_search_was_done(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``--config`` skips the search, so listing candidates would be a lie."""
        given = tmp_path / "given" / CONFIG_FILENAME
        given.parent.mkdir()
        given.write_text("# handed over\n")
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        _stub_registry(monkeypatch, _all_ok())
        result = runner.invoke(cli, ["--config", str(given), "doctor"])
        assert _table(result.output) == [
            f"used {given.absolute()} (given with --config; no search)"
        ]

    def test_a_search_with_nothing_to_look_at_says_none_recorded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A caption with no entries under it reads as a table that failed to print.

        Settings built directly -- a test fixture, or the web app's own
        construction -- carry no recording, and the same line covers them.
        """
        lines = self._run(monkeypatch, tmp_path, discover_config(()))
        assert lines == ["none recorded"]

    def test_the_paths_line_up_under_one_label_column(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        Every path starts at the same column, whatever its label.

        The widths are derived from the label strings in ``cli.py`` rather than
        written down, so this is the assertion that keeps the derivation
        honest; it is asserted on the unstripped lines because indentation is
        what does the aligning.
        """
        candidates = _candidates(tmp_path)
        candidates[0].write_text("# in use\n")
        candidates[1].write_text("# also here\n")
        stale = candidates[2].with_name(LEGACY_CONFIG_FILENAME)
        stale.write_text("# left behind\n")
        settings = _make_settings(tmp_path)
        settings._config_discovery = discover_config(candidates)
        runner = _patch_doctor(monkeypatch, settings)
        _stub_registry(monkeypatch, _all_ok())
        output = runner.invoke(cli, ["doctor"]).output
        printed = _lines(output)[_lines(output).index(_TABLE_CAPTION) + 1 :]
        assert len(printed) == len(candidates) + 1
        assert len({line.index(str(tmp_path)) for line in printed}) == 1


class TestDoctorExitsOnTheConfigurationRow:
    """
    D-05/D-07: the config situation decides the gate, like every other row.

    A lone superseded-name file is red because nothing the operator wrote was
    read; no file at all is amber because configuring saneless entirely through
    the environment is supported.  Both are asserted through the exit code
    rather than the printed words, because the exit code is what a scripted
    health gate sees.
    """

    def test_a_stale_only_config_exits_two_through_the_real_registry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Unstubbed, so the wiring from the recording to the gate is under test."""
        candidates = _candidates(tmp_path)
        stale = candidates[2].with_name(LEGACY_CONFIG_FILENAME)
        stale.write_text("# left behind\n")
        settings = _make_settings(tmp_path)
        settings._config_discovery = discover_config(candidates)
        runner = _patch_doctor(monkeypatch, settings)

        result = runner.invoke(cli, ["doctor"])

        assert result.exit_code == ExitCode.CONFIG
        rows = [line for line in _rows(result.output) if line.startswith("[")]
        assert len(rows) == len(CheckKey)
        configuration = rows[list(CheckKey).index(CheckKey.CONFIGURATION)]
        assert configuration.startswith(_state_marker(CheckState.FAIL))
        assert f"saneless now reads {CONFIG_FILENAME}" in configuration
        assert str(stale.absolute()) in result.output

    def test_no_config_file_at_all_still_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        An environment-only deployment is a working one, so the gate stays green.

        Every other row is stubbed green so that only the Configuration row can
        decide the answer.
        """
        settings = _make_settings(tmp_path)
        settings._config_discovery = discover_config(_candidates(tmp_path))
        runner = _patch_doctor(monkeypatch, settings)
        _stub_registry(monkeypatch, _all_ok_but_real_configuration(settings))

        result = runner.invoke(cli, ["doctor"])

        assert result.exit_code == 0
        rows = [line for line in _rows(result.output) if line.startswith("[")]
        configuration = rows[list(CheckKey).index(CheckKey.CONFIGURATION)]
        assert configuration.startswith(_state_marker(CheckState.WARN))


class TestASkippedRowIsNotAPassingRow:
    """
    R3-WR-03, the CLI half: ``[ OK ]`` is the token a reader scans for as "fine".

    ``_scanner_skipped`` and ``_scanner_busy`` return ``CheckState.OK`` with
    ``skipped`` set, so a marker derived from the state alone prints
    ``[ OK ] Scanner  Not checked while a scan is running.`` -- a captured
    transcript that says a probe passed when none was taken.  The marker is
    wired even though no current ``doctor`` invocation can produce the row,
    because D-02's claim is that one registry feeds both surfaces and a surface
    that would mis-render a row the registry can build is a latent divergence.
    """

    @pytest.mark.parametrize("state", list(CheckState))
    def test_a_probed_row_still_gets_its_state_marker(self, state: CheckState) -> None:
        """
        With the flag clear, ``_row_marker`` is exactly ``_state_marker``.

        Args:
            state: The state under test.

        """
        result = CheckResult(key=CheckKey.SCANNER, state=state, message="Probed.")
        assert _row_marker(result) == _state_marker(state)

    @pytest.mark.parametrize("state", list(CheckState))
    def test_a_skipped_row_gets_the_skipped_marker(self, state: CheckState) -> None:
        """
        With the flag set, the state is not what the row prints.

        Args:
            state: The state under test.

        """
        result = CheckResult(
            key=CheckKey.SCANNER, state=state, message="Not looked at.", skipped=True
        )
        assert _row_marker(result) == _SKIPPED_MARKER
        assert _row_marker(result) != _state_marker(CheckState.OK)

    def test_the_skipped_marker_is_as_wide_as_the_state_markers(self) -> None:
        """
        A wider token would push one row's name column out of line.

        ``_MARKER_WIDTH`` is what ``_NEXT_STEP_INDENT`` is derived from, so this
        is the assertion that keeps the derivation honest rather than trusting
        two string literals to stay the same length by coincidence.
        """
        assert len(_SKIPPED_MARKER) <= _MARKER_WIDTH
        assert {len(_state_marker(state)) for state in CheckState} == {
            len(_SKIPPED_MARKER)
        }

    def test_the_printed_table_marks_the_skipped_row(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The row the registry skipped prints as skipped, and the rest do not."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        _stub_registry(monkeypatch, _one_skipped_scanner())
        result = runner.invoke(cli, ["doctor"])
        lines = _rows(result.output)
        assert len(lines) == len(CheckKey)
        # Found by key, not by position: the Scanner row stopped being the
        # first one the day Configuration was inserted above it.
        skipped = list(CheckKey).index(CheckKey.SCANNER)
        assert lines[skipped].startswith(f"{_SKIPPED_MARKER} ")
        assert not lines[skipped].startswith(_state_marker(CheckState.OK))
        assert all(
            line.startswith(f"{_state_marker(CheckState.OK)} ")
            for index, line in enumerate(lines)
            if index != skipped
        )

    def test_a_skipped_row_does_not_fail_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        D-01: a probe nobody took is not a red appliance.

        The state stays ``OK`` for exactly this reason, so wiring the marker
        must not have moved the exit-code rule -- a scripted health gate that
        went red for the duration of every scan is what the flag exists to
        avoid.
        """
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        _stub_registry(monkeypatch, _one_skipped_scanner())
        assert runner.invoke(cli, ["doctor"]).exit_code == 0

    def test_the_name_column_still_lines_up(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Every message starts at the same column, skipped row included."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        _stub_registry(monkeypatch, _one_skipped_scanner())
        result = runner.invoke(cli, ["doctor"])
        starts = {
            line.index(f"{check_name(key)} message.")
            for key, line in zip(CheckKey, _rows(result.output), strict=True)
        }
        assert len(starts) == 1


class TestDoctorUsesTheRealRegistry:
    """End-to-end through ``run_checks``, unstubbed."""

    def test_a_placeholder_token_exits_two_and_names_the_row(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """APPL-01/D-14: an unset token is a red appliance, with no HTTP request."""
        settings = _make_settings(tmp_path, token="changeme")
        runner = _patch_doctor(monkeypatch, settings)
        result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == ExitCode.CONFIG
        assert "The paperless-ngx API token has not been set." in result.output

    def test_the_token_value_is_never_printed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ASVS V7: a report that reads secrets must not print them."""
        secret = "super-secret-token-value"
        settings = _make_settings(tmp_path, token=secret)
        runner = _patch_doctor(monkeypatch, settings)
        result = runner.invoke(cli, ["doctor"])
        assert secret not in result.output
        assert secret not in result.stderr

    def test_every_check_runs_against_the_real_registry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Six rows come back from ``run_checks`` itself, not from a stub."""
        settings = _make_settings(tmp_path, consume_dir=str(tmp_path / "data"))
        runner = _patch_doctor(monkeypatch, settings)
        result = runner.invoke(cli, ["doctor"])
        rows = [line for line in _lines(result.output) if line.startswith("[")]
        assert len(rows) == len(CheckKey)
        assert "Connected to paperless-ngx." in result.output


class TestDoctorWithoutPythonSane:
    """Amendment A-1: the machine that most needs a diagnosis still gets one."""

    def test_doctor_never_calls_require_sane(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``scan``'s first statement is exactly what ``doctor`` must not copy."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        _stub_registry(monkeypatch, _all_ok())
        result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert _REQUIRE_SANE_CALLS == []

    def test_a_missing_python_sane_still_prints_five_rows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The import failure is one FAIL row, not a refusal to run."""

        class _NoPythonSane:
            """A backend class whose import of python-sane fails."""

            def __init__(self, host: str = "") -> None:
                """Fail the way a missing package does."""
                msg = "No module named 'sane'"
                raise ImportError(msg)

        runner = _patch_doctor(
            monkeypatch, _make_settings(tmp_path), scanner_cls=_NoPythonSane
        )
        result = runner.invoke(cli, ["doctor"])
        rows = [line for line in _lines(result.output) if line.startswith("[")]
        assert len(rows) == len(CheckKey)
        assert "Scanner support is not installed on this machine." in result.output
        assert result.exit_code == ExitCode.CONFIG

    def test_a_refusing_require_sane_still_prints_five_rows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        ``SaneBackend`` reports the missing import as ``ConfigError``, not ``ImportError``.

        ``require_sane`` translates the ``ImportError`` into a ``ConfigError``
        before ``SaneBackend.__init__`` returns, so catching only ``ImportError``
        would let the guard print ``scan``'s refusal line and exit 2 with no
        rows at all -- which is the exact behaviour Amendment A-1 forbids.
        """

        class _RefusingBackend:
            """A backend class that refuses the way ``require_sane`` does."""

            def __init__(self, host: str = "") -> None:
                """Raise the ConfigError require_sane raises."""
                msg = "python-sane cannot be imported (No module named 'sane')"
                raise ConfigError(msg)

        runner = _patch_doctor(
            monkeypatch, _make_settings(tmp_path), scanner_cls=_RefusingBackend
        )
        result = runner.invoke(cli, ["doctor"])
        rows = [line for line in _lines(result.output) if line.startswith("[")]
        assert len(rows) == len(CheckKey)
        assert "Scanner support is not installed on this machine." in result.output
        assert result.exit_code == ExitCode.CONFIG
        assert "python-sane cannot be imported" not in result.stderr


class TestDoctorKeepsItsDocumentedExitCodes:
    """Nothing ``doctor`` builds may exit with a code its table does not list."""

    def test_an_unbuildable_paperless_client_does_not_exit_three(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A malformed URL is the Paperless row, not exit 3.

        ``PaperlessClient.__init__`` raises ``PaperlessError`` for a URL httpx2
        will not parse, which the group guard would turn into exit 3 -- a code
        ``doctor``'s documented table does not list, and a refusal that would
        cost the operator the other four rows.
        """

        class _UnbuildablePaperless:
            """A client class that refuses its URL."""

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                """Raise the way a malformed URL does."""
                msg = "Paperless URL http://:: is not valid"
                raise PaperlessError(msg)

        runner = _patch_doctor(
            monkeypatch, _make_settings(tmp_path), paperless_cls=_UnbuildablePaperless
        )
        result = runner.invoke(cli, ["doctor"])
        rows = [line for line in _lines(result.output) if line.startswith("[")]
        assert len(rows) == len(CheckKey)
        assert "The paperless-ngx API was not found at that URL." in result.output
        assert result.exit_code == ExitCode.CONFIG


class TestProfilesRowAgreement:
    """
    ``doctor`` and a started ``ScanWorker`` give the Profiles row one answer.

    D-02 promises the two surfaces report the same checks in the same words,
    and ``profile_storage`` is the one field ``checks.py`` is handed rather than
    computing for itself. CR-01 was that field derived twice, with only one copy
    correct: ``doctor`` printed ``[ OK ] Profiles  2 scan profiles configured.``
    while the status strip printed a permanent amber "Generated in memory -- no
    configuration file is in use, so they are lost on restart", for one machine,
    at the same moment.

    The rendered ``CheckResult`` is compared rather than the enum alone,
    because the promise is about the words a household member reads. The enum
    is compared too, so a failure says which half of the contract broke.
    """

    @staticmethod
    def _doctors_storage(
        monkeypatch: pytest.MonkeyPatch, settings: Settings, config_path: Path | None
    ) -> ProfileStorage:
        """
        Run ``doctor`` and hand back the storage value it passed to the registry.

        Read out of the real command rather than recomputed here, so this
        asserts what ``doctor`` does and not what a test thinks it does.

        Args:
            monkeypatch: pytest's patcher.
            settings: The settings ``load_settings`` should return.
            config_path: What to pass as ``--config``, or None for no file.

        Returns:
            The ``ProfileStorage`` member ``doctor`` built its context from.

        """
        captured: list[CheckContext] = []

        def _capture(context: CheckContext) -> tuple[CheckResult, ...]:
            """
            Record the context and answer with six green rows.

            Args:
                context: What ``doctor`` built.

            Returns:
                One OK result per check, so D-01's exit code stays 0.

            """
            captured.append(context)
            return _all_ok()

        runner = _patch_doctor(monkeypatch, settings)
        monkeypatch.setattr("saneless.cli.run_checks", _capture)
        argv = (
            ["doctor"]
            if config_path is None
            else ["--config", str(config_path), "doctor"]
        )
        result = runner.invoke(cli, argv)

        assert result.exit_code == 0
        assert len(captured) == 1
        return captured[0].profile_storage

    @staticmethod
    def _workers_storage(settings: Settings) -> ProfileStorage:
        """
        Start a worker over the same settings and hand back what it recorded.

        Args:
            settings: The settings the worker runs on.

        Returns:
            The ``ProfileStorage`` member the started worker reports.

        """
        store = JobStore()
        worker = ScanWorker(_ReadyScanner(), MagicMock(), settings, store)
        try:
            worker.start()
            stopped = worker.stop()
            assert stopped
            return worker.profile_storage
        finally:
            worker.stop()
            store.close()

    @staticmethod
    def _profiles_row(settings: Settings, storage: ProfileStorage) -> CheckResult:
        """
        Render the Profiles row one surface would show for this storage value.

        Args:
            settings: The loaded configuration.
            storage: What that surface says became of the profiles.

        Returns:
            The single ``CheckKey.PROFILES`` result.

        """
        results = run_checks(
            CheckContext(
                settings=settings,
                scanner=None,
                paperless=None,
                profile_storage=storage,
            )
        )
        return next(result for result in results if result.key is CheckKey.PROFILES)

    @pytest.mark.parametrize("with_config_file", ["yes", "no"])
    def test_both_surfaces_render_the_same_profiles_row(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        with_config_file: str,
    ) -> None:
        """
        One appliance, one Profiles row, whichever surface a reader asks.

        The settings are taken out of the bare default, which is what every
        deployment looks like once ``saneless auto-profiles`` has written the
        file once -- and the exact shape that made the strip lie.

        Args:
            monkeypatch: pytest's patcher.
            tmp_path: pytest's per-test directory.
            with_config_file: "yes" to load a config file, "no" for none.

        """
        settings = _make_settings(tmp_path)
        settings.profiles = {
            "default": ProfileConfig(),
            "adf": ProfileConfig(source="ADF"),
        }
        config_path: Path | None = None
        if with_config_file == "yes":
            config_path = tmp_path / "saneless.toml"
            config_path.write_text("# loaded by --config\n")

        doctors = self._doctors_storage(monkeypatch, settings, config_path)
        workers = self._workers_storage(settings)
        doctors_row = self._profiles_row(settings, doctors)
        workers_row = self._profiles_row(settings, workers)

        assert doctors is workers
        assert doctors_row.state is workers_row.state
        assert doctors_row.message == workers_row.message
        assert doctors_row.next_step == workers_row.next_step

    def test_a_loaded_config_file_is_the_green_row_on_both(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The row CR-01 got wrong, named rather than merely agreed upon.

        Two surfaces can agree and both be wrong, so the value itself is
        pinned: a config file is in use, and the row must say so.
        """
        settings = _make_settings(tmp_path)
        settings.profiles = {
            "default": ProfileConfig(),
            "adf": ProfileConfig(source="ADF"),
        }
        config_path = tmp_path / "saneless.toml"
        config_path.write_text("# loaded by --config\n")

        doctors = self._doctors_storage(monkeypatch, settings, config_path)
        workers = self._workers_storage(settings)
        row = self._profiles_row(settings, workers)

        assert doctors is ProfileStorage.PERSISTED
        assert workers is ProfileStorage.PERSISTED
        assert row.state is CheckState.OK
        assert "in memory" not in row.message.lower()

    def test_doctor_derives_the_row_through_the_shared_function(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A third copy of the rule has nowhere to hide.

        Agreement alone would still be satisfied by two hand-written
        conditionals that happen to match today, which is exactly the shape
        CR-01 grew out of. This asserts the call, so the rule can only be
        changed in one place.
        """
        settings = _make_settings(tmp_path)
        config_path = tmp_path / "saneless.toml"
        config_path.write_text("# loaded by --config\n")
        seen: list[Settings] = []

        def _recording(given: Settings) -> ProfileStorage:
            """
            Record the call and answer as the real derivation does.

            Args:
                given: The settings ``doctor`` asked about.

            Returns:
                Whatever ``profile_storage_for_loaded`` returns.

            """
            seen.append(given)
            return profile_storage_for_loaded(given)

        monkeypatch.setattr("saneless.cli.profile_storage_for_loaded", _recording)
        storage = self._doctors_storage(monkeypatch, settings, config_path)

        assert seen == [settings]
        assert storage is ProfileStorage.PERSISTED
