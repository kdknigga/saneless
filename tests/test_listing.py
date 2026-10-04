"""
Tests for ``saneless.scanner.listing``, the launcher every scanner listing uses.

The launcher hands a short-lived child interpreter one JSON request line and
waits under a hard deadline; the child is dead and reaped before the launcher
returns or raises.  A signal death is a crashed listing, an overrun a
timed-out one, and a child that cannot start or answers off-schema gave no
answer: three ``ScanError`` subclasses, each logging one WARNING with nothing
host-derived.  The child's environment drops every ``SANELESS_*`` variable.

No test here touches libsane: each runs a stand-in child script written into
``tmp_path`` exactly as the real child is run.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import NoReturn

import pytest

from saneless import exceptions
from saneless.exceptions import (
    ListingAbortedError,
    ListingCrashedError,
    ListingNoAnswerError,
    ListingTimedOutError,
    ScanError,
    ScanInterrupted,
)
from saneless.scanner import listing
from saneless.scanner.listing import (
    ChildError,
    ListingReply,
    ListingRequest,
    child_environment,
    run_listing_child,
)
from tests.conftest import poll_until

_LOGGER = "saneless.scanner.listing"
_NET_ID = "net:scanbox.lan:test:0"
_TEST_DEVICE = ("test:0", "Noname", "frontend-tester", "virtual device")
_NO_ANSWER = "The scanner library returned no answer while listing scanners"

# Generous on purpose: a passing timeout test finishes in well under a second.
# This bound only turns "the launcher never gave up" into a failure instead of
# a hung session.
_ELAPSED_CEILING_SECONDS = 5.0

# The real method, kept before any test swaps it for a spy.
_REAL_COMMUNICATE = subprocess.Popen.communicate

# Every stand-in below uses the standard library only, because the launcher
# runs it in isolated mode.

_REPLY_CHILD = """\
import json
import sys

sys.stdin.readline()
device = ["test:0", "Noname", "frontend-tester", "virtual device"]
sys.stdout.write(json.dumps({"devices": [device]}) + "\\n")
"""

_ECHO_REQUEST_CHILD = """\
import json
import sys

line = sys.stdin.readline().rstrip("\\n")
sys.stdout.write(json.dumps({"devices": [[line, "v", "m", "t"]]}) + "\\n")
"""

# Reads everything sent on stdin, up to the end of the stream, then takes long
# enough over its answer that the launcher waits more than once before it
# comes, and echoes all of it back.
_SLOW_ECHO_CHILD = """\
import json
import sys
import time

received = sys.stdin.read()
time.sleep(0.35)
sys.stdout.write(json.dumps({"devices": [[received, "v", "m", "t"]]}) + "\\n")
"""

_NOISY_CHILD = """\
import json
import sys

sys.stdin.readline()
sys.stdout.write("noise\\n")
device = ["test:0", "Noname", "frontend-tester", "virtual device"]
sys.stdout.write(json.dumps({"devices": [device]}) + "\\n")
sys.stdout.write("\\n")
"""

_SEGFAULT_CHILD = """\
import os
import resource
import signal
from pathlib import Path

Path(os.environ["LISTING_TEST_PIDFILE"]).write_text(str(os.getpid()))
# No core file: this crash is deliberate.
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.kill(os.getpid(), signal.SIGSEGV)
"""

_SLEEPER_CHILD = """\
import os
import signal
from pathlib import Path

Path(os.environ["LISTING_TEST_PIDFILE"]).write_text(str(os.getpid()))
signal.pause()
"""

# Starts a process of its own, as a backend that runs a helper program does,
# records its PID, and then both sleep.
_FORKING_SLEEPER_CHILD = """\
import os
import signal
from pathlib import Path

helper = os.fork()
if helper == 0:
    signal.pause()
Path(os.environ["LISTING_TEST_PIDFILE"]).write_text(str(helper))
signal.pause()
"""

_ALARM_CHILD = """\
import os
import signal

os.kill(os.getpid(), signal.SIGALRM)
"""

_EXIT_THREE_CHILD = """\
import sys

sys.exit(3)
"""

_SILENT_CHILD = """\
import sys

sys.stdin.readline()
"""

_NOISE_ONLY_CHILD = """\
import sys

sys.stdin.readline()
sys.stdout.write("starting up\\nnothing to report\\n")
"""

_ENVIRONMENT_CHILD = """\
import json
import os
import sys

sys.stdin.readline()
keys = json.dumps(sorted(k for k in os.environ if k.startswith("SANELESS_")))
sys.stdout.write(json.dumps({"devices": [[keys, "v", "m", "t"]]}) + "\\n")
"""

_FLAGS_CHILD = """\
import json
import sys

