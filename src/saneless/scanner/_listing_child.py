"""
List the scanners, or read one's options, in a fresh interpreter.

Every device listing and capabilities read saneless makes runs this file in a
short-lived child process, so a crash or a hang inside libsane cannot take
saneless down and the parent can kill a child that runs too long.  A fresh
interpreter runs its own ``sane_init``, so it never holds the stale
net-backend control connection a fork of saneless would inherit.
See docs/explanation/decisions/0002-listing-in-a-child-process.md.

It imports the standard library and saneless's stdlib-only reply-pipe helper,
and, late and by name, saneless's thread-unwinder loader and python-sane.  The
child imports little because a bare interpreter starts in tens of milliseconds,
and every module it loads widens what a tampered path could inject.

The request is one JSON line on stdin::

    {"open": <device id or null>, "capabilities": <device id or null>,
     "alarm": <whole seconds, 0 or more>}

``capabilities`` may be left out, and is then null.

The child arms ``signal.alarm`` with ``alarm`` before it imports python-sane, so
a child whose parent died cannot sit inside libsane for ever: the default action
for ``SIGALRM`` ends the process even inside a blocking C call.  It then loads
the C library's thread unwinder, before python-sane, so no backend thread is
ever the first to load it.

The reply is one JSON line on the pipe the parent reads as stdout.  For a
listing, it holds only these keys:

- ``devices``, always: a list of ``[name, vendor, model, type]`` lists of
  strings, in python-sane's order, and empty when the listing raised;
- ``list_error``, only when the listing raised: ``{"type", "message"}``, the
  exception's class name and its text;
- ``opened``, only when the request named a device and the listing does not
  contain that exact id: whether opening it succeeded.  The open is attempted
  even when the listing raised, because a scan opens a configured id without
  listing first;
- ``open_error``, only when ``opened`` is false: ``{"type", "message"}``.

A request naming ``capabilities`` lists nothing: it opens that device, reads
its options, and cancels and closes it.  Its reply holds ``devices``, always
empty, and one of:

- ``options``: one ``[name, kind, values]`` list per option, in the device's
  order.  ``name`` is a string, empty for option 0, or null for a group
  heading; ``kind`` is ``"list"`` for a word or string list, ``"range"`` for
  a ``(min, max, step)`` range and ``"none"`` for an unconstrained option,
  whose ``values`` is empty;
- ``open_error``, when the open raised: ``{"type", "message"}``;
- ``options_error``, when reading the options raised: ``{"type", "message"}``.

When python-sane cannot be imported or SANE will not start, the reply also
holds ``init_error``: ``{"type", "message"}``.  A listing's reply then keeps
its ``list_error``, and its ``opened`` and ``open_error`` when it asked for an
open, all carrying the same error; a capabilities reply holds ``devices`` and
``init_error`` alone.

The reply uses ``json.dumps``' default ASCII escaping, so a lone surrogate
from a name python-sane could not decode reaches the parent unchanged.  JSON
rather than pickle: nothing the child writes can execute when the parent reads
it.

That pipe is kept private to the reply: before anything else runs, the child
moves it to a descriptor of its own and points fd 1 at stderr.  C stdio on a
pipe holds its output until exit, so a single ``printf`` in any backend left
on the reply's pipe would turn a good reply into no answer.

Every python-sane failure travels back as data, so the child ends without a
reply only when a signal ends it or it cannot read its request.  It has no
logger: the parent decides what to log.  Once the reply is written the child
ends with ``os._exit``, because the parent reads the exit status first and a
crash or hang in teardown would otherwise turn a good listing into a failed
one.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import signal
import sys
from typing import TYPE_CHECKING, Final, Protocol, TextIO

from saneless.scanner._child_stdio import flush_standard_streams, take_reply_fd

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

__all__ = ["SaneDevice", "SaneModule", "main", "respond", "start_failure_reply"]

_BAD_REQUEST: Final = 2
"""The exit status for a request line the child cannot read."""

_DEVICE_FIELDS: Final = 4
"""How many fields a listed device has: name, vendor, model and type."""

_NAME_FIELD: Final = 1
"""Where a SANE option tuple holds the option's name."""

_CONSTRAINT_FIELD: Final = 8
"""Where a SANE option tuple holds the option's constraint."""


class SaneDevice(Protocol):
    """The part of a python-sane device handle the child uses."""

    def cancel(self) -> object:
        """Cancel any operation in progress on the device."""
        ...

    def close(self) -> object:
        """Close the device handle."""
        ...

    def get_options(self) -> Iterable[Sequence[object]]:
        """Return the device's option tuples."""
        ...


