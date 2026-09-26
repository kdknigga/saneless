"""
The one health-check registry, and the words both surfaces use to show it.

This is not a leaf module -- it needs ``Settings``, a scanner backend and a
Paperless client to answer anything -- but it has a leaf's import rule all the
same, and the rule is the point of the module.  ``saneless doctor`` and the web
status strip are required to report the *same* checks in the *same* words, so
this module must import **nothing from ``saneless.web`` and nothing from
``saneless.cli``**.  If it imported either, it would belong to that
surface, and the other one would end up building a web application to print a
terminal table or importing Click to render a page.

Every dependency is injected instead: the settings, the scanner backend, the
Paperless client and the worker's profile-storage outcome all arrive as
parameters on ``CheckContext``.  That is also what lets ``doctor`` run on a
machine with no python-sane at all -- it passes ``scanner=None`` and still
reports all six rows.

ASVS V7 applies to every string this module can render.  No message and no next
step carries a filesystem path, a URL, a token value or exception text.  The
Paperless URL says where paperless-ngx runs (a user name or password in it is
refused when the config loads) and the fallback folder is a host path, both on
a LAN-visible page, so both are deliberately omitted, exactly as the log
file's path is.

There is exactly one exception, and it is this narrow: the Configuration row's
*next step*, in the two states where a file under the superseded name was
found, names that file -- as one of three fixed documented spellings (``./``,
``$XDG_CONFIG_HOME/saneless/`` or ``/etc/saneless/`` followed by the old
name), never as a resolved host path.  Renaming that exact file is the fix, so
a row that would not name it could not be acted on; and because the spelling
comes from the search *position* rather than from the path, what reaches the
page is a constant this module could have hard-coded.  Every other row, every
message, and every other state stays under the rule above.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import socket
import struct
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from string import ascii_letters, digits, hexdigits
from time import monotonic
from typing import TYPE_CHECKING, Final, assert_never

import httpx2

from saneless.config import (
    CONFIG_FILENAME,
    LEGACY_CONFIG_FILENAME,
    config_file_state,
    is_placeholder_token,
)
from saneless.vocabulary import (
    ConfigFileState,
    ConnectionStatus,
    ProfileStorage,
    connection_status_message,
)

if TYPE_CHECKING:
    import threading
    from collections.abc import Iterable
    from pathlib import Path

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
    "POLL_PROBE_ATTEMPT_CAP",
    "POLL_STILL_CHECKING_LINE",
    "PROBE_CONNECT_SECONDS",
    "PROBE_HANDSHAKE_SECONDS",
    "PROBE_READ_SECONDS",
    "SANED_PORT",
    "SKIPPED_STATE_LABEL",
    "CheckContext",
    "CheckKey",
    "CheckResult",
    "CheckState",
    "check_name",
    "check_row_class",
    "check_row_glyph",
    "check_row_label",
    "check_state_class",
    "check_state_glyph",
    "check_state_label",
    "configuration_check",
    "run_checks",
    "worst_state",
]

logger = logging.getLogger(__name__)


# How long a saned pre-probe waits before giving up on a configured host.  It
# is a deadline, not a per-socket timeout: every address the host resolves to
# is dialled, and all of them together get this much.  So it bounds one
# configured host, and nothing else.  Name resolution runs before it and is
# not inside it -- `getaddrinfo` takes no timeout, so an unreachable resolver
# costs whatever `resolv.conf` says.  Once a connect succeeds, the handshake
# has ``PROBE_HANDSHAKE_SECONDS`` of its own, so one host's worst case is
# resolution plus this budget plus that one, and every configured host is
# probed -- up to ``_MAX_PROBE_HOSTS`` of them.  The bound still
# matters for the reason it always did: what it
# replaces is `get_devices()`, which has no timeout at any layer and costs
# roughly 127 s for a silently unreachable host, because Linux retries a SYN
# six times by default, inside a blocking C call nothing can interrupt.  Read
# at call time, so tests can shorten it.
PROBE_CONNECT_SECONDS: Final = 2.0

# How long a saned pre-probe waits for saned's answer once a connection is
# made.  It is started after the connect succeeds and is separate from
# ``PROBE_CONNECT_SECONDS`` for one reason: saned does name lookups before it
# replies -- a reverse lookup of the peer, a lookup of its own host name, and
# one per name line in its ``saned.conf`` -- so on a scanner host with slow DNS
# a perfectly working saned can take seconds to say hello.  One short budget
# over connect and reply would call that host timed out and keep it away from
# enumeration.  Read at call time, so tests can shorten it.
PROBE_HANDSHAKE_SECONDS: Final = 5.0

# How long the Paperless probe waits for a response body once connected.  The
# client's own default is a flat 30 s, which is the right budget for an upload
# and the wrong one for a health row.  Read at call time.
PROBE_READ_SECONDS: Final = 5.0

# saned's registered port.  IANA names 6566 ``sane-port``, and this machine's
# ``/etc/services`` agrees.  Read at call time.
SANED_PORT: Final = 6566

# The SANE network protocol, as far as the pre-probe speaks it.  Every word is
# four big-endian bytes (``sanei_codec_bin.c`` in sane-backends).  Procedure 0
# is ``SANE_NET_INIT`` and procedure 10 is ``SANE_NET_EXIT``, which saned
# answers by ending the session without a reply.  The version sent is
# ``SANE_VERSION_CODE(1, 0, 3)``, what libsane 1.0.32 sends; saned does not read
# it.
_SANE_NET_INIT: Final = 0
_SANE_NET_EXIT: Final = 10
_SANE_VERSION_CODE: Final = 0x01000003

# The user name the probe introduces itself with.  Fixed rather than the local
# login name, which saned would log on the scanner host and which is nobody
# else's business; saned logs the session as ``saneless@<address>``.
_PROBE_USER: Final = b"saneless"

# A ``SANE_NET_INIT`` reply is a status word and a version word and nothing
# else, so the probe never reads more than this from a peer.
_INIT_REPLY_LENGTH: Final = 8

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
# ends agree on.
_ASCII_DIGITS: Final = frozenset(digits)

# The digits an ASCII ``0x``/``0X`` literal may be made of.  glibc reads such a
# segment as a number, so a name may not look like one.
_ASCII_HEX_DIGITS: Final = frozenset(hexdigits)

# How many hosts one setting may put in front of the pre-probe.
# ``_scanner_preflight`` probes every entry, paying an unbounded
# ``getaddrinfo`` plus ``PROBE_CONNECT_SECONDS`` plus
# ``PROBE_HANDSHAKE_SECONDS`` for each, and those probes run inside the
# ``POST /api/checks/refresh`` request thread.  The
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

# The cold-start row, before any check has run.  It lives here rather
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

# The word a screen reader hears in front of a row nothing looked at.  A
# skipped row borrows the cold-start glyph and the cold-start
# colour -- U+00B7 says "not yet", not "bad", and no new colour token is needed
# -- but it deliberately does not borrow the cold-start *word*.
# ``CHECKING_STATE_LABEL`` is "Checking", and a listener hearing "Checking:
# Scanner. Not checked while a scan is running." is told a probe is running
# when none is: a smaller version of the same lie the green tick told before
# this constant existed.  It is a word rather than the glyph for the reason
# ``CHECKING_STATE_LABEL`` is one -- the glyph is ``aria-hidden``, so the
# marker's meaning only survives as speech if something spells it out.
SKIPPED_STATE_LABEL: Final = "Not checked"

# How many times the cold-start strip may ask for results before it stops
# asking.  The poll's only other terminating condition is results
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
# asking, which is roughly twenty refresher ticks (``TICK_SECONDS`` is 1 s).
# That is *not* sized against the worst probe a cold start can cost, and the
# arithmetic that once claimed it was -- two and a half times
# ``PROBE_CONNECT_SECONDS`` plus ``PROBE_READ_SECONDS``, so about 7 s -- is
# disproved by three things this same module documents.
# ``getaddrinfo`` is outside every budget here: ``PROBE_CONNECT_SECONDS`` says
# at length that an unreachable resolver costs whatever ``resolv.conf`` says,
# and nothing bounds it.  The saned pre-probe pays that once per configured
# host, up to ``_MAX_PROBE_HOSTS`` of them.  And the ordinary local-USB
# deployment has no parseable host at all, so ``_scanner_preflight`` hands
# over to enumeration without dialling anything and ``_scanner_enumeration``
# enters ``get_devices()``, which this file costs at roughly 127 s for a silently
# unreachable host, inside a blocking C call nothing can interrupt.
#
# So what this cap means is narrower than it used to claim: ten attempts is the
# bound for a chain with **nothing in flight**, which is the case it was always
# really sized for -- a refresher thread that has died, and a tab left open in
# front of it.  A chain that is waiting on a probe somebody demonstrably holds
# is bounded by ``POLL_PROBE_ATTEMPT_CAP`` instead.  A healthy cold start
# settles on its second request either way; this leaves it eight it will never
# need.
POLL_ATTEMPT_CAP: Final = 10

# How many times a strip may ask while a probe is demonstrably in flight --
# that is, while some checker holds the refresher's single-flight lock.
# Ninety attempts at the real two-second interval is about 180 s.  The two
# slow paths a first check can take are either/or, not both: with no host to
# probe, ``get_devices()`` can cost the ~127 s this file documents; with
# hosts, the saned pre-probe costs at most ``PROBE_CONNECT_SECONDS`` plus
# ``PROBE_HANDSHAKE_SECONDS`` -- 7 s -- for each of up to ``_MAX_PROBE_HOSTS``,
# so 28 s, and a host that could hang enumeration never reaches it.  Either
# path plus ``PROBE_READ_SECONDS`` for Paperless leaves room for the
# pre-probe's unbounded resolutions.  Below it, a cold start on a wedged scanner stopped
# asking while its first probe was still running and told a household member to
# press a button that starts the thing already running.
#
# It is a second cap and not an exemption, and that is deliberate.
# ``Lock.locked()`` stays true forever if the holder dies, and a thread that
# died inside the lock is precisely the failure mode this area keeps hitting,
# so an unbounded "keep asking while the lock is held" would reintroduce the
# endless poll ``POLL_ATTEMPT_CAP`` exists to stop, through a narrower door.  A
# refresher that died holding the lock therefore still makes the asking stop;
# it just takes about
# three minutes instead of twenty seconds.  That cost lands only on the tab
# that was already waiting on a probe, never on the healthy-idle case.
POLL_PROBE_ATTEMPT_CAP: Final = 90

# What the strip says once it has stopped asking.  It replaces the freshness
# line, because there is no freshness to report -- nothing has ever been
# checked -- and it lives here rather than in the template for the same reason
# ``CHECKING_MESSAGE`` does: templates own no vocabulary.  It names the button
# that is still on the page and nothing else: no path, no URL, no host and no
# exception text, because this sentence is rendered on a page the whole LAN can
# read.
POLL_GAVE_UP_LINE: Final = "The checks have not run yet. Press Check again to try now."

# What the strip says instead, while it is still asking because a probe is
# demonstrably in flight.  ``POLL_GAVE_UP_LINE`` points at the
# ``Check again`` button, and beside a running probe that is advice that cannot
# help: the click collapses into the probe already running.  So this sentence
# says what is true -- the first check has not finished -- and sets an
# expectation for how long that can take, which is the ~127 s ``get_devices()``
# figure rounded into words a household member reads.  Held to exactly
# ``POLL_GAVE_UP_LINE``'s rule: no host, no address, no port, no path, no URL
# and no exception text, because it renders on a page the whole LAN can read
# (ASVS V7).
POLL_STILL_CHECKING_LINE: Final = (
    "The first check is still running. This can take a couple of minutes "
    "if the scanner is not reachable."
)

# How much of a device's own description a row will print.  Nothing else bounds
# what a scanner can call itself, and the row is rendered into HTML next to
# four others whose width is fixed.
_DEVICE_LABEL_MAX_LENGTH: Final = 60

# The row a check that raised is rendered as.  Developer constants, because the
# exception that produced them is exactly the thing that must not reach the
# page.
_CHECK_FAILED_MESSAGE: Final = "This check could not be completed."
_CHECK_FAILED_NEXT_STEP: Final = "Restart saneless, then press Check again."


class CheckState(StrEnum):
    """
    How one health check came out: three states, and only three.

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
    The six checks, and the contract that both surfaces show all six.

    This enum *is* the "neither surface may define a check the other does not
    have" rule.  ``run_checks`` iterates it and returns one result per member,
    ``saneless doctor`` prints them in member order and the status strip
    renders them in member order, so there is no list of checks anywhere else
    to drift out of step with this one.

    ``CONFIGURATION`` is first because member order is reading order, and it
    is the row that can explain the others.  A missing or unreadable
    configuration file turns the Paperless, Profiles and Fallback rows red or
    amber at once, each of them reporting a symptom truthfully and none of
    them naming the cause; a reader who meets the cause first has the other
    rows explained before reaching them, and one who meets it last has already
    drawn three wrong conclusions.

    Adding a seventh member is therefore a deliberate act with a visible cost:
    ``check_name`` stops type-checking until the new key has a name, the
    dispatch in ``run_checks`` stops type-checking until it has a check
    function, and the completeness test in ``tests/test_checks.py`` fails until
    the count is updated.

    ``DATA_DIR`` is the member name, and ``"Data folder"`` is what a household
    member reads.  The member names are internal; only ``check_name`` is user
    copy.
    """

    CONFIGURATION = "CONFIGURATION"
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
    ``WARN`` and ``FAIL`` row, because a red row that does not say what to do
    about it is a red row a household member can only escalate.

    ``skipped`` is the "not checked while a scan is running" row.  It is a
    separate flag rather than a fourth state because the row still has to carry
    *some* state for its glyph, and "we did not look" is a fact about the
    probe, not a verdict about the appliance.

    Attributes:
        key: Which of the six checks this is.
        state: How it came out.
        message: A developer-authored sentence.  Never a path, a URL, a token
            or exception text (ASVS V7).  The module docstring states the one
            exception: the Configuration row's ``next_step``, in its two
            superseded-name states, carries one of three fixed documented
            spellings of the file to rename -- never a resolved host path, and
            never in ``message``.
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
    Everything the six checks need, handed in rather than reached for.

    Every dependency is injected because the two surfaces build them
    differently, and neither may be the one this module knows about.
    The web app has a worker, a long-lived Paperless client and a scanner
    backend it opened at startup; ``saneless doctor`` has none of those and
    builds what it needs for one command.

    ``scanner=None`` is how "python-sane is not installed on this machine" is
    represented.  That is what lets ``doctor`` report all six rows on such a
    machine instead of refusing at ``require_sane()`` and reporting none --
    the thing an operator most needs a diagnostic for is the
    machine where the diagnostic would otherwise not run.

    ``paperless=None`` means no usable client could be built at all:
    ``PaperlessClient.__init__`` refused, for a URL httpx2 will not parse or
    that carries a user name or password, a token an HTTP header cannot
    carry, or a TLS trust store it could not read.

    ``profile_storage`` is the outcome the worker recorded when it wrote the
    generated profiles, not something re-derived here.  ``doctor`` derives its
    own from whether a config file was loaded.  It has to be a record rather
    than a fresh probe, because the two in-memory cases are indistinguishable
    afterwards and the read-only one is the only one worth acting on.

    Attributes:
        settings: The loaded configuration.
        scanner: The scanner backend, or None when there is no SANE support.
        paperless: The Paperless client, or None when none could be built.
        profile_storage: What the profile write actually did.
        skip_scanner: True while a scan is running, which pauses the scanner
            check without touching the backend.

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
    collision.

    Args:
        key: The check to name.

    Returns:
        A short noun phrase, e.g. ``"Data folder"``.

    Raises:
        AssertionError: If the value is not a CheckKey member.

    """
    match key:
        case CheckKey.CONFIGURATION:
            name = "Configuration"
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
    is an alias over an existing colour token; none introduces a new colour
    value.

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


