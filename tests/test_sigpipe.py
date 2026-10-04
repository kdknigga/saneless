"""
A dead peer never kills saneless with SIGPIPE.

libsane puts SIGPIPE back to its default action, behind Python's back, whenever
a read ends with an error status, the end of every feeder batch included, and a
write to a socket whose peer has gone would then end the process with no
traceback.  saneless makes no libsane call itself: every scan runs in a scan
child.  The libsane test runs a process that blocks SIGPIPE as saneless does,
scans through a real scan child whose read fails, and then writes to a closed
peer; it must get ``BrokenPipeError`` and must never have loaded python-sane.
A blocked mask is inherited across fork and exec, so the listing and scan
child tests block SIGPIPE first, as a launching thread in saneless has it, and
check that the child starts with it unblocked.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING, Final

import pytest

from saneless.exceptions import ListingNoAnswerError, ScanError
from saneless.pipeline import _SPOOL_LABEL_A
from saneless.scanner import listing, scan_child
from saneless.scanner.base import ScanSettings
from saneless.scanner.listing import ListingRequest
from saneless.scanner.scan_child import ScanChildSession
from saneless.spool import SpooledPageSink

if TYPE_CHECKING:
    from pathlib import Path

_CHILD_TIMEOUT_SECONDS: Final = 30
_SIGPIPE_BIT: Final = 1 << (signal.SIGPIPE - 1)

# Blocks SIGPIPE as saneless does, runs one flatbed pass through a real scan
# child whose read ends with the parametrised error status, then writes to a
# socket whose peer is closed.  It reports that the read failed, how many
# children had to be killed, whether python-sane was ever imported in this
# process, and what the write did.
_LIBSANE_PROBE: Final = """\
import os
import socket
import sys

from saneless.sigpipe import block_sigpipe

block_sigpipe()

from saneless.exceptions import ScanError
from saneless.pipeline import _SPOOL_LABEL_A
from saneless.scanner import scan_child
from saneless.scanner.base import ScanSettings
from saneless.scanner.scan_child import ScanChildSession
from saneless.spool import SpooledPageSink
from saneless.spool import SpooledPageSink

sink = SpooledPageSink(os.environ["SANELESS_TEST_SPOOL"], _SPOOL_LABEL_A, 0)
settings = ScanSettings(source="Flatbed", resolution=50, mode="Gray")
session = ScanChildSession(lambda: scan_child.start_scan_child(""))
try:
    session.scan_pass("test:0", settings, sink)
except ScanError:
    print("read-failed", flush=True)
finally:
    session.close()
print(f"children-killed {session.children_killed}", flush=True)
if "sane" not in sys.modules:
    print("no-sane", flush=True)
a, b = socket.socketpair()
b.close()
try:
    a.sendall(b"x")
except BrokenPipeError:
    print("broken-pipe", flush=True)
finally:
    a.close()
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


# A stand-in scan child that records its own blocked-signal mask in the file
# named by the format field, then exits without a frame.  saneless sends a
# scan child nothing before it says ready, so it reads no command first.
_SCAN_MASK_RECORDING_CHILD: Final = """\
with open("/proc/self/status", encoding="ascii") as status:
    for line in status:
        if line.startswith("SigBlk:"):
            mask = line.split()[1]
            break
with open({record!r}, "w", encoding="ascii") as record:
    record.write(mask)
"""


@pytest.mark.sane_hardware
@pytest.mark.parametrize(
    "status",
    ["SANE_STATUS_NO_DOCS", "SANE_STATUS_CANCELLED"],
    ids=["NO_DOCS", "CANCELLED"],
)
def test_a_failed_libsane_read_cannot_let_a_dead_peer_kill_saneless(
    status: str, tmp_path: Path
) -> None:
    """
    A write to a closed peer after a failed read in a scan child raises.

    The read really fails in the child, the probe never loads python-sane
    itself, and its write to a dead peer gets ``BrokenPipeError``.

    Args:
        status: The status the test backend ends its read with.
        tmp_path: Holds the SANE configuration and the spool.

    """
    config = tmp_path / "sane.d"
    config.mkdir()
    (config / "dll.conf").write_text("test\n", encoding="ascii")
    (config / "test.conf").write_text(
        f'read-status-code "{status}"\n', encoding="ascii"
    )
    spool = tmp_path / "spool"
    spool.mkdir()
    result = subprocess.run(
        [sys.executable, "-I", "-c", _LIBSANE_PROBE],
        env={
            **os.environ,
            "SANE_CONFIG_DIR": str(config),
            "SANELESS_TEST_SPOOL": str(spool),
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
    lines = result.stdout.splitlines()
    assert "read-failed" in lines, result.stdout
    assert "children-killed 0" in lines, result.stdout
    assert "no-sane" in lines, result.stdout
    assert "broken-pipe" in lines, result.stdout


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


def test_the_scan_child_does_not_inherit_a_blocked_sigpipe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The scan child starts with SIGPIPE unblocked, and the caller keeps it.

    Args:
        monkeypatch: Points the launcher at the stand-in child.
        tmp_path: Holds the stand-in child and the mask it records.

    """
    record = tmp_path / "sigblk"
    child = tmp_path / "scan_mask_recording_child.py"
    child.write_text(
        _SCAN_MASK_RECORDING_CHILD.format(record=str(record)), encoding="utf-8"
    )
    monkeypatch.setattr(scan_child, "_CHILD_FILE", child)
    settings = ScanSettings(source="Flatbed", resolution=50, mode="Gray")
    sink = SpooledPageSink(tmp_path, _SPOOL_LABEL_A, 0)
    session = ScanChildSession(lambda: scan_child.start_scan_child(""))

    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGPIPE})
    try:
        with pytest.raises(ScanError):
            session.scan_pass("test:0", settings, sink)
        session.close()
        still_blocked = signal.SIGPIPE in signal.pthread_sigmask(
            signal.SIG_BLOCK, set()
        )
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)

    child_mask = int(record.read_text(encoding="ascii"), 16)
    assert not child_mask & _SIGPIPE_BIT, f"child SigBlk {child_mask:#x}"
    assert still_blocked
