"""
List scanners in a short-lived child process that can be killed.

libsane can kill the whole process while listing after a saned restart, and a
listing can hang for minutes inside a blocking C call, so every listing runs
in a child that has a deadline and is killed and reaped before the listing
returns.  See docs/explanation/decisions/0002-listing-in-a-child-process.md.

The argv is the literal ``/bin/sh -c 'exec "$P" -I "$C"'``, with the two paths
passed in the environment, because ruff's S603 accepts only a literal argv.
``exec`` replaces the shell, so the child's PID is the interpreter's own and
killing it leaves no grandchild.  ``-I`` ignores every ``PYTHON*`` variable,
so python-sane must be importable from the interpreter's own site-packages,
and the device id travels on stdin, never in argv, which any local user can
read.

The child inherits stderr and points its own fd 1 at it, keeping the stdout
pipe for the reply alone.  A backend's C stdio output reaches the log only
when stderr is a terminal: on a pipe or a socket it sits in a buffer that the
child's exit, which skips teardown, drops.

This is a leaf.  It imports neither python-sane nor the health checks: the
child does the SANE work, and the checks import the scanner layer, not the
other way round.
"""

from __future__ import annotations

import errno
import json
import logging
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from saneless.exceptions import (
    ListingAbortedError,
    ListingCrashedError,
    ListingNoAnswerError,
    ListingTimedOutError,
)
from saneless.scanner.net_hosts import SANE_NET_HOSTS, effective_sane_net_hosts
from saneless.sigpipe import sigpipe_unblocked

if TYPE_CHECKING:
    import threading

__all__ = [
    "LISTING_DEADLINE_SECONDS",
    "ChildError",
    "ListingReply",
    "ListingRequest",
    "child_environment",
    "run_listing_child",
]

logger = logging.getLogger(__name__)

# How long one listing child may run before it is killed: about 18 times the
# slowest local listing (1.7 s), and about a quarter of the ~127 s Linux's SYN
# retries cost a listing of a silently unreachable host.  Read at call time,
# so tests can shorten it.
LISTING_DEADLINE_SECONDS: Final = 30.0

# The child arms ``alarm(ceil(deadline) + margin)`` before it touches SANE.
# SIGALRM's default action ends a process even inside a blocking C call, so a
# child whose parent died -- killed, or out of memory -- cannot stay inside
# libsane after the parent's own deadline would have stopped it.
_ALARM_MARGIN_SECONDS: Final = 5

# The longest the launcher waits on a child before it looks at the caller's
# abort Event again.  The stopping thread only sets the Event; the thread that
# started the child kills and reaps it, so no thread signals a PID it does not
# own.
_ABORT_POLL_SECONDS: Final = 0.1

# The largest reply accepted.  A real reply is a few hundred bytes per device;
# anything larger is not a reply.
_MAX_REPLY_BYTES: Final = 1_048_576

# The script the child runs.  Read at call time, so tests can point the
# launcher at a stand-in.
_CHILD_FILE: Final = Path(__file__).with_name("_listing_child.py")

# The two variables the child's literal argv expands.
_PYTHON_VARIABLE: Final = "SANELESS_LISTING_PYTHON"
_CHILD_VARIABLE: Final = "SANELESS_LISTING_CHILD"

# The child's environment keeps everything SANE and its backends read (SANE
# settings, locale, proxies, certificates) and drops saneless's own settings,
# which include the Paperless token.  Matched ignoring case, because the
# settings loader reads its variables ignoring case.
_OWN_PREFIX: Final = "saneless_"

