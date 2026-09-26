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

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = [
    "EXIT_REQUEST",
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

# SANE_NET_EXIT: procedure 10, no arguments, and saned sends no reply.
EXIT_REQUEST: Final = struct.pack(">i", 10)

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
    """

    HEALTHY = "HEALTHY"
    CLOSE = "CLOSE"
    BAD_STATUS = "BAD_STATUS"
    BAD_VERSION = "BAD_VERSION"
    FLOOD = "FLOOD"


@dataclass(frozen=True, slots=True)
class FakeSaned:
    """
    A running fake saned: where it listens and what it was sent.

    Attributes:
        port: The ephemeral loopback port it listens on.
        received: Every chunk of bytes read from any client, in arrival order.
            Complete once the context manager has exited, because teardown
            joins the serving thread.

    """

    port: int
    received: list[bytes] = field(default_factory=list)


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


def _handle(
    conn: socket.socket, behaviour: SanedBehaviour, received: list[bytes]
) -> None:
    """
    Serve one accepted connection, then close it whatever happened.

    Args:
        conn: The accepted connection, owned by this function.
        behaviour: What to do with it.
        received: Where every byte read is recorded.

    """
    try:
        if behaviour is SanedBehaviour.CLOSE:
            return
        conn.settimeout(_CONNECTION_IDLE_SECONDS)
        if not _read_init_request(conn, received):
            return
        conn.sendall(_reply_for(behaviour))
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
    stopping: threading.Event,
) -> None:
    """
    Accept connections one at a time until teardown asks the loop to stop.

    Args:
        listener: The bound, listening socket.
        behaviour: What to do with each connection.
        received: Where every byte read is recorded.
        stopping: Set by teardown before it wakes ``accept()``.

    """
    while True:
        try:
            conn, _peer = listener.accept()
        except OSError:
            return
        if stopping.is_set():
            conn.close()
            return
        _handle(conn, behaviour, received)


@contextlib.contextmanager
def fake_saned(behaviour: SanedBehaviour) -> Generator[FakeSaned]:
    """
    Serve one saned behaviour on an ephemeral 127.0.0.1 port.

    Point the scanner check at it with ``scanner.host`` set to
    ``f"127.0.0.1:{fake.port}"``, or call the probe with the port directly.

    Args:
        behaviour: What every accepted connection gets.

    Yields:
        Where the fake listens, and every byte it was sent.

    """
    received: list[bytes] = []
    stopping = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port: int = listener.getsockname()[1]
        thread = threading.Thread(
            target=_serve,
            args=(listener, behaviour, received, stopping),
            name="fake-saned",
            daemon=True,
        )
        thread.start()
        try:
            yield FakeSaned(port=port, received=received)
        finally:
            stopping.set()
            # Wake the blocked accept() deterministically: the loop takes this
            # connection, sees the flag and returns.  The listener is still
            # open, so the connect cannot be refused.
            with socket.create_connection(("127.0.0.1", port)):
                pass
            thread.join()