class SaneModule(Protocol):
    """
    The part of the python-sane module ``respond`` uses.

    Both members are read-only properties rather than methods, so the real
    module, whose attributes a type checker sees as read-only module
    attributes, matches the protocol as well as a stand-in class does.
    """

    @property
    def get_devices(self) -> Callable[[], Iterable[Sequence[object]]]:
        """The function that lists the devices SANE can see."""
        ...

    @property
    def open(self) -> Callable[[str], SaneDevice]:
        """The function that opens one device by its SANE name."""
        ...


def _error(exc: Exception) -> dict[str, str]:
    """
    Describe a python-sane failure as data the parent can read.

    Args:
        exc: The exception python-sane raised.

    Returns:
        The exception's class name and its text.

    """
    return {"type": type(exc).__name__, "message": str(exc)}


def _requested_device(request: Mapping[str, object]) -> str | None:
    """
    Return the device the request asks to open, if it names one.

    Args:
        request: The decoded request.

    Returns:
        The device id, or ``None`` when ``open`` is null or empty.

    """
    target = request.get("open")
    return target if isinstance(target, str) and target else None


def _open_and_close(sane_module: SaneModule, device_id: str) -> dict[str, object]:
    """
    Open one device, then cancel and close it whatever happens.

    A failure to cancel or close is swallowed: the open has already shown that
    the device can be reached, which is the one fact the caller wants.

    Returns:
        ``{"opened": True}``, or ``{"opened": False, "open_error": ...}`` when
        the open raised.

    """
    try:
        dev = sane_module.open(device_id)
    except Exception as exc:
        # python-sane raises _sane.error, RuntimeError or AttributeError with
        # no shared base, so the boundary catches Exception.
        return {"opened": False, "open_error": _error(exc)}
    with contextlib.suppress(Exception):
        dev.cancel()
    with contextlib.suppress(Exception):
        dev.close()
    return {"opened": True}


def _capabilities_device(request: Mapping[str, object]) -> str | None:
    """
    Return the device whose options the request asks for, if it names one.

    Args:
        request: The decoded request.

    Returns:
        The device id, or ``None`` for a listing.

    """
    target = request.get("capabilities")
    return target if isinstance(target, str) and target else None


def _option_value(member: object) -> object:
    """
    Make one constraint member JSON data the parent accepts.

    A number or a string is kept; a boolean becomes its number, and anything
    else its text, so a device's odd value cannot cost the whole reply.

    Returns:
        A string, an int or a float.

    """
    if isinstance(member, bool):
        return int(member)
    if isinstance(member, str | int | float):
        return member
    return str(member)


def _option_entry(option: Sequence[object]) -> list[object]:
    """
    Describe one SANE option tuple as ``[name, kind, values]``.

    Only the name and the constraint are sent: they are all a capabilities
    read uses.  A tuple too short to hold a constraint is unconstrained.

    Returns:
        The entry the module docstring describes.

    """
    name = option[_NAME_FIELD] if len(option) > _NAME_FIELD else None
    if name is not None and not isinstance(name, str):
        name = str(name)
    constraint = option[_CONSTRAINT_FIELD] if len(option) > _CONSTRAINT_FIELD else None
    if isinstance(constraint, list):
        return [name, "list", [_option_value(member) for member in constraint]]
    if isinstance(constraint, tuple):
        return [name, "range", [_option_value(member) for member in constraint]]
    return [name, "none", []]


def _capabilities(sane_module: SaneModule, device_id: str) -> dict[str, object]:
    """
    Open one device, read its options, then cancel and close it whatever happens.

    A failure to cancel or close is swallowed: the options are already read,
    and they are what the caller wants.

    Returns:
        ``{"options": ...}``, or ``{"open_error": ...}`` when the open raised,
        or ``{"options_error": ...}`` when reading the options did.

    """
    try:
        dev = sane_module.open(device_id)
    except Exception as exc:
        # python-sane raises _sane.error, RuntimeError or AttributeError with
        # no shared base, so the boundary catches Exception.
        return {"open_error": _error(exc)}
    try:
        options = [_option_entry(option) for option in dev.get_options()]
    except Exception as exc:
        # The same boundary as the open, for the same reason.
        return {"options_error": _error(exc)}
    else:
        return {"options": options}
    finally:
        with contextlib.suppress(Exception):
            dev.cancel()
        with contextlib.suppress(Exception):
            dev.close()


