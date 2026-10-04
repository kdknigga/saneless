"""
Read the saned hosts SANE will dial, and probe each one before SANE does.

The Scanner check dials every saned host libsane's net backend is about to
dial, with short budgets, before it lets libsane near them.  libsane's own
connect takes no timeout, so a switched-off host costs a listing its whole
deadline; the pre-probe finds that host in seconds, with the scanner gate
free, and tells a dead host, a refused port, a name that does not resolve and
a saned that turns this machine away apart.  This module holds both halves:
the parser that turns ``scanner.host`` (or an exported ``SANE_NET_HOSTS``)
into the ``(host, port)`` pairs to dial, and the TCP probe that speaks the
opening of the SANE network protocol to each of them.  ``checks.py`` turns
what they find into rows.

This is a leaf module.  At runtime it imports nothing from ``saneless`` but
``saneless.scanner.net_hosts``, so ``checks.py`` can import it and ``doctor``
still runs on a machine without python-sane.
"""

from __future__ import annotations

import contextlib
import errno
import ipaddress
import logging
import socket
import struct
from dataclasses import dataclass
from enum import StrEnum
from string import ascii_letters, digits, hexdigits
from time import monotonic
from typing import TYPE_CHECKING, Final, assert_never

from saneless.scanner.net_hosts import effective_sane_net_hosts

if TYPE_CHECKING:
    import threading

    from saneless.config import Settings

__all__ = [
    "PROBE_CONNECT_SECONDS",
    "PROBE_HANDSHAKE_SECONDS",
    "PROBE_READ_SECONDS",
    "SANED_PORT",
    "HostProbe",
    "PreProbeAbortedError",
    "SanedOutcome",
    "blocks_enumeration",
    "configured_device_probes",
    "net_device_entry",
    "outcome_severity",
    "probe_saned",
    "saned_host_setting",
    "saned_hosts",
    "worst_outcome",
]

logger = logging.getLogger(__name__)


# How long a saned pre-probe waits before giving up on a configured host.  It
# is a deadline, not a per-socket timeout: every address the host resolves to
# is dialled, and all of them together get this much.  So it bounds one
# configured host, and nothing else.  Name resolution runs before it and is
# not inside it -- `getaddrinfo` takes no timeout, so an unreachable resolver
# costs whatever `resolv.conf` says.  Once a connect succeeds, the handshake
# has ``PROBE_HANDSHAKE_SECONDS`` of its own, so one host's worst case is
# resolution plus this budget plus that one, and every host the pre-probe can
# dial is probed -- up to ``_MAX_PROBE_HOSTS`` setting entries, and the
# configured ``net:`` device's own host.  The bound still
# matters, although a listing is no longer unbounded: libsane's own connect
# takes no timeout and costs roughly 127 s for a silently unreachable host,
# because Linux retries a SYN six times by default, inside a blocking C call
# nothing can interrupt.  That call now runs in a listing child stopped at
# ``LISTING_DEADLINE_SECONDS`` (see below), and this budget is what keeps a
# host it can see is dead from costing a check even that.  Read at call time,
# so tests can shorten it.
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

# The longest a pre-probe that may have to stop waits on saned's reply before
# it looks at the caller's abort Event again.  So a stopping server ends a
# pre-probe stuck on a host that accepted the connection and says nothing
# within about this long, rather than waiting out ``PROBE_HANDSHAKE_SECONDS``.
# The connect is not sliced: it is bounded by ``PROBE_CONNECT_SECONDS`` over
# the host's addresses, and the abort is looked at before each address.
_ABORT_POLL_SECONDS: Final = 0.1

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

