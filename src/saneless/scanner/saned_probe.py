"""
Read the saned hosts SANE will dial, and probe each one before SANE does.

libsane's own connect takes no timeout, so a switched-off host costs a listing
its whole deadline.  The Scanner check dials each host first, with short
budgets and the scanner gate free, and tells a dead host, a refused port, a
name that does not resolve and a saned that turns this machine away apart.
This module parses the host setting into ``(host, port)`` pairs and probes
each with the opening of the SANE network protocol; ``checks.py`` turns what
they find into rows.

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


# The connect deadline for one configured host, shared by every address it
# resolves to.  Name resolution runs before it and is not inside it:
# ``getaddrinfo`` takes no timeout.  Read at call time, so tests can shorten it.
PROBE_CONNECT_SECONDS: Final = 2.0

# How long saned may take to answer once connected.  It is separate from the
# connect budget because saned does name lookups before it replies, so on slow
# DNS a working saned can take seconds to say hello.  Read at call time.
PROBE_HANDSHAKE_SECONDS: Final = 5.0

# The longest a pre-probe that may have to stop waits on saned's reply before
# it looks at the caller's abort Event again.  The connect is not sliced; the
# abort is looked at before each address.
_ABORT_POLL_SECONDS: Final = 0.1

# How long the Paperless probe waits for a response body once connected.  The
# client's own default is a flat 30 s, which is the right budget for an upload
# and the wrong one for a health row.  Read at call time.
PROBE_READ_SECONDS: Final = 5.0

# saned's registered port, IANA's ``sane-port``.  Read at call time.
SANED_PORT: Final = 6566

# The SANE network protocol, as far as the pre-probe speaks it: every word is
# four big-endian bytes, and saned answers ``SANE_NET_EXIT`` by ending the
# session without a reply.  The version sent is what libsane 1.0.32 sends;
# saned does not read it.
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

# The ways a connect fails on this machine before anything reaches the host,
# as a dual-stack name's IPv6 address does in a container with no IPv6.  They
# prove nothing about the host, so the pre-probe sets them aside rather than
# counting them as a host that did not answer.
_ADDRESS_CANNOT_BE_TRIED: Final = frozenset(
    {errno.ENETUNREACH, errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT}
)

# Every character a segment of ``scanner.host`` may contain and still be read
# as a host name: not a validator, only the smallest set that tells a name
# apart from the halves of an IPv6 literal split on ``:``.
_HOST_NAME_CHARACTERS: Final = frozenset(ascii_letters + digits + "-.")

# The only digits this module will read as a number.  ``str.isdigit()`` also
# accepts characters ``int()`` refuses (U+00B2) and non-ASCII decimals libsane
# would read as a name, so ASCII decimal is the only reading both ends share.
_ASCII_DIGITS: Final = frozenset(digits)

# The digits an ASCII ``0x``/``0X`` literal may be made of.  glibc reads such a
# segment as a number, so a name may not look like one.
_ASCII_HEX_DIGITS: Final = frozenset(hexdigits)

# How many distinct hosts one setting may put in front of the pre-probe.  The
# probes run inside the ``POST /api/checks/refresh`` request thread, so this is
# the only bound on how long one refresh takes; a fifth host loses the
# pre-probe, not the check.  The configured ``net:`` device's own host is
# probed on top of the cap.
_MAX_PROBE_HOSTS: Final = 4

# How libsane's net backend starts every device id it names.  Opening such an
# id dials a saned host, so the Scanner check probes that host first.
_NET_DEVICE_PREFIX: Final = "net:"


def saned_host_setting(settings: Settings) -> str:
    """
    Return the host list SANE will actually use, not merely the configured one.

    The rule is ``net_hosts.effective_sane_net_hosts``, the one the scanner
    backend uses, so the probe dials exactly the hosts SANE will.

    Args:
        settings: The injected configuration.

    Returns:
        The colon-separated host list to parse, possibly empty.

    """
    return effective_sane_net_hosts(settings.scanner.host)


def _part_is_a_number_to_glibc(part: str) -> bool:
    """
    Say whether one dot-separated part of a segment is a number to glibc.

    glibc reads each part as an ASCII decimal run or an ``0x`` hex literal;
    an octal part such as ``0755`` is a decimal run here and is caught by the
    caller's dotted-quad test.  Anything else is a name.
    """
    if not part:
        return False
    # A non-ASCII digit is never a number here, whatever the digit sets hold.
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

    Measured here, ``0.0`` and ``0x0.0`` resolve to ``0.0.0.0`` and
    ``127.1`` to ``127.0.0.1``.  On Linux a ``connect()`` to ``0.0.0.0``
    reaches loopback, so any local listener on 6566 could pass a dead host.
    A segment whose parts are all numbers is refused unless it is a legal
    dotted quad other than ``0.0.0.0``.
    """
    if not all(_part_is_a_number_to_glibc(part) for part in segment.split(".")):
        return False
    try:
        address = ipaddress.IPv4Address(segment)
    except ValueError:
        # Numeric, but not a legal literal: one of the shorthands glibc
        # invents an address from.
        return True
    # The unspecified address is a legal literal but dials loopback, and no
    # scanner is ever at it.
    return address.is_unspecified