# The three lookups above answer "what does this *state* look like".  The three
# below answer "what does this *row* look like", which is not the same question
# whenever ``skipped`` is set, and it is the second question both surfaces
# actually ask.  ``check_state_*`` stays exactly as it is: it is still the
# state's own vocabulary, it is what these three delegate to, and
# ``saneless doctor``'s ``_state_marker`` is its CLI counterpart.


def check_row_class(result: CheckResult) -> str:
    """
    Return the CSS class that colours one rendered row's glyph.

    A skipped row is drawn in the neutral cold-start colour rather than in its
    state's, because the state on a skipped row is not a verdict anybody
    reached: it is ``CheckState.OK`` so that the row has *some* colour to draw
    and so that a scripted health gate does not go red for a probe that was
    deliberately not taken.  Colouring by it would paint an unprobed row
    green, telling the reader that a probe passed when none ever ran.

    Args:
        result: The finished row about to be rendered.

    Returns:
        ``CHECKING_STATE_CLASS`` when the probe was skipped, otherwise
        ``check_state_class(result.state)``.

    """
    if result.skipped:
        return CHECKING_STATE_CLASS
    return check_state_class(result.state)


def check_row_glyph(result: CheckResult) -> str:
    """
    Return the glyph one rendered row is marked with.

    The skipped glyph is the cold-start one, U+00B7, and for the same reason it
    is the cold-start one: it says "not yet", not "bad".  "We did not look" is
    a fact about the probe, not a verdict about the appliance, and the green
    tick is the one mark that must never stand in front of a sentence saying
    nothing was checked.

    Args:
        result: The finished row about to be rendered.

    Returns:
        ``CHECKING_GLYPH`` when the probe was skipped, otherwise
        ``check_state_glyph(result.state)``.

    """
    if result.skipped:
        return CHECKING_GLYPH
    return check_state_glyph(result.state)