_NO_ANSWER: Final = "The scanner library returned no answer while listing scanners"
_NOT_STARTED: Final = "The scanner library could not be started to list scanners"

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
            ListingNoAnswerError: The output is over the size cap, not UTF-8,
                not JSON, or not exactly the reply schema.

        """
        if len(out) > _MAX_REPLY_BYTES:
            raise ListingNoAnswerError(_NO_ANSWER)
        try:
            lines = [line for line in out.decode("utf-8").split("\n") if line.strip()]
            payload: object = json.loads(lines[-1]) if lines else None
        except ValueError, RecursionError:
            # UnicodeDecodeError and JSONDecodeError are both ValueErrors; a
            # deeply nested document exhausts the decoder's recursion.
            raise ListingNoAnswerError(_NO_ANSWER) from None
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
            ListingNoAnswerError: ``payload`` is not exactly the reply schema.

        """
        if not isinstance(payload, dict) or not payload.keys() <= _REPLY_KEYS:
            raise ListingNoAnswerError(_NO_ANSWER)
        devices = payload.get("devices")
        if not isinstance(devices, list):
            raise ListingNoAnswerError(_NO_ANSWER)
        list_error = (
            _child_error(payload["list_error"]) if "list_error" in payload else None
        )
        opened: bool | None = None
        if "opened" in payload:
            value = payload["opened"]
            if not isinstance(value, bool):
                raise ListingNoAnswerError(_NO_ANSWER)
            opened = value
        open_error: ChildError | None = None
        if "open_error" in payload:
            if opened is not False:
                raise ListingNoAnswerError(_NO_ANSWER)
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

    Raises:
        ListingNoAnswerError: The entry is any other shape.

    """
    if not isinstance(value, list):
        raise ListingNoAnswerError(_NO_ANSWER)
    fields = [field for field in value if isinstance(field, str)]
    if len(value) != _DEVICE_FIELDS or len(fields) != _DEVICE_FIELDS:
        raise ListingNoAnswerError(_NO_ANSWER)
    return (fields[0], fields[1], fields[2], fields[3])


def _child_error(value: object) -> ChildError:
    """
    Validate one reported error: an object with exactly two strings.

    Raises:
        ListingNoAnswerError: The object is any other shape.

    """
    if not isinstance(value, dict) or value.keys() != _ERROR_KEYS:
        raise ListingNoAnswerError(_NO_ANSWER)
    type_name = value["type"]
    message = value["message"]
    if not isinstance(type_name, str) or not isinstance(message, str):
        raise ListingNoAnswerError(_NO_ANSWER)
    return ChildError(type_name=type_name, message=message)


def child_environment(configured_host: str) -> dict[str, str]:
    """
    Build the environment a listing child runs in.

    It is this process's environment minus every ``SANELESS_*`` variable, in
    any case, so no saneless setting or secret reaches the child.
    ``SANE_NET_HOSTS`` is set from the same derivation the scanner check
    probes, never copied from whatever this process's environment holds at
    the moment, and removed when that derivation names no host.

    Args:
        configured_host: The ``scanner.host`` setting, possibly empty.

    Returns:
        The child's environment, before the launcher adds its own two
        variables.

    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.casefold().startswith(_OWN_PREFIX)
    }
    hosts = effective_sane_net_hosts(configured_host)
    if hosts:
        env[SANE_NET_HOSTS] = hosts
    else:
        env.pop(SANE_NET_HOSTS, None)
    return env


