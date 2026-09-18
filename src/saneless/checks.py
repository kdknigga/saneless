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

import ipaddress
import logging
import os
import socket
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from string import ascii_letters, digits, hexdigits
from time import monotonic
from typing import TYPE_CHECKING, Final, assert_never

import httpx

from saneless.config import is_placeholder_token
from saneless.vocabulary import (
    ConnectionStatus,
    ProfileStorage,
    connection_status_message,
)

if TYPE_CHECKING:
    import threading
    from collections.abc import Iterable

    from saneless.config import Settings
    from saneless.paperless import PaperlessClient
    from saneless.scanner.base import DeviceInfo, ScannerBackend

__all__ = [
    "CHECKING_GLYPH",
    "CHECKING_MESSAGE",
    "CHECKING_STATE_CLASS",
    "CHECKING_STATE_LABEL",
    "POLL_ATTEMPT_CAP",
    "POLL_GAVE_UP_LINE",
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


# How long a saned pre-probe waits before giving up on a configured host.  It
# is a deadline, not a per-socket timeout: every address the host resolves to
# is dialled, and all of them together get this much.  So it bounds one
# configured host, and nothing else.  Name resolution runs before it and is
# not inside it -- `getaddrinfo` takes no timeout, so an unreachable resolver
# costs whatever `resolv.conf` says -- and with N configured hosts the probe's
# worst case is N times (resolution plus this budget).  The bound still
# matters for the reason it always did: what it
# replaces is `get_devices()`, which has no timeout at any layer and costs
# roughly 127 s for a silently unreachable host, because Linux retries a SYN
# six times by default, inside a blocking C call nothing can interrupt.  Read
# at call time, so tests can shorten it.
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

# The only digits this module will read as a number.  ``str.isdigit()`` is not
# that set: it is True for Unicode category ``No`` characters ``int()`` refuses
# outright (U+00B2 SUPERSCRIPT TWO, U+2460 CIRCLED DIGIT ONE) and True for
# non-ASCII decimals ``int()`` accepts but libsane's C-side parsing would read
# as a name (U+0661.. Arabic-Indic).  ASCII decimal is the only reading both
# ends agree on (R3-CR-01).
_ASCII_DIGITS: Final = frozenset(digits)

# The digits an ASCII ``0x``/``0X`` literal may be made of.  glibc reads such a
# segment as a number, so a name may not look like one (R3-WR-01).
_ASCII_HEX_DIGITS: Final = frozenset(hexdigits)

# How many hosts one setting may put in front of the pre-probe (R3-IN-05).
# ``_scanner_preflight`` walks the entries with ``any(...)``, paying an
# unbounded ``getaddrinfo`` plus ``PROBE_CONNECT_SECONDS`` for each, and that
# walk runs inside the ``POST /api/checks/refresh`` request thread.  The
# manual-refresh floor in ``checks_cache.claim_manual_refresh`` bounds the
# *rate* of those requests and says so; it cannot bound the duration of one, so
# without this an uncapped setting is a request that can take minutes.
#
# Four is chosen against the deployment rather than against the clock: this is
# a household appliance bridging scanners to paperless-ngx, and a LAN with more
# than four network scanners is not the machine this project is for.  The cost
# of being wrong about that is small and is the module's usual one -- the fifth
# host onwards loses the pre-probe, not the check.
_MAX_PROBE_HOSTS: Final = 4

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

# How many times the cold-start strip may ask for results before it stops
# asking (IN-07).  The poll's only other terminating condition is results
# landing in the cache, so an appliance whose refresher thread has died -- or
# one where the watch window and scanner-gate contention keep every tick from
# storing -- leaves every open tab asking indefinitely, and this is a machine
# meant to be left open on a tablet in a hallway.
#
# Measured in Chromium before the cap existed: a cold strip on a stopped
# refresher issued 254 requests in six seconds -- 42 a second, eighty-five
# times the "every 2s" the markup advertised.  htmx re-fires ``load`` on
# content it has just swapped in and that body swapped in a copy of itself
# carrying ``load, every 2s``, so the poll ran at the round-trip rate.  The
# same measurement is why the polling body no longer carries ``load``: a cap
# counted in attempts is only a cap in *time* if the interval is real, and at
# 42 requests a second ten attempts would have been a quarter of a second.
#
# Ten attempts at the real two-second interval is about twenty seconds of
# asking, which is roughly twenty refresher ticks (``TICK_SECONDS`` is 1 s) and
# about two and a half times the worst probe budget a cold start can cost --
# ``PROBE_CONNECT_SECONDS`` for saned plus ``PROBE_READ_SECONDS`` for
# Paperless.  A healthy cold start settles on its second request; this leaves
# it eight it will never need.
POLL_ATTEMPT_CAP: Final = 10

# What the strip says once it has stopped asking.  It replaces the freshness
# line, because there is no freshness to report -- nothing has ever been
# checked -- and it lives here rather than in the template for the same reason
# ``CHECKING_MESSAGE`` does: templates own no vocabulary.  It names the button
# that is still on the page and nothing else: no path, no URL, no host and no
# exception text, because this sentence is rendered on a page the whole LAN can
# read.
POLL_GAVE_UP_LINE: Final = "The checks have not run yet. Press Check again to try now."

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


def _saned_host_setting(settings: Settings) -> str:
    """
    Return the host list SANE will actually use, not merely the configured one.

    ``_ensure_initialised`` (``sane_backend.py:876-883``) writes
    ``scanner.host`` into ``SANE_NET_HOSTS`` only when the variable is not
    already set, and logs "already set externally … ignoring scanner.host
    config" when it is.  A probe that read the setting alone could therefore
    dial a host SANE is not using: a configured host that is switched off
    would produce a row while the live environment host quietly served
    devices.  Reading the environment first makes the probe's subject and
    SANE's subject the same machine.

    This places no new trust in the variable.  It is already the value libsane
    reads; the alternative is describing a host nothing is dialling.

    The ``or`` rather than a two-argument ``get`` is deliberate: an exported
    but empty variable names no host, and ``sane_backend.py`` only treats a
    non-empty host as a host list, so the setting is what remains.

    Args:
        settings: The injected configuration.

    Returns:
        The colon-separated host list to parse, possibly empty.

    """
    return os.environ.get("SANE_NET_HOSTS") or settings.scanner.host


def _part_is_a_number_to_glibc(part: str) -> bool:
    """
    Say whether one dot-separated part of a segment is a number to glibc.

    glibc's ``inet_aton``-style parsing reads each dot-separated part in its
    own base: a plain run of decimal digits, an ASCII ``0x``/``0X`` hex
    literal, or -- because a leading zero means octal -- ``01`` and ``0755``,
    which are decimal runs as far as *this* function is concerned and are
    caught by the caller's dotted-quad test instead.  Anything holding a
    character outside those forms is a name, because no numeric reading of it
    exists.

    Args:
        part: One dot-separated part of a colon-separated segment.

    Returns:
        True when the part is empty of anything but a number glibc could
        read; False when it is empty, non-ASCII, or holds a letter outside a
        leading hex prefix.

    """
    if not part:
        return False
    # Stated once, for both arms below: a non-ASCII digit is never a number
    # here.  Both character sets happen to be ASCII-only already, so this is
    # belt and braces -- but the rule is the point of the function, and a rule
    # that holds only because of how a constant was spelled is one edit from
    # not holding.
    if not part.isascii():
        return False
    if set(part) <= _ASCII_DIGITS:
        return True
    if part[:2] not in {"0x", "0X"}:
        return False
    after_prefix = part[2:]
    return bool(after_prefix) and set(after_prefix) <= _ASCII_HEX_DIGITS


def _segment_is_a_numeric_address_shorthand(segment: str) -> bool:
    """
    Say whether glibc would resolve this segment as an address nobody typed.

    ``_looks_like_a_host_name`` used to reject a segment only when
    ``segment.isdigit()``, and the hazard it was written for is glibc's
    non-dotted-quad numeric parsing, which accepts far more than all-digit
    strings.  Measured by round 3 on this machine: ``0.0`` and ``0x0.0``
    resolve to ``0.0.0.0``, ``0x7f.1`` and ``127.1`` resolve to ``127.0.0.1``,
    and ``6566.0`` costs one unbounded lookup before NXDOMAIN.  All of them
    passed the old guard because they contain a ``.`` (R3-WR-01).

    On Linux a ``connect()`` to ``0.0.0.0`` reaches loopback, so one of those
    in the dial list lets any unrelated local process listening on 6566 make
    ``any(...)`` true, suppress ``_scanner_host_unanswered`` and let the check
    fall through to ``get_devices()`` -- reinstating the ~127 s uninterruptible
    hang the pre-probe exists to avoid, on an appliance whose configured host
    is in fact dead.

    The rule is in two steps.  If any dot-separated part is not a number glibc
    could read, the segment holds a letter outside a hex prefix and is a name,
    so the answer is False -- which is what keeps ``box-x1.lan`` dialable.
    Otherwise every part is a number, and the stdlib decides: a segment that
    parses as a legal dotted quad is the address the operator configured and
    the answer is False, while one that does not (``0.0``, ``127.1``,
    ``0x0.0``, ``6566.0``, ``01.02.03.04``, ``0xdeadbeef``) is numeric in some
    form nobody typed as an address, and the answer is True.

    Args:
        segment: One stripped segment of the ``scanner.host`` setting.

    Returns:
        True when glibc would read the segment as a number and it is not a
        legal dotted-quad IPv4 literal.

    """
    if not all(_part_is_a_number_to_glibc(part) for part in segment.split(".")):
        return False
    try:
        ipaddress.IPv4Address(segment)
    except ValueError:
        # Numeric, but not a legal literal: one of the shorthands glibc
        # invents an address from.
        return True
    return False


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

    A segment glibc would read as a number is rejected for that reason and not
    for tidiness, and the rule is wider than "all digits" because glibc's
    parsing is.  Measured on this machine, ``getaddrinfo('2001', 6566)``
    answers ``0.0.7.209``, ``getaddrinfo('99999', 6566)`` answers
    ``0.1.134.159``, ``getaddrinfo('0', 6566)`` answers ``0.0.0.0``, and --
    the five forms an all-digit test misses because they carry a ``.`` --
    ``0.0`` and ``0x0.0`` answer ``0.0.0.0``, ``0x7f.1`` and ``127.1`` answer
    ``127.0.0.1``, and ``6566.0`` spends one unbounded lookup before NXDOMAIN
    (R3-WR-01).  ``0.0.0.0`` is the worst of them: on Linux a ``connect()`` to
    it reaches loopback, so such a segment in the dial list lets the probe
    report the configured scanner host "reachable" off any unrelated local
    process listening on 6566 (R2-WR-01, T-30-28-01, T-30-31-02).
    ``_segment_is_a_numeric_address_shorthand`` is what answers the wide
    question; the ``isdigit()`` line below is kept because it only ever
    rejects and is cheaper, and the wider rule subsumes rather than replaces
    it.

    A legal dotted-quad IPv4 literal is accepted, deliberately and by name.
    ``192.0.2.10`` is not a number glibc invented an address from -- it is the
    address the operator configured -- and dialling it is precisely the
    pre-probe's job on a static-IP scanner, worth about 127 s of
    uninterruptible ``get_devices()`` when the appliance is off.

    Args:
        segment: One stripped segment of the ``scanner.host`` setting.

    Returns:
        True when the segment is non-empty, is not a number glibc would
        resolve as an address, is made only of ASCII letters, digits, hyphens
        and dots, and neither starts nor ends with a hyphen or a dot.

    """
    if not segment:
        return False
    if segment.isdigit():
        return False
    if _segment_is_a_numeric_address_shorthand(segment):
        return False
    if segment[0] in "-." or segment[-1] in "-.":
        return False
    return set(segment) <= _HOST_NAME_CHARACTERS


def _looks_like_an_ipv6_literal(host_setting: str) -> bool:
    """
    Say whether the whole setting is one IPv6 address rather than a host list.

    This asks the stdlib, and it asks *before* anything is split on ``:``,
    because splitting is precisely what destroys a literal.  Brackets are
    removed rather than stripped from the ends, so ``[fe80::1]:6566`` reaches
    the parser as ``fe80::1:6566`` -- a legal address, since ``6566`` is legal
    hex -- instead of as a name with a stray ``]`` in it.  Mangling the input
    that way is safe here because the answer is only ever used to *refuse*: a
    false "yes" costs the pre-probe's latency saving, and there is no path from
    this function to an address that gets dialled.

    A zone suffix needs no handling of its own.  ``ipaddress.ip_address`` has
    accepted scoped literals since Python 3.9, and it was verified against the
    interpreter this project pins that ``fe80::1%eth0`` parses as version 6.

    An IPv4 literal answers False deliberately.  Dots are not ambiguous, so
    ``192.0.2.10`` and ``192.0.2.10:6566`` each have exactly one reading and
    the caller's segment rules get them both right.

    Args:
        host_setting: The configured ``scanner.host``, possibly empty.

    Returns:
        True when the setting, with brackets removed, is an IPv6 address.

    """
    try:
        parsed = ipaddress.ip_address(
            host_setting.strip().replace("[", "").replace("]", "")
        )
    except ValueError:
        # Not an address at all: a name, a list of names, or a literal too
        # mangled to parse.  The caller's segment rules are the reading for
        # all three.
        return False
    return parsed.version == 6


def _saned_hosts(host_setting: str) -> tuple[tuple[str, int], ...]:
    """
    Parse ``scanner.host`` into the ``(host, port)`` pairs saned would be dialled on.

    sane-net's own syntax is genuinely ambiguous.  The setting is
    colon-separated when it names several hosts (``sane_backend.py:849``,
    ``sane-net(5)``), *and* ``host:port`` is a legal single entry.  ``a:b`` is
    therefore either two hosts or one host on a port named ``b``, and nothing
    in the string settles it.

    The reading taken here is the narrow one: a trailing segment is a port only
    when the setting has exactly two segments and that segment is ASCII decimal
    digits within 1..65535.  Anything else is a list of host names, minus any
    segment that could not be a name.  It is narrow on purpose -- misreading a
    host as a port would probe the wrong address entirely, and misreading a
    port as a host dials whatever glibc makes of the number, which is why
    neither reading is applied to a segment glibc would read as a number
    (``_segment_is_a_numeric_address_shorthand``).

    A leading zero in the port is read as decimal, not octal: ``host:065``
    yields port 65.  This module does not claim libsane reads it the same way,
    and it is not in a position to: the range test and the ASCII-decimal test
    are the whole of what is guaranteed here, and a spelling whose two ends
    might disagree is one an operator is better off not using.

    The range check is not cosmetic, and it is not a tidiness rule either.  A
    port outside 0..65535 does not fail loudly on the way to a socket: passed
    to ``connect`` it raises ``OverflowError``, which is not an ``OSError`` and
    so is not caught by the probe, and passed to ``getaddrinfo`` it is
    truncated modulo 65536 instead, which means ``host:99999`` would quietly
    dial port 34463 -- a real probe of an address nobody configured.

    Reading such a segment as a host name does *not* avoid that, which is what
    R2-WR-01 established and what this docstring used to claim.  Measured on
    this machine, ``getaddrinfo('99999', 6566)`` answers ``0.1.134.159``:
    glibc's single-integer IPv4 form means the junk dial happens anyway, at a
    different address nobody configured.  The segment is therefore *dropped* --
    ``_looks_like_a_host_name`` refuses all-digit segments and the returned
    tuple is filtered through it -- so ``host:99999`` yields ``host`` alone,
    and dropping is what avoids the dial.

    **The refusal.**  An IPv6 literal is exactly the input where
    "colon-separated list of hosts" and "one address" are indistinguishable, so
    the stdlib is asked first: when the whole setting parses as an IPv6
    address, there are no entries and no probe
    (``_looks_like_an_ipv6_literal``).  That covers the compressed spelling and
    the expanded one alike, which the segment rules alone did not -- before it,
    ``2001:db8:0:0:0:0:0:1`` produced eight entries, five of them ``0``
    (R2-WR-01).

    The segment rules stay on top of it, because the stdlib will not parse
    every spelling an operator can type.  With more than one colon present the
    setting is refused outright unless every segment could be a host name and
    no blank segment sits anywhere but the first or last position.  The blank
    half catches ``[fe80::1]:6566`` if it ever reaches here, and the
    host-name half catches ``[2001:db8:0:0:0:0:0:1]:6566``, which has no blank
    segment at all and whose bracket-stripped form is nine groups the stdlib
    rejects.  A stray colon at either edge is tolerated only when no port is
    present: ``: host-a :`` is one host, but adding a port takes the setting
    past two segments and the refusal above takes the whole thing --
    ``localhost:6566:`` and ``:localhost:6566`` each yield ``()``.  That is the
    safe direction rather than a gap: the refusal costs the pre-probe's latency
    saving and never produces a wrong verdict.

    A fully-qualified name written with its root dot yields ``()`` as well:
    ``scanner.local.`` is legal DNS, and ``_looks_like_a_host_name``'s
    trailing-dot rule rejects it.  This is accepted rather than fixed, because
    widening the accept surface for a spelling that appears nowhere in this
    project's config examples buys nothing, while the cost of leaving it is
    only that one latency saving -- the trade the whole module is built on.

    **The cap.**  At most ``_MAX_PROBE_HOSTS`` -- four -- entries are returned,
    taken from the front of the configured order.  ``_scanner_preflight`` walks
    them with ``any(...)``, paying an unbounded ``getaddrinfo`` plus
    ``PROBE_CONNECT_SECONDS`` for each, inside the ``POST /api/checks/refresh``
    request thread; the manual-refresh floor bounds how often that request may
    be made and not how long one of them takes, so the length of this tuple is
    the only place the duration can be bounded (R3-IN-05).  A longer setting
    loses the pre-probe for its tail rather than losing the bound, which is the
    module's standard safe direction: no entry means no probe for that host,
    and the scanner check falls through to ``get_devices()`` exactly as it did
    before the probe existed.

    Returning ``()`` is not a silent failure; it is the fallback this module
    documents everywhere else.  No entries means no probe, which means the
    scanner check calls ``get_devices()`` and behaves exactly as it did before
    the probe existed.  An operator who typed an IPv6 literal loses the
    pre-probe's latency saving and never gets a wrong verdict, which is the
    trade the whole module is built on.

    Args:
        host_setting: The configured ``scanner.host``, possibly empty.

    Returns:
        One ``(host, port)`` pair per entry that could be a host name, in the
        configured order.  Empty when nothing is configured, every segment is
        blank or was dropped, or the setting is one this module refuses to
        guess at.

    """
    if _looks_like_an_ipv6_literal(host_setting):
        return ()
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
        # The host half is checked too, so ``0:6566`` cannot slip an all-digit
        # host through the one branch that does not reach the filter below.
        #
        # The port half is tested against ASCII decimal rather than with
        # ``str.isdigit()``, which is True for Unicode category ``No``
        # characters ``int()`` refuses -- U+00B2 SUPERSCRIPT TWO, U+2460
        # CIRCLED DIGIT ONE -- and True for non-ASCII decimals ``int()``
        # accepts but libsane's C-side parsing would read as a name.  The
        # first class raised a ``ValueError`` out of this function and turned
        # the Scanner row into ``run_checks``' generic failure row; the second
        # derived a port number from a string libsane never would (R3-CR-01).
        # ASCII decimal is the only reading both ends agree on.  The emptiness
        # test is not redundant: ``set("") <= _ASCII_DIGITS`` is True where
        # ``"".isdigit()`` was False, and ``int("")`` raises.
        if (
            _looks_like_a_host_name(host)
            and maybe_port
            and set(maybe_port) <= _ASCII_DIGITS
            and 0 < int(maybe_port) <= 65535
        ):
            return ((host, int(maybe_port)),)
    # Filter first, then cap: the cap is on entries that would really be
    # dialled, not on segments that were dropped before anything reached a
    # socket.
    return tuple(
        (host, SANED_PORT) for host in present if _looks_like_a_host_name(host)
    )[:_MAX_PROBE_HOSTS]


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

    **What the budget covers.**  Every address the name resolved to, in
    resolver order, and all of them together.  The budget is a *deadline*,
    read once before the walk rather than handed to each socket, so every
    attempt gets only what the attempts before it left over and a host with
    three addresses costs no more than a host with one.  That is the property
    WR-02 asked for, and it is kept.

    What WR-02's fix did instead was dial only the resolver's first answer,
    and that was a strict regression, for a reason that lives in the resolver
    rather than in the handshake.  ``getaddrinfo`` is called with no
    ``AI_ADDRCONFIG``, so glibc returns AAAA records even on a host with no
    IPv6 route, and RFC 6724 orders the IPv6 address first -- measured on this
    machine, ``localhost`` resolves to ``::1`` and then ``127.0.0.1``.  saned
    commonly binds v4-only.  So the justification offered for one address --
    "the answer for a host that is switched off is the same on every address
    it has" -- was true of the case that does not matter and false of the one
    that does: a host that is *on*, answering over one family and not the
    other, was reported dead, permanently, on an appliance that scans
    perfectly well (R2-CR-01).

    Resolution itself is outside the budget and is left that way deliberately.
    ``getaddrinfo`` takes no timeout, so bounding it means running it on a
    thread and abandoning the thread when the deadline passes.  That is the
    same trade plan 30-21's decision record refuses for the enumeration: a
    thread parked in a C call that nothing can interrupt is worse than a slow
    answer, because it is still in there after the caller has moved on.  An
    unreachable resolver therefore costs whatever ``resolv.conf`` says, and
    that is stated rather than claimed away.

    **Failure policy.**  This returns ``False`` for every ``OSError`` --
    refused, timed out, unresolvable, no route -- and raises nothing.  An
    empty resolver answer is covered by the same promise without a guard of
    its own: the walk holds no subscript, so nothing to dial is a loop body
    that never runs.  Subscripting the resolver's first answer raised
    ``IndexError`` there, which is not an ``OSError`` and so escaped into
    ``run_checks``' generic red row (R2-IN-01).  It is
    the *caller* that decides what a ``False`` means, and the caller never
    turns "the probe could not be run at all" into a bad row: when
    ``_saned_hosts`` yields no entries to dial, no probe happens and the
    scanner check falls back to ``get_devices()``, which is exactly the
    behaviour that existed before this module.  A probe that actually ran and
    was refused on every configured entry produces ``WARN``, not ``FAIL`` --
    it establishes that the configured host did not answer, which is not the
    same claim as "there is no scanner" (CR-02, ``_scanner_host_unanswered``)
    -- and in that state ``get_devices()`` would spend two minutes reaching a
    less useful version of the same observation.

    Args:
        host: The host name or address to dial.
        port: The TCP port to dial.
        timeout: How long to wait for the handshake, in seconds.

    Returns:
        True when the connection was established, False otherwise.

    """
    try:
        candidates = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        # Logged at DEBUG, not WARNING: a name that does not resolve is the
        # ordinary state of an appliance whose scanner host is switched off or
        # misspelled, and the row the caller renders is where an operator is
        # told about it.  ``type(exc).__name__`` only -- no host, no address
        # and no exception text reaches a log line or a row (ASVS V7).
        logger.debug("saned pre-probe did not resolve: %s", type(exc).__name__)
        return False
    deadline = monotonic() + timeout
    for family, socket_type, protocol, _canonical_name, address in candidates:
        remaining = deadline - monotonic()
        if remaining <= 0:
            # The budget is spent, so the addresses left over do not get one.
            return False
        try:
            with socket.socket(family, socket_type, protocol) as probe:
                probe.settimeout(remaining)
                probe.connect(address)
                return True
        except OSError as exc:
            # One address refusing says nothing about the next one: this is
            # exactly the dual-stack case where the IPv6 address the resolver
            # put first has no listener and the IPv4 address does.
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


