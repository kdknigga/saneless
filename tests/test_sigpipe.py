"""
A dead peer never kills saneless, or a scan child, with SIGPIPE.

libsane puts SIGPIPE back to its default action, behind Python's back, when it
ends a scan read: after a read that succeeds, and in the cancel after one that
fails, the end of every feeder batch included.  A write to a pipe or socket
whose peer has gone would then end the process with no traceback.  saneless
makes no libsane call itself: every scan runs in a scan child.  One libsane
test confirms the reset still happens, in a real scan child, so the hazard
these tests guard against is real.  Others close saneless's end of a real scan
child's reply pipe, during a pass and after one whose read failed: the child
ignores SIGPIPE again before every reply, so it must end with the status for
a closed reply channel, not die from the signal.  Another runs a process that
blocks SIGPIPE as saneless does, scans through a real scan child whose read
fails, and then writes to a closed peer; it must get ``BrokenPipeError`` and
must never have loaded python-sane.  A blocked mask is inherited across fork
and exec, so the listing and scan child tests block SIGPIPE first, as a
launching thread in saneless has it, and check that the child starts with it
unblocked.
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
    PageHeader,
    PassDone,
    Ready,
    ScanCommand,
    StageFrame,
    encode_command,
    read_frame,
    receive_page,
)
from saneless.spool import SpooledPageSink

if TYPE_CHECKING:
    from collections.abc import Callable
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


# The real scan child, run as it is, except that it records the child's
# ignored-signal mask, as libsane leaves it, once a sheet's read has returned or
# failed and once the device is cancelled at the pass's end, just before the
# close is reported, in the file SCAN_TEST_SIGIGN names.  The variable has no
# SANELESS_ prefix, so the child environment's strip keeps it.
_SIGIGN_RECORDING_CHILD: Final = """\
import os
from pathlib import Path

from saneless.scanner import _scan_child as child

real_snap_sheet = child.scan_session._snap_sheet
real_report_quietly = child.scan_session._report_quietly


def record(point):
    with open("/proc/self/status", encoding="ascii") as status:
        for line in status:
            if line.startswith("SigIgn:"):
                with Path(os.environ["SCAN_TEST_SIGIGN"]).open("a") as out:
                    out.write(f"{point} {line.split()[1]}\\n")


def recording_snap_sheet(*args):
    try:
        return real_snap_sheet(*args)
    finally:
        record("read")


def recording_report_quietly(outlet, stage):
    if stage is child.ScanStage.CLOSE:
        record("cancel")
    return real_report_quietly(outlet, stage)


