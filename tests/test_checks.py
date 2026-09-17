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

import os
import socket
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx
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
    _saned_hosts,
    _saned_reachable,
    check_name,
    check_state_class,
    check_state_glyph,
    check_state_label,
    run_checks,
    worst_state,
)
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.exceptions import ScanError
from saneless.paperless import PaperlessClient
from saneless.scanner.base import DeviceInfo
from saneless.vocabulary import (
    ConnectionStatus,
    ProfileStorage,
    connection_status_message,
)
from tests.conftest import StubScannerBackend

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# Loopback connects resolve or refuse immediately, so a short budget keeps a
# hung test from burning the suite's 60 s timeout.
_PROBE_BUDGET = 0.5

# A token that is not a placeholder, so the Paperless check reaches its probe.
_REAL_TOKEN = "a-real-looking-token"

# Skips the two writability tests when the suite runs as root, for whom a
# directory with no write bit is writable anyway.
_NOT_ROOT = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores the write bit, so the case cannot exist"
)


@pytest.fixture(autouse=True)
def _no_ambient_sane_net_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Keep the developer's own ``SANE_NET_HOSTS`` out of every test in this file.

    The scanner check reads the environment before it reads ``scanner.host``,
    because ``_ensure_initialised`` does (``sane_backend.py:876-883``).  That
    is the behaviour under test, and it is also a way for the suite's verdict
    to depend on the machine it runs on: a developer or CI image with the
    variable exported would silently redirect every pre-probe here.  The cases
    that want it set say so with ``monkeypatch.setenv`` in their own body.

    Args:
        monkeypatch: pytest's environment patcher.

    """
    monkeypatch.delenv("SANE_NET_HOSTS", raising=False)


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

    def test_a_bare_ipv6_literal_produces_no_entries_to_probe(self) -> None:
        """
        ``fe80::1`` is refused rather than read as a host and a port.

        An IPv6 literal is exactly the input where "colon-separated list of
        hosts" and "one address" are indistinguishable.  Before the refusal
        this parsed to ``(("fe80", 1),)`` -- host ``fe80`` on port 1 -- so
        every dial failed and, through the pre-probe's short circuit, a
        perfectly healthy appliance went red (WR-01 feeding CR-02).
        """
        assert _saned_hosts("fe80::1") == ()

    def test_a_bracketed_ipv6_literal_produces_no_entries_to_probe(self) -> None:
        """
        ``[fe80::1]:6566`` is refused rather than read as three host names.

        Before the refusal this parsed to ``(("[fe80", 6566), ("1]", 6566),
        ("6566", 6566))`` -- three names no resolver can answer, and three
        connect budgets spent to learn nothing.
        """
        assert _saned_hosts("[fe80::1]:6566") == ()

    def test_the_ipv6_loopback_produces_no_entries_to_probe(self) -> None:
        """
        ``::1`` is refused rather than read as the host name ``1``.

        Before the refusal this parsed to ``(("1", 6566),)``.
        """
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

    def test_a_dotted_name_with_an_out_of_range_port_is_two_hosts(self) -> None:
        """
        The ``OverflowError`` guard is not weakened by the refusal.

        ``99999`` is not a port a socket could reach, so both segments are read
        as host names, exactly as they were before.
        """
        assert _saned_hosts("scanner.local:99999") == (
            ("scanner.local", SANED_PORT),
            ("99999", SANED_PORT),
        )


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


# ---------------------------------------------------------------------------
# The five checks
# ---------------------------------------------------------------------------


class _CountingBackend(StubScannerBackend):
    """A backend that reports what it is told to and counts the times it is asked."""

    def __init__(self, devices: list[DeviceInfo] | None = None) -> None:
        """
        Record the devices this backend will report.

        Args:
            devices: What ``get_devices`` should return; empty when omitted.

        """
        self.devices = devices if devices is not None else []
        self.calls = 0

    def get_devices(self) -> list[DeviceInfo]:
        """
        Report the configured devices and count the call.

        Returns:
            The devices this stub was built with.

        """
        self.calls += 1
        return self.devices


class _RaisingBackend(StubScannerBackend):
    """A backend whose enumeration fails the way a wedged SANE fails."""

    def __init__(self) -> None:
        """Start with no calls recorded."""
        self.calls = 0

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
        name="net:scanbox.lan:brother5:bus0;dev1",
        vendor=vendor,
        model=model,
        device_type="scanner",
    )


class _RequestCounter:
    """Counts the HTTP requests a Paperless check issues, and their timeouts."""

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        """
        Wrap a responder so every call through it is recorded.

        Args:
            responder: What the stub server answers with.

        """
        self.responder = responder
        self.count = 0
        self.timeouts: list[dict[str, float | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
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


def _ok_response(_request: httpx.Request) -> httpx.Response:
    """
    Answer every request with an empty, successful tag page.

    Args:
        _request: Ignored.

    Returns:
        A 200 with an empty result list.

    """
    return httpx.Response(200, json={"count": 0, "results": []})


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
        url=url, token=_REAL_TOKEN, _transport=httpx.MockTransport(counter)
    )


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
    return Settings(
        scanner=ScannerConfig(host=host, device="test:device:001"),
        paperless=PaperlessConfig(
            url="http://paperless:8000", token=token, consume_dir=consume_dir
        ),
        output=OutputConfig(
            tmp_dir=str(tmp_path / "tmp"),
            data_dir=data_dir,
            log_file=str(tmp_path / "saneless.log"),
        ),
        profiles={"default": ProfileConfig()},
    )


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


def _recording_dialler(
    monkeypatch: pytest.MonkeyPatch, *, reachable: bool
) -> list[tuple[str, int]]:
    """
    Replace the probe's dialler with one that records what it was asked for.

    Nothing connects: the point of these cases is *which address* the check
    decided to dial, which is a decision made before any socket exists.

    Args:
        monkeypatch: pytest's attribute patcher.
        reachable: What the stubbed dialler should answer.

    Returns:
        The list the stub appends each ``(host, port)`` pair to.

    """
    dialled: list[tuple[str, int]] = []

    def _dial(host: str, port: int, _timeout: float) -> bool:
        dialled.append((host, port))
        return reachable

    monkeypatch.setattr(checks, "_saned_reachable", _dial)
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

    def test_no_devices_is_not_reachable(self, tmp_path: Path) -> None:
        """
        A backend that finds nothing is reported as not reachable.

        Args:
            tmp_path: The test's own directory.

        """
        results = run_checks(_context(_settings(tmp_path), scanner=_CountingBackend()))
        row = _row(results, CheckKey.SCANNER)
        assert row.state is CheckState.FAIL
        assert row.message == "Not reachable."
        assert row.next_step == (
            "Check the scanner is switched on and connected, then press Check again."
        )

    def test_a_raising_backend_is_not_reachable(self, tmp_path: Path) -> None:
        """
        An enumeration that throws is a red row, not a crash.

        Args:
            tmp_path: The test's own directory.

        """
        results = run_checks(_context(_settings(tmp_path), scanner=_RaisingBackend()))
        row = _row(results, CheckKey.SCANNER)
        assert row.state is CheckState.FAIL
        assert row.message == "Not reachable."

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

    def test_a_closed_saned_port_skips_the_backend(self, tmp_path: Path) -> None:
        """
        An unplugged sane-net host answers in about two seconds, not two minutes.

        The pre-probe is the whole mitigation for T-30-22: ``get_devices()``
        has no timeout at any layer, so the only way not to hang is not to
        call it.

        Args:
            tmp_path: The test's own directory.

        """
        backend = _CountingBackend([_device()])
        settings = _settings(tmp_path, host=f"127.0.0.1:{_closed_port()}")
        results = run_checks(_context(settings, scanner=backend))
        row = _row(results, CheckKey.SCANNER)
        assert backend.calls == 0
        assert row.state is CheckState.WARN
        assert row.message == (
            "The configured scanner host is not answering, "
            "so the scanner could not be checked."
        )

    def test_a_listening_saned_port_reaches_the_backend(
        self, tmp_path: Path, listening_port: int
    ) -> None:
        """
        A reachable host costs one extra handshake and then behaves as before.

        Args:
            tmp_path: The test's own directory.
            listening_port: A loopback port the fixture is listening on.

        """
        backend = _CountingBackend([_device()])
        settings = _settings(tmp_path, host=f"127.0.0.1:{listening_port}")
        results = run_checks(_context(settings, scanner=backend))
        assert backend.calls == 1
        assert _row(results, CheckKey.SCANNER).state is CheckState.OK

    def test_no_configured_host_is_never_pre_probed(self, tmp_path: Path) -> None:
        """
        A USB deployment has no TCP to probe, so the backend runs directly.

        This is also the fallback that cannot produce a false FAIL: when there
        is no entry to dial, no probe runs and the check behaves exactly as it
        did before the probe existed.

        Args:
            tmp_path: The test's own directory.

        """
        backend = _CountingBackend([_device()])
        results = run_checks(_context(_settings(tmp_path, host=""), scanner=backend))
        assert backend.calls == 1
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
        dialled = _recording_dialler(monkeypatch, reachable=True)
        settings = _settings(tmp_path, host="config-host")
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
        dialled = _recording_dialler(monkeypatch, reachable=True)
        settings = _settings(tmp_path, host="config-host")
        run_checks(_context(settings, scanner=_CountingBackend([_device()])))
        assert dialled == [("env-host", SANED_PORT)]

    def test_an_empty_environment_variable_leaves_the_setting_in_charge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        An exported-but-empty variable is not a configured host.

        ``sane_backend.py`` only treats a *set* variable as a host list, and an
        empty one names nothing, so the setting is what remains.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.

        """
        monkeypatch.setenv("SANE_NET_HOSTS", "")
        dialled = _recording_dialler(monkeypatch, reachable=True)
        settings = _settings(tmp_path, host="config-host")
        run_checks(_context(settings, scanner=_CountingBackend([_device()])))
        assert dialled == [("config-host", SANED_PORT)]

    def test_an_unanswered_host_is_amber_and_says_what_to_do(
        self, tmp_path: Path
    ) -> None:
        """
        CR-02: a refused dial is a fact about the host, not about the appliance.

        ``SANE_NET_HOSTS`` *adds* net devices; it does not replace local
        backend enumeration, so "every configured host refused TCP" never
        implied "there is no scanner".  D-01 calls a true statement about a
        deployment that still works amber.

        Args:
            tmp_path: The test's own directory.

        """
        backend = _CountingBackend([_device()])
        settings = _healthy_settings(tmp_path, host=f"127.0.0.1:{_closed_port()}")
        row = _row(run_checks(_context(settings, scanner=backend)), CheckKey.SCANNER)
        assert row.state is CheckState.WARN
        assert row.skipped is False
        assert row.next_step == (
            "Check the scanner is switched on and connected, then press Check again."
        )

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

    @pytest.mark.parametrize(
        ("environment", "setting"),
        [
            pytest.param(None, "scanbox.lan", id="setting-only"),
            pytest.param("scanbox.lan:6566", "", id="environment-only"),
            pytest.param("192.0.2.10", "scanbox.lan", id="both"),
        ],
    )
    def test_the_unanswered_row_names_no_host_address_or_port(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        environment: str | None,
        setting: str,
    ) -> None:
        """
        ASVS V7: a LAN address never reaches a LAN-visible page through this row.

        Same class of fact, and the same refusal, as ``_device_label``
        declining to print a ``net:<host>`` identifier.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.
            environment: What ``SANE_NET_HOSTS`` is set to, or None for unset.
            setting: What ``scanner.host`` is configured as.

        """
        if environment is not None:
            monkeypatch.setenv("SANE_NET_HOSTS", environment)
        _recording_dialler(monkeypatch, reachable=False)
        settings = _settings(tmp_path, host=setting)
        row = _row(
            run_checks(_context(settings, scanner=_CountingBackend([_device()]))),
            CheckKey.SCANNER,
        )
        assert row.state is CheckState.WARN
        for text in (row.message, row.next_step):
            assert ":" not in text
            assert "scanbox" not in text
            assert "192.0.2.10" not in text
            assert "6566" not in text

    def test_an_enumeration_that_found_nothing_is_still_red(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The amber row is only for the case where the probe replaced the enumeration.

        A probe that answered, followed by a backend reporting no devices, is
        an enumeration that actually ran, so its verdict stands.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.

        """
        _recording_dialler(monkeypatch, reachable=True)
        backend = _CountingBackend()
        settings = _settings(tmp_path, host="scanbox.lan")
        row = _row(run_checks(_context(settings, scanner=backend)), CheckKey.SCANNER)
        assert backend.calls == 1
        assert row.state is CheckState.FAIL
        assert row.message == "Not reachable."

    def test_a_reachable_host_whose_backend_raises_is_still_red(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        An enumeration that threw is still a red row, not the amber one.

        Args:
            tmp_path: The test's own directory.
            monkeypatch: pytest's patcher.

        """
        _recording_dialler(monkeypatch, reachable=True)
        settings = _settings(tmp_path, host="scanbox.lan")
        row = _row(
            run_checks(_context(settings, scanner=_RaisingBackend())), CheckKey.SCANNER
        )
        assert row.state is CheckState.FAIL
        assert row.message == "Not reachable."

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


_PROBE_OUTCOMES = [
    pytest.param(200, CheckState.OK, ConnectionStatus.CONNECTED, id="connected"),
    pytest.param(
        401, CheckState.FAIL, ConnectionStatus.TOKEN_REJECTED, id="token-rejected"
    ),
    pytest.param(404, CheckState.FAIL, ConnectionStatus.NOT_FOUND, id="not-found"),
    pytest.param(
        500, CheckState.FAIL, ConnectionStatus.SERVER_ERROR, id="server-error"
    ),
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

        def responder(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, text="")

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

        def responder(_request: httpx.Request) -> httpx.Response:
            msg = "no route"
            raise httpx.ConnectError(msg)

        counter = _RequestCounter(responder)
        client = _paperless(counter)
        try:
            results = run_checks(_context(_settings(tmp_path), paperless=client))
        finally:
            client.close()
        row = _row(results, CheckKey.PAPERLESS)
        assert row.state is CheckState.FAIL
        assert row.message == "Could not reach paperless-ngx."
        assert row.next_step == (
            "Check paperless-ngx is running and on the network, then press Check again."
        )

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
        A client that could not be built at all is reported as a bad address.

        The only way ``PaperlessClient.__init__`` refuses is an unusable URL,
        so this reuses the "not found at that URL" row rather than inventing a
        sixth sentence.

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

    @_NOT_ROOT
    def test_an_unwritable_fallback_folder_is_red(self, tmp_path: Path) -> None:
        """
        Writability is probed by writing, not by asking ``os.access``.

        Args:
            tmp_path: The test's own directory.

        """
        folder = tmp_path / "readonly-consume"
        folder.mkdir()
        folder.chmod(0o500)
        try:
            settings = _settings(tmp_path, consume_dir=str(folder))
            row = _row(run_checks(_context(settings)), CheckKey.FALLBACK)
        finally:
            folder.chmod(0o700)
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

    @_NOT_ROOT
    def test_an_unwritable_data_folder_is_red(self, tmp_path: Path) -> None:
        """
        A data folder that refuses a write stops saneless recording anything.

        Args:
            tmp_path: The test's own directory.

        """
        folder = tmp_path / "readonly-data"
        folder.mkdir()
        folder.chmod(0o500)
        try:
            settings = _settings(tmp_path, data_dir=str(folder))
            row = _row(run_checks(_context(settings)), CheckKey.DATA_DIR)
        finally:
            folder.chmod(0o700)
        assert row.state is CheckState.FAIL
        assert row.message == "The data folder cannot be written to."
        assert row.next_step == (
            "Check the folder exists and saneless can write to it, "
            "then restart saneless."
        )


def _broken_context(tmp_path: Path) -> CheckContext:
    """
    Build a context in which every single check goes wrong.

    Args:
        tmp_path: The test's own directory.

    Returns:
        A context whose five rows are all WARN or FAIL.

    """
    settings = _with_profiles(
        _settings(
            tmp_path,
            host=f"127.0.0.1:{_closed_port()}",
            token="changeme",
            consume_dir=str(tmp_path / "missing-consume"),
            data_dir=str(tmp_path / "missing-data"),
        ),
        {},
    )
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
        A healthy context still produces all five rows.

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
        A context where nothing works still produces all five rows.

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
        assert [result.skipped for result in results] == [
            True,
            False,
            False,
            False,
            False,
        ]

    @pytest.mark.parametrize("key", list(CheckKey))
    def test_no_row_leaks_a_path_url_token_or_traceback(
        self, tmp_path: Path, key: CheckKey
    ) -> None:
        """
        ASVS V7: nothing internal reaches a LAN-visible page through a row.

        Args:
            tmp_path: The test's own directory.
            key: The row under inspection.

        """
        row = _row(run_checks(_broken_context(tmp_path)), key)
        for text in (row.message, row.next_step):
            assert "/" not in text
            assert "http" not in text
            assert "changeme" not in text
            assert "Traceback" not in text
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
