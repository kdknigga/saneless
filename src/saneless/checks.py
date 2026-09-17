"""
The one health-check registry, and the words both surfaces use to show it.

This is not a leaf module -- it needs ``Settings``, a scanner backend and a
Paperless client to answer anything -- but it has a leaf's import rule all the
same, and the rule is the point of the module.  ``saneless doctor`` and the web
status strip are required to report the *same* checks in the *same* words
(D-02), so this module must import **nothing from ``saneless.web`` and nothing
from ``saneless.cli``**.  If it imported either, it would belong to that
surface, and the other one would end up building a web application to print a
terminal table or importing Click to render a page.

Every dependency is injected instead: the settings, the scanner backend, the
Paperless client and the worker's profile-storage outcome all arrive as
parameters on ``CheckContext``.  That is also what lets ``doctor`` run on a
machine with no python-sane at all -- it passes ``scanner=None`` and still
reports all five rows.

ASVS V7 applies to every string this module can render.  No message and no next
step carries a filesystem path, a URL, a token value or exception text.  The
Paperless URL may hold ``user:pass@`` (``paperless.py:461``) and the fallback
folder is a host path on a LAN-visible page, so both are deliberately omitted
for the same reason D-13 omits the log path.
"""

from __future__ import annotations

import logging
import socket
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from string import ascii_letters, digits
from typing import TYPE_CHECKING, Final, assert_never

import httpx