# The ways a connect can fail on *this* machine before anything reaches the
# scanner host: no route to that network, no local address to send from, or
# no socket family for it at all.  They are what a dual-stack name's IPv6
# address does inside a container with no IPv6, because the resolver is asked
# without ``AI_ADDRCONFIG``.  They prove nothing about the host either way, and
# libsane fails such an address just as fast, so the pre-probe sets them
# aside rather than counting them as a host that did not answer.
_ADDRESS_CANNOT_BE_TRIED: Final = frozenset(
    {errno.ENETUNREACH, errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT}
)

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
# ``checks._scanner_preflight`` probes every entry, paying an unbounded
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
# of being wrong about that is stated rather than hidden: the fifth distinct
# host onwards loses the pre-probe, not the check, and libsane still dials it
# inside the listing, so a dead one there holds the listing, and the scanner
# gate, until ``LISTING_DEADLINE_SECONDS`` stops it.  The configured ``net:``
# device's own host is probed on top of the cap, because the check may open
# it.
_MAX_PROBE_HOSTS: Final = 4

# The listing has a bound of its own: ``LISTING_DEADLINE_SECONDS``, 30 s, in
# ``saneless.scanner.listing``.  Every listing, the Scanner check's included,
# runs in a short-lived child process that is killed and reaped at that
# deadline, so the uninterruptible connect inside libsane costs a check at
# most 30 s rather than the ~127 s a silent host costs the C call itself.  It
# is defined in the scanner layer, beside the launcher that enforces it,
# because the scan path lists through the same launcher and must not import
# ``checks`` to do so.  The pre-probe still matters: a silent host it
# catches never holds the scanner gate for the whole deadline, and the row
# names the cause rather than saying only that the listing ran out of time.

# How libsane's net backend starts every device id it names
# (``sane_get_devices`` in ``backend/net.c``).  An id that starts this way is
# opened by dialling a saned host, which is why the Scanner check probes that
# host before it will open one.
_NET_DEVICE_PREFIX: Final = "net:"


def saned_host_setting(settings: Settings) -> str:
    """
    Return the host list SANE will actually use, not merely the configured one.

    The probe must dial the hosts SANE's net backend will read, or it
    describes a machine nothing is dialling: a configured host that is
    switched off would produce a row while a host exported in
    ``SANE_NET_HOSTS`` quietly served devices.  The rule for which list that
    is lives in one helper, which the scanner backend calls before it hands
    the list to SANE and this module calls here, so the two cannot drift.  In
    particular both count an exported but empty variable as unset: it names
    no host, so the configured host is what SANE is given.

    This places no new trust in the variable.  It is already the value libsane
    reads; the alternative is describing a host nothing is dialling.

    Args:
        settings: The injected configuration.

    Returns:
        The colon-separated host list to parse, possibly empty.

    """
    return effective_sane_net_hosts(settings.scanner.host)


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
    enumeration start -- a listing that then hangs on the dead host until
    ``LISTING_DEADLINE_SECONDS`` stops it, holding the scanner gate, which is
    what the pre-probe exists to avoid, on an appliance whose configured host
    is in fact dead.

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

    Being narrow is the safe direction.  A false "no" costs the pre-probe for
    that entry: ``saned_hosts`` drops it and the scanner check falls through
    to the listing, which may still dial it until ``LISTING_DEADLINE_SECONDS``
    stops the child.  A false
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
    pre-probe's job on a static-IP scanner, which would otherwise cost a
    listing its whole ``LISTING_DEADLINE_SECONDS`` when the appliance is off.
    The one
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
    false "yes" leaves the setting unprobed, so a dead host in it can still
    hold a listing until ``LISTING_DEADLINE_SECONDS``, but there is no path
    from this function to an address that gets dialled.

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


