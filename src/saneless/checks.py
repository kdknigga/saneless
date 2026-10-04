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
reports every row.

ASVS 4.0.3 V7.4 applies to every string this module can render.  No message and
no next
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

``CheckResult.terminal_detail`` is outside the rule because it is never
rendered on the page: only ``saneless doctor`` prints it, on the machine.  It
carries where paperless-ngx redirected to, sanitised by ``paperless.py``.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, assert_never

import httpx2

from saneless.config import (
    CONFIG_FILENAME,
    LEGACY_CONFIG_FILENAME,
    config_file_state,
    is_placeholder_token,
    nearest_existing_ancestor,
)
from saneless.exceptions import (
    ConfigError,
    ListingAbortedError,
    ListingCrashedError,
    ListingNoAnswerError,
    ListingTimedOutError,
)
from saneless.private_dirs import check_private_dir
from saneless.scanner import saned_probe
from saneless.scanner.base import DeviceSurvey
from saneless.vocabulary import (
    RETRY_PLACEHOLDER,
    RETRY_SENTENCE_PLACEHOLDER,
    UNSET_CREDENTIAL_CLAUSE,
    ConfigFileState,
    ConnectionStatus,
    ProfileStorage,
    connection_status_message,
    sentence_case,
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
    "SKIPPED_STATE_LABEL",
    "CheckContext",
    "CheckKey",
    "CheckResult",
    "CheckState",
    "PaperlessRefusal",
    "ScannerRefusal",
    "check_name",
    "check_row_class",
    "check_row_glyph",
    "check_row_label",
    "check_state_class",
    "check_state_glyph",
    "check_state_label",
    "configuration_check",
    "leftover_config_check",
    "run_checks",
    "worst_state",
]

logger = logging.getLogger(__name__)


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
# ``saned_probe.PROBE_CONNECT_SECONDS`` plus
# ``saned_probe.PROBE_READ_SECONDS``, so about 7 s -- is
# disproved by three things this same module documents.
# ``getaddrinfo`` is outside every budget here:
# ``saned_probe.PROBE_CONNECT_SECONDS`` says
# at length that an unreachable resolver costs whatever ``resolv.conf`` says,
# and nothing bounds it.  The saned pre-probe pays that once per configured
# host, up to ``saned_probe._MAX_PROBE_HOSTS`` of them.  And the ordinary
# local-USB
# deployment has no parseable host at all, so ``_scanner_preflight`` hands
# over to enumeration without dialling anything and ``_scanner_enumeration``
# starts a listing child, which a silently unreachable host holds until
# ``LISTING_DEADLINE_SECONDS`` stops it.
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
# Ninety attempts at the real two-second interval is about 180 s.  The saned
# pre-probe costs at most ``saned_probe.PROBE_CONNECT_SECONDS`` plus
# ``saned_probe.PROBE_HANDSHAKE_SECONDS`` -- 7 s -- for each of up to
# ``saned_probe._MAX_PROBE_HOSTS``
# setting entries and the configured ``net:`` device's own host, so 35 s, and
# a *probed* host that could hang enumeration never reaches it: a configured
# device is opened only once its own host has been probed, on the port libsane
# opens it on, and did not time out.  A host the pre-probe cannot dial is
# another matter -- ``_scanner_preflight`` lists which those are --
# and the listing still dials it, so a dead one holds the listing child until
# ``LISTING_DEADLINE_SECONDS``, 30 s, stops it, as a wedged local backend can.
# That is one listing at most, never two: the open of an unlisted device runs
# in the same child, and is refused when its host could not be probed on that
# port.  35 s of probing, one 30 s listing and
# ``saned_probe.PROBE_READ_SECONDS`` for
# Paperless is about 70 s, which leaves the pre-probe's unbounded resolutions
# the rest; the cap was sized when one listing could cost ~127 s on its own,
# and is kept rather than shrunk because a tab asking a little longer loses
# nothing.  Below it, a cold start on a wedged scanner stopped asking while
# its first probe was still running and told a household member to press a
# button that starts the thing already running.
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
# read.  Only the strip shows it, never ``saneless doctor``, so it names the
# button directly instead of holding a retry placeholder.
POLL_GAVE_UP_LINE: Final = "The checks have not run yet. Press Check again to try now."

# What the strip says instead, while it is still asking because a probe is
# demonstrably in flight.  ``POLL_GAVE_UP_LINE`` points at the
# ``Check again`` button, and beside a running probe that is advice that cannot
# help: the click collapses into the probe already running.  So this sentence
# says what is true -- the first check has not finished -- and sets an
# expectation for how long that can take, in words a household member reads.
# It was written when one listing could cost ~127 s; a listing now stops at
# ``LISTING_DEADLINE_SECONDS``, so the sentence errs long, which for an
# expectation is the safe direction.  Held to exactly
# ``POLL_GAVE_UP_LINE``'s rule: no host, no address, no port, no path, no URL
# and no exception text, because it renders on a page the whole LAN can read
# (ASVS 4.0.3 V7.4).
POLL_STILL_CHECKING_LINE: Final = (
    "The first check is still running. This can take a couple of minutes "
    "if the scanner is not reachable."
)

# How much of a device's own description a row will print.  Nothing else bounds
# what a scanner can call itself, and the row is rendered into HTML next to
# columns whose width is fixed.
_DEVICE_LABEL_MAX_LENGTH: Final = 60