def _looks_like_a_host_name(segment: str) -> bool:
    """
    Say whether one colon-separated segment could be a host name at all.

    Deliberately not a hostname RFC check: it only tells a typed name apart
    from the fragments of an IPv6 literal split on ``:`` and from numbers
    glibc would resolve as an address (``'2001'`` answers ``0.0.7.209``).
    Narrow is the safe direction: a false "no" only costs that entry its
    pre-probe, while a false "yes" can give a wrong verdict.  A legal dotted
    quad other than ``0.0.0.0`` is accepted.
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

    Asked before anything is split on ``:``, which destroys a literal.
    Brackets are removed anywhere, so ``[fe80::1]:6566`` parses as
    ``fe80::1:6566``; that mangling is safe because the answer is only used to
    refuse.  A zone suffix such as ``fe80::1%eth0`` parses as version 6, and
    an IPv4 literal answers False because dots are not ambiguous.
    """
    try:
        parsed = ipaddress.ip_address(
            host_setting.strip().replace("[", "").replace("]", "")
        )
    except ValueError:
        # A name, a list of names, or a literal too mangled to parse: the
        # caller's segment rules read all three.
        return False
    return parsed.version == 6


def saned_hosts(host_setting: str) -> tuple[tuple[str, int], ...]:
    """
    Parse ``scanner.host`` into the ``(host, port)`` pairs saned would be dialled on.

    libsane splits the setting on every ``:`` and always dials saned's
    registered port, so the two-segment ``host:port`` reading is this module's
    own: it only changes the port this probe dials, as a test does to aim at a
    loopback port.  A trailing segment is a port only when there are exactly
    two segments and it is ASCII decimal within 1..65535, read as decimal.  An
    out-of-range port is dropped, not read as a host, because glibc would
    dial a number as an address (``'99999'`` answers ``0.1.134.159``).

    A setting that parses as one IPv6 address yields no entries.  With more
    than one colon the setting is refused unless every segment could be a host
    name and no blank segment sits between two others, so ``localhost:6566:``
    yields ``()``.  A name written with its root dot, ``scanner.local.``, is
    dropped as well.

    At most ``_MAX_PROBE_HOSTS`` distinct entries are returned, from the front
    of the configured order.  A refused or dropped entry is not probed, and
    libsane may still dial it with no timeout, but it never yields a wrong
    verdict from a probe of something nobody configured.

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
        # host through the one branch that skips the filter below.  The port
        # emptiness test is needed: ``set("") <= _ASCII_DIGITS`` is True, and
        # ``int("")`` raises.
        if (
            _looks_like_a_host_name(host)
            and maybe_port
            and set(maybe_port) <= _ASCII_DIGITS
            and 0 < int(maybe_port) <= 65535
        ):
            return ((host, int(maybe_port)),)
    # Filter, then drop repeats, then cap, so a dropped segment or a host named
    # twice cannot push a different host past the cap unprobed.
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

    Each ``recv`` asks only for what is missing, with the timeout reset to
    what is left of the deadline, so a trickling peer cannot stretch the read
    and a flooding one cannot make the probe take more.  With ``abort``, no
    ``recv`` waits longer than ``_ABORT_POLL_SECONDS``.

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

    The reply is accepted on the terms libsane's net backend accepts it:
    status 0, major version 1, build 2 or 3.  After a valid reply the probe
    sends ``SANE_NET_EXIT``, so saned logs one ordinary session, not an error.
    ``TimeoutError`` is caught before ``OSError``, which it subclasses: a
    silent peer is not the same finding as one that hung up.

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
        # The status and version are the peer's words, so they stay out of
        # the log line.
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

    A connect is not enough: saned turns away a peer its ``saned.conf`` does
    not allow by closing the socket before it replies, so only the
    ``SANE_NET_INIT`` reply tells that refusal apart from a working saned.

    The connect budget is one deadline over every address the name resolved
    to, in resolver order.  Every address is tried because glibc returns the
    IPv6 address first even without an IPv6 route, and saned often binds
    IPv4 only; the first address that connects decides, as in libsane.
    Resolution is outside both budgets, because a thread abandoned inside
    ``getaddrinfo`` is worse than a slow answer.

    ``abort`` is looked at before resolution and before each address, and
    every ``_ABORT_POLL_SECONDS`` while saned's reply is awaited.  Every
    ``OSError`` becomes an outcome, so this raises nothing but the abort, and
    log lines carry the outcome and exception type only, never a host,
    address, port or exception text.

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
        # DEBUG, not WARNING: the row the caller renders is where an operator
        # is told about a name that does not resolve.
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
            # One address refusing says nothing about the next: the IPv6
            # address may have no listener where the IPv4 one does.
            refused = True
            logger.debug("saned pre-probe: one address refused")
            continue
        except OSError as exc:
            # A timeout or an unreachable host is what libsane would wait on.
            # A route or family this machine lacks proves nothing about the
            # host, so it cannot outweigh another address's refusal.
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
        # Every address failed on this machine before anything was sent, so
        # the host cannot be reached from here.
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

    Timed out ranks worst because it alone stops the scanner being checked;
    rejected is the mildest problem, fixed by one line in ``saned.conf``.  A
    ``match`` rather than a table, so a new outcome stops type-checking here
    until it is ranked.

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

    A timed-out host must, including a peer that connected and then said
    nothing: libsane has no read timeout, so a listing would hold the scanner
    gate until the listing deadline.  Refused, rejected and unresolved hosts
    fail at once inside libsane, so enumeration still runs to learn whether a
    usable scanner is visible anyway.

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

    libsane dials a ``net:`` device's host with no timeout when the device is
    opened, whether or not the host is in ``SANE_NET_HOSTS``, and the Scanner
    check opens a configured device it does not find listed.  Probing that
    host here, unless the setting already probed it on ``SANED_PORT``, lets a
    timed-out one end the check before libsane is touched.

    A ``net:`` id whose host this module cannot dial, such as an IPv6 literal,
    is not probed, and the caller must not open it.  A device that is not a
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
    # The entry holds no ``:``, so ``saned_hosts`` read no port out of it.
    host = dialable[0][0]
    outcome = probe_saned(
        host, SANED_PORT, PROBE_CONNECT_SECONDS, PROBE_HANDSHAKE_SECONDS, abort
    )
    return (*probes, HostProbe(host, outcome, SANED_PORT)), True
