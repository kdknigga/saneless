"""
List scanners, and read a scanner's options, in a child process that can be killed.

libsane can kill the whole process while listing after a saned restart, and a
listing can hang for minutes inside a blocking C call, so every listing runs
in a child that has a deadline and is killed and reaped before the listing
returns.  A capabilities read, which opens a device and reads its options,
runs in the same child under the same deadline.  See
docs/explanation/decisions/0002-listing-in-a-child-process.md.

The child is started by ``child_launch.start_child``, which every SANE child
shares.  The device id travels on stdin, never in argv, which any local user
can read.

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
import signal
import subprocess
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
from saneless.scanner.child_launch import (
    child_environment,
    kill_and_reap,
    signal_name,
    start_child,
)

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

_NO_ANSWER: Final = "The scanner library returned no answer while listing scanners"
_NOT_STARTED: Final = "The scanner library could not be started to list scanners"

_REPLY_KEYS: Final = frozenset(
    {
        "devices",
        "list_error",
        "opened",
        "open_error",
        "options",
        "options_error",
        "init_error",
    }
)
_ERROR_KEYS: Final = frozenset({"type", "message"})
_DEVICE_FIELDS: Final = 4

# One option is ``[name, kind, values]``: the kind of its constraint, and the
# constraint's members.  An unconstrained option has no values.
_OPTION_FIELDS: Final = 3
_OPTION_KINDS: Final = frozenset({"list", "range", "none"})
_UNCONSTRAINED: Final = "none"

type OptionValue = str | int | float
type OptionEntry = tuple[str | None, str, tuple[OptionValue, ...]]


@dataclass(frozen=True, slots=True)
class ListingRequest:
    """
    What the child is asked to do, beyond listing.

    Attributes:
        open: A device id to open and close when the listing does not include
            it, or ``None`` to only list.
        capabilities: A device id whose options to read instead of listing,
            or ``None`` for a listing.

    """

    open: str | None = None
    capabilities: str | None = None

    def to_line(self, alarm: int) -> bytes:
        """
        Render the request as the one JSON line the child reads from stdin.

        Args:
            alarm: The seconds the child arms its own alarm for.

        Returns:
            The request, newline-terminated.

        """
        line = {"open": self.open, "capabilities": self.capabilities, "alarm": alarm}
        return json.dumps(line).encode() + b"\n"


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
        open_error: Why the requested open failed: beside ``opened`` set to
            ``False`` for a listing, or alone for a capabilities read.
        options: One ``(name, kind, values)`` entry per option a capabilities
            read found, in the device's order, or ``None`` when no options
            were read.  ``kind`` is ``"list"``, ``"range"`` or ``"none"``.
        options_error: Why reading the options of an opened device failed.
        init_error: Why python-sane could not be imported or SANE would not
            start; the request's own failure keys are reported beside it.

    """

    devices: tuple[tuple[str, str, str, str], ...]
    list_error: ChildError | None = None
    opened: bool | None = None
    open_error: ChildError | None = None
    options: tuple[OptionEntry, ...] | None = None
    options_error: ChildError | None = None
    init_error: ChildError | None = None

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
            # A listing's failed open says ``opened: false``; a capabilities
            # read's failed open has no ``opened`` at all.
            if opened is True:
                raise ListingNoAnswerError(_NO_ANSWER)
            open_error = _child_error(payload["open_error"])
        options, options_error = _capabilities(payload)
        init_error = (
            _child_error(payload["init_error"]) if "init_error" in payload else None
        )
        return cls(
            devices=tuple(_device(device) for device in devices),
            list_error=list_error,
            opened=opened,
            open_error=open_error,
            options=options,
            options_error=options_error,
            init_error=init_error,
        )


def _capabilities(
    payload: dict[str, object],
) -> tuple[tuple[OptionEntry, ...] | None, ChildError | None]:
    """
    Validate a capabilities read's result: its options, or why they failed.

    The two keys exclude each other, and neither sits beside an open or a
    listing's open result: a capabilities read does nothing else.

    Returns:
        The options, or ``None``; and the error, or ``None``.

    Raises:
        ListingNoAnswerError: The keys are combined, or an entry is off-schema.

    """
    if "options" not in payload and "options_error" not in payload:
        return None, None
    if not payload.keys().isdisjoint({"opened", "open_error"}) or (
        "options" in payload and "options_error" in payload
    ):
        raise ListingNoAnswerError(_NO_ANSWER)
    if "options_error" in payload:
        return None, _child_error(payload["options_error"])
    entries = payload["options"]
    if not isinstance(entries, list):
        raise ListingNoAnswerError(_NO_ANSWER)
    return tuple(_option(entry) for entry in entries), None


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


def _option(value: object) -> OptionEntry:
    """
    Validate one option entry: ``[name, kind, values]``.

    The name is a string or ``None`` (a group heading's); the kind is one of
    the three constraint kinds; the values are strings or numbers, never
    booleans, and an unconstrained option has none.

    Raises:
        ListingNoAnswerError: The entry is any other shape.

    """
    if not isinstance(value, list) or len(value) != _OPTION_FIELDS:
        raise ListingNoAnswerError(_NO_ANSWER)
    name, kind, values = value
    if name is not None and not isinstance(name, str):
        raise ListingNoAnswerError(_NO_ANSWER)
    if not isinstance(kind, str) or kind not in _OPTION_KINDS:
        raise ListingNoAnswerError(_NO_ANSWER)
    if not isinstance(values, list):
        raise ListingNoAnswerError(_NO_ANSWER)
    members = tuple(
        member
        for member in values
        if isinstance(member, str | int | float) and not isinstance(member, bool)
    )
    if len(members) != len(values) or (kind == _UNCONSTRAINED and members):
        raise ListingNoAnswerError(_NO_ANSWER)
    return (name, kind, members)


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
        request: What to ask the child for beyond the listing, or instead of
            it.
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
    line = request.to_line(math.ceil(deadline) + _ALARM_MARGIN_SECONDS)
    try:
        proc = start_child(_CHILD_FILE, configured_host)
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
                kill_and_reap(proc)
                raise _aborted() from None
            if time.monotonic() >= end:
                kill_and_reap(proc)
                raise _timed_out(deadline) from None
        except BaseException:
            # A KeyboardInterrupt or ScanInterrupted: the child is stopped
            # here, before the caller's gate release runs.
            kill_and_reap(proc)
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
    name = signal_name(signum)
    logger.warning("The scanner library died while listing scanners: %s", name)
    message = f"The scanner library failed while listing scanners ({name})"
    return ListingCrashedError(message)
