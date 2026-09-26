"""
A loopback stand-in for saned, speaking just enough of the SANE network protocol.

The scanner check's pre-probe performs the opening of the SANE network
handshake (``SANE_NET_INIT``) and classifies the host by what comes back.  The
cases that matter are ones a bare listening socket cannot produce: a saned that
answers, one that accepts the connection and closes it without reading (which
is what saned's access list does to a peer it refuses), one that answers with a
non-success status, one that answers with a protocol version libsane would
reject, and one that answers correctly and then keeps sending.  This module
serves exactly one of those behaviours on an ephemeral 127.0.0.1 port and
records every byte it was sent, so a test can assert both what the probe
concluded and what it put on the wire.

Two more behaviours go one procedure further and answer
``SANE_NET_GET_DEVICES`` with one device.  One then drops the connection, the
way a saned that restarts leaves a client's persistent control connection
dead; the other keeps it.  Real libsane is their client, not the probe, and
libsane dials only port 6566, so they are served on that fixed port.

The wire format is the one ``sanei_codec_bin.c`` and ``sanei_net.c`` in
sane-backends define: a word is four big-endian bytes, a string is a word
holding its length (trailing NUL included) followed by that many bytes, the
``SANE_NET_INIT`` request is a procedure word, a version word and a user-name
string, and the reply is a status word and a version word -- eight bytes, with
nothing after them.

Nothing here sleeps and nothing waits on a fixed pause.  Connections are
handled one at a time on a single thread; teardown wakes the blocked
``accept()`` by connecting to it, then joins the thread, so every socket is
closed before the context manager returns.  Every handler catches ``OSError``
and closes its connection in a ``finally``, because an unclosed socket or an
exception escaping a thread is a test error under ``filterwarnings = ["error"]``.

Import it as ``from tests.fake_saned import ...``; the bare ``fake_saned`` form
raises ``ModuleNotFoundError`` under pytest 9's importlib mode.
"""

from __future__ import annotations

import contextlib
import socket
import struct
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = [
    "EXIT_REQUEST",
    "GET_DEVICES_REPLY",
    "INIT_REQUEST",
    "FakeSaned",
    "SanedBehaviour",
    "fake_saned",
]

# The protocol version saned 1.0.32 reports: SANE_VERSION_CODE(1, 0, 3).
_VERSION_CODE: Final = 0x01000003

# What saneless sends, byte for byte: procedure 0 (SANE_NET_INIT), the version
# word, and the user name "saneless" as a NUL-terminated string.
INIT_REQUEST: Final = struct.pack(">iii", 0, _VERSION_CODE, 9) + b"saneless\0"

# The two procedures after SANE_NET_INIT that the listing behaviours read:
# SANE_NET_GET_DEVICES (1) and SANE_NET_EXIT (10).  Neither takes arguments,
# so each request is the bare procedure word.
_GET_DEVICES: Final = 1
_EXIT: Final = 10

# SANE_NET_EXIT: procedure 10, no arguments, and saned sends no reply.
EXIT_REQUEST: Final = struct.pack(">i", _EXIT)


def _string(text: str) -> bytes:
    """
    Encode ``text`` as a SANE wire string.

    Args:
        text: The string to encode.

    Returns:
        A word holding the length with the trailing NUL, then the bytes and
        the NUL.

    """
    encoded = text.encode() + b"\0"
    return struct.pack(">i", len(encoded)) + encoded


# The SANE_NET_GET_DEVICES reply listing one device (sanei_w_get_devices_reply
# in sanei_net.c): a status word, then an array of device pointers.  The array
# is a length word counting the devices plus the NULL that terminates them;
# each element is a pointer word, 0 for "not NULL" followed by the
# SANE_Device's name, vendor, model and type strings, and 1 for the NULL.
GET_DEVICES_REPLY: Final = (
    struct.pack(">i", 0)  # status SANE_STATUS_GOOD
    + struct.pack(">i", 2)  # array length: one device and the NULL terminator
    + struct.pack(">i", 0)  # element 0: not NULL
    + _string("fake0")
    + _string("Fake")
    + _string("Loopback")
    + _string("flatbed scanner")
    + struct.pack(">i", 1)  # element 1: NULL
)

# The INIT request header: procedure word, version word, string-length word.
_HEADER_LENGTH: Final = 12

# The longest user name this fake will read.  A real client sends a login
# name; anything longer is a malformed request and the handler gives up on it.
_MAX_USER_LENGTH: Final = 4096

# How long one accepted connection may sit idle before the handler abandons
# it.  A timeout, not a pause: a correct probe always closes its end long
# before this, and the bound only stops a broken one hanging the suite.
_CONNECTION_IDLE_SECONDS: Final = 10.0

# How much junk the flooding behaviour sends after its valid reply.
_FLOOD_BYTES: Final = 1024 * 1024