def check_row_label(result: CheckResult) -> str:
    """
    Return the word a screen reader announces before one rendered row.

    The glyph and the colour are borrowed from the cold-start trio; the word is
    not.  ``CHECKING_STATE_LABEL`` would tell a listener a probe is running
    when the whole point of the flag is that none was, so a skipped row gets
    ``SKIPPED_STATE_LABEL`` instead -- the only way the neutral glyph's meaning
    survives for somebody who cannot see it.

    Args:
        result: The finished row about to be rendered.

    Returns:
        ``SKIPPED_STATE_LABEL`` when the probe was skipped, otherwise
        ``check_state_label(result.state)``.

    """
    if result.skipped:
        return SKIPPED_STATE_LABEL
    return check_state_label(result.state)


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
    strings.  Measured on this machine: ``0.0`` and ``0x0.0``
    resolve to ``0.0.0.0``, ``0x7f.1`` and ``127.1`` resolve to ``127.0.0.1``,
    and ``6566.0`` costs one unbounded lookup before NXDOMAIN.  All of them
    passed the old guard because they contain a ``.``.

    On Linux a ``connect()`` to ``0.0.0.0`` reaches loopback, so one of those
    in the dial list lets any unrelated local process listening on 6566 make
    that entry HEALTHY or REJECTED instead of timed out or refused, and let
    enumeration start -- reinstating the ~127 s uninterruptible hang the
    pre-probe exists to avoid, on an appliance whose configured host is in
    fact dead.

    The rule is in two steps.  If any dot-separated part is not a number glibc
    could read, the segment holds a letter outside a hex prefix and is a name,
    so the answer is False -- which is what keeps ``box-x1.lan`` dialable.
    Otherwise every part is a number, and the stdlib decides: a segment that
    parses as a legal dotted quad is the address the operator configured and
    the answer is False, while one that does not (``0.0``, ``127.1``,
    ``0x0.0``, ``6566.0``, ``01.02.03.04``, ``0xdeadbeef``) is numeric in some
    form nobody typed as an address, and the answer is True.

    One legal literal is refused all the same.  ``0.0.0.0`` parses as a dotted
    quad, so the rule above accepted it -- and it is the very address the
    hazard paragraph names, since a ``connect()`` to the unspecified address
    is the loopback dial the shorthands were refused for.  No scanner is ever
    at the unspecified address, so refusing it by name costs nothing and
    closes the one dotted quad the rule left open.

    Args:
        segment: One stripped segment of the ``scanner.host`` setting.

    Returns:
        True when glibc would read the segment as a number and it is either
        not a legal dotted-quad IPv4 literal or the unspecified address
        ``0.0.0.0``.

    """
    if not all(_part_is_a_number_to_glibc(part) for part in segment.split(".")):
        return False
    try:
        address = ipaddress.IPv4Address(segment)
    except ValueError:
        # Numeric, but not a legal literal: one of the shorthands glibc
        # invents an address from.
        return True
    # The one legal literal that is still the hazard this function documents:
    # on Linux a connect() to the unspecified address reaches loopback, so it
    # would report the configured host reachable off any local listener.  It
    # names no scanner, so nothing is lost by refusing it.
    return address.is_unspecified


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
    "yes" costs junk dials and, through the pre-probe, possibly a wrong
    verdict.

    A segment glibc would read as a number is rejected for that reason and not
    for tidiness, and the rule is wider than "all digits" because glibc's
    parsing is.  Measured on this machine, ``getaddrinfo('2001', 6566)``
    answers ``0.0.7.209``, ``getaddrinfo('99999', 6566)`` answers
    ``0.1.134.159``, ``getaddrinfo('0', 6566)`` answers ``0.0.0.0``, and --
    the five forms an all-digit test misses because they carry a ``.`` --
    ``0.0`` and ``0x0.0`` answer ``0.0.0.0``, ``0x7f.1`` and ``127.1`` answer
    ``127.0.0.1``, and ``6566.0`` spends one unbounded lookup before
    NXDOMAIN.  ``0.0.0.0`` is the worst of them: on Linux a ``connect()`` to
    it reaches loopback, so such a segment in the dial list lets the probe
    report the configured scanner host "reachable" off any unrelated local
    process listening on 6566.
    ``_segment_is_a_numeric_address_shorthand`` is what answers the wide
    question; the ``isdigit()`` line below is kept because it only ever
    rejects and is cheaper, and the wider rule subsumes rather than replaces
    it.

    A legal dotted-quad IPv4 literal is accepted, deliberately and by name.
    ``192.0.2.10`` is not a number glibc invented an address from -- it is the
    address the operator configured -- and dialling it is precisely the
    pre-probe's job on a static-IP scanner, worth about 127 s of
    uninterruptible ``get_devices()`` when the appliance is off.  The one
    exception is ``0.0.0.0`` itself: a legal literal, but the very address the
    paragraph above names as the worst case, and one no scanner is ever at, so
    ``_segment_is_a_numeric_address_shorthand`` refuses it by name.

    Args:
        segment: One stripped segment of the ``scanner.host`` setting.

    Returns:
        True when the segment is non-empty, is not a number glibc would
        resolve as an address, is not the unspecified address, is made only of
        ASCII letters, digits, hyphens and dots, and neither starts nor ends
        with a hyphen or a dot.

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

    The setting is colon-separated when it names several hosts
    (``sane-net(5)``), and ``SaneBackend`` hands it to libsane as
    ``SANE_NET_HOSTS`` unchanged.  sane-net(5) documents no port syntax, and
    libsane's net backend splits the variable on every ``:`` and always dials
    saned's registered port, so to libsane ``host:6566`` is two hosts,
    ``host`` and ``6566``.  The two-segment ``host:port`` reading below is this
    module's own, not libsane's.  It is kept because it only ever changes the
    port *this probe* dials -- which is how a test aims the probe at an
    ephemeral loopback port -- and with the registered port it probes exactly
    the host libsane will dial.  With any other port the probe and libsane
    disagree about where saned is, and a setting like that is one an operator
    is better off not using.

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

    Reading such a segment as a host name does *not* avoid that, whatever this
    docstring once claimed.  Measured on
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
    ``2001:db8:0:0:0:0:0:1`` produced eight entries, five of them ``0``.

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
    taken from the front of the configured order.  ``_scanner_preflight``
    probes every one of them, paying an unbounded ``getaddrinfo`` plus
    ``PROBE_CONNECT_SECONDS`` plus ``PROBE_HANDSHAKE_SECONDS`` for each,
    inside the ``POST /api/checks/refresh`` request thread; the manual-refresh floor bounds how often that request may
    be made and not how long one of them takes, so the length of this tuple is
    the only place the duration can be bounded.  A longer setting
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
        # derived a port number from a string libsane never would.
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


class _SanedOutcome(StrEnum):
    """
    What the saned pre-probe learnt about one configured host.

    Five outcomes, each a different thing an operator has to do, which is why
    the probe tells them apart at all:

    - ``UNRESOLVED``: the resolver raised, or answered with no address.
    - ``TIMED_OUT``: nothing connected and at least one address did not
      answer -- a connect timed out, the budget ran out before an address was
      dialled, or the network said there was no route.  A peer that accepted
      the connection and then sent nothing before the handshake deadline is
      counted here too, because it is the peer libsane would wait on forever.
    - ``REFUSED``: every address refused the connection.  The host is up and
      nothing is listening on saned's port.
    - ``REJECTED``: a connection was made and the handshake failed: the peer
      closed or reset it, the reply ended early, or it carried a failure
      status or a protocol version libsane does not speak.
    - ``HEALTHY``: a valid ``SANE_NET_INIT`` reply.

    Private, and every consumer is a total ``match`` ending in
    ``assert_never``, so a sixth outcome stops type-checking until each of
    them decides what it means.
    """

    UNRESOLVED = "UNRESOLVED"
    TIMED_OUT = "TIMED_OUT"
    REFUSED = "REFUSED"
    REJECTED = "REJECTED"
    HEALTHY = "HEALTHY"


def _init_request() -> bytes:
    """
    Build the ``SANE_NET_INIT`` request the probe sends.

    A procedure word, a version word and the user name as a SANE string: a
    word holding the length *including* the trailing NUL, then the bytes.

    Returns:
        The 21-byte request.

    """
    user = _PROBE_USER + b"\0"
    return struct.pack(">iii", _SANE_NET_INIT, _SANE_VERSION_CODE, len(user)) + user


def _recv_exactly(sock: socket.socket, count: int, deadline: float) -> bytes | None:
    """
    Read exactly ``count`` bytes before ``deadline``, and never one more.

    Every ``recv`` asks only for what is still missing, and the socket's
    timeout is reset to what is left of the deadline before each one, so a
    peer that trickles bytes cannot stretch the read past the deadline and a
    peer that floods cannot make the probe take more than it asked for.

    Args:
        sock: The connected socket.
        count: How many bytes to read.
        deadline: The ``monotonic`` reading the read must finish by.

    Returns:
        The bytes, or ``None`` when the peer closed the connection first.

    Raises:
        TimeoutError: When the deadline passes before ``count`` bytes arrive.

    """
    buffer = bytearray()
    while len(buffer) < count:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        sock.settimeout(remaining)
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            return None
        buffer += chunk
    return bytes(buffer)


def _handshake(sock: socket.socket, deadline: float) -> _SanedOutcome:
    """
    Say hello to a connected saned and classify what comes back.

    The request is ``SANE_NET_INIT``; the reply is exactly eight bytes, a
    status word and a version word.  It is accepted on the terms libsane's
    net backend accepts it (``connect_dev`` in ``backend/net.c``): status 0,
    major version 1, build 2 or 3.  After a valid reply the probe sends
    ``SANE_NET_EXIT``, best effort, so saned ends the session cleanly and logs
    one ordinary session per probe rather than an error.

    ``TimeoutError`` is caught before ``OSError``, which it subclasses: a peer
    that accepted the connection and then said nothing is not answering, and
    is not the same finding as one that hung up.  Nothing escapes this
    function; only the outcome and the exception's type name are logged.

    Args:
        sock: A socket whose ``connect`` has just succeeded.
        deadline: The ``monotonic`` reading the reply must arrive by.

    Returns:
        ``HEALTHY``, ``REJECTED`` or ``TIMED_OUT``.

    """
    remaining = deadline - monotonic()
    if remaining <= 0:
        return _SanedOutcome.TIMED_OUT
    try:
        sock.settimeout(remaining)
        sock.sendall(_init_request())
        reply = _recv_exactly(sock, _INIT_REPLY_LENGTH, deadline)
    except TimeoutError:
        logger.debug("saned pre-probe handshake: %s", _SanedOutcome.TIMED_OUT.value)
        return _SanedOutcome.TIMED_OUT
    except OSError as exc:
        logger.debug(
            "saned pre-probe handshake: %s (%s)",
            _SanedOutcome.REJECTED.value,
            type(exc).__name__,
        )
        return _SanedOutcome.REJECTED
    if reply is None:
        logger.debug(
            "saned pre-probe handshake: %s (closed)", _SanedOutcome.REJECTED.value
        )
        return _SanedOutcome.REJECTED
    status, version = struct.unpack(">ii", reply)
    major = (version >> 24) & 0xFF
    build = version & 0xFFFF
    if status != 0 or major != 1 or build not in {2, 3}:
        # The status and version stay out of the log line: they are the
        # peer's words, and only the outcome is this module's.
        logger.debug(
            "saned pre-probe handshake: %s (reply)", _SanedOutcome.REJECTED.value
        )
        return _SanedOutcome.REJECTED
    with contextlib.suppress(OSError):
        sock.sendall(struct.pack(">i", _SANE_NET_EXIT))
    return _SanedOutcome.HEALTHY


def _probe_saned(
    host: str, port: int, connect_timeout: float, handshake_timeout: float
) -> _SanedOutcome:
    """
    Classify one configured saned host by the opening of the SANE handshake.

    There is nowhere else to put a bound.  ``SaneBackend.get_devices()``
    calls into libsane, which has no timeout parameter at any layer -- not in
    python-sane, not in ``sane_get_devices(3)``, and not settable from Python.
    With the ``net`` backend that call opens a TCP connection to each entry of
    ``SANE_NET_HOSTS``, so an unplugged host is a connect that hangs until the
    kernel gives up: on Linux ``tcp_syn_retries`` defaults to 6, roughly 127
    seconds, inside a blocking C call nothing can interrupt.  Dialling the same
    address first, with a timeout, is the only bound available.

    **Why a connect is not enough.**  saned refuses a peer its ``saned.conf``
    does not allow by closing the socket before it reads anything
    (``check_host``, called from ``init`` in ``frontend/saned.c``).  The
    client never gets a status: it gets a reset or an end of file where the
    reply should be.  A probe that stopped at ``connect()`` counted that host
    as reachable, and the row then blamed a scanner that was switched on and
    answering.  Going as far as the ``SANE_NET_INIT`` reply is what tells a
    refusal by the access list apart from a working saned.  It also changes
    what saned logs: a bare connect that hung up made saned log an error for
    every healthy check, while a handshake that ends with ``SANE_NET_EXIT``
    leaves one ordinary session.

    **What the budgets cover.**  The connect budget covers every address the
    name resolved to, in resolver order, and all of them together.  It is a
    *deadline*, read once before the walk rather than handed to each socket,
    so every attempt gets only what the attempts before it left over and a
    host with three addresses costs no more than a host with one.  The
    handshake gets a deadline of its own, started when a connect succeeds,
    because saned does name lookups before it replies
    (``PROBE_HANDSHAKE_SECONDS``).

    Every address is tried because of the resolver, not the handshake.
    ``getaddrinfo`` is called with no ``AI_ADDRCONFIG``, so glibc returns
    AAAA records even on a host with no IPv6 route, and RFC 6724 orders the
    IPv6 address first -- measured on this machine, ``localhost`` resolves to
    ``::1`` and then ``127.0.0.1``.  saned commonly binds v4-only, so a probe
    that dialled only the first answer reported a host that is *on*,
    answering over one family and not the other, as dead.  A refused address
    therefore moves the walk on.  The first address that *connects* decides:
    a rejected handshake there is not retried on the next address, which is
    what libsane's ``connect_dev`` does too.

    Resolution itself is outside both budgets, deliberately.  ``getaddrinfo``
    takes no timeout, so bounding it means running it on a thread and
    abandoning the thread when the deadline passes.  That is the same trade
    the scanner check refuses for the enumeration: a thread parked in a C call
    that nothing can interrupt is worse than a slow answer, because it is
    still in there after the caller has moved on.  An unreachable resolver
    therefore costs whatever ``resolv.conf`` says, and that is stated rather
    than claimed away.

    **Failure policy.**  This raises nothing.  Every ``OSError`` becomes an
    outcome, and an empty resolver answer needs no guard of its own: the walk
    holds no subscript, so nothing to dial is a loop body that never runs and
    the answer is ``UNRESOLVED``.  Subscripting the resolver's first answer
    once raised ``IndexError``, which is not an ``OSError`` and so escaped
    into ``run_checks``' generic red row.  Log lines carry the outcome and
    ``type(exc).__name__`` only -- no host, no address, no port and no
    exception text (ASVS V7).

    Args:
        host: The host name or address to dial.
        port: The TCP port to dial.
        connect_timeout: The deadline for connecting, over every address.
        handshake_timeout: The deadline for saned's reply, once connected.

    Returns:
        What the probe learnt about the host.

    """
    try:
        candidates = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        # Logged at DEBUG, not WARNING: a name that does not resolve is the
        # ordinary state of an appliance whose scanner host is switched off or
        # misspelled, and the row the caller renders is where an operator is
        # told about it.
        logger.debug(
            "saned pre-probe: %s (%s)",
            _SanedOutcome.UNRESOLVED.value,
            type(exc).__name__,
        )
        return _SanedOutcome.UNRESOLVED
    deadline = monotonic() + connect_timeout
    unanswered = False
    for family, socket_type, protocol, _canonical_name, address in candidates:
        remaining = deadline - monotonic()
        if remaining <= 0:
            # The budget is spent, so the addresses left over do not get one.
            unanswered = True
            break
        try:
            with socket.socket(family, socket_type, protocol) as probe:
                probe.settimeout(remaining)
                probe.connect(address)
                outcome = _handshake(probe, monotonic() + handshake_timeout)
        except ConnectionRefusedError:
            # One address refusing says nothing about the next one: this is
            # exactly the dual-stack case where the IPv6 address the resolver
            # put first has no listener and the IPv4 address does.
            logger.debug("saned pre-probe: one address refused")
            continue
        except OSError as exc:
            # A timeout, no route, or a socket this machine cannot open for
            # that family.  None of them proves anything is up there.
            unanswered = True
            logger.debug("saned pre-probe did not connect: %s", type(exc).__name__)
            continue
        logger.debug("saned pre-probe: %s", outcome.value)
        return outcome
    if unanswered:
        outcome = _SanedOutcome.TIMED_OUT
    elif not candidates:
        outcome = _SanedOutcome.UNRESOLVED
    else:
        outcome = _SanedOutcome.REFUSED
    logger.debug("saned pre-probe: %s", outcome.value)
    return outcome


@dataclass(frozen=True, slots=True)
class _HostProbe:
    """
    One configured saned host and what its pre-probe found.

    The host is kept only so enumeration can match a configured ``net:``
    device to the host it lives on.  It is never rendered and never logged:
    rows interpolate counts, not hosts (ASVS V7).

    Attributes:
        host: The configured entry that was dialled.
        outcome: What the probe learnt about it.

    """

    host: str
    outcome: _SanedOutcome


@dataclass(frozen=True, slots=True)
class _ScannerPreflight:
    """
    What the gate-free preflight hands to enumeration when it cannot decide.

    Carrying the backend here, already narrowed to not-``None``, is how both
    type checkers learn that enumeration has one without a cast.

    Attributes:
        scanner: The scanner backend enumeration will ask.
        probes: One entry per configured saned host, in configured order;
            empty when no host is configured or none could be parsed.

    """

    scanner: ScannerBackend
    probes: tuple[_HostProbe, ...]


@dataclass(frozen=True, slots=True)
class _Enumeration:
    """
    What the gated half of the Scanner check saw.

    Attributes:
        devices: What the backend listed, in its order; empty when it listed
            nothing, and also when listing raised -- the verdict does not need
            to tell those two apart.
        configured_opened: ``None`` when no open was attempted; otherwise
            whether a configured device that the backend did not list could
            be opened and closed again, which is what a scan does with it.

    """

    devices: tuple[DeviceInfo, ...]
    configured_opened: bool | None = None


def _outcome_severity(outcome: _SanedOutcome) -> int:
    """
    Rank a probe outcome, so several hosts can be reported by the worst one.

    Most severe first: timed out, refused, unresolved, rejected, healthy.
    The two outcomes that stop the scanner being checked at all come first,
    timed out above refused because a refused host is at least up.
    Unresolved comes next, because it is the only fast outcome whose fix
    needs a config edit and a restart.  Rejected is last among the problems:
    the host is up, saned is up, and one line in its ``saned.conf`` fixes it.

    A ``match`` rather than a table, so a new outcome stops type-checking
    here until it is ranked.

    Args:
        outcome: What one probe found.

    Returns:
        A rank: higher is worse, and ``HEALTHY`` is 0.

    Raises:
        AssertionError: If the value is not a ``_SanedOutcome`` member.

    """
    match outcome:
        case _SanedOutcome.HEALTHY:
            severity = 0
        case _SanedOutcome.REJECTED:
            severity = 1
        case _SanedOutcome.UNRESOLVED:
            severity = 2
        case _SanedOutcome.REFUSED:
            severity = 3
        case _SanedOutcome.TIMED_OUT:
            severity = 4
        case _:
            assert_never(outcome)
    return severity


def _blocks_enumeration(outcome: _SanedOutcome) -> bool:
    """
    Say whether a host with this outcome must keep the check out of libsane.

    A timed-out host must, and that includes a peer that accepted the
    connection and then said nothing: libsane has no read timeout at any
    layer -- its net backend's connect timeout is cleared once the connect
    succeeds (``connect_dev`` in ``backend/net.c``) -- so ``get_devices()``
    would wait on that host for as long as it stays silent, inside a C call
    nothing can interrupt.

    A refused host must as well, for a different reason.  Refused is what a
    scanner host restarting its saned looks like, and libsane's net backend
    keeps one control connection per host open between enumerations.  When
    that connection has been lost, ``sane_get_devices`` does not check the
    failed call's status and goes on to read a reply that was never filled
    in, which crashes the process.  Enumerating a refused host that was
    connected earlier would be a new way into that crash.

    Rejected and unresolved hosts return at once inside libsane, and
    enumeration is how the check learns whether a usable scanner is visible
    anyway, so they do not block it.

    Args:
        outcome: What one probe found.

    Returns:
        True for ``TIMED_OUT`` and ``REFUSED``.

    Raises:
        AssertionError: If the value is not a ``_SanedOutcome`` member.

    """
    match outcome:
        case _SanedOutcome.TIMED_OUT | _SanedOutcome.REFUSED:
            blocks = True
        case _SanedOutcome.UNRESOLVED | _SanedOutcome.REJECTED | _SanedOutcome.HEALTHY:
            blocks = False
        case _:
            assert_never(outcome)
    return blocks


def _worst_outcome(probes: tuple[_HostProbe, ...]) -> _SanedOutcome:
    """
    Pick the most severe outcome among the probed hosts.

    Args:
        probes: What each configured host's probe found.

    Returns:
        The outcome ``_outcome_severity`` ranks highest, or ``HEALTHY`` when
        nothing was probed.

    """
    return max(
        (probe.outcome for probe in probes),
        key=_outcome_severity,
        default=_SanedOutcome.HEALTHY,
    )


def _hosts_subject(count: int, total: int) -> tuple[str, bool]:
    """
    Name the hosts a row is about, without naming any host.

    Args:
        count: How many hosts share the outcome being reported.
        total: How many hosts were probed.

    Returns:
        The subject of the row's sentence, and whether its verb is plural.
        One probed host is "the scanner host"; several are counted, as in
        "1 of 2 scanner hosts".

    """
    if total == 1:
        return "the scanner host", False
    return f"{count} of {total} scanner hosts", count > 1


def _host_problem_clause(outcome: _SanedOutcome, subject: str, *, plural: bool) -> str:
    """
    Say what a probe outcome means, as the start of a row's sentence.

    Args:
        outcome: The outcome being reported.
        subject: Who it is about, from ``_hosts_subject``.
        plural: Whether ``subject`` takes a plural verb.

    Returns:
        A clause starting lower-case, with no full stop.

    Raises:
        AssertionError: If the value is not a ``_SanedOutcome`` member.

    """
    match outcome:
        case _SanedOutcome.TIMED_OUT:
            clause = (
                f"{subject} are not answering"
                if plural
                else f"{subject} is not answering"
            )
        case _SanedOutcome.REFUSED:
            clause = (
                f"{subject} are on, but their scanner service is not running"
                if plural
                else f"{subject} is on, but its scanner service is not running"
            )
        case _SanedOutcome.UNRESOLVED:
            clause = f"{subject} could not be found by name"
        case _SanedOutcome.REJECTED:
            clause = (
                f"{subject} are refusing this machine"
                if plural
                else f"{subject} is refusing this machine"
            )
        case _SanedOutcome.HEALTHY:
            clause = f"{subject} are answering" if plural else f"{subject} is answering"
        case _:
            assert_never(outcome)
    return clause


def _host_problem_next_step(outcome: _SanedOutcome) -> str:
    """
    Return what to do about one probe outcome.

    Each state was measured against a real saned: a host that was refusing
    this machine, not listening, or not answering is picked up again by the
    next enumeration in the same process, so pressing Check again is honest
    advice for all three.  A name that did not resolve is different.
    libsane's net backend resolves each ``SANE_NET_HOSTS`` entry once, at the
    first enumeration, and drops one it cannot resolve for the life of the
    process (``add_device`` in ``backend/net.c``), so that fix needs a
    restart.

    Args:
        outcome: The outcome being reported.

    Returns:
        A next step, or the empty string when there is nothing to do.

    Raises:
        AssertionError: If the value is not a ``_SanedOutcome`` member.

    """
    match outcome:
        case _SanedOutcome.TIMED_OUT:
            next_step = "Check the scanner host is switched on and on the network, then press Check again."
        case _SanedOutcome.REFUSED:
            next_step = "Start saned on the scanner host, or check it is listening on the network, then press Check again."
        case _SanedOutcome.UNRESOLVED:
            next_step = (
                "Check [scanner] host in the config file, then restart saneless."
            )
        case _SanedOutcome.REJECTED:
            next_step = "Add this machine to saned.conf on the scanner host, then press Check again."
        case _SanedOutcome.HEALTHY:
            next_step = ""
        case _:
            assert_never(outcome)
    return next_step


def _net_device_entry(device_id: str) -> str | None:
    """
    Name the host entry a ``net:`` device was listed from.

    libsane's net backend names each remote device ``net:``, then the
    ``SANE_NET_HOSTS`` entry it dialled, then ``:`` and the name saned gave it
    (``sane_get_devices`` in ``backend/net.c``).  The entry is therefore the
    host the device lives on, spelt exactly as it was configured, and an IPv6
    literal keeps its brackets.

    The result is only ever compared with a probed host.  It is never dialled,
    rendered or logged, because it is a LAN address (ASVS V7).

    Args:
        device_id: A SANE device id, as configured or as listed.

    Returns:
        The host entry, or ``None`` when the id is not a ``net:`` id or names
        no entry.

    """
    prefix = "net:"
    if not device_id.startswith(prefix):
        return None
    remainder = device_id.removeprefix(prefix)
    if remainder.startswith("["):
        closing = remainder.find("]")
        entry = remainder[: closing + 1] if closing >= 0 else ""
    else:
        entry = remainder.partition(":")[0]
    return entry or None


def _directory_accepts_a_write(path: Path) -> bool:
    """
    Say whether a directory will actually take a file, by putting one there.

    This writes and removes a temporary file rather than asking
    ``os.access``.  ``os.access`` answers a question about the directory's mode
    bits, and the failure that matters here is not a mode bit: a
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