def _scanner_host_unanswered() -> CheckResult:
    """
    Build the "the configured scanner host did not answer" row (CR-02).

    Amber, not red, and the distinction is the whole point.  What the
    pre-probe observed is a fact about the *configured host*, not about the
    appliance: ``SANE_NET_HOSTS`` **adds** net devices to what the dll backend
    enumerates, it does not replace local backend enumeration
    (``sane_backend.py:876`` only sets the variable), so "every configured
    sane-net host refused TCP" never implied "there is no scanner".  A machine
    with a working USB scanner and a switched-off network one scans perfectly,
    and D-01 calls a true statement about a deployment that still works amber
    -- the same shape as D-22's read-only-configuration row.  Reporting it red
    would break the rule ``CheckState``'s own docstring states outright: a
    healthy appliance must never go red.

    The cost is recorded rather than hidden.  An appliance whose *only*
    scanner is an unreachable network host now reports amber, so ``saneless
    doctor`` exits 0 for it.  That is accepted: D-01 already keys a scripted
    gate on red alone, the row is still visible, it still names the scanner
    host as the thing that did not answer, and it still carries the same next
    step, so a human loses nothing.  Getting the red back means bounding
    ``get_devices()`` on a second thread, which cannot be done safely while
    ``sane_get_devices`` is uninterruptible -- the reasoning is in plan
    30-21's decision record and should be read before anyone tries.

    Neither string names the host, its address or its port.  A LAN address on
    a LAN-visible page is the same class of disclosure as the SANE device id
    ``_device_label`` refuses to print (ASVS V7).

    Returns:
        The amber Scanner row, with ``skipped`` false -- the probe was run,
        and it answered.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.WARN,
        message=(
            "The configured scanner host is not answering, "
            "so the scanner could not be checked."
        ),
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


def _scanner_busy() -> CheckResult:
    """
    Build the row shown when something else held the scanner gate (R2-WR-02).

    This exists separately from ``_scanner_skipped`` because the distinction is
    the whole of the fix.  ``run_checks`` honours ``context.skip_scanner``
    *before* the gate is consulted, so by the time a non-blocking acquire fails
    a running scan has already been excluded: whatever holds the gate is not a
    scan, and a row that says one is running is simply false.  Today there is
    one known contender, and it is not hypothetical --
    ``ScanWorker._read_generated_profiles`` takes the gate around
    ``get_devices()`` and ``get_capabilities()`` as the worker thread's first
    act at startup (``worker.py:947``), while ``_current_job_id`` is still
    ``None`` (it is set at ``worker.py:1394``).  The lifespan starts the worker
    and then the refresher, so that window coincides exactly with the
    cold-start poll -- which is how ``_scanner_skipped``'s sentence came to sit
    beside a last-checked time on an appliance that had never scanned.

    The state is ``OK`` for the same reason ``_scanner_skipped``'s is, restated
    because it is easy to read as a bug: "we did not look" is a fact about the
    probe, not a verdict about the appliance, and a scripted health gate keyed
    on red (D-01) must not fail because two threads wanted the scanner in the
    same instant.  The ``skipped`` flag discloses that nothing was checked.

    There is deliberately **no** next step.  ``_scanner_skipped`` carries none
    either, and for the same reason: the next probe fixes this by itself,
    within one refresh interval, so telling a household member to do something
    would be asking them to act on a condition that is already clearing.

    The message names no scan, and it names no host, address, port, path or
    exception either (ASVS V7), which is the same omission
    ``_scanner_host_unanswered`` makes on purpose.

    Returns:
        The neutral contention row, ``skipped`` true because no probe ran.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.OK,
        message="The scanner was busy, so it was not checked this time.",
        skipped=True,
    )


