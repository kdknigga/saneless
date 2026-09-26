"""
List scanners in a short-lived child process that can be killed.

Why a child at all: libsane's net backend keeps one control connection per
scanner host open between listings.  Once the saned on that host restarts or
dies, the next ``sane_get_devices`` sends its request on the lost connection,
ignores the failed status and reads a reply that was never filled in
(sane-backends ``backend/net.c``), and the whole process dies from SIGSEGV,
SIGABRT or SIGBUS.  A listing can also hang for minutes inside a blocking C
call on a host that has silently gone away.  Neither can be contained in the
process that made the call.  In a child, a crash is a failed listing and a
hang is a process to kill.

How the child is started, measured against the local ``test`` backend from a
parent that had already initialised SANE and had a second thread running:

- ``fork`` inherits the parent's libsane, stale control connection included,
  and after a saned restart every forked child crashed.  It is not an option.
- The standard library's "spawn" start method for Python worker processes
  costs about 340 ms per listing, because it re-imports the parent's
  ``__main__``, and it leaves a resource tracker running.
- Its "forkserver" start method leaves two resident helper processes.
- A clean exec of the interpreter on the child script, by path, costs about
  14 ms and leaves nothing behind.  That is what this module does.

Every argv element is a literal: ``/bin/sh -c 'exec "$P" -I "$C"'``, with the
interpreter and the child script passed in the environment, quoted so they
are never re-split.  ruff's S603 accepts only a literal argv, and ``exec``
replaces the shell, so the child's PID is the interpreter's own and killing
and waiting on it leaves no grandchild.  ``-I`` runs the interpreter in
isolated mode: every ``PYTHON*`` variable is ignored and the script's
directory is not put on ``sys.path``.  The device id travels on stdin, never
in argv, which any local user can read.

The child's stderr is inherited, so libsane's ``SANE_DEBUG_*`` output keeps
reaching the log it always reached.

This is a leaf.  It imports neither python-sane nor the health checks: the
child does the SANE work, and the checks import the scanner layer, not the
other way round.
"""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from saneless.exceptions import ListingCrashedError, ListingTimedOutError, ScanError
from saneless.scanner.net_hosts import SANE_NET_HOSTS, effective_sane_net_hosts

__all__ = [
    "LISTING_DEADLINE_SECONDS",
    "ChildError",
    "ListingReply",
    "ListingRequest",
    "child_environment",
    "run_listing_child",
]

logger = logging.getLogger(__name__)

# How long one listing child may run before it is killed.  The slowest
# listing measured locally took about 1.7 s, with a full distribution backend
# list, so this is about 18 times that.  The margin is for what a real
# deployment adds and a test bench does not: a saned doing name lookups
# before it answers, a scanner host enumerating real USB hardware, eSCL
# discovery on a real network.  It is about a quarter of the ~127 s a
# silently unreachable host costs a listing, because Linux retries a SYN six
# times by default inside a blocking C call nothing can interrupt.  Not a
# setting.  Read at call time, so tests can shorten it.
LISTING_DEADLINE_SECONDS: Final = 30.0

# The child arms ``alarm(ceil(deadline) + margin)`` before it touches SANE.
# SIGALRM's default action ends a process even inside a blocking C call, so a
# child whose parent died -- killed, or out of memory -- cannot stay inside
# libsane after the parent's own deadline would have stopped it.
_ALARM_MARGIN_SECONDS: Final = 5

# The largest reply accepted.  A real reply is a few hundred bytes per device;
# anything larger is not a reply.
_MAX_REPLY_BYTES: Final = 1_048_576

# The script the child runs.  Read at call time, so tests can point the
# launcher at a stand-in.
_CHILD_FILE: Final = Path(__file__).with_name("_listing_child.py")

# The two variables the child's literal argv expands.  The argv itself is
# written out at the ``Popen`` call: ruff's S603 accepts a literal there and
# flags the same tuple held in a constant.
_PYTHON_VARIABLE: Final = "SANELESS_LISTING_PYTHON"
_CHILD_VARIABLE: Final = "SANELESS_LISTING_CHILD"

# The child's environment keeps everything SANE and its backends read (SANE
# settings, locale, proxies, certificates) and drops saneless's own settings,
# which include the Paperless token.
_OWN_PREFIX: Final = "SANELESS_"

_NO_ANSWER: Final = "The scanner library returned no answer while listing scanners"

_REPLY_KEYS: Final = frozenset({"devices", "list_error", "opened", "open_error"})
_ERROR_KEYS: Final = frozenset({"type", "message"})
_DEVICE_FIELDS: Final = 4


