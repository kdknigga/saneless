"""
The one health-check registry both surfaces read.

Covers requirements: APPL-01, APPL-02, APPL-06, APPL-07, APPL-11.

``saneless doctor`` and the web status strip are required to report the *same*
checks with the *same* words (D-02).  Nothing mechanical enforces that if each
surface owns its own list, so ``checks.py`` owns one registry and both surfaces
iterate ``CheckKey``.  These tests are what makes that a fact rather than a
convention: every vocabulary case is parametrised over ``list(CheckKey)`` and
``list(CheckState)``, so a sixth check or a fourth state cannot be added
without forcing a decision here -- the discipline
``tests/test_vocabulary.py`` already applies to the lookup functions and
``tests/test_web_state_rendering.py`` applies to the templates.

The saned pre-probe is tested against real loopback sockets.  It has no analog
anywhere in the tree: ``SaneBackend.get_devices()`` is a blocking C call with
no timeout at any layer, so this socket probe is the only bound available and
is therefore treated as new code rather than as a variation on something
already proven.  No test here sleeps.
"""

from __future__ import annotations

import socket
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from saneless.checks import (
    PROBE_CONNECT_SECONDS,
    SANED_PORT,
    CheckKey,
    CheckResult,
    CheckState,
    _saned_hosts,
    _saned_reachable,
    check_name,
    check_state_class,
    check_state_glyph,
    check_state_label,
    worst_state,
)

from saneless import checks

if TYPE_CHECKING:
    from collections.abc import Iterator

# Loopback connects resolve or refuse immediately, so a short budget keeps a
# hung test from burning the suite's 60 s timeout.
_PROBE_BUDGET = 0.5


@pytest.fixture
def listening_port() -> Iterator[int]:
    """
    Bind an ephemeral loopback port and keep it listening for one test.

    Yields:
        The port number a probe can successfully connect to.

    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        yield server.getsockname()[1]


def _closed_port() -> int:
    """
    Return a loopback port that was bound and then released.

    Returns:
        A port number nothing is listening on, so a connect is refused.

    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def _result(state: CheckState, key: CheckKey = CheckKey.SCANNER) -> CheckResult:
    """
    Build a throwaway result carrying one state.

    Args:
        state: The state the result should report.
        key: The check the result belongs to; irrelevant to worst_state.

    Returns:
        A CheckResult with a developer-constant message.

    """
    return CheckResult(key=key, state=state, message="stub")


class TestCheckVocabulary:
    """The enums and their four presentation lookups are total (D-01, D-02)."""

    def test_check_state_has_exactly_three_members(self) -> None:
        """
        Three states, no more (D-01).

        A fourth state would have to be given an exit code, a glyph, a colour
        class and a screen-reader label, and ``doctor``'s "non-zero on FAIL
        only" rule would have to be re-decided.  The count is asserted so that
        happens deliberately.
        """
        assert len(list(CheckState)) == 3
        assert set(CheckState) == {CheckState.OK, CheckState.WARN, CheckState.FAIL}

    def test_check_key_has_exactly_five_members(self) -> None:
        """
        Five checks, and this enum is the contract that both surfaces see them.

        D-02: neither ``saneless doctor`` nor the status strip may hold a check
        the other does not have.  Both iterate ``CheckKey``, so the only way to
        add a sixth is here.
        """
        assert len(list(CheckKey)) == 5
        assert set(CheckKey) == {
            CheckKey.SCANNER,
            CheckKey.PAPERLESS,
            CheckKey.PROFILES,
            CheckKey.FALLBACK,
            CheckKey.DATA_DIR,
        }

    @pytest.mark.parametrize("key", list(CheckKey))
    def test_check_name_is_complete(self, key: CheckKey) -> None:
        """
        Every key has a human name that is not its own member value.

        Args:
            key: The check key under test.

        """
        name = check_name(key)
        assert name
        assert name != key.value

    def test_check_names_are_the_ui_spec_column(self) -> None:
        """The name column is UI-SPEC S1's, verbatim, in member order."""
        assert [check_name(key) for key in CheckKey] == [
            "Scanner",
            "Paperless",
            "Profiles",
            "Fallback",
            "Data folder",
        ]

    @pytest.mark.parametrize("state", list(CheckState))
    def test_state_lookups_are_complete(self, state: CheckState) -> None:
        """
        Every state has a label, a CSS class and a glyph.

        Args:
            state: The state under test.

        """
        assert check_state_label(state)
        assert check_state_class(state)
        assert check_state_glyph(state)

    def test_state_labels_are_for_a_screen_reader(self) -> None:
        """The labels are words, not the member values a reader would hear."""
        assert check_state_label(CheckState.OK) == "OK"
        assert check_state_label(CheckState.WARN) == "Warning"
        assert check_state_label(CheckState.FAIL) == "Failed"

    def test_state_classes_are_the_ui_spec_selectors(self) -> None:
        """The three glyph colours are named here, never in a template."""
        assert check_state_class(CheckState.OK) == "check-ok"
        assert check_state_class(CheckState.WARN) == "check-warn"
        assert check_state_class(CheckState.FAIL) == "check-fail"

    def test_state_glyphs_have_text_presentation(self) -> None:
        """
        The glyphs are the three UI-SPEC code points, and none is an emoji.

        ``!`` rather than U+26A0 or U+2757 is deliberate: every Unicode warning
        symbol has emoji presentation on at least one shipping platform, and
        the master spec already records U+26A0 as rejected for that reason.
        """
        assert check_state_glyph(CheckState.OK) == "✓"
        assert check_state_glyph(CheckState.WARN) == "!"
        assert check_state_glyph(CheckState.FAIL) == "✗"

    def test_cold_start_marker_is_the_neutral_glyph(self) -> None:
        """The cold-start row is marked U+00B7, which is neither good nor bad."""
        assert checks.CHECKING_GLYPH == "·"
        assert checks.CHECKING_STATE_CLASS == "check-checking"
        assert checks.CHECKING_MESSAGE == "Checking…"

    def test_check_name_rejects_an_unrecognised_value(self) -> None:
        """A value that is not a CheckKey is a programming error, not a row."""
        with pytest.raises(AssertionError):
            check_name(cast("CheckKey", "not-a-key"))

    def test_check_state_label_rejects_an_unrecognised_value(self) -> None:
        """A value that is not a CheckState cannot be labelled."""
        with pytest.raises(AssertionError):
            check_state_label(cast("CheckState", "MAYBE"))

    def test_check_state_class_rejects_an_unrecognised_value(self) -> None:
        """A value that is not a CheckState cannot be coloured."""
        with pytest.raises(AssertionError):
            check_state_class(cast("CheckState", "MAYBE"))

    def test_check_state_glyph_rejects_an_unrecognised_value(self) -> None:
        """A value that is not a CheckState has no glyph."""
        with pytest.raises(AssertionError):
            check_state_glyph(cast("CheckState", "MAYBE"))