def saned_hosts(host_setting: str) -> tuple[tuple[str, int], ...]:
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
    safer direction, though not a free one: the refused entries are not
    probed, and libsane may still dial them with no timeout, but the refusal
    never produces a wrong verdict from a probe of something nobody
    configured.

    A fully-qualified name written with its root dot yields ``()`` as well:
    ``scanner.local.`` is legal DNS, and ``_looks_like_a_host_name``'s
    trailing-dot rule rejects it.  This is accepted rather than fixed, because
    widening the accept surface for a spelling that appears nowhere in this
    project's config examples buys little.  The cost of leaving it is the one
    stated under "Returning ``()``" below: the name is not probed, and libsane
    may still dial it with no timeout.

    **The cap.**  At most ``_MAX_PROBE_HOSTS`` -- four -- distinct entries are
    returned, taken from the front of the configured order after repeats are
    dropped.  ``checks._scanner_preflight`` probes every one of them, paying an
    unbounded ``getaddrinfo`` plus ``PROBE_CONNECT_SECONDS`` plus
    ``PROBE_HANDSHAKE_SECONDS`` for each, inside the
    ``POST /api/checks/refresh`` request thread; the manual-refresh floor
    bounds how often that request may be made and not how long one of them
    takes, so the length of this tuple is the only place the duration can be
    bounded.  A longer setting loses the pre-probe for its tail rather than
    losing the bound.  That is a real loss, not only a latency one: libsane
    still dials every entry of the tail inside the listing, so a dead fifth
    host holds the listing child, and the scanner gate, until
    ``LISTING_DEADLINE_SECONDS`` stops it, and the row can only say the
    listing ran out of time.  It is accepted, and documented, because refusing to
    enumerate beside an unprobed entry would turn every working five-host
    setup permanently amber.

    Returning ``()`` is not a silent failure; it is the fallback this module
    documents everywhere else.  No entries means no probe, which means the
    scanner check lists and behaves exactly as it did before the probe
    existed.  The same is true of a segment dropped from a list: it is not
    probed, and libsane may still dial it.  An operator who typed an IPv6
    literal, or a name this module will not guess at, therefore loses the
    pre-probe's protection for that host -- a dead one holds the listing until
    ``LISTING_DEADLINE_SECONDS`` -- but never gets a wrong verdict from a probe of
    something nobody configured, which is the trade the whole module is built
    on.  ``checks._scanner_preflight`` lists every kind of entry that goes unprobed.

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
        # the Scanner row into ``checks.run_checks``' generic failure row; the second
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
    # Filter first, then drop repeats, then cap: the cap is on distinct entries
    # that would really be dialled, not on segments that were dropped before
    # anything reached a socket, and a host named twice must not push a
    # different host past the cap unprobed.
    names = dict.fromkeys(host for host in present if _looks_like_a_host_name(host))
    return tuple((host, SANED_PORT) for host in names)[:_MAX_PROBE_HOSTS]