def _stale_file_as_named(settings: Settings, *, absolute_paths: bool) -> str:
    """
    Spell the superseded-name file the row is about to tell someone to rename.

    Two spellings of one file, one caller each.  The status strip gets the
    documented spelling, because it is a page anyone on the LAN can load and
    a search *position* maps to a constant string; ``saneless doctor`` and the
    log get the absolute path, because they are read by the operator on the
    machine, where the resolved path is the useful half and the disclosure
    rule does not apply.

    Args:
        settings: The settings whose recorded search found the file.
        absolute_paths: True for the terminal spelling.

    Returns:
        The file, spelled for the surface that asked.

    Raises:
        AssertionError: If the settings carry no recording.  Only a recorded
            search can report a superseded-name state, so reaching this means
            the state derivation and this function have come apart.

    """
    discovery = settings.config_discovery
    if discovery is None:
        msg = "A superseded-name state can only come from a recorded search"
        raise AssertionError(msg)
    stale = discovery.stale[0]
    if absolute_paths:
        return str(stale.absolute())
    return discovery.documented_spelling(stale)


def configuration_check(
    settings: Settings, *, absolute_paths: bool = False
) -> CheckResult:
    """
    Report which configuration file is in use, and whether an old one is not.

    The row that exists because four rows once went red or amber for one
    missing file and not one of them named it.  Its four outcomes come from
    ``config_file_state``, which is the single derivation the startup log, the
    status strip, ``saneless doctor`` and the one-shot commands all read, so
    none of them can describe the same appliance differently.

    Public, and with a spelling switch, for that same reason.  The terminal
    surfaces call it with ``absolute_paths=True`` and get *these* sentences
    with the file spelled as a resolved path; nothing there re-authors the
    wording, so a change here changes every surface at once.

    Why a missing file is amber and never red: configuring saneless entirely
    through environment variables is supported and documented, so a deployment
    with no file may be working exactly as designed.  Whether paperless-ngx is
    reachable is the Paperless row's question, and it already goes red on an
    unset URL or token -- two rows reporting one fact is how a reader learns
    to discount both.

    Why a lone file under the superseded name is red: nothing the operator
    wrote was read.  Every value they set is absent, the appliance is running
    on defaults, and the file sitting in the searched directory is the reason.

    Why the leftover next step says move before it says delete: after an
    upgrade the leftover can hold the only copy of the Paperless URL and
    token, and a bare "delete it" would be advice to destroy the
    configuration.

    Why both next steps end in a restart: the search is run once, at load, and
    the outcome is recorded.  Renaming the file changes nothing until the
    process reads it.

    Args:
        settings: The settings in hand, carrying the search that built them.
        absolute_paths: True to spell the file as a resolved path, for the
            terminal and the log; False for the LAN-visible strip.

    Returns:
        Exactly one result for ``CheckKey.CONFIGURATION``.

    Raises:
        AssertionError: If the state is not a ConfigFileState member.

    """
    state = config_file_state(settings)
    match state:
        case ConfigFileState.LOADED:
            return CheckResult(
                key=CheckKey.CONFIGURATION,
                state=CheckState.OK,
                message="Config file loaded.",
            )
        case ConfigFileState.LOADED_WITH_LEFTOVER:
            named = _stale_file_as_named(settings, absolute_paths=absolute_paths)
            return CheckResult(
                key=CheckKey.CONFIGURATION,
                state=CheckState.WARN,
                # One literal, deliberately over the 88-column guide (E501 is
                # off in this project): the sentence is pinned word for word,
                # and a grep for it has to find it on one line.
                message=f"Using {CONFIG_FILENAME}; an old {LEGACY_CONFIG_FILENAME} is being ignored.",
                next_step=(
                    f"Move anything you still need from {named} into the "
                    f"{CONFIG_FILENAME} in use, then delete {named} and "
                    "restart saneless."
                ),
            )
        case ConfigFileState.NOT_FOUND:
            return CheckResult(
                key=CheckKey.CONFIGURATION,
                state=CheckState.WARN,
                # One literal for the same reason as the row above.
                message="No config file; running on defaults and environment variables.",
                next_step=(
                    f"The saneless log lists every place it looked for {CONFIG_FILENAME}."
                ),
            )
        case ConfigFileState.STALE_ONLY:
            named = _stale_file_as_named(settings, absolute_paths=absolute_paths)
            return CheckResult(
                key=CheckKey.CONFIGURATION,
                state=CheckState.FAIL,
                # One literal for the same reason as the rows above.
                message=f"No config file loaded: saneless now reads {CONFIG_FILENAME}, not {LEGACY_CONFIG_FILENAME}.",
                next_step=(
                    f"Rename {named} to {CONFIG_FILENAME}, then restart saneless."
                ),
            )
        case _:
            assert_never(state)


