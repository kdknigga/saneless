"""
The one health-check registry, and the words both surfaces use to show it.

``saneless doctor`` and the web status strip must report the same checks in the
same words, so this module imports nothing from ``saneless.web`` or
``saneless.cli``.  Every dependency arrives on ``CheckContext`` instead, which
is also what lets ``doctor`` report every row with no python-sane installed.

No message or next step carries a filesystem path, a URL, a token value or
exception text (ASVS 4.0.3 V7.4), so the Paperless URL and the fallback folder
are never named.  The one exception is the Configuration row's next step in the
two superseded-name states: it names the file to rename in one of three fixed
documented spellings, taken from the search position, never a resolved host
path.

``CheckResult.terminal_detail`` is outside the rule: only ``saneless doctor``
prints it, on the machine.
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
from saneless.paperless import PROBE_READ_SECONDS
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


# The cold-start row, before any check has run; templates own no vocabulary.
# U+00B7 says "not yet", not "bad".
CHECKING_GLYPH: Final = "·"
CHECKING_STATE_CLASS: Final = "check-checking"
CHECKING_MESSAGE: Final = "Checking…"
# Spoken in place of the cold-start glyph, which is ``aria-hidden``.
CHECKING_STATE_LABEL: Final = "Checking"

# Spoken in front of a skipped row.  A skipped row borrows the cold-start glyph
# and colour but not ``CHECKING_STATE_LABEL``, which would tell a listener a
# probe is running when none is.
SKIPPED_STATE_LABEL: Final = "Not checked"

# How many times the cold-start strip asks before it stops: about twenty
# seconds, the bound for a chain with nothing in flight, such as a refresher
# thread that has died.  The polling body must not carry htmx's ``load``
# trigger: htmx re-fires it on swapped-in content, so the poll would run at the
# round-trip rate and the cap would bound nothing in time.
POLL_ATTEMPT_CAP: Final = 10

# How many times a strip asks while some checker holds the refresher's
# single-flight lock: about 180 s, against roughly 70 s for probing every host,
# one listing stopped at ``LISTING_DEADLINE_SECONDS`` and the Paperless read,
# with the rest left for name resolution, which nothing bounds.  It is a second
# cap rather than an exemption because ``Lock.locked()`` stays true forever if
# the holder dies.
POLL_PROBE_ATTEMPT_CAP: Final = 90

# Shown by the strip once it stops asking, in place of the freshness line.  It
# names only the button on the page, never a path, URL, host or exception text,
# because the page is LAN-visible; only the strip shows it, so it holds no
# retry placeholder.
POLL_GAVE_UP_LINE: Final = "The checks have not run yet. Press Check again to try now."

# Shown instead while a probe is in flight, where pressing Check again would
# only join the running probe.  Held to ``POLL_GAVE_UP_LINE``'s disclosure rule
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
    How one health check came out.

    ``OK`` is nothing to do.  ``WARN`` is a true statement about a deployment
    that still works, such as no fallback folder, and ``FAIL`` is something
    that stops scanning or filing.  ``saneless doctor`` exits non-zero on
    ``FAIL`` only, so a scripted health gate never goes red for tidiness.
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

    ``CONFIGURATION`` is first because member order is reading order, and a
    missing configuration file turns the Paperless, Profiles and Fallback rows
    red or amber without any of them naming the cause.

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

    Member names are internal; only ``check_name`` is user copy.
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

    ``next_step`` is empty for every ``OK`` row and non-empty for every
    ``WARN`` and ``FAIL`` row.  A next step that says to try again holds a
    retry placeholder from ``saneless.vocabulary``, because the strip has a
    Check again button and ``saneless doctor`` is run again; each surface
    renders it with ``render_check_step``.

    ``skipped`` is a flag rather than a state because the row still needs a
    state for its glyph, and "we did not look" is not a verdict.

    Attributes:
        key: Which ``CheckKey`` this is.
        state: How it came out.
        message: A developer-authored sentence.  Never a path, a URL, a token
            or exception text (ASVS 4.0.3 V7.4).
        next_step: What to do about it, for ``WARN`` and ``FAIL`` rows, with
            any retry still a placeholder.
        skipped: True when the probe was deliberately not run.
        terminal_detail: A line for ``saneless doctor`` only, or empty.  It
            may name an address, such as where paperless-ngx redirected to,
            so the LAN-visible strip never renders it.

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

    The web app and ``saneless doctor`` build these differently, and this
    module may know neither.  ``scanner=None`` and ``paperless=None`` mean none
    could be built; a caller that gives no refusal gets the not-installed and
    "not found at that URL" rows.  ``profile_storage`` is the outcome the
    worker recorded, not a fresh probe, because the two in-memory cases cannot
    be told apart afterwards.

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

    The name is a separate element from the message, so
    ``connection_status_message``'s sentences drop into the Paperless row
    verbatim.

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

    The glyph is ``aria-hidden`` and this replaces it, so these are words a
    listener understands rather than the member values.

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

    Templates own no vocabulary, so the mapping lives here.  Each class is an
    alias over an existing colour token.

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

    U+2713 and U+2717 are the glyphs ``Done`` and ``Error`` already use.  The
    warning glyph is a plain ASCII ``!`` because every Unicode warning sign
    renders as an emoji on at least one shipping platform.
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


# The ``check_row_*`` lookups answer what a *row* looks like, which differs
# from what its state looks like whenever ``skipped`` is set.


def check_row_class(result: CheckResult) -> str:
    """
    Return the CSS class that colours one rendered row's glyph.

    A skipped row carries ``CheckState.OK`` so that a health gate does not go
    red for a probe nobody took; colouring by it would paint the row green as
    if a probe had passed.

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

    A skipped row gets the cold-start glyph, never the green tick, because
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

    A skipped row borrows the cold-start glyph and colour but not
    ``CHECKING_STATE_LABEL``, which would say a probe is running.

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

    It is a ``match`` rather than ``max()`` over the member values, because
    alphabetical order is not severity order: ``"FAIL"`` sorts first.

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
            nothing and also when listing raised.
        configured_opened: ``None`` when no open was attempted; otherwise
            whether a configured device that the backend did not list could
            be opened and closed again, which is what a scan does with it.
        open_withheld: True when the configured device was not listed and
            was deliberately not opened, because it is a ``net:`` device
            whose host the pre-probe could not dial.
        failure: ``None`` when the listing ran to an end, otherwise how the
            listing child failed, kept apart from "listed nothing" because the
            row must say the check could not see.

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
        The sentence's subject, "the scanner host" or "1 of 2 scanner hosts",
        and whether its verb is plural.

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
    Return what to do about one probe outcome, or "" when there is nothing.

    Measured against a real saned, the next Check sees a fixed host, and a
    fixed DNS or hosts-file name too, because each listing's libsane resolves
    ``SANE_NET_HOSTS`` afresh; settings are read once, so a setting fix needs a
    restart.  The
    unresolved step names every place the name can come from, because the row
    cannot say which; it never names the host (ASVS 4.0.3 V7.4).
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

    ``os.access`` reads mode bits, and says yes on a writable directory where
    the write itself fails, such as EBUSY on a single-file bind mount.

    Returns:
        True when a file was created and removed, False on any OSError.

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

    A ``net`` backend id embeds the host, a LAN address the page must not
    publish (ASVS 4.0.3 V7.4).

    Returns:
        A bounded vendor and model, or "" when the backend reported neither.

    """
    label = " ".join(f"{device.vendor} {device.model}".split())
    if len(label) > _DEVICE_LABEL_MAX_LENGTH:
        label = label[: _DEVICE_LABEL_MAX_LENGTH - 1].rstrip() + "…"
    return label


def _stale_file_as_named(settings: Settings, *, absolute_paths: bool) -> str:
    """
    Spell the superseded-name file the row is about to tell someone to rename.

    The LAN-visible strip gets the documented spelling, a constant per search
    position; ``saneless doctor`` and the log, read on the machine, get the
    absolute path.

    Raises:
        AssertionError: If the settings carry no recorded search.

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

    The same two spellings as ``_stale_file_as_named``.

    Raises:
        AssertionError: If the settings carry no recorded search.

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

    Public so that a terminal surface can report the leftover in these same
    sentences when a shadowed file outranks it on the Configuration row.  The
    next step says move before delete, because after an upgrade the leftover
    can hold the only copy of the Paperless URL and token.

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

    The outcome comes from ``config_file_state``, the one derivation every
    surface reads, and the terminal surfaces call this with
    ``absolute_paths=True`` rather than re-authoring the sentences.

    A missing file is amber, because configuring through environment variables
    alone is supported, and the Paperless row already goes red on an unset URL
    or token.  A lone superseded-name file is red, because nothing the operator
    wrote was read.  A shadowed file is amber, because a per-user file
    overriding the system one can be deliberate.  The search runs once, at
    load, so every fix ends in a restart.

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
                message=f"No config file loaded: saneless now reads {CONFIG_FILENAME}, not {LEGACY_CONFIG_FILENAME}.",
                next_step=(
                    f"Rename {named} to {CONFIG_FILENAME}, then restart saneless."
                ),
            )
        case _:
            assert_never(state)