class SanedOutcome(StrEnum):
    """
    What the saned pre-probe learnt about one configured host.

    Each outcome is a different thing an operator has to do, which is why
    the probe tells them apart at all:

    - ``UNRESOLVED``: the resolver raised, or answered with no address.
    - ``TIMED_OUT``: nothing connected and at least one address did not
      answer -- a connect timed out, the budget ran out before an address was
      dialled, or the network said the host was unreachable -- or no address
      could be dialled from this machine at all.  A peer that accepted the
      connection and then sent nothing before the handshake deadline is
      counted here too, because it is the peer libsane would wait on forever.
    - ``REFUSED``: at least one address refused the connection and every
      other one either refused too or could not be dialled from this machine
      (``_ADDRESS_CANNOT_BE_TRIED``).  The host is up and nothing is
      listening on saned's port.
    - ``REJECTED``: a connection was made and the handshake failed: the peer
      closed or reset it, the reply ended early, or it carried a failure
      status or a protocol version libsane does not speak.
    - ``HEALTHY``: a valid ``SANE_NET_INIT`` reply.

    Every consumer is a total ``match`` ending in
    ``assert_never``, so a new member stops type-checking until each of
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


class PreProbeAbortedError(Exception):
    """
    A saned pre-probe stopped part way because its caller is stopping.

    Not a finding about the host: what the probe would have learnt is
    unknown.  ``checks.run_checks`` ends the run on it, as it does on an aborted
    listing, and a stopping caller stores nothing.
    """


def _raise_if_aborted(abort: threading.Event | None) -> None:
    """
    Stop a pre-probe whose caller has asked it to.

    Args:
        abort: The caller's abort Event, or ``None`` for a caller that never
            stops part way.

    Raises:
        PreProbeAbortedError: ``abort`` is set.

    """
    if abort is not None and abort.is_set():
        logger.debug("saned pre-probe stopped because saneless is stopping")
        raise PreProbeAbortedError


def _recv_exactly(
    sock: socket.socket,
    count: int,
    deadline: float,
    abort: threading.Event | None = None,
) -> bytes | None:
    """
    Read exactly ``count`` bytes before ``deadline``, and never one more.

    Every ``recv`` asks only for what is still missing, and the socket's
    timeout is reset to what is left of the deadline before each one, so a
    peer that trickles bytes cannot stretch the read past the deadline and a
    peer that floods cannot make the probe take more than it asked for.

    With an ``abort`` Event, no ``recv`` waits longer than
    ``_ABORT_POLL_SECONDS``, and the Event is looked at each time one runs
    out, so a stop ends the read within about that long.

    Args:
        sock: The connected socket.
        count: How many bytes to read.
        deadline: The ``monotonic`` reading the read must finish by.
        abort: The caller's abort Event, or ``None``.

    Returns:
        The bytes, or ``None`` when the peer closed the connection first.

    Raises:
        TimeoutError: When the deadline passes before ``count`` bytes arrive.
        PreProbeAbortedError: ``abort`` was set while the read waited.

    """
    buffer = bytearray()
    while len(buffer) < count:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        sock.settimeout(
            remaining if abort is None else min(remaining, _ABORT_POLL_SECONDS)
        )
        try:
            chunk = sock.recv(count - len(buffer))
        except TimeoutError:
            if abort is None:
                raise
            # One slice ran out, not the deadline: the loop's own check says
            # whether the deadline has passed.
            _raise_if_aborted(abort)
            continue
        if not chunk:
            return None
        buffer += chunk
    return bytes(buffer)


def _handshake(
    sock: socket.socket, deadline: float, abort: threading.Event | None = None
) -> SanedOutcome:
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
        abort: The caller's abort Event, or ``None``; the wait for the reply
            ends within ``_ABORT_POLL_SECONDS`` of it being set.

    Returns:
        ``HEALTHY``, ``REJECTED`` or ``TIMED_OUT``.

    Raises:
        PreProbeAbortedError: ``abort`` was set while the reply was awaited.

    """
    remaining = deadline - monotonic()
    if remaining <= 0:
        return SanedOutcome.TIMED_OUT
    try:
        sock.settimeout(remaining)
        sock.sendall(_init_request())
        reply = _recv_exactly(sock, _INIT_REPLY_LENGTH, deadline, abort)
    except TimeoutError:
        logger.debug("saned pre-probe handshake: %s", SanedOutcome.TIMED_OUT.value)
        return SanedOutcome.TIMED_OUT
    except OSError as exc:
        logger.debug(
            "saned pre-probe handshake: %s (%s)",
            SanedOutcome.REJECTED.value,
            type(exc).__name__,
        )
        return SanedOutcome.REJECTED
    if reply is None:
        logger.debug(
            "saned pre-probe handshake: %s (closed)", SanedOutcome.REJECTED.value
        )
        return SanedOutcome.REJECTED
    status, version = struct.unpack(">ii", reply)
    major = (version >> 24) & 0xFF
    build = version & 0xFFFF
    if status != 0 or major != 1 or build not in {2, 3}:
        # The status and version stay out of the log line: they are the
        # peer's words, and only the outcome is this module's.
        logger.debug(
            "saned pre-probe handshake: %s (reply)", SanedOutcome.REJECTED.value
        )
        return SanedOutcome.REJECTED
    with contextlib.suppress(OSError):
        sock.sendall(struct.pack(">i", _SANE_NET_EXIT))
    return SanedOutcome.HEALTHY


