"""
The one health-check registry both surfaces read.

``saneless doctor`` and the web status strip report the same checks with the
same words, because ``checks.py`` owns one registry and both surfaces iterate
``CheckKey``.  Every vocabulary case is parametrised over ``list(CheckKey)``
and ``list(CheckState)``, so a seventh check or a fourth state cannot be added
without a decision here.

The saned pre-probe is tested against real loopback sockets: libsane's listing
is a blocking C call with no timeout of its own, so this socket probe is what
finds a dead host quickly.  No test here sleeps.
"""

from __future__ import annotations

import ast
import errno
import logging
import os
import re
import socket
import struct
import tempfile
import threading
from dataclasses import FrozenInstanceError, dataclass, replace
from itertools import pairwise
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Final, cast

import httpx2
import pytest

from saneless import checks
from saneless.checks import (
    PROBE_CONNECT_SECONDS,
    PROBE_READ_SECONDS,
    SANED_PORT,
    CheckContext,
    CheckKey,
    CheckResult,
    CheckState,
    PaperlessRefusal,
    ScannerRefusal,
    _saned_hosts,
    _scanner_busy,
    _scanner_skipped,
    check_name,
    check_row_class,
    check_row_glyph,
    check_row_label,
    check_state_class,
    check_state_glyph,
    check_state_label,
    run_checks,
    worst_state,
)
from saneless.config import (
    CONFIG_FILENAME,
    LEGACY_CONFIG_FILENAME,
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
    discover_config,
)
from saneless.exceptions import (
    ListingAbortedError,
    ListingCrashedError,
    ListingNoAnswerError,
    ListingTimedOutError,
    ScanError,
)
from saneless.paperless import PaperlessClient
from saneless.scanner import listing
from saneless.scanner import sane_backend as sane_backend_mod
from saneless.scanner.base import DeviceInfo, DeviceSurvey
from saneless.scanner.net_hosts import effective_sane_net_hosts
from saneless.scanner.sane_backend import SaneBackend
from saneless.vocabulary import (
    CheckSurface,
    ConnectionStatus,
    ProfileStorage,
    connection_status_message,
    render_check_step,
)
from tests.conftest import StubScannerBackend, poll_until
from tests.fake_sane import FakeSaneDev, FakeSaneHandle, FakeSaneModule
from tests.fake_saned import EXIT_REQUEST, INIT_REQUEST, SanedBehaviour, fake_saned

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from saneless.config import ConfigDiscovery
    from saneless.scanner.base import DeviceCapabilities, ScannerBackend

# Loopback connects resolve or refuse immediately, so a short budget keeps a
# hung test from burning the suite's 60 s timeout.
_PROBE_BUDGET = 0.5

# A token that is not a placeholder, so the Paperless check reaches its probe.
_REAL_TOKEN = "a-real-looking-token"

# The only three strings a check row is allowed to spell a filesystem path
# with.  They are built from the config constants so this file
# never holds the superseded name on its own, and they are the exception the
# V7 guard below carves out -- one row, one field, two states, three constants.
# Longest first, so stripping them from a string cannot leave a shorter one's
# tail behind.
_ALLOWED_PATH_SPELLINGS: Final = (
    f"$XDG_CONFIG_HOME/saneless/{LEGACY_CONFIG_FILENAME}",
    f"/etc/saneless/{LEGACY_CONFIG_FILENAME}",
    f"./{LEGACY_CONFIG_FILENAME}",
)


def _refuse_temp_files_in(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    """
    Make creating a temporary file in ``folder`` fail with EACCES.

    Every other directory still gets a real temporary file.  Injecting the
    refusal rather than taking the folder's write bit away keeps the test
    meaningful as root, whom file modes do not stop.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        folder: The directory that must refuse the probe's file.

    """
    real_named_temporary_file = tempfile.NamedTemporaryFile

    def refusing(**kwargs: str | Path) -> object:
        """Refuse a file in ``folder``; create any other for real."""
        directory = Path(kwargs["dir"])
        if directory == folder:
            raise PermissionError(
                errno.EACCES, os.strerror(errno.EACCES), str(directory)
            )
        return real_named_temporary_file(dir=directory, prefix=str(kwargs["prefix"]))

    monkeypatch.setattr("saneless.checks.tempfile.NamedTemporaryFile", refusing)


def _a_file_in(folder: Path) -> Path:
    """
    Put a regular file in ``folder``, for a setting that must not name one.

    Args:
        folder: The directory to create the file in.

    Returns:
        The file's path.

    """
    file = folder / "a-file"
    file.write_text("not a folder", encoding="utf-8")
    return file


# The real saned probe, kept before any test replaces it, so the probe's own
# tests can still reach it by name.
_REAL_PROBE_SANED: Final = checks._probe_saned

# The host the configured device ``_settings()`` writes lives on.
_DEVICE_HOST: Final = "scanbox.lan"


@pytest.fixture(autouse=True)
def _the_configured_device_host_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Answer healthy for the default configured device's host, without a socket.

    ``_settings()`` configures a ``net:`` device on ``_DEVICE_HOST``, and the
    Scanner check's preflight probes a ``net:`` device's own host before it
    may open that device.  A real probe of that name would ask this machine's
    resolver: network traffic, and a verdict that depends on the machine the
    suite runs on.  So that one host answers healthy with no socket, and
    every other host still goes to the real probe.  A test that cares what
    the host answers says so with ``_recording_dialler``, which replaces this.

    Args:
        monkeypatch: pytest's attribute patcher.

    """

    def _probe(
        host: str,
        port: int,
        connect_timeout: float,
        handshake_timeout: float,
        abort: threading.Event | None = None,
    ) -> checks._SanedOutcome:
        if host == _DEVICE_HOST:
            return checks._SanedOutcome.HEALTHY
        return _REAL_PROBE_SANED(host, port, connect_timeout, handshake_timeout, abort)

    monkeypatch.setattr(checks, "_probe_saned", _probe)


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
    """The enums and their four presentation lookups are total."""

    def test_check_state_has_exactly_three_members(self) -> None:
        """
        There are three check states, no more.

        A fourth state would need an exit code, a glyph, a colour class and a
        screen-reader label, and ``doctor``'s "non-zero on FAIL only" rule
        would need deciding again, so adding one has to be deliberate.
        """
        assert len(list(CheckState)) == 3
        assert set(CheckState) == {CheckState.OK, CheckState.WARN, CheckState.FAIL}

    def test_check_key_has_exactly_six_members(self) -> None:
        """
        There are six checks, and this enum is how both surfaces see them.

        Neither ``saneless doctor`` nor the status strip may hold a check the
        other does not have.  Both iterate ``CheckKey``, so the only way to
        add a seventh is here.
        """
        assert len(list(CheckKey)) == 6
        assert set(CheckKey) == {
            CheckKey.CONFIGURATION,
            CheckKey.SCANNER,
            CheckKey.PAPERLESS,
            CheckKey.PROFILES,
            CheckKey.FALLBACK,
            CheckKey.DATA_DIR,
        }

    def test_configuration_is_the_first_member(self) -> None:
        """
        The Configuration check comes first, so the cause is read above the symptoms.

        Both surfaces render in member order, so first here is first on the
        page and first in ``doctor``'s transcript.  One missing config file
        can turn several other rows red or amber; this row names it before
        they are read.
        """
        assert next(iter(CheckKey)) is CheckKey.CONFIGURATION

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
        """
        The check names are the designed column, verbatim, in member order.

        "Configuration" heads it, followed by the other five in their order.
        """
        assert [check_name(key) for key in CheckKey] == [
            "Configuration",
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
        The glyphs are three fixed code points, and none is an emoji.

        ``!`` stands in for U+26A0 or U+2757 because every Unicode warning
        symbol has emoji presentation on at least one shipping platform.
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


class TestRowMarkersReadTheSkippedFlag:
    """
    The marker a row renders comes from the whole result, not the state.

    ``CheckResult.skipped`` means "we did not look".  The state beside it is
    ``OK`` -- the row still needs a colour and a scripted health gate must not
    go red for a probe nobody took -- so a marker derived from the state alone
    would show a green tick in front of a sentence saying nothing was checked.
    The three row-marker functions are pinned over every state, not only the
    one skipped rows carry.
    """

    @pytest.mark.parametrize("state", list(CheckState))
    def test_a_probed_row_delegates_to_its_state(self, state: CheckState) -> None:
        """
        With the flag clear, each row marker is exactly its state marker.

        Args:
            state: The state under test.

        """
        result = CheckResult(key=CheckKey.SCANNER, state=state, message="Probed.")
        assert check_row_class(result) == check_state_class(state)
        assert check_row_glyph(result) == check_state_glyph(state)
        assert check_row_label(result) == check_state_label(state)

    @pytest.mark.parametrize("state", list(CheckState))
    def test_a_skipped_row_is_neutral_whatever_its_state(
        self, state: CheckState
    ) -> None:
        """
        With the flag set, the state is not what the row renders.

        An OK, a WARN and a FAIL row all render the neutral cold-start trio when
        nothing was probed, because the state is only there to give the row a
        colour to draw and is not a verdict anybody took.

        Args:
            state: The state under test.

        """
        result = CheckResult(
            key=CheckKey.SCANNER, state=state, message="Not looked at.", skipped=True
        )
        assert check_row_class(result) == checks.CHECKING_STATE_CLASS
        assert check_row_glyph(result) == checks.CHECKING_GLYPH
        assert check_row_label(result) == checks.SKIPPED_STATE_LABEL

    @pytest.mark.parametrize("state", list(CheckState))
    def test_a_skipped_row_never_renders_the_ok_marker(self, state: CheckState) -> None:
        """
        The green tick and the word "OK" are exactly what must not appear.

        Args:
            state: The state under test.

        """
        result = CheckResult(
            key=CheckKey.SCANNER, state=state, message="Not looked at.", skipped=True
        )
        assert check_row_class(result) != check_state_class(CheckState.OK)
        assert check_row_glyph(result) != check_state_glyph(CheckState.OK)
        assert check_row_label(result) != check_state_label(CheckState.OK)

    def test_the_skipped_word_is_not_the_cold_start_word(self) -> None:
        """
        A skipped row borrows the glyph and the colour, but not the word.

        "Checking" spoken over a row that was deliberately not probed tells a
        listener a probe is running when none is -- a smaller version of the
        same lie the green tick tells.
        """
        assert checks.SKIPPED_STATE_LABEL == "Not checked"
        assert checks.SKIPPED_STATE_LABEL != checks.CHECKING_STATE_LABEL

    @pytest.mark.parametrize("builder", [_scanner_skipped, _scanner_busy])
    def test_the_two_skipped_rows_render_neutral(
        self, builder: Callable[[], CheckResult]
    ) -> None:
        """
        Both rows the registry can actually produce are neutral on both surfaces.

        Args:
            builder: The row factory under test.

        """
        result = builder()
        assert result.skipped is True
        assert result.state is CheckState.OK
        assert check_row_class(result) == checks.CHECKING_STATE_CLASS
        assert check_row_glyph(result) == checks.CHECKING_GLYPH
        assert check_row_label(result) == checks.SKIPPED_STATE_LABEL


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
    """The collapse rule behind ``doctor``'s exit code."""

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

    def test_three_segments_holding_a_number_are_refused(self) -> None:
        """
        A number among three or more segments refuses the whole setting.

        glibc reads a bare integer as the single-integer IPv4 form, so
        ``getaddrinfo('6566', 6566)`` resolves rather than failing, and through
        the pre-probe's short circuit a junk dial that answers is a wrong
        answer.  ``6566`` is not a plausible host name, so the
        more-than-two-segment guard refuses the setting.  Refusal is the safe
        fallback: no entries means no probe, and the scanner check calls
        ``get_devices()`` directly.
        """
        assert _saned_hosts("host-a:6566:host-b") == ()

    def test_an_out_of_range_port_is_dropped_rather_than_dialled(self) -> None:
        """
        A number no socket could bind is dropped, leaving the host beside it.

        Read as a *port*, ``connect`` raises ``OverflowError``, which the
        probe does not catch, and ``getaddrinfo`` truncates it modulo 65536,
        so ``host-a:99999`` would dial port 34463.  Read as a *host name*,
        glibc answers ``getaddrinfo('99999', 6566)`` with
        ``0.1.134.159:6566``, its single-integer IPv4 form.  Both are
        connections to an address nobody configured; dropping the segment
        avoids them.
        """
        assert _saned_hosts("host-a:99999") == (("host-a", SANED_PORT),)

    def test_an_expanded_ipv6_literal_produces_no_entries_to_probe(self) -> None:
        """
        A fully written-out literal is refused, like its compressed form.

        An expanded literal has no empty interior segment and no rejected
        character, so the blank-segment rule alone would read it as eight
        host entries.  The stdlib's ``ipaddress`` is asked first, and it
        recognises both spellings.
        """
        assert _saned_hosts("2001:db8:0:0:0:0:0:1") == ()

    def test_a_bracketed_expanded_ipv6_literal_produces_no_entries(self) -> None:
        """
        ``[2001:db8:0:0:0:0:0:1]:6566`` is refused as well.

        Bracket-stripped it is nine groups, which the stdlib will not parse,
        so this one is caught by the segment rules rather than by
        ``ipaddress``: ``2001`` is all digits, so it is not a plausible host
        name and the more-than-two-segment guard refuses the setting.  Both
        halves of the defence are load-bearing.
        """
        assert _saned_hosts("[2001:db8:0:0:0:0:0:1]:6566") == ()

    def test_a_zone_suffixed_ipv6_literal_produces_no_entries(self) -> None:
        """
        A link-local literal carrying its interface is refused too.

        ``ipaddress.ip_address`` accepts scoped literals, so ``fe80::1%eth0``
        parses as version 6 with no help from this module.
        """
        assert _saned_hosts("fe80::1%eth0") == ()

    def test_a_bare_zero_produces_no_entries_to_probe(self) -> None:
        """
        ``0`` is dropped, because dialling it reaches this machine.

        glibc answers ``getaddrinfo('0', 6566)`` with ``0.0.0.0:6566``, and
        on Linux a ``connect()`` to ``0.0.0.0`` reaches
        loopback.  So an all-digit segment is not merely untidy: a ``0``
        reaching the dial list lets the probe report the configured scanner
        host "reachable" off any unrelated local process that happens to
        listen on 6566.
        """
        assert _saned_hosts("0") == ()

    def test_a_bare_number_is_not_a_host_name(self) -> None:
        """
        ``2001`` is dropped rather than resolved as an address.

        glibc answers ``getaddrinfo('2001', 6566)`` with ``0.0.7.209:6566``.
        It *accepts* these strings, which is why the
        all-digit rejection is a security rule and not a cosmetic one: half an
        IPv6 literal resolves to a routable address nobody typed.
        """
        assert _saned_hosts("2001") == ()

    def test_a_bare_out_of_range_number_produces_no_entries(self) -> None:
        """
        ``99999`` on its own is dropped, the same as beside a host.

        glibc answers ``getaddrinfo('99999', 6566)`` with ``0.1.134.159:6566``.
        """
        assert _saned_hosts("99999") == ()

    def test_blank_segments_are_dropped(self) -> None:
        """A stray or doubled colon contributes no host to probe."""
        assert _saned_hosts(" : host-a : ") == (("host-a", SANED_PORT),)

    def test_a_bare_ipv6_literal_produces_no_entries_to_probe(self) -> None:
        """
        ``fe80::1`` is refused rather than read as a host and a port.

        An IPv6 literal is exactly the input where "colon-separated list of
        hosts" and "one address" are indistinguishable.  Read as a list it
        is ``(("fe80", 1),)`` -- host ``fe80`` on port 1 -- so every dial
        fails and, through the pre-probe's short circuit, a healthy
        appliance goes red.
        """
        assert _saned_hosts("fe80::1") == ()

    def test_a_bracketed_ipv6_literal_produces_no_entries_to_probe(self) -> None:
        """
        ``[fe80::1]:6566`` is refused rather than read as three host names.

        Read as a list it is ``(("[fe80", 6566), ("1]", 6566),
        ("6566", 6566))`` -- three names no resolver can answer, and three
        connect budgets spent to learn nothing.
        """
        assert _saned_hosts("[fe80::1]:6566") == ()

    def test_the_ipv6_loopback_produces_no_entries_to_probe(self) -> None:
        """``::1`` is refused rather than read as the host name ``1``."""
        assert _saned_hosts("::1") == ()

    def test_a_dotted_name_is_still_one_host_on_the_default_port(self) -> None:
        """The refusal does not touch the unambiguous single-entry reading."""
        assert _saned_hosts("scanner.local") == (("scanner.local", SANED_PORT),)

    def test_a_dotted_name_with_a_trailing_port_is_still_one_entry(self) -> None:
        """The refusal does not touch the unambiguous ``host:port`` reading."""
        assert _saned_hosts("scanner.local:6566") == (("scanner.local", 6566),)

    def test_two_short_names_are_still_two_hosts(self) -> None:
        """``a:b`` keeps the documented two-host reading."""
        assert _saned_hosts("a:b") == (("a", SANED_PORT), ("b", SANED_PORT))

    def test_three_plausible_segments_are_still_three_hosts(self) -> None:
        """Every segment of ``a:b:c`` is a plausible host name, so all are kept."""
        assert _saned_hosts("a:b:c") == (
            ("a", SANED_PORT),
            ("b", SANED_PORT),
            ("c", SANED_PORT),
        )

    def test_an_ipv4_literal_with_a_trailing_port_is_one_entry(self) -> None:
        """
        Dots and digits are a plausible host name, so IPv4 literals survive.

        The refusal is aimed at the colon, not at address literals in general.
        """
        assert _saned_hosts("192.0.2.10:6566") == (("192.0.2.10", 6566),)

    def test_two_ipv4_literals_are_two_hosts(self) -> None:
        """Two IPv4 literals read as sane-net's two-host list, unchanged."""
        assert _saned_hosts("192.0.2.10:192.0.2.11") == (
            ("192.0.2.10", SANED_PORT),
            ("192.0.2.11", SANED_PORT),
        )

    def test_a_dotted_name_with_an_out_of_range_port_keeps_only_the_name(self) -> None:
        """
        The ``OverflowError`` guard is not weakened by the drop.

        ``99999`` is not a port a socket could reach, so it is not read as
        one, and glibc resolves it to ``0.1.134.159``, so it is not kept as a
        host name either: that would dial an address nobody configured.
        """
        assert _saned_hosts("scanner.local:99999") == (("scanner.local", SANED_PORT),)


class TestUnicodeDigitPorts:
    """A port segment ``str.isdigit()`` accepts but ``int()`` refuses is no port."""

    def test_a_superscript_two_port_does_not_raise(self) -> None:
        """
        ``host:²`` parses instead of raising ``ValueError``.

        ``"²".isdigit()`` is True -- U+00B2 is Unicode category ``No`` -- and
        ``int("²")`` raises before any socket call, so nothing short of
        ``run_checks``' generic handler would catch it: the Scanner row would
        read "This check could not be completed." as FAIL and ``saneless
        doctor`` would exit 2 on a working appliance.  The segment reads like
        any unparseable port: the host is kept alone, as with ``host:99999``.
        """
        assert _saned_hosts("host:²") == (("host", SANED_PORT),)

    def test_a_circled_one_port_does_not_raise(self) -> None:
        """
        ``host:①`` parses too, so the property is pinned on the class.

        A second Unicode category ``No`` character, so what is pinned is "no
        character ``str.isdigit()`` accepts and ``int()`` refuses can reach
        ``int()``" rather than one code point.
        """
        assert _saned_hosts("host:①") == (("host", SANED_PORT),)

    def test_an_arabic_indic_port_is_never_read_as_a_number(self) -> None:
        """
        ``host:١٢٣٤`` never becomes port 1234.

        ``int()`` accepts non-ASCII decimals, so a gate built on it would read
        Arabic-Indic 1234 as port 1234 -- a number libsane's C-side parsing
        would never derive from that string, the divergence
        ``_saned_host_setting`` exists to prevent.  1234 rather than 6566 keeps
        the wrong answer and the right answer different tuples.
        """
        assert _saned_hosts("host:١٢٣٤") == (("host", SANED_PORT),)

    def test_an_ascii_port_is_untouched(self) -> None:
        """An ASCII-decimal port reads as that number."""
        assert _saned_hosts("host:6566") == (("host", 6566),)

    def test_the_lowest_ascii_port_is_untouched(self) -> None:
        """A one-character ASCII port still parses, so the gate is not a length rule."""
        assert _saned_hosts("host:1") == (("host", 1),)


class TestNumericAddressShorthand:
    """glibc reads far more than all-digit strings as an address."""

    @pytest.mark.parametrize(
        "setting",
        ["0.0", "0x0.0", "0x7f.1", "127.1", "6566.0", "0xdeadbeef", "01.02.03.04"],
    )
    def test_a_numeric_shorthand_segment_is_never_dialled(self, setting: str) -> None:
        """
        No segment glibc reads as a number reaches the dial list.

        ``0.0`` and ``0x0.0`` resolve to ``0.0.0.0`` and ``127.1`` to
        ``127.0.0.1``, though each contains a ``.``.  A loopback dial answered
        by any unrelated local process on 6566 would let the check fall
        through to the ~127 s uninterruptible ``get_devices()`` hang the
        pre-probe exists to avoid.  ``0xdeadbeef`` is the bare hex form and
        ``01.02.03.04`` the octal one, whose parts glibc reads as octal.

        Args:
            setting: One numeric spelling glibc accepts as an address.

        """
        assert _saned_hosts(setting) == ()

    def test_a_mistyped_port_does_not_invent_a_loopback_entry(self) -> None:
        """
        ``host:0.0`` drops the invented segment rather than dialling it.

        The operator never typed an address here.  Kept, the stray ``0.0``
        segment would be a second entry that dials loopback.
        """
        assert _saned_hosts("host:0.0") == (("host", SANED_PORT),)

    def test_a_legal_dotted_quad_is_still_dialled(self) -> None:
        """A static-IP scanner keeps its pre-probe and its ~127 s saving."""
        assert _saned_hosts("192.0.2.10") == (("192.0.2.10", SANED_PORT),)

    @pytest.mark.parametrize("setting", ["0.0.0.0", "0.0.0.0:6566"])
    def test_the_unspecified_address_is_never_dialled(self, setting: str) -> None:
        """
        ``0.0.0.0`` is refused although it is a legal dotted quad.

        On Linux a ``connect()`` to it reaches loopback, so any unrelated
        local listener on 6566 would let a dead scanner host fall through to
        the ~127 s ``get_devices()``.  No scanner is at the unspecified
        address, so refusing it costs nothing.

        Args:
            setting: The unspecified address, bare and with an explicit port.

        """
        assert _saned_hosts(setting) == ()

    def test_a_stray_character_in_a_port_does_not_add_a_loopback_dial(
        self,
    ) -> None:
        """``scanbox:0.0.0.0`` is one host, not a host and the unspecified address."""
        assert _saned_hosts("scanbox:0.0.0.0") == (("scanbox", SANED_PORT),)

    def test_a_legal_dotted_quad_with_a_port_is_still_dialled(self) -> None:
        """The wider refusal does not touch the ``host:port`` reading either."""
        assert _saned_hosts("192.0.2.10:6566") == (("192.0.2.10", 6566),)

    def test_a_name_containing_an_x_is_not_mistaken_for_a_hex_literal(self) -> None:
        """
        ``box-x1.lan`` is a name, not ``0x``-prefixed anything.

        The hex arm of the refusal matches a literal ``0x`` or ``0X`` prefix,
        not the presence of the letter somewhere in the segment, so an ordinary
        appliance name survives.
        """
        assert _saned_hosts("box-x1.lan") == (("box-x1.lan", SANED_PORT),)


class TestProbeHostCap:
    """The probe list is capped, because the walk over it runs in a request thread."""

    def test_a_long_host_list_is_capped(self) -> None:
        """
        Forty configured segments yield ``_MAX_PROBE_HOSTS`` entries.

        ``_scanner_preflight`` walks the entries with ``any(...)``, paying an
        unbounded ``getaddrinfo`` plus ``PROBE_CONNECT_SECONDS`` for each, and
        that walk runs inside the ``POST /api/checks/refresh`` request thread.
        The manual-refresh floor bounds the *rate* of those requests, not the
        duration of one, so without a cap a forty-host setting is a request
        that can take minutes.
        """
        entries = _saned_hosts(":".join(f"h{index}" for index in range(1, 41)))
        assert len(entries) == checks._MAX_PROBE_HOSTS

    def test_the_capped_list_keeps_the_first_entries_in_configured_order(self) -> None:
        """
        The tail is what is dropped, so the first-named host is still probed.

        No entry means no probe for that host, and the scanner check falls
        through to ``get_devices()``, which still dials the tail, so the tail
        loses the pre-probe's protection, not only its latency saving.
        """
        entries = _saned_hosts(":".join(f"h{index}" for index in range(1, 41)))
        assert entries[0] == ("h1", SANED_PORT)
        assert all(host != "h40" for host, _port in entries)

    def test_a_repeated_host_does_not_use_up_the_cap(self) -> None:
        """
        A host named twice is probed once, and does not push another past the cap.

        The cap exists to bound the probing, and a second probe of the same
        host bounds nothing and learns nothing.  Counting repeats against it
        would leave a distinct host unprobed -- one libsane still dials.
        """
        assert _saned_hosts("a:a:a:a:b") == (("a", SANED_PORT), ("b", SANED_PORT))

    def test_a_short_host_list_is_unchanged_entry_for_entry(self) -> None:
        """A setting under the cap is not touched by it."""
        assert _saned_hosts("a:b:c") == (
            ("a", SANED_PORT),
            ("b", SANED_PORT),
            ("c", SANED_PORT),
        )

    def test_the_host_port_reading_is_unaffected_by_the_cap(self) -> None:
        """``host:port`` returns a single entry, so no cap can bite it."""
        assert _saned_hosts("scanner.local:6566") == (("scanner.local", 6566),)


class TestTheParserDocstringIsTrue:
    """One test per sentence of ``_saned_hosts``' documented contract."""

    def test_a_stray_colon_after_a_port_refuses_the_setting(self) -> None:
        """
        ``localhost:6566:`` yields ``()``, not one host on a port.

        A stray edge colon is tolerated beside a bare name, but beside a port
        it takes the setting to three segments, and ``6566`` is not a
        plausible host name, so the whole setting is refused.  Refusal is the
        safe direction: it costs the pre-probe's latency saving and never
        produces a wrong verdict.
        """
        assert _saned_hosts("localhost:6566:") == ()

    def test_a_stray_colon_before_a_port_refuses_the_setting(self) -> None:
        """``:localhost:6566`` is refused for the same reason as its mirror."""
        assert _saned_hosts(":localhost:6566") == ()

    def test_a_leading_zero_port_is_read_as_decimal(self) -> None:
        """
        ``host:065`` is port 65, as the parser's docstring says.

        Nothing here claims libsane reads ``065`` the same way.  The range test
        and the ASCII-decimal test are the whole of what this module
        guarantees about a port segment, and this case pins the reading rather
        than the agreement.
        """
        assert _saned_hosts("host:065") == (("host", 65),)

    def test_a_root_dot_fully_qualified_name_yields_no_entries(self) -> None:
        """
        ``scanner.local.`` is legal DNS and is still refused.

        ``_looks_like_a_host_name``'s trailing-dot rule rejects it, so the name
        loses its pre-probe.  Widening the accept surface for a spelling no
        config example uses buys nothing; the cost is one latency saving.
        """
        assert _saned_hosts("scanner.local.") == ()


def _probe(host: str = "scanbox.lan", port: int = SANED_PORT) -> checks._SanedOutcome:
    """
    Run the saned probe with both of its budgets shortened for the suite.

    Args:
        host: The host to probe.
        port: The port to dial.

    Returns:
        What the probe concluded about the host.

    """
    return _REAL_PROBE_SANED(host, port, _PROBE_BUDGET, _PROBE_BUDGET)


# A valid SANE_NET_INIT reply: status 0 (SANE_STATUS_GOOD), version 1.0.3.
_VALID_REPLY: Final = struct.pack(">ii", 0, 0x01000003)


class TestSanedProbe:
    """The saned pre-probe, against real loopback sockets and a fake saned."""

    def test_a_peer_that_closes_without_reading_is_rejected(self) -> None:
        """
        Accept-then-close is how saned refuses a peer, and it is REJECTED.

        saned's access check runs before it reads anything and closes the
        socket when the peer is not allowed, so the client sees a reset or an
        end of file where the reply should be.  A bare ``connect()`` would
        count that as reachable and report the denial as a scanner that is
        switched off.  The race between the fake's close and
        the probe's send can surface as a reset, a broken pipe or an end of
        file; all three must read the same.
        """
        with fake_saned(SanedBehaviour.CLOSE) as fake:
            outcome = _probe("127.0.0.1", fake.port)
        assert outcome is checks._SanedOutcome.REJECTED

    def test_a_closed_port_is_refused(self) -> None:
        """Nothing listening is REFUSED, and the probe raises nothing."""
        assert _probe("127.0.0.1", _closed_port()) is checks._SanedOutcome.REFUSED

    def test_a_healthy_saned_hears_one_init_and_one_exit(self) -> None:
        """
        A valid reply is HEALTHY, and the probe leaves saned one clean session.

        The fake records every byte it read, and teardown joins its thread,
        so after the ``with`` block the record is complete: the 21-byte INIT
        request, then ``SANE_NET_EXIT``, then the end of the connection.
        Anything else on the wire would be a probe doing more than it says.
        """
        with fake_saned(SanedBehaviour.HEALTHY) as fake:
            outcome = _probe("127.0.0.1", fake.port)
        assert outcome is checks._SanedOutcome.HEALTHY
        assert b"".join(fake.received) == INIT_REQUEST + EXIT_REQUEST
        assert len(INIT_REQUEST) == 21

    def test_a_non_success_status_is_rejected(self) -> None:
        """A reply carrying a failure status is REJECTED, not HEALTHY."""
        with fake_saned(SanedBehaviour.BAD_STATUS) as fake:
            outcome = _probe("127.0.0.1", fake.port)
        assert outcome is checks._SanedOutcome.REJECTED

    def test_a_version_libsane_would_refuse_is_rejected(self) -> None:
        """
        A success status with a foreign major version is REJECTED.

        libsane's net backend accepts a reply only when its major version is 1
        and its build is 2 or 3, so a peer that answers anything else is not a
        saned this appliance can scan through.
        """
        with fake_saned(SanedBehaviour.BAD_VERSION) as fake:
            outcome = _probe("127.0.0.1", fake.port)
        assert outcome is checks._SanedOutcome.REJECTED

    def test_a_peer_that_keeps_sending_is_read_only_eight_bytes(self) -> None:
        """
        A valid reply followed by a mebibyte of junk is HEALTHY, and quick.

        The probe reads exactly the eight reply bytes and stops, so a peer
        that floods it cannot hold it past the handshake budget or make it
        buffer what it sends.
        """
        with fake_saned(SanedBehaviour.FLOOD) as fake:
            started = monotonic()
            outcome = _probe("127.0.0.1", fake.port)
            elapsed = monotonic() - started
        assert outcome is checks._SanedOutcome.HEALTHY
        assert elapsed < _PROBE_BUDGET

    def test_a_peer_that_accepts_and_says_nothing_times_out(
        self, listening_port: int
    ) -> None:
        """
        Accept-but-silent is TIMED_OUT, inside both budgets.

        The fixture listens and never accepts, so the kernel completes the
        TCP handshake from the backlog and nothing ever replies -- exactly the
        peer libsane would wait on forever, because nothing bounds its INIT
        read.  The probe's handshake deadline is what bounds it here.

        Args:
            listening_port: A loopback port the fixture is listening on.

        """
        started = monotonic()
        outcome = _probe("127.0.0.1", listening_port)
        elapsed = monotonic() - started
        assert outcome is checks._SanedOutcome.TIMED_OUT
        assert elapsed < 3 * _PROBE_BUDGET

    def test_a_name_that_does_not_resolve_is_unresolved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A resolver error is UNRESOLVED and raises nothing.

        The resolver is replaced rather than asked about a reserved name, so
        the suite stays offline: glibc still sends a ``.invalid`` lookup to
        the configured resolver.

        Args:
            monkeypatch: pytest's patcher.

        """

        def _fail(*_args: object, **_kwargs: object) -> list[object]:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", _fail)
        assert _probe("scanbox.lan") is checks._SanedOutcome.UNRESOLVED

    def test_an_empty_resolver_answer_is_unresolved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A resolver that answers with no addresses is UNRESOLVED.

        Args:
            monkeypatch: pytest's patcher.

        """
        monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [])
        assert _probe("scanbox.lan") is checks._SanedOutcome.UNRESOLVED

    def test_the_registered_port_is_used(self) -> None:
        """The saned port is 6566, IANA's ``sane-port`` in ``/etc/services``."""
        assert SANED_PORT == 6566

    def test_the_handshake_budget_is_separate_from_the_connect_budget(self) -> None:
        """
        The reply gets its own budget, because saned does name lookups first.

        A working saned on a scanner host with slow DNS must not read as timed
        out, which it would if one short budget covered connect and reply.
        """
        assert checks.PROBE_HANDSHAKE_SECONDS == 5.0
        assert "PROBE_HANDSHAKE_SECONDS" in checks.__all__

    def test_no_probe_log_line_names_the_host_the_port_or_the_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        listening_port: int,
    ) -> None:
        """
        ASVS V7: probe logs carry outcome names and exception type names only.

        Every outcome a real socket can produce is driven through the probe
        with DEBUG captured, and no record may contain the address, a port
        number or the text of any exception the probe caught.

        Args:
            monkeypatch: pytest's patcher.
            caplog: pytest's log capture.
            listening_port: A loopback port the fixture is listening on.

        """
        caplog.set_level(logging.DEBUG, logger="saneless.checks")
        ports = [listening_port]
        for behaviour in SanedBehaviour:
            with fake_saned(behaviour) as fake:
                ports.append(fake.port)
                _probe("127.0.0.1", fake.port)
        closed = _closed_port()
        ports.append(closed)
        _probe("127.0.0.1", closed)
        _probe("127.0.0.1", listening_port)

        def _fail(*_args: object, **_kwargs: object) -> list[object]:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", _fail)
        _probe("scanbox.lan")

        messages = [record.getMessage() for record in caplog.records]
        assert messages, "the probe logged nothing, so this guard proves nothing"
        for message in messages:
            assert "127.0.0.1" not in message
            assert "scanbox" not in message
            assert "Name or service" not in message
            assert "Errno" not in message
            assert "Connection refused" not in message
            assert "reset by peer" not in message
            for port in ports:
                assert str(port) not in message