# The row a check that raised is rendered as.  Developer constants, because the
# exception that produced them is exactly the thing that must not reach the
# page.
_CHECK_FAILED_MESSAGE: Final = "This check could not be completed."
_CHECK_FAILED_NEXT_STEP: Final = f"Restart saneless, then {RETRY_PLACEHOLDER}."


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
    Every check, and the contract that both surfaces show every one.

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
    drawn a wrong conclusion from each of them.

    Adding a member is therefore a deliberate act with a visible cost.  The
    ``match`` in ``check_name`` stops type-checking until the new key has a
    name, and the ``match`` in ``_dispatch`` stops type-checking until it has a
    check function.  ``test_check_names_are_the_ui_spec_column`` fails until
    the designed column names it, and the row tests hold both surfaces to one
    row per member: ``test_all_ok_prints_one_line_per_check`` and
    ``test_rows_are_printed_in_check_key_order`` for ``saneless doctor``, and
    ``test_index_renders_the_cached_rows`` and
    ``test_cold_start_renders_one_checking_row_per_check`` for the status
    strip.

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
    about it is a red row a household member can only escalate.  A next step
    that tells the reader to try again holds a retry placeholder from
    ``saneless.vocabulary`` instead of the words, because the two surfaces
    retry differently: the strip has a Check again button and ``saneless
    doctor`` is run again.  Each surface renders it with ``render_check_step``.

    ``skipped`` is the "not checked while a scan is running" row.  It is a
    separate flag rather than a fourth state because the row still has to carry
    *some* state for its glyph, and "we did not look" is a fact about the
    probe, not a verdict about the appliance.

    Attributes:
        key: Which ``CheckKey`` this is.
        state: How it came out.
        message: A developer-authored sentence.  Never a path, a URL, a token
            or exception text (ASVS 4.0.3 V7.4).  The module docstring states the one
            exception: the Configuration row's ``next_step``, in its two
            superseded-name states, carries one of three fixed documented
            spellings of the file to rename -- never a resolved host path, and
            never in ``message``.
        next_step: What to do about it, for ``WARN`` and ``FAIL`` rows, with
            any retry still a placeholder.
        skipped: True when the probe was deliberately not run.
        terminal_detail: A line for ``saneless doctor`` only, or empty.  It
            may name an address -- today it is where paperless-ngx redirected
            to, already sanitised by ``paperless.py`` -- which is why the
            status strip never renders it: the strip is visible to anyone on
            the LAN, and the terminal is on the machine.  ``doctor`` passes it
            through ``neutralise_controls`` before printing it.

    """

    key: CheckKey
    state: CheckState
    message: str
    next_step: str = ""
    skipped: bool = False
    terminal_detail: str = ""


class PaperlessRefusal(StrEnum):
    """
    Why a Paperless client could not be built, for the Paperless row.

    ``PaperlessClient.__init__`` refuses for two kinds of reason, and they are
    fixed in different places.  ``TRUST_STORE`` is the TLS trust store named by
    ``SSL_CERT_FILE`` or ``SSL_CERT_DIR`` that could not be read: the settings
    are fine and the environment is not.  ``CONFIGURATION`` is a
    ``paperless.url`` or ``paperless.token`` the client cannot send.  Reporting
    either as a wrong address sends the reader to the wrong file.
    """

    TRUST_STORE = "TRUST_STORE"
    CONFIGURATION = "CONFIGURATION"


class ScannerRefusal(StrEnum):
    """
    Why a scanner backend could not be built, for the Scanner row.

    ``NOT_INSTALLED`` is python-sane missing, which installing it fixes.
    ``START_FAILED`` is python-sane present and ``sane.init()`` refusing:
    the scanner library is there and would not start, which reinstalling
    does not fix and the log explains.
    """

    NOT_INSTALLED = "NOT_INSTALLED"
    START_FAILED = "START_FAILED"


@dataclass(frozen=True, slots=True)
class CheckContext:
    """
    Everything the checks need, handed in rather than reached for.

    Every dependency is injected because the two surfaces build them
    differently, and neither may be the one this module knows about.
    The web app has a worker, a long-lived Paperless client and a scanner
    backend it opened at startup; ``saneless doctor`` has none of those and
    builds what it needs for one command.

    ``scanner=None`` means no scanner backend could be built.  That is what
    lets ``doctor`` report every row on such a machine instead of refusing
    at ``require_sane()`` and reporting none -- the thing an operator most
    needs a diagnostic for is the machine where the diagnostic would otherwise
    not run.  ``scanner_refusal`` says why: python-sane is not installed, or
    it is installed and the scanner library would not start.  The two need
    different remedies, so they are different rows; a caller that does not
    say gets the not-installed row.

    ``paperless=None`` means no usable client could be built at all:
    ``PaperlessClient.__init__`` refused, for a URL httpx2 will not parse or
    that carries a user name or password, a token an HTTP header cannot
    carry, or a TLS trust store it could not read.  ``paperless_refusal``
    tells the trust store apart from the URL and token, because the remedy
    for one is an environment variable and for the other a setting.  A caller
    that does not say gets the "not found at that URL" row.

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
        abort: Set when the caller is stopping, or ``None`` for a caller
            that is never stopped part way, such as ``saneless doctor``.
            The scanner check hands it to the listing, which then ends
            within a fraction of a second, and ``run_checks`` stops between
            checks once it is set.
        paperless_refusal: Why ``paperless`` is None, or None when the
            caller does not know or a client was built.
        scanner_refusal: Why ``scanner`` is None, or None when the caller
            does not know or a backend was built.

    """

    settings: Settings
    scanner: ScannerBackend | None
    paperless: PaperlessClient | None
    profile_storage: ProfileStorage
    skip_scanner: bool = False
    abort: threading.Event | None = None
    paperless_refusal: PaperlessRefusal | None = None
    scanner_refusal: ScannerRefusal | None = None


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

    Every Unicode warning sign -- U+26A0, U+2757 -- renders with emoji
    presentation on at least one shipping platform, so the warning glyph stays
    a plain ASCII ``!``, which has text presentation everywhere and needs no
    variation selector.

    See docs/explanation/decisions/0013-text-presentation-glyphs.md.

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


@dataclass(frozen=True, slots=True)
class _ScannerPreflight:
    """
    What the gate-free preflight hands to enumeration when it cannot decide.

    Carrying the backend here, already narrowed to not-``None``, is how both
    type checkers learn that enumeration has one without a cast.

    Attributes:
        scanner: The scanner backend enumeration will ask.
        probes: One entry per probed saned host: the setting's entries in
            configured order, then the configured ``net:`` device's own host
            when the setting did not already probe it on ``saned_probe.SANED_PORT``.
            Empty when there was nothing to probe.
        may_open: Whether enumeration may open a configured device it does
            not find listed.  False only for a ``net:`` device whose host the
            probe cannot dial, because opening it would make libsane dial
            that host with no timeout and nothing has shown it is up.  A host
            the probe did dial reaches enumeration only when it did not time
            out, so the open cannot hang on it.

    """

    scanner: ScannerBackend
    probes: tuple[saned_probe.HostProbe, ...]
    may_open: bool = True


class _ListingFailure(StrEnum):
    """
    Why a device listing could not see, when it could not.

    Each listing runs in a child process with its own scanner library, so a
    listing that goes wrong there ends in one of three ways the check can
    tell apart from "listed nothing".  ``CRASHED``: the child died from a
    signal, which is the scanner library failing inside a C call.
    ``TIMED_OUT``: the child was still listing at the deadline and was
    stopped, for example because a peer accepted a connection and then said
    nothing.  ``NO_ANSWER``: the child could not be started, or ended without
    a reply that could be read.
    """

    CRASHED = "crashed"
    TIMED_OUT = "timed_out"
    NO_ANSWER = "no_answer"


@dataclass(frozen=True, slots=True)
class _Enumeration:
    """
    What the gated half of the Scanner check saw.

    Attributes:
        devices: What the backend listed, in its order; empty when it listed
            nothing, and also when listing raised -- the verdict does not need
            to tell those two apart, because either way nothing usable was
            seen.
        configured_opened: ``None`` when no open was attempted; otherwise
            whether a configured device that the backend did not list could
            be opened and closed again, which is what a scan does with it.
        open_withheld: True when the configured device was not listed and
            was deliberately not opened, because it is a ``net:`` device
            whose host the pre-probe could not dial.
        failure: ``None`` when the listing ran to an end.  Otherwise the way
            the listing child failed: it crashed, it was stopped at the
            deadline, or it gave no usable answer.  Those are kept apart from
            "listed nothing" because the row must say the check could not
            see, not that nothing is there.

    """

    devices: tuple[DeviceInfo, ...]
    configured_opened: bool | None = None
    open_withheld: bool = False
    failure: _ListingFailure | None = None


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


def _host_problem_clause(
    outcome: saned_probe.SanedOutcome, subject: str, *, plural: bool
) -> str:
    """
    Say what a probe outcome means, as the start of a row's sentence.

    Args:
        outcome: The outcome being reported.
        subject: Who it is about, from ``_hosts_subject``.
        plural: Whether ``subject`` takes a plural verb.

    Returns:
        A clause starting lower-case, with no full stop.

    Raises:
        AssertionError: If the value is not a ``saned_probe.SanedOutcome`` member.

    """
    match outcome:
        case saned_probe.SanedOutcome.TIMED_OUT:
            clause = (
                f"{subject} are not answering"
                if plural
                else f"{subject} is not answering"
            )
        case saned_probe.SanedOutcome.REFUSED:
            clause = (
                f"{subject} are on, but their scanner service is not running"
                if plural
                else f"{subject} is on, but its scanner service is not running"
            )
        case saned_probe.SanedOutcome.UNRESOLVED:
            clause = f"{subject} could not be found by name"
        case saned_probe.SanedOutcome.REJECTED:
            clause = (
                f"{subject} are refusing this machine"
                if plural
                else f"{subject} is refusing this machine"
            )
        case saned_probe.SanedOutcome.HEALTHY:
            clause = f"{subject} are answering" if plural else f"{subject} is answering"
        case _:
            assert_never(outcome)
    return clause


def _host_problem_next_step(outcome: saned_probe.SanedOutcome) -> str:
    """
    Return what to do about one probe outcome.

    Each state was measured against a real saned: a host that was refusing
    this machine, not listening, or not answering is picked up again by the
    next Check, so pressing Check again is honest advice for all three.  So
    is a name that did not resolve, when the fix is in DNS or the hosts file:
    every listing runs in a fresh process whose scanner library resolves each
    ``SANE_NET_HOSTS`` entry afresh (``sane_init`` calling ``add_device`` in
    ``backend/net.c``).  A fix to a saneless setting is different, because
    settings are read once, at start, so that half of the advice says to
    restart saneless.

    That next step names every place the name can have come from, because
    the row cannot say which: ``[scanner] host``, a ``net:`` id in
    ``[scanner] device`` (whose host is probed too), and ``SANE_NET_HOSTS``,
    which wins over ``[scanner] host`` whenever it is exported and not empty
    (``effective_sane_net_hosts``).  Naming only the config file would send
    an operator with the variable exported to edit a setting that changes
    nothing.  The variable is named; the host never is (ASVS 4.0.3 V7.4).

    Args:
        outcome: The outcome being reported.

    Returns:
        A next step, or the empty string when there is nothing to do.

    Raises:
        AssertionError: If the value is not a ``saned_probe.SanedOutcome`` member.

    """
    match outcome:
        case saned_probe.SanedOutcome.TIMED_OUT:
            next_step = f"Check the scanner host is switched on and on the network, then {RETRY_PLACEHOLDER}."
        case saned_probe.SanedOutcome.REFUSED:
            next_step = f"Start saned on the scanner host, or check it is listening on the network, then {RETRY_PLACEHOLDER}."
        case saned_probe.SanedOutcome.UNRESOLVED:
            next_step = f"Check the host name in [scanner] host or [scanner] device, or in SANE_NET_HOSTS if that is set. If you fixed the name in DNS or the hosts file, {RETRY_PLACEHOLDER}; if you changed a setting, restart saneless."
        case saned_probe.SanedOutcome.REJECTED:
            next_step = f"Add this machine to saned.conf on the scanner host, then {RETRY_PLACEHOLDER}."
        case saned_probe.SanedOutcome.HEALTHY:
            next_step = ""
        case _:
            assert_never(outcome)
    return next_step


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
    is the same reason the fallback row omits the folder path (ASVS 4.0.3 V7.4).
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


def _config_file_as_named(
    settings: Settings, path: Path, *, absolute_paths: bool
) -> str:
    """
    Spell a found config file the way the surface asking may show it.

    The same two spellings as ``_stale_file_as_named``, for the same two
    callers: the documented spelling for the LAN-visible strip, the absolute
    path for ``saneless doctor`` and the log.

    Args:
        settings: The settings whose recorded search found the file.
        path: A file the search found under the current name.
        absolute_paths: True for the terminal spelling.

    Returns:
        The file, spelled for the surface that asked.

    Raises:
        AssertionError: If the settings carry no recording.  Only a recorded
            search can find more than one file, so reaching this means the
            state derivation and this function have come apart.

    """
    discovery = settings.config_discovery
    if discovery is None:
        msg = "A shadowed config file can only come from a recorded search"
        raise AssertionError(msg)
    if absolute_paths:
        return str(path.absolute())
    return discovery.documented_spelling(path)


def leftover_config_check(
    settings: Settings, *, absolute_paths: bool = False
) -> CheckResult | None:
    """
    Report an old-name file left beside a config file that did load.

    Its own function because two states need it.  It is the Configuration
    row when a leftover is the only thing wrong; and when a shadowed file
    outranks it on that row, a terminal surface can still tell the operator
    about the leftover in these same sentences, with no second copy of them.

    Why the next step says move before it says delete: after an upgrade the
    leftover can hold the only copy of the Paperless URL and token, and a
    bare "delete it" would be advice to destroy the configuration.

    Args:
        settings: The settings in hand, carrying the search that built them.
        absolute_paths: True to spell the file as a resolved path, for the
            terminal and the log; False for the LAN-visible strip.

    Returns:
        The amber row, or None when no file loaded or no leftover was found.

    """
    discovery = settings.config_discovery
    if discovery is None or discovery.loaded is None or not discovery.stale:
        return None
    named = _stale_file_as_named(settings, absolute_paths=absolute_paths)
    return CheckResult(
        key=CheckKey.CONFIGURATION,
        state=CheckState.WARN,
        # One literal, deliberately over the 88-column guide (E501 is off in
        # this project): the sentence is pinned word for word, and a grep for
        # it has to find it on one line.
        message=f"Using {CONFIG_FILENAME}; an old {LEGACY_CONFIG_FILENAME} is being ignored.",
        next_step=(
            f"Move anything you still need from {named} into the "
            f"{CONFIG_FILENAME} in use, then delete {named} and "
            "restart saneless."
        ),
    )


def _shadowed_config_check(settings: Settings, *, absolute_paths: bool) -> CheckResult:
    """
    Report a config file in use with at least one more that is not read.

    Args:
        settings: The settings in hand, carrying the search that built them.
        absolute_paths: True to spell the files as resolved paths.

    Returns:
        The amber row naming the file in use and every file not read.

    Raises:
        AssertionError: If the recording holds fewer than two found files,
            which the shadowed state cannot come from.

    """
    discovery = settings.config_discovery
    if discovery is None or len(discovery.found) <= 1:
        msg = "A shadowed config state needs a recording that found two files"
        raise AssertionError(msg)
    used, *others = (
        _config_file_as_named(settings, path, absolute_paths=absolute_paths)
        for path in discovery.found
    )
    unread = " and ".join(others)
    if len(others) == 1:
        # One literal per sentence, over the 88-column guide for the reason
        # the leftover row gives.
        message = f"Using {used}; {unread} is also there and is not read."
        next_step = f"If that is not deliberate, move anything you still need from {unread} into {used}, then delete {unread} and restart saneless."
    else:
        message = f"Using {used}; {unread} are also there and are not read."
        next_step = f"If that is not deliberate, move anything you still need from {unread} into {used}, then delete them and restart saneless."
    return CheckResult(
        key=CheckKey.CONFIGURATION,
        state=CheckState.WARN,
        message=message,
        next_step=next_step,
    )


def configuration_check(
    settings: Settings, *, absolute_paths: bool = False
) -> CheckResult:
    """
    Report which configuration file is in use, and whether an old one is not.

    The row that exists because the other rows once went red or amber for one
    missing file and not one of them named it.  Its outcomes come from
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
    configuration.  Those sentences live in ``leftover_config_check``.

    Why a shadowed file is amber and not red: more than one ``saneless.toml``
    can be deliberate, such as a per-user file overriding the system one, so
    nothing may have failed.  But only the first is read, and the one an
    operator edits may be another, so the row names the file in use and every
    file not read, and its next step allows for the override being meant.

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
            leftover = leftover_config_check(settings, absolute_paths=absolute_paths)
            if leftover is None:
                msg = "A leftover state needs a recording with a loaded file and a leftover"
                raise AssertionError(msg)
            return leftover
        case ConfigFileState.LOADED_WITH_SHADOWED:
            return _shadowed_config_check(settings, absolute_paths=absolute_paths)
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