def probe_saned(
    host: str,
    port: int,
    connect_timeout: float,
    handshake_timeout: float,
    abort: threading.Event | None = None,
) -> SanedOutcome:
    """
    Classify one configured saned host by the opening of the SANE handshake.

    libsane's listing takes no timeout parameter -- not in python-sane, not
    in ``sane_get_devices(3)``, and not settable from Python.  With the
    ``net`` backend that call opens a TCP connection to each entry of
    ``SANE_NET_HOSTS``, so an unplugged host is a connect that hangs until the
    kernel gives up: on Linux ``tcp_syn_retries`` defaults to 6, roughly 127
    seconds, inside a blocking C call nothing can interrupt.  The listing
    child is stopped at ``LISTING_DEADLINE_SECONDS``, which bounds that, but
    only by abandoning the listing: the check then knows nothing but that it
    ran out of time, and it held the scanner gate for the whole deadline.
    Dialling the same address first, with a short timeout, is what finds a
    dead host quickly, with the gate free, and lets the row name it.

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
    therefore moves the walk on.  So does an address this machine cannot dial
    at all (``_ADDRESS_CANNOT_BE_TRIED``), and it is set aside rather than
    counted as not answering: in a container with no IPv6, the IPv6 address
    fails locally at once, and a refusal from the IPv4 address is still the
    host's real answer.  The first address that *connects* decides:
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

    **Stopping.**  A caller that may have to stop part way passes ``abort``.
    It is looked at before resolution and before each address is dialled, and
    the wait for saned's reply looks at it every ``_ABORT_POLL_SECONDS``, so
    a stop costs at most what is left of the connect budget.  Resolution
    itself still cannot be interrupted.

    **Failure policy.**  This raises nothing but the abort.  Every ``OSError``
    becomes an outcome, and an empty resolver answer needs no guard of its own:
    the walk holds no subscript, so nothing to dial is a loop body that never
    runs and the answer is ``UNRESOLVED``.  Subscripting the resolver's first
    answer once raised ``IndexError``, which is not an ``OSError`` and so
    escaped into ``checks.run_checks``' generic red row.  Log lines carry the outcome
    and ``type(exc).__name__`` only -- no host, no address, no port and no
    exception text (ASVS 4.0.3 V7.1).

    Args:
        host: The host name or address to dial.
        port: The TCP port to dial.
        connect_timeout: The deadline for connecting, over every address.
        handshake_timeout: The deadline for saned's reply, once connected.
        abort: The caller's abort Event, or ``None`` for a caller that never
            stops part way.

    Returns:
        What the probe learnt about the host.

    Raises:
        PreProbeAbortedError: ``abort`` was set before the probe finished.

    """
    _raise_if_aborted(abort)
    try:
        candidates = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        # Logged at DEBUG, not WARNING: a name that does not resolve is the
        # ordinary state of an appliance whose scanner host is switched off or
        # misspelled, and the row the caller renders is where an operator is
        # told about it.
        logger.debug(
            "saned pre-probe: %s (%s)",
            SanedOutcome.UNRESOLVED.value,
            type(exc).__name__,
        )
        return SanedOutcome.UNRESOLVED
    deadline = monotonic() + connect_timeout
    unanswered = False
    refused = False
    for family, socket_type, protocol, _canonical_name, address in candidates:
        _raise_if_aborted(abort)
        remaining = deadline - monotonic()
        if remaining <= 0:
            # The budget is spent, so the addresses left over do not get one.
            unanswered = True
            break
        try:
            with socket.socket(family, socket_type, protocol) as probe:
                probe.settimeout(remaining)
                probe.connect(address)
                outcome = _handshake(probe, monotonic() + handshake_timeout, abort)
        except ConnectionRefusedError:
            # One address refusing says nothing about the next one: this is
            # exactly the dual-stack case where the IPv6 address the resolver
            # put first has no listener and the IPv4 address does.
            refused = True
            logger.debug("saned pre-probe: one address refused")
            continue
        except OSError as exc:
            # A timeout or an unreachable host proves nothing is up there, and
            # is what libsane would wait on.  A route or family this machine
            # lacks proves nothing about the host at all, so it is set aside
            # and cannot outweigh another address's definite refusal.
            if exc.errno not in _ADDRESS_CANNOT_BE_TRIED:
                unanswered = True
            logger.debug("saned pre-probe did not connect: %s", type(exc).__name__)
            continue
        logger.debug("saned pre-probe: %s", outcome.value)
        return outcome
    if unanswered:
        outcome = SanedOutcome.TIMED_OUT
    elif refused:
        outcome = SanedOutcome.REFUSED
    elif not candidates:
        outcome = SanedOutcome.UNRESOLVED
    else:
        # Every address failed on this machine before anything was sent: the
        # host cannot be reached from here, which is what TIMED_OUT's row
        # tells its reader.
        outcome = SanedOutcome.TIMED_OUT
    logger.debug("saned pre-probe: %s", outcome.value)
    return outcome