def _scanner_support_missing() -> CheckResult:
    """
    Build the "there is no python-sane on this machine" row (Amendment A-1).

    Its own constructor because two callers need the same row and a row
    written twice is a row that can drift.

    Returns:
        The UI-SPEC S1 no-scanner-support row.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.FAIL,
        message="Scanner support is not installed on this machine.",
        next_step="Install saneless with scanner support, then restart it.",
    )


def _scanner_preflight(context: CheckContext) -> CheckResult | None:
    """
    Decide the scanner row without entering SANE, or say the backend is needed.

    This is everything the scanner check can settle before libsane is touched,
    and it is a separate function so it can run with the worker's scanner gate
    **free** (R2-IN-03).  Nothing here is SANE work: it is a settings read, a
    name resolution and a TCP handshake.  That matters because resolution is
    outside every budget this module states -- ``getaddrinfo`` takes no
    timeout, as ``PROBE_CONNECT_SECONDS`` says at length -- so a check holding
    the gate across it can park a ``ScanWorker._scan_job`` whose job row
    already reads ``SCANNING`` for as long as a broken resolver takes, and
    ``POST /api/checks/refresh`` can re-arm that parking every couple of
    seconds.

    The order inside it is the order ``_check_scanner`` always had.  A machine
    with no python-sane is its own row (Amendment A-1) and is decided without
    touching anything.  The host SANE will actually dial is then pre-probed,
    and every configured entry refusing a TCP connection ends the check right
    there, with the amber ``_scanner_host_unanswered`` row: ``get_devices()``
    would spend about two minutes reaching a conclusion inside a C call
    nothing can interrupt (T-30-22), and the conclusion it would reach is not
    the one the probe is entitled to report.  A refused dial says the
    configured host did not answer; it does not say there is no scanner,
    because ``SANE_NET_HOSTS`` adds net devices rather than replacing local
    enumeration (CR-02).

    Args:
        context: The injected dependencies and configuration.

    Returns:
        The row, when it can be decided here; ``None`` to mean "go and
        enumerate", which is the case where there is no host to probe or one
        of them answered.

    """
    if context.scanner is None:
        return _scanner_support_missing()
    entries = _saned_hosts(_saned_host_setting(context.settings))
    if entries and not any(
        _saned_reachable(host, port, PROBE_CONNECT_SECONDS) for host, port in entries
    ):
        return _scanner_host_unanswered()
    return None


def _scanner_enumeration(context: CheckContext) -> CheckResult:
    """
    Ask the backend what it can see, which is the part that enters SANE.

    This is the region the scanner gate exists to make exclusive, and the only
    region: whatever the enumeration reports stands, red included, because an
    enumeration that actually ran has earned its verdict.  It is also the
    branch that cannot produce a false row, because a host setting this module
    cannot parse into an entry leaves the check behaving exactly as it did
    before the pre-probe existed.

    Args:
        context: The injected dependencies and configuration.

    Returns:
        Exactly one result for ``CheckKey.SCANNER``.

    """
    scanner = context.scanner
    if scanner is None:
        # Unreachable through both real callers: each runs the preflight
        # first, and the preflight answers this case itself.  Re-narrowing it
        # here is how the type checkers learn that, without a cast and without
        # a suppression, and it returns the one shared row rather than a
        # second copy of it.
        return _scanner_support_missing()
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


def _check_scanner(context: CheckContext) -> CheckResult:
    """
    Report whether a scanner is there to scan with.

    The ungated path: this is what ``_dispatch`` calls, and therefore what
    ``saneless doctor`` runs.  It is the preflight followed by the
    enumeration, in that order, which is the order this check has always had
    -- the split changed where the gate sits, not what any caller sees.

    ``if pre is not None`` rather than a truthiness shortcut: ``CheckResult``
    is a frozen dataclass and therefore always truthy, so ``pre or
    _scanner_enumeration(context)`` would read as a bug even though it would
    work.

    Args:
        context: The injected dependencies and configuration.

    Returns:
        Exactly one result for ``CheckKey.SCANNER``.

    """
    pre = _scanner_preflight(context)
    if pre is not None:
        return pre
    return _scanner_enumeration(context)


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


def _scanner_result(context: CheckContext, scanner_gate: threading.Lock) -> CheckResult:
    """
    Run the scanner check, holding the worker's gate for the part that needs it.

    This is the only place in the registry that takes the gate, because the
    enumeration is the only thing any check does inside libsane.  What changed
    with R2-IN-03 is the size of the gated region: the pre-probe runs *first*,
    with the gate free, and only ``_scanner_enumeration`` is held.  The
    pre-probe is a name resolution and a TCP handshake, and resolution is
    outside every budget this module states, so holding the gate across it
    could park a ``ScanWorker._scan_job`` whose job row already reads
    ``SCANNING`` for as long as a broken resolver takes.  A pre-probe that
    settles the row -- no python-sane, or every configured host refusing --
    therefore never touches the gate at all.

    The attempt is non-blocking and a failure is reported rather than waited
    out, which is the single move ``ScanWorker.scanner_gate`` documents as
    permitted: blocking here would queue behind a scan that can legitimately
    run for minutes and then enter SANE at some arbitrary later moment.  A
    failed acquire is ``_scanner_busy()`` and not ``_scanner_skipped()``: the
    latter names a running scan, and ``run_checks`` has already dealt with that
    case before this function is reached, so the only thing a lost gate
    establishes is that somebody else is in SANE -- today, the worker's startup
    capability read, which runs before any job exists (R2-WR-02).

    The release is in a ``finally``, so a check that raises still hands the
    scanner back before the exception reaches ``run_checks``' per-check
    handler.  A registry that left the gate held would lock the worker out of
    its own scanner for the life of the process.

    What the split costs is worth recording: this function no longer routes
    through ``_dispatch``, so the uniform-dispatch seam no longer guarantees
    that the gated path and ``saneless doctor``'s ungated ``_check_scanner``
    agree.  A test guarantees it instead --
    ``test_a_gated_run_returns_what_an_ungated_run_returns`` in
    ``tests/test_checks.py`` -- and that is where anyone changing either half
    should look.

    Args:
        context: The injected dependencies and configuration.
        scanner_gate: The worker's gate, tried without blocking.

    Returns:
        The scanner row, or the neutral busy row when the gate was not free.

    """
    pre = _scanner_preflight(context)
    if pre is not None:
        return pre
    if not scanner_gate.acquire(blocking=False):
        return _scanner_busy()
    try:
        return _scanner_enumeration(context)
    finally:
        scanner_gate.release()


def run_checks(
    context: CheckContext, *, scanner_gate: threading.Lock | None = None
) -> tuple[CheckResult, ...]:
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
    is a second caller into the same C library (Pitfall 2).  It is honoured
    *first*, before the gate is looked at: a caller that already knows a scan
    is running has no reason to touch the gate at all.

    The gate is a parameter rather than something the caller holds around this
    call, and that is the whole of WR-03's fix.  Only ``_check_scanner`` enters
    libsane.  ``_check_paperless`` carries a multi-second HTTP budget, and
    ``_check_fallback`` and ``_check_data_dir`` each create and delete a real
    file.  A caller that wrapped all five made the lock that exists to keep two
    callers out of libsane into the lock a scan start waits on: ``ScanWorker``
    would sit in ``with self._scanner_gate:`` with the job row already written
    ``SCANNING`` while a health probe waited on a Paperless timeout.  Passing
    the gate in lets the registry hold it for the one check that needs it.

    The attempt on the gate is non-blocking and a failure produces the paused
    row, which is the one move ``ScanWorker.scanner_gate`` documents as
    permitted for a caller.  A blocking acquire would be wrong here for the
    reason that docstring gives: the probe would queue behind a scan that can
    run for minutes and then enter SANE with the freshness its own caller
    assumed long gone.

    A check that raises is caught and rendered as a red row with a
    developer-constant message.  A registry that could raise would take the
    whole strip down and with it the four checks that were fine, and the
    exception text is exactly the thing that must not reach a LAN-visible page.
    That handler covers the gated scanner branch too, and the gate is released
    on the way out of it.

    Args:
        context: The injected dependencies and configuration.
        scanner_gate: The worker's scanner gate, held around the scanner check
            and nothing else, or ``None`` for a caller with no worker to
            exclude -- which is what ``saneless doctor`` is.

    Returns:
        One result per ``CheckKey`` member, in member order.

    """
    results: list[CheckResult] = []
    for key in CheckKey:
        if key is CheckKey.SCANNER and context.skip_scanner:
            results.append(_scanner_skipped())
            continue
        try:
            if key is CheckKey.SCANNER and scanner_gate is not None:
                results.append(_scanner_result(context, scanner_gate))
            else:
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