class SanedBehaviour(StrEnum):
    """
    What the fake does with each connection it accepts.

    ``HEALTHY`` reads the INIT request, replies success with version 1.0.3 and
    then reads until the client closes.  ``CLOSE`` closes the connection
    without reading anything, the way saned refuses a peer its access list does
    not allow.  ``BAD_STATUS`` reads the request and replies status 11
    (``SANE_STATUS_ACCESS_DENIED``).  ``BAD_VERSION`` reads the request and
    replies success with a major version libsane does not speak.  ``FLOOD``
    reads the request and sends a valid reply followed by a mebibyte of junk.
    ``LIST_THEN_DROP`` reads the INIT request, replies success, reads one
    procedure word and, when it is ``SANE_NET_GET_DEVICES``, replies with one
    device and closes the connection -- the way a saned that restarts leaves
    the client's persistent control connection dead.  ``LIST_AND_KEEP`` does
    the same but keeps the connection, answering every ``SANE_NET_GET_DEVICES``
    until the client sends ``SANE_NET_EXIT`` or closes.
    """

    HEALTHY = "HEALTHY"
    CLOSE = "CLOSE"
    BAD_STATUS = "BAD_STATUS"
    BAD_VERSION = "BAD_VERSION"
    FLOOD = "FLOOD"
    LIST_THEN_DROP = "LIST_THEN_DROP"
    LIST_AND_KEEP = "LIST_AND_KEEP"


# The behaviours that answer SANE_NET_GET_DEVICES after the INIT exchange.
_LISTING_BEHAVIOURS: Final = frozenset(
    {SanedBehaviour.LIST_THEN_DROP, SanedBehaviour.LIST_AND_KEEP}
)


@dataclass(frozen=True, slots=True)
class FakeSaned:
    """
    A running fake saned: where it listens and what it was sent.

    Attributes:
        port: The loopback port it listens on: ephemeral unless the caller
            fixed it.
        received: Every chunk of bytes read from any client, in arrival order.
            Complete once the context manager has exited, because teardown
            joins the serving thread.
        listings: One entry per ``SANE_NET_GET_DEVICES`` reply served, holding
            the number of the connection (counting from 1) it was served on.
            Complete once the context manager has exited, like ``received``.

    """

    port: int
    received: list[bytes] = field(default_factory=list)
    listings: list[int] = field(default_factory=list)


def _read_exactly(conn: socket.socket, count: int, received: list[bytes]) -> bool:
    """
    Read ``count`` bytes from ``conn``, recording each chunk as it arrives.

    Args:
        conn: The accepted connection.
        count: How many bytes to read.
        received: Where each chunk is appended.

    Returns:
        True when all ``count`` bytes arrived, False when the client closed
        first.

    """
    remaining = count
    while remaining > 0:
        chunk = conn.recv(remaining)
        if not chunk:
            return False
        received.append(chunk)
        remaining -= len(chunk)
    return True


def _read_init_request(conn: socket.socket, received: list[bytes]) -> bool:
    """
    Read one whole ``SANE_NET_INIT`` request.

    Args:
        conn: The accepted connection.
        received: Where each chunk is appended.

    Returns:
        True when a complete request was read.

    """
    header: list[bytes] = []
    if not _read_exactly(conn, _HEADER_LENGTH, header):
        received.extend(header)
        return False
    received.extend(header)
    _procedure, _version, user_length = struct.unpack(">iii", b"".join(header))
    if not 0 <= user_length <= _MAX_USER_LENGTH:
        return False
    return _read_exactly(conn, user_length, received)


def _drain(conn: socket.socket, received: list[bytes]) -> None:
    """
    Read and record everything the client sends until it closes.

    Args:
        conn: The accepted connection.
        received: Where each chunk is appended.

    """
    while chunk := conn.recv(4096):
        received.append(chunk)


def _reply_for(behaviour: SanedBehaviour) -> bytes:
    """
    Return the bytes the fake answers a complete INIT request with.

    Args:
        behaviour: The behaviour being served; never ``CLOSE``.

    Returns:
        The reply, including the flood's junk tail for ``FLOOD``.

    """
    match behaviour:
        case SanedBehaviour.BAD_STATUS:
            return struct.pack(">ii", 11, 0)
        case SanedBehaviour.BAD_VERSION:
            return struct.pack(">ii", 0, 0x7FFF0003)
        case SanedBehaviour.FLOOD:
            return struct.pack(">ii", 0, _VERSION_CODE) + b"\xff" * _FLOOD_BYTES
        case _:
            return struct.pack(">ii", 0, _VERSION_CODE)


@dataclass(frozen=True, slots=True)
class _Listing:
    """
    Where a listing behaviour records what it served.

    Attributes:
        connection: The number of the connection being served, from 1.
        listings: The fake's ``listings`` list.

    """

    connection: int
    listings: list[int]