class TestCheckResult:
    """A rendered row is a report, and nothing downstream may rewrite it."""

    def test_result_is_frozen(self) -> None:
        """
        Reassigning a state on a finished result raises.

        Written through ``setattr`` with the attribute name in a variable
        rather than as a plain assignment: a plain assignment is a type error
        both checkers would reject at the gate, and this project adds no
        suppression comment to get a test past them.
        """
        result = _result(CheckState.OK)
        attribute = "state"
        with pytest.raises(FrozenInstanceError):
            setattr(result, attribute, CheckState.FAIL)

    def test_next_step_and_skipped_default_to_empty_and_false(self) -> None:
        """An OK row needs neither a next step nor a skip marker."""
        result = _result(CheckState.OK)
        assert result.next_step == ""
        assert result.skipped is False


class TestWorstState:
    """The collapse rule behind ``doctor``'s exit code (D-01)."""

    def test_no_results_is_ok(self) -> None:
        """Nothing wrong was found, so nothing is reported wrong."""
        assert worst_state(()) is CheckState.OK

    def test_all_ok_is_ok(self) -> None:
        """A healthy appliance collapses to OK."""
        results = [_result(CheckState.OK) for _ in range(3)]
        assert worst_state(results) is CheckState.OK

    def test_a_warn_with_no_fail_is_warn(self) -> None:
        """A WARN is a true statement about a deployment that still works."""
        results = [_result(CheckState.OK), _result(CheckState.WARN)]
        assert worst_state(results) is CheckState.WARN

    def test_a_fail_wins_over_a_warn(self) -> None:
        """One red check makes the whole verdict red."""
        results = [_result(CheckState.WARN), _result(CheckState.FAIL)]
        assert worst_state(results) is CheckState.FAIL

    def test_a_fail_wins_regardless_of_order(self) -> None:
        """A WARN arriving after a FAIL cannot soften the verdict."""
        results = [_result(CheckState.FAIL), _result(CheckState.WARN)]
        assert worst_state(results) is CheckState.FAIL

    def test_a_single_warn_is_warn(self) -> None:
        """One amber row on its own is amber, not red."""
        assert worst_state([_result(CheckState.WARN)]) is CheckState.WARN

    def test_it_accepts_a_one_shot_iterator(self) -> None:
        """The signature takes an Iterable, so a generator is a valid caller."""
        results = (_result(state) for state in (CheckState.OK, CheckState.WARN))
        assert worst_state(results) is CheckState.WARN