sys.stdin.readline()
isolated = str(sys.flags.isolated)
sys.stdout.write(json.dumps({"devices": [[isolated, "v", "m", "t"]]}) + "\\n")
"""

_PROCESS_GROUP_CHILD = """\
import json
import os
import sys

sys.stdin.readline()
group = str(os.getpgid(0))
sys.stdout.write(json.dumps({"devices": [[group, "v", "m", "t"]]}) + "\\n")
"""

# The real child script, kept before any test points the launcher elsewhere.
_REAL_CHILD_FILE = listing._CHILD_FILE

# Runs the real child script as a program, over a stand-in ``sane`` module,
# so what the real entry point does with the process's own file descriptors
# is what is tested.  The stand-in's ``init`` writes to fd 1 the two ways C
# code does: through C stdio, which holds the text in its buffer until the
# process exits, and straight to the descriptor, with no newline.
_PRINTING_BACKEND_SETUP = """\
import ctypes
import os
import runpy
import sys
import types

libc = ctypes.CDLL(None)


def init():
    libc.puts(b"backend chatter through C stdio")
    os.write(1, b"backend chatter with no newline")
    return (1, 0, 0)


def get_devices():
    return [("test:0", "Noname", "frontend-tester", "virtual device")]


sane = types.ModuleType("sane")
sane.init = init
sane.get_devices = get_devices
sys.modules["sane"] = sane
"""

_RUN_REAL_CHILD = """\
runpy.run_path(os.environ["LISTING_TEST_REAL_CHILD"], run_name="__main__")
"""

_PRINTING_BACKEND_CHILD = _PRINTING_BACKEND_SETUP + _RUN_REAL_CHILD

# Runs the real child script over a stand-in ``sane`` module whose ``init``
# leaves behind a teardown step that crashes, as a library destructor or an
# ``atexit`` hook registered from C can, after the reply is already written.
_TEARDOWN_CRASH_CHILD = """\
import atexit
import os
import resource
import runpy
import signal
import sys
import types


def crash():
    # No core file: this crash is deliberate.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.kill(os.getpid(), signal.SIGSEGV)


def init():
    atexit.register(crash)
    return (1, 0, 0)


def get_devices():
    return [("test:0", "Noname", "frontend-tester", "virtual device")]


sane = types.ModuleType("sane")
sane.init = init
sane.get_devices = get_devices
sys.modules["sane"] = sane
runpy.run_path(os.environ["LISTING_TEST_REAL_CHILD"], run_name="__main__")
"""

# Runs the real child script as a program whose stderr is closed, over the
# printing stand-in ``sane`` module above.  Python started with fd 2 closed
# sets ``sys.stderr`` to None, and one whose fd 2 was closed later keeps a
# stream on a dead descriptor; the parametrised line picks which.  Either way
# fd 2 is the lowest free descriptor, so a plain ``dup`` of fd 1 lands there.
# The close comes after the setup's imports: ``import ctypes`` keeps a
# descriptor of its own open, which would otherwise take the free fd 2.
_CLOSED_STDERR_CHILD = (
    _PRINTING_BACKEND_SETUP + "os.close(2)\n{stderr_line}\n" + _RUN_REAL_CHILD
)

# Runs the real child script with its stderr closed, over a stand-in ``sane``
# module whose listing starts a helper program, as a backend that execs one
# does.  The helper exits 0 only when it starts with a descriptor on fd 2, and
# its exit status comes back as the listed device's vendor.
_HELPER_PROGRAM_CHILD = (
    """\
import os
import runpy
import subprocess
import sys
import types


def get_devices():
    helper = [sys.executable, "-I", "-c", "import os; os.fstat(2)"]
    status = subprocess.run(helper, check=False).returncode
    return [("test:0", str(status), "frontend-tester", "virtual device")]