@dataclass(frozen=True, slots=True)
class HostProbe:
    """
    One configured saned host and what its pre-probe found.

    The host and port are kept only so enumeration can match a configured
    ``net:`` device to the host it lives on, on the port libsane opens it
    on.  Neither is ever rendered or logged: rows interpolate counts, not
    hosts (ASVS 4.0.3 V7.1, V7.4).

    Attributes:
        host: The configured entry that was dialled.
        outcome: What the probe learnt about it.
        port: The port it was dialled on.  A two-segment ``host:port``
            setting is probed on its own port, which is not the one libsane
            opens a ``net:`` device on, so an answer there says nothing
            about the device's host.

    """

    host: str
    outcome: SanedOutcome
    port: int = SANED_PORT


def outcome_severity(outcome: SanedOutcome) -> int:
    """
    Rank a probe outcome, so several hosts can be reported by the worst one.

    Most severe first: timed out, refused, unresolved, rejected, healthy.
    Timed out comes first because it is the only outcome that stops the
    scanner being checked at all.  Refused is next: the host is up but its
    scanner service is not, so nothing on that host can be listed.
    Unresolved follows, because the name itself may be wrong, whether in DNS
    or in a setting.  Rejected is last among the problems: the host is up,
    saned is up, and one line in its ``saned.conf`` fixes it.

    A ``match`` rather than a table, so a new outcome stops type-checking
    here until it is ranked.

    Args:
        outcome: What one probe found.

    Returns:
        A rank: higher is worse, and ``HEALTHY`` is 0.

    Raises:
        AssertionError: If the value is not a ``SanedOutcome`` member.

    """
    match outcome:
        case SanedOutcome.HEALTHY:
            severity = 0
        case SanedOutcome.REJECTED:
            severity = 1
        case SanedOutcome.UNRESOLVED:
            severity = 2
        case SanedOutcome.REFUSED:
            severity = 3
        case SanedOutcome.TIMED_OUT:
            severity = 4
        case _:
            assert_never(outcome)
    return severity


def blocks_enumeration(outcome: SanedOutcome) -> bool:
    """
    Say whether a host with this outcome must keep the check out of libsane.

    A timed-out host must, and that includes a peer that accepted the
    connection and then said nothing: libsane has no read timeout at any
    layer -- its net backend's connect timeout is cleared once the connect
    succeeds (``connect_dev`` in ``backend/net.c``) -- so a listing would wait
    on that host for as long as it stays silent, inside a C call nothing can
    interrupt.  The listing child would be stopped at
    ``LISTING_DEADLINE_SECONDS``, but only after holding the scanner gate that
    long, and its row could say no more than that the listing ran out of
    time; keeping out gives a fast row that names the host problem.

    Refused, rejected and unresolved hosts return at once inside libsane: a
    refused connect fails straight away, a rejection is an answer, and a name
    that does not resolve is never dialled.  Enumeration is how the check
    learns whether a usable scanner is visible anyway, so none of them blocks
    it, and the row can then say amber when a scanner is still usable and red
    when nothing is.

    Args:
        outcome: What one probe found.

    Returns:
        True for ``TIMED_OUT`` only.

    Raises:
        AssertionError: If the value is not a ``SanedOutcome`` member.

    """
    match outcome:
        case SanedOutcome.TIMED_OUT:
            blocks = True
        case (
            SanedOutcome.UNRESOLVED
            | SanedOutcome.REJECTED
            | SanedOutcome.REFUSED
            | SanedOutcome.HEALTHY
        ):
            blocks = False
        case _:
            assert_never(outcome)
    return blocks


def worst_outcome(probes: tuple[HostProbe, ...]) -> SanedOutcome:
    """
    Pick the most severe outcome among the probed hosts.

    Args:
        probes: What each configured host's probe found.

    Returns:
        The outcome ``outcome_severity`` ranks highest, or ``HEALTHY`` when
        nothing was probed.

    """
    return max(
        (probe.outcome for probe in probes),
        key=outcome_severity,
        default=SanedOutcome.HEALTHY,
    )