def _scanner_host_unanswered(probes: tuple[saned_probe.HostProbe, ...]) -> CheckResult:
    """
    Build the row for a scanner host the check must not enumerate.

    This is the row for timed-out hosts -- a peer that accepted the
    connection and then said nothing counts as timed out -- and only for
    them: ``saned_probe.blocks_enumeration`` says why they are kept out of libsane.
    With several hosts it reports the worst outcome among them, by
    ``saned_probe.outcome_severity``, and how many of the hosts share it, so one dead
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
    scanner is a timed-out network host reports amber, so ``saneless doctor``
    exits 0 for it.  That is accepted: a scripted gate is keyed on red alone
    by design, the row is still visible, it still names the scanner host as
    the thing that is wrong, and it still carries the next step that fixes
    it, so a human loses nothing.

    Neither string names a host, its address or its port; the only thing
    interpolated is a count.  A LAN address on a LAN-visible page is the same
    class of disclosure as the SANE device id ``_device_label`` refuses to
    print (ASVS 4.0.3 V7.4).

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
    ``StartupProfiles`` takes the gate around ``get_devices`` and
    ``get_capabilities`` as the worker thread's first act at startup
    (``ScanWorker._run`` runs it before it takes its first job), while
    ``_current_job_id`` is still ``None``
    (``ScanWorker._process_job`` sets it).  The lifespan starts the worker
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
    exception either (ASVS 4.0.3 V7.4), which is the same omission
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


def _scanner_would_not_start() -> CheckResult:
    """
    Build the "the scanner library is installed and would not start" row.

    Not the not-installed row: python-sane imported, and ``sane.init()``
    refused.  Installing scanner support again changes nothing, and the
    reason is in the log, which is where the next step sends the reader.
    The row names no backend, path or host, because the log has those.

    Returns:
        The red scanner-support-would-not-start row.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.FAIL,
        message="Scanner support could not be started.",
        next_step=(
            "See the saneless log for why the scanner library would not start, "
            f"fix the SANE setup on this machine, then {RETRY_PLACEHOLDER}."
        ),
    )


