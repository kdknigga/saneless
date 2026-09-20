"""
Tests for ``saneless doctor``, the CLI half of the shared check registry.

This file exists because ``doctor`` is the command surface of
``saneless.checks``.  The index page shows the five checks to a household
member; ``doctor`` prints the same five, from the same registry and in the same
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
  so the missing import is rendered as one ``FAIL`` row among five rather than
  as a refusal to run at all.  ``scan``, ``devices``, ``serve`` and
  ``auto-profiles`` all refuse; ``doctor`` is the one command that must not.
* **APPL-01/D-14** -- a placeholder paperless-ngx token makes ``doctor`` exit
  non-zero.  That one is exercised through the *real* registry rather than a
  stub, so the wiring between the command and ``run_checks`` is genuinely
  under test at least once.

The state-permutation tests stub ``saneless.cli.run_checks`` with hand-built
results, for the same reason ``tests/test_cli.py`` stubs the scanner backend:
arranging five real checks into a chosen set of states would test the registry,
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
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
    profile_storage_for_loaded,
)
from saneless.exceptions import ConfigError, PaperlessError
from saneless.job import JobStore
from saneless.scanner.base import DeviceInfo
from saneless.vocabulary import ConnectionStatus, ExitCode, ProfileStorage
from saneless.worker import ScanWorker
from tests.conftest import StubScannerBackend

if TYPE_CHECKING:
    import httpx

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
        self, *, timeout: httpx.Timeout | None = None
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
        settings._config_path = None if config_path is None else Path(config_path)
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
        Five green rows in ``CheckKey`` order.

    """
    return tuple(_row(key, CheckState.OK) for key in CheckKey)


def _one_skipped_scanner() -> tuple[CheckResult, ...]:
    """
    Build five rows of which the Scanner one was never probed.

    ``run_checks`` really can produce this -- ``_scanner_skipped`` and
    ``_scanner_busy`` both do -- but no ``doctor`` invocation reaches it today,
    because the command builds its context with ``skip_scanner`` defaulted and
    passes no scanner gate.  The stub is therefore how this row gets in front
    of the printer at all, which is exactly the half-wiring R3-WR-03 found:
    until something rendered it, nothing noticed it rendered wrong.

    Returns:
        Five rows in ``CheckKey`` order, the Scanner one skipped.

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
        results = (
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
    """The printed table: five rows, in order, each with its marker."""

    def test_all_ok_prints_exactly_five_lines(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One line per check and nothing else -- no header, no summary."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        _stub_registry(monkeypatch, _all_ok())
        result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert len(_lines(result.output)) == len(CheckKey)

    def test_rows_are_printed_in_check_key_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Member order is the contract both surfaces render in (D-02)."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        _stub_registry(monkeypatch, _all_ok())
        result = runner.invoke(cli, ["doctor"])
        printed = [line.split("]", 1)[1].split()[0] for line in _lines(result.output)]
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
        rows = [line for line in _lines(result.output) if line.startswith("[")]
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
            for key, line in zip(CheckKey, _lines(result.output), strict=True)
        }
        assert len(starts) == 1

    def test_a_warn_row_is_followed_by_its_indented_next_step(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """APPL-04: a row nobody can act on is a row that only gets escalated."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        step = "Set a fallback folder in the saneless config."
        results = (
            _row(CheckKey.SCANNER, CheckState.OK),
            _row(CheckKey.PAPERLESS, CheckState.OK),
            _row(CheckKey.PROFILES, CheckState.OK),
            _row(CheckKey.FALLBACK, CheckState.WARN, next_step=step),
            _row(CheckKey.DATA_DIR, CheckState.OK),
        )
        _stub_registry(monkeypatch, results)
        result = runner.invoke(cli, ["doctor"])
        lines = _lines(result.output)
        assert len(lines) == len(CheckKey) + 1
        row, step_line = lines[3], lines[4]
        assert step_line.strip() == step
        assert step_line.index(step) == row.index("Fallback message.")

    def test_a_fail_row_is_followed_by_its_indented_next_step(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Red rows carry a remedy for the same reason amber ones do."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        step = "Put a real API token in the saneless config file."
        results = (
            _row(CheckKey.SCANNER, CheckState.OK),
            _row(CheckKey.PAPERLESS, CheckState.FAIL, next_step=step),
            _row(CheckKey.PROFILES, CheckState.OK),
            _row(CheckKey.FALLBACK, CheckState.OK),
            _row(CheckKey.DATA_DIR, CheckState.OK),
        )
        _stub_registry(monkeypatch, results)
        result = runner.invoke(cli, ["doctor"])
        lines = _lines(result.output)
        assert lines[2].strip() == step

    def test_ok_rows_print_no_next_step_line(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Nothing to do means nothing printed; five green rows are five lines."""
        runner = _patch_doctor(monkeypatch, _make_settings(tmp_path))
        _stub_registry(monkeypatch, _all_ok())
        result = runner.invoke(cli, ["doctor"])
        assert all(line.startswith("[") for line in _lines(result.output))


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
        lines = _lines(result.output)
        assert len(lines) == len(CheckKey)
        assert lines[0].startswith(f"{_SKIPPED_MARKER} ")
        assert not lines[0].startswith(_state_marker(CheckState.OK))
        assert all(
            line.startswith(f"{_state_marker(CheckState.OK)} ") for line in lines[1:]
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
            for key, line in zip(CheckKey, _lines(result.output), strict=True)
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
        """Five rows come back from ``run_checks`` itself, not from a stub."""
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

        ``PaperlessClient.__init__`` raises ``PaperlessError`` for a URL httpx
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
            Record the context and answer with five green rows.

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