class _SpendingClock:
    """
    A monotonic clock that only moves when a dial spends the budget it was given.

    A real clock is useless for measuring a *shared* deadline in this file,
    because nothing here waits: three refused connects land in the same
    microsecond, every one of them is handed very nearly the whole budget, and
    a per-socket implementation is indistinguishable from a deadline.  So the
    clock is driven by the thing that would really consume the time -- the dial
    -- and ``spend`` says what fraction of its allowance each dial burns
    through.  One means a socket that sat there until its timeout expired.
    """

    def __init__(self, *, spend: float) -> None:
        """
        Start at zero, burning ``spend`` of every granted timeout per dial.

        Args:
            spend: The fraction of each granted timeout a dial consumes.

        """
        self.now = 0.0
        self.spend = spend

    def __call__(self) -> float:
        """
        Read the clock, the way ``checks.monotonic`` is read.

        Returns:
            The current reading, in seconds.

        """
        return self.now

    def spend_on_a_dial(self, granted: float) -> None:
        """
        Move the clock forward by what this dial cost.

        Args:
            granted: The timeout the probe handed the socket it just dialled.

        """
        self.now += granted * self.spend


class _ProbeRecorder:
    """Records what the saned probe did, in place of doing any of it."""

    def __init__(
        self,
        connectable: Iterable[object] = (),
        clock: _SpendingClock | None = None,
        *,
        peer: _PeerScript | None = None,
    ) -> None:
        """
        Start with nothing recorded.

        Args:
            connectable: The sockaddrs whose ``connect`` is allowed to
                succeed.  Empty by default, so every address refuses unless a
                case says otherwise and the cases that measure the budget are
                untouched by the option existing.
            clock: The scripted clock each dial moves forward, or ``None`` to
                leave time alone, which is what every case but the shared-budget
                one wants.
            peer: How a connected peer behaves once the handshake starts, and
                which addresses time out instead of refusing.  Omitted, the
                peer answers with a valid reply and nothing times out.

        """
        self.constructions: list[tuple[int, int, int]] = []
        self.events: list[str] = []
        self.timeouts: list[float] = []
        self.addresses: list[object] = []
        self.sent: list[bytes] = []
        self.recv_sizes: list[int] = []
        self.recv_served: list[int] = []
        self.connectable = frozenset(connectable)
        self.clock = clock
        self.peer = peer if peer is not None else _PeerScript()

    def note_a_dial(self) -> None:
        """Charge the scripted clock, if there is one, for the dial just made."""
        if self.clock is not None and self.timeouts:
            self.clock.spend_on_a_dial(self.timeouts[-1])

    def socket(self, family: int, socktype: int, proto: int) -> _RecordingSocket:
        """
        Stand in for ``socket.socket`` and record the construction.

        Args:
            family: The address family the probe chose.
            socktype: The socket type the probe chose.
            proto: The protocol the probe chose.

        Returns:
            A socket double wired back to this recorder.

        """
        self.constructions.append((family, socktype, proto))
        return _RecordingSocket(self)


@dataclass(frozen=True, slots=True)
class _PeerScript:
    """
    How the far end behaves, for the socket doubles below.

    Attributes:
        timeout_addresses: Sockaddrs whose ``connect`` times out rather than
            being refused.
        connect_errors: Pairs of a sockaddr and the ``errno`` its
            ``connect`` fails with instead, the way an address this machine has no route
            to, or no socket family for, fails at once.
        reply: The bytes a connected peer has to give; the probe's ``recv``
            is served from these, and runs dry into an end of file.
        recv_chunk: The most bytes one ``recv`` hands back, or ``None`` for
            as many as were asked for.
        send_error: What ``sendall`` raises, or ``None`` to accept the bytes.
        recv_error: What ``recv`` raises, or ``None`` to serve ``reply``.

    """

    timeout_addresses: frozenset[object] = frozenset()
    connect_errors: tuple[tuple[object, int], ...] = ()
    reply: bytes = _VALID_REPLY
    recv_chunk: int | None = None
    send_error: OSError | None = None
    recv_error: OSError | None = None


class _RecordingSocket:
    """A socket that records the calls made to it and refuses by default."""

    def __init__(self, recorder: _ProbeRecorder) -> None:
        """
        Wire this double to the recorder collecting the run.

        Args:
            recorder: Where every call is appended.

        """
        self.recorder = recorder
        self.read_offset = 0

    def __enter__(self) -> _RecordingSocket:
        """
        Enter the ``with`` block the probe opens.

        Returns:
            This double.

        """
        self.recorder.events.append("enter")
        return self

    def __exit__(self, *_exc_info: object) -> None:
        """
        Leave the ``with`` block, recording that the socket was closed.

        Args:
            _exc_info: Ignored; nothing is suppressed.

        """
        self.recorder.events.append("exit")

    def settimeout(self, timeout: float) -> None:
        """
        Record the budget the probe applied.

        Args:
            timeout: The budget, in seconds.

        """
        self.recorder.events.append("settimeout")
        self.recorder.timeouts.append(timeout)

    def close(self) -> None:
        """Record an explicit close, which a failed dial performs."""
        self.recorder.events.append("close")

    def connect(self, address: object) -> None:
        """
        Record the address dialled, and refuse unless the case allows it.

        Args:
            address: The sockaddr the probe passed.

        Raises:
            TimeoutError: When the case named this address as one that does
                not answer, the way a host whose SYNs are dropped behaves.
            OSError: When the case named an ``errno`` for this address.
            ConnectionRefusedError: Whenever the address is not one the
                recorder was told to accept, the way a closed port does.  That
                is every address unless a case named one.

        """
        self.recorder.events.append("connect")
        self.recorder.addresses.append(address)
        self.recorder.note_a_dial()
        if address in self.recorder.connectable:
            return
        if address in self.recorder.peer.timeout_addresses:
            msg = "timed out"
            raise TimeoutError(msg)
        code = dict(self.recorder.peer.connect_errors).get(address)
        if code is not None:
            raise OSError(code, os.strerror(code))
        raise ConnectionRefusedError(
            errno.ECONNREFUSED, os.strerror(errno.ECONNREFUSED)
        )

    def sendall(self, data: bytes) -> None:
        """
        Record what the probe sent, or fail the way the case says.

        Args:
            data: The bytes the probe put on the wire.

        Raises:
            OSError: The case's ``send_error``, when it set one.

        """
        self.recorder.events.append("sendall")
        if self.recorder.peer.send_error is not None:
            raise self.recorder.peer.send_error
        self.recorder.sent.append(data)

    def recv(self, size: int) -> bytes:
        """
        Serve the next slice of the scripted reply.

        Args:
            size: The most bytes the probe asked for.

        Returns:
            Up to ``size`` bytes (fewer when the case chunks the reply), or
            ``b""`` once the reply has run out, which is an end of file.

        Raises:
            OSError: The case's ``recv_error``, when it set one.

        """
        self.recorder.events.append("recv")
        self.recorder.recv_sizes.append(size)
        peer = self.recorder.peer
        if peer.recv_error is not None:
            raise peer.recv_error
        limit = size if peer.recv_chunk is None else min(size, peer.recv_chunk)
        chunk = peer.reply[self.read_offset : self.read_offset + limit]
        self.read_offset += len(chunk)
        self.recorder.recv_served.append(len(chunk))
        return chunk


# Three addresses for one name: the shape a dual-stack scanner host has, and
# the one a per-address budget would charge three connect budgets for.
_THREE_ADDRESSES = [
    (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 6566, 0, 0)),
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 6566)),
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.11", 6566)),
]


def _install_probe_recorder(
    monkeypatch: pytest.MonkeyPatch,
    *,
    connectable: Iterable[object] = (),
    clock: _SpendingClock | None = None,
    peer: _PeerScript | None = None,
) -> _ProbeRecorder:
    """
    Replace resolution and socket construction with recording doubles.

    Nothing leaves the process: ``getaddrinfo`` answers from a constant and
    every socket refuses unless the case names one that may answer, so what is
    measured is the shape of the walk and the handshake rather than any real
    network, and the test still sleeps for nothing.

    Args:
        monkeypatch: pytest's attribute patcher.
        connectable: The sockaddrs whose ``connect`` succeeds.
        clock: A scripted clock each dial moves forward, substituted for
            ``checks.monotonic``.  Omitted, the real clock is left in place and
            no dial costs anything.
        peer: How the far end behaves once connected, and which addresses
            time out.  Omitted, a connected peer answers with a valid reply.

    Returns:
        The recorder the probe's calls land in.

    """
    recorder = _ProbeRecorder(connectable, clock, peer=peer)
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *_args, **_kwargs: _THREE_ADDRESSES
    )
    monkeypatch.setattr(socket, "socket", recorder.socket)
    if clock is not None:
        monkeypatch.setattr(checks, "monotonic", clock)
    return recorder


def _install_a_clock_that_jumps(
    monkeypatch: pytest.MonkeyPatch, readings: Sequence[float]
) -> None:
    """
    Substitute the probe's monotonic clock with a scripted one.

    The deadline is read from ``checks.monotonic``, imported by bare name
    precisely so it can be replaced here without touching ``time`` globally.
    Each call takes the next scripted reading and the last one repeats, so a
    script can jump the clock past the deadline without any test sleeping.

    Args:
        monkeypatch: pytest's attribute patcher.
        readings: The values successive calls return, in order.

    """
    remaining = list(readings)
    final = remaining[-1]

    def _monotonic() -> float:
        return remaining.pop(0) if remaining else final

    monkeypatch.setattr(checks, "monotonic", _monotonic)


