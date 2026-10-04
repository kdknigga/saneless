"""
A failed libsane read cannot leave a process hung.

Some SANE backends, ``test`` among them, read on a thread started by
``sane_start`` and stop it with an asynchronous ``pthread_cancel`` from
``sane_cancel``.  A thread cancelled so can die at any instruction, holding the
dynamic loader's lock, taken when the first thread exit loads ``libgcc_s``, or a
``malloc`` arena lock, and the process then hangs at exit, at its next
``dlopen`` or in ``sane_cancel``.  saneless loads the unwinder at startup and
waits for a failed read's threads to end before anything cancels it.  Every scan
here runs in a child process, so a hang or a lock left held costs only the child.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from pathlib import Path

# A bound on one child, not a pause: a healthy child is done in about a second.
_CHILD_TIMEOUT_SECONDS: Final = 30

# Marks the point in the child's stderr after which the unwinder must not be
# loaded again.  Written with os.write, as the loader's own trace is, so the
# two cannot be reordered by a buffer.
_MARK_BEFORE: Final = "SANELESS-PROBE-BEFORE"
_MARK_AFTER: Final = "SANELESS-PROBE-AFTER"

# Started through saneless.main(), so the startup path is the shipped one.  A
# thread that ends through pthread_exit makes glibc fetch its unwinder: the
# loader trace shows that load between the two marks unless startup did it.
_UNWINDER_PROBE: Final = f"""\
import ctypes
import os

import saneless
import saneless.cli


def probe():
    libc = ctypes.CDLL(None)
    thread = ctypes.c_ulong()
    os.write(2, b"\\n{_MARK_BEFORE}\\n")
    if libc.pthread_create(ctypes.byref(thread), None, libc.pthread_exit, None):
        raise OSError("pthread_create failed")
    libc.pthread_join(thread, None)
    os.write(2, b"\\n{_MARK_AFTER}\\n")
    os._exit(0)


saneless.cli.cli = probe
saneless.main()
"""

# Runs one saneless scan through the real test backend, whose read is made to
# fail, with python-sane's start, snap and cancel watched.  The read is slowed
# so the backend's reader thread is certainly still alive when a cancel that
# does not wait for it goes out.  A failed snap() cancels by itself unless it
# is passed no_cancel, so the watched snap() asks for no cancel and makes the
# one python-sane would have made through the watched cancel instead.
_CANCEL_PROBE: Final = """\
import json
import os
import threading

import sane

import saneless
import saneless.cli
from saneless.exceptions import FeederEmptyError
from saneless.pipeline import _SPOOL_LABEL_A
from saneless.scanner import sane_backend
from saneless.scanner.base import ScanSettings
from saneless.spool import SpooledPageSink

SOURCE = os.environ["SANELESS_TEST_SOURCE"]
SPOOL = os.environ["SANELESS_TEST_SPOOL"]
report = {"started": [], "alive_at_cancel": [], "outcome": None}


def native_ids():
    return {int(name) for name in os.listdir("/proc/self/task")}


def python_ids():
    return {thread.native_id for thread in threading.enumerate()}


def watched(dev):
    real_start, real_snap, real_cancel = dev.start, dev.snap, dev.cancel
    spawned = set()

    def start():
        before = native_ids()
        real_start()
        spawned.clear()
        spawned.update(native_ids() - before - python_ids())
        report["started"].append(sorted(spawned))

    def cancel():
        report["alive_at_cancel"].append(sorted(spawned & native_ids()))
        real_cancel()

    def snap(no_cancel=False, progress=None):
        try:
            image = real_snap(True, progress)
        except Exception:
            if not no_cancel:
                cancel()
            raise
        if not no_cancel:
            cancel()
        return image

    dev.start = start
    dev.snap = snap
    dev.cancel = cancel
    return dev


class WatchedSane:
    def __getattr__(self, name):
        return getattr(sane, name)

    def open(self, name):
        return watched(sane.open(name))


def probe():
    sane_backend.sane = WatchedSane()
    backend = sane_backend.SaneBackend()
    setup = sane.open("test:0")
    setup.test_picture = "Solid black"
    setup.read_delay = True
    setup.read_delay_duration = 200000
    setup.read_return_value = "SANE_STATUS_NO_DOCS"
    setup.close()
    sink = SpooledPageSink(SPOOL, _SPOOL_LABEL_A, 0)
    settings = ScanSettings(source=SOURCE, resolution=50, mode="Gray")
    try:
        backend.scan_pages("test:0", settings, sink)
    except FeederEmptyError:
        report["outcome"] = "feeder-empty"
    print(json.dumps(report), flush=True)
    os._exit(0)


saneless.cli.cli = probe
saneless.main()
"""

# One whole saneless run: a flatbed scan whose read fails, a later dlopen, as
# a long-running server makes when it loads a module lazily, and an ordinary
# interpreter exit.  Each stage announces itself, so a hang is placed.  The
# flatbed path is the one looped here because a cancel that reaches a running
# reader hangs it most often; the deterministic test above covers both paths.
_LIFECYCLE_PROBE: Final = """\
import ctypes
import os

import sane

import saneless
import saneless.cli
from saneless.exceptions import FeederEmptyError
from saneless.pipeline import _SPOOL_LABEL_A
from saneless.scanner import sane_backend
from saneless.scanner.base import ScanSettings
from saneless.spool import SpooledPageSink

SPOOL = os.environ["SANELESS_TEST_SPOOL"]