# The outcomes that reach enumeration and explain why a configured ``net:``
# device is missing: its host refused the connection, refused this machine, or
# could not be found by name.  A timed-out host ends the check before
# enumeration, and a healthy one explains nothing.
_CONFIGURED_HOST_OUTCOMES: Final = frozenset(
    {
        saned_probe.SanedOutcome.REFUSED,
        saned_probe.SanedOutcome.REJECTED,
        saned_probe.SanedOutcome.UNRESOLVED,
    }
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


def _worst_host_clause(
    probes: tuple[saned_probe.HostProbe, ...],
) -> tuple[str, saned_probe.SanedOutcome]:
    """
    Say what the worst of the probed hosts found, and how many hosts share it.

    Args:
        probes: What each configured host's probe found; at least one.

    Returns:
        The clause for the worst outcome, counted by ``_hosts_subject``, and
        that outcome, whose next step the row carries.

    """
    worst = saned_probe.worst_outcome(probes)
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
    probes: tuple[saned_probe.HostProbe, ...], subject: str, unchosen: int
) -> CheckResult:
    """
    Build the row for a scanner that can be used.

    A host problem still shows, in amber, because scanning works: the rule is
    that a working appliance never goes red.  It is reported ahead of the
    several-devices warning, because it is the one that is a failure.

    The sentence has one "but", and a stopped scanner service is worded for
    it on purpose.  ``_host_problem_clause`` says that outcome as "is on, but
    its scanner service is not running", which is right as a sentence of its
    own and reads as two "but"s after "is ready, but", so this row names the
    service and counts the hosts instead.

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
    if any(probe.outcome is not saned_probe.SanedOutcome.HEALTHY for probe in probes):
        clause, worst = _worst_host_clause(probes)
        if worst is saned_probe.SanedOutcome.REFUSED:
            count = sum(1 for probe in probes if probe.outcome is worst)
            hosts, _plural = _hosts_subject(count, len(probes))
            clause = f"the scanner service is not running on {hosts}"
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


def _scanner_host_problem_row(
    clause: str, outcome: saned_probe.SanedOutcome
) -> CheckResult:
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

    The next step covers both ways this happens.  A scanner that is off or
    unplugged is seen by the next Check once it is back, because every
    listing runs in a fresh process with a fresh scanner library, so that
    half says press Check again.  If ``saneless devices`` does not list it
    either, the configured id is wrong, and changing ``[scanner] device`` is a
    settings edit, which is read once at start, so only that half says to
    restart saneless.

    Returns:
        The red Scanner row.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.FAIL,
        message="The configured scanner was not found.",
        next_step=f"Check it is switched on and connected, then {RETRY_PLACEHOLDER}. If saneless devices does not list it, set [scanner] device to one it lists, then restart saneless.",
    )


def _scanner_configured_unprobed_row() -> CheckResult:
    """
    Build the amber row for an unlisted ``net:`` device that was not opened.

    The configured device is a ``net:`` device the backend did not list, and
    its host is one the pre-probe cannot dial, so the check did not open it:
    opening it would make libsane dial that host with no timeout.  Amber, not
    red, because nothing was found wrong -- the device may well open for a
    scan -- and the rule is that a working appliance never goes red.

    The next step names both ways out.  A host in ``[scanner] host`` is one
    SANE lists devices from, so the device becomes a listed one; a device
    ``saneless devices`` lists is one the check can find.  Either is a config
    edit, which needs a restart.  Neither names the host (ASVS 4.0.3 V7.4).

    Returns:
        The amber Scanner row.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.WARN,
        message="The configured scanner is not listed, and its host cannot be checked in advance, so the scanner could not be checked.",
        next_step="Add the configured scanner's host to [scanner] host, or set [scanner] device to one saneless devices lists, then restart saneless.",
    )


def _scanner_listing_crashed_row() -> CheckResult:
    """
    Build the amber row for a listing whose child process crashed.

    The scanner library failed inside the listing child, so the check saw
    nothing at all.  Amber, not red: a crash proves the listing failed, not
    that scanning is impossible -- the next scan runs with its own fresh
    library and may well work -- and the rule is that a working appliance
    never goes red, so ``saneless doctor`` still exits 0 for it.  The child
    is gone and the next listing starts another, so pressing Check again is
    the whole of the advice.

    Nothing is interpolated: the crash is not tied to any one host, and a host
    or device id on this LAN-visible row would be a disclosure (ASVS 4.0.3 V7.4).

    Returns:
        The amber Scanner row.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.WARN,
        message="The scanner library failed while listing scanners, so the scanner could not be checked.",
        next_step=f"{RETRY_SENTENCE_PLACEHOLDER}.",
    )


def _scanner_listing_timed_out_row() -> CheckResult:
    """
    Build the amber row for a listing stopped at its deadline.

    The listing child was still waiting when the deadline came, and was
    stopped.  That is a peer somewhere that accepted a connection and then
    said nothing, which may be the scanner itself or a scanner host the
    pre-probe could not dial in advance.  Amber, not red, for the same reason
    as the crash row: the check could not see, which is not proof that
    scanning is impossible, and a working appliance never goes red.

    The next step asks for the scanner, and its host if it has one, to be on
    and reachable, and it deliberately avoids "switched on and connected",
    the phrase no row reporting a rejection may use.  Nothing is interpolated,
    for the same reason as the crash row (ASVS 4.0.3 V7.4).

    Returns:
        The amber Scanner row.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.WARN,
        message="The scanner library did not finish listing scanners in time, so the scanner could not be checked.",
        next_step=f"Check the scanner, and its scanner host if it has one, are switched on and reachable, then {RETRY_PLACEHOLDER}.",
    )