def run_listing_child(
    request: ListingRequest,
    *,
    configured_host: str,
    abort: threading.Event | None = None,
) -> ListingReply:
    """
    Run one listing in a fresh child process, under a hard deadline.

    The child is dead and reaped before this returns or raises, on the
    deadline, on ``abort`` and on a Ctrl-C or server stop alike, so a caller
    holding the scanner gate releases it with nothing still inside libsane.
    Only this thread kills the child; the thread that sets ``abort`` never
    touches it, so no thread signals a PID that may already be another's.

    The child runs in a session of its own, so a Ctrl-C sent to saneless's
    process group does not kill a listing on the worker thread and get
    reported as a library crash.  A parent that dies outright leaves the child
    to its own alarm, which ends it a few seconds after the deadline.

    A crash is reported and not retried: a fresh child cannot hit the stale
    connection defect, so a crash here is a new defect that a retry would
    hide.

    Args:
        request: What to ask the child for beyond the listing.
        configured_host: The ``scanner.host`` setting, possibly empty.
        abort: Set by another thread to stop the listing part way, or
            ``None`` for a listing only the deadline ends.

    Returns:
        The child's validated reply.

    Raises:
        ListingCrashedError: The child died from a signal.
        ListingTimedOutError: The child did not finish before the deadline,
            or its own alarm ended it.
        ListingNoAnswerError: The child exited without a reply that fits
            the schema, or could not be started.
        ListingAbortedError: ``abort`` was set while the child ran.

    """
    deadline = LISTING_DEADLINE_SECONDS
    env = child_environment(configured_host)
    env[_PYTHON_VARIABLE] = sys.executable
    env[_CHILD_VARIABLE] = str(_CHILD_FILE)
    line = request.to_line(math.ceil(deadline) + _ALARM_MARGIN_SECONDS)
    try:
        # The child inherits this thread's signal mask, and must not start
        # with SIGPIPE blocked the way saneless runs.
        with sigpipe_unblocked():
            proc = subprocess.Popen(
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
                start_new_session=True,
            )
    except OSError as exc:
        # The fork or the exec failed, for instance under a process or memory
        # limit: nothing ran, so nothing could be seen.
        raise _not_started(exc) from exc
    with proc:
        out = _wait_for_child(proc, line, deadline, abort)
    returncode = proc.returncode
    if returncode == -signal.SIGALRM:
        raise _timed_out(deadline)
    if returncode < 0:
        raise _crashed(-returncode)
    if returncode != 0:
        logger.warning(
            "The scanner library's listing process exited with status %d and no answer",
            returncode,
        )
        raise ListingNoAnswerError(_NO_ANSWER)
    try:
        return ListingReply.from_stdout(out)
    except ListingNoAnswerError:
        logger.warning(
            "The scanner library's listing process wrote %d bytes, none of "
            "them an answer saneless could read",
            len(out),
        )
        raise


def _wait_for_child(
    proc: subprocess.Popen[bytes],
    line: bytes,
    deadline: float,
    abort: threading.Event | None,
) -> bytes:
    """
    Send the child its request and wait for it to finish, in short slices.

    The request goes with the first slice only: ``communicate`` keeps writing
    the rest of it across a retry after a timeout, and refuses input sent
    again.  The child is killed and reaped before anything is raised.

    Raises:
        ListingAbortedError: ``abort`` was set while the child ran.
        ListingTimedOutError: The child was still running at the deadline.

    """
    end = time.monotonic() + deadline
    payload: bytes | None = line
    while True:
        remaining = max(0.0, end - time.monotonic())
        try:
            out, _ = proc.communicate(
                payload, timeout=min(_ABORT_POLL_SECONDS, remaining)
            )
        except subprocess.TimeoutExpired:
            payload = None
            if abort is not None and abort.is_set():
                _kill_and_reap(proc)
                raise _aborted() from None
            if time.monotonic() >= end:
                _kill_and_reap(proc)
                raise _timed_out(deadline) from None
        except BaseException:
            # A KeyboardInterrupt or ScanInterrupted: the child is stopped
            # here, before the caller's gate release runs.
            _kill_and_reap(proc)
            raise
        else:
            return out


def _not_started(exc: OSError) -> ListingNoAnswerError:
    """
    Log a listing child that could not be started, and build its error.

    Only the error number's name is logged: the exception's text can name
    the interpreter's path.
    """
    name = "unknown error"
    if exc.errno is not None:
        name = errno.errorcode.get(exc.errno, f"error {exc.errno}")
    logger.warning(
        "The scanner library could not be started to list scanners: %s", name
    )
    return ListingNoAnswerError(_NOT_STARTED)


def _kill_and_reap(proc: subprocess.Popen[bytes]) -> None:
    """
    Kill the child and its process group, and wait for the child.

    The child leads a process group of its own, so killing the group also
    stops a helper program a backend started, or a fork of the child, that
    would otherwise outlive it, still holding a device or the reply's pipe.
    Only the child is waited for: the others are not this process's children.

    Args:
        proc: The child process.

    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        # The group is already gone, or holds nothing this process may signal.
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


def _aborted() -> ListingAbortedError:
    """
    Log the listing stopped for saneless's own stop, and build its error.

    Logged at INFO: the caller asked for the stop, so it says nothing about
    the scanner.

    Returns:
        The error for the caller to raise.

    """
    logger.info("Scanner listing stopped because saneless is stopping")
    return ListingAbortedError(
        "The scanner listing was stopped because saneless is stopping"
    )


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