def probe():
    backend = sane_backend.SaneBackend()
    setup = sane.open("test:0")
    setup.test_picture = "Solid black"
    setup.read_delay = False
    setup.read_return_value = "SANE_STATUS_NO_DOCS"
    setup.close()
    sink = SpooledPageSink(SPOOL, _SPOOL_LABEL_A, 0)
    settings = ScanSettings(source="Flatbed", resolution=50, mode="Gray")
    try:
        backend.scan_pages("test:0", settings, sink)
    except FeederEmptyError:
        pass
    print("scanned", flush=True)
    ctypes.CDLL("libc.so.6")
    print("dlopened", flush=True)


saneless.cli.cli = probe
saneless.main()
"""

# How many whole runs the hang loop makes, and how many at once.  A cancel
# that reaches a running reader hangs about one flatbed run in twenty, so a
# hundred runs all finishing by chance is under one in a hundred.  Four at a
# time keeps the loop to about fifteen seconds.
_LIFECYCLE_RUNS: Final = 100
_LIFECYCLE_PARALLEL: Final = 4

# A healthy run takes about half a second; a hung one never finishes.
_LIFECYCLE_TIMEOUT_SECONDS: Final = 10

_FLATBED: Final = "Flatbed"
_FEEDER: Final = "Automatic Document Feeder"


def _run_child(
    code: str, extra_env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """
    Run ``code`` in a fresh interpreter and return the finished child.

    Args:
        code: The Python source the child runs.
        extra_env: Variables added to the child's environment.
        timeout: Seconds before the child counts as hung.

    Returns:
        The finished child, with its output captured as text.

    """
    return subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, **extra_env},
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=timeout,
    )


def test_startup_loads_the_thread_unwinder_before_any_thread_exits() -> None:
    """
    After saneless.main()'s startup, a thread's exit loads nothing.

    The loader's file trace (``LD_DEBUG=files``) names every library it
    opens.  The first thread exit in a process opens ``libgcc_s``, so the
    trace between the marks shows it unless startup already did it -- and
    that open, made by a backend's reader thread, is what an asynchronous
    cancel can interrupt with the loader's lock held.
    """
    if sys.platform != "linux":
        pytest.skip("needs Linux")
    result = _run_child(
        _UNWINDER_PROBE, {"LD_DEBUG": "files"}, timeout=_CHILD_TIMEOUT_SECONDS
    )

    assert result.returncode == 0, result.stderr[-2000:]
    if "file=" not in result.stderr:
        pytest.skip("this C library has no LD_DEBUG file trace")
    before, _, rest = result.stderr.partition(_MARK_BEFORE)
    between, found, _ = rest.partition(_MARK_AFTER)
    assert found, result.stderr[-2000:]
    assert "libgcc_s" in before
    assert "libgcc_s" not in between, between


@pytest.mark.sane_hardware
@pytest.mark.usefixtures("sane_test_backend_config")
@pytest.mark.parametrize("source", [_FLATBED, _FEEDER], ids=["flatbed", "feeder"])
def test_no_cancel_reaches_a_reader_thread_that_is_still_running(
    source: str, tmp_path: Path
) -> None:
    """
    Every cancel after a failed read waits for the backend's reader to end.

    The read is slowed so the reader thread outlives any cancel that does not
    wait, which makes the check deterministic for both the cancel
    python-sane's ``snap()`` makes (flatbed) and the one the feeder
    iterator's finaliser makes (feeder).

    Args:
        source: The source the scan reads, choosing the acquisition path.
        tmp_path: Holds the scan's spool.

    """
    result = _run_child(
        _CANCEL_PROBE,
        {"SANELESS_TEST_SOURCE": source, "SANELESS_TEST_SPOOL": str(tmp_path)},
        timeout=_CHILD_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout)
    assert report["outcome"] == "feeder-empty", report
    # The check means something only if the read did start a thread.
    assert any(report["started"]), report
    assert report["alive_at_cancel"], report
    assert not any(report["alive_at_cancel"]), report


def _lifecycle_run(spool: Path) -> str | None:
    """
    Make one whole run, and say where it hung, if it did.

    Args:
        spool: The run's own spool directory.

    Returns:
        ``None`` if the run finished cleanly, else what went wrong.

    """
    spool.mkdir()
    try:
        result = _run_child(
            _LIFECYCLE_PROBE,
            {"SANELESS_TEST_SPOOL": str(spool)},
            timeout=_LIFECYCLE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as expired:
        output = expired.stdout or b""
        text = output.decode() if isinstance(output, bytes) else output
        if "dlopened" in text:
            return "hung at exit"
        if "scanned" in text:
            return "hung in a later dlopen"
        return "hung inside the scan"
    if result.returncode != 0 or result.stdout.split() != ["scanned", "dlopened"]:
        return f"failed: {result.returncode} {result.stdout!r} {result.stderr[-500:]!r}"
    return None


@pytest.mark.slow
@pytest.mark.sane_hardware
@pytest.mark.usefixtures("sane_test_backend_config")
@pytest.mark.timeout(
    _LIFECYCLE_RUNS * (_LIFECYCLE_TIMEOUT_SECONDS + 5) // _LIFECYCLE_PARALLEL
)
def test_a_failed_read_never_leaves_saneless_hung(tmp_path: Path) -> None:
    """
    A hundred whole runs that end their scan with a failed read all finish.

    Each run scans, makes a later ``dlopen`` and exits normally; a run that
    does not finish in time hung, and is reported with the stage it stopped
    in.

    Args:
        tmp_path: Holds each run's spool.

    """
    spools = [tmp_path / f"run{run}" for run in range(_LIFECYCLE_RUNS)]
    with ThreadPoolExecutor(max_workers=_LIFECYCLE_PARALLEL) as pool:
        outcomes = list(pool.map(_lifecycle_run, spools))

    failures = [
        f"run {run + 1}: {outcome}"
        for run, outcome in enumerate(outcomes)
        if outcome is not None
    ]
    assert not failures, failures