def _scanner_host_unanswered(probes: tuple[saned_probe.HostProbe, ...]) -> CheckResult:
    """
    Build the amber row for a timed-out host the check must not enumerate.

    Amber, because ``SANE_NET_HOSTS`` adds network devices to local
    enumeration rather than replacing it, so a dead host does not mean no
    scanner; an appliance whose only scanner is that host therefore passes
    ``saneless doctor``.  The row reports the worst outcome and a count, never
    a host (ASVS 4.0.3 V7.4).
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

    The state is ``OK`` so a scripted health gate does not go red for every
    scan; the ``skipped`` flag is what the surfaces render.
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

    Not ``_scanner_skipped``: a running scan is excluded before the gate is
    tried, so the holder is something else, such as ``StartupProfiles`` at
    worker start.  ``OK`` for the same reason, with no next step because the
    next probe clears it.
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

    python-sane imported and ``sane.init()`` refused, so reinstalling changes
    nothing and the next step sends the reader to the log.

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
    Name the scanner a scan would use, or ``None`` when there is none.

    A set ``scanner.device`` is matched by exact id, and an unlisted one that
    opened is usable too, because SANE opens ids it never lists.  An empty
    setting takes the first device listed, as a scan does.  The name may be
    "" for a device that reported no vendor and no model.
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

    A host problem shows in amber, because scanning works, and ahead of the
    several-devices warning.  A stopped service is reworded here because
    ``_host_problem_clause``'s "is on, but" would put two "but"s in one
    sentence.

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
        # With no device configured, a scanner that appears on the LAN can
        # become the first one listed and take every scan.  Count-only,
        # because device ids are LAN addresses.
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

    Each listing runs in a fresh process, so a scanner switched back on is
    seen by the next Check; a wrong ``[scanner] device`` is a setting, read
    once, so only that half of the next step says to restart.
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

    Its host is one the pre-probe cannot dial, and opening the device would
    make libsane dial it with no timeout.  Amber, because nothing was found
    wrong; the next step never names the host (ASVS 4.0.3 V7.4).
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

    Amber, because a crashed listing proves the check could not see, not that
    scanning is impossible; the next listing starts a fresh child.  Nothing is
    interpolated (ASVS 4.0.3 V7.4).
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

    Some peer accepted a connection and said nothing; amber for the crash
    row's reason.  The next step avoids "switched on and connected", the
    phrase no row reporting a rejection may use.
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

    The child could not start or ended without a readable reply; amber for
    the crash row's reason.
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

    Each listing runs in a fresh process, so the next Check sees a scanner
    that has been switched on or plugged in, with no restart.

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

    A configured ``net:`` device is blamed on its own host's probe on
    ``saned_probe.SANED_PORT``, the port libsane opens it on; any other
    configured device is just not found, because an unrelated host does not
    explain its absence.
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

    Pure, so every row can be pinned by a unit test.  The row goes red
    exactly when scanning cannot work, and the more specific cause wins.
    Next steps say press Check again wherever a fresh listing clears the
    state, which each listing's own libsane does for names, hosts and
    scanners (measured against a real saned); they say restart only for a
    settings fix.  Device ids are compared, never rendered (ASVS 4.0.3 V7.4).
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

    It runs with the scanner gate free, because ``getaddrinfo`` takes no
    timeout and a gate held across it would park a scan job.  Every host
    libsane will dial is probed, with no short circuit, because one dead host
    holds the whole listing; hosts the probe cannot parse, such as IPv6
    literals, are still enumerated, and a timed-out host ends the check here.

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

    One ``list_and_open`` call in a fresh listing child: it lists, and opens
    a configured id only when it is not listed, because SANE opens ids it
    never lists and a scan opens the id first.  A crashed, timed-out or
    unanswered child is recorded as such, not as an empty listing; an abort
    propagates, because a stop is not a scanner fault.

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
        # A backend may raise anything; only the type name goes further.
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
    # Anything but a confirmed open counts as not opened.
    return _Enumeration(
        devices=devices, configured_opened=survey.configured_opened is True
    )


def _check_scanner(context: CheckContext) -> CheckResult:
    """
    Report whether a scanner is there to scan with, without the scanner gate.

    The same three steps as ``_scanner_result``, which differs only in where
    it takes the gate.  ``pre`` is tested with ``isinstance`` because both of
    its types are dataclasses and always truthy.
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

    This module authors the remedy; ``connection_status_message`` owns the
    diagnosis.  A redirect's next step never names where it pointed, because
    the row is LAN-visible.

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

    A placeholder token or an empty ``paperless.url`` is reported without a
    request, because neither can succeed; ``is_placeholder_token`` is the
    predicate every surface shares.  A redirect's sanitised target goes only
    in ``terminal_detail``.  A trust store that could not be read gets its own
    row, because the settings are not what is wrong.

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
                PROBE_READ_SECONDS,
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

    No profiles is red; a read-only config location, no config file and an
    unnamed generated profile are amber, in that order.  The storage outcome is
    the worker's record, because ``os.access`` says yes on a single-file bind
    mount where only the rename fails.

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
                message="Generated in memory — no configuration file is in use, so they are lost on restart.",
                next_step="Create a saneless config file so the profiles are saved.",
            )
        case ProfileStorage.PERSISTED:
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

    An unset fallback folder is amber, because the appliance works without
    one.  The configured path is never shown, because the page is
    LAN-visible.

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


