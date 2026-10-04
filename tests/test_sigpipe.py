"""
A dead peer never kills saneless with SIGPIPE.

libsane puts SIGPIPE back to its default action, behind Python's back, whenever
a read ends with an error status, the end of every feeder batch included, and a
write to a socket whose peer has gone would then end the process with no
traceback.  saneless makes no libsane call itself: every scan runs in a scan
child.  One libsane test confirms the reset still happens, in a real scan
child whose read fails, so the hazard these tests guard against is real.
Another runs a process that blocks SIGPIPE as saneless does, scans through a
real scan child whose read fails, and then writes to a closed peer; it must
get ``BrokenPipeError`` and must never have loaded python-sane.  A blocked
mask is inherited across fork and exec, so the listing and scan child tests
block SIGPIPE first, as a launching thread in saneless has it, and check that
the child starts with it unblocked.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import select
import signal
import subprocess
import sys
from typing import TYPE_CHECKING, Final

import pytest

from saneless.exceptions import ListingNoAnswerError, ScanError
from saneless.pipeline import _SPOOL_LABEL_A
from saneless.scanner import _scan_child, child_launch, listing, scan_child
from saneless.scanner.base import ScanSettings
from saneless.scanner.listing import ListingRequest
from saneless.scanner.scan_child import ScanChildSession
from saneless.scanner.scan_protocol import (
    ChildFailure,
    ControlOp,
    LogLine,
    PassDone,
    Ready,
    ScanCommand,
    encode_command,
    read_frame,
)
from saneless.spool import SpooledPageSink

if TYPE_CHECKING:
    from pathlib import Path

    from saneless.scanner.scan_protocol import Frame

_CHILD_TIMEOUT_SECONDS: Final = 30
_SIGPIPE_BIT: Final = 1 << (signal.SIGPIPE - 1)
_MAX_PIXELS: Final = 1 << 30
_FLATBED_SCAN: Final = ScanCommand(
    device="test:0",
    settings=dataclasses.asdict(
        ScanSettings(source="Flatbed", resolution=50, mode="Gray")
    ),
    log_level=logging.INFO,
)

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


# The real scan child, run as it is, except that each pass also records the
# child's ignored-signal mask, as libsane leaves it at the pass's end, in the file
# SCAN_TEST_SIGIGN names.  The variable has no SANELESS_ prefix, so the child
# environment's strip keeps it.
_SIGIGN_RECORDING_CHILD: Final = """\
import os
import signal
import sys
from pathlib import Path

from saneless.scanner import _scan_child as child

real_run_pass = child.scan_session.run_pass


def recording_run_pass(*args):
    try:
        return real_run_pass(*args)
    finally:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("SigIgn:"):
                    Path(os.environ["SCAN_TEST_SIGIGN"]).write_text(line.split()[1])


child.scan_session.run_pass = recording_run_pass
reply_fd = child.take_reply_fd()
child.prepare_process()
code = child.main(
    sys.stdin.buffer,
    reply_fd,
    child.ChildRuntime(arm_alarm=signal.alarm, exit_process=os._exit, grace_seconds=10.0),
    forward_logs=True,
)
child.flush_standard_streams()
os._exit(code)
"""


def _failing_read_config(tmp_path: Path, status: str) -> Path:
    """
    Write a SANE configuration whose test backend ends every read with ``status``.

    Returns:
        The configuration directory, for ``SANE_CONFIG_DIR``.

    """
    config = tmp_path / "sane.d"
    config.mkdir()
    (config / "dll.conf").write_text("test\n", encoding="ascii")
    (config / "test.conf").write_text(
        f'read-status-code "{status}"\n', encoding="ascii"
    )
    return config


@pytest.mark.sane_hardware
@pytest.mark.parametrize(
    "status",
    ["SANE_STATUS_NO_DOCS", "SANE_STATUS_CANCELLED"],
    ids=["NO_DOCS", "CANCELLED"],
)
def test_a_failed_libsane_read_resets_sigpipe_in_the_scan_child(
    status: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A read that fails in a real scan child leaves SIGPIPE no longer ignored.

    This is the hazard itself: if libsane stopped resetting SIGPIPE, the
    reason to keep it out of saneless would be gone, and this test says so.

    Args:
        status: The status the test backend ends its read with.
        tmp_path: Holds the SANE configuration, the stand-in and the mask.
        monkeypatch: Points the session at the recording child.

    """
    config = _failing_read_config(tmp_path, status)
    record = tmp_path / "sigign"
    child_file = tmp_path / "sigign_recording_child.py"
    child_file.write_text(_SIGIGN_RECORDING_CHILD, encoding="utf-8")
    monkeypatch.setattr(scan_child, "_CHILD_FILE", child_file)
    monkeypatch.setenv("SANE_CONFIG_DIR", str(config))
    monkeypatch.setenv("SCAN_TEST_SIGIGN", str(record))
    settings = ScanSettings(source="Flatbed", resolution=50, mode="Gray")
    sink = SpooledPageSink(tmp_path, _SPOOL_LABEL_A, 0)

    with ScanChildSession(lambda: scan_child.start_scan_child("")) as session:
        with pytest.raises(ScanError):
            session.scan_pass("test:0", settings, sink)
        killed = session.children_killed

    assert killed == 0
    ignored = int(record.read_text(encoding="ascii"), 16)
    assert not ignored & _SIGPIPE_BIT, f"child SigIgn {ignored:#x}"


