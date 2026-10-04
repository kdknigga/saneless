"""
A failed libsane read cannot leave saneless's scan child hung.

Some SANE backends, ``test`` among them, read on a thread started by
``sane_start`` and stop it with an asynchronous ``pthread_cancel`` from
``sane_cancel``.  A thread cancelled so can die at any instruction, holding the
dynamic loader's lock, taken when the first thread exit loads ``libgcc_s``, or a
``malloc`` arena lock, and the process then hangs at exit, at its next
``dlopen`` or in ``sane_cancel``.  Every scan runs in saneless's scan child, so
such a hang costs only the child, which saneless kills; these tests pin the two
halves of the mitigation that keep the child from hanging at all.  The child's
start-up loads the unwinder before any thread can exit, and its pass code waits
for a failed read's threads to end before anything cancels the handle.  The
last test counts how many real children, each making a failed read, a SANE
restart (which loads the backends again) and an exit, saneless had to kill.

Child processes get their SANE configuration from a ``SANE_CONFIG_DIR`` of
their own: options set through python-sane in one process do not reach another.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final

import pytest

from saneless.exceptions import FeederEmptyError, SanelessError
from saneless.pipeline import _SPOOL_LABEL_A
from saneless.scanner import page_budget, scan_child
from saneless.scanner.base import ScanSettings
from saneless.scanner.scan_child import ScanChildSession
from saneless.spool import SpooledPageSink

if TYPE_CHECKING:
    from pathlib import Path

# A bound on one child, not a pause: a healthy child is done in about a second.
_CHILD_TIMEOUT_SECONDS: Final = 30

# Marks the point in the child's stderr after which the unwinder must not be
# loaded again.  Written with os.write, as the loader's own trace is, so the
# two cannot be reordered by a buffer.
_MARK_BEFORE: Final = "SANELESS-PROBE-BEFORE"
_MARK_AFTER: Final = "SANELESS-PROBE-AFTER"

# Makes the scan child's own start-up preparation, then lets one thread end
# through pthread_exit, which makes glibc fetch its unwinder: the loader trace
# shows that load between the two marks unless the preparation did it.  ctypes
# is imported only after the preparation, as in the child.
_UNWINDER_PROBE: Final = f"""\
import os

from saneless.scanner import _scan_child

_scan_child.prepare_process()

import ctypes

libc = ctypes.CDLL(None)
thread = ctypes.c_ulong()
os.write(2, b"\\n{_MARK_BEFORE}\\n")
if libc.pthread_create(ctypes.byref(thread), None, libc.pthread_exit, None):
    raise OSError("pthread_create failed")
libc.pthread_join(thread, None)
os.write(2, b"\\n{_MARK_AFTER}\\n")
os._exit(0)
"""

# Runs one pass of the scan child's own pass code through the real test
# backend, whose read is made to fail and slowed, with python-sane's start,
# snap and cancel watched.  The read is slowed so the backend's reader thread
# is certainly still alive when a cancel that does not wait for it goes out.
# The pass asks snap() for no cancel; the cancel it makes later goes through
# the watched cancel, which notes the reader threads still alive.
_CANCEL_PROBE: Final = """\
import json
import os
import threading

from saneless.scanner import _scan_child, scan_session

_scan_child.prepare_process()

import sane

from saneless.exceptions import FeederEmptyError
from saneless.scanner.base import ScanSettings

SOURCE = os.environ["SANELESS_TEST_SOURCE"]
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


class Outlet:
    def stage(self, stage, page):
        pass

    def configured(self, parameters, *, resolution, use_adf):
        pass

    def page(self, image, *, number, dpi):
        return True

    def reading(self, dev):
        pass

    def cancel_requested(self):
        return False


scan_session.sane = WatchedSane()
scan_session.start_library()
settings = ScanSettings(source=SOURCE, resolution=50, mode="Gray")
try:
    scan_session.run_pass("test:0", settings, Outlet())
except FeederEmptyError:
    report["outcome"] = "feeder-empty"