# The Data folder row names a faulty setting by its key, never by its path,
# because the page is LAN-visible.
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

    A missing folder is created when first needed, so it is judged by its
    nearest existing ancestor.  A file or a dangling link at the path counts
    as existing, because creating a folder there would fail.

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

    The verdict is ``check_private_dir``'s, so the row refuses what start-up
    refuses, but its message names the path, so the problem is re-read here.

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

    Scans in progress are kept there, so an existing one must be private; a
    missing one is created 0700.
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

    Unlike the fallback folder these are not optional, so a fault in either is
    red.  The data folder is judged first, and the first faulty setting
    decides the row.
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

    A total ``match`` rather than a dict, so a new ``CheckKey`` member stops
    this type-checking until it has a check.

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

    The gate is tried without blocking, held only while the listing child
    runs and released after it is reaped; a held gate gives the busy row.
    Holding it across name resolution could park a scan behind a broken
    resolver, and waiting on it would queue the probe behind a long scan.
    See docs/explanation/decisions/0003-scanner-gate-is-a-lock.md.

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
    whole of what either of them may show.

    ``skip_scanner`` is honoured first, before the gate, and never enters the
    backend: SANE does not allow a second call into the library while a scan's
    read is outstanding.  The gate is passed in, not held by the caller around
    the whole run, so a scan start never waits on the Paperless check's HTTP
    budget.

    A check that raises becomes a red row with a developer-constant message,
    because its exception text must not reach a LAN-visible page.  A set
    ``context.abort`` ends the run before the next check, or as soon as an
    in-flight probe or listing aborts; an in-flight Paperless request is not
    interruptible.

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