from saneless.config import is_placeholder_token
from saneless.vocabulary import (
    ConnectionStatus,
    ProfileStorage,
    connection_status_message,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from saneless.config import Settings
    from saneless.paperless import PaperlessClient
    from saneless.scanner.base import DeviceInfo, ScannerBackend

__all__ = [
    "CHECKING_GLYPH",
    "CHECKING_MESSAGE",
    "CHECKING_STATE_CLASS",
    "CHECKING_STATE_LABEL",
    "PROBE_CONNECT_SECONDS",
    "PROBE_READ_SECONDS",
    "SANED_PORT",
    "CheckContext",
    "CheckKey",
    "CheckResult",
    "CheckState",
    "check_name",
    "check_state_class",
    "check_state_glyph",
    "check_state_label",
    "run_checks",
    "worst_state",
]

logger = logging.getLogger(__name__)


# How long a saned pre-probe waits for a TCP handshake before giving up.  Two
# seconds is the whole of success criterion 2 on the SANE side: without it an
# unplugged sane-net host costs roughly 127 s inside a blocking C call, because
# Linux retries a SYN six times by default.  Read at call time, so tests can
# shorten it.
PROBE_CONNECT_SECONDS: Final = 2.0

# How long the Paperless probe waits for a response body once connected.  The
# client's own default is a flat 30 s, which is the right budget for an upload
# and the wrong one for a health row.  Read at call time.
PROBE_READ_SECONDS: Final = 5.0

# saned's registered port.  IANA names 6566 ``sane-port``, and this machine's
# ``/etc/services`` agrees, which settles RESEARCH assumption A1.  Read at call
# time.
SANED_PORT: Final = 6566

# Every character a segment of ``scanner.host`` may contain and still be read
# as a host name.  Deliberately narrower than any hostname RFC: this is not a
# validator, it is the smallest set that tells a name apart from the halves an
# IPv6 literal falls into when it is split on ``:``.
_HOST_NAME_CHARACTERS: Final = frozenset(ascii_letters + digits + "-.")

# The cold-start row, before any check has run (D-06).  It lives here rather
# than in the template for the same reason the state glyphs do: templates own
# no vocabulary.  U+00B7 is neutral -- it says "not yet", not "bad" -- and
# U+2026 matches the spelling of the Scan button's "Scanning...".
CHECKING_GLYPH: Final = "·"
CHECKING_STATE_CLASS: Final = "check-checking"
CHECKING_MESSAGE: Final = "Checking…"
# The word that replaces the cold-start glyph for a screen reader.  The glyph
# is ``aria-hidden``, so without this a listener would hear the row's name and
# message with no marker at all where every other row has one.  It is a word
# rather than the ellipsis for the same reason ``check_state_label`` says
# "Failed" instead of "FAIL": the glyph's meaning has to survive as speech.
CHECKING_STATE_LABEL: Final = "Checking"

# How much of a device's own description a row will print.  Nothing else bounds
# what a scanner can call itself, and the row is rendered into HTML next to
# four others whose width is fixed (ROBU-08).
_DEVICE_LABEL_MAX_LENGTH: Final = 60

# The row a check that raised is rendered as.  Developer constants, because the
# exception that produced them is exactly the thing that must not reach the
# page.
_CHECK_FAILED_MESSAGE: Final = "This check could not be completed."
_CHECK_FAILED_NEXT_STEP: Final = "Restart saneless, then press Check again."


class CheckState(StrEnum):
    """
    How one health check came out: three states, and only three (D-01).

    ``OK`` is nothing to do.  ``WARN`` is a true statement about a deployment
    that still works -- no fallback folder, profiles that live only in memory
    -- and ``FAIL`` is something that stops scanning or filing.

    The distinction is what ``saneless doctor``'s exit code is built on: it
    exits non-zero on ``FAIL`` only.  A ``WARN`` must not fail a scripted
    health gate, because an appliance that scans and files correctly is not
    broken just because it could be tidier, and a gate that goes red for
    tidiness is a gate people learn to ignore.

    A fourth state would need an exit-code rule, a glyph, a colour class and a
    screen-reader label, and every ``match`` here would stop type-checking
    until it got them.  That is the intended cost.
    """

    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


class CheckKey(StrEnum):
    """
    The five checks, and the contract that both surfaces show all five (D-02).

    This enum *is* the "neither surface may define a check the other does not
    have" rule.  ``run_checks`` iterates it and returns one result per member,
    ``saneless doctor`` prints them in member order and the status strip
    renders them in member order, so there is no list of checks anywhere else
    to drift out of step with this one.

    Adding a sixth member is therefore a deliberate act with a visible cost:
    ``check_name`` stops type-checking until the new key has a name, the
    dispatch in ``run_checks`` stops type-checking until it has a check
    function, and the completeness test in ``tests/test_checks.py`` fails until
    the count is updated.

    ``DATA_DIR`` is the member name, and ``"Data folder"`` is what a household
    member reads.  The member names are internal; only ``check_name`` is user
    copy.
    """

    SCANNER = "SCANNER"
    PAPERLESS = "PAPERLESS"
    PROFILES = "PROFILES"
    FALLBACK = "FALLBACK"
    DATA_DIR = "DATA_DIR"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """
    One finished check, as both surfaces render it.

    Frozen for the reason ``ScanBatch`` is frozen: this is a report of a probe
    that has already happened, and nothing downstream has any business editing
    it on the way to a page or a terminal.

    ``next_step`` is empty for every ``OK`` row and non-empty for every
    ``WARN`` and ``FAIL`` row.  That is the whole of APPL-04's shape here: a
    red row that does not say what to do about it is a red row a household
    member can only escalate.

    ``skipped`` is D-08's "not checked while a scan is running".  It is a
    separate flag rather than a fourth state because the row still has to carry
    *some* state for its glyph, and "we did not look" is a fact about the
    probe, not a verdict about the appliance.

    Attributes:
        key: Which of the five checks this is.
        state: How it came out.
        message: A developer-authored sentence.  Never a path, a URL, a token
            or exception text (ASVS V7).
        next_step: What to do about it, for ``WARN`` and ``FAIL`` rows.
        skipped: True when the probe was deliberately not run.

    """

    key: CheckKey
    state: CheckState
    message: str
    next_step: str = ""
    skipped: bool = False


@dataclass(frozen=True, slots=True)
class CheckContext:
    """
    Everything the five checks need, handed in rather than reached for.

    Every dependency is injected because the two surfaces build them
    differently, and neither may be the one this module knows about (D-02).
    The web app has a worker, a long-lived Paperless client and a scanner
    backend it opened at startup; ``saneless doctor`` has none of those and
    builds what it needs for one command.

    ``scanner=None`` is how "python-sane is not installed on this machine" is
    represented.  That is what lets ``doctor`` report all five rows on such a
    machine instead of refusing at ``require_sane()`` and reporting none
    (Amendment A-1) -- the thing an operator most needs a diagnostic for is the
    machine where the diagnostic would otherwise not run.

    ``paperless=None`` means no usable client could be built at all, which
    ``PaperlessClient.__init__`` only refuses for a URL httpx will not parse.

    ``profile_storage`` is the outcome the worker recorded when it wrote the
    generated profiles, not something re-derived here.  ``doctor`` derives its
    own from whether a config file was loaded.  It has to be a record rather
    than a fresh probe, because the two in-memory cases are indistinguishable
    afterwards and the read-only one is the only one worth acting on (D-22).

    Attributes:
        settings: The loaded configuration.
        scanner: The scanner backend, or None when there is no SANE support.
        paperless: The Paperless client, or None when none could be built.
        profile_storage: What the profile write actually did.
        skip_scanner: True while a scan is running, which pauses the scanner
            check without touching the backend (D-08).

    """

    settings: Settings
    scanner: ScannerBackend | None
    paperless: PaperlessClient | None
    profile_storage: ProfileStorage
    skip_scanner: bool = False


def check_name(key: CheckKey) -> str:
    """
    Return the name column a reader sees for one check.

    The name is a separate element from the message in both surfaces, which is
    what lets ``connection_status_message``'s existing sentences drop into the
    Paperless row verbatim with no string surgery and no capitalisation
    collision (UI-SPEC S1).

    Args:
        key: The check to name.

    Returns:
        A short noun phrase, e.g. ``"Data folder"``.

    Raises:
        AssertionError: If the value is not a CheckKey member.

    """
    match key:
        case CheckKey.SCANNER:
            name = "Scanner"
        case CheckKey.PAPERLESS:
            name = "Paperless"
        case CheckKey.PROFILES:
            name = "Profiles"
        case CheckKey.FALLBACK:
            name = "Fallback"
        case CheckKey.DATA_DIR:
            name = "Data folder"
        case _:
            assert_never(key)
    return name


def check_state_label(state: CheckState) -> str:
    """
    Return the word a screen reader announces before a check row.

    The glyph is ``aria-hidden`` and this is what replaces it, so these are
    words rather than the member values: "Failed" is a sentence a listener
    understands and "FAIL" is shouting.

    Args:
        state: The state to label.

    Returns:
        ``"OK"``, ``"Warning"`` or ``"Failed"``.

    Raises:
        AssertionError: If the value is not a CheckState member.

    """
    match state:
        case CheckState.OK:
            label = "OK"
        case CheckState.WARN:
            label = "Warning"
        case CheckState.FAIL:
            label = "Failed"
        case _:
            assert_never(state)
    return label


def check_state_class(state: CheckState) -> str:
    """
    Return the CSS class that colours one check's glyph.

    Templates own no vocabulary, so the mapping from a state to a class lives
    here and never as a ``{% if state == 'FAIL' %}`` in a template.  Each class
    is an alias over an existing colour token; this phase introduces no new
    colour value.

    Args:
        state: The state to classify.

    Returns:
        ``"check-ok"``, ``"check-warn"`` or ``"check-fail"``.

    Raises:
        AssertionError: If the value is not a CheckState member.

    """
    match state:
        case CheckState.OK:
            css_class = "check-ok"
        case CheckState.WARN:
            css_class = "check-warn"
        case CheckState.FAIL:
            css_class = "check-fail"
        case _:
            assert_never(state)
    return css_class


def check_state_glyph(state: CheckState) -> str:
    """
    Return the text-presentation glyph for one check's state.

    U+2713 and U+2717 are already in use for ``Done`` and ``Error``, so the
    strip borrows them rather than inventing a second visual language.

    The warning glyph is a plain ASCII ``!``.  Every Unicode warning symbol --
    U+26A0, U+2757 -- renders with emoji presentation on at least one shipping
    platform, and the master spec already records U+26A0 as rejected for
    exactly that reason.  ``!`` has text presentation everywhere and needs no
    variation selector.

    Args:
        state: The state to mark.

    Returns:
        A single-character glyph.

    Raises:
        AssertionError: If the value is not a CheckState member.

    """
    match state:
        case CheckState.OK:
            glyph = "✓"
        case CheckState.WARN:
            glyph = "!"
        case CheckState.FAIL:
            glyph = "✗"
        case _:
            assert_never(state)
    return glyph


def worst_state(results: Iterable[CheckResult]) -> CheckState:
    """
    Collapse a list of results into the one verdict a caller acts on.

    ``FAIL`` beats ``WARN`` beats ``OK``, and an empty list is ``OK`` -- there
    is nothing to report, which is not the same as refusing to answer.

    The collapse is a ``match`` over ``CheckState`` rather than ``max()`` over
    the member values.  ``max()`` would happen to work today only because
    ``"WARN"`` sorts after ``"OK"`` and ``"FAIL"`` sorts before both, which it
    does not -- and even where alphabetical order accidentally matched the
    severity order, it would be an ordering nobody chose and no type checker
    would defend when a member is added.

    Args:
        results: The finished checks to collapse, in any order.

    Returns:
        The most severe state present, or ``OK`` when there are none.

    Raises:
        AssertionError: If a result carries a value that is not a CheckState.

    """
    worst = CheckState.OK
    for result in results:
        match result.state:
            case CheckState.FAIL:
                worst = CheckState.FAIL
            case CheckState.WARN:
                if worst is CheckState.OK:
                    worst = CheckState.WARN
            case CheckState.OK:
                # Cannot raise the verdict, so the running answer stands.
                pass
            case _:
                assert_never(result.state)
    return worst


def _looks_like_a_host_name(segment: str) -> bool:
    """
    Say whether one colon-separated segment could be a host name at all.

    This is deliberately not a hostname RFC implementation and must not grow
    into one.  It answers a single narrower question -- "could sane-net have
    meant this as a name?" -- and the only inputs it has to tell apart are the
    names an operator types and the fragments an IPv6 literal falls into when
    it is split on ``:``.  ``[fe80`` and ``1]`` fail on their brackets, and the
    empty string fails on being empty, which is all that is needed.

    Being narrow is the safe direction.  A false "no" costs the pre-probe's
    latency saving and nothing else, because ``_saned_hosts`` then returns no
    entries and the scanner check falls through to ``get_devices()``.  A false
    "yes" is what WR-01 was: three junk dials and, through the pre-probe's
    short circuit, a wrong verdict.

    Args:
        segment: One stripped segment of the ``scanner.host`` setting.

    Returns:
        True when the segment is non-empty, made only of ASCII letters,
        digits, hyphens and dots, and neither starts nor ends with a hyphen or
        a dot.

    """
    if not segment:
        return False
    if segment[0] in "-." or segment[-1] in "-.":
        return False
    return set(segment) <= _HOST_NAME_CHARACTERS


def _saned_hosts(host_setting: str) -> tuple[tuple[str, int], ...]:
    """
    Parse ``scanner.host`` into the ``(host, port)`` pairs saned would be dialled on.

    sane-net's own syntax is genuinely ambiguous.  The setting is
    colon-separated when it names several hosts (``sane_backend.py:849``,
    ``sane-net(5)``), *and* ``host:port`` is a legal single entry.  ``a:b`` is
    therefore either two hosts or one host on a port named ``b``, and nothing
    in the string settles it.

    The reading taken here is the narrow one: a trailing segment is a port only
    when the setting has exactly two segments and that segment is all digits
    within 1..65535.  Anything else is a list of host names.  It is narrow on
    purpose -- misreading a host as a port would probe the wrong address
    entirely, while misreading a port as a host costs one connect to a name
    that does not resolve and then falls through to the behaviour that existed
    before this module.

    The range check is not cosmetic: ``socket.create_connection`` raises
    ``OverflowError`` -- which is not an ``OSError`` -- for a port outside
    0..65535, so accepting ``host:99999`` as a port would put an exception the
    probe does not catch inside the probe.

    **The refusal.**  With more than one colon present, the setting is refused
    outright -- no entries, no probe -- unless every segment could be a host
    name and no blank segment sits anywhere but the first or last position.
    An IPv6 literal is exactly the input where "colon-separated list of hosts"
    and "one address" are indistinguishable: ``fe80::1`` split on ``:`` used to
    read as host ``fe80`` on port 1, and ``[fe80::1]:6566`` as three names no
    resolver can answer (WR-01).  The stray colon at either edge stays
    tolerated, because ``: host-a :`` has only ever meant one host.

    Returning ``()`` is not a silent failure; it is the fallback this module
    documents everywhere else.  No entries means no probe, which means the
    scanner check calls ``get_devices()`` and behaves exactly as it did before
    the probe existed.  An operator who typed an IPv6 literal loses the
    pre-probe's latency saving and never gets a wrong verdict, which is the
    trade the whole module is built on.

    Args:
        host_setting: The configured ``scanner.host``, possibly empty.

    Returns:
        One ``(host, port)`` pair per entry, in the configured order.  Empty
        when nothing is configured, every segment is blank, or the setting is
        one this module refuses to guess at.

    """
    segments = [segment.strip() for segment in host_setting.split(":")]
    present = [segment for segment in segments if segment]
    if not present:
        return ()
    if len(segments) > 2 and (
        "" in segments[1:-1]
        or not all(_looks_like_a_host_name(segment) for segment in present)
    ):
        return ()
    if len(present) == 2:
        host, maybe_port = present
        if maybe_port.isdigit() and 0 < int(maybe_port) <= 65535:
            return ((host, int(maybe_port)),)
    return tuple((host, SANED_PORT) for host in present)


def _saned_reachable(host: str, port: int, timeout: float) -> bool:
    """
    Say whether a TCP connection to a saned host can be established in time.

    There is no other reachability probe anywhere in this tree, and this one
    exists because there is nowhere else to put a bound.
    ``SaneBackend.get_devices()`` (``sane_backend.py:2036-2078``) calls into
    libsane, which has no timeout parameter at any layer -- not in
    python-sane, not in ``sane_get_devices(3)``, and not settable from Python.
    With the ``net`` backend that call opens a TCP connection to each entry of
    ``SANE_NET_HOSTS``, so an unplugged host is a connect that hangs until the
    kernel gives up: on Linux ``tcp_syn_retries`` defaults to 6, roughly 127
    seconds, inside a blocking C call nothing can interrupt.  Dialling the same
    address first, with a timeout, is the only bound available.

    The port is 6566, IANA's ``sane-port``, verified in ``/etc/services``.

    **Failure policy.**  This returns ``False`` for every ``OSError`` --
    refused, timed out, unresolvable, no route -- and raises nothing.  It is
    the *caller* that decides what a ``False`` means, and the caller never
    turns "the probe could not be run at all" into a red row: when
    ``_saned_hosts`` yields no entries to dial, no probe happens and the
    scanner check falls back to ``get_devices()``, which is exactly the
    behaviour that existed before this module.  Only a probe that actually ran
    and was refused on every configured entry produces ``FAIL``, and in that
    state ``get_devices()`` would spend two minutes reaching the same
    conclusion.

    Args:
        host: The host name or address to dial.
        port: The TCP port to dial.
        timeout: How long to wait for the handshake, in seconds.

    Returns:
        True when the connection was established, False otherwise.

    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as exc:
        # Logged at DEBUG, not WARNING: a closed scanner port is the ordinary
        # state of an appliance whose scanner is switched off, and the row the
        # caller renders is where an operator is told about it.
        logger.debug("saned pre-probe did not connect: %s", type(exc).__name__)
        return False


def _directory_accepts_a_write(path: Path) -> bool:
    """
    Say whether a directory will actually take a file, by putting one there.

    This writes and removes a temporary file rather than asking
    ``os.access``.  ``os.access`` answers a question about the directory's mode
    bits, and the failure Phase 27 D-09 was written for is not a mode bit: a
    single-file bind mount where the directory is writable, ``os.access`` says
    yes, and only the operation itself fails with EBUSY.  A check that asks a
    different question to the one the appliance will ask at scan time is a
    check that can be green while scanning is broken.

    Args:
        path: The directory to probe.

    Returns:
        True when a file was created and removed, False on any OSError --
        missing, not a directory, read-only, out of space or busy.

    """
    try:
        with tempfile.NamedTemporaryFile(dir=path, prefix=".saneless-check-"):
            return True
    except OSError as exc:
        logger.debug("directory did not accept a write: %s", type(exc).__name__)
        return False


def _device_label(device: DeviceInfo) -> str:
    """
    Describe a device in the words on its lid, never by its SANE identifier.

    ``DeviceInfo.name`` is the SANE device id, and for the ``net`` backend it
    is ``net:<host>:<backend>:...`` -- a LAN address.  Putting it in a row
    would publish that address to everyone who can load the index page, which
    is the same reason the fallback row omits the folder path (ASVS V7).
    ``vendor`` and ``model`` are what the device calls itself and what is
    printed on its lid, so they are what a household member can match against
    the machine in front of them.

    Args:
        device: The device the backend reported.

    Returns:
        A bounded description, or the empty string when the backend reported
        no vendor and no model.

    """
    label = " ".join(f"{device.vendor} {device.model}".split())
    if len(label) > _DEVICE_LABEL_MAX_LENGTH:
        label = label[: _DEVICE_LABEL_MAX_LENGTH - 1].rstrip() + "…"
    return label


def _scanner_unreachable() -> CheckResult:
    """
    Build the "the scanner is not answering" row.

    Returns:
        The UI-SPEC S1 not-reachable row.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.FAIL,
        message="Not reachable.",
        next_step=(
            "Check the scanner is switched on and connected, then press Check again."
        ),
    )