def _scanner_listing_no_answer_row() -> CheckResult:
    """
    Build the amber row for a listing child that gave no usable answer.

    The child could not be started, or it ended without a reply the check
    could read, so the check saw nothing at all.  Amber, not red, for the
    same reason as the crash row: the check could not see, which is not proof
    that scanning is impossible, and a working appliance never goes red.
    Whatever the child printed is in the log, and the next listing starts
    another child, so pressing Check again is the whole of the advice.

    Nothing is interpolated, for the same reason as the crash row (ASVS 4.0.3
    V7.4).

    Returns:
        The amber Scanner row.

    """
    return CheckResult(
        key=CheckKey.SCANNER,
        state=CheckState.WARN,
        message="The scanner library gave no usable answer while listing scanners, so the scanner could not be checked.",
        next_step=f"{RETRY_SENTENCE_PLACEHOLDER}.",
    )


def _scanner_nothing_found_row(hosts: int) -> CheckResult:
    """
    Build the red row for no scanner at all, with every probed host healthy.

    Both next steps end in pressing Check again.  Every listing runs in a
    fresh process with a fresh scanner library, so a scanner that is switched
    on, plugged in, or added to the scanner host is seen by the next Check,
    with no restart; ``saneless devices`` would see no more than that.

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
            next_step=f"Check the scanner is switched on and connected, then {RETRY_PLACEHOLDER}.",
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
        next_step=f"Check the scanner is switched on and connected to the scanner host, then {RETRY_PLACEHOLDER}.",
    )


def _scanner_unusable_row(
    probes: tuple[saned_probe.HostProbe, ...], configured_device: str
) -> CheckResult:
    """
    Build the red row when no usable scanner was seen, naming the likeliest cause.

    The more specific cause wins.  A configured ``net:`` device whose own
    host had no scanner service running, was refusing this machine, or could
    not be found by name is missing *because of* that host, so the row says
    so.  A configured device that is not a ``net:`` device is simply not
    found, even if some unrelated network host is also bad, because a network
    host does not explain a local device's absence.  Only the own host's probe
    on ``saned_probe.SANED_PORT`` is read, because that is the port libsane opened the
    device on; a ``host:port`` setting's answer on another port does not
    explain the device's absence.

    Args:
        probes: What each configured host's probe found; none blocks
            enumeration.
        configured_device: The configured ``scanner.device``, possibly empty.

    Returns:
        The red Scanner row.

    """
    if configured_device:
        entry = saned_probe.net_device_entry(configured_device)
        own_host = next(
            (
                probe
                for probe in probes
                if probe.host == entry
                and probe.port == saned_probe.SANED_PORT
                and probe.outcome in _CONFIGURED_HOST_OUTCOMES
            ),
            None,
        )
        if own_host is None:
            return _scanner_configured_missing_row()
        clause = _host_problem_clause(
            own_host.outcome, "the configured scanner's host", plural=False
        )
        return _scanner_host_problem_row(clause, own_host.outcome)
    if any(probe.outcome is not saned_probe.SanedOutcome.HEALTHY for probe in probes):
        return _scanner_host_problem_row(*_worst_host_clause(probes))
    return _scanner_nothing_found_row(len(probes))


def _scanner_listing_failure_row(failure: _ListingFailure) -> CheckResult:
    """
    Pick the amber row for the way a listing child failed.

    A ``match`` ending in ``assert_never``, so a new way of failing stops
    type-checking here until it has a row.

    Args:
        failure: How the listing child failed.

    Returns:
        The row for that failure.

    """
    match failure:
        case _ListingFailure.CRASHED:
            return _scanner_listing_crashed_row()
        case _ListingFailure.TIMED_OUT:
            return _scanner_listing_timed_out_row()
        case _ListingFailure.NO_ANSWER:
            return _scanner_listing_no_answer_row()
        case _:
            assert_never(failure)


def _scanner_verdict(
    probes: tuple[saned_probe.HostProbe, ...],
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
    ``net:`` device whose own host had no scanner service running, was
    refusing this machine, or could not be found by name reports that host's
    problem, which explains the absence.  A configured device that is not a
    ``net:`` device and is missing is reported as not found, whatever an
    unrelated host is doing.  With nothing configured, a host problem is
    reported ahead of the several-devices warning.  Several bad hosts are
    reported by the worst outcome, with a count.

    The next steps follow what actually clears each state, which was measured
    against a real saned and agrees with sane-backends' source:

    - Each listing runs in a fresh child process with a fresh scanner
      library.  That library resolves every ``SANE_NET_HOSTS`` entry afresh
      and opens a new control connection to every host (``sane_init`` calling
      ``add_device``, then ``connect_dev``, in ``backend/net.c``), so a name
      that becomes resolvable, a host that starts answering, and a host that
      gains a scanner are all picked up on the next Check.  Those rows, and
      the rows for a scanner that was not found, say press Check again.
    - A restart is advised only where the fix is a change to saneless's own
      settings, which are read once, at start: a host name corrected in a
      setting, ``[scanner] device`` changed to a device ``saneless devices``
      lists, and a configured ``net:`` device whose host cannot be probed.
    - A refused host is enumerated, because a refused connect returns at
      once inside libsane.  A timed-out host is not.

    A host that must not be enumerated -- one that timed out -- gets the
    same row the preflight returns for it, so the two can never disagree.
    A listing that crashed, was stopped at its deadline or gave no usable
    answer gets its own amber row next, ahead of anything the devices would
    say, because the check could not see and that is not the same as seeing
    nothing.

    Rows interpolate counts and ``_device_label`` output only.  The
    configured id and the listed device ids are compared, never rendered,
    because they are LAN addresses on a LAN-visible page (ASVS 4.0.3 V7.4).

    Args:
        probes: What each configured host's probe found, in configured order;
            empty when no host is configured.
        enumeration: What the gated half of the check saw.
        configured_device: The configured ``scanner.device``, possibly empty.

    Returns:
        Exactly one result for ``CheckKey.SCANNER``.

    """
    if any(saned_probe.blocks_enumeration(probe.outcome) for probe in probes):
        return _scanner_host_unanswered(probes)
    if enumeration.failure is not None:
        return _scanner_listing_failure_row(enumeration.failure)
    subject = _scanner_ready_subject(enumeration, configured_device)
    if subject is None and enumeration.open_withheld:
        return _scanner_configured_unprobed_row()
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
    ``getaddrinfo`` takes no timeout, as
    ``saned_probe.PROBE_CONNECT_SECONDS`` says at
    length -- so a check holding the gate across it can park a
    ``ScanWorker._scan_job`` whose job row already reads ``SCANNING`` for as
    long as a broken resolver takes, and ``POST /api/checks/refresh`` can
    re-arm that parking every couple of seconds.

    The order inside it is the order ``_check_scanner`` always had.  A machine
    with no python-sane is its own row and is decided without touching
    anything.  Then every entry ``saned_probe.saned_hosts`` returns for the host
    list
    SANE will use is probed with ``saned_probe.probe_saned``, with no short circuit:
    libsane dials every entry, so a dead second host holds the listing until
    ``LISTING_DEADLINE_SECONDS`` however well the first one answers.  When
    ``scanner.device`` is a ``net:`` id whose host none of those entries
    covers on ``saned_probe.SANED_PORT``, that host is probed on
    ``saned_probe.SANED_PORT`` too (``saned_probe.configured_device_probes``),
    because the check may
    open the device and opening it dials its host there.

    What is *not* probed is stated rather than hidden, because libsane still
    dials it inside the listing.  ``saned_probe.saned_hosts`` returns at most
    ``saned_probe._MAX_PROBE_HOSTS`` entries and drops the ones it will not
    guess at: an
    IPv6 literal, a numeric shorthand, and the rest of a setting it refuses.
    Hosts named only in ``net.conf`` are not read at all.  And a two-segment
    ``host:port`` setting is probed as one host on that port, where libsane
    dials ``host`` and the port number as two hosts, both on
    ``saned_probe.SANED_PORT``
    (a configured ``net:`` device on that host is the exception, as above).
    Enumeration still runs beside such an entry, as it did before the
    pre-probe existed, so a dead host there can still hold the listing until
    ``LISTING_DEADLINE_SECONDS`` stops it; refusing to enumerate instead would turn
    every working IPv6 or five-host setup permanently amber.

    If any probed host timed out -- including one that accepted the
    connection and said nothing -- the check ends right there with the amber
    ``_scanner_host_unanswered`` row, and ``saned_probe.blocks_enumeration`` says why it
    may not be enumerated.  Refused, rejected, unresolved and healthy hosts,
    and a setting with no host to probe, go on to enumeration.

    A stop ends the probing: each probe looks at ``context.abort`` before
    it dials and while it waits for saned's reply, and an aborted probe
    raises rather than becoming a row, because a host nobody finished asking
    about has no outcome.

    Args:
        context: The injected dependencies and configuration.

    Returns:
        The row, when it can be decided here; otherwise what enumeration
        needs, which is the backend, what each host's probe found, and
        whether an unlisted configured device may be opened.

    Raises:
        saned_probe.PreProbeAbortedError: ``context.abort`` was set during a probe.

    """
    scanner = context.scanner
    if scanner is None:
        if context.scanner_refusal is ScannerRefusal.START_FAILED:
            return _scanner_would_not_start()
        return _scanner_support_missing()
    probes = tuple(
        saned_probe.HostProbe(
            host,
            saned_probe.probe_saned(
                host,
                port,
                saned_probe.PROBE_CONNECT_SECONDS,
                saned_probe.PROBE_HANDSHAKE_SECONDS,
                context.abort,
            ),
            port,
        )
        for host, port in saned_probe.saned_hosts(
            saned_probe.saned_host_setting(context.settings)
        )
    )
    probes, may_open = saned_probe.configured_device_probes(
        context.settings.scanner.device, probes, context.abort
    )
    if any(saned_probe.blocks_enumeration(probe.outcome) for probe in probes):
        return _scanner_host_unanswered(probes)
    return _ScannerPreflight(scanner=scanner, probes=probes, may_open=may_open)


def _scanner_enumeration(
    scanner: ScannerBackend,
    configured_device: str,
    *,
    may_open: bool,
    abort: threading.Event | None,
) -> _Enumeration:
    """
    Ask the backend what it can see, which is the part that enters SANE.

    This is the only region of the Scanner check that enters libsane, and it
    runs under the scanner gate on the status strip; it decides nothing.  It
    is one call, ``scanner.list_and_open``, which lists the devices and, only
    when ``scanner.device`` is set and no listed device has exactly that id,
    opens and closes that id once.  Both steps run inside the backend's
    short-lived listing child, so a check never uses this process's libsane
    and always sees a fresh control connection to every scanner host.  The
    child is killed and reaped at ``LISTING_DEADLINE_SECONDS``, and the call
    does not return, or raise, until it has been reaped.

    The open exists because SANE opens ids it never lists.  An ``escl:`` URL
    with no ``escl.conf`` entry is one, and a ``net:`` device on a host that
    is not in ``SANE_NET_HOSTS`` is another: libsane's net backend adds that
    host when the device is opened.  Opening the id is what a scan does first,
    so an unlisted device that opens is one a scan can use, and reporting it
    red would break the rule that a working appliance never goes red.  It is
    attempted only in that absent case, never on every check, and also when
    the listing itself failed, because a scan opens a configured id without
    listing anything.

    A child that crashed, that was stopped at its deadline, or that gave no
    usable answer is not a listing that found nothing: each has its own row,
    so each is recorded as what it was.  The launcher has already logged the
    crash or the timeout, so nothing more is logged for them here; no answer
    is logged by its class name.  Any other failure is logged by its class
    name only and treated as an empty listing, with a configured id counted
    as not opened.

    It is never reached when the preflight stopped the check, and the
    preflight probes a ``net:`` device's own host before this can open it, so
    no *probed* host that timed out, or accepted the connection and then said
    nothing, is listed or opened here (``saned_probe.blocks_enumeration``).  A refused
    host is listed, and a device on it opened, because a refused connect
    returns at once inside libsane.  A ``net:`` device whose host could not
    be probed at all is not opened either (``may_open``): the call is asked
    to open nothing.  The listing itself still dials every host libsane
    knows of, including any the preflight could not probe;
    ``_scanner_preflight`` lists which those are.

    A listing stopped on ``abort`` is none of those: the caller is stopping,
    so there is no row to give, and recording it as a failed listing would
    log a warning and draw a scanner fault for a stop.  It propagates, and
    ``run_checks`` ends the run there.  The launcher has already logged it,
    at INFO.

    Args:
        scanner: The backend to ask.
        configured_device: The configured ``scanner.device``, possibly empty.
        may_open: Whether an unlisted configured device may be opened; False
            for a ``net:`` device whose host the preflight could not probe.
        abort: The caller's abort Event, handed to the listing, or ``None``.

    Returns:
        What was listed, whether an unlisted configured device opened or was
        deliberately left unopened, and whether the listing crashed, ran out
        of time or gave no usable answer.

    Raises:
        ListingAbortedError: The listing was stopped on ``abort``.

    """
    open_target = configured_device if may_open else ""
    try:
        survey = scanner.list_and_open(open_target, abort=abort)
    except ListingAbortedError:
        raise
    except ListingCrashedError:
        return _Enumeration(devices=(), failure=_ListingFailure.CRASHED)
    except ListingTimedOutError:
        return _Enumeration(devices=(), failure=_ListingFailure.TIMED_OUT)
    except ListingNoAnswerError as exc:
        logger.warning("Scanner enumeration failed: %s", type(exc).__name__)
        return _Enumeration(devices=(), failure=_ListingFailure.NO_ANSWER)
    except Exception as exc:
        # The backend raises ScanError, for instance while a read is stuck,
        # but a backend is free to raise anything, so the boundary catches
        # Exception.  Only the type name goes any further.
        survey = DeviceSurvey(
            devices=(),
            list_error=type(exc).__name__,
            configured_opened=False if open_target else None,
        )
    # The survey carries class names only, never an id or exception text: a
    # ``net:`` id is a LAN address, and the text of a SANE error usually
    # repeats it.
    if survey.list_error is not None:
        logger.warning("Scanner enumeration failed: %s", survey.list_error)
    if survey.open_error is not None:
        logger.warning("Configured scanner could not be opened: %s", survey.open_error)
    return _enumeration_from(survey, configured_device, may_open=may_open)


def _enumeration_from(
    survey: DeviceSurvey, configured_device: str, *, may_open: bool
) -> _Enumeration:
    """
    Turn what the listing found into the verdict's plain record.

    Args:
        survey: What the backend's list-then-open found.
        configured_device: The configured ``scanner.device``, possibly empty.
        may_open: Whether an unlisted configured device could be opened.

    Returns:
        The listed devices, and for an unlisted configured device, whether it
        opened or was deliberately left unopened.

    """
    devices = survey.devices
    if not configured_device or any(
        device.name == configured_device for device in devices
    ):
        return _Enumeration(devices=devices)
    if not may_open:
        return _Enumeration(devices=devices, open_withheld=True)
    # ``None`` here would mean no open was attempted although the id is not
    # listed, which the backend does only when its listing included it -- the
    # branch above.  Anything but a confirmed open is therefore not opened.
    return _Enumeration(
        devices=devices, configured_opened=survey.configured_opened is True
    )


def _check_scanner(context: CheckContext) -> CheckResult:
    """
    Report whether a scanner is there to scan with.

    The ungated path: this is what ``_dispatch`` calls, and therefore what
    ``saneless doctor`` runs.  It is three steps, in the same order the gated
    ``_scanner_result`` runs them: the preflight, which may settle the row
    before SANE is entered; the enumeration, which is the only step that
    enters SANE; and the verdict, which decides the row from plain values.
    Only the placement of the gate differs between the two functions.

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
    configured_device = context.settings.scanner.device
    enumeration = _scanner_enumeration(
        pre.scanner,
        configured_device,
        may_open=pre.may_open,
        abort=context.abort,
    )
    return _scanner_verdict(pre.probes, enumeration, configured_device)