sane = types.ModuleType("sane")
sane.init = lambda: (1, 0, 0)
sane.get_devices = get_devices
sys.modules["sane"] = sane
os.close(2)
sys.stderr = None
"""
    + _RUN_REAL_CHILD
)


def _use_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str) -> Path:
    """
    Write a stand-in child into ``tmp_path`` and point the launcher at it.

    Args:
        monkeypatch: Undoes the redirection after the test.
        tmp_path: Where the script is written.
        source: The script's text.

    Returns:
        The script's path.

    """
    child = tmp_path / "stand_in_child.py"
    child.write_text(source, encoding="utf-8")
    monkeypatch.setattr(listing, "_CHILD_FILE", child)
    return child


def _pidfile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """
    Name a file a stand-in child writes its PID into.

    The variable has no ``SANELESS_`` prefix, so it survives the child
    environment's strip.

    Args:
        monkeypatch: Sets the variable for this test only.
        tmp_path: Where the file goes.

    Returns:
        The file's path; it exists only once the child has written it.

    """
    path = tmp_path / "child.pid"
    monkeypatch.setenv("LISTING_TEST_PIDFILE", str(path))
    return path


def _assert_reaped(pid: int) -> None:
    """
    Assert a child process is gone and already waited for.

    ``waitpid`` raising ``ChildProcessError`` means this process has no
    unreaped child with that PID: not a running one, and not a zombie.

    Args:
        pid: The child's process id.

    """
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    assert not Path(f"/proc/{pid}").exists()


def _is_dead(pid: int) -> bool:
    """
    Tell whether a process that is not this process's child has ended.

    Such a process is reaped by whichever process adopted it, so it can
    still show briefly as a zombie after it died.

    Args:
        pid: The process id.

    Returns:
        Whether the process is gone or a zombie.

    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError, ProcessLookupError:
        # Reaped before the open, or between the open and the read.
        return True
    return stat.rpartition(")")[2].split()[0] in {"Z", "X"}


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """
    Return the launcher's WARNING records.

    Args:
        caplog: The test's log capture.

    Returns:
        Every WARNING the launcher's logger emitted.

    """
    return [
        record
        for record in caplog.records
        if record.name == _LOGGER and record.levelno == logging.WARNING
    ]