def _scanner_unreachable() -> CheckResult:
    """
    Build the "the scanner is not answering" row.

    Returns:
        The red not-reachable Scanner row.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.FAIL,
        message="Not reachable.",
        next_step=(
            "Check the scanner is switched on and connected, then press Check again."
        ),
    )


def _scanner_host_unanswered(probes: tuple[_HostProbe, ...]) -> CheckResult:
    """
    Build the row for a scanner host the check must not enumerate.

    This is the row for timed-out and refused hosts -- a peer that accepted
    the connection and then said nothing counts as timed out -- and only for
    them: ``_blocks_enumeration`` says why each of those is kept out of
    libsane.  With several hosts it reports the worst outcome among them, by
    ``_outcome_severity``, and how many of the hosts share it, so one dead
    host beside a healthy one is still visible.

    Amber, not red, and the distinction is the whole point.  What the
    pre-probe observed is a fact about the *configured host*, not about the
    appliance: ``SANE_NET_HOSTS`` **adds** net devices to what the dll backend
    enumerates, it does not replace local backend enumeration (``SaneBackend``
    only sets the variable), so "a configured sane-net host is dead" never
    implied "there is no scanner".  A machine with a working USB scanner and a
    switched-off network one scans perfectly, and a true statement about a
    deployment that still works is amber -- the same shape as the
    read-only-configuration Profiles row.  Reporting it red would break the
    rule ``CheckState``'s own docstring states outright: a healthy appliance
    must never go red.

    The cost is recorded rather than hidden.  An appliance whose *only*
    scanner is a timed-out or refused network host reports amber, so
    ``saneless doctor`` exits 0 for it.  That is accepted: a scripted gate is
    keyed on red alone by design, the row is still visible, it still names the
    scanner host as the thing that is wrong, and it still carries the next
    step that fixes it, so a human loses nothing.  Getting the red back means
    bounding ``get_devices()`` on a second thread, which cannot be done safely
    while ``sane_get_devices`` is uninterruptible.  A thread past its deadline
    is still inside libsane after the caller has returned and released the
    gate, which breaks the gate's one-caller-inside-libsane rule, and
    ``scanner.close()`` would then run ``sane_exit()`` with a SANE call still
    outstanding -- a segfault risk.  Making it safe needs a helper thread that
    owns the gate itself and that the refresher's shutdown can see.

    Neither string names a host, its address or its port; the only thing
    interpolated is a count.  A LAN address on a LAN-visible page is the same
    class of disclosure as the SANE device id ``_device_label`` refuses to
    print (ASVS V7).

    Args:
        probes: What each configured host's probe found; at least one of them
            blocks enumeration.

    Returns:
        The amber Scanner row, with ``skipped`` false -- the probe was run,
        and what it found is the row.

    """
    clause, worst = _worst_host_clause(probes)
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.WARN,
        message=f"{_capitalised(clause)}, so the scanner could not be checked.",
        next_step=_host_problem_next_step(worst),
    )


def _scanner_skipped() -> CheckResult:
    """
    Build the row shown while a scan is running.

    The state is ``OK`` rather than ``WARN`` or ``FAIL``.  "We did not look" is
    a fact about the probe, not a verdict about the appliance, and a scan in
    flight is direct evidence the scanner was working moments ago; a scripted
    health gate must not go red for the duration of every scan.  The state
    stays ``OK`` precisely so that gate keeps passing -- ``worst_state`` and
    ``saneless doctor``'s exit rule read it and nothing else.

    The ``skipped`` flag, not the state, is what the two surfaces *render*, and
    the functions that read it are named rather than implied so the claim is
    checkable by grep: ``check_row_class``, ``check_row_glyph`` and
    ``check_row_label`` in this module draw the web row, and ``_row_marker`` in
    ``saneless.cli`` draws the ``doctor`` one.

    Returns:
        The neutral skipped Scanner row.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.OK,
        message="Not checked while a scan is running.",
        skipped=True,
    )