@dataclass(frozen=True, slots=True)
class ListingRequest:
    """
    What the child is asked to do, beyond listing.

    Attributes:
        open: A device id to open and close when the listing does not include
            it, or ``None`` to only list.

    """

    open: str | None = None

    def to_line(self, alarm: int) -> bytes:
        """
        Render the request as the one JSON line the child reads from stdin.

        Args:
            alarm: The seconds the child arms its own alarm for.

        Returns:
            The request, newline-terminated.

        """
        return json.dumps({"open": self.open, "alarm": alarm}).encode() + b"\n"


@dataclass(frozen=True, slots=True)
class ChildError:
    """
    An exception the child caught, as it reported it.

    Attributes:
        type_name: The exception's class name.
        message: Its text, exactly as the child sent it: not yet normalised.

    """

    type_name: str
    message: str


@dataclass(frozen=True, slots=True)
class ListingReply:
    """
    What a listing child reported, validated against its schema.

    Attributes:
        devices: One ``(name, vendor, model, type)`` tuple per device listed.
        list_error: Why the listing failed, when it raised; ``devices`` is
            then empty.
        opened: Whether the requested open worked, or ``None`` when no open
            was attempted.
        open_error: Why the requested open failed; only present when
            ``opened`` is ``False``.

    """

    devices: tuple[tuple[str, str, str, str], ...]
    list_error: ChildError | None = None
    opened: bool | None = None
    open_error: ChildError | None = None

    @classmethod
    def from_stdout(cls, out: bytes) -> ListingReply:
        """
        Decode the child's stdout into a reply.

        The reply is the last non-empty line, one JSON object with exactly
        the child's keys and types.  Anything else is treated as no answer:
        the child's text is not trusted enough to pass on, or to chain.

        Args:
            out: Everything the child wrote to stdout.

        Returns:
            The validated reply.

        Raises:
            ScanError: The output is over the size cap, not UTF-8, not JSON,
                or not exactly the reply schema.

        """
        if len(out) > _MAX_REPLY_BYTES:
            raise ScanError(_NO_ANSWER)
        try:
            lines = [line for line in out.decode("utf-8").split("\n") if line.strip()]
            payload: object = json.loads(lines[-1]) if lines else None
        except ValueError, RecursionError:
            # UnicodeDecodeError and JSONDecodeError are both ValueErrors; a
            # deeply nested document exhausts the decoder's recursion.
            raise ScanError(_NO_ANSWER) from None
        return cls._from_payload(payload)

    @classmethod
    def _from_payload(cls, payload: object) -> ListingReply:
        """
        Validate a decoded reply object against the schema.

        Args:
            payload: The decoded JSON value.

        Returns:
            The validated reply.

        Raises:
            ScanError: ``payload`` is not exactly the reply schema.

        """
        if not isinstance(payload, dict) or not payload.keys() <= _REPLY_KEYS:
            raise ScanError(_NO_ANSWER)
        devices = payload.get("devices")
        if not isinstance(devices, list):
            raise ScanError(_NO_ANSWER)
        list_error = (
            _child_error(payload["list_error"]) if "list_error" in payload else None
        )
        opened: bool | None = None
        if "opened" in payload:
            value = payload["opened"]
            if not isinstance(value, bool):
                raise ScanError(_NO_ANSWER)
            opened = value
        open_error: ChildError | None = None
        if "open_error" in payload:
            if opened is not False:
                raise ScanError(_NO_ANSWER)
            open_error = _child_error(payload["open_error"])
        return cls(
            devices=tuple(_device(device) for device in devices),
            list_error=list_error,
            opened=opened,
            open_error=open_error,
        )


def _device(value: object) -> tuple[str, str, str, str]:
    """
    Validate one device entry: a list of exactly four strings.

    Args:
        value: The decoded entry.

    Returns:
        The entry as a ``(name, vendor, model, type)`` tuple.

    Raises:
        ScanError: The entry is any other shape.

    """
    if not isinstance(value, list):
        raise ScanError(_NO_ANSWER)
    fields = [field for field in value if isinstance(field, str)]
    if len(value) != _DEVICE_FIELDS or len(fields) != _DEVICE_FIELDS:
        raise ScanError(_NO_ANSWER)
    return (fields[0], fields[1], fields[2], fields[3])