class TestSanedHostParsing:
    """sane-net's host syntax is ambiguous, so the reading is pinned here."""

    def test_no_host_yields_no_entries(self) -> None:
        """A USB deployment has nothing to pre-probe."""
        assert _saned_hosts("") == ()

    def test_a_single_host_uses_the_default_port(self) -> None:
        """One bare name is one host on saned's registered port."""
        assert _saned_hosts("scanbox") == (("scanbox", SANED_PORT),)

    def test_two_names_are_two_hosts(self) -> None:
        """``host-a:host-b`` is sane-net's multi-host spelling, not host:port."""
        assert _saned_hosts("host-a:host-b") == (
            ("host-a", SANED_PORT),
            ("host-b", SANED_PORT),
        )

    def test_a_trailing_port_is_a_port(self) -> None:
        """``host-a:6566`` is one host on an explicit port."""
        assert _saned_hosts("host-a:6566") == (("host-a", 6566),)

    def test_a_non_default_trailing_port_is_still_a_port(self) -> None:
        """The port rule is about the shape of the segment, not its value."""
        assert _saned_hosts("host-a:7000") == (("host-a", 7000),)

    def test_three_segments_are_all_hosts(self) -> None:
        """
        The two-segment rule is deliberately narrow.

        With three or more segments there is no reading that is safe to guess,
        so every segment is treated as a host name.  A segment that is really a
        port then simply fails to resolve, which costs one refused connect and
        no wrong answer.
        """
        assert _saned_hosts("host-a:6566:host-b") == (
            ("host-a", SANED_PORT),
            ("6566", SANED_PORT),
            ("host-b", SANED_PORT),
        )

    def test_an_out_of_range_port_is_not_a_port(self) -> None:
        """
        A number no socket could bind is read as a host name, not a port.

        ``socket.create_connection`` raises ``OverflowError`` -- not
        ``OSError`` -- for a port outside 0..65535, so accepting one here would
        put an exception the probe does not catch inside the probe.
        """
        assert _saned_hosts("host-a:99999") == (
            ("host-a", SANED_PORT),
            ("99999", SANED_PORT),
        )

    def test_blank_segments_are_dropped(self) -> None:
        """A stray or doubled colon contributes no host to probe."""
        assert _saned_hosts(" : host-a : ") == (("host-a", SANED_PORT),)


class TestSanedReachable:
    """The only bounded reachability probe in the tree."""

    def test_a_listening_socket_is_reachable(self, listening_port: int) -> None:
        """
        A port that accepts a connection answers True.

        Args:
            listening_port: A loopback port the fixture is listening on.

        """
        assert _saned_reachable("127.0.0.1", listening_port, _PROBE_BUDGET) is True

    def test_a_closed_port_is_not_reachable(self) -> None:
        """A refused connect answers False and raises nothing."""
        assert _saned_reachable("127.0.0.1", _closed_port(), _PROBE_BUDGET) is False

    def test_an_unresolvable_name_is_not_reachable(self) -> None:
        """
        A name that does not resolve answers False rather than raising.

        ``socket.gaierror`` subclasses ``OSError``, so the one ``except`` arm
        covers a DNS failure as well as a refused connect.  The name used here
        is in the reserved ``.invalid`` TLD (RFC 2606), which is guaranteed
        never to resolve, so this test needs no network.
        """
        assert _saned_reachable("scanner.invalid", SANED_PORT, _PROBE_BUDGET) is False

    def test_the_default_probe_budget_is_two_seconds(self) -> None:
        """
        The connect budget is what makes an unplugged host cost 2 s, not 127 s.

        Linux retries a SYN six times by default, so an unplugged host without
        this bound costs roughly two minutes inside a blocking C call nothing
        can interrupt.
        """
        assert PROBE_CONNECT_SECONDS == 2.0

    def test_the_registered_port_is_used(self) -> None:
        """
        6566 is IANA's ``sane-port``, verified in ``/etc/services``.

        RESEARCH assumption A1 flagged this as unverified; it is settled, and
        the constant is pinned so a future edit has to argue with this test.
        """
        assert SANED_PORT == 6566


class TestImportHygiene:
    """``checks.py`` is read by both surfaces, so it may depend on neither."""

    def test_checks_imports_neither_web_nor_cli(self) -> None:
        """
        No import of ``saneless.web`` or ``saneless.cli`` appears in the source.

        D-02 makes this module the shared registry.  An import of either
        surface would make it that surface's module, and the other one would
        either import a web app to print a terminal table or import Click to
        render a page.  Every dependency is injected instead.
        """
        source = Path(checks.__file__).read_text(encoding="utf-8")
        forbidden = (
            "from saneless.web",
            "import saneless.web",
            "from saneless.cli",
            "import saneless.cli",
            "from .web",
            "from .cli",
        )
        offenders = [line for line in source.splitlines() if line.startswith(forbidden)]
        assert offenders == []