child.scan_session._snap_sheet = recording_snap_sheet
child.scan_session._report_quietly = recording_report_quietly
child.run_as_main()
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
    ("status", "reset_at"),
    [
        ("SANE_STATUS_NO_DOCS", "cancel"),
        ("SANE_STATUS_CANCELLED", "cancel"),
        (None, "read"),
    ],
    ids=["NO_DOCS", "CANCELLED", "GOOD"],
)
def test_a_libsane_read_resets_sigpipe_in_the_scan_child(
    status: str | None,
    reset_at: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A read in a real scan child leaves SIGPIPE no longer ignored.

    This is the hazard itself.  A read that succeeds resets it as it returns;
    one that fails resets it in the cancel that ends its reader, just before
    the pass reports its close.  If libsane stopped resetting SIGPIPE, the
    reason to keep it out of saneless, and to ignore it again before each of
    the child's replies, would be gone, and this test says so.

    Args:
        status: The status the test backend ends its read with, or None for
            a read that succeeds.
        reset_at: Where the reset is first seen: once the read has returned,
            or once the device is cancelled.
        tmp_path: Holds the SANE configuration, the stand-in and the mask.
        monkeypatch: Points the session at the recording child.

    """
    config = (
        _plain_config(tmp_path)
        if status is None
        else _failing_read_config(tmp_path, status)
    )
    record = tmp_path / "sigign"
    child_file = tmp_path / "sigign_recording_child.py"
    child_file.write_text(_SIGIGN_RECORDING_CHILD, encoding="utf-8")
    monkeypatch.setattr(scan_child, "_CHILD_FILE", child_file)
    monkeypatch.setenv("SANE_CONFIG_DIR", str(config))
    monkeypatch.setenv("SCAN_TEST_SIGIGN", str(record))
    settings = ScanSettings(source="Flatbed", resolution=50, mode="Gray")
    sink = SpooledPageSink(tmp_path, _SPOOL_LABEL_A, 0)

    with ScanChildSession(lambda: scan_child.start_scan_child("")) as session:
        if status is None:
            session.scan_pass("test:0", settings, sink)
        else:
            with pytest.raises(ScanError):
                session.scan_pass("test:0", settings, sink)
        killed = session.children_killed

    assert killed == 0
    masks = dict(
        line.split() for line in record.read_text(encoding="ascii").splitlines()
    )
    ignored = int(masks[reset_at], 16)
    assert not ignored & _SIGPIPE_BIT, f"child SigIgn after the {reset_at} {ignored:#x}"


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


def _slow_failing_read_config(tmp_path: Path) -> Path:
    """
    Write a SANE configuration whose reads take a second and then fail.

    Returns:
        The configuration directory, for ``SANE_CONFIG_DIR``.

    """
    config = _failing_read_config(tmp_path, "SANE_STATUS_NO_DOCS")
    with (config / "test.conf").open("a", encoding="ascii") as conf:
        conf.write("read-delay true\nread-delay-duration 1000000\n")
    return config


def _plain_config(tmp_path: Path) -> Path:
    """
    Write a SANE configuration for the test backend as it comes.

    Returns:
        The configuration directory, for ``SANE_CONFIG_DIR``.

    """
    config = tmp_path / "sane.d"
    config.mkdir()
    (config / "dll.conf").write_text("test\n", encoding="ascii")
    return config


def _slow_good_read_config(tmp_path: Path) -> Path:
    """
    Write a SANE configuration whose reads take a second and then succeed.

    Returns:
        The configuration directory, for ``SANE_CONFIG_DIR``.

    """
    config = _plain_config(tmp_path)
    (config / "test.conf").write_text(
        "read-delay true\nread-delay-duration 1000000\n", encoding="ascii"
    )
    return config


def _close_during_the_read(proc: subprocess.Popen[bytes], reply_fd: int) -> None:
    """Close saneless's end of the reply pipe once the first read is under way."""
    assert proc.stdout is not None
    while _frame(reply_fd) != StageFrame(stage="read", page=1):
        pass
    proc.stdout.close()


def _close_after_the_first_page(proc: subprocess.Popen[bytes], reply_fd: int) -> None:
    """Close saneless's end of the reply pipe after page 1, then answer it."""
    assert proc.stdin is not None
    assert proc.stdout is not None
    frame = _frame(reply_fd)
    while not isinstance(frame, PageHeader):
        frame = _frame(reply_fd)
    receive_page(reply_fd, frame, lambda: _wait_readable(reply_fd))
    proc.stdout.close()
    proc.stdin.write(encode_command(ControlOp.SPOOLED))
    proc.stdin.flush()


@pytest.mark.sane_hardware
@pytest.mark.parametrize(
    ("source", "configure", "close"),
    [
        ("Flatbed", _slow_failing_read_config, _close_during_the_read),
        ("Automatic Document Feeder", _slow_good_read_config, _close_during_the_read),
        ("Automatic Document Feeder", _plain_config, _close_after_the_first_page),
    ],
    ids=["failed-read", "good-read", "after-a-page"],
)
def test_a_scan_child_survives_its_reply_pipe_closing_mid_pass(
    source: str,
    configure: Callable[[Path], Path],
    close: Callable[[subprocess.Popen[bytes], int], None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A reply pipe closed during a pass ends the child cleanly, not by SIGPIPE.

    libsane puts SIGPIPE back to its default action inside a pass, after a
    read that succeeds and in the cancel after one that fails, and the pass
    writes again before it ends.  Closed during a read that fails, the first
    write to meet the closed pipe is the cleanup's close report; closed
    during a read that succeeds, it is the page's header, right after the
    reset; closed after page 1 is answered, it is the next sheet's start
    report.  Each such write to a saneless that has gone must fail, so the
    child cancels and closes the device and ends with the status for a closed
    reply channel.  Its stdin stays open, so the end of commands cannot be
    what ends it.

    Args:
        source: The source the pass reads.
        configure: Writes the SANE configuration the pass runs under.
        close: Closes saneless's end of the reply pipe at its point in the
            pass.
        tmp_path: Holds the SANE configuration.
        monkeypatch: Points SANE at the configuration.

    """
    monkeypatch.setenv("SANE_CONFIG_DIR", str(configure(tmp_path)))
    command = ScanCommand(
        device="test:0",
        settings=dataclasses.asdict(
            ScanSettings(source=source, resolution=50, mode="Gray")
        ),
        log_level=logging.INFO,
    )
    proc = child_launch.start_child(scan_child._CHILD_FILE, "")
    assert proc.stdin is not None
    assert proc.stdout is not None
    try:
        reply_fd = proc.stdout.fileno()
        assert _frame(reply_fd) == Ready()
        proc.stdin.write(encode_command(command))
        proc.stdin.flush()
        close(proc, reply_fd)
        returncode = proc.wait(_CHILD_TIMEOUT_SECONDS)
    finally:
        child_launch.kill_and_reap(proc)
        proc.stdin.close()
        proc.stdout.close()

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
