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

import pytest
from click.testing import CliRunner

from saneless.checks import CheckKey, CheckResult, CheckState, check_name
from saneless.cli import cli
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.exceptions import ConfigError, PaperlessError
from saneless.scanner.base import DeviceInfo
from saneless.vocabulary import ConnectionStatus, ExitCode
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