def _scanner_skipped() -> CheckResult:
    """
    Build the row shown while a scan is running (D-08).

    The state is ``OK`` rather than ``WARN`` or ``FAIL``.  "We did not look" is
    a fact about the probe, not a verdict about the appliance, and a scan in
    flight is direct evidence the scanner was working moments ago; a scripted
    health gate must not go red for the duration of every scan.  The
    ``skipped`` flag, not the state, is what the two surfaces render.

    Returns:
        The UI-SPEC S1 skipped row.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.OK,
        message="Not checked while a scan is running.",
        skipped=True,
    )


def _check_scanner(context: CheckContext) -> CheckResult:
    """
    Report whether a scanner is there to scan with.

    The order is deliberate.  A machine with no python-sane is its own row
    (Amendment A-1) and is decided without touching anything.  A configured
    sane-net host is then pre-probed, and every configured entry refusing a TCP
    connection ends the check right there: ``get_devices()`` would spend about
    two minutes reaching the same conclusion inside a C call nothing can
    interrupt (T-30-22).  Only when there is no host to probe, or one of them
    answered, is the backend entered at all -- which is also the fallback that
    cannot produce a false red row, because a setting this module cannot parse
    into an entry leaves the check behaving exactly as it did before the probe
    existed.

    Args:
        context: The injected dependencies and configuration.

    Returns:
        Exactly one result for ``CheckKey.SCANNER``.

    """
    scanner = context.scanner
    if scanner is None:
        return CheckResult(
            key=CheckKey.SCANNER,
            state=CheckState.FAIL,
            message="Scanner support is not installed on this machine.",
            next_step="Install saneless with scanner support, then restart it.",
        )
    entries = _saned_hosts(context.settings.scanner.host)
    if entries and not any(
        _saned_reachable(host, port, PROBE_CONNECT_SECONDS) for host, port in entries
    ):
        return _scanner_unreachable()
    try:
        devices = scanner.get_devices()
    except Exception as exc:
        # The backend raises ScanError, but python-sane underneath it raises
        # _sane.error, RuntimeError or AttributeError with no shared base
        # (sane_backend.py D-08), so the boundary catches Exception.  The type
        # name is logged; nothing from the exception reaches the row.
        logger.warning("Scanner enumeration failed: %s", type(exc).__name__)
        return _scanner_unreachable()
    if not devices:
        return _scanner_unreachable()
    label = _device_label(devices[0])
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.OK,
        message=f"{label} is ready." if label else "Ready.",
    )


def _paperless_next_step(status: ConnectionStatus) -> str:
    """
    Return what to do about one connection outcome.

    The sentences are UI-SPEC S1's, and they pair with the messages
    ``connection_status_message`` already owns -- this module authors the
    remedy, never the diagnosis, so the two surfaces cannot disagree about
    what happened even if they disagreed about what to do.

    Args:
        status: The connection-test outcome.

    Returns:
        A next step, or the empty string when there is nothing to do.

    Raises:
        AssertionError: If the value is not a ConnectionStatus member.

    """
    match status:
        case ConnectionStatus.CONNECTED:
            next_step = ""
        case ConnectionStatus.TOKEN_REJECTED:
            next_step = (
                "Check the API token in the saneless config file, "
                "then restart saneless."
            )
        case ConnectionStatus.NOT_FOUND:
            next_step = "Check the paperless-ngx address in the saneless config file."
        case ConnectionStatus.SERVER_ERROR:
            next_step = "Check paperless-ngx is healthy, then press Check again."
        case ConnectionStatus.UNREACHABLE:
            next_step = (
                "Check paperless-ngx is running and on the network, "
                "then press Check again."
            )
        case _:
            assert_never(status)
    return next_step


def _check_paperless(context: CheckContext) -> CheckResult:
    """
    Report whether scans can be filed, without spending thirty seconds on it.

    The token is examined first and the probe is skipped entirely when it is a
    placeholder (D-14): an unset token cannot succeed, so a request would only
    tell paperless-ngx about it.  ``is_placeholder_token`` is the one predicate
    ``doctor``, this check, the scan route and ``saneless scan`` share, so all
    four agree on whether the appliance can upload (APPL-07).

    A ``None`` client means one could not be constructed, and the only way
    ``PaperlessClient.__init__`` refuses is a URL httpx will not parse -- which
    is the "not found at that URL" row, not a sixth sentence.

    Args:
        context: The injected dependencies and configuration.

    Returns:
        Exactly one result for ``CheckKey.PAPERLESS``.

    """
    if is_placeholder_token(context.settings.paperless.token.get_secret_value()):
        return CheckResult(
            key=CheckKey.PAPERLESS,
            state=CheckState.FAIL,
            message="The paperless-ngx API token has not been set.",
            next_step=(
                "Put a real API token in the saneless config file, "
                "then restart saneless."
            ),
        )
    client = context.paperless
    if client is None:
        status = ConnectionStatus.NOT_FOUND
    else:
        status = client.test_connection(
            timeout=httpx.Timeout(PROBE_READ_SECONDS, connect=PROBE_CONNECT_SECONDS)
        )
    state = CheckState.OK if status is ConnectionStatus.CONNECTED else CheckState.FAIL
    return CheckResult(
        key=CheckKey.PAPERLESS,
        state=state,
        message=connection_status_message(status),
        next_step=_paperless_next_step(status),
    )


def _check_profiles(context: CheckContext) -> CheckResult:
    """
    Report whether there are scan profiles and whether they will survive a restart.

    Exactly one result comes out, and the precedence is fixed: no profiles at
    all (red) beats a read-only config location (amber) beats no config file at
    all (amber) beats a generated profile with no name (amber) beats the count.
    The two amber rows are deliberately different sentences, because "saneless
    has no file to save to" and "saneless has one and cannot write it" are
    different facts and only the second is worth investigating (D-22,
    Amendment A-2).

    The storage outcome is recorded by the worker rather than recomputed here.
    A fresh ``os.access`` probe cannot substitute for it: Phase 27 D-09's
    motivating failure is a bind mount where the directory is writable and only
    the rename fails.

    Args:
        context: The injected dependencies and configuration.

    Returns:
        Exactly one result for ``CheckKey.PROFILES``.

    Raises:
        AssertionError: If the storage outcome is not a ProfileStorage member.

    """
    profiles = context.settings.profiles
    if not profiles:
        return CheckResult(
            key=CheckKey.PROFILES,
            state=CheckState.FAIL,
            message="No scan profiles are configured.",
            next_step='Run "saneless auto-profiles" to create them.',
        )
    match context.profile_storage:
        case ProfileStorage.IN_MEMORY_UNWRITABLE:
            return CheckResult(
                key=CheckKey.PROFILES,
                state=CheckState.WARN,
                # One literal, deliberately over the 88-column guide (E501 is
                # off in this project): D-22 pins this sentence verbatim, and a
                # grep for it has to find it on one line.
                message="Generated in memory — the config location is read-only, so they are lost on restart.",
                next_step=(
                    "Make the saneless config directory writable, "
                    "then restart saneless."
                ),
            )
        case ProfileStorage.IN_MEMORY_NO_CONFIG_FILE:
            return CheckResult(
                key=CheckKey.PROFILES,
                state=CheckState.WARN,
                # One literal for the same reason as the sibling row above.
                message="Generated in memory — no configuration file is in use, so they are lost on restart.",
                next_step="Create a saneless config file so the profiles are saved.",
            )
        case ProfileStorage.PERSISTED:
            # Saved to the config file and will survive a restart, so the only
            # question left is whether they have names.
            pass
        case _:
            assert_never(context.profile_storage)
    if any(
        profile.auto_generated and not profile.label for profile in profiles.values()
    ):
        return CheckResult(
            key=CheckKey.PROFILES,
            state=CheckState.WARN,
            message="Some profiles have no name yet.",
            next_step='Run "saneless auto-profiles --force" to name them.',
        )
    count = len(profiles)
    noun = "profile" if count == 1 else "profiles"
    return CheckResult(
        key=CheckKey.PROFILES,
        state=CheckState.OK,
        message=f"{count} scan {noun} configured.",
    )


def _check_fallback(context: CheckContext) -> CheckResult:
    """
    Report whether a scan has somewhere to go when paperless-ngx is down.

    An unset fallback folder is amber and never red (APPL-11, D-22).  The
    appliance scans and files perfectly without one; what it cannot do is
    survive paperless-ngx being down, and a red row for a deployment that works
    is a row people learn to ignore.

    The configured path is not in either sentence.  It is a host filesystem
    path on a LAN-visible page, omitted for the same reason D-13 omits the log
    path (T-30-21).

    Args:
        context: The injected dependencies and configuration.

    Returns:
        Exactly one result for ``CheckKey.FALLBACK``.

    """
    consume_dir = context.settings.paperless.consume_dir
    if not consume_dir:
        return CheckResult(
            key=CheckKey.FALLBACK,
            state=CheckState.WARN,
            message="Not configured; scans cannot be kept if paperless-ngx is down.",
            next_step=(
                "Set a fallback folder in the saneless config so scans are kept "
                "when paperless-ngx is down."
            ),
        )
    if _directory_accepts_a_write(Path(consume_dir)):
        return CheckResult(
            key=CheckKey.FALLBACK,
            state=CheckState.OK,
            message="A folder is set up to keep scans if paperless-ngx is down.",
        )
    return CheckResult(
        key=CheckKey.FALLBACK,
        state=CheckState.FAIL,
        message="The fallback folder cannot be written to.",
        next_step="Check the folder exists and saneless can write to it.",
    )


def _check_data_dir(context: CheckContext) -> CheckResult:
    """
    Report whether the folder holding the job database will take a write.

    Unlike the fallback folder this one is not optional: the job store and the
    preserved scans live in it, so a data folder that refuses a write is red.

    Args:
        context: The injected dependencies and configuration.

    Returns:
        Exactly one result for ``CheckKey.DATA_DIR``.

    """
    if _directory_accepts_a_write(Path(context.settings.output.data_dir)):
        return CheckResult(
            key=CheckKey.DATA_DIR,
            state=CheckState.OK,
            message="The data folder is writable.",
        )
    return CheckResult(
        key=CheckKey.DATA_DIR,
        state=CheckState.FAIL,
        message="The data folder cannot be written to.",
        next_step=(
            "Check the folder exists and saneless can write to it, "
            "then restart saneless."
        ),
    )


def _dispatch(key: CheckKey, context: CheckContext) -> CheckResult:
    """
    Run the one check a key names.

    A total ``match`` rather than a dict of functions: a sixth ``CheckKey``
    member stops this function type-checking until somebody decides what it
    does, which a dict lookup with a fallback would not.

    Args:
        key: The check to run.
        context: The injected dependencies and configuration.

    Returns:
        That check's single result.

    Raises:
        AssertionError: If the value is not a CheckKey member.

    """
    match key:
        case CheckKey.SCANNER:
            result = _check_scanner(context)
        case CheckKey.PAPERLESS:
            result = _check_paperless(context)
        case CheckKey.PROFILES:
            result = _check_profiles(context)
        case CheckKey.FALLBACK:
            result = _check_fallback(context)
        case CheckKey.DATA_DIR:
            result = _check_data_dir(context)
        case _:
            assert_never(key)
    return result


def run_checks(context: CheckContext) -> tuple[CheckResult, ...]:
    """
    Run every check once, in member order, and never raise.

    This is the function both surfaces call, and the tuple it returns is the
    whole of what either of them may show (D-02).  It iterates ``CheckKey``, so
    a check that exists for ``saneless doctor`` and not for the status strip is
    not something either surface is able to express.

    ``skip_scanner`` is honoured here rather than inside the scanner check, and
    it returns the paused row without entering the backend at all.  That is
    correctness, not politeness: nothing in ``sane_backend.py`` mutually
    excludes two SANE calls, so a status probe landing on the device mid-scan
    is a second caller into the same C library (Pitfall 2).  The caller derives
    the flag from the worker's scanner gate.

    A check that raises is caught and rendered as a red row with a
    developer-constant message.  A registry that could raise would take the
    whole strip down and with it the four checks that were fine, and the
    exception text is exactly the thing that must not reach a LAN-visible page.

    Args:
        context: The injected dependencies and configuration.

    Returns:
        One result per ``CheckKey`` member, in member order.

    """
    results: list[CheckResult] = []
    for key in CheckKey:
        if key is CheckKey.SCANNER and context.skip_scanner:
            results.append(_scanner_skipped())
            continue
        try:
            results.append(_dispatch(key, context))
        except Exception as exc:
            logger.warning("Check %s raised %s", key.value, type(exc).__name__)
            results.append(
                CheckResult(
                    key=key,
                    state=CheckState.FAIL,
                    message=_CHECK_FAILED_MESSAGE,
                    next_step=_CHECK_FAILED_NEXT_STEP,
                )
            )
    return tuple(results)