def _child_error(value: object) -> ChildError:
    """
    Validate one reported error: an object with exactly two strings.

    Args:
        value: The decoded error object.

    Returns:
        The reported error.

    Raises:
        ScanError: The object is any other shape.

    """
    if not isinstance(value, dict) or value.keys() != _ERROR_KEYS:
        raise ScanError(_NO_ANSWER)
    type_name = value["type"]
    message = value["message"]
    if not isinstance(type_name, str) or not isinstance(message, str):
        raise ScanError(_NO_ANSWER)
    return ChildError(type_name=type_name, message=message)


def child_environment(configured_host: str) -> dict[str, str]:
    """
    Build the environment a listing child runs in.

    It is this process's environment minus every ``SANELESS_*`` variable, so
    no saneless setting or secret reaches the child.  ``SANE_NET_HOSTS`` is
    set from the same derivation the scanner check probes, never copied from
    whatever this process's environment holds at the moment, and removed
    when that derivation names no host.

    Args:
        configured_host: The ``scanner.host`` setting, possibly empty.

    Returns:
        The child's environment, before the launcher adds its own two
        variables.

    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_OWN_PREFIX)
    }
    hosts = effective_sane_net_hosts(configured_host)
    if hosts:
        env[SANE_NET_HOSTS] = hosts
    else:
        env.pop(SANE_NET_HOSTS, None)
    return env


def run_listing_child(request: ListingRequest, *, configured_host: str) -> ListingReply:
    """
    Run one listing in a fresh child process, under a hard deadline.

    The child is dead and reaped before this returns or raises, whatever
    happens: on the deadline it is killed and waited for, and so is a child
    still running when the wait is interrupted by a Ctrl-C or a server stop.
    A caller that holds the scanner gate around this call therefore releases
    it with nothing still inside libsane.  No new session is started, so a
    terminal's Ctrl-C reaches the child as well.

    A crash is reported and not retried: a fresh child cannot hit the stale
    connection defect, so a crash here is a new defect that a retry would
    hide.

    Args:
        request: What to ask the child for beyond the listing.
        configured_host: The ``scanner.host`` setting, possibly empty.

    Returns:
        The child's validated reply.

    Raises:
        ListingCrashedError: The child died from a signal.
        ListingTimedOutError: The child did not finish before the deadline,
            or its own alarm ended it.
        ScanError: The child exited without a reply that fits the schema.

    """
    deadline = LISTING_DEADLINE_SECONDS
    env = child_environment(configured_host)
    env[_PYTHON_VARIABLE] = sys.executable
    env[_CHILD_VARIABLE] = str(_CHILD_FILE)
    line = request.to_line(math.ceil(deadline) + _ALARM_MARGIN_SECONDS)
    with subprocess.Popen(
        (
            "/bin/sh",
            "-c",
            'exec "$SANELESS_LISTING_PYTHON" -I "$SANELESS_LISTING_CHILD"',
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        env=env,
        close_fds=True,
    ) as proc:
        try:
            out, _ = proc.communicate(line, timeout=deadline)
        except subprocess.TimeoutExpired:
            _kill_and_reap(proc)
            raise _timed_out(deadline) from None
        except BaseException:
            # A KeyboardInterrupt or ScanInterrupted: the child is stopped
            # here, before the caller's gate release runs.
            _kill_and_reap(proc)
            raise
    returncode = proc.returncode
    if returncode == -signal.SIGALRM:
        raise _timed_out(deadline)
    if returncode < 0:
        raise _crashed(-returncode)
    if returncode != 0:
        raise ScanError(_NO_ANSWER)
    return ListingReply.from_stdout(out)


def _kill_and_reap(proc: subprocess.Popen[bytes]) -> None:
    """
    Kill the child and wait for it, so it is neither running nor a zombie.

    Args:
        proc: The child process.

    """
    proc.kill()
    proc.wait()


def _timed_out(deadline: float) -> ListingTimedOutError:
    """
    Log the stopped listing and build the error that reports it.

    Args:
        deadline: The deadline the child overran, in seconds.

    Returns:
        The error for the caller to raise.

    """
    logger.warning(
        "The scanner library did not finish listing scanners within %.0f s, "
        "so the listing was stopped",
        deadline,
    )
    message = (
        f"The scanner library did not finish listing scanners in time "
        f"({deadline:.0f} s)"
    )
    return ListingTimedOutError(message)


def _crashed(signum: int) -> ListingCrashedError:
    """
    Log the crashed listing, naming the signal only, and build its error.

    Args:
        signum: The signal the child died from.

    Returns:
        The error for the caller to raise.

    """
    try:
        name = signal.Signals(signum).name
    except ValueError:
        name = f"signal {signum}"
    logger.warning("The scanner library died while listing scanners: %s", name)
    message = f"The scanner library failed while listing scanners ({name})"
    return ListingCrashedError(message)