def _paperless_next_step(
    status: ConnectionStatus, *, https_upgrade: bool = False
) -> str:
    """
    Return what to do about one connection outcome.

    The sentences are fixed user copy, and they pair with the messages
    ``connection_status_message`` already owns -- this module authors the
    remedy, never the diagnosis, so the two surfaces cannot disagree about
    what happened even if they disagreed about what to do.

    A redirect's next step never names where it pointed: that is upstream
    text, and this row is shown on the LAN-visible status strip.  It says to
    use ``https://`` only when the redirect changed nothing but the scheme.

    Args:
        status: The connection-test outcome.
        https_upgrade: Whether a redirect went from ``http`` to ``https`` on
            the same address.  Read only for REDIRECTED.

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
            next_step = f"Check paperless-ngx is healthy, then {RETRY_PLACEHOLDER}."
        case ConnectionStatus.UNREACHABLE:
            next_step = (
                "Check paperless-ngx is running and on the network, "
                f"then {RETRY_PLACEHOLDER}."
            )
        case ConnectionStatus.INCOMPATIBLE:
            next_step = (
                "saneless needs paperless-ngx 2.16 or later (API version 9 or 10); "
                f"upgrade paperless-ngx, then {RETRY_PLACEHOLDER}."
            )
        case ConnectionStatus.REDIRECTED if https_upgrade:
            next_step = (
                "Change paperless.url in the saneless config file to start with "
                "https://, then restart saneless."
            )
        case ConnectionStatus.REDIRECTED:
            next_step = (
                "Set paperless.url in the saneless config file to the address "
                "paperless-ngx answers on, then restart saneless."
            )
        case ConnectionStatus.MISCONFIGURED:
            next_step = (
                "Correct paperless.url and paperless.token in the saneless config "
                "file, then restart saneless."
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
    fails inside httpx2 before anything is sent.  The probe would call that
    MISCONFIGURED, which is true but names both settings; an address that
    was never set gets its own, plainer row here.

    A redirect's row names no address, because the strip is visible to
    anyone on the LAN: only whether it was a plain switch to ``https://``
    reaches the next step.  The sanitised target goes in ``terminal_detail``
    instead, which ``saneless doctor`` prints and the strip never renders.

    A ``None`` client means one could not be constructed, and
    ``context.paperless_refusal`` says why.  ``PaperlessClient.__init__``
    refuses a URL httpx2 will not parse or that carries a user name or
    password, a token an HTTP header cannot carry, and a TLS trust store it
    cannot read.  The first three are the configuration row that names
    ``paperless.url`` and ``paperless.token``; the trust store is a row of its
    own that names ``SSL_CERT_FILE`` and ``SSL_CERT_DIR``, because the settings
    are not what is wrong.  A caller that does not say why keeps the "not
    found at that URL" row.

    Args:
        context: The injected dependencies and configuration.

    Returns:
        Exactly one result for ``CheckKey.PAPERLESS``.

    """
    if is_placeholder_token(context.settings.paperless.token.get_secret_value()):
        return CheckResult(
            key=CheckKey.PAPERLESS,
            state=CheckState.FAIL,
            message=f"{sentence_case(UNSET_CREDENTIAL_CLAUSE)}.",
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
    https_upgrade = False
    terminal_detail = ""
    if client is None and context.paperless_refusal is PaperlessRefusal.TRUST_STORE:
        return CheckResult(
            key=CheckKey.PAPERLESS,
            state=CheckState.FAIL,
            message="The TLS trust store could not be read.",
            next_step=(
                "Check that SSL_CERT_FILE names a readable CA bundle file and "
                "SSL_CERT_DIR a readable directory, or unset them, then restart "
                "saneless."
            ),
        )
    if client is None:
        status = (
            ConnectionStatus.MISCONFIGURED
            if context.paperless_refusal is PaperlessRefusal.CONFIGURATION
            else ConnectionStatus.NOT_FOUND
        )
    else:
        probe = client.probe_connection(
            timeout=httpx2.Timeout(
                saned_probe.PROBE_READ_SECONDS,
                connect=saned_probe.PROBE_CONNECT_SECONDS,
            )
        )
        status = probe.status
        https_upgrade = probe.https_upgrade
        if probe.redirect_target is not None:
            terminal_detail = f"Redirected to {probe.redirect_target}"
    state = CheckState.OK if status is ConnectionStatus.CONNECTED else CheckState.FAIL
    return CheckResult(
        key=CheckKey.PAPERLESS,
        state=state,
        message=connection_status_message(status),
        next_step=_paperless_next_step(status, https_upgrade=https_upgrade),
        terminal_detail=terminal_detail,
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
    # Unlike the data and working folders, a missing consume folder is a fault
    # and not judged by the folder above it: saneless never creates this one,
    # because it belongs to paperless-ngx, which has to be watching it.
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


# The two settings the Data folder row judges, in the order it judges them.
# The row names whichever is wrong by its key: a key is in the saneless config
# file the reader will open, and a path would put a host filesystem path on a
# LAN-visible page.
_DATA_DIR_KEY: Final = "output.data_dir"
_TMP_DIR_KEY: Final = "output.tmp_dir"


@dataclass(frozen=True, slots=True)
class _FolderFault:
    """
    What is wrong with one folder setting, in words the status strip may show.

    Attributes:
        problem: A phrase that follows the setting's key, naming no path.
        private: Whether the problem is that another local user could reach
            the folder, rather than that saneless cannot use it.

    """

    problem: str
    private: bool = False


def _folder_fault(path: Path) -> _FolderFault | None:
    """
    Judge a folder setting the way saneless will use it.

    A folder that exists has to be a folder and take a write.  One that does
    not exist yet is created when it is first needed, so it is judged by its
    nearest existing ancestor, which is where creating it would fail: that has
    to be a folder and take a write too.  Anything named by the setting -- a
    file, or a dangling link -- counts as existing, because creating a folder
    there would fail.

    Args:
        path: The configured folder.

    Returns:
        None when saneless can use or create the folder, else what is wrong.

    """
    if os.path.lexists(path):
        if not path.is_dir():
            return _FolderFault("is not a folder")
        if _directory_accepts_a_write(path):
            return None
        return _FolderFault("cannot be written to")
    ancestor = nearest_existing_ancestor(path)
    if not ancestor.is_dir():
        return _FolderFault("is under a file, not a folder")
    if _directory_accepts_a_write(ancestor):
        return None
    return _FolderFault(
        "does not exist and cannot be created, because the folder it would go "
        "in cannot be written to"
    )


def _privacy_fault(path: Path) -> _FolderFault:
    """
    Say why ``check_private_dir`` refused a working folder, without its path.

    The verdict is ``check_private_dir``'s, so the row refuses exactly what
    start-up refuses.  Its message names the path, which the status strip must
    not show, so the kind of problem is read back here for the row's words.

    Args:
        path: The working folder that was refused.

    Returns:
        The problem, in words that name no path.

    """
    try:
        info = os.lstat(path)
    except OSError:
        return _FolderFault("could not be checked")
    if stat.S_ISLNK(info.st_mode):
        return _FolderFault(
            "is a symbolic link, so another local user could redirect the scans "
            "kept there",
            private=True,
        )
    if not stat.S_ISDIR(info.st_mode):
        return _FolderFault("is not a folder")
    if info.st_uid != os.geteuid():
        return _FolderFault(
            "is owned by another user, who could read or replace the scans kept there",
            private=True,
        )
    return _FolderFault(
        "can be written by other users, who could read or replace the scans kept there",
        private=True,
    )


def _working_folder_fault(path: Path) -> _FolderFault | None:
    """
    Judge the working folder the way start-up does: private first, then usable.

    An existing working folder must be a real folder this user owns that
    nobody else can write to, because scans in progress are kept there.  A
    missing one is created 0700 when it is first needed, so only where it
    would be created is judged.

    Args:
        path: The configured working folder.

    Returns:
        None when saneless can use or create the folder, else what is wrong.

    """
    if os.path.lexists(path):
        try:
            check_private_dir(path, key=_TMP_DIR_KEY)
        except ConfigError:
            return _privacy_fault(path)
    return _folder_fault(path)


def _data_folder_failure(key: str, fault: _FolderFault) -> CheckResult:
    """
    Build the red Data folder row for the setting that is wrong.

    Args:
        key: The setting, ``output.data_dir`` or ``output.tmp_dir``.
        fault: What is wrong with the folder it names.

    Returns:
        A FAIL row naming the setting and the problem, and what to do.

    """
    if fault.private:
        next_step = (
            f"Remove that folder or link so saneless creates it privately, or set "
            f"{key} in the saneless config to a folder only saneless's user can "
            f"write to, then restart saneless."
        )
    else:
        next_step = (
            f"Fix that folder, or set {key} in the saneless config to a folder "
            f"saneless can write to, then restart saneless."
        )
    return CheckResult(
        key=CheckKey.DATA_DIR,
        state=CheckState.FAIL,
        message=f"{key} {fault.problem}.",
        next_step=next_step,
    )


def _data_folder_ready(*, data_exists: bool, tmp_exists: bool) -> str:
    """
    Word the green Data folder row for which folders exist yet.

    Args:
        data_exists: Whether the data folder is already there.
        tmp_exists: Whether the working folder is already there.

    Returns:
        The row's message.

    """
    if data_exists and tmp_exists:
        return "The data folder is writable."
    if tmp_exists:
        return "The data folder will be created when first needed."
    if data_exists:
        return (
            "The data folder is writable, and the working folder will be created "
            "when first needed."
        )
    return "The data and working folders will be created when first needed."


def _check_data_dir(context: CheckContext) -> CheckResult:
    """
    Report whether the data and working folders will work, as start-up asks.

    Unlike the fallback folder these are not optional: the job store and the
    preserved scans live in ``output.data_dir``, and every scan is built in
    ``output.tmp_dir``.  Both are judged here, so the row asks the question
    start-up asks and a fault in either is red:

    - a folder that exists has to be a folder that takes a write;
    - one that does not exist yet is fine when it can be created, judged by
      its nearest existing ancestor, the way saneless will create it;
    - an existing working folder must also be private, as
      ``check_private_dir`` decides: not a symbolic link, owned by this user,
      and not writable by anyone else.

    The data folder is judged first, and the first setting that fails decides
    the row, which names it by its key and never by its path.

    Args:
        context: The injected dependencies and configuration.

    Returns:
        Exactly one result for ``CheckKey.DATA_DIR``.

    """
    output = context.settings.output
    data_fault = _folder_fault(output.data_dir)
    if data_fault is not None:
        return _data_folder_failure(_DATA_DIR_KEY, data_fault)
    tmp_fault = _working_folder_fault(output.tmp_dir)
    if tmp_fault is not None:
        return _data_folder_failure(_TMP_DIR_KEY, tmp_fault)
    return CheckResult(
        key=CheckKey.DATA_DIR,
        state=CheckState.OK,
        message=_data_folder_ready(
            data_exists=output.data_dir.exists(), tmp_exists=output.tmp_dir.exists()
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
    Run the scanner check, holding the worker's gate only around the listing.

    This is the only check that takes the gate, because the device listing is
    the only thing any check does inside libsane.  The preflight -- name
    resolution and the saned handshake -- runs with the gate free, and a
    preflight that settles the row never touches it.  The listing, and the
    open of an unlisted configured device, run in the backend's listing child
    inside one non-blocking hold, released in a ``finally`` only after the
    child is reaped; the verdict runs after the release.  A gate already held
    is reported as the neutral busy row, not waited on.

    Holding the gate across resolution could park a scan whose job row already
    reads ``SCANNING`` behind a broken resolver, waiting on it would queue the
    probe behind a scan that runs for minutes, and releasing it while the child
    is still inside libsane would let a scan in beside it.
    ``test_a_gated_run_returns_what_an_ungated_run_returns`` holds this path to
    the rows ``_check_scanner`` gives ``saneless doctor``.

    See docs/explanation/decisions/0003-scanner-gate-is-a-lock.md.

    Args:
        context: The injected dependencies and configuration.
        scanner_gate: The worker's gate, tried without blocking.

    Returns:
        The scanner row, or the neutral busy row when the gate was not free.

    """
    pre = _scanner_preflight(context)
    if isinstance(pre, CheckResult):
        return pre
    configured_device = context.settings.scanner.device
    if not scanner_gate.acquire(blocking=False):
        return _scanner_busy()
    try:
        enumeration = _scanner_enumeration(
            pre.scanner,
            configured_device,
            may_open=pre.may_open,
            abort=context.abort,
        )
    finally:
        scanner_gate.release()
    return _scanner_verdict(pre.probes, enumeration, configured_device)


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
    file.  A caller that wrapped every check made the lock that exists to keep two
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
    whole strip down and with it the checks that passed, and the
    exception text is exactly the thing that must not reach a LAN-visible page.
    That handler covers the gated scanner branch too, and the gate is released
    on the way out of it.

    A caller that is stopping sets ``context.abort``.  The run then ends
    before the next check, and a saned pre-probe or a scanner listing in
    flight ends as aborted, which also ends the run there, so a stop never
    waits out the Paperless check's budget after the scanner check was
    stopped.  The Paperless probe itself is one HTTP request that cannot be
    interrupted: a stop that lands during it waits for it, up to
    ``saned_probe.PROBE_CONNECT_SECONDS`` plus
    ``saned_probe.PROBE_READ_SECONDS``.  What is returned then is
    only the rows of the checks that finished, which is the one case where
    there is not one row per member: it exists only for a stopping caller,
    and that caller stores none of it.

    Args:
        context: The injected dependencies and configuration.
        scanner_gate: The worker's scanner gate, held around the scanner check
            and nothing else, or ``None`` for a caller with no worker to
            exclude -- which is what ``saneless doctor`` is.

    Returns:
        One result per ``CheckKey`` member, in member order, or fewer when
        ``context.abort`` was set part way.

    """
    results: list[CheckResult] = []
    for key in CheckKey:
        if context.abort is not None and context.abort.is_set():
            break
        if key is CheckKey.SCANNER and context.skip_scanner:
            results.append(_scanner_skipped())
            continue
        try:
            if key is CheckKey.SCANNER and scanner_gate is not None:
                results.append(_scanner_result(context, scanner_gate))
            else:
                results.append(_dispatch(key, context))
        except ListingAbortedError, saned_probe.PreProbeAbortedError:
            # The caller is stopping: no row, and not a check that failed.
            break
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