def _scanner_busy() -> CheckResult:
    """
    Build the row shown when something else held the scanner gate.

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
    on red must not fail because two threads wanted the scanner in the
    same instant.  Keeping the state at ``OK`` is what holds that gate open.

    The ``skipped`` flag is what discloses that nothing was checked, through
    the same four functions ``_scanner_skipped``'s docstring names:
    ``check_row_class``, ``check_row_glyph`` and ``check_row_label`` here, and
    ``_row_marker`` in ``saneless.cli``.

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
    Build the "there is no python-sane on this machine" row.

    Its own constructor because two callers need the same row and a row
    written twice is a row that can drift.

    Returns:
        The red no-scanner-support row.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.FAIL,
        message="Scanner support is not installed on this machine.",
        next_step="Install saneless with scanner support, then restart it.",
    )


# The outcomes that explain why a configured ``net:`` device is missing.  Only
# these two reach enumeration while still being a problem: a timed-out or
# refused host ends the check before it, and a healthy one explains nothing.
_CONFIGURED_HOST_OUTCOMES: Final = frozenset(
    {_SanedOutcome.REJECTED, _SanedOutcome.UNRESOLVED}
)


def _capitalised(clause: str) -> str:
    """
    Start a clause with a capital letter, so it can open a row's sentence.

    Args:
        clause: A clause starting lower-case, as ``_host_problem_clause``
            builds them.

    Returns:
        The same clause with its first character upper-cased.

    """
    return f"{clause[:1].upper()}{clause[1:]}"


def _worst_host_clause(probes: tuple[_HostProbe, ...]) -> tuple[str, _SanedOutcome]:
    """
    Say what the worst of the probed hosts found, and how many hosts share it.

    Args:
        probes: What each configured host's probe found; at least one.

    Returns:
        The clause for the worst outcome, counted by ``_hosts_subject``, and
        that outcome, whose next step the row carries.

    """
    worst = _worst_outcome(probes)
    count = sum(1 for probe in probes if probe.outcome is worst)
    subject, plural = _hosts_subject(count, len(probes))
    return _host_problem_clause(worst, subject, plural=plural), worst


def _scanner_ready_subject(
    enumeration: _Enumeration, configured_device: str
) -> str | None:
    """
    Name the scanner a scan would use, if there is one it can use.

    With ``scanner.device`` set the scan opens exactly that id, so the check
    looks it up by exact name and labels the device it finds, whatever else
    is listed and in whatever order.  A device the backend does not list but
    that opened is usable too: SANE opens ids it never lists.  With the
    setting empty the scan takes the first device listed, and so does this.

    Args:
        enumeration: What the gated half of the check saw.
        configured_device: The configured ``scanner.device``, possibly empty.

    Returns:
        ``None`` when no usable scanner was seen.  Otherwise the words the
        row uses for it: the device's label, "The configured scanner" for an
        unlisted device that opened, or the empty string for a device that
        reported no vendor and no model.

    """
    if configured_device:
        listed = next(
            (
                device
                for device in enumeration.devices
                if device.name == configured_device
            ),
            None,
        )
        if listed is not None:
            return _device_label(listed)
        if enumeration.configured_opened is True:
            return "The configured scanner"
        return None
    if enumeration.devices:
        return _device_label(enumeration.devices[0])
    return None


def _scanner_ready_row(
    probes: tuple[_HostProbe, ...], subject: str, unchosen: int
) -> CheckResult:
    """
    Build the row for a scanner that can be used.

    A host problem still shows, in amber, because scanning works: the rule is
    that a working appliance never goes red.  It is reported ahead of the
    several-devices warning, because it is the one that is a failure.

    Args:
        probes: What each configured host's probe found; none blocks
            enumeration.
        subject: The scanner's words from ``_scanner_ready_subject``.
        unchosen: How many devices are visible with ``scanner.device`` empty;
            0 when a device is configured.

    Returns:
        WARN for a host problem or for several devices with none chosen;
        otherwise OK.

    """
    if any(probe.outcome is not _SanedOutcome.HEALTHY for probe in probes):
        clause, worst = _worst_host_clause(probes)
        return CheckResult(
            key=CheckKey.SCANNER,
            state=CheckState.WARN,
            message=f"{subject or 'The scanner'} is ready, but {clause}.",
            next_step=_host_problem_next_step(worst),
        )
    if unchosen > 1:
        # With no device configured, every scan goes to whichever device SANE
        # lists first, and a scanner that appears on the LAN can take that
        # place.  A warning, not a failure: scanning still works.  Count-only,
        # because device ids are LAN addresses and this row is LAN-visible.
        return CheckResult(
            key=CheckKey.SCANNER,
            state=CheckState.WARN,
            message=f"{unchosen} scanners are visible and none is chosen.",
            next_step=(
                "Set [scanner] device to the one you use; saneless devices "
                "lists them, and saneless auto-profiles writes it for you."
            ),
        )
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.OK,
        message=f"{subject} is ready." if subject else "Ready.",
    )


def _scanner_host_problem_row(clause: str, outcome: _SanedOutcome) -> CheckResult:
    """
    Build the red row for a host problem that leaves no usable scanner.

    Args:
        clause: What the host problem is, from ``_host_problem_clause``.
        outcome: The outcome the clause reports, which picks the next step.

    Returns:
        The red Scanner row.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.FAIL,
        message=f"{_capitalised(clause)}.",
        next_step=_host_problem_next_step(outcome),
    )


