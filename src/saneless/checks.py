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
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, assert_never

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "CHECKING_GLYPH",
    "CHECKING_MESSAGE",
    "CHECKING_STATE_CLASS",
    "PROBE_CONNECT_SECONDS",
    "PROBE_READ_SECONDS",
    "SANED_PORT",
    "CheckKey",
    "CheckResult",
    "CheckState",
    "check_name",
    "check_state_class",
    "check_state_glyph",
    "check_state_label",
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

# The cold-start row, before any check has run (D-06).  It lives here rather
# than in the template for the same reason the state glyphs do: templates own
# no vocabulary.  U+00B7 is neutral -- it says "not yet", not "bad" -- and
# U+2026 matches the spelling of the Scan button's "Scanning...".
CHECKING_GLYPH: Final = "·"
CHECKING_STATE_CLASS: Final = "check-checking"
CHECKING_MESSAGE: Final = "Checking…"


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

    Args:
        host_setting: The configured ``scanner.host``, possibly empty.

    Returns:
        One ``(host, port)`` pair per entry, in the configured order.  Empty
        when nothing is configured or every segment is blank.

    """
    segments = [segment.strip() for segment in host_setting.split(":")]
    present = [segment for segment in segments if segment]
    if not present:
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