def _wait_readable(fd: int) -> None:
    """
    Block until ``fd`` is readable, failing the test after a while.

    Raises:
        AssertionError: Nothing arrived in time.

    """
    readable, _, _ = select.select([fd], [], [], _CHILD_TIMEOUT_SECONDS)
    if not readable:
        msg = "the scan child sent nothing in time"
        raise AssertionError(msg)


def _frame(fd: int) -> Frame:
    """
    Read the scan child's next frame other than a log record.

    Returns:
        The frame.

    """
    while True:
        frame = read_frame(fd, lambda: _wait_readable(fd), max_pixels=_MAX_PIXELS)
        if not isinstance(frame, LogLine):
            return frame


@pytest.mark.sane_hardware
@pytest.mark.parametrize(
    "next_command",
    [ControlOp.EXIT, _FLATBED_SCAN],
    ids=["exit", "scan"],
)
def test_a_scan_child_whose_read_failed_survives_a_closed_reply_pipe(
    next_command: ControlOp | ScanCommand,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    After a failed read, a write to a closed reply pipe ends the child cleanly.

    libsane has put SIGPIPE back to its default action by the pass's end, so
    unless the child ignores it again, its next write to a saneless that has
    gone kills it with SIGPIPE before its own cleanup can run.  It must
    instead see the write fail and end with the status for a closed reply
    channel.  Its stdin stays open, so the end of commands cannot be what
    ends it.

    Args:
        next_command: The command whose first reply meets the closed pipe.
        tmp_path: Holds the SANE configuration.
        monkeypatch: Points SANE at the configuration.

    """
    config = _failing_read_config(tmp_path, "SANE_STATUS_NO_DOCS")
    monkeypatch.setenv("SANE_CONFIG_DIR", str(config))
    proc = child_launch.start_child(scan_child._CHILD_FILE, "")
    assert proc.stdin is not None
    assert proc.stdout is not None
    try:
        reply_fd = proc.stdout.fileno()
        assert _frame(reply_fd) == Ready()
        proc.stdin.write(encode_command(_FLATBED_SCAN))
        proc.stdin.flush()
        frame = _frame(reply_fd)
        while not isinstance(frame, ChildFailure | PassDone):
            frame = _frame(reply_fd)
        proc.stdout.close()
        proc.stdin.write(encode_command(next_command))
        proc.stdin.flush()
        returncode = proc.wait(_CHILD_TIMEOUT_SECONDS)
    finally:
        child_launch.kill_and_reap(proc)
        proc.stdin.close()
        proc.stdout.close()

    assert isinstance(frame, ChildFailure), frame
    assert not frame.fatal, frame
    assert returncode != -signal.SIGPIPE, "the scan child was killed by SIGPIPE"
    assert returncode == _scan_child._PARENT_GONE_STATUS


@pytest.mark.sane_hardware
@pytest.mark.parametrize(
    "status",
    ["SANE_STATUS_NO_DOCS", "SANE_STATUS_CANCELLED"],
    ids=["NO_DOCS", "CANCELLED"],
)
def test_a_scan_whose_read_failed_leaves_saneless_free_of_python_sane(
    status: str, tmp_path: Path
) -> None:
    """
    After a failed read in a scan child, saneless has no python-sane loaded.

    The read really fails in the child, the probe never loads python-sane
    itself, and its write to a dead peer, with SIGPIPE blocked as saneless
    blocks it, gets ``BrokenPipeError``.

    Args:
        status: The status the test backend ends its read with.
        tmp_path: Holds the SANE configuration and the spool.

    """
    config = _failing_read_config(tmp_path, status)
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