def _scanner_configured_missing_row() -> CheckResult:
    """
    Build the red row for a configured scanner that was not found.

    The next step covers both ways this happens.  If ``saneless devices`` --
    a fresh process -- lists the device, this process's libsane has lost it
    and a restart fixes that; if it does not, the configured id is wrong.

    Returns:
        The red Scanner row.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.FAIL,
        message="The configured scanner was not found.",
        next_step="Check it is switched on and connected. If saneless devices lists it, restart saneless; if not, set [scanner] device to one it lists.",
    )


def _scanner_nothing_found_row(hosts: int) -> CheckResult:
    """
    Build the red row for no scanner at all, with every probed host healthy.

    Args:
        hosts: How many scanner hosts were probed, all of them healthy.

    Returns:
        The red Scanner row.

    """
    if hosts == 0:
        return CheckResult(
            key=CheckKey.SCANNER,
            state=CheckState.FAIL,
            message="No scanner was found.",
            next_step="Check the scanner is switched on and connected, then press Check again. If saneless devices lists it, restart saneless.",
        )
    message = (
        "The scanner host is answering, but no scanner was found on it."
        if hosts == 1
        else "The scanner hosts are answering, but no scanner was found on them."
    )
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.FAIL,
        message=message,
        next_step="Check the scanner is switched on and connected to the scanner host. If saneless devices lists it, restart saneless.",
    )


def _scanner_unusable_row(
    probes: tuple[_HostProbe, ...], configured_device: str
) -> CheckResult:
    """
    Build the red row when no usable scanner was seen, naming the likeliest cause.

    The more specific cause wins.  A configured ``net:`` device whose own
    host was refusing this machine or could not be found by name is missing
    *because of* that host, so the row says so.  A configured device that is
    not a ``net:`` device is simply not found, even if some unrelated network
    host is also bad, because a network host does not explain a local
    device's absence.

    Args:
        probes: What each configured host's probe found; none blocks
            enumeration.
        configured_device: The configured ``scanner.device``, possibly empty.

    Returns:
        The red Scanner row.

    """
    if configured_device:
        entry = _net_device_entry(configured_device)
        own_host = next(
            (
                probe
                for probe in probes
                if probe.host == entry and probe.outcome in _CONFIGURED_HOST_OUTCOMES
            ),
            None,
        )
        if own_host is None:
            return _scanner_configured_missing_row()
        clause = _host_problem_clause(
            own_host.outcome, "the configured scanner's host", plural=False
        )
        return _scanner_host_problem_row(clause, own_host.outcome)
    if any(probe.outcome is not _SanedOutcome.HEALTHY for probe in probes):
        return _scanner_host_problem_row(*_worst_host_clause(probes))
    return _scanner_nothing_found_row(len(probes))


def _scanner_verdict(
    probes: tuple[_HostProbe, ...],
    enumeration: _Enumeration,
    configured_device: str,
) -> CheckResult:
    """
    Decide the Scanner row from what the probes and the enumeration found.

    Pure: no socket, no backend and no clock, so every row can be pinned by a
    unit test.  The row goes red exactly when scanning cannot work.  While a
    usable scanner is visible a host problem keeps the row amber, because a
    working appliance never goes red.  With ``scanner.device`` set, the row is
    about that device, found by its exact id; the first device listed stands
    in only when the setting is empty, which is the device a scan would use.

    Where two causes compete, the more specific one wins.  A configured
    ``net:`` device whose own host was refusing this machine or could not be
    found by name reports that host's problem, which explains the absence.  A
    configured device that is not a ``net:`` device and is missing is reported
    as not found, whatever an unrelated host is doing.  With nothing
    configured, a host problem is reported ahead of the several-devices
    warning.  Several bad hosts are reported by the worst outcome, with a
    count.

    The next steps follow what actually clears each state, which was measured
    against a real saned and agrees with sane-backends' source:

    - libsane's net backend is initialised lazily, inside the first device
      listing of the process, and resolves each ``SANE_NET_HOSTS`` entry
      exactly once then (``sane_init`` calling ``add_device`` in
      ``backend/net.c``).  An entry it cannot resolve is dropped for the life
      of the process, so the row for a name that did not resolve says to
      restart saneless.
    - A host that was refusing this machine, was not listening, or did not
      answer is dialled again on every listing: ``connect_dev`` closes and
      resets the connection whenever connecting or the opening handshake
      fails.  Those rows say press Check again.
    - A healthy host with nothing visible on it is the state a dropped entry
      leaves behind, so that row, and the rows for a scanner that was not
      found, say to restart only conditionally: "If saneless devices lists
      it, restart saneless".  That is true on both surfaces.
      ``saneless devices`` and ``saneless doctor`` run in fresh processes,
      while the web server's libsane may be stale: when the status strip is
      red but ``saneless devices`` lists the scanner, the server's libsane is
      what is wrong, and when ``doctor`` is red ``saneless devices`` shows the
      same nothing, which points the reader at the scanner instead.

    A host that must not be enumerated -- timed out or refused -- gets the
    same row the preflight returns for it, so the two can never disagree.

    Rows interpolate counts and ``_device_label`` output only.  The
    configured id and the listed device ids are compared, never rendered,
    because they are LAN addresses on a LAN-visible page (ASVS V7).

    Args:
        probes: What each configured host's probe found, in configured order;
            empty when no host is configured.
        enumeration: What the gated half of the check saw.
        configured_device: The configured ``scanner.device``, possibly empty.

    Returns:
        Exactly one result for ``CheckKey.SCANNER``.

    """
    if any(_blocks_enumeration(probe.outcome) for probe in probes):
        return _scanner_host_unanswered(probes)
    subject = _scanner_ready_subject(enumeration, configured_device)
    if subject is None:
        return _scanner_unusable_row(probes, configured_device)
    unchosen = 0 if configured_device else len(enumeration.devices)
    return _scanner_ready_row(probes, subject, unchosen)


def _scanner_preflight(context: CheckContext) -> CheckResult | _ScannerPreflight:
    """
    Decide the scanner row without entering SANE, or hand over to enumeration.

    This is everything the scanner check can settle before libsane is touched,
    and it is a separate function so it can run with the worker's scanner gate
    **free**.  Nothing here is SANE work: it is a settings read, name
    resolution and the opening of the SANE network handshake.  That matters
    because resolution is outside every budget this module states --
    ``getaddrinfo`` takes no timeout, as ``PROBE_CONNECT_SECONDS`` says at
    length -- so a check holding the gate across it can park a
    ``ScanWorker._scan_job`` whose job row already reads ``SCANNING`` for as
    long as a broken resolver takes, and ``POST /api/checks/refresh`` can
    re-arm that parking every couple of seconds.

    The order inside it is the order ``_check_scanner`` always had.  A machine
    with no python-sane is its own row and is decided without touching
    anything.  Then **every** configured host SANE will dial is probed with
    ``_probe_saned``, with no short circuit: libsane dials every entry, so a
    dead second host costs its full uninterruptible connect inside
    ``get_devices()`` however well the first one answers.  If any host timed
    out -- including one that accepted the connection and said nothing -- or
    refused, the check ends right there with the amber
    ``_scanner_host_unanswered`` row, and ``_blocks_enumeration`` says why
    neither may be enumerated.  Rejected, unresolved and healthy hosts, and a
    setting with no host to probe, go on to enumeration.

    Args:
        context: The injected dependencies and configuration.

    Returns:
        The row, when it can be decided here; otherwise what enumeration
        needs, which is the backend and what each host's probe found.

    """
    scanner = context.scanner
    if scanner is None:
        return _scanner_support_missing()
    probes = tuple(
        _HostProbe(
            host,
            _probe_saned(host, port, PROBE_CONNECT_SECONDS, PROBE_HANDSHAKE_SECONDS),
        )
        for host, port in _saned_hosts(_saned_host_setting(context.settings))
    )
    if any(_blocks_enumeration(probe.outcome) for probe in probes):
        return _scanner_host_unanswered(probes)
    return _ScannerPreflight(scanner=scanner, probes=probes)


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
        Exactly one result for ``CheckKey.SCANNER``: WARN when several devices
        are visible and ``scanner.device`` chooses none of them.

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
        # _sane.error, RuntimeError or AttributeError with no shared base, so
        # the boundary catches Exception.  The type name is logged; nothing
        # from the exception reaches the row.
        logger.warning("Scanner enumeration failed: %s", type(exc).__name__)
        return _scanner_unreachable()
    if not devices:
        return _scanner_unreachable()
    if not context.settings.scanner.device and len(devices) > 1:
        # With no device configured, every scan goes to whichever device SANE
        # lists first, and a scanner that appears on the LAN can take that
        # place.  A warning, not a failure: scanning still works.  Count-only,
        # because device ids are LAN addresses and this row is LAN-visible.
        return CheckResult(
            key=CheckKey.SCANNER,
            state=CheckState.WARN,
            message=f"{len(devices)} scanners are visible and none is chosen.",
            next_step=(
                "Set [scanner] device to the one you use; saneless devices "
                "lists them, and saneless auto-profiles writes it for you."
            ),
        )
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

    An ``isinstance`` test against ``CheckResult`` rather than a truthiness
    shortcut: the preflight returns either a finished row or what enumeration
    needs, and both are frozen dataclasses and therefore always truthy.  The
    ``isinstance`` test is also what lets both type checkers narrow ``pre``
    without a cast.

    Args:
        context: The injected dependencies and configuration.

    Returns:
        Exactly one result for ``CheckKey.SCANNER``.

    """
    pre = _scanner_preflight(context)
    if isinstance(pre, CheckResult):
        return pre
    return _scanner_enumeration(context)