print(json.dumps(report), flush=True)
os._exit(0)
"""

# The test backend's configuration for every read here: it fails with "out of
# documents", flatbed and feeder alike.
_NO_DOCS_CONF: Final = 'read-status-code "SANE_STATUS_NO_DOCS"\n'

# Slows the read: the backend's reader waits 0.2 s before it writes the page,
# which at 50 dpi is one buffer, so the reader ends shortly after.
_SLOW_NO_DOCS_CONF: Final = (
    _NO_DOCS_CONF + "read-delay true\nread-delay-duration 200000\n"
)

# How many runs the kill-rate loop makes, how many at once, and how many scan
# children each run starts in turn, each with a session of its own.  Only a
# child's first failed read is a hazard: once one backend thread has ended,
# the unwinder is loaded and the next ones cannot take the loader's lock, so
# the loop counts fresh children, not passes.  Without the mitigation between
# one child in two hundred and one in thirty had to be killed, so twelve
# hundred children all ending on their own by chance is out of the question.
_LIFECYCLE_RUNS: Final = 300
_LIFECYCLE_PARALLEL: Final = 8
_CHILDREN_PER_RUN: Final = 4

# The deadlines the loop shortens, so a hung child is killed (and counted)
# quickly: the stage deadline, the cancel grace and the page budget's floor
# and ceiling, which make every page budget exactly this long.
_LOOP_STAGE_SECONDS: Final = 5.0
_LOOP_GRACE_SECONDS: Final = 2.0
_LOOP_PAGE_SECONDS: Final = 5.0

# The longest one child can take when it hangs at every step: start-up, the
# page and its grace, the restart and the exit, plus a margin.
_CHILD_BOUND_SECONDS: Final = int(
    3 * _LOOP_STAGE_SECONDS + _LOOP_PAGE_SECONDS + _LOOP_GRACE_SECONDS + 10
)

_FLATBED: Final = "Flatbed"
_FEEDER: Final = "Automatic Document Feeder"


def _sane_config(directory: Path, test_conf: str) -> Path:
    """
    Write a SANE configuration naming only the ``test`` backend.

    Args:
        directory: Where to write it; created here.
        test_conf: The ``test.conf`` contents.

    Returns:
        The directory, for ``SANE_CONFIG_DIR``.

    """
    directory.mkdir()
    (directory / "dll.conf").write_text("test\n", encoding="ascii")
    (directory / "test.conf").write_text(test_conf, encoding="ascii")
    return directory


def _run_child(
    code: str, extra_env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """
    Run ``code`` in a fresh, isolated interpreter and return the finished child.

    Args:
        code: The Python source the child runs.
        extra_env: Variables added to the child's environment.

    Returns:
        The finished child, with its output captured as text.

    """
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        env={**os.environ, **extra_env},
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=_CHILD_TIMEOUT_SECONDS,
    )


def test_the_scan_child_loads_the_unwinder_before_any_thread_exits() -> None:
    """
    After the scan child's start-up preparation, a thread's exit loads nothing.

    The loader's file trace (``LD_DEBUG=files``) names every library it
    opens.  The first thread exit in a process opens ``libgcc_s``, so the
    trace between the marks shows it unless the preparation already did it --
    and that open, made by a backend's reader thread, is what an asynchronous
    cancel can interrupt with the loader's lock held.
    """
    if sys.platform != "linux":
        pytest.skip("needs Linux")
    result = _run_child(_UNWINDER_PROBE, {"LD_DEBUG": "files"})

    assert result.returncode == 0, result.stderr[-2000:]
    if "file=" not in result.stderr:
        pytest.skip("this C library has no LD_DEBUG file trace")
    before, _, rest = result.stderr.partition(_MARK_BEFORE)
    between, found, _ = rest.partition(_MARK_AFTER)
    assert found, result.stderr[-2000:]
    assert "libgcc_s" in before
    assert "libgcc_s" not in between, between


@pytest.mark.sane_hardware
@pytest.mark.parametrize("source", [_FLATBED, _FEEDER], ids=["flatbed", "feeder"])
def test_no_cancel_reaches_a_reader_thread_that_is_still_running(
    source: str, tmp_path: Path
) -> None:
    """
    In the scan child's pass, every cancel after a failed read waits for the reader.

    The read is slowed so the reader thread outlives any cancel that does not
    wait, which makes the check deterministic for the one-sheet flatbed pass
    and the feeder pass alike.

    Args:
        source: The source the pass reads, choosing the acquisition path.
        tmp_path: Holds the SANE configuration.

    """
    config = _sane_config(tmp_path / "sane.d", _SLOW_NO_DOCS_CONF)
    result = _run_child(
        _CANCEL_PROBE,
        {"SANELESS_TEST_SOURCE": source, "SANE_CONFIG_DIR": str(config)},
    )

    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout)
    assert report["outcome"] == "feeder-empty", report
    # The check means something only if the read did start a thread.
    assert any(report["started"]), report
    assert report["alive_at_cancel"], report
    assert not any(report["alive_at_cancel"]), report


def _lifecycle_child(spool: Path) -> tuple[int, str | None]:
    """
    Run one scan child through a failed read, a SANE restart and an exit.

    Args:
        spool: The run's spool directory.

    Returns:
        How many children the session had to kill, and what else went wrong,
        if anything did.

    """
    sink = SpooledPageSink(spool, _SPOOL_LABEL_A, 0)
    settings = ScanSettings(source=_FLATBED, resolution=50, mode="Gray")
    session = ScanChildSession(lambda: scan_child.start_scan_child(""))
    problem: str | None = None
    try:
        try:
            session.scan_pass("test:0", settings, sink)
        except FeederEmptyError:
            pass
        else:
            problem = "the read did not fail"
        session.restart()
    except SanelessError as error:
        problem = f"{type(error).__name__}: {error}"
    finally:
        try:
            session.close()
        except SanelessError as error:
            problem = problem or f"{type(error).__name__}: {error}"
    return session.children_killed, problem


def _lifecycle_run(spool: Path) -> list[tuple[int, str | None]]:
    """
    Run ``_CHILDREN_PER_RUN`` scan children in turn.

    Args:
        spool: The run's own spool directory.

    Returns:
        Each child's kills and problem, in order.

    """
    spool.mkdir()
    return [_lifecycle_child(spool) for _ in range(_CHILDREN_PER_RUN)]


@pytest.mark.slow
@pytest.mark.sane_hardware
@pytest.mark.timeout(
    _LIFECYCLE_RUNS * _CHILDREN_PER_RUN * _CHILD_BOUND_SECONDS // _LIFECYCLE_PARALLEL
)
def test_no_scan_child_needs_killing_after_a_failed_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Twelve hundred real scan children whose read fails all end on their own.

    Three hundred runs, eight at a time, each start four children in turn.
    Each child is started through saneless's own launcher, fails a flatbed
    read with "out of documents", restarts SANE, which loads the backends
    again, and is told to exit.  A child that hangs anywhere misses a
    deadline and is killed, and the kill is counted.

    Args:
        monkeypatch: Shortens the deadlines and points SANE at the
            configuration.
        tmp_path: Holds the configuration and each run's spool.

    """
    config = _sane_config(tmp_path / "sane.d", _NO_DOCS_CONF)
    monkeypatch.setenv("SANE_CONFIG_DIR", str(config))
    monkeypatch.setattr(scan_child, "STAGE_DEADLINE_SECONDS", _LOOP_STAGE_SECONDS)
    monkeypatch.setattr(scan_child, "CANCEL_GRACE_SECONDS", _LOOP_GRACE_SECONDS)
    monkeypatch.setattr(page_budget, "_PAGE_TIMEOUT_FLOOR_SECONDS", _LOOP_PAGE_SECONDS)
    monkeypatch.setattr(
        page_budget, "_PAGE_TIMEOUT_CEILING_SECONDS", _LOOP_PAGE_SECONDS
    )

    spools = [tmp_path / f"run{run}" for run in range(_LIFECYCLE_RUNS)]
    with ThreadPoolExecutor(max_workers=_LIFECYCLE_PARALLEL) as pool:
        outcomes = list(pool.map(_lifecycle_run, spools))

    killed = sum(kills for children in outcomes for kills, _ in children)
    failures = [
        f"run {run + 1} child {child + 1}: killed {kills}, {problem}"
        for run, children in enumerate(outcomes)
        for child, (kills, problem) in enumerate(children)
        if kills or problem is not None
    ]
    assert killed == 0, (killed, failures)
    assert not failures, failures