def _serve_listings(
    conn: socket.socket,
    behaviour: SanedBehaviour,
    received: list[bytes],
    listing: _Listing,
) -> None:
    """
    Answer ``SANE_NET_GET_DEVICES`` requests after a successful INIT exchange.

    Returning is the drop: the caller's ``finally`` closes the connection.

    Args:
        conn: The accepted connection, already past its INIT reply.
        behaviour: ``LIST_THEN_DROP`` or ``LIST_AND_KEEP``.
        received: Where every byte read is recorded.
        listing: Which connection this is, and where each served listing is
            counted.

    """
    while True:
        word: list[bytes] = []
        if not _read_exactly(conn, 4, word):
            received.extend(word)
            return
        received.extend(word)
        (procedure,) = struct.unpack(">i", b"".join(word))
        if procedure != _GET_DEVICES:
            # SANE_NET_EXIT, or a procedure this fake does not speak.
            return
        conn.sendall(GET_DEVICES_REPLY)
        listing.listings.append(listing.connection)
        if behaviour is SanedBehaviour.LIST_THEN_DROP:
            return


def _handle(
    conn: socket.socket,
    behaviour: SanedBehaviour,
    received: list[bytes],
    listing: _Listing,
) -> None:
    """
    Serve one accepted connection, then close it whatever happened.

    Args:
        conn: The accepted connection, owned by this function.
        behaviour: What to do with it.
        received: Where every byte read is recorded.
        listing: Which connection this is, and where each listing served on
            it is counted.

    """
    try:
        if behaviour is SanedBehaviour.CLOSE:
            return
        conn.settimeout(_CONNECTION_IDLE_SECONDS)
        if not _read_init_request(conn, received):
            return
        conn.sendall(_reply_for(behaviour))
        if behaviour in _LISTING_BEHAVIOURS:
            _serve_listings(conn, behaviour, received, listing)
            return
        _drain(conn, received)
    except OSError:
        # The client closed or reset mid-exchange -- which the flooding
        # behaviour provokes on purpose -- or the idle bound ran out.  Either
        # way the connection is over, and nothing may escape the thread.
        return
    finally:
        conn.close()


def _serve(
    listener: socket.socket,
    behaviour: SanedBehaviour,
    received: list[bytes],
    listings: list[int],
    stopping: threading.Event,
) -> None:
    """
    Accept connections one at a time until teardown asks the loop to stop.

    Args:
        listener: The bound, listening socket.
        behaviour: What to do with each connection.
        received: Where every byte read is recorded.
        listings: Where every ``SANE_NET_GET_DEVICES`` reply served is counted.
        stopping: Set by teardown before it wakes ``accept()``.

    """
    connection = 0
    while True:
        try:
            conn, _peer = listener.accept()
        except OSError:
            return
        if stopping.is_set():
            conn.close()
            return
        connection += 1
        _handle(conn, behaviour, received, _Listing(connection, listings))


def _bind(listener: socket.socket, port: int) -> None:
    """
    Bind ``listener`` to 127.0.0.1, failing the test if a fixed port is taken.

    Args:
        listener: The unbound socket.
        port: The port to bind, or 0 for an ephemeral one.

    """
    if not port:
        listener.bind(("127.0.0.1", 0))
        return
    # A previous test's connections to the same fixed port may still be in
    # TIME_WAIT; without this the next bind would fail for no real reason.
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", port))
    except OSError:
        # A failure and never a skip: a skip would silently drop the
        # regression test on every machine that happens to run saned.
        pytest.fail(
            f"127.0.0.1:{port} is already in use, so the fake saned cannot "
            f"listen there. libsane's net backend dials only port {port}; stop "
            "whatever is listening on it (usually a local saned or "
            "saned.socket) and run the test again."
        )


@contextlib.contextmanager
def fake_saned(behaviour: SanedBehaviour, *, port: int = 0) -> Generator[FakeSaned]:
    """
    Serve one saned behaviour on a 127.0.0.1 port, ephemeral by default.

    Point the scanner check at it with ``scanner.host`` set to
    ``f"127.0.0.1:{fake.port}"``, or call the probe with the port directly.

    Real libsane cannot be pointed at an ephemeral port.  Its net backend
    resolves every host with ``getaddrinfo(name, "sane-port")`` and
    ``SANE_NET_HOSTS`` has no port syntax (sane-backends ``backend/net.c``),
    so a test whose client is libsane itself -- the reason ``LIST_THEN_DROP``
    and ``LIST_AND_KEEP`` exist -- passes ``port=6566``.  A fixed port that is
    already taken fails the test, naming the port.

    Args:
        behaviour: What every accepted connection gets.
        port: The port to listen on, or 0 for an ephemeral one.

    Yields:
        Where the fake listens, every byte it was sent, and every listing it
        served.

    """
    received: list[bytes] = []
    listings: list[int] = []
    stopping = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        _bind(listener, port)
        listener.listen(8)
        bound: int = listener.getsockname()[1]
        thread = threading.Thread(
            target=_serve,
            args=(listener, behaviour, received, listings, stopping),
            name="fake-saned",
            daemon=True,
        )
        thread.start()
        try:
            yield FakeSaned(port=bound, received=received, listings=listings)
        finally:
            stopping.set()
            # Wake the blocked accept() deterministically: the loop takes this
            # connection, sees the flag and returns.  The listener is still
            # open, so the connect cannot be refused.
            with socket.create_connection(("127.0.0.1", bound)):
                pass
            thread.join()