class TestSanedHandshake:
    """What the probe does once a connection exists, measured on a fake socket."""

    def test_the_reply_is_read_in_bounded_pieces(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        No ``recv`` asks for more than is still missing of the eight-byte reply.

        The double serves at most three bytes per call from a reply that runs
        far past eight, so a probe that asked for a buffer's worth would be
        handed junk it then had to parse, and one that looped past eight bytes
        would be caught by the total.

        Args:
            monkeypatch: pytest's patcher.

        """
        answering = _THREE_ADDRESSES[0][4]
        recorder = _install_probe_recorder(
            monkeypatch,
            connectable=[answering],
            peer=_PeerScript(reply=_VALID_REPLY + b"\xff" * 64, recv_chunk=3),
        )
        assert _probe() is checks._SanedOutcome.HEALTHY
        already = 0
        for asked, served in zip(
            recorder.recv_sizes, recorder.recv_served, strict=True
        ):
            assert asked == 8 - already
            already += served
        assert already == 8

    def test_every_read_is_bounded_by_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A timeout is set before every ``recv``, not once before the first.

        Args:
            monkeypatch: pytest's patcher.

        """
        answering = _THREE_ADDRESSES[0][4]
        recorder = _install_probe_recorder(
            monkeypatch, connectable=[answering], peer=_PeerScript(recv_chunk=1)
        )
        assert _probe() is checks._SanedOutcome.HEALTHY
        read_events = [e for e in recorder.events if e in {"settimeout", "recv"}]
        assert read_events.count("recv") == 8
        for earlier, later in pairwise(read_events):
            if later == "recv":
                assert earlier == "settimeout", read_events

    def test_a_healthy_reply_is_followed_by_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The probe sends INIT, reads the reply, then sends EXIT and nothing else.

        Args:
            monkeypatch: pytest's patcher.

        """
        answering = _THREE_ADDRESSES[0][4]
        recorder = _install_probe_recorder(monkeypatch, connectable=[answering])
        assert _probe() is checks._SanedOutcome.HEALTHY
        assert recorder.sent == [INIT_REQUEST, EXIT_REQUEST]

    @pytest.mark.parametrize(
        ("send_error", "recv_error", "reply"),
        [
            pytest.param(BrokenPipeError(), None, _VALID_REPLY, id="send-broken-pipe"),
            pytest.param(
                None, ConnectionResetError(), _VALID_REPLY, id="recv-connection-reset"
            ),
            pytest.param(None, None, b"", id="eof-before-any-reply"),
            pytest.param(None, None, _VALID_REPLY[:5], id="eof-mid-reply"),
            pytest.param(
                None, None, struct.pack(">ii", 11, 0x01000003), id="failure-status"
            ),
            pytest.param(
                None, None, struct.pack(">ii", 0, 0x02000003), id="wrong-major"
            ),
            pytest.param(
                None, None, struct.pack(">ii", 0, 0x01000007), id="wrong-build"
            ),
        ],
    )
    def test_a_connection_that_fails_the_handshake_is_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        send_error: OSError | None,
        recv_error: OSError | None,
        reply: bytes,
    ) -> None:
        """
        Anything but a valid reply, once connected, is REJECTED.

        Args:
            monkeypatch: pytest's patcher.
            send_error: What ``sendall`` raises, or None.
            recv_error: What ``recv`` raises, or None.
            reply: The bytes the peer has to give.

        """
        answering = _THREE_ADDRESSES[0][4]
        recorder = _install_probe_recorder(
            monkeypatch,
            connectable=[answering],
            peer=_PeerScript(reply=reply, send_error=send_error, recv_error=recv_error),
        )
        assert _probe() is checks._SanedOutcome.REJECTED
        assert EXIT_REQUEST not in recorder.sent

    def test_a_read_that_times_out_is_timed_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A ``recv`` timeout is TIMED_OUT, not REJECTED.

        ``TimeoutError`` subclasses ``OSError``, so a probe that caught
        ``OSError`` first would call a silent peer a rejecting one -- and a
        silent peer is the one that must never be enumerated.

        Args:
            monkeypatch: pytest's patcher.

        """
        answering = _THREE_ADDRESSES[0][4]
        _install_probe_recorder(
            monkeypatch,
            connectable=[answering],
            peer=_PeerScript(recv_error=TimeoutError()),
        )
        assert _probe() is checks._SanedOutcome.TIMED_OUT

    def test_a_stop_ends_the_wait_for_a_silent_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        With an abort Event, the reply is awaited in short slices, and a stop ends it.

        The peer never answers, so every ``recv`` runs out.  The Event is set
        before the probe starts waiting, so the first slice that runs out
        must end the probe as aborted rather than wait out the handshake
        budget, and no slice may be longer than the abort poll.

        Args:
            monkeypatch: pytest's patcher.

        """
        answering = _THREE_ADDRESSES[0][4]
        recorder = _install_probe_recorder(
            monkeypatch,
            connectable=[answering],
            peer=_PeerScript(recv_error=TimeoutError()),
        )
        abort = threading.Event()
        real_handshake = checks._handshake

        def _stop_then_handshake(
            sock: socket.socket,
            deadline: float,
            handshake_abort: threading.Event | None,
        ) -> checks._SanedOutcome:
            abort.set()
            return real_handshake(sock, deadline, handshake_abort)

        monkeypatch.setattr(checks, "_handshake", _stop_then_handshake)

        with pytest.raises(checks._PreProbeAbortedError):
            _REAL_PROBE_SANED("scanbox.lan", SANED_PORT, 60.0, 60.0, abort)

        assert recorder.events.count("recv") == 1
        assert recorder.timeouts[-1] <= checks._ABORT_POLL_SECONDS
        assert "exit" in recorder.events

    def test_a_probe_stopped_before_it_starts_dials_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A set abort Event ends the probe before it resolves or dials.

        Args:
            monkeypatch: pytest's patcher.

        """
        recorder = _install_probe_recorder(monkeypatch)
        abort = threading.Event()
        abort.set()

        with pytest.raises(checks._PreProbeAbortedError):
            _REAL_PROBE_SANED("scanbox.lan", SANED_PORT, 60.0, 60.0, abort)

        assert recorder.constructions == []

    def test_a_rejection_is_not_retried_on_the_next_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The first address that connects decides, as libsane's ``connect_dev`` does.

        Args:
            monkeypatch: pytest's patcher.

        """
        first, second = _THREE_ADDRESSES[0][4], _THREE_ADDRESSES[1][4]
        recorder = _install_probe_recorder(
            monkeypatch,
            connectable=[first, second],
            peer=_PeerScript(recv_error=ConnectionResetError()),
        )
        assert _probe() is checks._SanedOutcome.REJECTED
        assert recorder.addresses == [first]


class TestSanedProbeBound:
    """One configured host costs one connect budget, not one per address."""

    def test_three_resolved_addresses_share_one_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Every resolved address is dialled, and all of them share one budget.

        A per-socket timeout would hand out three equal budgets.  The clock is
        scripted and driven by the dial, because under a real clock three
        refused connects land in the same microsecond and look alike.  When a
        dial spends its whole allowance the grants sum to one budget at most;
        when it spends a quarter, all three addresses are reached with
        strictly decreasing grants.

        Args:
            monkeypatch: pytest's patcher.

        """
        spent = _install_probe_recorder(monkeypatch, clock=_SpendingClock(spend=1.0))
        assert _probe() is checks._SanedOutcome.TIMED_OUT
        assert sum(spent.timeouts) <= _PROBE_BUDGET, spent.timeouts

        partial = _install_probe_recorder(monkeypatch, clock=_SpendingClock(spend=0.25))
        assert _probe() is checks._SanedOutcome.REFUSED
        assert len(partial.constructions) == 3
        assert all(timeout > 0 for timeout in partial.timeouts), partial.timeouts
        assert all(earlier > later for earlier, later in pairwise(partial.timeouts)), (
            partial.timeouts
        )

    def test_the_addresses_dialled_are_the_ones_the_resolver_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Resolution order is the resolver's business, and the probe respects it.

        Args:
            monkeypatch: pytest's patcher.

        """
        recorder = _install_probe_recorder(monkeypatch)
        _probe()
        assert recorder.addresses == [info[4] for info in _THREE_ADDRESSES]
        assert recorder.constructions == [info[:3] for info in _THREE_ADDRESSES]

    def test_the_budget_is_applied_before_every_connect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A timeout set after the connect would bound nothing at all.

        With three attempts, one ``settimeout`` before the first ``connect``
        would leave the other two unbounded, so the event list is walked and
        every ``connect`` must be preceded by a ``settimeout`` of its own.

        Args:
            monkeypatch: pytest's patcher.

        """
        recorder = _install_probe_recorder(monkeypatch)
        _probe()
        unspent_budgets = 0
        for event in recorder.events:
            if event == "settimeout":
                unspent_budgets += 1
            elif event == "connect":
                assert unspent_budgets > 0, "a connect was dialled with no budget set"
                unspent_budgets -= 1
        assert recorder.events.count("connect") == 3

    def test_a_later_address_that_answers_decides_the_handshake(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A host answering on its second address is healthy, not dead.

        ``getaddrinfo`` is called without ``AI_ADDRCONFIG``, so glibc returns
        AAAA records even with no IPv6 route and RFC 6724 puts them first,
        while saned commonly binds v4-only.  Dialling only the first answer
        would leave a permanent amber row on a working appliance.  The third
        address is never dialled: the first that connects decides.

        Args:
            monkeypatch: pytest's patcher.

        """
        answering = _THREE_ADDRESSES[1][4]
        recorder = _install_probe_recorder(monkeypatch, connectable=[answering])
        assert _probe() is checks._SanedOutcome.HEALTHY
        assert recorder.addresses == [_THREE_ADDRESSES[0][4], answering]
        assert recorder.sent == [INIT_REQUEST, EXIT_REQUEST]

    def test_the_deadline_stops_the_walk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        A clock past the deadline ends the walk as TIMED_OUT instead of dialling on.

        This is what keeps the multi-address walk inside the bound rather than
        multiplying it: a first attempt that spends the whole budget leaves
        nothing for the second, so the second is not attempted at all.  The
        clock is scripted rather than waited on -- no test in this file sleeps.

        Args:
            monkeypatch: pytest's patcher.

        """
        recorder = _install_probe_recorder(monkeypatch)
        _install_a_clock_that_jumps(monkeypatch, [0.0, 0.0, 99.0])
        assert _probe() is checks._SanedOutcome.TIMED_OUT
        assert len(recorder.constructions) == 1

    def test_a_resolver_that_returns_nothing_is_unresolved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        An empty resolver answer is UNRESOLVED, not an ``IndexError``.

        ``getaddrinfo(...)[0]`` on an empty list would raise ``IndexError``,
        which is not an ``OSError`` and would reach ``run_checks``' generic
        red row.  The walk has no subscript, so an empty answer is a loop
        body that never runs.

        Args:
            monkeypatch: pytest's patcher.

        """
        recorder = _ProbeRecorder()
        monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(socket, "socket", recorder.socket)
        assert _probe() is checks._SanedOutcome.UNRESOLVED
        assert recorder.constructions == []

    def test_a_resolver_that_raises_is_unresolved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A name that cannot be resolved is UNRESOLVED rather than raising.

        ``socket.gaierror`` subclasses ``OSError``; resolution has its own
        ``except`` arm, so a resolver failure is never mistaken for a refusal.

        Args:
            monkeypatch: pytest's patcher.

        """

        def _fail(*_args: object, **_kwargs: object) -> list[object]:
            msg = "Name or service not known"
            raise socket.gaierror(msg)

        monkeypatch.setattr(socket, "getaddrinfo", _fail)
        assert _probe("scanbox.invalid") is checks._SanedOutcome.UNRESOLVED

    def test_every_address_refusing_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Only a refusal from every address is REFUSED: the host is up, saned is not.

        Args:
            monkeypatch: pytest's patcher.

        """
        recorder = _install_probe_recorder(monkeypatch)
        assert _probe() is checks._SanedOutcome.REFUSED
        assert len(recorder.addresses) == 3

    def test_one_address_timing_out_makes_the_host_timed_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A connect timeout anywhere, with nothing connecting, is TIMED_OUT.

        A refusal proves something is up at that address; a timeout proves
        nothing, and a host that may be silently dropping packets is the one
        that must not be enumerated.

        Args:
            monkeypatch: pytest's patcher.

        """
        silent = _THREE_ADDRESSES[1][4]
        recorder = _install_probe_recorder(
            monkeypatch, peer=_PeerScript(timeout_addresses=frozenset([silent]))
        )
        assert _probe() is checks._SanedOutcome.TIMED_OUT
        assert len(recorder.addresses) == 3

    @pytest.mark.parametrize(
        "code",
        [
            pytest.param(errno.ENETUNREACH, id="no-route-to-that-network"),
            pytest.param(errno.EADDRNOTAVAIL, id="no-local-address"),
            pytest.param(errno.EAFNOSUPPORT, id="no-such-family-here"),
        ],
    )
    def test_an_address_this_machine_cannot_dial_does_not_hide_a_refusal(
        self, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        """
        A dual-stack host whose saned is stopped is REFUSED, not TIMED_OUT.

        Without ``AI_ADDRCONFIG`` a dual-stack name answers its IPv6 address
        first even in a container with no IPv6, and that address fails here at
        once.  That says nothing about the scanner host and must not outweigh
        the IPv4 addresses' definite answer: the host is up and saned is not,
        so the advice is to start saned.  libsane fails the same address just
        as fast, so it is no hang risk.

        Args:
            monkeypatch: pytest's patcher.
            code: The ``errno`` the IPv6 address fails with locally.

        """
        local_only = _THREE_ADDRESSES[0][4]
        recorder = _install_probe_recorder(
            monkeypatch, peer=_PeerScript(connect_errors=((local_only, code),))
        )
        assert _probe() is checks._SanedOutcome.REFUSED
        assert len(recorder.addresses) == 3

    def test_an_unreachable_host_still_counts_as_not_answering(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``EHOSTUNREACH`` is the network saying the host is not there.

        Unlike a family or route this machine lacks, it is what a switched-off
        host on the local network produces once its address stops answering
        ARP, so it is a real non-answer and still outweighs a refusal.

        Args:
            monkeypatch: pytest's patcher.

        """
        unreachable = _THREE_ADDRESSES[1][4]
        _install_probe_recorder(
            monkeypatch,
            peer=_PeerScript(connect_errors=((unreachable, errno.EHOSTUNREACH),)),
        )
        assert _probe() is checks._SanedOutcome.TIMED_OUT

    def test_a_host_no_address_of_which_can_be_dialled_is_timed_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Every address failing locally leaves nothing to report but "not answering".

        Nothing was tried, so nothing refused; the host cannot be reached from
        this machine, which is what the timed-out row tells its reader.

        Args:
            monkeypatch: pytest's patcher.

        """
        _install_probe_recorder(
            monkeypatch,
            peer=_PeerScript(
                connect_errors=tuple(
                    (address, errno.ENETUNREACH) for *_rest, address in _THREE_ADDRESSES
                )
            ),
        )
        assert _probe() is checks._SanedOutcome.TIMED_OUT

    def test_the_handshake_gets_its_own_budget_after_the_connect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A connect that spent its whole budget still leaves the reply its own.

        The scripted clock charges the successful dial its entire allowance,
        so a probe that read the reply against the connect deadline would find
        nothing left and call a healthy host timed out.

        Args:
            monkeypatch: pytest's patcher.

        """
        answering = _THREE_ADDRESSES[0][4]
        _install_probe_recorder(
            monkeypatch, connectable=[answering], clock=_SpendingClock(spend=1.0)
        )
        assert _probe() is checks._SanedOutcome.HEALTHY


_SURFACE_PACKAGES: Final = ("saneless.web", "saneless.cli")


def _imported_modules(source: str, package: str) -> set[str]:
    """
    Name every module an ``import`` statement anywhere in ``source`` reaches.

    Each ``import`` and ``from ... import`` node is read, at any depth, so an
    import inside a function or a ``TYPE_CHECKING`` block counts as much as
    one at the top.  Relative imports are resolved against ``package``, and a
    ``from X import name`` also yields ``X.name``, because ``name`` may be a
    submodule.

    Args:
        source: The module's source text.
        package: The package the module lives in, for relative imports.

    Returns:
        The absolute dotted names reached.

    """
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts = package.split(".")
            base_parts = parts[: len(parts) - node.level + 1] if node.level else []
            if node.module:
                base_parts = [*base_parts, node.module]
            base = ".".join(base_parts)
            modules.add(base)
            modules.update(f"{base}.{alias.name}" for alias in node.names)
    return modules


def _surface_imports(source: str) -> list[str]:
    """
    List the modules under either surface package that ``source`` imports.

    Args:
        source: Source text of a module in the ``saneless`` package.

    Returns:
        The offending module names, sorted.

    """
    return sorted(
        module
        for module in _imported_modules(source, "saneless")
        if any(
            module == surface or module.startswith(f"{surface}.")
            for surface in _SURFACE_PACKAGES
        )
    )


class TestImportHygiene:
    """``checks.py`` is read by both surfaces, so it may depend on neither."""

    def test_checks_imports_neither_web_nor_cli(self) -> None:
        """
        No import statement in ``checks.py`` reaches ``saneless.web`` or ``saneless.cli``.

        This module is the shared registry.  An import of either
        surface would make it that surface's module, and the other one would
        either import a web app to print a terminal table or import Click to
        render a page.  Every dependency is injected instead.
        """
        source = Path(checks.__file__).read_text(encoding="utf-8")
        assert _surface_imports(source) == []

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            pytest.param(
                "def late() -> None:\n    from saneless.web import routes\n",
                ["saneless.web", "saneless.web.routes"],
                id="indented",
            ),
            pytest.param(
                "from . import cli as terminal\n",
                ["saneless.cli"],
                id="relative-aliased",
            ),
            pytest.param(
                "import saneless.web.app\n",
                ["saneless.web.app"],
                id="plain-import",
            ),
            pytest.param(
                '"""Neither saneless.web nor saneless.cli is imported."""\n'
                "from saneless import vocabulary\n",
                [],
                id="prose-only",
            ),
        ],
    )
    def test_the_import_reader_finds_imports_and_ignores_prose(
        self, source: str, expected: list[str]
    ) -> None:
        """
        The reader catches indented, relative and aliased imports, not prose.

        A check that matched lines of text would miss an import inside a
        function and flag a docstring naming a surface; this one does neither.

        Args:
            source: Module text to read.
            expected: The surface modules it must report.

        """
        assert _surface_imports(source) == expected


# ---------------------------------------------------------------------------
# The six checks
# ---------------------------------------------------------------------------


class _CountingBackend(StubScannerBackend):
    """
    A backend that reports what it is told to and counts the times it is asked.

    Both ways the Scanner check can enter SANE are counted: ``calls`` for a
    device listing and ``opens`` for opening a device.  The check makes one
    ``list_and_open`` call, whose base-class default lists through
    ``get_devices`` and opens through ``open_and_close`` and then
    ``get_capabilities``, so counting there counts the check's listings and
    opens.
    """

    def __init__(self, devices: list[DeviceInfo] | None = None) -> None:
        """
        Record the devices this backend will report.

        Args:
            devices: What ``get_devices`` should return; empty when omitted.

        """
        self.devices = devices if devices is not None else []
        self.calls = 0
        self.opens = 0

    def get_devices(self) -> list[DeviceInfo]:
        """
        Report the configured devices and count the call.

        Returns:
            The devices this stub was built with.

        """
        self.calls += 1
        return self.devices

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """
        Count the open, then answer the way the plain stub does.

        Args:
            device_id: The device being opened.

        Returns:
            The stub's capabilities.

        """
        self.opens += 1
        return super().get_capabilities(device_id)


class _UnopenableBackend(_CountingBackend):
    """A backend that lists what it is told to and cannot open any device."""

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """
        Count the open, then fail it the way a missing device fails.

        Args:
            device_id: The device being opened.

        Returns:
            Never returns.

        Raises:
            ScanError: Always, with text that must not reach a row or a log.

        """
        self.opens += 1
        msg = f"cannot open {device_id}"
        raise ScanError(msg)


class _RaisingBackend(_CountingBackend):
    """A backend whose enumeration fails the way a wedged SANE fails."""

    def __init__(self) -> None:
        """Start with no calls and no opens recorded."""
        super().__init__()

    def get_devices(self) -> list[DeviceInfo]:
        """
        Fail the way ``SaneBackend.get_devices`` fails.

        Returns:
            Never returns.

        Raises:
            ScanError: Always.

        """
        self.calls += 1
        msg = "SANE is wedged"
        raise ScanError(msg)


class _ListingFailureBackend(_CountingBackend):
    """
    A backend whose isolated list-then-open fails in one given way.

    It still counts direct listings and opens, which the check must not make:
    its only way into SANE is the one ``list_and_open`` call.
    """

    def __init__(self, error: ScanError) -> None:
        """
        Fail every list-then-open with ``error``.

        Args:
            error: What ``list_and_open`` raises.

        """
        super().__init__()
        self.error = error

    def list_and_open(
        self, open_if_unlisted: str, *, abort: threading.Event | None = None
    ) -> DeviceSurvey:
        """
        Fail the way the configured error says.

        Args:
            open_if_unlisted: The id the check asked to have opened.
            abort: The caller's abort Event, unused.

        Returns:
            Never returns.

        Raises:
            ScanError: Always, the error this backend was built with.

        """
        _ = open_if_unlisted, abort
        raise self.error


class _SurveyRecordingBackend(_CountingBackend):
    """
    A backend that answers every list-then-open with one survey, and records it.

    ``asked`` holds the id each call was asked to open, in order.  The direct
    listing and open counters it inherits must stay at zero.
    """

    def __init__(self, survey: DeviceSurvey) -> None:
        """
        Answer with ``survey``.

        Args:
            survey: What every ``list_and_open`` returns.

        """
        super().__init__(list(survey.devices))
        self.survey = survey
        self.asked: list[str] = []

    def list_and_open(
        self, open_if_unlisted: str, *, abort: threading.Event | None = None
    ) -> DeviceSurvey:
        """
        Record the id the check asked to have opened, and answer.

        Args:
            open_if_unlisted: The id the check asked to have opened.
            abort: The caller's abort Event, unused.

        Returns:
            The survey this backend was built with.

        """
        _ = abort
        self.asked.append(open_if_unlisted)
        return self.survey


class _AbortedListingBackend(_CountingBackend):
    """
    A backend whose listing is stopped part way, as a stopping caller's is.

    ``list_and_open`` records the abort Event it was handed, sets it the way
    the stopping thread would, and then raises what the launcher raises once
    it has killed and reaped the child.
    """

    def __init__(self) -> None:
        """Record no abort Event yet."""
        super().__init__()
        self.aborts: list[threading.Event | None] = []

    def list_and_open(
        self, open_if_unlisted: str, *, abort: threading.Event | None = None
    ) -> DeviceSurvey:
        """
        Record the abort, set it, and raise the aborted listing.

        Args:
            open_if_unlisted: The id the check asked to have opened.
            abort: The caller's abort Event.

        Returns:
            Never returns.

        Raises:
            ListingAbortedError: Always.

        """
        _ = open_if_unlisted
        self.aborts.append(abort)
        if abort is not None:
            abort.set()
        msg = "The scanner listing was stopped because saneless is stopping"
        raise ListingAbortedError(msg)


# The id ``_device()`` reports, and the ``scanner.device`` ``_settings()``
# configures, so a "ready" test goes down the path where the configured device
# is listed rather than the one where it has to be opened to be found.
_DEVICE_ID: Final = "net:scanbox.lan:brother5:bus0;dev1"

# A configured device that is not a ``net:`` device, as a USB scanner is.
_LOCAL_DEVICE_ID: Final = "epson2:libusb:001:004"


def _device(vendor: str = "Brother", model: str = "ADS-2700W") -> DeviceInfo:
    """
    Build a DeviceInfo whose SANE id names a host, as a net-backend id does.

    Args:
        vendor: The device's vendor string.
        model: The device's model string.

    Returns:
        A DeviceInfo for the scanner check to describe.

    """
    return DeviceInfo(
        name=_DEVICE_ID,
        vendor=vendor,
        model=model,
        device_type="scanner",
    )


class _RequestCounter:
    """Counts the HTTP requests a Paperless check issues, and their timeouts."""

    def __init__(self, responder: Callable[[httpx2.Request], httpx2.Response]) -> None:
        """
        Wrap a responder so every call through it is recorded.

        Args:
            responder: What the stub server answers with.

        """
        self.responder = responder
        self.count = 0
        self.timeouts: list[dict[str, float | None]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """
        Record the request and delegate to the responder.

        Args:
            request: The request the client issued.

        Returns:
            Whatever the responder answers.

        """
        self.count += 1
        self.timeouts.append(request.extensions["timeout"])
        return self.responder(request)


def _ok_response(_request: httpx2.Request) -> httpx2.Response:
    """
    Answer every request with an empty, successful tag page.

    Args:
        _request: Ignored.

    Returns:
        A 200 with an empty result list.

    """
    return httpx2.Response(200, json={"count": 0, "results": []})


def _paperless(
    counter: _RequestCounter, url: str = "http://paperless:8000"
) -> PaperlessClient:
    """
    Build a Paperless client wired to a counting mock transport.

    Args:
        counter: The recorder every request passes through.
        url: The base URL to configure.

    Returns:
        A client that issues no real network traffic.

    """
    return PaperlessClient(
        url=url, token=_REAL_TOKEN, transport=httpx2.MockTransport(counter)
    )


def _candidates(root: Path) -> tuple[Path, Path, Path]:
    """
    Make three empty search directories and name the candidate in each.

    The three stand for ``./``, ``$XDG_CONFIG_HOME/saneless/`` and
    ``/etc/saneless/`` in that order, so a test can say "the file is in the
    third searched place" without going anywhere near the real ``/etc``.

    Args:
        root: The directory the three search directories are made under.

    Returns:
        The three candidate paths, in search order; none of them exists yet.

    """
    directories = [root / name for name in ("cfg-cwd", "cfg-xdg", "cfg-etc")]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    return (
        directories[0] / CONFIG_FILENAME,
        directories[1] / CONFIG_FILENAME,
        directories[2] / CONFIG_FILENAME,
    )


def _write_stale(candidate: Path) -> Path:
    """
    Put a superseded-name file beside one candidate.

    Args:
        candidate: The current-name candidate whose directory gets the file.

    Returns:
        The path written.

    """
    stale = candidate.with_name(LEGACY_CONFIG_FILENAME)
    stale.write_text("", encoding="utf-8")
    return stale


def _discovery(
    tmp_path: Path,
    *,
    loaded: int | None = None,
    stale: tuple[int, ...] = (),
    also_found: tuple[int, ...] = (),
) -> ConfigDiscovery:
    """
    Record a real search over three temporary candidates.

    The files are written and then ``discover_config`` is run over them, rather
    than a ``ConfigDiscovery`` being constructed by hand: a hand-built
    recording can describe a filesystem that could not exist.

    The search directories are separate from the ones ``_settings`` writes its
    healthy default into, so attaching this recording afterwards cannot find
    that file and quietly report a different state than the test asked for.

    Args:
        tmp_path: The test's own directory.
        loaded: The index whose current-name file exists, or None for a search
            that found nothing to load.
        stale: The indexes whose directories also hold a superseded-name file.
        also_found: The indexes whose current-name file also exists, after
            ``loaded`` in search order, so the search finds more than one.

    Returns:
        The recording that search produced.

    """
    candidates = _candidates(tmp_path / "search")
    if loaded is not None:
        candidates[loaded].write_text("", encoding="utf-8")
    for index in also_found:
        candidates[index].write_text("", encoding="utf-8")
    for index in stale:
        _write_stale(candidates[index])
    return discover_config(candidates)


def _settings(
    tmp_path: Path,
    *,
    host: str = "",
    token: str = _REAL_TOKEN,
    consume_dir: str = "",
    data_dir: str | None = None,
) -> Settings:
    """
    Build a Settings object whose every path is inside the test's tmp_path.

    The working folder exists and is private, created 0700 the way saneless
    creates it, so the Data folder row is green unless a test says otherwise.

    A healthy discovery is attached: a file loaded from the first searched
    place, and no superseded-name file anywhere.  Without it every test in
    this file would carry an amber Configuration row it never asked for,
    because a directly constructed ``Settings`` has run no search at all.
    Tests about the other three states say so with ``_with_discovery``.

    Args:
        tmp_path: The test's own directory.
        host: The configured ``scanner.host``.
        token: The configured paperless token.
        consume_dir: The fallback folder, empty when not configured.
        data_dir: The data folder; defaults to a writable one under tmp_path.

    Returns:
        A Settings instance touching nothing outside tmp_path.

    """
    if data_dir is None:
        resolved = tmp_path / "data"
        resolved.mkdir(exist_ok=True)
        data_dir = str(resolved)
    scratch = tmp_path / "tmp"
    scratch.mkdir(mode=0o700, exist_ok=True)
    settings = Settings(
        scanner=ScannerConfig(host=host, device=_DEVICE_ID),
        paperless=PaperlessConfig(
            url="http://paperless:8000", token=token, consume_dir=consume_dir
        ),
        output=OutputConfig(
            tmp_dir=str(scratch),
            data_dir=data_dir,
            log_file=str(tmp_path / "saneless.log"),
        ),
        profiles={"default": ProfileConfig()},
    )
    healthy = _candidates(tmp_path / "loaded")
    healthy[0].write_text("", encoding="utf-8")
    return _with_discovery(settings, discover_config(healthy))


def _with_discovery(settings: Settings, discovery: ConfigDiscovery | None) -> Settings:
    """
    Attach a recording of the config search to settings already built.

    The private attributes are written directly, the way ``load_settings``
    writes them and the way the doctor fake in ``tests/test_doctor.py``
    already does: they are deliberately not fields, so no public setter
    exists and none should.

    Args:
        settings: The settings to record against, modified in place.
        discovery: The recording, or None for settings that ran no search.

    Returns:
        The same settings, for use as an expression.

    """
    settings._config_discovery = discovery
    settings._config_path = None if discovery is None else discovery.loaded
    return settings


def _with_profiles(settings: Settings, profiles: dict[str, ProfileConfig]) -> Settings:
    """
    Return the same settings carrying a different profile set.

    Args:
        settings: The base settings.
        profiles: The profiles to substitute, possibly none at all.

    Returns:
        A copy whose ``profiles`` is exactly what was passed.

    """
    return settings.model_copy(update={"profiles": profiles})


def _with_tmp_dir(settings: Settings, tmp_dir: Path) -> Settings:
    """
    Return the same settings with a different ``output.tmp_dir``.

    Args:
        settings: The base settings.
        tmp_dir: The working folder to substitute.

    Returns:
        A copy whose working folder is exactly what was passed.

    """
    output = settings.output.model_copy(update={"tmp_dir": tmp_dir})
    return settings.model_copy(update={"output": output})


def _context(
    settings: Settings,
    *,
    scanner: StubScannerBackend | None = None,
    paperless: PaperlessClient | None = None,
    profile_storage: ProfileStorage = ProfileStorage.PERSISTED,
    skip_scanner: bool = False,
) -> CheckContext:
    """
    Assemble a CheckContext from the pieces a test cares about.

    Args:
        settings: The configuration the checks read.
        scanner: The backend, or None for "python-sane is not installed".
        paperless: The client, or None for "no usable client was built".
        profile_storage: What the worker's profile write actually did.
        skip_scanner: Whether a scan is running.

    Returns:
        A ready CheckContext.

    """
    return CheckContext(
        settings=settings,
        scanner=scanner,
        paperless=paperless,
        profile_storage=profile_storage,
        skip_scanner=skip_scanner,
    )


def _row(results: tuple[CheckResult, ...], key: CheckKey) -> CheckResult:
    """
    Pick one check's row out of a full run.

    Args:
        results: Everything ``run_checks`` returned.
        key: The row wanted.

    Returns:
        The single result carrying that key.

    """
    return next(result for result in results if result.key is key)


def _strip(step: str) -> str:
    """
    Render a row's next step the way the status strip shows it.

    A next step that says to try again is stored with a placeholder, and each
    surface spells the retry its own way.  The sentences these tests pin are
    the strip's, so a stored step is compared after rendering it for the strip.

    Args:
        step: The next step as the registry stored it.

    Returns:
        The sentence the strip shows.

    """
    return render_check_step(step, CheckSurface.STRIP)


def _without_allowed_spellings(text: str) -> str:
    """
    Remove the three documented path spellings a row may legitimately carry.

    What is left is what the no-path rule applies to unchanged, so the
    exception cannot widen without this helper being changed too.

    Args:
        text: A rendered message or next step.

    Returns:
        The same text with every allowed spelling removed.

    """
    remainder = text
    for spelling in _ALLOWED_PATH_SPELLINGS:
        remainder = remainder.replace(spelling, "")
    return remainder


def _recording_dialler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcome: checks._SanedOutcome = checks._SanedOutcome.HEALTHY,
    outcomes: Mapping[str, checks._SanedOutcome] | None = None,
    port_outcomes: Mapping[tuple[str, int], checks._SanedOutcome] | None = None,
) -> list[tuple[str, int]]:
    """
    Replace the saned probe with one that records what it was asked for.

    Nothing connects: the point of these cases is *which address* the check
    decided to dial, and what it made of each answer, which is decided without
    any socket existing.

    Args:
        monkeypatch: pytest's attribute patcher.
        outcome: What the stubbed probe answers for any host.
        outcomes: A per-host answer, which wins over ``outcome`` for the
            hosts it names.
        port_outcomes: An answer for one host on one port, which wins over
            both of the others for the ``(host, port)`` pairs it names.

    Returns:
        The list the stub appends each ``(host, port)`` pair to.

    """
    dialled: list[tuple[str, int]] = []
    per_host = dict(outcomes or {})
    per_address = dict(port_outcomes or {})

    def _probe_stub(
        host: str,
        port: int,
        _connect_timeout: float,
        _handshake_timeout: float,
        _abort: threading.Event | None = None,
    ) -> checks._SanedOutcome:
        dialled.append((host, port))
        return per_address.get((host, port), per_host.get(host, outcome))

    monkeypatch.setattr(checks, "_probe_saned", _probe_stub)
    return dialled


def _healthy_settings(tmp_path: Path, *, host: str) -> Settings:
    """
    Build settings whose only imperfection is the configured scanner host.

    Args:
        tmp_path: The test's own directory.
        host: The ``scanner.host`` to configure.

    Returns:
        Settings with a writable data folder and a real fallback folder, so
        every row but the Scanner one is green.

    """
    folder = tmp_path / "consume"
    folder.mkdir(exist_ok=True)
    return _settings(tmp_path, host=host, consume_dir=str(folder))


# The one outcome that ends the scanner check before enumeration, and the
# wording its amber row carries, then the two rows a refused host gets once it
# has been enumerated: red when nothing usable is listed, amber beside a
# scanner that works.  Pinned here, word for word, because the next step is
# the part of the row a household member acts on.
_TIMED_OUT_MESSAGE: Final = (
    "The scanner host is not answering, so the scanner could not be checked."
)
_TIMED_OUT_NEXT_STEP: Final = (
    "Check the scanner host is switched on and on the network, then press Check again."
)
_REFUSED_MESSAGE: Final = (
    "The scanner host is on, but its scanner service is not running."
)
_REFUSED_READY_MESSAGE: Final = (
    "Brother ADS-2700W is ready, but the scanner service is not running "
    "on the scanner host."
)
_REFUSED_NEXT_STEP: Final = (
    "Start saned on the scanner host, or check it is listening on the network, "
    "then press Check again."
)

# The amber row for an unlisted ``net:`` device whose host the pre-probe
# cannot dial, so the check never opens it.
_UNPROBED_DEVICE_MESSAGE: Final = (
    "The configured scanner is not listed, and its host cannot be checked "
    "in advance, so the scanner could not be checked."
)
_UNPROBED_DEVICE_NEXT: Final = (
    "Add the configured scanner's host to [scanner] host, or set [scanner] "
    "device to one saneless devices lists, then restart saneless."
)

# Two scanner hosts in one setting, so the count wording and the
# probe-every-entry rule have something to count.
_TWO_HOSTS: Final = "scanbox-a.lan:scanbox-b.lan"

# The two surfaces that show the Scanner row: ``saneless doctor``, which runs
# the ungated check, and the status strip, which runs it under the scanner gate.
_SURFACES: Final = ("doctor", "strip")


def _scanner_row_on(surface: str, context: CheckContext) -> CheckResult:
    """
    Run the Scanner check the way one surface runs it.

    Args:
        surface: ``"doctor"`` for the ungated check, ``"strip"`` for the full
            registry run with a free scanner gate handed in.
        context: The context to check.

    Returns:
        The Scanner row that surface would show.

    Raises:
        ValueError: If ``surface`` is not one of ``_SURFACES``.

    """
    if surface == "doctor":
        return checks._check_scanner(context)
    if surface == "strip":
        gate = cast("threading.Lock", _RecordingLock())
        return _row(run_checks(context, scanner_gate=gate), CheckKey.SCANNER)
    msg = f"unknown surface: {surface}"
    raise ValueError(msg)


class TestScannerCheck:
    """The Scanner row, including the pre-probe that keeps it fast (APPL-02)."""

    def test_a_named_device_is_ready(self, tmp_path: Path) -> None:
        """
        A reported device is named in the row a household member reads.

        Args:
            tmp_path: The test's own directory.

        """
        backend = _CountingBackend([_device()])
        results = run_checks(_context(_settings(tmp_path), scanner=backend))
        row = _row(results, CheckKey.SCANNER)
        assert row.state is CheckState.OK
        assert row.message == "Brother ADS-2700W is ready."
        assert row.next_step == ""

    def test_an_unnamed_device_is_just_ready(self, tmp_path: Path) -> None:
        """
        A device the backend cannot describe still reports ready.

        Args:
            tmp_path: The test's own directory.

        """
        backend = _CountingBackend([_device(vendor="", model="")])
        results = run_checks(_context(_settings(tmp_path), scanner=backend))
        row = _row(results, CheckKey.SCANNER)
        assert row.state is CheckState.OK
        assert row.message == "Ready."

    def test_the_sane_device_id_is_never_rendered(self, tmp_path: Path) -> None:
        """
        The row names the model, never the SANE id, which embeds the host.

        A ``net`` backend device id is ``net:<host>:<backend>:...``.  Putting
        it on the index page would publish a LAN address to everyone who can
        load the page, which is the same reason the fallback row omits the
        folder path (ASVS V7, T-30-21).

        Args:
            tmp_path: The test's own directory.

        """
        backend = _CountingBackend([_device()])
        results = run_checks(_context(_settings(tmp_path), scanner=backend))
        row = _row(results, CheckKey.SCANNER)
        assert "scanbox.lan" not in row.message
        assert "net:" not in row.message

    def test_no_devices_and_no_host_is_no_scanner_found(self, tmp_path: Path) -> None:
        """
        A backend that finds nothing, with no host to blame, finds no scanner.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_device(_settings(tmp_path), "")
        results = run_checks(_context(settings, scanner=_CountingBackend()))
        row = _row(results, CheckKey.SCANNER)
        assert row.state is CheckState.FAIL
        assert row.message == "No scanner was found."
        assert _strip(row.next_step) == _NOTHING_FOUND_NEXT
        assert "Not reachable" not in row.message

    def test_a_raising_backend_finds_no_scanner(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        An enumeration that throws is a red row and a log line, not a crash.

        The log line carries the exception's type and nothing from its text,
        which is where a device id or a host would be.

        Args:
            tmp_path: The test's own directory.
            caplog: pytest's log capture.

        """
        settings = _with_device(_settings(tmp_path), "")
        backend = _RaisingBackend()
        with caplog.at_level(logging.WARNING, logger="saneless.checks"):
            results = run_checks(_context(settings, scanner=backend))
        row = _row(results, CheckKey.SCANNER)
        assert row.state is CheckState.FAIL
        assert row.message == "No scanner was found."
        assert "Not reachable" not in row.message
        assert backend.opens == 0
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.name == "saneless.checks" and record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "ScanError" in warnings[0]
        assert "SANE is wedged" not in warnings[0]

    def test_a_raising_backend_with_a_configured_device_that_opens_is_ready(
        self, tmp_path: Path
    ) -> None:
        """
        A pinned device that opens is usable even when listing fails.

        A scan opens the configured id without listing anything first, so a
        failed listing says nothing about whether that scan would work.

        Args:
            tmp_path: The test's own directory.

        """
        backend = _RaisingBackend()
        results = run_checks(_context(_settings(tmp_path), scanner=backend))
        row = _row(results, CheckKey.SCANNER)
        assert row.state is CheckState.OK
        assert row.message == "The configured scanner is ready."
        assert backend.calls == 1
        assert backend.opens == 1

    def test_the_not_reachable_row_is_gone(self) -> None:
        """
        The old red row that blamed the scanner for every empty listing is gone.

        Its advice, "switched on and connected ... press Check again", was
        wrong for a host that refuses this machine, which was the case that
        produced it.
        """
        assert not hasattr(checks, "_scanner_unreachable")

    @pytest.mark.parametrize("surface", _SURFACES)
    def test_a_rejecting_host_with_nothing_listed_is_refusing_this_machine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
    ) -> None:
        """
        The 2026-09-22 case reads as a denial, and names the file that grants access.

        saned accepted the connection and then reset it during the opening
        handshake, so enumeration found nothing.  The row used to say the
        scanner was not reachable and to check it was switched on, which was
        wrong on both counts.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            surface: Which of the two surfaces runs the check.

        """
        _recording_dialler(monkeypatch, outcome=checks._SanedOutcome.REJECTED)
        backend = _CountingBackend()
        settings = _with_device(_settings(tmp_path, host="scanbox.lan"), "")
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert backend.calls == 1
        assert row.state is CheckState.FAIL
        assert row.message == "The scanner host is refusing this machine."
        assert "saned.conf" in row.next_step
        for text in (row.message, row.next_step):
            assert "Not reachable" not in text
            assert "switched on and connected" not in text

    @pytest.mark.parametrize("surface", _SURFACES)
    def test_a_rejecting_host_beside_a_usable_scanner_is_amber(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
    ) -> None:
        """
        A scanner that works keeps the row out of red, whatever a host says.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            surface: Which of the two surfaces runs the check.

        """
        _recording_dialler(monkeypatch, outcome=checks._SanedOutcome.REJECTED)
        backend = _CountingBackend([_device()])
        settings = _with_device(_settings(tmp_path, host="scanbox.lan"), "")
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert row.state is CheckState.WARN
        assert row.message == (
            "Brother ADS-2700W is ready, but the scanner host is refusing this machine."
        )
        assert _strip(row.next_step) == _REJECTED_NEXT

    @pytest.mark.parametrize("surface", _SURFACES)
    def test_an_unresolved_host_is_enumerated_and_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
    ) -> None:
        """
        A name that did not resolve costs libsane nothing, so it is enumerated.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            surface: Which of the two surfaces runs the check.

        """
        _recording_dialler(monkeypatch, outcome=checks._SanedOutcome.UNRESOLVED)
        backend = _CountingBackend()
        settings = _with_device(_settings(tmp_path, host="scanbox.lan"), "")
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert backend.calls == 1
        assert row.state is CheckState.FAIL
        assert row.message == "The scanner host could not be found by name."
        assert _strip(row.next_step) == _UNRESOLVED_NEXT

    @pytest.mark.parametrize("surface", _SURFACES)
    def test_an_unresolved_name_from_the_environment_points_at_the_variable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
    ) -> None:
        """
        A name exported in ``SANE_NET_HOSTS`` is fixed there, and the row says so.

        A non-empty exported variable wins over ``[scanner] host``, so a next
        step that only named the config file would send the operator to edit
        a setting that changes nothing, and the row would never clear.  The
        variable is named; the host never is.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            surface: Which of the two surfaces runs the check.

        """
        monkeypatch.setenv("SANE_NET_HOSTS", "env-scanbox.lan")
        dialled = _recording_dialler(
            monkeypatch, outcome=checks._SanedOutcome.UNRESOLVED
        )
        settings = _with_device(_settings(tmp_path, host="cfg-scanbox.lan"), "")
        row = _scanner_row_on(surface, _context(settings, scanner=_CountingBackend()))
        assert dialled == [("env-scanbox.lan", SANED_PORT)]
        assert row.state is CheckState.FAIL
        assert row.message == "The scanner host could not be found by name."
        assert "SANE_NET_HOSTS" in row.next_step
        assert "restart saneless" in row.next_step
        assert "scanbox" not in f"{row.message} {row.next_step}"

    @pytest.mark.parametrize("surface", _SURFACES)
    def test_a_host_that_must_not_be_enumerated_is_neither_listed_nor_opened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
    ) -> None:
        """
        A silent host never reaches libsane, not even to open a device.

        The configured device is one the backend would not list, which is
        exactly the case that would otherwise open it.  libsane has no read
        timeout, so enumerating a host that does not answer would wait on it
        for as long as it stays silent, and opening a device on it is no
        different.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            surface: Which of the two surfaces runs the check.

        """
        _recording_dialler(monkeypatch, outcome=checks._SanedOutcome.TIMED_OUT)
        backend = _CountingBackend()
        settings = _with_device(
            _settings(tmp_path, host="scanbox.lan"), _LOCAL_DEVICE_ID
        )
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert backend.calls == 0
        assert backend.opens == 0
        assert row.state is CheckState.WARN

    @pytest.mark.parametrize("surface", _SURFACES)
    def test_a_refused_host_is_listed_and_an_unlisted_device_opened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
    ) -> None:
        """
        A refused host returns at once inside libsane, so the check goes on.

        The configured device is one the backend would not list, so the check
        opens it, and a device that opens is one a scan can use: the row is
        amber, naming the stopped scanner service, never red.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            surface: Which of the two surfaces runs the check.

        """
        _recording_dialler(monkeypatch, outcome=checks._SanedOutcome.REFUSED)
        backend = _CountingBackend()
        settings = _with_device(
            _settings(tmp_path, host="scanbox.lan"), _LOCAL_DEVICE_ID
        )
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert backend.calls == 1
        assert backend.opens == 1
        assert row.state is CheckState.WARN
        assert row.message == (
            "The configured scanner is ready, but the scanner service is not "
            "running on the scanner host."
        )
        assert _strip(row.next_step) == _REFUSED_NEXT_STEP

    @pytest.mark.parametrize("surface", _SURFACES)
    @pytest.mark.parametrize(
        "setting",
        [
            pytest.param(("", 1), id="no-host-configured"),
            pytest.param(("a.lan:b.lan:c.lan:d.lan:scanbox.lan", 5), id="past-the-cap"),
        ],
    )
    def test_a_configured_net_device_on_an_unprobed_dead_host_is_never_opened(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        setting: tuple[str, int],
        surface: str,
    ) -> None:
        """
        A configured ``net:`` device's own host is probed before SANE is entered.

        The device is not listed, which is the case that opens it.  libsane's
        net backend dials the device's host when the device is opened, with no
        timeout, so a switched-off host there is the ~127 s uninterruptible
        connect the pre-probe exists to prevent -- and on the status strip it
        would be paid holding the scanner gate.  The host is not among the
        probed setting entries (none is configured, or it is past the cap), so
        the preflight probes it on its own, and a host that does not answer
        ends the check before libsane is touched at all.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            setting: The configured ``scanner.host``, and how many hosts the
                preflight must probe for it.
            surface: Which of the two surfaces runs the check.

        """
        host, expected_dials = setting
        dialled = _recording_dialler(
            monkeypatch, outcomes={"scanbox.lan": checks._SanedOutcome.TIMED_OUT}
        )
        backend = _CountingBackend()
        settings = _settings(tmp_path, host=host)
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert ("scanbox.lan", SANED_PORT) in dialled
        assert len(dialled) == expected_dials
        assert backend.calls == 0
        assert backend.opens == 0
        assert row.state is CheckState.WARN
        if host:
            assert "1 of 5 scanner hosts" in row.message
        else:
            assert row.message == _TIMED_OUT_MESSAGE

    @pytest.mark.parametrize("surface", _SURFACES)
    @pytest.mark.parametrize(
        ("setting", "hosts_subject"),
        [
            pytest.param(("", 1), "the scanner host", id="no-host-configured"),
            pytest.param(
                ("a.lan:b.lan:c.lan:d.lan:scanbox.lan", 5),
                "1 of 5 scanner hosts",
                id="past-the-cap",
            ),
        ],
    )
    def test_a_configured_net_device_on_an_unprobed_refusing_host_is_opened(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        setting: tuple[str, int],
        hosts_subject: str,
        surface: str,
    ) -> None:
        """
        A refusing own host is probed, then listed and the device opened.

        A refused connect returns at once inside libsane, so it costs the open
        nothing, and the open is how the check learns whether a scan could use
        the device.  This one opens, so the row is amber and names the stopped
        scanner service without naming the host.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            setting: The configured ``scanner.host``, and how many hosts the
                preflight must probe for it.
            hosts_subject: How the row counts the refusing host.
            surface: Which of the two surfaces runs the check.

        """
        host, expected_dials = setting
        dialled = _recording_dialler(
            monkeypatch, outcomes={"scanbox.lan": checks._SanedOutcome.REFUSED}
        )
        backend = _CountingBackend()
        settings = _settings(tmp_path, host=host)
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert ("scanbox.lan", SANED_PORT) in dialled
        assert len(dialled) == expected_dials
        assert backend.calls == 1
        assert backend.opens == 1
        assert row.state is CheckState.WARN
        assert row.message == (
            "The configured scanner is ready, but the scanner service is not "
            f"running on {hosts_subject}."
        )
        assert _strip(row.next_step) == _REFUSED_NEXT_STEP

    @pytest.mark.parametrize("surface", _SURFACES)
    def test_a_configured_net_device_on_a_probed_host_is_dialled_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
    ) -> None:
        """
        The configured device's host is not probed twice when the setting names it.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            surface: Which of the two surfaces runs the check.

        """
        dialled = _recording_dialler(monkeypatch)
        backend = _CountingBackend([_device()])
        settings = _settings(tmp_path, host="scanbox.lan")
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert dialled == [("scanbox.lan", SANED_PORT)]
        assert row.state is CheckState.OK

    @pytest.mark.parametrize("surface", _SURFACES)
    @pytest.mark.parametrize(
        ("outcome", "entered"),
        [
            pytest.param(checks._SanedOutcome.REFUSED, 1, id="refused"),
            pytest.param(checks._SanedOutcome.TIMED_OUT, 0, id="timed-out"),
        ],
    )
    def test_a_configured_net_device_is_probed_on_the_port_libsane_dials(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        outcome: checks._SanedOutcome,
        entered: int,
        surface: str,
    ) -> None:
        """
        A setting's probe on another port does not cover the device's host.

        ``scanbox.lan:7000`` is probed on port 7000, but libsane opens a
        ``net:scanbox.lan:...`` device on saned's registered port.  The answer
        on 7000 says nothing about that port, so the device's host is probed
        on it as well.  A silent answer there ends the check before libsane
        is touched: no listing, and no open.  A refusal there does not,
        because a refused connect returns at once inside libsane, so the
        device is listed and then opened.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            outcome: What the device's host answers on saned's registered port.
            entered: How many listings, and how many opens, libsane sees.
            surface: Which of the two surfaces runs the check.

        """
        dialled = _recording_dialler(
            monkeypatch, port_outcomes={("scanbox.lan", SANED_PORT): outcome}
        )
        backend = _CountingBackend()
        settings = _settings(tmp_path, host="scanbox.lan:7000")
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert dialled == [("scanbox.lan", 7000), ("scanbox.lan", SANED_PORT)]
        assert backend.calls == entered
        assert backend.opens == entered
        assert row.state is CheckState.WARN

    @pytest.mark.parametrize("surface", _SURFACES)
    def test_a_device_host_answering_on_the_registered_port_may_be_opened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
    ) -> None:
        """
        Once the port libsane dials has answered, an unlisted device is opened.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            surface: Which of the two surfaces runs the check.

        """
        dialled = _recording_dialler(monkeypatch)
        backend = _CountingBackend()
        settings = _settings(tmp_path, host="scanbox.lan:7000")
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert dialled == [("scanbox.lan", 7000), ("scanbox.lan", SANED_PORT)]
        assert backend.calls == 1
        assert backend.opens == 1
        assert row.state is CheckState.OK

    @pytest.mark.parametrize("surface", _SURFACES)
    @pytest.mark.parametrize(
        "device",
        [
            pytest.param("net:[fe80::1]:brother5:bus0;dev1", id="ipv6-literal"),
            pytest.param("net:127.1:brother5:bus0;dev1", id="numeric-shorthand"),
            pytest.param("net:[fe80::1", id="no-entry"),
        ],
    )
    def test_a_configured_net_device_whose_host_cannot_be_probed_is_never_opened(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        device: str,
        surface: str,
    ) -> None:
        """
        An unlisted ``net:`` device on a host the probe cannot dial is not opened.

        Opening it would dial that host from inside libsane with no timeout,
        and nothing has shown the host is up.  The listing still runs, as it
        did before this device was looked at, and the row is amber: nothing
        was found wrong, but the configured scanner could not be checked.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            device: The configured ``scanner.device``.
            surface: Which of the two surfaces runs the check.

        """
        dialled = _recording_dialler(monkeypatch)
        backend = _CountingBackend([_device()])
        settings = _with_device(_settings(tmp_path), device)
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert dialled == []
        assert backend.calls == 1
        assert backend.opens == 0
        assert row.state is CheckState.WARN
        assert row.message == _UNPROBED_DEVICE_MESSAGE
        assert row.next_step == _UNPROBED_DEVICE_NEXT

    @pytest.mark.parametrize("surface", _SURFACES)
    def test_a_listed_net_device_whose_host_cannot_be_probed_is_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
    ) -> None:
        """
        A device the backend lists needs no open, so an unprobeable host is moot.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            surface: Which of the two surfaces runs the check.

        """
        _recording_dialler(monkeypatch)
        device = DeviceInfo(
            name="net:[fe80::1]:brother5:bus0;dev1",
            vendor="Brother",
            model="ADS-2700W",
            device_type="scanner",
        )
        backend = _CountingBackend([device])
        settings = _with_device(_settings(tmp_path), device.name)
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert backend.opens == 0
        assert row.state is CheckState.OK
        assert row.message == "Brother ADS-2700W is ready."

    @pytest.mark.parametrize("surface", _SURFACES)
    def test_a_configured_device_that_is_not_listed_and_does_not_open_is_red(
        self, tmp_path: Path, surface: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        The configured device is the one checked, not whatever is listed first.

        The log line for the failed open names the exception's type only:
        neither the configured id nor the exception's text reaches it.

        Args:
            tmp_path: The test's own directory.
            surface: Which of the two surfaces runs the check.
            caplog: pytest's log capture.

        """
        backend = _UnopenableBackend([_device()])
        settings = _with_device(_settings(tmp_path), _LOCAL_DEVICE_ID)
        with caplog.at_level(logging.WARNING, logger="saneless.checks"):
            row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert backend.opens == 1
        assert row.state is CheckState.FAIL
        assert row.message == "The configured scanner was not found."
        assert "saneless devices" in row.next_step
        logged = " ".join(record.getMessage() for record in caplog.records)
        assert "ScanError" in logged
        assert _LOCAL_DEVICE_ID not in logged
        assert "cannot open" not in logged

    @pytest.mark.parametrize("surface", _SURFACES)
    def test_a_configured_device_that_is_not_listed_but_opens_is_ready(
        self, tmp_path: Path, surface: str
    ) -> None:
        """
        SANE opens ids it never lists, so an unlisted device that opens is fine.

        Args:
            tmp_path: The test's own directory.
            surface: Which of the two surfaces runs the check.

        """
        backend = _CountingBackend([_device()])
        settings = _with_device(_settings(tmp_path), _LOCAL_DEVICE_ID)
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert backend.opens == 1
        assert row.state is CheckState.OK
        assert row.message == "The configured scanner is ready."

    @pytest.mark.parametrize("surface", _SURFACES)
    @pytest.mark.parametrize(
        "device",
        [
            pytest.param(_DEVICE_ID, id="listed"),
            pytest.param("", id="unset"),
        ],
    )
    def test_no_device_is_opened_unless_the_configured_one_is_missing(
        self, tmp_path: Path, device: str, surface: str
    ) -> None:
        """
        The open is a fallback for an unlisted device, never a routine step.

        Args:
            tmp_path: The test's own directory.
            device: The configured ``scanner.device``.
            surface: Which of the two surfaces runs the check.

        """
        backend = _CountingBackend([_device()])
        settings = _with_device(_settings(tmp_path), device)
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert backend.opens == 0
        assert row.state is CheckState.OK
        assert row.message == "Brother ADS-2700W is ready."

    def test_no_scanner_support_is_its_own_row(self, tmp_path: Path) -> None:
        """
        A machine without python-sane gets the A-1 row, not a refusal to run.

        Args:
            tmp_path: The test's own directory.

        """
        results = run_checks(_context(_settings(tmp_path), scanner=None))
        row = _row(results, CheckKey.SCANNER)
        assert row.state is CheckState.FAIL
        assert row.message == "Scanner support is not installed on this machine."
        assert (
            row.next_step == "Install saneless with scanner support, then restart it."
        )

    def test_a_skipped_scanner_makes_no_backend_call(self, tmp_path: Path) -> None:
        """
        D-08: while a scan runs the backend is not touched at all.

        Nothing in ``sane_backend.py`` mutually excludes two SANE calls, so
        this is correctness rather than politeness -- a status probe landing on
        the device mid-scan is a second caller into the same C library.

        Args:
            tmp_path: The test's own directory.

        """
        backend = _CountingBackend([_device()])
        results = run_checks(
            _context(_settings(tmp_path), scanner=backend, skip_scanner=True)
        )
        row = _row(results, CheckKey.SCANNER)
        assert backend.calls == 0
        assert row.skipped is True
        assert row.message == "Not checked while a scan is running."

    def test_a_skipped_scanner_is_not_red(self, tmp_path: Path) -> None:
        """
        The skipped row does not turn a scripted health gate red.

        A scan in flight is direct evidence the scanner was working moments
        ago, so "we did not look" is reported as ``OK`` with ``skipped`` set
        rather than as a failure.

        Args:
            tmp_path: The test's own directory.

        """
        backend = _CountingBackend([_device()])
        results = run_checks(
            _context(_settings(tmp_path), scanner=backend, skip_scanner=True)
        )
        scanner_row = _row(results, CheckKey.SCANNER)
        assert scanner_row.state is CheckState.OK
        assert worst_state([scanner_row]) is CheckState.OK

    def test_a_closed_saned_port_is_enumerated(self, tmp_path: Path) -> None:
        """
        A closed saned port is enumerated, and a usable scanner keeps the row amber.

        A refused connect returns at once inside libsane, so listing costs
        nothing and is how the check learns a scanner is still usable.  The
        row names the stopped scanner service once, with one "but".

        Args:
            tmp_path: The test's own directory.

        """
        backend = _CountingBackend([_device()])
        settings = _with_device(
            _settings(tmp_path, host=f"127.0.0.1:{_closed_port()}"), ""
        )
        results = run_checks(_context(settings, scanner=backend))
        row = _row(results, CheckKey.SCANNER)
        assert backend.calls == 1
        assert row.state is CheckState.WARN
        assert row.message == _REFUSED_READY_MESSAGE
        assert _strip(row.next_step) == _REFUSED_NEXT_STEP

    def test_a_healthy_saned_reaches_the_backend(self, tmp_path: Path) -> None:
        """
        A host whose saned answers the handshake then behaves as before.

        Args:
            tmp_path: The test's own directory.

        """
        backend = _CountingBackend([_device()])
        with fake_saned(SanedBehaviour.HEALTHY) as fake:
            settings = _settings(tmp_path, host=f"127.0.0.1:{fake.port}")
            results = run_checks(_context(settings, scanner=backend))
        assert backend.calls == 1
        assert _row(results, CheckKey.SCANNER).state is CheckState.OK

    def test_a_silent_saned_port_never_reaches_the_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listening_port: int
    ) -> None:
        """
        A host that accepts and then says nothing is the timed-out row, unenumerated.

        libsane bounds nothing after its connect, so enumerating this host
        would wait on the INIT reply for as long as the peer keeps quiet.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Shortens both probe budgets.
            listening_port: A loopback port that listens and never answers.

        """
        monkeypatch.setattr(checks, "PROBE_CONNECT_SECONDS", _PROBE_BUDGET)
        monkeypatch.setattr(checks, "PROBE_HANDSHAKE_SECONDS", _PROBE_BUDGET)
        backend = _CountingBackend([_device()])
        settings = _with_device(
            _settings(tmp_path, host=f"127.0.0.1:{listening_port}"), ""
        )
        row = _row(run_checks(_context(settings, scanner=backend)), CheckKey.SCANNER)
        assert backend.calls == 0
        assert row.state is CheckState.WARN
        assert row.message == _TIMED_OUT_MESSAGE
        assert _strip(row.next_step) == _TIMED_OUT_NEXT_STEP

    def test_every_configured_host_is_probed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A healthy first host does not excuse the second from its probe.

        libsane dials every entry, so a dead second host costs its full
        uninterruptible connect inside ``get_devices()`` however well the first
        one answers.  Probing stops at nothing short of the last entry.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.

        """
        dialled = _recording_dialler(monkeypatch)
        settings = _with_device(_settings(tmp_path, host=_TWO_HOSTS), "")
        run_checks(_context(settings, scanner=_CountingBackend([_device()])))
        assert dialled == [("scanbox-a.lan", SANED_PORT), ("scanbox-b.lan", SANED_PORT)]

    def test_a_healthy_host_beside_a_dead_one_never_reaches_the_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        One answering host is not enough to enter SANE when another is silent.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.

        """
        _recording_dialler(
            monkeypatch,
            outcomes={
                "scanbox-a.lan": checks._SanedOutcome.HEALTHY,
                "scanbox-b.lan": checks._SanedOutcome.TIMED_OUT,
            },
        )
        backend = _CountingBackend([_device()])
        settings = _settings(tmp_path, host=_TWO_HOSTS)
        row = _row(run_checks(_context(settings, scanner=backend)), CheckKey.SCANNER)
        assert backend.calls == 0
        assert row.state is CheckState.WARN

    @pytest.mark.parametrize(
        "outcome",
        [
            pytest.param(checks._SanedOutcome.REFUSED, id="refused"),
            pytest.param(checks._SanedOutcome.REJECTED, id="rejected"),
            pytest.param(checks._SanedOutcome.UNRESOLVED, id="unresolved"),
            pytest.param(checks._SanedOutcome.HEALTHY, id="healthy"),
        ],
    )
    def test_hosts_that_cannot_hang_libsane_are_enumerated(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        outcome: checks._SanedOutcome,
    ) -> None:
        """
        Refused, rejected, unresolved and healthy hosts go on to ``get_devices()``.

        A refused connect, a rejection and a failed lookup all return at once
        inside libsane, so the enumeration costs nothing and says whether a
        scanner is usable anyway.  None of them gets the no-enumeration amber
        row.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            outcome: What the one configured host answers.

        """
        _recording_dialler(monkeypatch, outcome=outcome)
        backend = _CountingBackend([_device()])
        settings = _settings(tmp_path, host="scanbox.lan")
        row = _row(run_checks(_context(settings, scanner=backend)), CheckKey.SCANNER)
        assert backend.calls == 1
        assert "could not be checked" not in row.message

    @pytest.mark.parametrize(
        "device",
        [
            pytest.param(_LOCAL_DEVICE_ID, id="usb-device-configured"),
            pytest.param("", id="no-device-configured"),
        ],
    )
    def test_a_usb_deployment_with_no_host_is_never_pre_probed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, device: str
    ) -> None:
        """
        A USB deployment has no TCP to probe, so the backend runs directly.

        No host is configured and the configured device, if any, is not a
        ``net:`` device, so there is no entry to dial and nothing is dialled.
        This is also the fallback that cannot produce a false FAIL: the check
        behaves exactly as it did before the probe existed.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            device: The configured ``scanner.device``.

        """
        dialled = _recording_dialler(monkeypatch)
        usb_device = DeviceInfo(
            name=_LOCAL_DEVICE_ID,
            vendor="Epson",
            model="DS-530",
            device_type="scanner",
        )
        backend = _CountingBackend([usb_device])
        settings = _with_device(_settings(tmp_path, host=""), device)
        results = run_checks(_context(settings, scanner=backend))
        assert dialled == []
        assert backend.calls == 1
        assert backend.opens == 0
        assert _row(results, CheckKey.SCANNER).state is CheckState.OK

    def test_the_configured_host_is_dialled_when_the_environment_is_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        With no ``SANE_NET_HOSTS``, ``scanner.host`` is what SANE will use.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.

        """
        dialled = _recording_dialler(monkeypatch)
        settings = _with_device(_settings(tmp_path, host="config-host"), "")
        run_checks(_context(settings, scanner=_CountingBackend([_device()])))
        assert dialled == [("config-host", SANED_PORT)]

    def test_the_environment_variable_wins_over_the_configured_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The probe dials the host SANE dials, not the one the file names.

        ``_ensure_initialised`` (``sane_backend.py:876-883``) honours a
        pre-existing ``SANE_NET_HOSTS`` and logs that it is ignoring
        ``scanner.host``.  A probe reading only the setting can therefore
        describe a host that is not in play at all: a down config host
        producing a row while the live environment host serves devices.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.

        """
        monkeypatch.setenv("SANE_NET_HOSTS", "env-host")
        dialled = _recording_dialler(monkeypatch)
        settings = _with_device(_settings(tmp_path, host="config-host"), "")
        run_checks(_context(settings, scanner=_CountingBackend([_device()])))
        assert dialled == [("env-host", SANED_PORT)]

    def test_an_empty_environment_variable_leaves_the_setting_in_charge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        An exported-but-empty variable is not a configured host.

        The scanner backend and this check read the variable through one
        helper, and that helper counts an exported empty value as unset: it
        names no host, so the backend writes the configured host into it and
        SANE dials that.  The probe therefore dials the configured host too.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.

        """
        monkeypatch.setenv("SANE_NET_HOSTS", "")
        dialled = _recording_dialler(monkeypatch)
        settings = _with_device(_settings(tmp_path, host="cfg-host"), "")
        run_checks(_context(settings, scanner=_CountingBackend([_device()])))
        assert dialled == [("cfg-host", SANED_PORT)]

    @pytest.mark.parametrize(
        "environment",
        [
            pytest.param(None, id="unset"),
            pytest.param("", id="exported-empty"),
            pytest.param("ext-host", id="exported"),
        ],
    )
    def test_the_probe_and_sane_agree_on_the_host_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: str | None
    ) -> None:
        """
        The probe's host list is the one the scanner backend hands SANE.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            environment: What ``SANE_NET_HOSTS`` holds, or None for unset.

        """
        if environment is not None:
            monkeypatch.setenv("SANE_NET_HOSTS", environment)
        settings = _settings(tmp_path, host="cfg-host")
        assert checks._saned_host_setting(settings) == effective_sane_net_hosts(
            "cfg-host"
        )

    @pytest.mark.parametrize(
        ("host", "outcomes", "expected"),
        [
            pytest.param(
                "scanbox.lan",
                {"scanbox.lan": checks._SanedOutcome.TIMED_OUT},
                (_TIMED_OUT_MESSAGE, _TIMED_OUT_NEXT_STEP),
                id="one-timed-out",
            ),
            pytest.param(
                _TWO_HOSTS,
                {
                    "scanbox-a.lan": checks._SanedOutcome.HEALTHY,
                    "scanbox-b.lan": checks._SanedOutcome.TIMED_OUT,
                },
                (
                    "1 of 2 scanner hosts is not answering, "
                    "so the scanner could not be checked.",
                    _TIMED_OUT_NEXT_STEP,
                ),
                id="healthy-and-timed-out",
            ),
            pytest.param(
                _TWO_HOSTS,
                {
                    "scanbox-a.lan": checks._SanedOutcome.REFUSED,
                    "scanbox-b.lan": checks._SanedOutcome.TIMED_OUT,
                },
                (
                    "1 of 2 scanner hosts is not answering, "
                    "so the scanner could not be checked.",
                    _TIMED_OUT_NEXT_STEP,
                ),
                id="refused-and-timed-out",
            ),
        ],
    )
    def test_an_unanswered_host_is_amber_and_says_what_to_do(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        host: str,
        outcomes: dict[str, checks._SanedOutcome],
        expected: tuple[str, str],
    ) -> None:
        """
        CR-02: a dead host is a fact about the host, not about the appliance.

        ``SANE_NET_HOSTS`` *adds* net devices; it does not replace local
        backend enumeration, so "a configured host did not answer" never
        implied "there is no scanner".  A true statement about a deployment
        that may still work is amber.  The row names the worst outcome among
        the hosts, with a count once there is more than one, and each outcome
        carries its own next step.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            host: The configured ``scanner.host``.
            outcomes: What each configured host answers.
            expected: The row's message and next step.

        """
        _recording_dialler(monkeypatch, outcomes=outcomes)
        backend = _CountingBackend([_device()])
        settings = _with_device(_healthy_settings(tmp_path, host=host), "")
        row = _row(run_checks(_context(settings, scanner=backend)), CheckKey.SCANNER)
        assert backend.calls == 0
        assert row.state is CheckState.WARN
        assert row.skipped is False
        assert (row.message, _strip(row.next_step)) == expected

    @pytest.mark.parametrize(
        ("host", "outcomes", "hosts_subject"),
        [
            pytest.param(
                "scanbox.lan",
                {"scanbox.lan": checks._SanedOutcome.REFUSED},
                "the scanner host",
                id="one-refused",
            ),
            pytest.param(
                _TWO_HOSTS,
                {
                    "scanbox-a.lan": checks._SanedOutcome.HEALTHY,
                    "scanbox-b.lan": checks._SanedOutcome.REFUSED,
                },
                "1 of 2 scanner hosts",
                id="healthy-and-refused",
            ),
            pytest.param(
                _TWO_HOSTS,
                {
                    "scanbox-a.lan": checks._SanedOutcome.REFUSED,
                    "scanbox-b.lan": checks._SanedOutcome.REFUSED,
                },
                "2 of 2 scanner hosts",
                id="both-refused",
            ),
            pytest.param(
                _TWO_HOSTS,
                {
                    "scanbox-a.lan": checks._SanedOutcome.REJECTED,
                    "scanbox-b.lan": checks._SanedOutcome.REFUSED,
                },
                "1 of 2 scanner hosts",
                id="rejected-and-refused",
            ),
        ],
    )
    def test_a_refusing_host_beside_a_usable_scanner_is_enumerated_and_amber(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        host: str,
        outcomes: dict[str, checks._SanedOutcome],
        hosts_subject: str,
    ) -> None:
        """
        A stopped scanner service is enumerated, and a working scanner keeps it amber.

        A refused connect returns at once inside libsane, so the listing runs
        and finds the usable scanner.  The row reports the worst outcome among
        the hosts, with a count once there is more than one, in a sentence
        with a single "but".

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            host: The configured ``scanner.host``.
            outcomes: What each configured host answers.
            hosts_subject: How the row counts the refusing hosts.

        """
        _recording_dialler(monkeypatch, outcomes=outcomes)
        backend = _CountingBackend([_device()])
        settings = _with_device(_healthy_settings(tmp_path, host=host), "")
        row = _row(run_checks(_context(settings, scanner=backend)), CheckKey.SCANNER)
        assert backend.calls == 1
        assert row.state is CheckState.WARN
        assert row.message == (
            "Brother ADS-2700W is ready, but the scanner service is not running "
            f"on {hosts_subject}."
        )
        assert _strip(row.next_step) == _REFUSED_NEXT_STEP

    def test_the_timed_out_and_refused_rows_advise_different_things(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A host that is off and a host whose saned is stopped need different fixes.

        Both still end in pressing Check again: each state was measured to
        clear on the next Check, with no restart.  The silent host is never
        enumerated, so its row is amber; the refused one is, and with nothing
        listed its row is red.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.

        """
        rows = []
        for outcome in (checks._SanedOutcome.TIMED_OUT, checks._SanedOutcome.REFUSED):
            _recording_dialler(monkeypatch, outcome=outcome)
            settings = _with_device(_settings(tmp_path, host="scanbox.lan"), "")
            rows.append(
                _row(
                    run_checks(_context(settings, scanner=_CountingBackend())),
                    CheckKey.SCANNER,
                )
            )
        timed_out, refused = rows
        assert timed_out.state is CheckState.WARN
        assert refused.state is CheckState.FAIL
        assert refused.message == _REFUSED_MESSAGE
        assert _strip(timed_out.next_step) != _strip(refused.next_step)
        assert _strip(timed_out.next_step).endswith("press Check again.")
        assert _strip(refused.next_step).endswith("press Check again.")

    def test_an_unanswered_host_does_not_turn_a_health_gate_red(
        self, tmp_path: Path
    ) -> None:
        """
        A machine with a working local scanner and a stale host exits 0.

        ``CheckState``'s own rule is that a healthy appliance must never go
        red.  ``saneless doctor`` exits non-zero on ``FAIL`` only, so no row
        being ``FAIL`` is the exit code.

        Args:
            tmp_path: The test's own directory.

        """
        backend = _CountingBackend([_device()])
        settings = _healthy_settings(tmp_path, host=f"127.0.0.1:{_closed_port()}")
        results = run_checks(
            _context(
                settings,
                scanner=backend,
                paperless=_paperless(_RequestCounter(_ok_response)),
            )
        )
        assert [row.key for row in results if row.state is CheckState.FAIL] == []
        assert worst_state(results) is CheckState.WARN

    def test_a_unicode_digit_port_does_not_redden_the_whole_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        R3-CR-01 end to end: a U+00B2 in the port leaves the run green.

        The probe half of this is stubbed -- ``_recording_dialler`` replaces
        ``checks._probe_saned``, so the test resolves no name and
        opens no socket.  What is measured is the row the *parser* produces:
        before the fix the ``ValueError`` escaped into ``run_checks``' generic
        per-check handler, so the Scanner row carried
        ``_CHECK_FAILED_MESSAGE`` with state FAIL, ``worst_state`` was FAIL and
        ``saneless doctor`` exited 2 on an appliance that scans fine.  The
        setting is put in the environment rather than the config file because
        ``_saned_host_setting`` prefers ``SANE_NET_HOSTS``, which is where a
        container operator would hit this.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Sets the environment and stubs the dialler.

        """
        monkeypatch.setenv("SANE_NET_HOSTS", "scanbox:²")
        _recording_dialler(monkeypatch)
        results = run_checks(
            _context(
                _healthy_settings(tmp_path, host=""),
                scanner=_CountingBackend([_device()]),
                paperless=_paperless(_RequestCounter(_ok_response)),
            )
        )
        assert _row(results, CheckKey.SCANNER).message != checks._CHECK_FAILED_MESSAGE
        assert worst_state(results) is not CheckState.FAIL

    @pytest.mark.parametrize(
        "outcome",
        [
            pytest.param(checks._SanedOutcome.TIMED_OUT, id="timed-out"),
            pytest.param(checks._SanedOutcome.REFUSED, id="refused"),
        ],
    )
    @pytest.mark.parametrize(
        "hosts",
        [
            pytest.param((None, "scanbox.lan"), id="setting-only"),
            pytest.param(("scanbox.lan:6566", ""), id="environment-only"),
            pytest.param(("192.0.2.10", "scanbox.lan"), id="both"),
            pytest.param((None, _TWO_HOSTS), id="two-hosts"),
        ],
    )
    def test_the_unanswered_row_names_no_host_address_or_port(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        hosts: tuple[str | None, str],
        outcome: checks._SanedOutcome,
    ) -> None:
        """
        ASVS V7: a LAN address never reaches a LAN-visible page through this row.

        Same class of fact, and the same refusal, as ``_device_label``
        declining to print a ``net:<host>`` identifier.  It also never falls
        back on the red row's "switched on and connected", which was the
        wrong advice for a host whose saned refused the connection.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            hosts: What ``SANE_NET_HOSTS`` is set to (None for unset), and
                what ``scanner.host`` is configured as.
            outcome: What every configured host answers.

        """
        environment, setting = hosts
        if environment is not None:
            monkeypatch.setenv("SANE_NET_HOSTS", environment)
        _recording_dialler(monkeypatch, outcome=outcome)
        settings = _settings(tmp_path, host=setting)
        row = _row(
            run_checks(_context(settings, scanner=_CountingBackend([_device()]))),
            CheckKey.SCANNER,
        )
        assert row.state is CheckState.WARN
        for text in (row.message, row.next_step):
            assert ":" not in text
            assert "/" not in text
            assert "scanbox" not in text
            assert "192.0.2.10" not in text
            assert "6566" not in text
            assert "switched on and connected" not in text

    @pytest.mark.parametrize("surface", _SURFACES)
    def test_an_enumeration_that_found_nothing_is_still_red(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
    ) -> None:
        """
        The amber row is only for the case where the probe replaced the enumeration.

        A probe that answered, followed by a backend reporting no devices, is
        an enumeration that actually ran, so its verdict stands -- and it says
        the host answered, because that rules the network out.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            surface: Which of the two surfaces runs the check.

        """
        _recording_dialler(monkeypatch)
        backend = _CountingBackend()
        settings = _with_device(_settings(tmp_path, host="scanbox.lan"), "")
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert backend.calls == 1
        assert row.state is CheckState.FAIL
        assert row.message == (
            "The scanner host is answering, but no scanner was found on it."
        )
        assert _strip(row.next_step) == _HOST_ANSWERS_NOTHING_FOUND_NEXT
        assert "Not reachable" not in row.message

    def test_a_reachable_host_whose_backend_raises_is_still_red(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        An enumeration that threw is still a red row, not the amber one.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.

        """
        _recording_dialler(monkeypatch)
        settings = _with_device(_settings(tmp_path, host="scanbox.lan"), "")
        row = _row(
            run_checks(_context(settings, scanner=_RaisingBackend())), CheckKey.SCANNER
        )
        assert row.state is CheckState.FAIL
        assert row.message == (
            "The scanner host is answering, but no scanner was found on it."
        )
        assert "Not reachable" not in row.message

    def test_an_unparseable_host_still_reaches_the_backend(
        self, tmp_path: Path
    ) -> None:
        """
        WR-01 and CR-02 together: an IPv6 literal produces no probe and no row.

        Before the refusal, ``fe80::1`` dialled host ``fe80`` on port 1, every
        dial failed, and the short circuit turned that into a verdict.

        Args:
            tmp_path: The test's own directory.

        """
        backend = _CountingBackend([_device()])
        settings = _settings(tmp_path, host="fe80::1")
        row = _row(run_checks(_context(settings, scanner=backend)), CheckKey.SCANNER)
        assert backend.calls == 1
        assert row.state is CheckState.OK


def _without_url(settings: Settings) -> Settings:
    """
    Return ``settings`` with ``paperless.url`` left unset.

    Args:
        settings: Settings from ``_settings``.

    Returns:
        A copy whose paperless-ngx address is empty, everything else kept.

    """
    paperless = settings.paperless.model_copy(update={"url": ""})
    return settings.model_copy(update={"paperless": paperless})


def _with_device(settings: Settings, device: str) -> Settings:
    """
    Copy settings with ``scanner.device`` set to ``device``.

    Args:
        settings: Settings from ``_settings``, which pins a device.
        device: The device id to configure, ``""`` for auto-detection.

    Returns:
        A copy differing only in ``scanner.device``.

    """
    scanner = settings.scanner.model_copy(update={"device": device})
    return settings.model_copy(update={"scanner": scanner})


def _rogue_device() -> DeviceInfo:
    """
    Build a second device whose SANE id names a different LAN host.

    Returns:
        A DeviceInfo distinct from ``_device()`` in id, vendor and model.

    """
    return DeviceInfo(
        name="net:rogue.lan:escl:bus1;dev2",
        vendor="Canon",
        model="MF740C",
        device_type="scanner",
    )


class TestScannerCheckMultipleDevices:
    """
    Several visible scanners and none chosen is a WARN, never a FAIL.

    With ``scanner.device`` empty every scan goes to the first device SANE
    lists, and a newly visible scanner on the LAN can take that place.  The
    row says so by count only: device ids are LAN addresses and the strip is
    LAN-visible.
    """

    def test_multiple_devices_without_a_pin_warn(self, tmp_path: Path) -> None:
        """
        Two devices and an empty ``scanner.device`` give a count-only WARN.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_device(_settings(tmp_path), "")
        backend = _CountingBackend([_device(), _rogue_device()])
        row = _row(run_checks(_context(settings, scanner=backend)), CheckKey.SCANNER)
        assert row.state is CheckState.WARN
        assert row.message == "2 scanners are visible and none is chosen."
        assert "[scanner] device" in row.next_step
        rendered = f"{row.message} {row.next_step}"
        for leaked in ("net:", "scanbox.lan", "rogue.lan", "Brother", "Canon"):
            assert leaked not in rendered

    def test_multiple_devices_with_a_pin_stay_ok(self, tmp_path: Path) -> None:
        """
        A configured device settles the choice, however many are visible.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_device(_settings(tmp_path), _DEVICE_ID)
        backend = _CountingBackend([_rogue_device(), _device()])
        row = _row(run_checks(_context(settings, scanner=backend)), CheckKey.SCANNER)
        assert row.state is CheckState.OK
        assert row.message == "Brother ADS-2700W is ready."
        assert backend.opens == 0

    def test_one_device_without_a_pin_multiple_devices_absent_stays_ok(
        self, tmp_path: Path
    ) -> None:
        """
        One visible device leaves nothing ambiguous to warn about.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_device(_settings(tmp_path), "")
        backend = _CountingBackend([_device()])
        row = _row(run_checks(_context(settings, scanner=backend)), CheckKey.SCANNER)
        assert row.state is CheckState.OK
        assert row.message == "Brother ADS-2700W is ready."


_HEALTHY: Final = checks._SanedOutcome.HEALTHY
_REJECTED: Final = checks._SanedOutcome.REJECTED
_UNRESOLVED: Final = checks._SanedOutcome.UNRESOLVED
_REFUSED: Final = checks._SanedOutcome.REFUSED
_TIMED_OUT: Final = checks._SanedOutcome.TIMED_OUT

_REJECTED_NEXT: Final = (
    "Add this machine to saned.conf on the scanner host, then press Check again."
)
_UNRESOLVED_NEXT: Final = (
    "Check the host name in [scanner] host or [scanner] device, or in "
    "SANE_NET_HOSTS if that is set. If you fixed the name in DNS or the hosts "
    "file, press Check again; if you changed a setting, restart saneless."
)
_TIMED_OUT_NEXT: Final = (
    "Check the scanner host is switched on and on the network, then press Check again."
)
_REFUSED_NEXT: Final = "Start saned on the scanner host, or check it is listening on the network, then press Check again."
_MULTIPLE_DEVICES_NEXT: Final = "Set [scanner] device to the one you use; saneless devices lists them, and saneless auto-profiles writes it for you."
_CONFIGURED_MISSING_NEXT: Final = "Check it is switched on and connected, then press Check again. If saneless devices does not list it, set [scanner] device to one it lists, then restart saneless."
_HOST_ANSWERS_NOTHING_FOUND_NEXT: Final = "Check the scanner is switched on and connected to the scanner host, then press Check again."
_NOTHING_FOUND_NEXT: Final = (
    "Check the scanner is switched on and connected, then press Check again."
)

# The three rows for a listing that could not see: the scanner library crashed
# while listing, did not finish before the deadline, or gave no usable answer.
_LISTING_CRASHED_MESSAGE: Final = "The scanner library failed while listing scanners, so the scanner could not be checked."
_LISTING_CRASHED_NEXT: Final = "Press Check again."
_LISTING_TIMED_OUT_MESSAGE: Final = "The scanner library did not finish listing scanners in time, so the scanner could not be checked."
_LISTING_TIMED_OUT_NEXT: Final = "Check the scanner, and its scanner host if it has one, are switched on and reachable, then press Check again."
_LISTING_NO_ANSWER_MESSAGE: Final = "The scanner library gave no usable answer while listing scanners, so the scanner could not be checked."
_LISTING_NO_ANSWER_NEXT: Final = "Press Check again."

# The texts the listing launcher gives its three failures, which the rows
# above must never repeat.
_CRASH_TEXT: Final = "The scanner library failed while listing scanners (SIGSEGV)"
_TIMEOUT_TEXT: Final = (
    "The scanner library did not finish listing scanners in time (30 s)"
)
_NO_ANSWER_TEXT: Final = "The scanner library returned no answer while listing scanners"

# The longest message the Scanner row could carry before these two were added,
# which is what the status strip's layout is already known to hold.
_LONGEST_SCANNER_MESSAGE: Final = _UNPROBED_DEVICE_MESSAGE

_CRASHED: Final = checks._ListingFailure.CRASHED
_LISTING_TIMED_OUT: Final = checks._ListingFailure.TIMED_OUT
_LISTING_NO_ANSWER: Final = checks._ListingFailure.NO_ANSWER

_UNPROBED_NET_ID: Final = "net:[fe80::1]:brother5:bus0;dev1"

_UNLISTED_ESCL_ID: Final = "escl:http://10.0.0.5:80"


def _hp(
    host: str, outcome: checks._SanedOutcome, port: int = SANED_PORT
) -> checks._HostProbe:
    """
    Build what one configured host's probe found.

    Args:
        host: The configured entry.
        outcome: What its probe found.
        port: The port it was probed on.

    Returns:
        The probe result the verdict reads.

    """
    return checks._HostProbe(host, outcome, port)


def _unlabelled_device() -> DeviceInfo:
    """
    Build a device that reported neither a vendor nor a model.

    Returns:
        A DeviceInfo whose label is empty.

    """
    return DeviceInfo(
        name="net:scanbox.lan:plain:bus0;dev3",
        vendor="",
        model="",
        device_type="scanner",
    )


@dataclass(frozen=True, slots=True)
class _VerdictCase:
    """
    One set of inputs to the Scanner verdict and the row it must produce.

    Attributes:
        probes: What each configured host's probe found.
        devices: What enumeration listed.
        configured: The configured ``scanner.device``, possibly empty.
        opened: Whether opening an unlisted configured device worked, or
            ``None`` when no open was attempted.
        state: The row's expected state.
        message: The row's expected message.
        next_step: The row's expected next step.
        withheld: Whether the configured device was deliberately not opened.
        failure: How the listing failed, or ``None`` when it completed.

    """

    probes: tuple[checks._HostProbe, ...]
    devices: tuple[DeviceInfo, ...]
    configured: str
    opened: bool | None
    state: CheckState
    message: str
    next_step: str
    withheld: bool = False
    failure: checks._ListingFailure | None = None


_VERDICT_CASES: Final = [
    pytest.param(
        _VerdictCase(
            probes=(),
            devices=(_device(),),
            configured="",
            opened=None,
            state=CheckState.OK,
            message="Brother ADS-2700W is ready.",
            next_step="",
        ),
        id="one-device-ready",
    ),
    pytest.param(
        _VerdictCase(
            probes=(),
            devices=(_unlabelled_device(),),
            configured="",
            opened=None,
            state=CheckState.OK,
            message="Ready.",
            next_step="",
        ),
        id="unlabelled-device-ready",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.lan", _HEALTHY),),
            devices=(_rogue_device(), _device()),
            configured=_device().name,
            opened=None,
            state=CheckState.OK,
            message="Brother ADS-2700W is ready.",
            next_step="",
        ),
        id="configured-device-not-the-first-listed",
    ),
    pytest.param(
        _VerdictCase(
            probes=(),
            devices=(_device(), _rogue_device()),
            configured="",
            opened=None,
            state=CheckState.WARN,
            message="2 scanners are visible and none is chosen.",
            next_step=_MULTIPLE_DEVICES_NEXT,
        ),
        id="several-devices-none-chosen",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.lan", _REJECTED),),
            devices=(_device(),),
            configured="",
            opened=None,
            state=CheckState.WARN,
            message="Brother ADS-2700W is ready, but the scanner host is refusing this machine.",
            next_step=_REJECTED_NEXT,
        ),
        id="rejected-with-a-usable-scanner",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.lan", _REJECTED),),
            devices=(_unlabelled_device(),),
            configured="",
            opened=None,
            state=CheckState.WARN,
            message="The scanner is ready, but the scanner host is refusing this machine.",
            next_step=_REJECTED_NEXT,
        ),
        id="rejected-with-an-unlabelled-usable-scanner",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.lan", _HEALTHY), _hp("b.lan", _REJECTED)),
            devices=(_device(),),
            configured=_device().name,
            opened=None,
            state=CheckState.WARN,
            message="Brother ADS-2700W is ready, but 1 of 2 scanner hosts is refusing this machine.",
            next_step=_REJECTED_NEXT,
        ),
        id="configured-device-ready-beside-a-rejecting-host",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.lan", _REJECTED),),
            devices=(),
            configured="",
            opened=None,
            state=CheckState.FAIL,
            message="The scanner host is refusing this machine.",
            next_step=_REJECTED_NEXT,
        ),
        id="rejected-and-nothing-usable",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("a.lan", _HEALTHY), _hp("b.lan", _REJECTED)),
            devices=(),
            configured="",
            opened=None,
            state=CheckState.FAIL,
            message="1 of 2 scanner hosts is refusing this machine.",
            next_step=_REJECTED_NEXT,
        ),
        id="one-of-two-hosts-rejected",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("a.lan", _REJECTED), _hp("b.lan", _REJECTED)),
            devices=(),
            configured="",
            opened=None,
            state=CheckState.FAIL,
            message="2 of 2 scanner hosts are refusing this machine.",
            next_step=_REJECTED_NEXT,
        ),
        id="two-of-two-hosts-rejected",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.test", _UNRESOLVED),),
            devices=(),
            configured="",
            opened=None,
            state=CheckState.FAIL,
            message="The scanner host could not be found by name.",
            next_step=_UNRESOLVED_NEXT,
        ),
        id="unresolved-and-nothing-usable",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("a.lan", _REJECTED), _hp("b.lan", _UNRESOLVED)),
            devices=(),
            configured="",
            opened=None,
            state=CheckState.FAIL,
            message="1 of 2 scanner hosts could not be found by name.",
            next_step=_UNRESOLVED_NEXT,
        ),
        id="the-worst-outcome-is-reported",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.test", _UNRESOLVED),),
            devices=(_device(),),
            configured="",
            opened=None,
            state=CheckState.WARN,
            message="Brother ADS-2700W is ready, but the scanner host could not be found by name.",
            next_step=_UNRESOLVED_NEXT,
        ),
        id="unresolved-with-a-usable-scanner",
    ),
    pytest.param(
        _VerdictCase(
            probes=(),
            devices=(_rogue_device(),),
            configured=_LOCAL_DEVICE_ID,
            opened=False,
            state=CheckState.FAIL,
            message="The configured scanner was not found.",
            next_step=_CONFIGURED_MISSING_NEXT,
        ),
        id="configured-device-absent-and-will-not-open",
    ),
    pytest.param(
        _VerdictCase(
            probes=(),
            devices=(_rogue_device(),),
            configured=_UNLISTED_ESCL_ID,
            opened=True,
            state=CheckState.OK,
            message="The configured scanner is ready.",
            next_step="",
        ),
        id="configured-device-unlisted-but-opens",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.lan", _REJECTED),),
            devices=(),
            configured="net:other.lan:x:0",
            opened=True,
            state=CheckState.WARN,
            message="The configured scanner is ready, but the scanner host is refusing this machine.",
            next_step=_REJECTED_NEXT,
        ),
        id="configured-device-opens-beside-a-rejecting-host",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("a.lan", _HEALTHY), _hp("scanbox.lan", _REJECTED)),
            devices=(),
            configured=_device().name,
            opened=False,
            state=CheckState.FAIL,
            message="The configured scanner's host is refusing this machine.",
            next_step=_REJECTED_NEXT,
        ),
        id="configured-net-device-on-a-rejecting-host",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("a.lan", _HEALTHY), _hp("scanbox.lan", _UNRESOLVED)),
            devices=(),
            configured=_device().name,
            opened=False,
            state=CheckState.FAIL,
            message="The configured scanner's host could not be found by name.",
            next_step=_UNRESOLVED_NEXT,
        ),
        id="configured-net-device-on-an-unresolved-host",
    ),
    pytest.param(
        _VerdictCase(
            probes=(
                _hp("scanbox.lan", _REJECTED, port=7000),
                _hp("scanbox.lan", _HEALTHY),
            ),
            devices=(),
            configured=_device().name,
            opened=False,
            state=CheckState.FAIL,
            message="The configured scanner was not found.",
            next_step=_CONFIGURED_MISSING_NEXT,
        ),
        id="configured-net-device-whose-host-refuses-only-on-another-port",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.lan", _REJECTED),),
            devices=(),
            configured=_LOCAL_DEVICE_ID,
            opened=False,
            state=CheckState.FAIL,
            message="The configured scanner was not found.",
            next_step=_CONFIGURED_MISSING_NEXT,
        ),
        id="configured-local-device-absent-beside-a-rejecting-host",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.lan", _HEALTHY),),
            devices=(),
            configured="",
            opened=None,
            state=CheckState.FAIL,
            message="The scanner host is answering, but no scanner was found on it.",
            next_step=_HOST_ANSWERS_NOTHING_FOUND_NEXT,
        ),
        id="healthy-host-nothing-found",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("a.lan", _HEALTHY), _hp("b.lan", _HEALTHY)),
            devices=(),
            configured="",
            opened=None,
            state=CheckState.FAIL,
            message="The scanner hosts are answering, but no scanner was found on them.",
            next_step=_HOST_ANSWERS_NOTHING_FOUND_NEXT,
        ),
        id="healthy-hosts-nothing-found",
    ),
    pytest.param(
        _VerdictCase(
            probes=(),
            devices=(),
            configured="",
            opened=None,
            state=CheckState.FAIL,
            message="No scanner was found.",
            next_step=_NOTHING_FOUND_NEXT,
        ),
        id="no-host-nothing-found",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.lan", _REJECTED),),
            devices=(_device(), _rogue_device()),
            configured="",
            opened=None,
            state=CheckState.WARN,
            message="Brother ADS-2700W is ready, but the scanner host is refusing this machine.",
            next_step=_REJECTED_NEXT,
        ),
        id="a-host-problem-beats-the-several-devices-warning",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.lan", _TIMED_OUT),),
            devices=(_device(),),
            configured="",
            opened=None,
            state=CheckState.WARN,
            message="The scanner host is not answering, so the scanner could not be checked.",
            next_step=_TIMED_OUT_NEXT,
        ),
        id="timed-out",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("a.lan", _REFUSED), _hp("b.lan", _HEALTHY)),
            devices=(),
            configured="",
            opened=None,
            state=CheckState.FAIL,
            message="1 of 2 scanner hosts is on, but its scanner service is not running.",
            next_step=_REFUSED_NEXT,
        ),
        id="refused",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.lan", _REFUSED),),
            devices=(),
            configured="",
            opened=None,
            state=CheckState.FAIL,
            message="The scanner host is on, but its scanner service is not running.",
            next_step=_REFUSED_NEXT,
        ),
        id="refused-nothing-usable",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.lan", _REFUSED),),
            devices=(_device(),),
            configured="",
            opened=None,
            state=CheckState.WARN,
            message="Brother ADS-2700W is ready, but the scanner service is not running on the scanner host.",
            next_step=_REFUSED_NEXT,
        ),
        id="refused-with-local-scanner",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("a.lan", _HEALTHY), _hp("scanbox.lan", _REFUSED)),
            devices=(_device(),),
            configured="",
            opened=None,
            state=CheckState.WARN,
            message="Brother ADS-2700W is ready, but the scanner service is not running on 1 of 2 scanner hosts.",
            next_step=_REFUSED_NEXT,
        ),
        id="refused-one-of-two-with-local-scanner",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.lan", _REFUSED),),
            devices=(),
            configured=_device().name,
            opened=False,
            state=CheckState.FAIL,
            message="The configured scanner's host is on, but its scanner service is not running.",
            next_step=_REFUSED_NEXT,
        ),
        id="configured-net-device-own-host-refused",
    ),
    pytest.param(
        _VerdictCase(
            probes=(),
            devices=(_rogue_device(),),
            configured=_UNPROBED_NET_ID,
            opened=None,
            state=CheckState.WARN,
            message=_UNPROBED_DEVICE_MESSAGE,
            next_step=_UNPROBED_DEVICE_NEXT,
            withheld=True,
        ),
        id="configured-net-device-unprobed",
    ),
    pytest.param(
        _VerdictCase(
            probes=(),
            devices=(),
            configured="",
            opened=None,
            state=CheckState.WARN,
            message=_LISTING_CRASHED_MESSAGE,
            next_step=_LISTING_CRASHED_NEXT,
            failure=_CRASHED,
        ),
        id="crash",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.lan", _REJECTED),),
            devices=(),
            configured="",
            opened=None,
            state=CheckState.WARN,
            message=_LISTING_CRASHED_MESSAGE,
            next_step=_LISTING_CRASHED_NEXT,
            failure=_CRASHED,
        ),
        id="crash-beside-rejected",
    ),
    pytest.param(
        _VerdictCase(
            probes=(),
            devices=(),
            configured="",
            opened=None,
            state=CheckState.WARN,
            message=_LISTING_TIMED_OUT_MESSAGE,
            next_step=_LISTING_TIMED_OUT_NEXT,
            failure=_LISTING_TIMED_OUT,
        ),
        id="timeout",
    ),
    pytest.param(
        _VerdictCase(
            probes=(_hp("scanbox.lan", _HEALTHY),),
            devices=(),
            configured=_device().name,
            opened=None,
            state=CheckState.WARN,
            message=_LISTING_TIMED_OUT_MESSAGE,
            next_step=_LISTING_TIMED_OUT_NEXT,
            failure=_LISTING_TIMED_OUT,
        ),
        id="timeout-with-a-configured-device",
    ),
    pytest.param(
        _VerdictCase(
            probes=(),
            devices=(),
            configured="",
            opened=None,
            state=CheckState.WARN,
            message=_LISTING_NO_ANSWER_MESSAGE,
            next_step=_LISTING_NO_ANSWER_NEXT,
            failure=_LISTING_NO_ANSWER,
        ),
        id="no-answer",
    ),
    pytest.param(
        _VerdictCase(
            probes=(),
            devices=(),
            configured=_LOCAL_DEVICE_ID,
            opened=False,
            state=CheckState.WARN,
            message=_LISTING_NO_ANSWER_MESSAGE,
            next_step=_LISTING_NO_ANSWER_NEXT,
            failure=_LISTING_NO_ANSWER,
        ),
        id="no-answer-with-a-configured-device",
    ),
]


def _verdict(case: _VerdictCase) -> CheckResult:
    """
    Run the Scanner verdict on one case's inputs.

    Args:
        case: The inputs, and the expected row, which is not used here.

    Returns:
        The row the verdict built.

    """
    return checks._scanner_verdict(
        case.probes,
        checks._Enumeration(
            devices=case.devices,
            configured_opened=case.opened,
            open_withheld=case.withheld,
            failure=case.failure,
        ),
        case.configured,
    )


def _advice_kind(message: str) -> str:
    """
    Say which kind of next step a row's message calls for.

    Args:
        message: A Scanner row's message.

    Returns:
        ``"restart"`` for the unprobed configured device, whose only way out
        is a saneless configuration edit; ``"check-again-or-restart-after-edit"``
        for a name that did not resolve and a configured scanner that was not
        found, which the next Check clears unless the fix was a changed
        setting; ``"check-again"`` for every state a fresh listing picks up
        again; and ``"none"`` for everything else.

    """
    if "cannot be checked in advance" in message:
        return "restart"
    if "could not be found by name" in message or "was not found" in message:
        return "check-again-or-restart-after-edit"
    if any(
        phrase in message.lower()
        for phrase in (
            "refusing this machine",
            "not answering",
            "scanner service is not running",
            "failed while listing scanners",
            "did not finish listing scanners",
            "no usable answer while listing scanners",
            "no scanner was found",
        )
    ):
        return "check-again"
    return "none"


_LEAK_MARKERS: Final = (
    "scanbox",
    "a.lan",
    "b.lan",
    "other.lan",
    "rogue.lan",
    "10.0.0.5",
    "epson2",
    "libusb",
    "net:",
    "escl:",
    "/",
    "6566",
)


class TestScannerVerdict:
    """
    One pure function turns probe outcomes and a device list into the Scanner row.

    Every row the check can show is decided here from plain values, so each
    one is pinned without a socket or a backend.  The rows are amber while a
    usable scanner is visible and red only when scanning cannot work; a host
    that refuses this machine is named as such and never blamed on the
    scanner being switched off; and the next step says "press Check again"
    only where the next check really can clear the state.
    """

    @pytest.mark.parametrize("case", _VERDICT_CASES)
    def test_the_row(self, case: _VerdictCase) -> None:
        """
        Each set of inputs gives exactly the expected state, message and next step.

        Args:
            case: The inputs and the expected row.

        """
        row = _verdict(case)
        assert row.key is CheckKey.SCANNER
        assert (row.state, row.message, _strip(row.next_step)) == (
            case.state,
            case.message,
            case.next_step,
        )

    @pytest.mark.parametrize("case", _VERDICT_CASES)
    def test_a_refusing_host_is_never_blamed_on_the_scanner(
        self, case: _VerdictCase
    ) -> None:
        """
        A row about a refusing host points at saned.conf, not at the scanner's power.

        Args:
            case: The inputs to the verdict.

        """
        row = _verdict(case)
        if "refusing this machine" not in row.message:
            return
        assert "switched on and connected" not in row.message
        assert "switched on and connected" not in row.next_step
        assert "saned.conf" in row.next_step

    @pytest.mark.parametrize("case", _VERDICT_CASES)
    def test_the_next_step_is_the_one_that_clears_the_state(
        self, case: _VerdictCase
    ) -> None:
        """
        Only a saneless configuration edit calls for a restart.

        Each listing runs in a fresh process with a fresh scanner library, so
        a host that starts answering, a name that starts resolving and a host
        that gains a scanner are all picked up by the next Check.  Settings
        are read once, at start, so a row whose fix may be a changed setting
        says to restart only after that change, and the one row whose only
        fix is a setting says restart outright.

        Args:
            case: The inputs to the verdict.

        """
        row = _verdict(case)
        step = _strip(row.next_step)
        match _advice_kind(row.message):
            case "check-again":
                assert step.endswith("ress Check again.")
                assert "restart" not in step
            case "check-again-or-restart-after-edit":
                assert "press Check again" in step
                assert step.endswith("restart saneless.")
                assert (
                    "if you changed a setting" in step or "set [scanner] device" in step
                )
            case "restart":
                assert step.endswith("restart saneless.")
                assert "press Check again" not in step
            case _:
                assert "restart" not in step

    def test_the_restart_property_covers_every_kind_of_advice(self) -> None:
        """Every kind of next step is exercised by at least one case above."""
        kinds = {
            _advice_kind(cast("_VerdictCase", param.values[0]).message)
            for param in _VERDICT_CASES
        }
        assert kinds >= {
            "check-again",
            "check-again-or-restart-after-edit",
            "restart",
        }

    @pytest.mark.parametrize("case", _VERDICT_CASES)
    def test_every_restart_is_tied_to_a_configuration_edit(
        self, case: _VerdictCase
    ) -> None:
        """
        No row tells the reader to restart saneless for a state a Check clears.

        The old conditional advice, restart if ``saneless devices`` lists the
        scanner, answered a stale scanner library in a long-lived process.  A
        fresh library on every listing retired it, so it must never come back,
        and any restart a row still advises names the setting it follows.

        Args:
            case: The inputs to the verdict.

        """
        row = _verdict(case)
        assert "If saneless devices lists it, restart saneless" not in row.next_step
        if "restart saneless" in row.next_step:
            assert any(
                edit in row.next_step
                for edit in ("setting", "[scanner] device", "[scanner] host")
            )

    @pytest.mark.parametrize(
        ("probes", "message"),
        [
            pytest.param(
                (_hp("scanbox.lan", _REFUSED),),
                "Brother ADS-2700W is ready, but the scanner service is not running on the scanner host.",
                id="refused",
            ),
            pytest.param(
                (_hp("a.lan", _HEALTHY), _hp("scanbox.lan", _REFUSED)),
                "Brother ADS-2700W is ready, but the scanner service is not running on 1 of 2 scanner hosts.",
                id="refused-one-of-two",
            ),
            pytest.param(
                (_hp("a.lan", _REFUSED), _hp("b.lan", _REFUSED)),
                "Brother ADS-2700W is ready, but the scanner service is not running on 2 of 2 scanner hosts.",
                id="refused-two-of-two",
            ),
            pytest.param(
                (_hp("scanbox.lan", _REJECTED),),
                "Brother ADS-2700W is ready, but the scanner host is refusing this machine.",
                id="rejected",
            ),
            pytest.param(
                (_hp("a.lan", _REJECTED), _hp("b.lan", _REJECTED)),
                "Brother ADS-2700W is ready, but 2 of 2 scanner hosts are refusing this machine.",
                id="rejected-two-of-two",
            ),
            pytest.param(
                (_hp("scanbox.test", _UNRESOLVED),),
                "Brother ADS-2700W is ready, but the scanner host could not be found by name.",
                id="unresolved",
            ),
            pytest.param(
                (_hp("a.lan", _HEALTHY), _hp("b.lan", _UNRESOLVED)),
                "Brother ADS-2700W is ready, but 1 of 2 scanner hosts could not be found by name.",
                id="unresolved-one-of-two",
            ),
        ],
    )
    def test_a_ready_row_says_but_once(
        self, probes: tuple[checks._HostProbe, ...], message: str
    ) -> None:
        """
        A usable scanner beside a host problem is one sentence with one "but".

        The refused wording on its own already has a "but" in it ("is on, but
        its scanner service is not running"), so appending it to "is ready,
        but" read as two.  The ready row words that outcome its own way.

        Args:
            probes: What each configured host's probe found.
            message: The exact amber sentence expected.

        """
        row = checks._scanner_verdict(
            probes, checks._Enumeration(devices=(_device(),)), ""
        )
        assert row.state is CheckState.WARN
        assert row.message == message
        assert row.message.count(" but ") == 1

    def test_the_listing_failure_rows_name_nothing_and_count_nothing(self) -> None:
        """
        The crash and timeout rows are fixed sentences: no host, no number.

        They are amber because neither proves scanning is impossible, and a
        working appliance never goes red.
        """
        for failure in checks._ListingFailure:
            row = checks._scanner_verdict(
                (_hp("scanbox.lan", _HEALTHY),),
                checks._Enumeration(devices=(), failure=failure),
                "",
            )
            assert row.state is CheckState.WARN
            rendered = f"{row.message} {row.next_step}"
            assert not any(character.isdigit() for character in rendered)
            for leaked in _LEAK_MARKERS:
                assert leaked not in rendered

    @pytest.mark.parametrize(
        "message",
        [
            _LISTING_CRASHED_MESSAGE,
            _LISTING_TIMED_OUT_MESSAGE,
            _LISTING_NO_ANSWER_MESSAGE,
        ],
        ids=["crash", "timeout", "no-answer"],
    )
    def test_a_listing_failure_message_fits_where_the_longest_one_does(
        self, message: str
    ) -> None:
        """
        The new rows are no longer than a message the status strip already holds.

        Args:
            message: One of the two listing-failure messages.

        """
        assert len(message) <= len(_LONGEST_SCANNER_MESSAGE)

    @pytest.mark.parametrize("case", _VERDICT_CASES)
    def test_no_row_names_a_host_address_or_device_id(self, case: _VerdictCase) -> None:
        """
        Rows are LAN-visible, so they carry counts and device labels only.

        Args:
            case: The inputs to the verdict.

        """
        row = _verdict(case)
        rendered = f"{row.message} {row.next_step}"
        for leaked in _LEAK_MARKERS:
            assert leaked not in rendered

    @pytest.mark.parametrize("case", _VERDICT_CASES)
    def test_a_problem_row_says_what_to_do_and_a_ready_row_does_not(
        self, case: _VerdictCase
    ) -> None:
        """
        Amber and red rows carry a next step; green rows carry none.

        Args:
            case: The inputs to the verdict.

        """
        row = _verdict(case)
        if row.state is CheckState.OK:
            assert row.next_step == ""
        else:
            assert row.next_step

    @pytest.mark.parametrize(
        "devices",
        [(), (_device(),), (_device(), _rogue_device())],
        ids=["none", "one", "two"],
    )
    def test_a_host_that_must_not_be_enumerated_gives_the_preflight_row(
        self, devices: tuple[DeviceInfo, ...]
    ) -> None:
        """
        The verdict and the preflight can never disagree about a silent host.

        Whatever the enumeration is claimed to have seen or failed at, a
        timed-out host gives the row the preflight returns for it.

        Args:
            devices: Whatever enumeration is claimed to have listed.

        """
        probes = (_hp("scanbox.lan", _HEALTHY), _hp("b.lan", _TIMED_OUT))
        for failure in (None, *checks._ListingFailure):
            row = checks._scanner_verdict(
                probes,
                checks._Enumeration(devices=devices, failure=failure),
                _device().name,
            )
            assert row == checks._scanner_host_unanswered(probes)

    @pytest.mark.parametrize(
        ("outcome", "blocks"),
        [
            pytest.param(_TIMED_OUT, True, id="timed-out"),
            pytest.param(_REFUSED, False, id="refused"),
            pytest.param(_UNRESOLVED, False, id="unresolved"),
            pytest.param(_REJECTED, False, id="rejected"),
            pytest.param(_HEALTHY, False, id="healthy"),
        ],
    )
    def test_only_a_timed_out_host_keeps_the_check_out_of_libsane(
        self, outcome: checks._SanedOutcome, *, blocks: bool
    ) -> None:
        """
        Every outcome but a silent host returns at once inside libsane.

        Args:
            outcome: What one probe found.
            blocks: Whether that outcome keeps the check out of libsane.

        """
        assert checks._blocks_enumeration(outcome) is blocks

    def test_an_enumeration_records_no_open_by_default(self) -> None:
        """No open was attempted unless the caller says one was."""
        assert checks._Enumeration(devices=()).configured_opened is None

    def test_an_enumeration_records_no_listing_failure_by_default(self) -> None:
        """A listing completed unless the caller says it crashed or timed out."""
        assert checks._Enumeration(devices=()).failure is None

    @pytest.mark.parametrize(
        ("device_id", "expected"),
        [
            ("net:scanbox.lan:brother5:bus0;dev1", "scanbox.lan"),
            ("net:[fe80::1]:x:0", "[fe80::1]"),
            ("net:scanbox.lan", "scanbox.lan"),
            ("epson2:libusb:001:004", None),
            ("escl:http://10.0.0.5:80", None),
            ("net:", None),
            ("net::x:0", None),
            ("net:[fe80::1", None),
            ("", None),
        ],
    )
    def test_the_host_a_net_device_lives_on(
        self, device_id: str, expected: str | None
    ) -> None:
        """
        A ``net:`` device id names the host entry it was listed from.

        Args:
            device_id: A SANE device id.
            expected: The host entry, or ``None`` for an id that names none.

        """
        assert checks._net_device_entry(device_id) == expected


# ---------------------------------------------------------------------------
# The whole registry against a fake saned
# ---------------------------------------------------------------------------

# What the rows below may never carry.  The loopback address and a port are
# what a real scanner host's address and port stand in for here, "scanbox" is
# the only host name any of these tests configures, and a slash is how every
# path, URL and device id would show up.  The resolver's own text is checked
# in the log records, which is where an unguarded ``str(exc)`` would land.
_E2E_ROW_FORBIDDEN: Final = ("127.0.0", "scanbox", "/")
_E2E_LOG_FORBIDDEN: Final = ("127.0.0", "scanbox", "Name or service not known")

# The red row for a host that refuses this machine when nothing is usable.
_REJECTED_MESSAGE: Final = "The scanner host is refusing this machine."

# The red row for a host that answered and listed nothing.
_ANSWERING_NOTHING_FOUND_MESSAGE: Final = (
    "The scanner host is answering, but no scanner was found on it."
)


@dataclass(frozen=True, slots=True)
class _EndToEndRun:
    """
    One registry run's Scanner row, and the backend that run was handed.

    Attributes:
        row: The Scanner row ``run_checks`` returned.
        backend: The fresh backend that run used, so its counts are its own.
        seconds: How long the whole ``run_checks`` call took.

    """

    row: CheckResult
    backend: _CountingBackend
    seconds: float


def _run_ungated_and_gated(
    settings: Settings,
    devices: Sequence[DeviceInfo],
    *,
    backend_type: type[_CountingBackend] = _CountingBackend,
) -> tuple[_EndToEndRun, _EndToEndRun]:
    """
    Run the whole registry twice: once as ``doctor`` does, once as the strip does.

    Each run gets its own backend, so ``calls`` counts one run and not two.
    The gated run is handed a real ``threading.Lock``, not a recorder, because
    what is under test is the registry as the worker drives it; the lock is
    asserted free afterwards, so a run that kept it fails here.

    Args:
        settings: The configuration both runs read.
        devices: What each run's backend lists.
        backend_type: The counting backend each run is handed, so a case can
            use one whose opens fail.

    Returns:
        The ungated run, then the gated one.

    """
    gate = threading.Lock()
    runs: list[_EndToEndRun] = []
    for scanner_gate in (None, gate):
        backend = backend_type(list(devices))
        started = monotonic()
        results = run_checks(
            _context(settings, scanner=backend), scanner_gate=scanner_gate
        )
        runs.append(
            _EndToEndRun(
                row=_row(results, CheckKey.SCANNER),
                backend=backend,
                seconds=monotonic() - started,
            )
        )
    assert not gate.locked()
    ungated, gated = runs
    assert gated.row == ungated.row
    return ungated, gated


def _assert_names_nothing(
    runs: Iterable[_EndToEndRun], caplog: pytest.LogCaptureFixture, port: int
) -> None:
    """
    ASVS V7: neither the rows nor anything logged names the host or its port.

    Args:
        runs: The runs whose rows are checked.
        caplog: The log capture the runs were made under, at DEBUG.
        port: The loopback port the fake or the closed socket had.

    """
    for run in runs:
        for text in (run.row.message, run.row.next_step):
            for forbidden in (*_E2E_ROW_FORBIDDEN, str(port)):
                assert forbidden not in text
    messages = [record.getMessage() for record in caplog.records]
    assert messages, "nothing was logged, so the log half of this guard is vacuous"
    for message in messages:
        for forbidden in (*_E2E_LOG_FORBIDDEN, str(port)):
            assert forbidden not in message


@pytest.fixture
def _short_probe_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Shorten both probe budgets, so a silent peer costs half a second, not seven.

    Args:
        monkeypatch: pytest's attribute patcher.

    """
    monkeypatch.setattr(checks, "PROBE_CONNECT_SECONDS", _PROBE_BUDGET)
    monkeypatch.setattr(checks, "PROBE_HANDSHAKE_SECONDS", _PROBE_BUDGET)


@pytest.mark.usefixtures("_short_probe_budgets")
class TestScannerCheckAgainstAFakeSaned:
    """
    The real registry, real sockets and the real gate agree on the Scanner row.

    The tests above this class take one piece at a time, with the probe
    stubbed or the backend absent.  Here nothing is stubbed between
    ``run_checks`` and the socket: the pre-probe dials a fake saned on
    127.0.0.1, the preflight classifies what came back, the gated and ungated
    paths each enumerate a stub backend, and the verdict writes the row.  Each
    case runs both paths with a fresh backend and requires the same row from
    both.  Every fake is used inside its context manager, so a socket left open
    or an exception escaping its thread fails the test under
    ``filterwarnings = ["error"]``.
    """

    def test_accept_then_close_with_nothing_listed_is_a_denial(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        The 2026-09-22 signature reads as a denial and names saned.conf.

        saned accepted the connection and closed it before replying, which is
        what its access list does to a peer it does not allow.  The row this
        used to produce said the scanner was not reachable and to check it was
        switched on and connected; it must say the host is refusing this
        machine.

        Args:
            tmp_path: The test's own directory.
            caplog: Captures every log record at DEBUG.

        """
        caplog.set_level(logging.DEBUG)
        with fake_saned(SanedBehaviour.CLOSE) as fake:
            settings = _with_device(
                _healthy_settings(tmp_path, host=f"127.0.0.1:{fake.port}"), ""
            )
            runs = _run_ungated_and_gated(settings, [])
        for run in runs:
            assert run.backend.calls == 1
            assert run.row.state is CheckState.FAIL
            assert run.row.message == _REJECTED_MESSAGE
            assert _strip(run.row.next_step) == _REJECTED_NEXT
            assert "saned.conf" in _strip(run.row.next_step)
            for text in (run.row.message, _strip(run.row.next_step)):
                assert "switched on and connected" not in text
        _assert_names_nothing(runs, caplog, fake.port)

    def test_accept_then_close_beside_a_usable_scanner_is_amber(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        A denial beside a scanner that works is a warning, not a failure.

        Args:
            tmp_path: The test's own directory.
            caplog: Captures every log record at DEBUG.

        """
        caplog.set_level(logging.DEBUG)
        with fake_saned(SanedBehaviour.CLOSE) as fake:
            settings = _with_device(
                _healthy_settings(tmp_path, host=f"127.0.0.1:{fake.port}"), ""
            )
            runs = _run_ungated_and_gated(settings, [_device()])
        for run in runs:
            assert run.backend.calls == 1
            assert run.row.state is CheckState.WARN
            assert run.row.message == (
                "Brother ADS-2700W is ready, but the scanner host is refusing "
                "this machine."
            )
            assert _strip(run.row.next_step) == _REJECTED_NEXT
        _assert_names_nothing(runs, caplog, fake.port)

    def test_a_failure_status_reads_the_same_as_accept_then_close(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        A saned that answers with a failure status is refusing this machine too.

        Args:
            tmp_path: The test's own directory.
            caplog: Captures every log record at DEBUG.

        """
        caplog.set_level(logging.DEBUG)
        with fake_saned(SanedBehaviour.BAD_STATUS) as fake:
            settings = _with_device(
                _healthy_settings(tmp_path, host=f"127.0.0.1:{fake.port}"), ""
            )
            runs = _run_ungated_and_gated(settings, [])
        for run in runs:
            assert run.backend.calls == 1
            assert run.row.state is CheckState.FAIL
            assert run.row.message == _REJECTED_MESSAGE
            assert _strip(run.row.next_step) == _REJECTED_NEXT
        _assert_names_nothing(runs, caplog, fake.port)

    def test_a_refused_host_is_enumerated_and_red_when_nothing_is_listed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        A stopped scanner service with nothing else usable is red on both surfaces.

        A refused connect returns at once inside libsane, so the host is
        listed like any other.  With nothing usable listed, scanning cannot
        work, so the row is red, and ``saneless doctor`` exits non-zero on a
        network-only appliance whose saned is down.

        Args:
            tmp_path: The test's own directory.
            caplog: Captures every log record at DEBUG.

        """
        caplog.set_level(logging.DEBUG)
        port = _closed_port()
        settings = _with_device(
            _healthy_settings(tmp_path, host=f"127.0.0.1:{port}"), ""
        )
        runs = _run_ungated_and_gated(settings, [])
        for run in runs:
            assert run.backend.calls == 1
            assert run.row.state is CheckState.FAIL
            assert run.row.message == _REFUSED_MESSAGE
            assert _strip(run.row.next_step) == _REFUSED_NEXT_STEP
        backend = _CountingBackend()
        doctor_row = checks._check_scanner(_context(settings, scanner=backend))
        assert backend.calls == 1
        assert doctor_row == runs[0].row
        assert worst_state([doctor_row]) is CheckState.FAIL
        _assert_names_nothing(runs, caplog, port)

    def test_a_refused_host_beside_a_usable_scanner_is_enumerated_and_amber(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        A stopped scanner service beside a scanner that works is a warning.

        Args:
            tmp_path: The test's own directory.
            caplog: Captures every log record at DEBUG.

        """
        caplog.set_level(logging.DEBUG)
        port = _closed_port()
        settings = _with_device(
            _healthy_settings(tmp_path, host=f"127.0.0.1:{port}"), ""
        )
        runs = _run_ungated_and_gated(settings, [_device()])
        for run in runs:
            assert run.backend.calls == 1
            assert run.row.state is CheckState.WARN
            assert run.row.message == _REFUSED_READY_MESSAGE
            assert _strip(run.row.next_step) == _REFUSED_NEXT_STEP
        _assert_names_nothing(runs, caplog, port)

    def test_a_configured_net_device_on_a_refusing_host_is_opened_and_reports_its_host(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A configured device's own host is dialled for real, then the device opened.

        No scanner host is configured, and the backend lists nothing, so the
        check opens the configured ``net:`` id.  The default saned port is
        pointed at a closed loopback port, so the host refuses, which returns
        at once inside libsane and so does not keep the open out.  The open
        fails, and the row blames the device's own host rather than saying
        the scanner was not found.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Points the default saned port at the closed port.
            caplog: Captures every log record at DEBUG.

        """
        caplog.set_level(logging.DEBUG)
        port = _closed_port()
        monkeypatch.setattr(checks, "SANED_PORT", port)
        settings = _with_device(
            _healthy_settings(tmp_path, host=""), "net:127.0.0.1:brother5:bus0;dev1"
        )
        runs = _run_ungated_and_gated(settings, [], backend_type=_UnopenableBackend)
        for run in runs:
            assert run.backend.calls == 1
            assert run.backend.opens == 1
            assert run.row.state is CheckState.FAIL
            assert run.row.message == (
                "The configured scanner's host is on, but its scanner service "
                "is not running."
            )
            assert _strip(run.row.next_step) == _REFUSED_NEXT_STEP
        _assert_names_nothing(runs, caplog, port)

    def test_a_silent_peer_is_amber_bounded_and_never_enumerated(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        listening_port: int,
    ) -> None:
        """
        A peer that accepts and says nothing times out inside the budget.

        libsane bounds nothing after its connect, so enumerating this host
        would wait for as long as the peer stayed quiet.  Each run is timed
        rather than waited through: it has to come back within a few budgets,
        and without having entered SANE.

        Args:
            tmp_path: The test's own directory.
            caplog: Captures every log record at DEBUG.
            listening_port: A loopback port that listens and never answers.

        """
        caplog.set_level(logging.DEBUG)
        settings = _with_device(
            _healthy_settings(tmp_path, host=f"127.0.0.1:{listening_port}"), ""
        )
        runs = _run_ungated_and_gated(settings, [_device()])
        for run in runs:
            assert run.backend.calls == 0
            assert run.row.state is CheckState.WARN
            assert run.row.message == _TIMED_OUT_MESSAGE
            assert _strip(run.row.next_step) == _TIMED_OUT_NEXT_STEP
            assert run.seconds < 4 * _PROBE_BUDGET
        _assert_names_nothing(runs, caplog, listening_port)

    def test_a_healthy_saned_is_ready_and_left_one_clean_session_per_probe(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        A valid handshake reaches the backend, and saned sees INIT then EXIT.

        Two runs are two probes, and the fake serves them one after the other,
        so the complete record is two sessions end to end: the 21-byte INIT
        request, then ``SANE_NET_EXIT``, twice, and nothing else.

        Args:
            tmp_path: The test's own directory.
            caplog: Captures every log record at DEBUG.

        """
        caplog.set_level(logging.DEBUG)
        with fake_saned(SanedBehaviour.HEALTHY) as fake:
            settings = _with_device(
                _healthy_settings(tmp_path, host=f"127.0.0.1:{fake.port}"), ""
            )
            runs = _run_ungated_and_gated(settings, [_device()])
        for run in runs:
            assert run.backend.calls == 1
            assert run.row.state is CheckState.OK
            assert run.row.message == "Brother ADS-2700W is ready."
            assert run.row.next_step == ""
        assert len(INIT_REQUEST) == 21
        assert b"".join(fake.received) == (INIT_REQUEST + EXIT_REQUEST) * len(runs)
        _assert_names_nothing(runs, caplog, fake.port)

    def test_a_healthy_saned_with_nothing_listed_is_red(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        A host that answers rules the network out, so the row says so.

        Args:
            tmp_path: The test's own directory.
            caplog: Captures every log record at DEBUG.

        """
        caplog.set_level(logging.DEBUG)
        with fake_saned(SanedBehaviour.HEALTHY) as fake:
            settings = _with_device(
                _healthy_settings(tmp_path, host=f"127.0.0.1:{fake.port}"), ""
            )
            runs = _run_ungated_and_gated(settings, [])
        for run in runs:
            assert run.backend.calls == 1
            assert run.row.state is CheckState.FAIL
            assert run.row.message == _ANSWERING_NOTHING_FOUND_MESSAGE
            assert _strip(run.row.next_step) == _HOST_ANSWERS_NOTHING_FOUND_NEXT
        _assert_names_nothing(runs, caplog, fake.port)

    def test_a_name_that_does_not_resolve_is_red_and_says_restart(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        An unresolved name is enumerated, finds nothing, and names the setting.

        The resolver is replaced rather than asked about a reserved name, so
        the suite stays offline.  Its error text is what an unguarded log line
        would repeat, so the log half of the guard looks for it.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Replaces the resolver.
            caplog: Captures every log record at DEBUG.

        """

        def _fail(*_args: object, **_kwargs: object) -> list[object]:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", _fail)
        caplog.set_level(logging.DEBUG)
        settings = _with_device(_healthy_settings(tmp_path, host="scanbox.test"), "")
        runs = _run_ungated_and_gated(settings, [])
        for run in runs:
            assert run.backend.calls == 1
            assert run.row.state is CheckState.FAIL
            assert run.row.message == "The scanner host could not be found by name."
            assert _strip(run.row.next_step) == _UNRESOLVED_NEXT
        _assert_names_nothing(runs, caplog, SANED_PORT)

    def test_one_answering_host_beside_a_refusing_one_is_enumerated(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A second host refusing the connection does not keep SANE out.

        Both entries use the default port, which is pointed at the fake.  The
        fake listens on 127.0.0.1 only, so the same port on 127.0.0.2 refuses.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Points the default saned port at the fake.
            caplog: Captures every log record at DEBUG.

        """
        caplog.set_level(logging.DEBUG)
        with fake_saned(SanedBehaviour.HEALTHY) as fake:
            monkeypatch.setattr(checks, "SANED_PORT", fake.port)
            settings = _with_device(
                _healthy_settings(tmp_path, host="127.0.0.1:127.0.0.2"), ""
            )
            runs = _run_ungated_and_gated(settings, [_device()])
        for run in runs:
            assert run.backend.calls == 1
            assert run.row.state is CheckState.WARN
            assert run.row.message == (
                "Brother ADS-2700W is ready, but the scanner service is not "
                "running on 1 of 2 scanner hosts."
            )
            assert _strip(run.row.next_step) == _REFUSED_NEXT_STEP
        assert b"".join(fake.received) == (INIT_REQUEST + EXIT_REQUEST) * len(runs)
        _assert_names_nothing(runs, caplog, fake.port)


_PROBE_OUTCOMES = [
    pytest.param(200, CheckState.OK, ConnectionStatus.CONNECTED, id="connected"),
    pytest.param(
        401, CheckState.FAIL, ConnectionStatus.TOKEN_REJECTED, id="token-rejected"
    ),
    pytest.param(404, CheckState.FAIL, ConnectionStatus.NOT_FOUND, id="not-found"),
    pytest.param(
        500, CheckState.FAIL, ConnectionStatus.SERVER_ERROR, id="server-error"
    ),
    pytest.param(
        406, CheckState.FAIL, ConnectionStatus.INCOMPATIBLE, id="incompatible"
    ),
    pytest.param(302, CheckState.FAIL, ConnectionStatus.REDIRECTED, id="redirected"),
]


class TestPaperlessCheck:
    """The Paperless row reuses the five sentences that already exist (D-02)."""

    @pytest.mark.parametrize("token", ["", "   ", "changeme", "YOUR_TOKEN_HERE"])
    def test_a_placeholder_token_fails_without_a_request(
        self, tmp_path: Path, token: str
    ) -> None:
        """
        D-14: an unset token is decided before any network call is made.

        Args:
            tmp_path: The test's own directory.
            token: A token nobody replaced.

        """
        counter = _RequestCounter(_ok_response)
        client = _paperless(counter)
        try:
            results = run_checks(
                _context(_settings(tmp_path, token=token), paperless=client)
            )
        finally:
            client.close()
        row = _row(results, CheckKey.PAPERLESS)
        assert counter.count == 0
        assert row.state is CheckState.FAIL
        assert row.message == "The paperless-ngx API token has not been set."
        assert row.next_step == (
            "Put a real API token in the saneless config file, then restart saneless."
        )

    def test_an_unset_url_fails_as_configuration_without_a_request(
        self, tmp_path: Path
    ) -> None:
        """
        An empty ``paperless.url`` is a setting to fill in, not a network fault.

        Probing it would fail inside httpx2 before any request is sent, and
        that failure used to be reported as "Could not reach paperless-ngx",
        sending the operator to look for a server that was never named.

        Args:
            tmp_path: The test's own directory.

        """
        counter = _RequestCounter(_ok_response)
        client = _paperless(counter, url="")
        try:
            results = run_checks(
                _context(_without_url(_settings(tmp_path)), paperless=client)
            )
        finally:
            client.close()
        row = _row(results, CheckKey.PAPERLESS)
        assert counter.count == 0
        assert row.state is CheckState.FAIL
        assert row.message == "The paperless-ngx address has not been set."
        assert row.next_step == (
            "Set paperless.url in the saneless config file to the paperless-ngx "
            "address, then restart saneless."
        )

    def test_an_unset_token_is_named_before_an_unset_url(self, tmp_path: Path) -> None:
        """
        A fresh install has neither; the row names the token first.

        Args:
            tmp_path: The test's own directory.

        """
        counter = _RequestCounter(_ok_response)
        client = _paperless(counter, url="")
        try:
            results = run_checks(
                _context(_without_url(_settings(tmp_path, token="")), paperless=client)
            )
        finally:
            client.close()
        row = _row(results, CheckKey.PAPERLESS)
        assert counter.count == 0
        assert row.message == "The paperless-ngx API token has not been set."

    @pytest.mark.parametrize(("status_code", "state", "status"), _PROBE_OUTCOMES)
    def test_each_outcome_reuses_the_existing_sentence(
        self,
        tmp_path: Path,
        status_code: int,
        state: CheckState,
        status: ConnectionStatus,
    ) -> None:
        """
        Every outcome's message comes from ``connection_status_message``.

        Args:
            tmp_path: The test's own directory.
            status_code: What the stub server answers with.
            state: The check state that outcome produces.
            status: The ConnectionStatus the status code maps to.

        """

        def responder(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(status_code, text="")

        counter = _RequestCounter(responder)
        client = _paperless(counter)
        try:
            results = run_checks(_context(_settings(tmp_path), paperless=client))
        finally:
            client.close()
        row = _row(results, CheckKey.PAPERLESS)
        assert row.state is state
        assert row.message == connection_status_message(status)

    def test_an_unreachable_host_reuses_the_existing_sentence(
        self, tmp_path: Path
    ) -> None:
        """
        A transport failure is the fifth outcome, with its own next step.

        Args:
            tmp_path: The test's own directory.

        """

        def responder(_request: httpx2.Request) -> httpx2.Response:
            msg = "no route"
            raise httpx2.ConnectError(msg)

        counter = _RequestCounter(responder)
        client = _paperless(counter)
        try:
            results = run_checks(_context(_settings(tmp_path), paperless=client))
        finally:
            client.close()
        row = _row(results, CheckKey.PAPERLESS)
        assert row.state is CheckState.FAIL
        assert row.message == "Could not reach paperless-ngx."
        assert _strip(row.next_step) == (
            "Check paperless-ngx is running and on the network, then press Check again."
        )

    def test_an_incompatible_paperless_names_the_release_needed(
        self, tmp_path: Path
    ) -> None:
        """
        A 406 is its own failure row, not a server error, and says what to run.

        Args:
            tmp_path: The test's own directory.

        """

        def responder(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                406, json={"detail": 'Invalid version in "Accept" header.'}
            )

        counter = _RequestCounter(responder)
        client = _paperless(counter)
        try:
            results = run_checks(_context(_settings(tmp_path), paperless=client))
        finally:
            client.close()
        row = _row(results, CheckKey.PAPERLESS)
        assert row.state is CheckState.FAIL
        assert row.message == (
            "This paperless-ngx does not speak an API version saneless supports."
        )
        assert _strip(row.next_step) == (
            "saneless needs paperless-ngx 2.16 or later (API version 9 or 10); "
            "upgrade paperless-ngx, then press Check again."
        )

    @staticmethod
    def _row_for(
        tmp_path: Path, responder: Callable[[httpx2.Request], httpx2.Response]
    ) -> CheckResult:
        """
        Run the checks against a stub paperless-ngx and return its row.

        Args:
            tmp_path: The test's own directory.
            responder: What the stub server answers with, or raises.

        Returns:
            The Paperless row.

        """
        client = _paperless(_RequestCounter(responder))
        try:
            results = run_checks(_context(_settings(tmp_path), paperless=client))
        finally:
            client.close()
        return _row(results, CheckKey.PAPERLESS)

    def test_a_redirect_row_names_no_address(self, tmp_path: Path) -> None:
        """
        A redirect says to check paperless.url, never where it pointed.

        The strip is visible to anyone on the LAN, so the target is kept for
        the log and ``doctor`` only.

        Args:
            tmp_path: The test's own directory.

        """

        def responder(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                302, headers={"location": "https://elsewhere.example:9443/login/"}
            )

        row = self._row_for(tmp_path, responder)
        assert row.state is CheckState.FAIL
        assert row.message == connection_status_message(ConnectionStatus.REDIRECTED)
        assert "paperless.url" in row.next_step
        assert "https://" not in row.next_step
        for text in (row.message, row.next_step):
            assert "elsewhere.example" not in text
            assert "9443" not in text

    def test_a_redirect_to_https_says_use_https(self, tmp_path: Path) -> None:
        """
        Only the scheme changed, so the next step says to use https://.

        Args:
            tmp_path: The test's own directory.

        """

        def responder(request: httpx2.Request) -> httpx2.Response:
            target = request.url.copy_with(scheme="https")
            return httpx2.Response(301, headers={"location": str(target)})

        row = self._row_for(tmp_path, responder)
        assert row.state is CheckState.FAIL
        assert row.message == connection_status_message(ConnectionStatus.REDIRECTED)
        assert "paperless.url" in row.next_step
        assert "https://" in row.next_step
        for text in (row.message, row.next_step):
            assert "paperless:8000" not in text

    @pytest.mark.parametrize(
        "exc_type",
        [
            pytest.param(httpx2.UnsupportedProtocol, id="unsupported-protocol"),
            pytest.param(httpx2.LocalProtocolError, id="local-protocol-error"),
        ],
    )
    def test_a_misconfigured_row_is_configuration_copy(
        self, tmp_path: Path, exc_type: type[httpx2.TransportError]
    ) -> None:
        """
        A URL the library will not use is a setting to correct, not a network fault.

        The client is built with an empty token, which is outside the load
        rules, so a local refusal is blamed on the configuration as well.

        Args:
            tmp_path: The test's own directory.
            exc_type: How the library refused the request.

        """

        def responder(_request: httpx2.Request) -> httpx2.Response:
            msg = "refused before sending"
            raise exc_type(msg)

        client = PaperlessClient(
            url="http://paperless:8000",
            token="",
            transport=httpx2.MockTransport(_RequestCounter(responder)),
        )
        try:
            results = run_checks(_context(_settings(tmp_path), paperless=client))
        finally:
            client.close()
        row = _row(results, CheckKey.PAPERLESS)
        assert row.state is CheckState.FAIL
        assert row.message == connection_status_message(ConnectionStatus.MISCONFIGURED)
        assert "paperless.url" in row.next_step
        assert "paperless.token" in row.next_step
        for text in (row.message, row.next_step):
            assert "reach" not in text.lower()

    def test_the_probe_is_bounded(self, tmp_path: Path) -> None:
        """
        The check never spends the client's flat 30 s on a health row.

        Args:
            tmp_path: The test's own directory.

        """
        counter = _RequestCounter(_ok_response)
        client = _paperless(counter)
        try:
            run_checks(_context(_settings(tmp_path), paperless=client))
        finally:
            client.close()
        assert counter.timeouts[0]["connect"] == PROBE_CONNECT_SECONDS
        assert counter.timeouts[0]["read"] == PROBE_READ_SECONDS

    def test_no_client_is_an_address_problem(self, tmp_path: Path) -> None:
        """
        A client missing for no stated reason is reported as a bad address.

        ``PaperlessClient.__init__`` refuses a URL or token it cannot use and a
        TLS trust store it cannot read, and a caller that knows which says so
        on the context (``TestRefusalRows``).  One that says nothing keeps the
        "not found at that URL" row.

        Args:
            tmp_path: The test's own directory.

        """
        results = run_checks(_context(_settings(tmp_path), paperless=None))
        row = _row(results, CheckKey.PAPERLESS)
        assert row.state is CheckState.FAIL
        assert row.message == "The paperless-ngx API was not found at that URL."
        assert row.next_step == (
            "Check the paperless-ngx address in the saneless config file."
        )


class TestProfilesCheck:
    """One result, chosen by a precedence this test pins (D-22, A-3)."""

    def test_no_profiles_fails(self, tmp_path: Path) -> None:
        """
        An appliance with no profile cannot scan at all.

        Args:
            tmp_path: The test's own directory.

        """
        results = run_checks(_context(_with_profiles(_settings(tmp_path), {})))
        row = _row(results, CheckKey.PROFILES)
        assert row.state is CheckState.FAIL
        assert row.message == "No scan profiles are configured."
        assert row.next_step == 'Run "saneless auto-profiles" to create them.'

    def test_a_readonly_config_location_is_amber(self, tmp_path: Path) -> None:
        """
        D-22, verbatim: a read-only config location is a warning, not a failure.

        Args:
            tmp_path: The test's own directory.

        """
        context = _context(
            _settings(tmp_path), profile_storage=ProfileStorage.IN_MEMORY_UNWRITABLE
        )
        row = _row(run_checks(context), CheckKey.PROFILES)
        assert row.state is CheckState.WARN
        assert row.message == (
            "Generated in memory — the config location is read-only, "
            "so they are lost on restart."
        )
        assert row.next_step == (
            "Make the saneless config directory writable, then restart saneless."
        )

    def test_no_config_file_is_its_own_amber_row(self, tmp_path: Path) -> None:
        """
        "No file to save to" and "cannot write the file" are different facts.

        Args:
            tmp_path: The test's own directory.

        """
        context = _context(
            _settings(tmp_path),
            profile_storage=ProfileStorage.IN_MEMORY_NO_CONFIG_FILE,
        )
        row = _row(run_checks(context), CheckKey.PROFILES)
        assert row.state is CheckState.WARN
        assert row.message == (
            "Generated in memory — no configuration file is in use, "
            "so they are lost on restart."
        )
        assert (
            row.next_step == "Create a saneless config file so the profiles are saved."
        )

    def test_an_unnamed_generated_profile_is_amber(self, tmp_path: Path) -> None:
        """
        A-3: a generated profile with no label shows as a name the dropdown fakes.

        Args:
            tmp_path: The test's own directory.

        """
        profiles = {"default": ProfileConfig(auto_generated=True, label="")}
        row = _row(
            run_checks(_context(_with_profiles(_settings(tmp_path), profiles))),
            CheckKey.PROFILES,
        )
        assert row.state is CheckState.WARN
        assert row.message == "Some profiles have no name yet."
        assert row.next_step == 'Run "saneless auto-profiles --force" to name them.'

    def test_a_hand_written_profile_without_a_label_is_fine(
        self, tmp_path: Path
    ) -> None:
        """
        The A-3 warning is about generated profiles, not hand-written ones.

        Args:
            tmp_path: The test's own directory.

        """
        profiles = {"default": ProfileConfig(auto_generated=False, label="")}
        row = _row(
            run_checks(_context(_with_profiles(_settings(tmp_path), profiles))),
            CheckKey.PROFILES,
        )
        assert row.state is CheckState.OK

    def test_the_count_is_pluralised(self, tmp_path: Path) -> None:
        """
        Two profiles read as two, and one reads as one.

        Args:
            tmp_path: The test's own directory.

        """
        two = {"a": ProfileConfig(), "b": ProfileConfig()}
        row = _row(
            run_checks(_context(_with_profiles(_settings(tmp_path), two))),
            CheckKey.PROFILES,
        )
        assert row.message == "2 scan profiles configured."
        one = {"a": ProfileConfig()}
        row = _row(
            run_checks(_context(_with_profiles(_settings(tmp_path), one))),
            CheckKey.PROFILES,
        )
        assert row.message == "1 scan profile configured."

    def test_none_configured_beats_a_readonly_location(self, tmp_path: Path) -> None:
        """
        Precedence, step one: a red row wins over an amber one.

        Args:
            tmp_path: The test's own directory.

        """
        context = _context(
            _with_profiles(_settings(tmp_path), {}),
            profile_storage=ProfileStorage.IN_MEMORY_UNWRITABLE,
        )
        assert _row(run_checks(context), CheckKey.PROFILES).state is CheckState.FAIL

    def test_a_readonly_location_beats_an_unnamed_profile(self, tmp_path: Path) -> None:
        """
        Precedence, step two: losing the profiles matters more than naming them.

        Args:
            tmp_path: The test's own directory.

        """
        profiles = {"default": ProfileConfig(auto_generated=True, label="")}
        context = _context(
            _with_profiles(_settings(tmp_path), profiles),
            profile_storage=ProfileStorage.IN_MEMORY_UNWRITABLE,
        )
        row = _row(run_checks(context), CheckKey.PROFILES)
        assert "read-only" in row.message


class TestFallbackCheck:
    """A missing fallback folder is amber, never red (APPL-11, D-22)."""

    def test_an_unset_fallback_is_amber(self, tmp_path: Path) -> None:
        """
        APPL-11 verbatim: not configured is a warning about a real risk.

        Args:
            tmp_path: The test's own directory.

        """
        row = _row(run_checks(_context(_settings(tmp_path))), CheckKey.FALLBACK)
        assert row.state is CheckState.WARN
        assert row.message == (
            "Not configured; scans cannot be kept if paperless-ngx is down."
        )
        assert row.next_step == (
            "Set a fallback folder in the saneless config so scans are kept "
            "when paperless-ngx is down."
        )

    def test_a_writable_fallback_is_green(self, tmp_path: Path) -> None:
        """
        A configured folder that accepts a file is green.

        Args:
            tmp_path: The test's own directory.

        """
        folder = tmp_path / "consume"
        folder.mkdir()
        settings = _settings(tmp_path, consume_dir=str(folder))
        row = _row(run_checks(_context(settings)), CheckKey.FALLBACK)
        assert row.state is CheckState.OK
        assert row.message == (
            "A folder is set up to keep scans if paperless-ngx is down."
        )

    def test_a_missing_fallback_folder_is_red(self, tmp_path: Path) -> None:
        """
        A folder that was configured and is not there cannot keep anything.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _settings(tmp_path, consume_dir=str(tmp_path / "gone"))
        row = _row(run_checks(_context(settings)), CheckKey.FALLBACK)
        assert row.state is CheckState.FAIL
        assert row.message == "The fallback folder cannot be written to."
        assert row.next_step == "Check the folder exists and saneless can write to it."

    def test_an_unwritable_fallback_folder_is_red(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Writability is probed by writing, not by asking ``os.access``.

        The folder's mode is left alone, so ``os.access`` still says it is
        writable: only the refused write can turn the row red.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: The test's monkeypatch fixture.

        """
        folder = tmp_path / "readonly-consume"
        folder.mkdir()
        _refuse_temp_files_in(monkeypatch, folder)
        settings = _settings(tmp_path, consume_dir=str(folder))
        row = _row(run_checks(_context(settings)), CheckKey.FALLBACK)
        assert os.access(folder, os.W_OK)
        assert row.state is CheckState.FAIL

    def test_no_row_renders_the_folder_path(self, tmp_path: Path) -> None:
        """
        The configured path never reaches the page (T-30-21).

        Args:
            tmp_path: The test's own directory.

        """
        folder = tmp_path / "consume"
        folder.mkdir()
        settings = _settings(tmp_path, consume_dir=str(folder))
        row = _row(run_checks(_context(settings)), CheckKey.FALLBACK)
        assert str(folder) not in row.message
        assert str(folder) not in row.next_step


class TestDataDirCheck:
    """The data folder holds the job database, so it has to accept a write."""

    def test_a_writable_data_folder_is_green(self, tmp_path: Path) -> None:
        """
        The default test settings point at a folder that exists.

        Args:
            tmp_path: The test's own directory.

        """
        row = _row(run_checks(_context(_settings(tmp_path))), CheckKey.DATA_DIR)
        assert row.state is CheckState.OK
        assert row.message == "The data folder is writable."

    def test_an_unwritable_data_folder_is_red(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A data folder that refuses a write stops saneless recording anything.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: The test's monkeypatch fixture.

        """
        folder = tmp_path / "readonly-data"
        folder.mkdir()
        _refuse_temp_files_in(monkeypatch, folder)
        settings = _settings(tmp_path, data_dir=str(folder))
        row = _row(run_checks(_context(settings)), CheckKey.DATA_DIR)
        assert row.state is CheckState.FAIL
        assert row.message == "output.data_dir cannot be written to."
        assert row.next_step == _WRITABLE_NEXT_STEP.format(key="output.data_dir")


# The next step of a Data folder row that names a folder saneless cannot use,
# and of one that names a working folder another local user could reach.
# ``{key}`` is the setting the row names.
_WRITABLE_NEXT_STEP: Final = (
    "Fix that folder, or set {key} in the saneless config to a folder saneless "
    "can write to, then restart saneless."
)
_PRIVATE_NEXT_STEP: Final = (
    "Remove that folder or link so saneless creates it privately, or set {key} "
    "in the saneless config to a folder only saneless's user can write to, "
    "then restart saneless."
)


def _data_folder_row(settings: Settings) -> CheckResult:
    """
    Run every check and keep the Data folder row.

    Args:
        settings: The configuration under test.

    Returns:
        The ``CheckKey.DATA_DIR`` row.

    """
    return _row(run_checks(_context(settings)), CheckKey.DATA_DIR)


def _skip_as_root() -> None:
    """Skip a test whose refusal comes from a file mode, which root ignores."""
    if os.geteuid() == 0:
        pytest.skip("root is not stopped by a file mode")


class TestDataFolderRow:
    """
    The Data folder row asks the question start-up asks, for both folders.

    ``output.data_dir`` and ``output.tmp_dir`` are judged together, each the way
    saneless will use it: an existing folder must take a write, a missing one
    must be creatable where it would go, and an existing working folder must
    also be private.  The row names the setting that is wrong, never a path.
    """

    def test_a_missing_data_folder_that_can_be_created_is_ok(
        self, tmp_path: Path
    ) -> None:
        """
        The data folder is created when needed, so its absence is no fault.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _settings(tmp_path, data_dir=str(tmp_path / "new" / "data"))
        row = _data_folder_row(settings)
        assert row.state is CheckState.OK
        assert row.message == "The data folder will be created when first needed."
        assert row.next_step == ""

    def test_a_missing_working_folder_that_can_be_created_is_ok(
        self, tmp_path: Path
    ) -> None:
        """
        The working folder is created 0700 when it is first needed.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_tmp_dir(_settings(tmp_path), tmp_path / "new" / "scratch")
        row = _data_folder_row(settings)
        assert row.state is CheckState.OK
        assert row.message == (
            "The data folder is writable, and the working folder will be "
            "created when first needed."
        )

    def test_both_missing_but_creatable_is_ok(self, tmp_path: Path) -> None:
        """
        Neither folder exists yet, and both can be made where they would go.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_tmp_dir(
            _settings(tmp_path, data_dir=str(tmp_path / "new" / "data")),
            tmp_path / "new" / "scratch",
        )
        row = _data_folder_row(settings)
        assert row.state is CheckState.OK
        assert row.message == (
            "The data and working folders will be created when first needed."
        )

    def test_both_existing_and_usable_is_ok(self, tmp_path: Path) -> None:
        """
        The healthy case keeps its existing wording.

        Args:
            tmp_path: The test's own directory.

        """
        row = _data_folder_row(_settings(tmp_path))
        assert row.state is CheckState.OK
        assert row.message == "The data folder is writable."

    def test_a_missing_data_folder_under_a_read_only_folder_fails(
        self, tmp_path: Path
    ) -> None:
        """
        The nearest existing folder is where creating it would fail.

        Args:
            tmp_path: The test's own directory.

        """
        _skip_as_root()
        locked = tmp_path / "locked"
        locked.mkdir()
        settings = _settings(tmp_path, data_dir=str(locked / "a" / "data"))
        locked.chmod(0o555)
        try:
            row = _data_folder_row(settings)
        finally:
            locked.chmod(0o700)
        assert row.state is CheckState.FAIL
        assert row.message == (
            "output.data_dir does not exist and cannot be created, because the "
            "folder it would go in cannot be written to."
        )
        assert row.next_step == _WRITABLE_NEXT_STEP.format(key="output.data_dir")

    def test_a_missing_data_folder_is_probed_by_writing_to_its_ancestor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The ancestor is probed with a real write, so the refusal holds as root.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: The test's monkeypatch fixture.

        """
        ancestor = tmp_path / "refusing"
        ancestor.mkdir()
        _refuse_temp_files_in(monkeypatch, ancestor)
        settings = _settings(tmp_path, data_dir=str(ancestor / "a" / "data"))
        row = _data_folder_row(settings)
        assert row.state is CheckState.FAIL
        assert row.message.startswith("output.data_dir ")

    def test_a_missing_working_folder_under_a_refusing_folder_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The working folder gets the same nearest-ancestor rule.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: The test's monkeypatch fixture.

        """
        ancestor = tmp_path / "refusing"
        ancestor.mkdir()
        _refuse_temp_files_in(monkeypatch, ancestor)
        settings = _with_tmp_dir(_settings(tmp_path), ancestor / "scratch")
        row = _data_folder_row(settings)
        assert row.state is CheckState.FAIL
        assert row.message.startswith("output.tmp_dir ")
        assert row.next_step == _WRITABLE_NEXT_STEP.format(key="output.tmp_dir")

    def test_a_working_folder_others_can_write_to_fails(self, tmp_path: Path) -> None:
        """
        A shared working folder is refused at start-up, so the row is red.

        Args:
            tmp_path: The test's own directory.

        """
        scratch = tmp_path / "shared"
        scratch.mkdir()
        scratch.chmod(0o777)
        row = _data_folder_row(_with_tmp_dir(_settings(tmp_path), scratch))
        assert row.state is CheckState.FAIL
        assert row.message == (
            "output.tmp_dir can be written by other users, who could read or "
            "replace the scans kept there."
        )
        assert row.next_step == _PRIVATE_NEXT_STEP.format(key="output.tmp_dir")

    def test_a_working_folder_that_is_a_symlink_fails(self, tmp_path: Path) -> None:
        """
        A symlink is refused rather than followed, even to a private folder.

        Args:
            tmp_path: The test's own directory.

        """
        target = tmp_path / "real"
        target.mkdir(mode=0o700)
        link = tmp_path / "link"
        link.symlink_to(target)
        row = _data_folder_row(_with_tmp_dir(_settings(tmp_path), link))
        assert row.state is CheckState.FAIL
        assert row.message == (
            "output.tmp_dir is a symbolic link, so another local user could "
            "redirect the scans kept there."
        )
        assert row.next_step == _PRIVATE_NEXT_STEP.format(key="output.tmp_dir")

    def test_a_working_folder_that_is_a_file_fails(self, tmp_path: Path) -> None:
        """
        A file where the working folder should be is not a folder.

        Args:
            tmp_path: The test's own directory.

        """
        row = _data_folder_row(_with_tmp_dir(_settings(tmp_path), _a_file_in(tmp_path)))
        assert row.state is CheckState.FAIL
        assert row.message == "output.tmp_dir is not a folder."

    def test_a_data_folder_that_is_a_file_fails(self, tmp_path: Path) -> None:
        """
        A file passes a write probe's parent but is no place for a database.

        Args:
            tmp_path: The test's own directory.

        """
        row = _data_folder_row(_settings(tmp_path, data_dir=str(_a_file_in(tmp_path))))
        assert row.state is CheckState.FAIL
        assert row.message == "output.data_dir is not a folder."
        assert row.next_step == _WRITABLE_NEXT_STEP.format(key="output.data_dir")

    def test_a_data_folder_under_a_file_fails(self, tmp_path: Path) -> None:
        """
        Nothing can be created under a file, however deep the missing path.

        Args:
            tmp_path: The test's own directory.

        """
        data_dir = _a_file_in(tmp_path) / "a" / "data"
        row = _data_folder_row(_settings(tmp_path, data_dir=str(data_dir)))
        assert row.state is CheckState.FAIL
        assert row.message == "output.data_dir is under a file, not a folder."

    def test_the_data_folder_is_named_first_when_both_are_wrong(
        self, tmp_path: Path
    ) -> None:
        """
        The first setting that fails decides the row.

        Args:
            tmp_path: The test's own directory.

        """
        file = _a_file_in(tmp_path)
        settings = _with_tmp_dir(_settings(tmp_path, data_dir=str(file)), file)
        row = _data_folder_row(settings)
        assert row.state is CheckState.FAIL
        assert row.message.startswith("output.data_dir ")
        assert "output.tmp_dir" not in row.message

    @pytest.mark.parametrize(
        "case", ["data-file", "data-under-file", "tmp-shared", "tmp-link", "fine"]
    )
    def test_no_row_names_a_path(self, tmp_path: Path, case: str) -> None:
        """
        The strip is visible to the LAN, so the row names a setting, never a path.

        Args:
            tmp_path: The test's own directory.
            case: Which folder state to render.

        """
        if case == "data-file":
            settings = _settings(tmp_path, data_dir=str(_a_file_in(tmp_path)))
        elif case == "data-under-file":
            data_dir = _a_file_in(tmp_path) / "data"
            settings = _settings(tmp_path, data_dir=str(data_dir))
        elif case == "tmp-shared":
            shared = tmp_path / "shared"
            shared.mkdir()
            shared.chmod(0o777)
            settings = _with_tmp_dir(_settings(tmp_path), shared)
        elif case == "tmp-link":
            target = tmp_path / "real"
            target.mkdir(mode=0o700)
            (tmp_path / "link").symlink_to(target)
            settings = _with_tmp_dir(_settings(tmp_path), tmp_path / "link")
        else:
            settings = _settings(tmp_path, data_dir=str(tmp_path / "new" / "data"))
        row = _data_folder_row(settings)
        for text in (row.message, row.next_step):
            assert str(tmp_path) not in text
            assert "/" not in text

    def test_a_missing_consume_folder_is_still_red(self, tmp_path: Path) -> None:
        """
        The consume folder is never created, so it must already exist.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _settings(tmp_path, consume_dir=str(tmp_path / "new" / "consume"))
        results = run_checks(_context(settings))
        assert _row(results, CheckKey.FALLBACK).state is CheckState.FAIL
        assert _row(results, CheckKey.DATA_DIR).state is CheckState.OK

    def test_there_are_still_six_rows(self, tmp_path: Path) -> None:
        """
        The working folder joins the Data folder row rather than adding one.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_tmp_dir(_settings(tmp_path), _a_file_in(tmp_path))
        results = run_checks(_context(settings))
        assert len(results) == 6
        assert [result.key for result in results] == list(CheckKey)


def _broken_context(tmp_path: Path) -> CheckContext:
    """
    Build a context in which every single check goes wrong.

    The Configuration row is red rather than merely amber: a leftover
    superseded-name file and nothing loaded is the one state in which a row is
    allowed to print a path at all, so the V7 guard below is only falsifiable
    against a context that actually reaches it.

    Args:
        tmp_path: The test's own directory.

    Returns:
        A context whose six rows are all WARN or FAIL.

    """
    settings = _with_profiles(
        _settings(
            tmp_path,
            host=f"127.0.0.1:{_closed_port()}",
            token="changeme",
            consume_dir=str(tmp_path / "missing-consume"),
            data_dir=str(_a_file_in(tmp_path) / "data"),
        ),
        {},
    )
    _with_discovery(settings, _discovery(tmp_path, stale=(2,)))
    return _context(
        settings,
        scanner=_RaisingBackend(),
        paperless=None,
        profile_storage=ProfileStorage.IN_MEMORY_UNWRITABLE,
    )


class TestRunChecks:
    """The registry is complete, ordered, total and unable to raise (D-02)."""

    def test_every_key_appears_exactly_once(self, tmp_path: Path) -> None:
        """
        A healthy context still produces all six rows.

        Args:
            tmp_path: The test's own directory.

        """
        results = run_checks(
            _context(_settings(tmp_path), scanner=_CountingBackend([_device()]))
        )
        keys = [result.key for result in results]
        assert set(keys) == set(CheckKey)
        assert len(keys) == len(set(keys))

    def test_every_key_appears_when_everything_is_broken(self, tmp_path: Path) -> None:
        """
        A context where nothing works still produces all six rows.

        Args:
            tmp_path: The test's own directory.

        """
        results = run_checks(_broken_context(tmp_path))
        assert {result.key for result in results} == set(CheckKey)

    def test_rows_arrive_in_member_order(self, tmp_path: Path) -> None:
        """
        Both surfaces render in this order, so the order is the registry's.

        Args:
            tmp_path: The test's own directory.

        """
        results = run_checks(_context(_settings(tmp_path)))
        assert [result.key for result in results] == list(CheckKey)

    def test_a_raising_check_becomes_a_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A check that throws is a red row, never an exception out of the strip.

        A registry that can raise would take the whole status strip down, and
        with it the four checks that were fine.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Used to break one check on purpose.

        """

        def boom(_context: CheckContext) -> CheckResult:
            msg = "the data folder check exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(checks, "_check_data_dir", boom)
        results = run_checks(_context(_settings(tmp_path)))
        row = _row(results, CheckKey.DATA_DIR)
        assert row.state is CheckState.FAIL
        assert "exploded" not in row.message
        assert row.next_step

    def test_warnings_alone_do_not_collapse_to_fail(self, tmp_path: Path) -> None:
        """
        D-01, D-22: a missing fallback and a read-only config are amber together.

        This is the case the whole three-state design exists for: the
        appliance scans and files perfectly, and a scripted health gate must
        not go red because it could be tidier.

        Args:
            tmp_path: The test's own directory.

        """
        counter = _RequestCounter(_ok_response)
        client = _paperless(counter)
        try:
            context = _context(
                _settings(tmp_path),
                scanner=_CountingBackend([_device()]),
                paperless=client,
                profile_storage=ProfileStorage.IN_MEMORY_UNWRITABLE,
            )
            results = run_checks(context)
        finally:
            client.close()
        assert worst_state(results) is CheckState.WARN
        assert _row(results, CheckKey.FALLBACK).state is CheckState.WARN
        assert _row(results, CheckKey.PROFILES).state is CheckState.WARN

    def test_a_skipped_scanner_still_runs_the_other_four(self, tmp_path: Path) -> None:
        """
        Pausing the scanner check pauses nothing else.

        Args:
            tmp_path: The test's own directory.

        """
        backend = _CountingBackend([_device()])
        results = run_checks(
            _context(_settings(tmp_path), scanner=backend, skip_scanner=True)
        )
        # Keyed by check, not by position: a member inserted above SCANNER
        # would silently turn a positional list into an assertion about a
        # different row, which is the failure mode ``_check_row`` in
        # tests/test_browser.py documents for the same reason.
        skipped = {result.key: result.skipped for result in results}
        assert skipped == {key: key is CheckKey.SCANNER for key in CheckKey}

    @pytest.mark.parametrize("key", list(CheckKey))
    def test_no_row_leaks_a_path_url_token_or_traceback(
        self, tmp_path: Path, key: CheckKey
    ) -> None:
        """
        ASVS V7: nothing internal reaches a LAN-visible page through a row.

        The one exception is narrow and is asserted as such (Phase 37 D-14):
        the Configuration row's next step, in a superseded-name state, may
        carry one of three fixed documented spellings.  Strip those and the
        rule is unchanged -- no other slash, from any row, in either field.

        Args:
            tmp_path: The test's own directory.
            key: The row under inspection.

        """
        row = _row(run_checks(_broken_context(tmp_path)), key)
        for field, text in (("message", row.message), ("next_step", row.next_step)):
            allowed = key is CheckKey.CONFIGURATION and field == "next_step"
            assert "/" not in (_without_allowed_spellings(text) if allowed else text)
            assert "http" not in text
            assert "changeme" not in text
            assert "Traceback" not in text
            assert str(tmp_path) not in text

    @pytest.mark.parametrize("key", list(CheckKey))
    def test_no_row_leaks_a_path_when_a_leftover_sits_beside_a_loaded_file(
        self, tmp_path: Path, key: CheckKey
    ) -> None:
        """
        The amber leftover state names a file too, and the same narrow rule holds.

        The companion to the case above: both superseded-name states print a
        spelling, so both are checked, and neither may print anything else.

        Args:
            tmp_path: The test's own directory.
            key: The row under inspection.

        """
        settings = _with_discovery(
            _settings(tmp_path), _discovery(tmp_path, loaded=0, stale=(2,))
        )
        row = _row(run_checks(_context(settings)), key)
        for field, text in (("message", row.message), ("next_step", row.next_step)):
            allowed = key is CheckKey.CONFIGURATION and field == "next_step"
            assert "/" not in (_without_allowed_spellings(text) if allowed else text)
            assert str(tmp_path) not in text

    def test_a_warn_row_always_says_what_to_do(self, tmp_path: Path) -> None:
        """
        Every amber and red row carries a next step; green rows carry none.

        Args:
            tmp_path: The test's own directory.

        """
        for row in run_checks(_broken_context(tmp_path)):
            assert row.next_step, row.key
        healthy = run_checks(
            _context(_settings(tmp_path), scanner=_CountingBackend([_device()]))
        )
        for row in healthy:
            if row.state is CheckState.OK:
                assert row.next_step == "", row.key


class _RecordingLock:
    """A stand-in for the worker's scanner gate that counts every attempt on it."""

    def __init__(self) -> None:
        """Wrap a real lock, with nothing recorded yet."""
        self.lock = threading.Lock()
        self.acquires = 0
        self.releases = 0

    def acquire(self, *, blocking: bool = True) -> bool:
        """
        Take the underlying lock and record the attempt.

        Keyword-only because the one permitted move on a scanner gate is
        ``acquire(blocking=False)``; a positional call is not a move this
        stand-in needs to model.

        Args:
            blocking: Whether to wait for the lock.

        Returns:
            Whether the lock was taken.

        """
        self.acquires += 1
        return self.lock.acquire(blocking=blocking)

    def release(self) -> None:
        """Hand the underlying lock back and record it."""
        self.releases += 1
        self.lock.release()

    def is_free(self) -> bool:
        """
        Say whether the lock is free right now, without recording the peek.

        Returns:
            True when nothing holds the lock at this instant.

        """
        if self.lock.acquire(blocking=False):
            self.lock.release()
            return True
        return False


class _GateProbe:
    """Records, from inside each check, whether the scanner gate was free."""

    def __init__(self, gate: _RecordingLock) -> None:
        """
        Sample ``gate`` whenever a check asks it to.

        Args:
            gate: The gate whose state is being sampled.

        """
        self.gate = gate
        self.free_during: dict[str, bool] = {}

    def sample(self, where: str) -> None:
        """
        Record whether the gate is free while ``where`` is executing.

        Args:
            where: The name of the check taking the sample.

        """
        self.free_during[where] = self.gate.is_free()


class _GateSamplingBackend(StubScannerBackend):
    """A backend that samples the scanner gate from inside its own enumeration."""

    def __init__(self, probe: _GateProbe, devices: list[DeviceInfo]) -> None:
        """
        Report ``devices``, sampling the gate on the way.

        Args:
            probe: The recorder the sample goes to.
            devices: What ``get_devices`` should return.

        """
        self.probe = probe
        self.devices = devices
        self.calls = 0

    def get_devices(self) -> list[DeviceInfo]:
        """
        Sample the gate, then report the configured devices.

        Returns:
            The devices this stub was built with.

        """
        self.calls += 1
        self.probe.sample("scanner")
        return self.devices


class _GateSamplingOpenBackend(_GateSamplingBackend):
    """A backend that also samples the scanner gate while it opens a device."""

    def __init__(self, probe: _GateProbe, devices: list[DeviceInfo]) -> None:
        """
        Report ``devices`` and count opens, sampling the gate on the way.

        Args:
            probe: The recorder the samples go to.
            devices: What ``get_devices`` should return.

        """
        super().__init__(probe, devices)
        self.opens = 0

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """
        Sample the gate, then open the device the way the plain stub does.

        Args:
            device_id: The device being opened.

        Returns:
            The stub's capabilities.

        """
        self.opens += 1
        self.probe.sample("open")
        return super().get_capabilities(device_id)


def _gate_sampling_dialler(
    monkeypatch: pytest.MonkeyPatch, probe: _GateProbe, *, outcome: checks._SanedOutcome
) -> list[tuple[str, int]]:
    """
    Replace the saned dialler with one that samples the gate before answering.

    Nothing connects, the same way ``_recording_dialler`` connects to nothing:
    what is under test is whether the *gate* is free while the pre-probe runs,
    which is decided before any socket exists.

    Args:
        monkeypatch: pytest's attribute patcher.
        probe: The recorder the gate sample goes to.
        outcome: What the stubbed probe should answer.

    Returns:
        The list the stub appends each ``(host, port)`` pair to.

    """
    dialled: list[tuple[str, int]] = []

    def _probe_stub(
        host: str,
        port: int,
        _connect_timeout: float,
        _handshake_timeout: float,
        _abort: threading.Event | None = None,
    ) -> checks._SanedOutcome:
        dialled.append((host, port))
        probe.sample("pre-probe")
        return outcome

    monkeypatch.setattr(checks, "_probe_saned", _probe_stub)
    return dialled


def _gate_sampling_context(
    tmp_path: Path, probe: _GateProbe, monkeypatch: pytest.MonkeyPatch
) -> tuple[CheckContext, PaperlessClient, _GateSamplingBackend]:
    """
    Build a healthy context whose slow checks all sample the scanner gate.

    Args:
        tmp_path: The test's own directory.
        probe: The recorder every sample goes to.
        monkeypatch: Used to make the directory writes sample the gate.

    Returns:
        The context, the Paperless client the caller must close, and the
        backend, so a test can assert how often SANE was entered.

    """
    consume_dir = tmp_path / "consume"
    consume_dir.mkdir(exist_ok=True)

    def sampling_write(_path: Path) -> bool:
        probe.sample("directories")
        return True

    monkeypatch.setattr(checks, "_directory_accepts_a_write", sampling_write)

    def sampling_response(request: httpx2.Request) -> httpx2.Response:
        probe.sample("paperless")
        return _ok_response(request)

    client = _paperless(_RequestCounter(sampling_response))
    backend = _GateSamplingBackend(probe, [_device()])
    context = _context(
        _settings(tmp_path, consume_dir=str(consume_dir)),
        scanner=backend,
        paperless=client,
    )
    return context, client, backend


# The scanner contexts whose rows differ, which is what makes them the ones a
# gated-equals-ungated claim has to cover (R3-IN-01).  Each reaches a different
# branch of the scanner check, and each ends in a different row:
#
# - no python-sane, decided before anything is touched;
# - a pre-probe row decided before the gate is reached, for a host that does
#   not answer the connection;
# - an enumeration that lists one device, and one that raises;
# - a host that refuses the connection, a host that refuses this machine, and
#   a name that does not resolve, all enumerated and finding nothing;
# - a configured device that is not listed and does not open, and one that is
#   not listed but opens, both decided by the open held under the gate;
# - a configured ``net:`` device that is not listed and whose host cannot be
#   probed, so it is deliberately never opened;
# - a listing whose child crashed, one whose child ran past its deadline, and
#   one whose child gave no usable answer, each with its own row.
#
# ``_scanner_result``'s docstring lists the same contexts, and
# ``test_every_gated_context_reaches_a_different_row`` keeps them distinct.
_GATED_CONTEXTS = (
    "no-python-sane",
    "refused",
    "timed-out",
    "one-device",
    "enumeration-raises",
    "rejected",
    "unresolved",
    "wrong-device-open-fails",
    "wrong-device-open-succeeds",
    "unprobed-net-device",
    "listing-crashed",
    "listing-timed-out",
    "listing-no-answer",
)

# What the stubbed dialler answers in each context; any context not named here
# is answered as healthy.  ``no-python-sane`` never reaches the dialler, and it
# is given the outcome that would stop the check if it did.
_GATED_DIALLER_OUTCOMES: Final[Mapping[str, checks._SanedOutcome]] = {
    "no-python-sane": checks._SanedOutcome.TIMED_OUT,
    "refused": checks._SanedOutcome.REFUSED,
    "timed-out": checks._SanedOutcome.TIMED_OUT,
    "rejected": checks._SanedOutcome.REJECTED,
    "unresolved": checks._SanedOutcome.UNRESOLVED,
}


def _gated_context_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scanner_context: str
) -> Callable[[], CheckContext]:
    """
    Return a factory for one of the scanner contexts whose rows differ.

    A factory rather than a context, because the comparison runs the checks
    twice and the backends count their calls: handing the same object to both
    runs would compare a first enumeration against a second.

    The dialler is stubbed for every variant, including the one that never
    reaches it, so no case in this parametrisation can resolve a name or open
    a socket by accident.

    Args:
        tmp_path: The test's own directory.
        monkeypatch: Used to stub the probe seam.
        scanner_context: One of ``_GATED_CONTEXTS``.

    Returns:
        A callable building a fresh context for that scenario.

    Raises:
        ValueError: If ``scanner_context`` is not one of ``_GATED_CONTEXTS``.

    """
    if scanner_context not in _GATED_CONTEXTS:
        msg = f"unknown scanner context: {scanner_context}"
        raise ValueError(msg)
    _recording_dialler(
        monkeypatch, outcome=_GATED_DIALLER_OUTCOMES.get(scanner_context, _HEALTHY)
    )

    def on_scanbox(device: str, backend: StubScannerBackend) -> CheckContext:
        settings = _with_device(_settings(tmp_path, host="scanbox.lan"), device)
        return _context(settings, scanner=backend)

    factories: dict[str, Callable[[], CheckContext]] = {
        "no-python-sane": lambda: _context(_settings(tmp_path), scanner=None),
        "refused": lambda: on_scanbox("", _CountingBackend()),
        "timed-out": lambda: on_scanbox(_DEVICE_ID, _CountingBackend([_device()])),
        "one-device": lambda: on_scanbox(_DEVICE_ID, _CountingBackend([_device()])),
        "enumeration-raises": lambda: on_scanbox("", _RaisingBackend()),
        "rejected": lambda: on_scanbox("", _CountingBackend()),
        "unresolved": lambda: on_scanbox("", _CountingBackend()),
        "wrong-device-open-fails": lambda: on_scanbox(
            _LOCAL_DEVICE_ID, _UnopenableBackend([_device()])
        ),
        "wrong-device-open-succeeds": lambda: on_scanbox(
            _LOCAL_DEVICE_ID, _CountingBackend([_device()])
        ),
        "unprobed-net-device": lambda: on_scanbox(
            "net:[fe80::1]:brother5:bus0;dev1", _CountingBackend([_device()])
        ),
        "listing-crashed": lambda: on_scanbox(
            "", _ListingFailureBackend(ListingCrashedError(_CRASH_TEXT))
        ),
        "listing-timed-out": lambda: on_scanbox(
            "", _ListingFailureBackend(ListingTimedOutError(_TIMEOUT_TEXT))
        ),
        "listing-no-answer": lambda: on_scanbox(
            "", _ListingFailureBackend(ListingNoAnswerError(_NO_ANSWER_TEXT))
        ),
    }
    return factories[scanner_context]


class TestScannerRowGuards:
    """What no Scanner row may say, across every context whose row differs."""

    @pytest.mark.parametrize("scanner_context", list(_GATED_CONTEXTS))
    @pytest.mark.parametrize("gated", [False, True], ids=["ungated", "gated"])
    def test_no_scanner_row_names_a_host_a_device_id_or_a_traceback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        scanner_context: str,
        *,
        gated: bool,
    ) -> None:
        """
        ASVS V7 across every probe outcome and both wrong-device contexts.

        The contexts cover every outcome the dialler can return -- refused,
        timed out, healthy, rejected and unresolved -- and a configured device
        whose id names a local USB scanner.  The host is ``scanbox.lan``, the
        listed device's id is a ``net:`` one, and the configured one is an
        ``epson2`` one; none of them may reach either field of the row.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Used to stub the probe seam.
            scanner_context: Which of the scanner contexts to run.
            gated: Whether the run is handed a free scanner gate.

        """
        build = _gated_context_builder(tmp_path, monkeypatch, scanner_context)
        gate = cast("threading.Lock", _RecordingLock()) if gated else None
        row = _row(run_checks(build(), scanner_gate=gate), CheckKey.SCANNER)
        for text in (row.message, row.next_step):
            for forbidden in ("/", "http", "scanbox", "net:", "epson2", "Traceback"):
                assert forbidden not in text

    @pytest.mark.parametrize("surface", _SURFACES)
    @pytest.mark.parametrize(
        "outcomes",
        [
            pytest.param({"scanbox.lan": _REJECTED}, id="one-host"),
            pytest.param(
                {"scanbox-a.lan": _HEALTHY, "scanbox-b.lan": _REJECTED},
                id="two-hosts",
            ),
        ],
    )
    @pytest.mark.parametrize(
        "devices",
        [pytest.param([], id="nothing-usable"), pytest.param([_device()], id="usable")],
    )
    def test_no_rejected_row_says_switched_on_and_connected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        outcomes: dict[str, checks._SanedOutcome],
        devices: list[DeviceInfo],
        surface: str,
    ) -> None:
        """
        A host refusing this machine is never blamed on the scanner's power.

        "Switched on and connected" was the advice the 2026-09-22 denial got,
        and it sent the reader to the one part of the setup that was fine.
        Every combination here must reach the refusal wording, so the property
        cannot pass by never meeting the case it is about.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            outcomes: What each configured host answers.
            devices: What the backend lists.
            surface: Which of the two surfaces runs the check.

        """
        _recording_dialler(monkeypatch, outcomes=outcomes)
        settings = _with_device(_settings(tmp_path, host=":".join(outcomes)), "")
        row = _scanner_row_on(
            surface, _context(settings, scanner=_CountingBackend(devices))
        )
        assert "refusing this machine" in row.message
        assert "saned.conf" in row.next_step
        for text in (row.message, row.next_step):
            assert "switched on and connected" not in text


# Matches a row that talks about a *scan*, and deliberately not one that talks
# about the *scanner*: "scanner" is the row's own name and every scanner
# message is entitled to say it.  Word boundaries are what make the difference,
# so a plain substring test would not do.
_NAMES_A_SCAN = re.compile(r"\bscan(s|ning)?\b", re.IGNORECASE)

_SCAN_RUNNING_MESSAGE = "Not checked while a scan is running."


class TestRunChecksUnderTheScannerGate:
    """WR-03: the gate is held around the scanner check and around nothing else."""

    def test_an_ungated_run_still_produces_every_row(self, tmp_path: Path) -> None:
        """
        ``doctor`` passes no gate, and nothing about its run changes.

        Args:
            tmp_path: The test's own directory.

        """
        results = run_checks(
            _context(_settings(tmp_path), scanner=_CountingBackend([_device()]))
        )
        assert [result.key for result in results] == list(CheckKey)
        assert _row(results, CheckKey.SCANNER).skipped is False

    @pytest.mark.parametrize("scanner_context", list(_GATED_CONTEXTS))
    def test_a_gated_run_returns_what_an_ungated_run_returns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scanner_context: str
    ) -> None:
        """
        Handing in a free gate changes the rows not at all, in any context.

        ``_scanner_result``'s docstring names this test as the replacement for
        the uniform-dispatch seam the gate split removed: the gated path no
        longer routes through ``_dispatch``, so nothing mechanical keeps it and
        ``saneless doctor``'s ungated ``_check_scanner`` in step -- this does.

        Until R3-IN-02 it ran exactly one scenario, and it was the scenario
        where the gate is irrelevant (R3-IN-01): a free gate, a backend that
        answers, and no configured host.  Every context whose *row* differs was
        untested, so a gated path that diverged on the no-python-sane row, on a
        host that does not answer the pre-probe, or on an enumeration that
        raises would have passed.  Every context in ``_GATED_CONTEXTS`` runs
        here, including the refused, rejected, timed-out, unresolved and
        wrong-device ones, and all of
        them are reached without resolving a name or opening a socket -- the
        dialler is stubbed in every variant, including the one that never
        reaches it.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Used to stub the probe seam.
            scanner_context: Which of the scanner contexts to run.

        """
        build = _gated_context_builder(tmp_path, monkeypatch, scanner_context)
        ungated = run_checks(build())
        gated = run_checks(
            build(), scanner_gate=cast("threading.Lock", _RecordingLock())
        )
        assert [result.key for result in gated] == list(CheckKey)
        assert gated == ungated

    def test_every_gated_context_reaches_a_different_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        No two parity contexts end in the same Scanner row.

        The parity test is only as good as the branches its contexts reach.
        Two contexts that produced the same row would be one branch tested
        twice and another not at all, and the parity test could not tell.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Used to stub the probe seam.

        """
        rows: dict[str, tuple[CheckState, str]] = {}
        for scanner_context in _GATED_CONTEXTS:
            build = _gated_context_builder(tmp_path, monkeypatch, scanner_context)
            row = _row(run_checks(build()), CheckKey.SCANNER)
            rows[scanner_context] = (row.state, row.message)
        assert len(set(rows.values())) == len(_GATED_CONTEXTS), rows

    def test_the_gate_is_held_while_the_scanner_check_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Entering libsane is exactly what the gate exists to make exclusive.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Used to make the directory writes sample the gate.

        """
        gate = _RecordingLock()
        probe = _GateProbe(gate)
        context, client, _backend = _gate_sampling_context(tmp_path, probe, monkeypatch)
        try:
            run_checks(context, scanner_gate=cast("threading.Lock", gate))
        finally:
            client.close()
        assert probe.free_during["scanner"] is False

    def test_the_gate_is_free_while_the_paperless_check_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        WR-03: a multi-second HTTP budget must not park a scan start.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Used to make the directory writes sample the gate.

        """
        gate = _RecordingLock()
        probe = _GateProbe(gate)
        context, client, _backend = _gate_sampling_context(tmp_path, probe, monkeypatch)
        try:
            run_checks(context, scanner_gate=cast("threading.Lock", gate))
        finally:
            client.close()
        assert probe.free_during["paperless"] is True

    def test_the_gate_is_free_while_the_directory_writes_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Creating and deleting two real files touches no scanner.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Used to make the directory writes sample the gate.

        """
        gate = _RecordingLock()
        probe = _GateProbe(gate)
        context, client, _backend = _gate_sampling_context(tmp_path, probe, monkeypatch)
        try:
            run_checks(context, scanner_gate=cast("threading.Lock", gate))
        finally:
            client.close()
        assert probe.free_during["directories"] is True

    def test_the_gate_is_free_once_the_run_returns(self, tmp_path: Path) -> None:
        """
        Every acquire is matched by a release before the tuple comes back.

        Args:
            tmp_path: The test's own directory.

        """
        gate = _RecordingLock()
        run_checks(
            _context(_settings(tmp_path), scanner=_CountingBackend([_device()])),
            scanner_gate=cast("threading.Lock", gate),
        )
        assert gate.acquires == 1
        assert gate.releases == 1
        assert gate.is_free() is True

    def test_a_raising_scanner_check_still_releases_the_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A wedged check must never leave the worker locked out of its scanner.

        What is broken here is the enumeration, because that is the region the
        gate is held around: the pre-probe now runs before the acquire, so a
        stand-in for the whole check would never reach the gate to leave it
        held.  A scanner is supplied for the same reason -- without one the
        pre-probe settles the row and the gate is never taken.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Used to break the enumeration on purpose.

        """

        def boom(
            _scanner: ScannerBackend, _configured_device: str, *, may_open: bool
        ) -> checks._Enumeration:
            del may_open
            msg = "the scanner check exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(checks, "_scanner_enumeration", boom)
        gate = _RecordingLock()
        results = run_checks(
            _context(_settings(tmp_path), scanner=_CountingBackend([_device()])),
            scanner_gate=cast("threading.Lock", gate),
        )
        assert _row(results, CheckKey.SCANNER).state is CheckState.FAIL
        assert gate.releases == 1
        assert gate.is_free() is True

    def test_a_gate_held_elsewhere_skips_the_scanner_without_entering_sane(
        self, tmp_path: Path
    ) -> None:
        """
        A failed non-blocking attempt is a skipped row, never a second caller.

        Args:
            tmp_path: The test's own directory.

        """
        gate = _RecordingLock()
        backend = _CountingBackend([_device()])
        assert gate.lock.acquire(blocking=False) is True
        try:
            results = run_checks(
                _context(_settings(tmp_path), scanner=backend),
                scanner_gate=cast("threading.Lock", gate),
            )
        finally:
            gate.lock.release()
        assert _row(results, CheckKey.SCANNER).skipped is True
        assert backend.calls == 0
        assert gate.releases == 0

    def test_the_gate_is_free_while_the_saned_pre_probe_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        R2-IN-03: name resolution must not be able to park a scan start.

        The pre-probe is not SANE work -- it is a DNS lookup and a TCP
        handshake -- and its resolution step is outside every budget this
        module states, because ``getaddrinfo`` takes no timeout.  Holding the
        scanner gate across it means ``ScanWorker._scan_job`` can sit in
        ``with self._scanner_gate:`` with the job row already written
        ``SCANNING`` for as long as a broken resolver takes.

        A host is configured on purpose: with none, ``_saned_hosts`` yields
        nothing, the pre-probe never runs, and the case would pass vacuously
        while proving nothing.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Used to make the pre-probe sample the gate.

        """
        gate = _RecordingLock()
        probe = _GateProbe(gate)
        dialled = _gate_sampling_dialler(
            monkeypatch, probe, outcome=checks._SanedOutcome.HEALTHY
        )
        backend = _GateSamplingBackend(probe, [_device()])
        results = run_checks(
            _context(_settings(tmp_path, host=_DEVICE_HOST), scanner=backend),
            scanner_gate=cast("threading.Lock", gate),
        )
        assert dialled == [(_DEVICE_HOST, SANED_PORT)]
        assert probe.free_during["pre-probe"] is True
        assert probe.free_during["scanner"] is False
        assert _row(results, CheckKey.SCANNER).state is CheckState.OK
        assert gate.is_free() is True

    def test_the_configured_device_is_opened_under_the_same_gate_hold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Opening an unlisted configured device is SANE work, so the gate covers it.

        The open follows the listing inside one hold: one acquire, one
        release, the gate held during both, and free during the pre-probe and
        once the run returns.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Used to make the pre-probe sample the gate.

        """
        gate = _RecordingLock()
        probe = _GateProbe(gate)
        _gate_sampling_dialler(monkeypatch, probe, outcome=checks._SanedOutcome.HEALTHY)
        backend = _GateSamplingOpenBackend(probe, [_device()])
        settings = _with_device(_settings(tmp_path, host="scanbox"), _LOCAL_DEVICE_ID)
        results = run_checks(
            _context(settings, scanner=backend),
            scanner_gate=cast("threading.Lock", gate),
        )
        assert backend.opens == 1
        assert probe.free_during["pre-probe"] is True
        assert probe.free_during["scanner"] is False
        assert probe.free_during["open"] is False
        assert gate.acquires == 1
        assert gate.releases == 1
        assert gate.is_free() is True
        assert _row(results, CheckKey.SCANNER).message == (
            "The configured scanner is ready."
        )

    def test_a_timed_out_pre_probe_never_touches_the_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A check that decides before SANE has no business taking SANE's lock.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Used to make every configured host fail its probe.

        """
        gate = _RecordingLock()
        backend = _CountingBackend([_device()])
        dialled = _recording_dialler(
            monkeypatch, outcome=checks._SanedOutcome.TIMED_OUT
        )
        results = run_checks(
            _context(_settings(tmp_path, host=_DEVICE_HOST), scanner=backend),
            scanner_gate=cast("threading.Lock", gate),
        )
        assert dialled == [(_DEVICE_HOST, SANED_PORT)]
        assert gate.acquires == 0
        assert backend.calls == 0
        row = _row(results, CheckKey.SCANNER)
        assert row.state is CheckState.WARN
        assert row.message == _TIMED_OUT_MESSAGE

    def test_a_refused_pre_probe_takes_the_gate_for_enumeration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A refused host is enumerated, and enumeration is SANE work under the gate.

        One acquire and one release, and the gate free once the run returns.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Used to make every configured host refuse its probe.

        """
        gate = _RecordingLock()
        backend = _CountingBackend([_device()])
        dialled = _recording_dialler(monkeypatch, outcome=checks._SanedOutcome.REFUSED)
        results = run_checks(
            _context(_settings(tmp_path, host=_DEVICE_HOST), scanner=backend),
            scanner_gate=cast("threading.Lock", gate),
        )
        assert dialled == [(_DEVICE_HOST, SANED_PORT)]
        assert gate.acquires == 1
        assert gate.releases == 1
        assert gate.is_free() is True
        assert backend.calls == 1
        row = _row(results, CheckKey.SCANNER)
        assert row.state is CheckState.WARN
        assert row.message == _REFUSED_READY_MESSAGE

    def test_an_absent_backend_never_touches_the_gate(self, tmp_path: Path) -> None:
        """
        A machine with no python-sane is its own row, decided before the lock.

        Args:
            tmp_path: The test's own directory.

        """
        gate = _RecordingLock()
        results = run_checks(
            _context(_settings(tmp_path)),
            scanner_gate=cast("threading.Lock", gate),
        )
        row = _row(results, CheckKey.SCANNER)
        assert row.state is CheckState.FAIL
        assert row.message == "Scanner support is not installed on this machine."
        assert gate.acquires == 0

    def test_a_gate_held_elsewhere_does_not_claim_a_scan_is_running(
        self, tmp_path: Path
    ) -> None:
        """
        R2-WR-02: losing the gate is not a scan, and the row must not say it is.

        ``run_checks`` honours ``skip_scanner`` *before* it looks at the gate,
        so by the time a non-blocking acquire fails a running scan has already
        been excluded.  What holds the gate in that window today is
        ``ScanWorker._read_generated_profiles``, the worker thread's first act
        at startup, taken while ``_current_job_id`` is still ``None`` -- which
        is exactly when the cold-start poll probes.

        The row is asserted, not the constructor that built it: the sentence a
        household member reads is the contract.

        Args:
            tmp_path: The test's own directory.

        """
        gate = _RecordingLock()
        backend = _CountingBackend([_device()])
        assert gate.lock.acquire(blocking=False) is True
        try:
            results = run_checks(
                _context(_settings(tmp_path), scanner=backend),
                scanner_gate=cast("threading.Lock", gate),
            )
        finally:
            gate.lock.release()
        row = _row(results, CheckKey.SCANNER)
        assert _NAMES_A_SCAN.search(row.message) is None, row.message
        assert row.message != _SCAN_RUNNING_MESSAGE
        assert row.state is CheckState.OK
        assert row.skipped is True
        assert row.next_step == ""
        assert backend.calls == 0

    def test_the_skip_scanner_row_still_names_the_running_scan(
        self, tmp_path: Path
    ) -> None:
        """
        D-08's sentence is unchanged, and it stays the only row that says it.

        Its companion above proves the two branches are distinguishable by
        message alone, which is what lets either surface be trusted.

        Args:
            tmp_path: The test's own directory.

        """
        gate = _RecordingLock()
        results = run_checks(
            _context(
                _settings(tmp_path),
                scanner=_CountingBackend([_device()]),
                skip_scanner=True,
            ),
            scanner_gate=cast("threading.Lock", gate),
        )
        row = _row(results, CheckKey.SCANNER)
        assert row.message == _SCAN_RUNNING_MESSAGE
        assert row.state is CheckState.OK
        assert row.skipped is True
        assert gate.acquires == 0

    def test_skip_scanner_never_touches_the_gate(self, tmp_path: Path) -> None:
        """
        A caller that already knows a scan is running has no reason to probe.

        Args:
            tmp_path: The test's own directory.

        """
        gate = _RecordingLock()
        backend = _CountingBackend([_device()])
        results = run_checks(
            _context(_settings(tmp_path), scanner=backend, skip_scanner=True),
            scanner_gate=cast("threading.Lock", gate),
        )
        assert _row(results, CheckKey.SCANNER).skipped is True
        assert backend.calls == 0
        assert gate.acquires == 0

    def test_a_gated_run_keeps_member_order(self, tmp_path: Path) -> None:
        """
        The gate does not reorder or drop a row, whether taken or not.

        Args:
            tmp_path: The test's own directory.

        """
        gate = _RecordingLock()
        assert gate.lock.acquire(blocking=False) is True
        try:
            results = run_checks(
                _context(_settings(tmp_path)),
                scanner_gate=cast("threading.Lock", gate),
            )
        finally:
            gate.lock.release()
        assert [result.key for result in results] == list(CheckKey)


# Stand-in listing children for the tests below.  The launcher runs them in
# isolated mode, so each uses the standard library only.  The environment
# variables they read have no ``SANELESS_`` prefix, so they survive the child
# environment's strip.

# Records that it ran, then dies the way a libsane crash kills a listing.
_CRASHING_LISTING_CHILD = """\
import os
import resource
import signal
from pathlib import Path

with Path(os.environ["LISTING_TEST_RUNS"]).open("a") as runs:
    runs.write("ran\\n")
# No core file: this crash is deliberate.
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.kill(os.getpid(), signal.SIGSEGV)
"""

# Writes its PID, then waits for a signal that only the launcher's kill sends.
_SLEEPING_LISTING_CHILD = """\
import os
import signal
from pathlib import Path

Path(os.environ["LISTING_TEST_PIDFILE"]).write_text(str(os.getpid()))
signal.pause()
"""

# Lists nothing, and reports the configured eSCL device opened only when the
# request asked it to open exactly that id.
_OPENING_LISTING_CHILD = """\
import json
import sys

request = json.loads(sys.stdin.readline())
opened = request.get("open") == "escl:http://10.0.0.5:80"
sys.stdout.write(json.dumps({"devices": [], "opened": opened}) + "\\n")
"""

# How long a stand-in child gets to start and write its PID file.
_CHILD_START_BUDGET: Final = 10.0

# How long the check thread gets to finish once its child is under way.
_CHECK_THREAD_BUDGET: Final = 15.0


class _OpenCountingSaneModule(FakeSaneModule):
    """The fake python-sane module, recording every device this process opens."""

    def __init__(self, device: FakeSaneDev) -> None:
        """
        List one fake device and open handles to ``device``.

        Args:
            device: The device every handle ``open()`` returns shares.

        """
        super().__init__(
            device=device, devices=[("fake:0", "Fake", "Zero", "flatbed scanner")]
        )
        self.opened: list[str] = []

    def open(self, device_id: str) -> FakeSaneHandle:
        """
        Record the open, then open the way the plain fake does.

        Args:
            device_id: The device being opened.

        Returns:
            A new handle to the device.

        """
        self.opened.append(device_id)
        return super().open(device_id)


class _ReapCheckingLock(_RecordingLock):
    """A scanner gate that checks, as it is released, the child is already reaped."""

    def __init__(self, pidfile: Path) -> None:
        """
        Read the child's PID from ``pidfile`` at each release.

        Args:
            pidfile: Where the stand-in child writes its PID.

        """
        super().__init__()
        self.pidfile = pidfile
        self.reaped_at_release: list[bool] = []

    def release(self) -> None:
        """
        Record whether the child is reaped, then hand the lock back.

        ``waitpid`` raising ``ChildProcessError`` means this process has no
        unreaped child with that PID: not a running one, and not a zombie.
        The lock is handed back whatever the check finds.
        """
        try:
            pid = int(self.pidfile.read_text())
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                self.reaped_at_release.append(True)
            else:
                self.reaped_at_release.append(False)
        finally:
            super().release()


@dataclass(frozen=True)
class _IsolatedCallCase:
    """
    One configuration of the check's single list-then-open, and its outcome.

    Attributes:
        device: The configured ``scanner.device``.
        survey: What the backend answers.
        asked: The id the call must be asked to open.
        message: The row the survey must give.

    """

    device: str
    survey: DeviceSurvey
    asked: str
    message: str


class TestIsolatedListingWiring:
    """
    The Scanner check reaches SANE only through the backend's isolated listing.

    One ``list_and_open`` call per check lists the devices and, when the
    configured one is not listed, opens it, both in a short-lived child.  A
    crashed child and a child stopped at its deadline each get their own row,
    the scanner gate is held until the child has been reaped, and this process
    never lists or opens anything itself.
    """

    @pytest.fixture
    def isolated_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Callable[[str], tuple[CheckContext, _OpenCountingSaneModule, FakeSaneDev]]:
        """
        Build contexts around a real backend over the fake python-sane module.

        The settings name no scanner host, so no pre-probe runs, and the
        backend is a real ``SaneBackend``, so its listing goes through the
        real launcher to whatever stand-in child the test chose.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Patches the fake into the backend module.

        Returns:
            A function taking the configured device id and returning the
            context, the fake module and the fake device handle.

        """

        def build(
            device: str,
        ) -> tuple[CheckContext, _OpenCountingSaneModule, FakeSaneDev]:
            handle = FakeSaneDev()
            fake = _OpenCountingSaneModule(handle)
            monkeypatch.setattr(sane_backend_mod, "sane", fake)
            context = CheckContext(
                settings=_with_device(_settings(tmp_path), device),
                scanner=SaneBackend(),
                paperless=None,
                profile_storage=ProfileStorage.PERSISTED,
            )
            return context, fake, handle

        return build

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(
                _IsolatedCallCase(
                    device="",
                    survey=DeviceSurvey(devices=(_device(),)),
                    asked="",
                    message="Brother ADS-2700W is ready.",
                ),
                id="no-configured-device",
            ),
            pytest.param(
                _IsolatedCallCase(
                    device=_LOCAL_DEVICE_ID,
                    survey=DeviceSurvey(devices=(), configured_opened=True),
                    asked=_LOCAL_DEVICE_ID,
                    message="The configured scanner is ready.",
                ),
                id="configured-and-openable",
            ),
            pytest.param(
                _IsolatedCallCase(
                    device=_UNPROBED_NET_ID,
                    survey=DeviceSurvey(devices=(_device(),)),
                    asked="",
                    message=_UNPROBED_DEVICE_MESSAGE,
                ),
                id="net-device-on-an-unprobed-host",
            ),
        ],
    )
    @pytest.mark.parametrize("surface", _SURFACES)
    def test_the_check_makes_one_isolated_call(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        case: _IsolatedCallCase,
        surface: str,
    ) -> None:
        """
        One ``list_and_open`` per check, asked to open only what it may.

        The configured id goes to the call only when the check may open it.
        A ``net:`` device whose host could not be probed goes as ``""``, so
        the child lists but opens nothing.  The check never lists or opens
        through the backend's other methods.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            case: The configuration, the answer and what must follow.
            surface: Which of the two surfaces runs the check.

        """
        _recording_dialler(monkeypatch)
        backend = _SurveyRecordingBackend(case.survey)
        settings = _with_device(_settings(tmp_path), case.device)
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert backend.asked == [case.asked]
        assert backend.calls == 0
        assert backend.opens == 0
        assert row.message == case.message

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            pytest.param(
                ListingCrashedError(_CRASH_TEXT),
                (CheckState.WARN, _LISTING_CRASHED_MESSAGE, _LISTING_CRASHED_NEXT),
                id="crashed",
            ),
            pytest.param(
                ListingTimedOutError(_TIMEOUT_TEXT),
                (CheckState.WARN, _LISTING_TIMED_OUT_MESSAGE, _LISTING_TIMED_OUT_NEXT),
                id="timed-out",
            ),
            pytest.param(
                ListingNoAnswerError(_NO_ANSWER_TEXT),
                (CheckState.WARN, _LISTING_NO_ANSWER_MESSAGE, _LISTING_NO_ANSWER_NEXT),
                id="no-answer",
            ),
            pytest.param(
                ScanError("SANE is wedged"),
                (CheckState.FAIL, "No scanner was found.", _NOTHING_FOUND_NEXT),
                id="plain-failure",
            ),
        ],
    )
    @pytest.mark.parametrize("surface", _SURFACES)
    def test_a_failed_listing_gets_the_row_for_how_it_failed(
        self,
        tmp_path: Path,
        error: ScanError,
        expected: tuple[CheckState, str, str],
        surface: str,
    ) -> None:
        """
        A crash, a timeout and no answer have their own rows; else nothing listed.

        Args:
            tmp_path: The test's own directory.
            error: What the backend's list-then-open raises.
            expected: The row's state, message and next step.
            surface: Which of the two surfaces runs the check.

        """
        backend = _ListingFailureBackend(error)
        settings = _with_device(_settings(tmp_path), "")
        row = _scanner_row_on(surface, _context(settings, scanner=backend))
        assert (row.state, row.message, _strip(row.next_step)) == expected
        assert backend.calls == 0
        assert backend.opens == 0

    def test_a_crashed_listing_child_is_the_crash_row_and_is_not_retried(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        stand_in_listing_child: Callable[[str], Path],
        isolated_context: Callable[
            [str], tuple[CheckContext, _OpenCountingSaneModule, FakeSaneDev]
        ],
    ) -> None:
        """
        A child killed by SIGSEGV is one amber row, one WARNING and one run.

        The WARNING is the launcher's, naming the signal.  The check adds no
        line of its own, and does not try the listing again.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Sets the stand-in's run-counter variable.
            caplog: pytest's log capture.
            stand_in_listing_child: Points the real launcher at a stand-in.
            isolated_context: Builds a context around a real backend.

        """
        runs = tmp_path / "runs.txt"
        monkeypatch.setenv("LISTING_TEST_RUNS", str(runs))
        stand_in_listing_child(_CRASHING_LISTING_CHILD)
        context, fake, _handle = isolated_context("")
        with caplog.at_level(logging.WARNING):
            results = run_checks(context, scanner_gate=threading.Lock())
        row = _row(results, CheckKey.SCANNER)
        assert (row.state, row.message, _strip(row.next_step)) == (
            CheckState.WARN,
            _LISTING_CRASHED_MESSAGE,
            _LISTING_CRASHED_NEXT,
        )
        crash_lines = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING and "SIGSEGV" in record.getMessage()
        ]
        assert len(crash_lines) == 1
        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == "saneless.checks" and record.levelno >= logging.WARNING
        ] == []
        assert runs.read_text().splitlines() == ["ran"]
        assert fake.get_devices_call_count == 0

    def test_a_listing_child_with_no_reply_is_the_no_answer_row(
        self,
        real_listing_launcher: None,
        isolated_context: Callable[
            [str], tuple[CheckContext, _OpenCountingSaneModule, FakeSaneDev]
        ],
    ) -> None:
        """
        A child that exits without a reply is amber, not a missing scanner.

        The configured device is not listed, because nothing was listed, and
        it was not opened.  That is still the check not seeing, so the row
        must not send the operator to the hardware.

        Args:
            real_listing_launcher: Runs a stand-in child that exits at once.
            isolated_context: Builds a context around a real backend.

        """
        _ = real_listing_launcher  # the real launcher runs the stand-in
        context, fake, _handle = isolated_context(_LOCAL_DEVICE_ID)
        results = run_checks(context, scanner_gate=threading.Lock())
        row = _row(results, CheckKey.SCANNER)
        assert (row.state, row.message, _strip(row.next_step)) == (
            CheckState.WARN,
            _LISTING_NO_ANSWER_MESSAGE,
            _LISTING_NO_ANSWER_NEXT,
        )
        assert fake.get_devices_call_count == 0

    def test_a_listing_child_past_its_deadline_is_reaped_before_the_gate_opens(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stand_in_listing_child: Callable[[str], Path],
        isolated_context: Callable[
            [str], tuple[CheckContext, _OpenCountingSaneModule, FakeSaneDev]
        ],
    ) -> None:
        """
        A child stopped at the deadline is the timeout row, released after the reap.

        The gate records, at the moment it is handed back, whether the child
        still exists in any form.  It must not: a gate released while the
        child was still inside libsane would let a scan in beside it.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Shortens the deadline and sets the PID-file variable.
            stand_in_listing_child: Points the real launcher at a stand-in.
            isolated_context: Builds a context around a real backend.

        """
        pidfile = tmp_path / "child.pid"
        monkeypatch.setenv("LISTING_TEST_PIDFILE", str(pidfile))
        monkeypatch.setattr(listing, "LISTING_DEADLINE_SECONDS", 0.2)
        stand_in_listing_child(_SLEEPING_LISTING_CHILD)
        context, fake, _handle = isolated_context("")
        gate = _ReapCheckingLock(pidfile)
        row = _row(
            run_checks(context, scanner_gate=cast("threading.Lock", gate)),
            CheckKey.SCANNER,
        )
        assert (row.state, row.message, _strip(row.next_step)) == (
            CheckState.WARN,
            _LISTING_TIMED_OUT_MESSAGE,
            _LISTING_TIMED_OUT_NEXT,
        )
        assert gate.reaped_at_release == [True]
        assert gate.is_free()
        assert fake.get_devices_call_count == 0

    def test_the_gate_is_held_while_the_listing_child_runs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stand_in_listing_child: Callable[[str], Path],
        isolated_context: Callable[
            [str], tuple[CheckContext, _OpenCountingSaneModule, FakeSaneDev]
        ],
    ) -> None:
        """
        While the child is alive the gate is held; once the check ends it is free.

        The check runs on its own thread, the way the refresh route's request
        thread runs it, and the test watches the gate from outside while the
        stand-in child waits to be stopped.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Sets the deadline and the PID-file variable.
            stand_in_listing_child: Points the real launcher at a stand-in.
            isolated_context: Builds a context around a real backend.

        """
        pidfile = tmp_path / "child.pid"
        monkeypatch.setenv("LISTING_TEST_PIDFILE", str(pidfile))
        monkeypatch.setattr(listing, "LISTING_DEADLINE_SECONDS", 2.0)
        stand_in_listing_child(_SLEEPING_LISTING_CHILD)
        context, _fake, _handle = isolated_context("")
        gate = _RecordingLock()
        rows: list[CheckResult] = []

        def check() -> None:
            results = run_checks(context, scanner_gate=cast("threading.Lock", gate))
            rows.append(_row(results, CheckKey.SCANNER))

        worker = threading.Thread(target=check, daemon=True)
        worker.start()
        try:
            assert poll_until(pidfile.exists, _CHILD_START_BUDGET)
            assert gate.is_free() is False
        finally:
            worker.join(_CHECK_THREAD_BUDGET)
        assert not worker.is_alive()
        assert [row.message for row in rows] == [_LISTING_TIMED_OUT_MESSAGE]
        assert gate.acquires == gate.releases == 1
        assert gate.is_free()

    def test_a_gated_check_never_enters_libsane_in_this_process(
        self,
        stand_in_listing_child: Callable[[str], Path],
        isolated_context: Callable[
            [str], tuple[CheckContext, _OpenCountingSaneModule, FakeSaneDev]
        ],
    ) -> None:
        """
        An unlisted configured device is opened in the child, not here.

        This is the gated check the refresh route's request thread runs.  The
        fake module would list its own device and open anything, so the row
        coming from the stand-in's answer, with the fake never listing and
        nothing opened, is what shows the check left this process for both.

        Args:
            stand_in_listing_child: Points the real launcher at a stand-in.
            isolated_context: Builds a context around a real backend.

        """
        stand_in_listing_child(_OPENING_LISTING_CHILD)
        context, fake, handle = isolated_context(_UNLISTED_ESCL_ID)
        results = run_checks(context, scanner_gate=threading.Lock())
        row = _row(results, CheckKey.SCANNER)
        assert (row.state, row.message) == (
            CheckState.OK,
            "The configured scanner is ready.",
        )
        assert fake.get_devices_call_count == 0
        assert fake.opened == []
        assert handle.calls == []
        assert handle.cancel_calls == 0
        assert handle.close_calls == 0


class TestConfigurationRow:
    """
    The row that names the cause the other five could only hint at (Phase 37).

    Phase 37 D-03 gives the configuration file its own row rather than weaving
    a sentence into three others.  D-05 is the contract: four states, one of
    them green, and exactly two of them allowed to name a file.  Every message
    and next step here is asserted in full, because 37-03's terminal surfaces
    reuse these sentences verbatim and a paraphrase in one place would be a
    second wording source.
    """

    def test_the_row_is_named_configuration(self) -> None:
        """The name column a household member reads, and its column width."""
        assert check_name(CheckKey.CONFIGURATION) == "Configuration"

    def test_a_loaded_file_is_green_and_names_nothing(self, tmp_path: Path) -> None:
        """
        Phase 37 D-05, first case, and D-06: green shows no path.

        A path in the healthy row is noise to the reader the strip is for, and
        it is a host filesystem path on a page anyone on the LAN can load.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_discovery(_settings(tmp_path), _discovery(tmp_path, loaded=0))
        row = _row(run_checks(_context(settings)), CheckKey.CONFIGURATION)
        assert row.state is CheckState.OK
        assert row.message == "Config file loaded."
        assert row.next_step == ""

    def test_a_leftover_beside_a_loaded_file_is_amber(self, tmp_path: Path) -> None:
        """
        Phase 37 D-05 second case and D-10: the right file loaded, so nothing failed.

        It is still worth a row: the leftover is a trap for the next person to
        edit, who has no way to tell from the filesystem which file is live.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_discovery(
            _settings(tmp_path), _discovery(tmp_path, loaded=0, stale=(0,))
        )
        row = _row(run_checks(_context(settings)), CheckKey.CONFIGURATION)
        assert row.state is CheckState.WARN
        assert (
            row.message == "Using saneless.toml; an old config.toml is being ignored."
        )
        assert row.next_step == (
            f"Move anything you still need from ./{LEGACY_CONFIG_FILENAME} into the "
            f"{CONFIG_FILENAME} in use, then delete ./{LEGACY_CONFIG_FILENAME} "
            "and restart saneless."
        )

    def test_no_config_file_at_all_is_amber_and_points_at_the_log(
        self, tmp_path: Path
    ) -> None:
        """
        Phase 37 D-05 third case, D-06 and D-07: amber, and no path on the strip.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_discovery(_settings(tmp_path), _discovery(tmp_path))
        row = _row(run_checks(_context(settings)), CheckKey.CONFIGURATION)
        assert row.state is CheckState.WARN
        assert row.message == (
            "No config file; running on defaults and environment variables."
        )
        assert row.next_step == (
            f"The saneless log lists every place it looked for {CONFIG_FILENAME}."
        )
        assert "/" not in row.next_step

    def test_a_lone_superseded_file_is_red_and_names_the_rename(
        self, tmp_path: Path
    ) -> None:
        """
        Phase 37 D-05 fourth case: nothing the operator wrote was read.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_discovery(
            _settings(tmp_path), _discovery(tmp_path, stale=(0,))
        )
        row = _row(run_checks(_context(settings)), CheckKey.CONFIGURATION)
        assert row.state is CheckState.FAIL
        assert row.message == (
            "No config file loaded: saneless now reads saneless.toml, not config.toml."
        )
        assert row.next_step == (
            f"Rename ./{LEGACY_CONFIG_FILENAME} to {CONFIG_FILENAME}, "
            "then restart saneless."
        )

    def test_a_legacy_name_under_etc_is_named_above_the_paperless_row(
        self, tmp_path: Path
    ) -> None:
        """
        The failure this phase exists to remove, checked against its own wording.

        A container mounted its configuration into the third searched place
        under the superseded name.  Four rows went red or amber that evening
        and not one named the cause.  This asserts the row that now does, and
        that it is read before the Paperless row it explains.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_discovery(
            _settings(tmp_path, token="changeme"), _discovery(tmp_path, stale=(2,))
        )
        results = run_checks(_context(settings))
        row = _row(results, CheckKey.CONFIGURATION)
        assert row.state is CheckState.FAIL
        assert row.next_step == (
            f"Rename /etc/saneless/{LEGACY_CONFIG_FILENAME} to {CONFIG_FILENAME}, "
            "then restart saneless."
        )
        keys = [result.key for result in results]
        assert keys.index(CheckKey.CONFIGURATION) < keys.index(CheckKey.PAPERLESS)

    def test_the_docker_leftover_says_move_before_it_says_delete(
        self, tmp_path: Path
    ) -> None:
        """
        Phase 37 D-17: the leftover may hold the only copy of the URL and token.

        In the documented Docker layout ``auto-profiles`` writes the working
        directory's file, so after an upgrade the leftover in the third
        searched place is the one the operator actually filled in.  A bare
        "delete it" would be advice to destroy the configuration.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_discovery(
            _settings(tmp_path), _discovery(tmp_path, loaded=0, stale=(2,))
        )
        row = _row(run_checks(_context(settings)), CheckKey.CONFIGURATION)
        assert row.state is CheckState.WARN
        assert f"/etc/saneless/{LEGACY_CONFIG_FILENAME}" in row.next_step
        assert "Move anything you still need" in row.next_step
        assert row.next_step.index("then delete") > row.next_step.index("Move")

    def test_more_than_one_leftover_names_the_highest_priority_one(
        self, tmp_path: Path
    ) -> None:
        """
        One file is named, and it is the one whose rename would load next start.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_discovery(
            _settings(tmp_path), _discovery(tmp_path, stale=(1, 2))
        )
        row = _row(run_checks(_context(settings)), CheckKey.CONFIGURATION)
        assert f"$XDG_CONFIG_HOME/saneless/{LEGACY_CONFIG_FILENAME}" in row.next_step
        assert f"/etc/saneless/{LEGACY_CONFIG_FILENAME}" not in row.next_step

    def test_a_missing_file_is_amber_even_when_nothing_else_is_configured(
        self, tmp_path: Path
    ) -> None:
        """
        Phase 37 D-07: environment-only deployment is supported, so never red.

        And the Paperless row keeps its own verdict: saying whether paperless
        works is its job, and two rows reporting one fact is how a reader
        learns to discount both.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_discovery(
            _settings(tmp_path, token="changeme"), _discovery(tmp_path)
        )
        results = run_checks(_context(settings))
        assert _row(results, CheckKey.CONFIGURATION).state is CheckState.WARN
        assert _row(results, CheckKey.PAPERLESS).state is CheckState.FAIL

    def test_settings_that_ran_no_search_read_as_no_config_file(
        self, tmp_path: Path
    ) -> None:
        """
        A directly constructed Settings has searched nothing, and says so.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_discovery(_settings(tmp_path), None)
        assert settings.config_discovery is None
        row = _row(run_checks(_context(settings)), CheckKey.CONFIGURATION)
        assert row.state is CheckState.WARN
        assert row.message == (
            "No config file; running on defaults and environment variables."
        )

    def test_a_shadowed_file_is_amber_and_named(self, tmp_path: Path) -> None:
        """
        A second config file is amber, and the row says which one won.

        Two files can be deliberate -- a per-user file overriding the system
        one -- so nothing failed.  But the file an operator edits may be the
        one that is not read, and nothing else on the strip would say so.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_discovery(
            _settings(tmp_path), _discovery(tmp_path, loaded=0, also_found=(2,))
        )
        row = _row(run_checks(_context(settings)), CheckKey.CONFIGURATION)
        assert row.state is CheckState.WARN
        assert row.message == (
            f"Using ./{CONFIG_FILENAME}; /etc/saneless/{CONFIG_FILENAME} "
            "is also there and is not read."
        )
        assert row.next_step == (
            "If that is not deliberate, move anything you still need from "
            f"/etc/saneless/{CONFIG_FILENAME} into ./{CONFIG_FILENAME}, then "
            f"delete /etc/saneless/{CONFIG_FILENAME} and restart saneless."
        )
        assert str(tmp_path) not in row.message
        assert str(tmp_path) not in row.next_step

    def test_three_files_name_every_unread_one(self, tmp_path: Path) -> None:
        """
        Every file that is not read is named, and the sentence says so plainly.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_discovery(
            _settings(tmp_path), _discovery(tmp_path, loaded=0, also_found=(1, 2))
        )
        row = _row(run_checks(_context(settings)), CheckKey.CONFIGURATION)
        assert row.state is CheckState.WARN
        unread = (
            f"$XDG_CONFIG_HOME/saneless/{CONFIG_FILENAME} and "
            f"/etc/saneless/{CONFIG_FILENAME}"
        )
        assert row.message == (
            f"Using ./{CONFIG_FILENAME}; {unread} are also there and are not read."
        )
        assert row.next_step == (
            "If that is not deliberate, move anything you still need from "
            f"{unread} into ./{CONFIG_FILENAME}, then delete them and restart "
            "saneless."
        )
        assert str(tmp_path) not in row.message
        assert str(tmp_path) not in row.next_step

    def test_shadowed_row_absolute_spelling(self, tmp_path: Path) -> None:
        """
        The terminal form is the same sentences with both files absolute.

        Args:
            tmp_path: The test's own directory.

        """
        discovery = _discovery(tmp_path, loaded=0, also_found=(1,))
        settings = _with_discovery(_settings(tmp_path), discovery)
        used, unread = (str(path.absolute()) for path in discovery.found)
        row = checks.configuration_check(settings, absolute_paths=True)
        assert row.state is CheckState.WARN
        assert row.message == f"Using {used}; {unread} is also there and is not read."
        assert row.next_step == (
            "If that is not deliberate, move anything you still need from "
            f"{unread} into {used}, then delete {unread} and restart saneless."
        )

    def test_shadowed_takes_precedence_over_a_leftover(self, tmp_path: Path) -> None:
        """
        A shadowed file outranks an old-name leftover on the one row.

        The leftover is a trap for the next edit; the shadowed file may be
        the edit already made and not read.  The leftover is still named in
        full by the leftover row's own function.

        Args:
            tmp_path: The test's own directory.

        """
        settings = _with_discovery(
            _settings(tmp_path),
            _discovery(tmp_path, loaded=0, also_found=(2,), stale=(1,)),
        )
        row = _row(run_checks(_context(settings)), CheckKey.CONFIGURATION)
        assert row.state is CheckState.WARN
        assert row.message.endswith("is also there and is not read.")
        leftover = checks.leftover_config_check(settings)
        assert leftover is not None
        assert leftover.state is CheckState.WARN
        assert leftover.message == (
            "Using saneless.toml; an old config.toml is being ignored."
        )
        assert (
            f"$XDG_CONFIG_HOME/saneless/{LEGACY_CONFIG_FILENAME}" in leftover.next_step
        )

    def test_no_leftover_row_without_a_leftover(self, tmp_path: Path) -> None:
        """
        The leftover row is only for a loaded file with an old-name file beside it.

        Args:
            tmp_path: The test's own directory.

        """
        loaded_only = _with_discovery(
            _settings(tmp_path), _discovery(tmp_path, loaded=0, also_found=(2,))
        )
        assert checks.leftover_config_check(loaded_only) is None
        other = tmp_path / "other"
        other.mkdir()
        nothing_loaded = _with_discovery(
            _settings(other), _discovery(other, stale=(0,))
        )
        assert checks.leftover_config_check(nothing_loaded) is None

    def test_the_terminal_form_names_the_absolute_path(self, tmp_path: Path) -> None:
        """
        Phase 37 D-14: the no-path rule binds the LAN page, not the terminal.

        One function, one set of sentences, and the only difference is how the
        file is spelled -- so the log, ``doctor`` and the strip cannot come to
        describe the same appliance differently.

        Args:
            tmp_path: The test's own directory.

        """
        discovery = _discovery(tmp_path, stale=(2,))
        settings = _with_discovery(_settings(tmp_path), discovery)
        strip = _row(run_checks(_context(settings)), CheckKey.CONFIGURATION)
        terminal = checks.configuration_check(settings, absolute_paths=True)
        assert terminal.state is strip.state
        assert terminal.message == strip.message
        assert str(discovery.stale[0].absolute()) in terminal.next_step
        assert str(tmp_path) not in strip.next_step


class TestAbortedRun:
    """A run whose caller is stopping ends between checks and never raises."""

    @pytest.mark.parametrize("gated", [False, True], ids=["ungated", "gated"])
    def test_run_checks_stops_once_aborted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        *,
        gated: bool,
    ) -> None:
        """
        Nothing after an aborted scanner check runs, and nothing is a fault.

        The Paperless check would otherwise spend its whole budget after the
        listing was already stopped.  The run returns only the rows before
        the scanner, logs no warning, and hands the gate back.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: Replaces the Paperless check with a spy.
            caplog: The log capture.
            gated: Whether the run is handed a free scanner gate.

        """
        caplog.set_level(logging.INFO)
        paperless_calls: list[CheckContext] = []

        def paperless_spy(context: CheckContext) -> CheckResult:
            paperless_calls.append(context)
            return CheckResult(key=CheckKey.PAPERLESS, state=CheckState.OK, message="")

        monkeypatch.setattr(checks, "_check_paperless", paperless_spy)
        backend = _AbortedListingBackend()
        abort = threading.Event()
        context = replace(_context(_settings(tmp_path), scanner=backend), abort=abort)
        gate = threading.Lock()

        results = run_checks(context, scanner_gate=gate if gated else None)

        assert backend.aborts == [abort]
        assert [result.key for result in results] == [CheckKey.CONFIGURATION]
        assert paperless_calls == []
        assert not gate.locked()
        assert [
            record
            for record in caplog.records
            if record.name.startswith("saneless") and record.levelno >= logging.WARNING
        ] == []

    def test_run_checks_runs_nothing_once_already_aborted(self, tmp_path: Path) -> None:
        """
        A caller already stopping gets no rows and starts no listing.

        Args:
            tmp_path: The test's own directory.

        """
        backend = _AbortedListingBackend()
        abort = threading.Event()
        abort.set()
        context = replace(_context(_settings(tmp_path), scanner=backend), abort=abort)

        assert run_checks(context) == ()
        assert backend.aborts == []

    def test_an_aborted_listing_is_its_own_arm(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        An aborted listing propagates rather than reading as a failed one.

        Caught as any other exception it would be recorded as a listing that
        failed, logged as a warning and drawn as a scanner fault; it is the
        caller stopping, and the launcher has already logged it at INFO.
        """
        caplog.set_level(logging.INFO, logger="saneless.checks")
        backend = _AbortedListingBackend()
        abort = threading.Event()

        with pytest.raises(ListingAbortedError):
            checks._scanner_enumeration(backend, _DEVICE_ID, may_open=True, abort=abort)

        assert backend.aborts == [abort]
        assert [
            record for record in caplog.records if record.name == "saneless.checks"
        ] == []

    def test_a_context_has_no_abort_by_default(self, tmp_path: Path) -> None:
        """
        A caller that cannot be stopped part way, such as doctor, passes none.

        Args:
            tmp_path: The test's own directory.

        """
        assert _context(_settings(tmp_path)).abort is None


# Every check next step that ends in a retry, paired with exactly what the
# strip said before the retry ending became per-surface.  Each literal is
# written out, so the strip's wording cannot drift while the source moves.
_STRIP_WORDING: Final = (
    pytest.param(
        lambda: checks._CHECK_FAILED_NEXT_STEP,
        "Restart saneless, then press Check again.",
        id="check-failed",
    ),
    pytest.param(
        lambda: checks._host_problem_next_step(checks._SanedOutcome.TIMED_OUT),
        "Check the scanner host is switched on and on the network, "
        "then press Check again.",
        id="saned-timed-out",
    ),
    pytest.param(
        lambda: checks._host_problem_next_step(checks._SanedOutcome.REFUSED),
        "Start saned on the scanner host, or check it is listening on the "
        "network, then press Check again.",
        id="saned-refused",
    ),
    pytest.param(
        lambda: checks._host_problem_next_step(checks._SanedOutcome.UNRESOLVED),
        "Check the host name in [scanner] host or [scanner] device, or in "
        "SANE_NET_HOSTS if that is set. If you fixed the name in DNS or the "
        "hosts file, press Check again; if you changed a setting, restart "
        "saneless.",
        id="saned-unresolved",
    ),
    pytest.param(
        lambda: checks._host_problem_next_step(checks._SanedOutcome.REJECTED),
        "Add this machine to saned.conf on the scanner host, then press Check again.",
        id="saned-rejected",
    ),
    pytest.param(
        lambda: checks._scanner_configured_missing_row().next_step,
        "Check it is switched on and connected, then press Check again. If "
        "saneless devices does not list it, set [scanner] device to one it "
        "lists, then restart saneless.",
        id="configured-missing",
    ),
    pytest.param(
        lambda: checks._scanner_listing_crashed_row().next_step,
        "Press Check again.",
        id="listing-crashed",
    ),
    pytest.param(
        lambda: checks._scanner_listing_timed_out_row().next_step,
        "Check the scanner, and its scanner host if it has one, are switched "
        "on and reachable, then press Check again.",
        id="listing-timed-out",
    ),
    pytest.param(
        lambda: checks._scanner_listing_no_answer_row().next_step,
        "Press Check again.",
        id="listing-no-answer",
    ),
    pytest.param(
        lambda: checks._scanner_nothing_found_row(0).next_step,
        "Check the scanner is switched on and connected, then press Check again.",
        id="nothing-found",
    ),
    pytest.param(
        lambda: checks._scanner_nothing_found_row(1).next_step,
        "Check the scanner is switched on and connected to the scanner host, "
        "then press Check again.",
        id="host-answers-nothing-found",
    ),
    pytest.param(
        lambda: checks._paperless_next_step(ConnectionStatus.SERVER_ERROR),
        "Check paperless-ngx is healthy, then press Check again.",
        id="paperless-server-error",
    ),
    pytest.param(
        lambda: checks._paperless_next_step(ConnectionStatus.UNREACHABLE),
        "Check paperless-ngx is running and on the network, then press Check again.",
        id="paperless-unreachable",
    ),
    pytest.param(
        lambda: checks._paperless_next_step(ConnectionStatus.INCOMPATIBLE),
        "saneless needs paperless-ngx 2.16 or later (API version 9 or 10); "
        "upgrade paperless-ngx, then press Check again.",
        id="paperless-incompatible",
    ),
)


class TestStripWording:
    """
    One row, two endings: the strip still reads exactly as it did.

    A retrying next step is stored with a placeholder and rendered for the
    surface that shows it.  The strip's rendering must be byte-identical to
    the sentence it carried before, and the stored step must not name the
    web button, because ``saneless doctor`` prints the same row.
    """

    @pytest.mark.parametrize(("step", "today"), _STRIP_WORDING)
    def test_strip_wording_is_unchanged(
        self, step: Callable[[], str], today: str
    ) -> None:
        """
        The strip rendering is today's sentence, and the stored step is not.

        Args:
            step: Reads the stored next step from the module.
            today: What the strip said before.

        """
        stored = step()
        assert render_check_step(stored, CheckSurface.STRIP) == today
        assert "Check again" not in stored
        doctor = render_check_step(stored, CheckSurface.DOCTOR)
        assert "Check again" not in doctor
        assert "saneless doctor again" in doctor

    def test_the_give_up_line_stays_strip_only(self) -> None:
        """
        The give-up line is shown only by the strip, and keeps its wording.

        It names the button beside it and is never printed by doctor, so it
        carries no placeholder and renders unchanged.
        """
        line = "The checks have not run yet. Press Check again to try now."
        assert line == checks.POLL_GAVE_UP_LINE
        assert render_check_step(checks.POLL_GAVE_UP_LINE, CheckSurface.STRIP) == line


class TestRefusalRows:
    """
    A client or backend that could not be built is reported by why it was not.

    ``saneless doctor`` builds its own Paperless client and scanner backend, and
    each can be refused for more than one reason.  Folding every refusal into one
    row sent the reader to the wrong place: an unreadable TLS trust store read
    as a wrong address, and a scanner library that would not start read as one
    that was never installed.  The kind of refusal reaches the registry on the
    context, and each kind is its own row.
    """

    def test_an_unreadable_trust_store_is_its_own_row(self, tmp_path: Path) -> None:
        """
        The trust-store row names the two variables that steer it.

        Args:
            tmp_path: The test's own directory.

        """
        context = replace(
            _context(_settings(tmp_path), paperless=None),
            paperless_refusal=PaperlessRefusal.TRUST_STORE,
        )
        row = _row(run_checks(context), CheckKey.PAPERLESS)
        assert row.state is CheckState.FAIL
        assert row.message == "The TLS trust store could not be read."
        assert "SSL_CERT_FILE" in row.next_step
        assert "SSL_CERT_DIR" in row.next_step
        assert "not found" not in row.message

    def test_an_unusable_url_or_token_is_a_configuration_row(
        self, tmp_path: Path
    ) -> None:
        """
        A URL or token the client refused is the configuration row, not an address.

        Args:
            tmp_path: The test's own directory.

        """
        context = replace(
            _context(_settings(tmp_path), paperless=None),
            paperless_refusal=PaperlessRefusal.CONFIGURATION,
        )
        row = _row(run_checks(context), CheckKey.PAPERLESS)
        assert row.state is CheckState.FAIL
        assert row.message == connection_status_message(ConnectionStatus.MISCONFIGURED)
        assert "paperless.url" in row.next_step
        assert "paperless.token" in row.next_step

    def test_a_missing_client_with_no_reason_keeps_the_address_row(
        self, tmp_path: Path
    ) -> None:
        """
        A caller that says nothing about why keeps the row it always had.

        Args:
            tmp_path: The test's own directory.

        """
        row = _row(
            run_checks(_context(_settings(tmp_path), paperless=None)),
            CheckKey.PAPERLESS,
        )
        assert row.message == connection_status_message(ConnectionStatus.NOT_FOUND)

    def test_a_scanner_library_that_will_not_start_is_its_own_row(
        self, tmp_path: Path
    ) -> None:
        """
        A SANE that refused to start is not reported as one never installed.

        Args:
            tmp_path: The test's own directory.

        """
        context = replace(
            _context(_settings(tmp_path), scanner=None),
            scanner_refusal=ScannerRefusal.START_FAILED,
        )
        row = _row(run_checks(context), CheckKey.SCANNER)
        assert row.state is CheckState.FAIL
        assert row.message == "Scanner support could not be started."
        assert "install" not in row.next_step.lower()
        assert "log" in row.next_step
        doctor = render_check_step(row.next_step, CheckSurface.DOCTOR)
        assert "Check again" not in doctor

    @pytest.mark.parametrize(
        "refusal",
        [None, ScannerRefusal.NOT_INSTALLED],
        ids=["no-reason", "not-installed"],
    )
    def test_a_missing_python_sane_is_still_not_installed(
        self, tmp_path: Path, refusal: ScannerRefusal | None
    ) -> None:
        """
        No scanner library at all keeps the install row.

        Args:
            tmp_path: The test's own directory.
            refusal: What the caller said about the missing backend.

        """
        context = replace(
            _context(_settings(tmp_path), scanner=None), scanner_refusal=refusal
        )
        row = _row(run_checks(context), CheckKey.SCANNER)
        assert row.message == "Scanner support is not installed on this machine."

    @pytest.mark.parametrize(
        ("paperless_refusal", "scanner_refusal"),
        [
            (PaperlessRefusal.TRUST_STORE, ScannerRefusal.START_FAILED),
            (PaperlessRefusal.CONFIGURATION, ScannerRefusal.NOT_INSTALLED),
        ],
        ids=["trust-store", "configuration"],
    )
    def test_no_refusal_row_names_a_path(
        self,
        tmp_path: Path,
        paperless_refusal: PaperlessRefusal,
        scanner_refusal: ScannerRefusal,
    ) -> None:
        """
        The rows are fixed copy, which the strip could show without a leak.

        Args:
            tmp_path: The test's own directory.
            paperless_refusal: Why the client was not built.
            scanner_refusal: Why the backend was not built.

        """
        context = replace(
            _context(_settings(tmp_path), scanner=None, paperless=None),
            paperless_refusal=paperless_refusal,
            scanner_refusal=scanner_refusal,
        )
        results = run_checks(context)
        for key in (CheckKey.PAPERLESS, CheckKey.SCANNER):
            row = _row(results, key)
            for text in (row.message, row.next_step):
                assert str(tmp_path) not in text
                assert "/" not in text
