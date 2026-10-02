"""
Tests for SIGPIPE handling: a dead peer must never kill saneless.

libsane puts SIGPIPE back to its default action, at the C level and behind
Python's back, whenever a read ends with an error status -- the end of every
feeder batch included.  From then on a write to a socket whose peer has gone
would end the process with no traceback.  saneless blocks the signal at
startup, so such a write raises ``BrokenPipeError`` instead.

The libsane test runs the risky part in a child process started through
``saneless.main()``: the pytest process itself must never be left with SIGPIPE
re-armed, and only the real entry point shows that the startup path applies
the mask.

The listing test blocks SIGPIPE in its own thread first, because that is the
state a launching thread in saneless is in, and then checks what the listing
child started with.  A blocked mask is inherited across fork and exec, so a
launched program would start with SIGPIPE blocked unless the launcher undoes
it around the launch.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING, Final

import pytest

from saneless.exceptions import ListingNoAnswerError
from saneless.scanner import listing
from saneless.scanner.listing import ListingRequest

if TYPE_CHECKING:
    from pathlib import Path

_CHILD_TIMEOUT_SECONDS: Final = 30
_SIGPIPE_BIT: Final = 1 << (signal.SIGPIPE - 1)

# Runs a scan through real libsane that ends with the parametrised error
# status, then writes to a socket whose peer is closed.  The probe stands in
# for the CLI, so the process goes through saneless.main()'s real startup.
_LIBSANE_PROBE: Final = """\
import os
import socket

import saneless
import saneless.cli

STATUS = os.environ["SANELESS_TEST_STATUS"]


def _sigpipe_ignored():
    with open("/proc/self/status", encoding="ascii") as status:
        for line in status:
            if line.startswith("SigIgn:"):
                return bool(int(line.split()[1], 16) & (1 << 12))
    raise RuntimeError("no SigIgn line")


def probe():
    import sane

    from saneless.scanner import sane_backend

    sane.init()
    device = sane.open("test:0")
    try:
        device.source = "Flatbed"
        device.read_return_value = STATUS
        # Read and cancel the way saneless does: the cancel waits for the
        # backend's reader thread to end, so it cannot kill it holding a lock.
        before = sane_backend._native_thread_ids()
        device.start()
        try:
            device.snap(no_cancel=True)
        except Exception:  # the test backend's read error is the point
            pass
        sane_backend._await_backend_threads(before)
        device.cancel()
        if not _sigpipe_ignored():
            print("sigpipe-default", flush=True)
    finally:
        device.close()
    a, b = socket.socketpair()
    b.close()
    try:
        a.sendall(b"x")
    except BrokenPipeError:
        print("broken-pipe", flush=True)
    finally:
        a.close()


saneless.cli.cli = probe
saneless.main()
"""

# A stand-in listing child that records its own blocked-signal mask in the
# file named by the format field, then exits without a reply.
_MASK_RECORDING_CHILD: Final = """\
import sys

sys.stdin.readline()
with open("/proc/self/status", encoding="ascii") as status:
    for line in status:
        if line.startswith("SigBlk:"):
            mask = line.split()[1]
            break
with open({record!r}, "w", encoding="ascii") as record:
    record.write(mask)
"""


@pytest.mark.sane_hardware
@pytest.mark.usefixtures("sane_test_backend_config")
@pytest.mark.parametrize(
    "status",
    ["SANE_STATUS_NO_DOCS", "SANE_STATUS_CANCELLED"],
    ids=["NO_DOCS", "CANCELLED"],
)
def test_a_failed_libsane_read_cannot_let_a_dead_peer_kill_saneless(
    status: str,
) -> None:
    """
    A write to a closed peer after a failed read raises, not kills.

    The child first confirms libsane really did put SIGPIPE back to its
    default action, so the test cannot pass because libsane stopped doing so.

    Args:
        status: The status the test backend ends its read with.

    """
    # Every argv element is a literal and the per-run values travel in the
    # environment, quoted so they are never re-split.
    result = subprocess.run(
        ["/bin/sh", "-c", 'exec "$SANELESS_TEST_PYTHON" -c "$SANELESS_TEST_CODE"'],
        env={
            **os.environ,
            "SANELESS_TEST_PYTHON": sys.executable,
            "SANELESS_TEST_CODE": _LIBSANE_PROBE,
            "SANELESS_TEST_STATUS": status,
        },
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=_CHILD_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, (
        f"child returncode {result.returncode}, stderr: {result.stderr!r}"
    )
    assert "sigpipe-default" in result.stdout, result.stdout
    assert "broken-pipe" in result.stdout, result.stdout


def test_the_listing_child_does_not_inherit_a_blocked_sigpipe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The listing child starts with SIGPIPE unblocked, and the caller keeps it.

    Args:
        monkeypatch: Points the launcher at the stand-in child.
        tmp_path: Holds the stand-in child and the mask it records.

    """
    record = tmp_path / "sigblk"
    child = tmp_path / "mask_recording_child.py"
    child.write_text(_MASK_RECORDING_CHILD.format(record=str(record)), encoding="utf-8")
    monkeypatch.setattr(listing, "_CHILD_FILE", child)

    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGPIPE})
    try:
        with pytest.raises(ListingNoAnswerError):
            listing.run_listing_child(ListingRequest(), configured_host="")
        still_blocked = signal.SIGPIPE in signal.pthread_sigmask(
            signal.SIG_BLOCK, set()
        )
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)

    child_mask = int(record.read_text(encoding="ascii"), 16)
    assert not child_mask & _SIGPIPE_BIT, f"child SigBlk {child_mask:#x}"
    assert still_blocked