def net_device_entry(device_id: str) -> str | None:
    """
    Name the host entry a ``net:`` device was listed from.

    libsane's net backend names each remote device ``net:``, then the
    ``SANE_NET_HOSTS`` entry it dialled, then ``:`` and the name saned gave it
    (``sane_get_devices`` in ``backend/net.c``).  The entry is therefore the
    host the device lives on, spelt exactly as it was configured, and an IPv6
    literal keeps its brackets.

    The result is only ever compared with a probed host.  It is never dialled,
    rendered or logged, because it is a LAN address (ASVS 4.0.3 V7.1, V7.4).

    Args:
        device_id: A SANE device id, as configured or as listed.

    Returns:
        The host entry, or ``None`` when the id is not a ``net:`` id or names
        no entry.

    """
    if not device_id.startswith(_NET_DEVICE_PREFIX):
        return None
    remainder = device_id.removeprefix(_NET_DEVICE_PREFIX)
    if remainder.startswith("["):
        closing = remainder.find("]")
        entry = remainder[: closing + 1] if closing >= 0 else ""
    else:
        entry = remainder.partition(":")[0]
    return entry or None


def configured_device_probes(
    device_id: str,
    probes: tuple[HostProbe, ...],
    abort: threading.Event | None = None,
) -> tuple[tuple[HostProbe, ...], bool]:
    """
    Probe a configured ``net:`` device's own host, when nothing else did.

    libsane's net backend dials a ``net:`` device's host when the device is
    opened, whether or not that host is in ``SANE_NET_HOSTS``, and it dials
    with no timeout.  The Scanner check opens a configured device it does not
    find listed, so without this a switched-off host outside the setting --
    none configured, one past ``_MAX_PROBE_HOSTS``, or one named only in
    ``net.conf`` -- would hold the listing child until
    ``LISTING_DEADLINE_SECONDS`` stops it, and on the status strip that would
    be paid holding the scanner gate.  Probing that host here, with the gate free and
    on the same budgets as every other host, means a timed-out one ends the
    check before libsane is touched, exactly like a configured host.

    A host the setting already probed on ``SANED_PORT`` is not probed twice.
    The port matters: libsane opens a ``net:`` device on saned's registered
    port whatever ``scanner.host`` says, so a ``host:port`` setting's answer
    on another port covers nothing, and the host is probed here on
    ``SANED_PORT`` as well.  A ``net:`` id
    whose host this module cannot dial -- an IPv6 literal, a numeric
    shorthand ``saned_hosts`` drops, or an id that names no host -- is not
    probed, and the caller must not open it: nothing has shown the host is
    up, and libsane would find out with no timeout.  A device that is not a
    ``net:`` device is left alone; opening it dials no saned host.

    Args:
        device_id: The configured ``scanner.device``, possibly empty.
        probes: What the setting's own hosts' probes found.
        abort: The caller's abort Event, handed to the probe, or ``None``.

    Returns:
        The probes, with the device's host appended when it was probed here,
        and whether enumeration may open the device if it is not listed.

    Raises:
        PreProbeAbortedError: ``abort`` was set during the probe.

    """
    if not device_id.startswith(_NET_DEVICE_PREFIX):
        return probes, True
    entry = net_device_entry(device_id)
    if entry is not None and any(
        probe.host == entry and probe.port == SANED_PORT for probe in probes
    ):
        return probes, True
    dialable = saned_hosts(entry or "")
    if not dialable:
        return probes, False
    # The entry holds no ``:``, so ``saned_hosts`` cannot have read a port
    # out of it; the port is named here so the dial plainly is libsane's.
    host = dialable[0][0]
    outcome = probe_saned(
        host, SANED_PORT, PROBE_CONNECT_SECONDS, PROBE_HANDSHAKE_SECONDS, abort
    )
    return (*probes, HostProbe(host, outcome, SANED_PORT)), True