def _paperless_next_step(status: ConnectionStatus) -> str:
    """
    Return what to do about one connection outcome.

    The sentences are fixed user copy, and they pair with the messages
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
    placeholder: an unset token cannot succeed, so a request would only
    tell paperless-ngx about it.  ``is_placeholder_token`` is the one predicate
    ``doctor``, this check, the scan route and ``saneless scan`` share, so all
    four agree on whether the appliance can upload.

    An empty ``paperless.url`` is examined next and skips the probe too.  It
    loads, so ``serve`` can start and show this row, but a request to it
    fails inside httpx2 before anything is sent, and ``test_connection``
    would report that as UNREACHABLE -- a network fault, when the fix is a
    setting.  ``ConnectionStatus`` is a public JSON contract, so the unset URL
    gets its own row here rather than a sixth status.

    A ``None`` client means one could not be constructed.
    ``PaperlessClient.__init__`` refuses a URL httpx2 will not parse or that
    carries a user name or password, a token an HTTP header cannot carry, and
    a TLS trust store it cannot read; every one of them is shown as the "not
    found at that URL" row, not a sixth sentence.  Load-time validation of
    ``paperless.url`` and ``paperless.token`` rejects the first three before a
    client is ever built, so in practice the row is reached by the last.

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
    if not context.settings.paperless.url:
        return CheckResult(
            key=CheckKey.PAPERLESS,
            state=CheckState.FAIL,
            message="The paperless-ngx address has not been set.",
            next_step=(
                "Set paperless.url in the saneless config file to the "
                "paperless-ngx address, then restart saneless."
            ),
        )
    client = context.paperless
    if client is None:
        status = ConnectionStatus.NOT_FOUND
    else:
        status = client.test_connection(
            timeout=httpx2.Timeout(PROBE_READ_SECONDS, connect=PROBE_CONNECT_SECONDS)
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
    different facts and only the second is worth investigating.

    The storage outcome is recorded by the worker rather than recomputed here.
    A fresh ``os.access`` probe cannot substitute for it: the failure that
    matters is a single-file bind mount, where the directory is writable and
    only the rename fails.

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
                # off in this project): the sentence is pinned word for word,
                # and a grep for it has to find it on one line.
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

    An unset fallback folder is amber and never red.  The
    appliance scans and files perfectly without one; what it cannot do is
    survive paperless-ngx being down, and a red row for a deployment that works
    is a row people learn to ignore.

    The configured path is not in either sentence.  It is a host filesystem
    path on a LAN-visible page, omitted for the same reason the log file's
    path is.

    Args:
        context: The injected dependencies and configuration.

    Returns:
        Exactly one result for ``CheckKey.FALLBACK``.

    """
    consume_dir = context.settings.paperless.consume_dir
    if consume_dir is None:
        return CheckResult(
            key=CheckKey.FALLBACK,
            state=CheckState.WARN,
            message="Not configured; scans cannot be kept if paperless-ngx is down.",
            next_step=(
                "Set a fallback folder in the saneless config so scans are kept "
                "when paperless-ngx is down."
            ),
        )
    if _directory_accepts_a_write(consume_dir):
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
    if _directory_accepts_a_write(context.settings.output.data_dir):
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

    A total ``match`` rather than a dict of functions: a seventh ``CheckKey``
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
        case CheckKey.CONFIGURATION:
            # Documented spellings, not absolute paths: this path renders on
            # the status strip, which anyone on the LAN can load.
            result = configuration_check(context.settings)
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
    enumeration is the only thing any check does inside libsane.  What matters
    is the size of the gated region: the pre-probe runs *first*,
    with the gate free, and only ``_scanner_enumeration`` is held.  The
    pre-probe is name resolution and the opening of the SANE network
    handshake, and resolution is outside every budget this module states, so
    holding the gate across it could park a ``ScanWorker._scan_job`` whose job
    row already reads ``SCANNING`` for as long as a broken resolver takes.  A
    pre-probe that settles the row -- no python-sane, or a configured host
    that timed out or refused -- therefore never touches the gate at all.

    The attempt is non-blocking and a failure is reported rather than waited
    out, which is the single move ``ScanWorker.scanner_gate`` documents as
    permitted: blocking here would queue behind a scan that can legitimately
    run for minutes and then enter SANE at some arbitrary later moment.  A
    failed acquire is ``_scanner_busy()`` and not ``_scanner_skipped()``: the
    latter names a running scan, and ``run_checks`` has already dealt with that
    case before this function is reached, so the only thing a lost gate
    establishes is that somebody else is in SANE -- today, the worker's startup
    capability read, which runs before any job exists.

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
    should look.  It is parametrised over the four contexts whose rows differ:
    no python-sane, a configured host that does not answer the pre-probe,
    a host that answers and enumerates one device, and an enumeration that
    raises.  The first two are decided before the gate is reached and the last
    one leaves through ``run_checks``' handler with the gate released in a
    ``finally``, so all four halves of this function are compared against
    ``_check_scanner``.

    Args:
        context: The injected dependencies and configuration.
        scanner_gate: The worker's gate, tried without blocking.

    Returns:
        The scanner row, or the neutral busy row when the gate was not free.

    """
    pre = _scanner_preflight(context)
    if isinstance(pre, CheckResult):
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
    whole of what either of them may show.  It iterates ``CheckKey``, so
    a check that exists for ``saneless doctor`` and not for the status strip is
    not something either surface is able to express.

    ``skip_scanner`` is honoured here rather than inside the scanner check, and
    it returns the paused row without entering the backend at all.  That is
    correctness, not politeness: nothing in ``sane_backend.py`` mutually
    excludes two SANE calls, so a status probe landing on the device mid-scan
    is a second caller into the same C library while a read is outstanding,
    which SANE does not allow.  It is honoured
    *first*, before the gate is looked at: a caller that already knows a scan
    is running has no reason to touch the gate at all.

    The gate is a parameter rather than something the caller holds around this
    call, and that is deliberate.  Only ``_check_scanner`` enters
    libsane.  ``_check_paperless`` carries a multi-second HTTP budget, and
    ``_check_fallback`` and ``_check_data_dir`` each create and delete a real
    file.  A caller that wrapped all six made the lock that exists to keep two
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