def respond(
    request: Mapping[str, object], sane_module: SaneModule
) -> dict[str, object]:
    """
    Answer the request: read a device's options, or list and maybe open one.

    A capabilities request lists nothing.  Otherwise the devices are listed,
    and the requested one opened if it is not listed.

    SANE must already be initialised: this function never calls ``init()``, so
    a caller that already holds an initialised module can reuse it.

    Args:
        request: The decoded request.  Only ``open`` and ``capabilities`` are
            read here.
        sane_module: The python-sane module, or a stand-in for it.

    Returns:
        The reply, with exactly the keys the module docstring describes.

    """
    capabilities = _capabilities_device(request)
    if capabilities is not None:
        return {"devices": [], **_capabilities(sane_module, capabilities)}
    reply: dict[str, object] = {}
    devices: list[list[str]] = []
    try:
        devices = [
            [str(field) for field in list(device)[:_DEVICE_FIELDS]]
            for device in sane_module.get_devices()
        ]
    except Exception as exc:
        # python-sane raises _sane.error, RuntimeError or AttributeError with
        # no shared base, so the boundary catches Exception.
        devices = []
        reply["list_error"] = _error(exc)
    reply["devices"] = devices
    target = _requested_device(request)
    if target is not None and target not in {device[0] for device in devices if device}:
        reply.update(_open_and_close(sane_module, target))
    return reply


def _read_request(line: str) -> dict[str, object] | None:
    """
    Decode and check the request line.

    Args:
        line: The line read from stdin, possibly empty.

    Returns:
        The request, or ``None`` when it is not a JSON object whose ``open`` is
        null or a string, whose ``capabilities`` is missing, null or a
        non-empty string, and whose ``alarm`` is a whole number of seconds, 0
        or more.

    """
    try:
        request = json.loads(line)
    except ValueError, RecursionError:
        return None
    if not isinstance(request, dict):
        return None
    target = request.get("open")
    alarm = request.get("alarm")
    if target is not None and not isinstance(target, str):
        return None
    capabilities = request.get("capabilities")
    if capabilities is not None and not (
        isinstance(capabilities, str) and capabilities
    ):
        return None
    if isinstance(alarm, bool) or not isinstance(alarm, int) or alarm < 0:
        return None
    return request


def load_thread_unwinder() -> None:
    """
    Load the C library's thread unwinder, through saneless's own loader.

    The loader is imported here, once the reply's pipe is private, and not at
    the top of the file: it imports ``ctypes``, which opens a descriptor of its
    own.  In a child started with fd 2 closed, that descriptor would take fd 2
    first, and a helper program a backend starts would get no stderr.
    """
    loader = importlib.import_module("saneless.thread_unwinder")
    loader.load_thread_unwinder()


def start_failure_reply(
    request: Mapping[str, object], exc: Exception
) -> dict[str, object]:
    """
    Build the reply for a python-sane that cannot be imported or will not start.

    The failure is reported as ``init_error``, so the parent can tell a library
    that would not start from a request that failed.  A listing also fails its
    listing and its requested open alike, with the same error, as it always
    has.

    Args:
        request: The decoded request.
        exc: What the import or ``init()`` raised.

    Returns:
        The reply, with exactly the keys the module docstring describes.

    """
    error = _error(exc)
    if _capabilities_device(request) is not None:
        return {"devices": [], "init_error": error}
    reply: dict[str, object] = {"devices": [], "list_error": error}
    if _requested_device(request) is not None:
        reply["opened"] = False
        reply["open_error"] = error
    reply["init_error"] = error
    return reply


def main(stdin: TextIO, reply_channel: TextIO) -> int:
    """
    Read the request, answer it, and write the reply.

    The alarm is armed before python-sane is imported, so nothing below it can
    outlast the deadline the parent chose.  The thread unwinder is loaded
    after the alarm and before python-sane, so no backend thread loads it.

    Args:
        stdin: Where the request line is read from.
        reply_channel: Where the reply line is written: the parent's pipe,
            which nothing else in the process can write to.

    Returns:
        0 once the reply is written, or 2 when the request cannot be read, in
        which case nothing is written and python-sane is never imported.

    """
    request = _read_request(stdin.readline())
    if request is None:
        return _BAD_REQUEST
    alarm = request["alarm"]
    signal.alarm(alarm if isinstance(alarm, int) else 0)
    load_thread_unwinder()
    try:
        sane = importlib.import_module("sane")
        sane.init()
    except Exception as exc:
        # A missing python-sane or a SANE that cannot start is reported as
        # data, like every other python-sane failure.
        reply = start_failure_reply(request, exc)
    else:
        reply = respond(request, sane)
    reply_channel.write(json.dumps(reply) + "\n")
    reply_channel.flush()
    return 0


def _private_reply_channel() -> TextIO:
    """
    Take the parent's stdout pipe for the reply, and point fd 1 at stderr.

    Returns:
        A text stream on a new descriptor for the parent's pipe.

    """
    return os.fdopen(take_reply_fd(), "w", encoding="ascii")


if __name__ == "__main__":
    with _private_reply_channel() as channel:
        status = main(sys.stdin, channel)
    # The reply is on the pipe, so skip teardown, whose crash or hang would
    # turn a good listing into a failed one.  The kernel closes the sockets and
    # USB handles, and a saned sees EOF.
    flush_standard_streams()
    os._exit(status)