def _spy_on_communicate(
    monkeypatch: pytest.MonkeyPatch, interruption: BaseException | None = None
) -> list[subprocess.Popen[bytes]]:
    """
    Record every process whose ``communicate`` the launcher calls.

    ``Popen.communicate`` is replaced on the class for this test only, so the
    launcher still starts, kills and waits for a real child through the real
    ``Popen``.  With an interruption, the wait is interrupted while the child
    is still running, which is what a Ctrl-C or a server stop does to it.

    Args:
        monkeypatch: Undoes the replacement after the test.
        interruption: Raised instead of waiting, when given.

    Returns:
        The list each process is appended to, in the order they were waited on.

    """
    started: list[subprocess.Popen[bytes]] = []

    def communicate(
        self: subprocess.Popen[bytes],
        data: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        started.append(self)
        if interruption is not None:
            raise interruption
        return _REAL_COMMUNICATE(self, data, timeout)

    monkeypatch.setattr(subprocess.Popen, "communicate", communicate)
    return started


class TestHappyPath:
    """A child that answers is decoded into a reply."""

    def test_a_reply_is_decoded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One device on stdout becomes one device tuple in the reply."""
        _use_child(monkeypatch, tmp_path, _REPLY_CHILD)

        reply = run_listing_child(ListingRequest(), configured_host="")

        assert reply == ListingReply(devices=(_TEST_DEVICE,))

    def test_the_request_line_carries_the_device_and_the_alarm(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The child is sent one JSON line naming the device and its own alarm.

        The alarm is the deadline rounded up plus a margin, so an orphaned
        child cannot outlive its parent's bound by long.  The device id goes
        on stdin, never in argv.
        """
        _use_child(monkeypatch, tmp_path, _ECHO_REQUEST_CHILD)

        reply = run_listing_child(ListingRequest(open=_NET_ID), configured_host="")

        line = reply.devices[0][0]
        assert line == '{"open": "net:scanbox.lan:test:0", "alarm": 35}'
        assert json.loads(line) == {"open": _NET_ID, "alarm": 35}

    def test_only_the_last_non_empty_line_is_the_reply(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Output before the reply, and a blank line after it, are ignored."""
        _use_child(monkeypatch, tmp_path, _NOISY_CHILD)

        reply = run_listing_child(ListingRequest(), configured_host="")

        assert reply.devices == (_TEST_DEVICE,)

    def test_a_backend_writing_to_stdout_does_not_touch_the_reply(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        """
        What the scanner library prints goes to the log, not into the reply.

        The real child keeps the reply's pipe to itself and points fd 1 at
        stderr before python-sane is loaded.  Otherwise C stdio's buffer,
        flushed when the process exits, lands after the reply line, and a
        write with no newline runs into it.
        """
        monkeypatch.setenv("LISTING_TEST_REAL_CHILD", str(_REAL_CHILD_FILE))
        _use_child(monkeypatch, tmp_path, _PRINTING_BACKEND_CHILD)

        reply = run_listing_child(ListingRequest(), configured_host="")

        assert reply == ListingReply(devices=(_TEST_DEVICE,))
        assert "backend chatter with no newline" in capfd.readouterr().err

    @pytest.mark.parametrize(
        "stderr_line",
        ["sys.stderr = None", "pass"],
        ids=["no-stderr-stream", "stale-stderr-stream"],
    )
    def test_a_closed_stderr_does_not_lose_the_reply(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stderr_line: str
    ) -> None:
        """
        A child with no stderr still answers, and keeps its reply's pipe.

        The child inherits saneless's stderr, which is closed when saneless
        was started with ``2>&-`` or by a supervisor that closes the
        standard descriptors.  What the scanner library prints is then
        discarded rather than written into the reply.
        """
        monkeypatch.setenv("LISTING_TEST_REAL_CHILD", str(_REAL_CHILD_FILE))
        source = _CLOSED_STDERR_CHILD.format(stderr_line=stderr_line)
        _use_child(monkeypatch, tmp_path, source)

        reply = run_listing_child(ListingRequest(), configured_host="")

        assert reply == ListingReply(devices=(_TEST_DEVICE,))

    def test_a_closed_stderr_is_reopened_for_programs_a_backend_starts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A helper program a backend starts gets the child's replacement fd 2.

        Left close-on-exec, the ``/dev/null`` the child opens on a closed
        fd 2 would vanish in the helper, whose own first ``open`` would then
        take fd 2 and receive whatever it writes to stderr.
        """
        monkeypatch.setenv("LISTING_TEST_REAL_CHILD", str(_REAL_CHILD_FILE))
        _use_child(monkeypatch, tmp_path, _HELPER_PROGRAM_CHILD)

        reply = run_listing_child(ListingRequest(), configured_host="")

        assert reply == ListingReply(
            devices=(("test:0", "0", "frontend-tester", "virtual device"),)
        )

    def test_a_crash_after_the_reply_is_written_does_not_lose_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The child ends without teardown once its reply is on the pipe.

        Library destructors and exit hooks would otherwise run after the
        reply, and a crash or a hang in them would turn a listing that
        worked into a crashed or timed-out one.
        """
        monkeypatch.setenv("LISTING_TEST_REAL_CHILD", str(_REAL_CHILD_FILE))
        _use_child(monkeypatch, tmp_path, _TEARDOWN_CRASH_CHILD)

        reply = run_listing_child(ListingRequest(), configured_host="")

        assert reply == ListingReply(devices=(_TEST_DEVICE,))


class TestCrash:
    """A child that dies from a signal is a crashed listing."""

    def test_a_segfault_is_reported_by_signal_name_and_reaped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        SIGSEGV raises the crash error, logs one WARNING and leaves no child.

        The log line names the signal and nothing host-derived: neither the
        device id nor any part of it.
        """
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        pidfile = _pidfile(monkeypatch, tmp_path)
        _use_child(monkeypatch, tmp_path, _SEGFAULT_CHILD)

        with pytest.raises(ListingCrashedError) as crashed:
            run_listing_child(ListingRequest(open=_NET_ID), configured_host="")

        assert isinstance(crashed.value, ScanError)
        assert "SIGSEGV" in str(crashed.value)
        records = _warnings(caplog)
        assert len(records) == 1
        message = records[0].getMessage()
        assert "SIGSEGV" in message
        assert "net:" not in message
        assert "scanbox" not in message
        _assert_reaped(int(pidfile.read_text(encoding="utf-8")))

    def test_a_sigalrm_death_is_a_timeout_not_a_crash(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The child's own alarm firing means it ran out of time."""
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        _use_child(monkeypatch, tmp_path, _ALARM_CHILD)

        with pytest.raises(ListingTimedOutError):
            run_listing_child(ListingRequest(), configured_host="")

        assert len(_warnings(caplog)) == 1


class TestDeadline:
    """A child that outlives the deadline is killed and reaped."""

    def test_an_overrun_is_stopped_at_the_patched_deadline_and_reaped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A sleeping child raises the timeout error, and is gone when it does.

        The deadline is patched after the module was imported, so this also
        proves the launcher reads it at call time.
        """
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        monkeypatch.setattr(listing, "LISTING_DEADLINE_SECONDS", 0.2)
        started = _spy_on_communicate(monkeypatch)
        _use_child(monkeypatch, tmp_path, _SLEEPER_CHILD)
        _pidfile(monkeypatch, tmp_path)

        began = time.monotonic()
        with pytest.raises(ListingTimedOutError) as timed_out:
            run_listing_child(ListingRequest(open=_NET_ID), configured_host="")
        elapsed = time.monotonic() - began

        assert isinstance(timed_out.value, ScanError)
        assert "in time" in str(timed_out.value)
        assert elapsed < _ELAPSED_CEILING_SECONDS
        records = _warnings(caplog)
        assert len(records) == 1
        assert "net:" not in records[0].getMessage()
        # The wait is made in slices, every one of them on the same child.
        assert len(set(started)) == 1
        assert started[0].returncode is not None
        _assert_reaped(started[0].pid)

    def test_an_overrun_also_stops_what_the_child_started(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """
        A process the child started dies with it, rather than outliving it.

        The child leads a process group of its own, and the whole group is
        killed, so a helper program still holding a device or the reply's
        pipe does not keep running after the listing was stopped.
        """
        monkeypatch.setattr(listing, "LISTING_DEADLINE_SECONDS", 1.0)
        _use_child(monkeypatch, tmp_path, _FORKING_SLEEPER_CHILD)
        pidfile = _pidfile(monkeypatch, tmp_path)

        with pytest.raises(ListingTimedOutError):
            run_listing_child(ListingRequest(), configured_host="")

        helper = int(pidfile.read_text())
        try:
            assert poll_until(lambda: _is_dead(helper), _ELAPSED_CEILING_SECONDS)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(helper, signal.SIGKILL)


class TestNoAnswer:
    """A child that exits without a usable reply is a listing with no answer."""

    @pytest.mark.parametrize(
        ("source", "logged"),
        [
            pytest.param(
                _EXIT_THREE_CHILD,
                "exited with status 3 and no answer",
                id="positive-exit-status",
            ),
            pytest.param(_SILENT_CHILD, "wrote 0 bytes", id="exit-zero-empty-stdout"),
            pytest.param(
                _NOISE_ONLY_CHILD, "wrote 30 bytes", id="never-writes-a-reply"
            ),
        ],
    )
    def test_no_reply_is_no_answer(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        source: str,
        logged: str,
    ) -> None:
        """
        Not a crash or a timeout: the child did not die and did not overrun.

        One WARNING says which kind of no answer it was, through the exit
        status or the size of what the child wrote, never its text.
        """
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        _use_child(monkeypatch, tmp_path, source)

        with pytest.raises(ScanError) as failed:
            run_listing_child(ListingRequest(), configured_host="")

        assert type(failed.value) is ListingNoAnswerError
        assert str(failed.value) == _NO_ANSWER
        records = _warnings(caplog)
        assert len(records) == 1
        assert logged in records[0].getMessage()
        assert "starting up" not in records[0].getMessage()

    def test_a_missing_child_file_is_no_answer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The interpreter's own non-zero exit is reported the same way."""
        monkeypatch.setattr(listing, "_CHILD_FILE", tmp_path / "does_not_exist.py")

        with pytest.raises(ScanError) as failed:
            run_listing_child(ListingRequest(), configured_host="")

        assert type(failed.value) is ListingNoAnswerError
        assert str(failed.value) == _NO_ANSWER

    def test_a_child_that_cannot_be_started_is_no_answer(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        A fork refused under a process or memory limit is not an ``OSError``.

        Every caller catches ``ScanError``, so an ``OSError`` from starting
        the child would escape them all.  The WARNING names the error number,
        not the exception's text.
        """
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        refusal = BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")

        def refuse(*_args: object, **_kwargs: object) -> NoReturn:
            raise refusal

        monkeypatch.setattr(subprocess, "Popen", refuse)

        with pytest.raises(ListingNoAnswerError) as failed:
            run_listing_child(ListingRequest(), configured_host="")

        assert str(failed.value) == (
            "The scanner library could not be started to list scanners"
        )
        assert failed.value.__cause__ is refusal
        messages = [record.getMessage() for record in _warnings(caplog)]
        assert messages == [
            "The scanner library could not be started to list scanners: EAGAIN"
        ]


class TestInterrupt:
    """An interrupted wait kills and reaps the child before re-raising."""

    @pytest.mark.parametrize(
        "interruption",
        [
            pytest.param(KeyboardInterrupt(), id="keyboard-interrupt"),
            pytest.param(ScanInterrupted("stopping", signum=None), id="scan-stop"),
        ],
    )
    def test_an_interrupt_propagates_after_the_child_is_reaped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        interruption: BaseException,
    ) -> None:
        """The interruption is re-raised as it was, with no child left behind."""
        started = _spy_on_communicate(monkeypatch, interruption)
        _use_child(monkeypatch, tmp_path, _SLEEPER_CHILD)
        _pidfile(monkeypatch, tmp_path)

        with pytest.raises(type(interruption)) as raised:
            run_listing_child(ListingRequest(), configured_host="")

        assert raised.value is interruption
        assert len(started) == 1
        assert started[0].returncode is not None
        _assert_reaped(started[0].pid)


def _pid_written(pidfile: Path) -> bool:
    """
    Tell whether a stand-in child has written its whole PID yet.

    Args:
        pidfile: The file the child writes its PID into.

    Returns:
        Whether the file holds a PID.

    """
    try:
        return pidfile.read_text(encoding="utf-8").strip().isdigit()
    except FileNotFoundError:
        return False


class TestAbort:
    """
    An abort set on another thread ends the child at once.

    The thread that sets the abort never touches the child: the launcher, on
    the thread that started it, sees the abort between two slices of its
    wait, and kills and reaps the child itself.
    """

    def test_an_abort_ends_the_child_promptly_and_reaps_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        The abort error comes within a second, with the child already gone.

        It is logged once at INFO and never as a warning: saneless stopping
        is not a fault of the scanner.
        """
        caplog.set_level(logging.INFO, logger=_LOGGER)
        _use_child(monkeypatch, tmp_path, _SLEEPER_CHILD)
        pidfile = _pidfile(monkeypatch, tmp_path)
        abort = threading.Event()
        set_at: list[float] = []

        def abort_once_running() -> None:
            if poll_until(lambda: _pid_written(pidfile), _ELAPSED_CEILING_SECONDS):
                set_at.append(time.monotonic())
                abort.set()

        helper = threading.Thread(target=abort_once_running, daemon=True)
        helper.start()
        with pytest.raises(ListingAbortedError) as aborted:
            run_listing_child(ListingRequest(), configured_host="", abort=abort)
        ended = time.monotonic()
        helper.join(_ELAPSED_CEILING_SECONDS)

        assert isinstance(aborted.value, ScanError)
        assert set_at, "the child never wrote its PID"
        assert ended - set_at[0] < 1.0
        _assert_reaped(int(pidfile.read_text(encoding="utf-8")))
        assert _warnings(caplog) == []
        infos = [
            record
            for record in caplog.records
            if record.name == _LOGGER and record.levelno == logging.INFO
        ]
        assert len(infos) == 1

    def test_the_deadline_still_ends_a_slow_child(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        An abort that is never set leaves the deadline in charge.

        The deadline is still read at call time, so a patched one stops the
        sleeper and reports a timeout, not an abort.
        """
        monkeypatch.setattr(listing, "LISTING_DEADLINE_SECONDS", 0.3)
        started = _spy_on_communicate(monkeypatch)
        _use_child(monkeypatch, tmp_path, _SLEEPER_CHILD)
        _pidfile(monkeypatch, tmp_path)

        began = time.monotonic()
        with pytest.raises(ListingTimedOutError):
            run_listing_child(
                ListingRequest(), configured_host="", abort=threading.Event()
            )
        elapsed = time.monotonic() - began

        assert 0.3 <= elapsed < _ELAPSED_CEILING_SECONDS
        assert len(set(started)) == 1
        assert started[0].returncode is not None
        _assert_reaped(started[0].pid)

    def test_the_request_is_sent_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        A child slower than one slice of the wait still reads one request.

        The request goes with the first slice only.  Every later slice sends
        nothing, and what the child wrote across the slices is all kept.
        """
        _use_child(monkeypatch, tmp_path, _SLOW_ECHO_CHILD)
        started = _spy_on_communicate(monkeypatch)

        reply = run_listing_child(
            ListingRequest(open=_NET_ID), configured_host="", abort=threading.Event()
        )

        assert len(started) > 1
        assert reply.devices[0][0] == (
            '{"open": "net:scanbox.lan:test:0", "alarm": 35}\n'
        )

    def test_a_fast_child_answers_unchanged_with_no_abort(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With no abort at all, a child that answers is decoded."""
        _use_child(monkeypatch, tmp_path, _REPLY_CHILD)

        reply = run_listing_child(ListingRequest(), configured_host="", abort=None)

        assert reply == ListingReply(devices=(_TEST_DEVICE,))

    def test_the_abort_error_is_a_scan_error_and_exported(self) -> None:
        """Every ``except ScanError`` boundary catches an aborted listing."""
        assert issubclass(ListingAbortedError, ScanError)
        assert "ListingAbortedError" in exceptions.__all__


def _valid_device_line(name: str) -> bytes:
    """
    Build one valid reply line holding a single device.

    Args:
        name: The device's name field.

    Returns:
        The reply line, newline included.

    """
    return json.dumps({"devices": [[name, "v", "m", "t"]]}).encode() + b"\n"


class TestReplyDecoder:
    """``ListingReply.from_stdout`` accepts exactly the child's schema."""

    @pytest.mark.parametrize(
        "out",
        [
            pytest.param(b"", id="empty"),
            pytest.param(b"not json\n", id="not-json"),
            pytest.param(b"[]\n", id="not-an-object"),
            pytest.param(b"{}\n", id="no-devices"),
            pytest.param(b'{"devices": "x"}\n', id="devices-not-a-list"),
            pytest.param(b'{"devices": [["a", "b", "c"]]}\n', id="three-fields"),
            pytest.param(b'{"devices": [["a", "b", "c", 4]]}\n', id="non-str-field"),
            pytest.param(b'{"devices": [], "extra": 1}\n', id="unknown-key"),
            pytest.param(
                b'{"devices": [], "list_error": {"type": "RuntimeError"}}\n',
                id="list-error-without-message",
            ),
            pytest.param(
                b'{"devices": [], "list_error": null}\n', id="list-error-null"
            ),
            pytest.param(b'{"devices": [], "opened": "yes"}\n', id="opened-not-bool"),
            pytest.param(b'{"devices": [], "opened": null}\n', id="opened-null"),
            pytest.param(
                b'{"devices": [], "opened": true, '
                b'"open_error": {"type": "error", "message": "x"}}\n',
                id="open-error-with-opened-true",
            ),
            pytest.param(
                b'{"devices": [], "open_error": {"type": "error", "message": "x"}}\n',
                id="open-error-without-opened",
            ),
            pytest.param(b'{"devices": [["\xff", "v", "m", "t"]]}\n', id="bad-utf8"),
            pytest.param(b"[" * 100_000 + b"]" * 100_000 + b"\n", id="deep-nesting"),
            pytest.param(b'{"devices": []}\nnoise\n', id="reply-not-the-last-line"),
        ],
    )
    def test_off_schema_output_is_no_answer(self, out: bytes) -> None:
        """Nothing off-schema escapes as a json, Unicode or key error."""
        with pytest.raises(ScanError) as failed:
            ListingReply.from_stdout(out)

        assert type(failed.value) is ListingNoAnswerError
        assert str(failed.value) == _NO_ANSWER
        assert failed.value.__cause__ is None

    def test_a_reply_over_the_size_cap_is_refused(self) -> None:
        """A valid reply longer than the cap is refused; one under it is not."""
        cap = listing._MAX_REPLY_BYTES
        over = _valid_device_line("a" * cap)
        under = _valid_device_line("a" * (cap - 100))
        assert len(under) <= cap

        with pytest.raises(ScanError, match=_NO_ANSWER):
            ListingReply.from_stdout(over)
        assert ListingReply.from_stdout(under).devices[0][0] == "a" * (cap - 100)

    def test_an_empty_device_list_decodes(self) -> None:
        """A child that found nothing reports an empty tuple."""
        assert ListingReply.from_stdout(b'{"devices": []}\n') == ListingReply(
            devices=()
        )

    def test_a_list_error_decodes(self) -> None:
        """A listing that raised in the child carries its type and message."""
        reply = ListingReply.from_stdout(
            b'{"devices": [], "list_error": {"type": "RuntimeError", "message": "boom"}}\n'
        )

        assert reply.list_error == ChildError("RuntimeError", "boom")
        assert reply.opened is None
        assert reply.open_error is None

    def test_a_failed_open_decodes(self) -> None:
        """A requested open that failed carries its error beside ``opened``."""
        reply = ListingReply.from_stdout(
            b'{"devices": [], "opened": false, '
            b'"open_error": {"type": "error", "message": "Error during device I/O"}}\n'
        )

        assert reply.opened is False
        assert reply.open_error == ChildError("error", "Error during device I/O")

    def test_a_successful_open_decodes(self) -> None:
        """A requested open that worked reports ``opened`` and no error."""
        reply = ListingReply.from_stdout(b'{"devices": [], "opened": true}\n')

        assert reply.opened is True
        assert reply.open_error is None

    def test_a_lone_surrogate_round_trips(self) -> None:
        """A device name with an undecodable byte comes back as the same str."""
        reply = ListingReply.from_stdout(b'{"devices": [["\\udcff", "v", "m", "t"]]}\n')

        assert reply.devices[0][0] == "\udcff"


class TestChildEnvironment:
    """``child_environment`` strips secrets and derives the host list once."""

    def test_saneless_variables_are_dropped_and_the_host_is_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The token never reaches the child; SANE's own settings do."""
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", "secret")
        monkeypatch.setenv("SANE_CONFIG_DIR", "/x")

        env = child_environment("scanbox.lan")

        assert not [key for key in env if key.startswith("SANELESS_")]
        assert "secret" not in env.values()
        assert env["SANE_CONFIG_DIR"] == "/x"
        assert env["SANE_NET_HOSTS"] == "scanbox.lan"

    @pytest.mark.parametrize(
        "key",
        [
            pytest.param("saneless_paperless__token", id="lowercase"),
            pytest.param("Saneless_Paperless__Token", id="mixed-case"),
        ],
    )
    def test_saneless_variables_are_dropped_in_any_case(
        self, monkeypatch: pytest.MonkeyPatch, key: str
    ) -> None:
        """
        The prefix is matched as the settings loader matches it, ignoring case.

        The loader reads ``saneless_paperless__token`` as the Paperless token,
        so the filter must drop it too.
        """
        monkeypatch.setenv(key, "secret")

        env = child_environment("")

        assert key not in env
        assert "secret" not in env.values()

    def test_an_exported_host_list_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-empty exported SANE_NET_HOSTS is passed on over the setting."""
        monkeypatch.setenv("SANE_NET_HOSTS", "a.lan:b.lan")

        assert child_environment("scanbox.lan")["SANE_NET_HOSTS"] == "a.lan:b.lan"

    @pytest.mark.parametrize(
        "exported",
        [pytest.param(None, id="unset"), pytest.param("", id="exported-empty")],
    )
    def test_no_host_list_means_no_variable(
        self, monkeypatch: pytest.MonkeyPatch, exported: str | None
    ) -> None:
        """With nothing to dial, the child gets no SANE_NET_HOSTS at all."""
        if exported is None:
            monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
        else:
            monkeypatch.setenv("SANE_NET_HOSTS", exported)

        assert "SANE_NET_HOSTS" not in child_environment("")

    def test_only_the_launcher_plumbing_reaches_the_child(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Inside the child, the only SANELESS_ variables are the launcher's two."""
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", "secret")
        _use_child(monkeypatch, tmp_path, _ENVIRONMENT_CHILD)

        reply = run_listing_child(ListingRequest(), configured_host="")

        assert json.loads(reply.devices[0][0]) == [
            "SANELESS_CHILD_PYTHON",
            "SANELESS_CHILD_SCRIPT",
        ]

    def test_the_child_runs_in_isolated_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``-I`` reaches the interpreter, so no PYTHON* variable steers it."""
        _use_child(monkeypatch, tmp_path, _FLAGS_CHILD)

        reply = run_listing_child(ListingRequest(), configured_host="")

        assert reply.devices[0][0] == "1"


def test_the_child_argv_is_literal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    The argv holds no path and no device id: both travel elsewhere.

    The interpreter and the script are in the environment and the device id
    is on stdin, because argv is readable by every local user.  ``exec``
    makes the child's PID the interpreter's own, so killing and waiting on it
    leaves no grandchild.
    """
    started = _spy_on_communicate(monkeypatch)
    _use_child(monkeypatch, tmp_path, _REPLY_CHILD)

    run_listing_child(ListingRequest(open=_NET_ID), configured_host="")

    assert len(started) == 1
    assert started[0].args == (
        "/bin/sh",
        "-c",
        'exec "$SANELESS_CHILD_PYTHON" -I "$SANELESS_CHILD_SCRIPT"',
    )


def test_the_child_is_outside_this_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A signal sent to saneless's process group does not reach the child.

    A terminal's Ctrl-C on ``saneless serve`` signals the whole group.
    Were the child in it, a listing a job was running would die from
    SIGINT and be reported as a scanner-library crash.
    """
    _use_child(monkeypatch, tmp_path, _PROCESS_GROUP_CHILD)

    reply = run_listing_child(ListingRequest(), configured_host="")

    assert int(reply.devices[0][0]) != os.getpgid(0)


def test_the_deadline_is_thirty_seconds() -> None:
    """The documented bound, and the alarm margin the request line relies on."""
    assert listing.LISTING_DEADLINE_SECONDS == 30.0
    assert listing._ALARM_MARGIN_SECONDS == 5


def test_the_listing_failure_errors_are_distinct_scan_errors() -> None:
    """Every ``except ScanError`` boundary catches all four, each distinct."""
    failures = (
        ListingCrashedError,
        ListingTimedOutError,
        ListingNoAnswerError,
        ListingAbortedError,
    )
    for failure in failures:
        assert issubclass(failure, ScanError)
        for other in failures:
            assert failure is other or not issubclass(failure, other)
