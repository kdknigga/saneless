"""
List the scanners in a fresh interpreter, and report back over one JSON line.

Every device listing saneless makes runs this file in a short-lived child
process, so a crash or a hang inside libsane while it lists scanners cannot
take saneless down, and the parent can kill a child that runs too long.

Why the listing happens here and not in saneless's own process: libsane's net
backend keeps one control connection per scanner host open between listings.
Once that connection is lost, for example because the host's saned restarted,
the next ``sane_get_devices`` fails its request, ignores the failed status and
reads a reply that was never filled in (sane-backends ``backend/net.c``,
``sane_get_devices``), which crashes the process.  A child started from a fresh
interpreter runs its own ``sane_init``, so it never holds a stale control
connection, and it resolves the ``SANE_NET_HOSTS`` names afresh every time.  A
fork of saneless would inherit the very connection this avoids, which is why
the parent starts this file as a new program instead.

The file is run by path with ``python -I``, and it imports nothing from
``saneless``: importing the package runs its ``__init__``, which loads the
configuration layer and costs about 130 ms, several times what a local listing
takes.  It imports only the standard library, and python-sane late and by name.

The request is one JSON line on stdin::

    {"open": <device id or null>, "alarm": <whole seconds, 0 or more>}

The child arms ``signal.alarm`` with ``alarm`` before it imports python-sane, so
a child whose parent died cannot sit inside libsane for ever: the default action
for ``SIGALRM`` ends the process even inside a blocking C call.  The device id
arrives on stdin and never in argv, because any local user can read a process's
argv, and a network device id names a host on the LAN.

The reply is one JSON line on stdout, holding only these keys:

- ``devices``, always: a list of ``[name, vendor, model, type]`` lists of
  strings, in python-sane's order, and empty when the listing raised;
- ``list_error``, only when the listing raised: ``{"type", "message"}``, the
  exception's class name and its text;
- ``opened``, only when the request named a device and the listing does not
  contain that exact id: whether opening it succeeded.  The open is attempted
  even when the listing raised, because a scan opens a configured id without
  listing first;
- ``open_error``, only when ``opened`` is false: ``{"type", "message"}``.

The reply is written with ``json.dumps``' default ASCII escaping, so every
string, a lone surrogate from a name python-sane could not decode included,
reaches the parent unchanged.  JSON rather than pickle: nothing the child
writes can execute when the parent reads it.

Every python-sane call is wrapped in ``except Exception``, never
``BaseException``, and its failure travels back as data.  So the child ends
without a reply only when a signal ends it or when it cannot read its request,
which exits with status 2.  It has no logger: the parent decides what to log.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import signal
import sys
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

__all__ = ["SaneDevice", "SaneModule", "main", "respond"]

_BAD_REQUEST: Final = 2
"""The exit status for a request line the child cannot read."""

_DEVICE_FIELDS: Final = 4
"""How many fields a listed device has: name, vendor, model and type."""


class SaneDevice(Protocol):
    """The part of a python-sane device handle the child uses."""

    def cancel(self) -> object:
        """Cancel any operation in progress on the device."""
        ...

    def close(self) -> object:
        """Close the device handle."""
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

    Args:
        sane_module: The python-sane module, or a stand-in for it.
        device_id: The SANE name of the device to open.

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


def respond(
    request: Mapping[str, object], sane_module: SaneModule
) -> dict[str, object]:
    """
    List the devices, open the requested one if it is not listed, and reply.

    SANE must already be initialised: this function never calls ``init()``, so
    a caller that already holds an initialised module can reuse it.

    Args:
        request: The decoded request.  Only ``open`` is read here.
        sane_module: The python-sane module, or a stand-in for it.

    Returns:
        The reply, with exactly the keys the module docstring describes.

    """
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
        null or a string and whose ``alarm`` is a whole number of seconds, 0 or
        more.

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
    if isinstance(alarm, bool) or not isinstance(alarm, int) or alarm < 0:
        return None
    return request


def main() -> int:
    """
    Read the request, list the scanners, and write the reply.

    The alarm is armed before python-sane is imported, so nothing below it can
    outlast the deadline the parent chose.

    Returns:
        0 once the reply is written, or 2 when the request cannot be read, in
        which case nothing is written and python-sane is never imported.

    """
    request = _read_request(sys.stdin.readline())
    if request is None:
        return _BAD_REQUEST
    alarm = request["alarm"]
    signal.alarm(alarm if isinstance(alarm, int) else 0)
    try:
        sane = importlib.import_module("sane")
        sane.init()
    except Exception as exc:
        # A missing python-sane or a SANE that cannot start fails the listing
        # and the requested open alike, and both are reported as data.
        error = _error(exc)
        reply: dict[str, object] = {"devices": [], "list_error": error}
        if _requested_device(request) is not None:
            reply["opened"] = False
            reply["open_error"] = error
    else:
        reply = respond(request, sane)
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
