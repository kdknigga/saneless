"""
Tests for ``saneless.scanner.scan_child``, the parent side of a scan session.

The session starts a child interpreter, drives it over the scan protocol and
owns every deadline: a child that goes quiet at any stage is killed with its
process group and reaped, and the pass raises a ``ScanError`` that names the
stage and the page.  A cancel, an abort, a Ctrl-C or a sink that refuses a
page stops the child gracefully first, and kills it only once the grace has
run out.

No test here touches libsane.  Each one runs a stand-in child that speaks the
real protocol, written into ``tmp_path`` and run exactly as the real child
is, and blocks at a chosen stage with ``signal.pause()``.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest

import saneless.scanner.scan_child as scan_child_mod
from saneless.exceptions import (
    ConfigError,
    DiskSpaceError,
    FeederEmptyError,
    SanelessError,
    ScanError,
    ScanInterrupted,
)
from saneless.scanner import page_budget, scan_protocol
from saneless.scanner.base import (
    PageRecord,
    PageSink,
    PassCapReached,
    ScanBatch,
    ScanSettings,
)
from saneless.scanner.scan_child import ChildProcess, ScanChildSession
from saneless.vocabulary import (
    ScanStage,
    page_timeout_error,
    scan_child_crashed_error,
    scan_child_ended_error,
    scan_child_no_answer_error,
    scan_child_not_started_error,
    scan_child_out_of_time_error,
    scan_child_stopped_error,
    scan_child_unexpected_error,
)
from tests.conftest import poll_until

if TYPE_CHECKING:
    from PIL import Image

_LOGGER = "saneless.scanner.scan_child"
_DEVICE = "net:scanbox.lan:test:0"
_SETTINGS = ScanSettings(source="ADF", resolution=150, mode="Gray")

# Every shortened deadline: the stage deadline, the cancel grace and the page
# budget's floor and ceiling, so the page budget is exactly this long too.
_SHORT_SECONDS = 0.3

# Generous on purpose: a passing test finishes in well under a second.  These
# bounds only turn "the session never gave up" into a failure instead of a
# hung run.
_ELAPSED_CEILING_SECONDS = 3.0
_POLL_BUDGET_SECONDS = 5.0

# What the stand-in reports once it is configured, so a test can work out the
# page budget's description the same way the session does.
_PARAMETERS = page_budget._ScanParameters(
    frame_format="gray",
    last_frame=True,
    pixels_per_line=4,
    lines=3,
    depth=8,
    bytes_per_line=4,
)
_RESOLUTION = 150
_PAGE_DESCRIPTION = page_budget._describe_page(_PARAMETERS, _RESOLUTION)

# The stand-in child.  It uses the standard library and the protocol module
# only, writes its replies to fd 1 (the session's reply pipe), reads commands
# on a thread of its own and appends each line it reads to the op log, and
# walks a scripted pass of two 4x3 grey pages until the stage named by
# SCAN_TEST_HANG_AT, where it blocks.  Its variables carry no SANELESS_
# prefix, so the child environment's strip keeps them.
_STAND_IN = """\
import json
import os
import queue
import resource
import signal
import sys
import threading
from pathlib import Path

from saneless.scanner import scan_protocol as protocol

env = os.environ
Path(env["SCAN_TEST_PIDFILE"]).write_text(str(os.getpid()))
Path(env["SCAN_TEST_ARGV"]).write_bytes(Path("/proc/self/cmdline").read_bytes())
HANG = env.get("SCAN_TEST_HANG_AT", "")
CRASH = env.get("SCAN_TEST_CRASH_AT", "")
CRASH_SIGNAL = getattr(signal, env.get("SCAN_TEST_CRASH_SIGNAL", "SIGSEGV"))
EXIT_AT = env.get("SCAN_TEST_EXIT_AT", "")
GARBAGE = env.get("SCAN_TEST_GARBAGE", "")
ERROR = env.get("SCAN_TEST_ERROR_TYPE", "")
FATAL = env.get("SCAN_TEST_ERROR_FATAL", "") == "1"
ON_CANCEL = env.get("SCAN_TEST_ON_CANCEL", "ignore")
AT_PAGE = 2
PAGES = 2
WIDTH = 4
HEIGHT = 3
lock = threading.Lock()
commands = queue.Queue()
current = {"page": None}


def write_all(data):
    view = memoryview(data)
    while view:
        view = view[os.write(1, view):]


def send(frame, payload=b""):
    with lock:
        write_all(protocol.encode_frame(frame) + payload)


def hang(where):
    Path(env["SCAN_TEST_HUNG"]).write_text(where)
    while True:
        signal.pause()


def crash():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.kill(os.getpid(), CRASH_SIGNAL)


def stage(name, page=None):
    current["page"] = page
    send(protocol.StageFrame(stage=name, page=page))
    here = page is None or page == AT_PAGE
    if CRASH == name and here:
        crash()
    if EXIT_AT == name and here:
        os._exit(1)
    if HANG == name and here:
        hang(name)


def control():
    for line in sys.stdin.buffer:
        with Path(env["SCAN_TEST_OPLOG"]).open("a") as log:
            log.write(line.decode("ascii"))
        command = json.loads(line)
        if command["op"] == "cancel":
            if HANG == "cancel":
                send(protocol.StageFrame(stage="cancel", page=current["page"]))
            elif ON_CANCEL == "exit":
                os._exit(0)
            continue
        commands.put(command)
    commands.put(None)


def run_pass():
    stage("open")
    if env.get("SCAN_TEST_LOG"):
        send(
            protocol.LogLine(
                level=30,
                logger="saneless.scanner.scan_session",
                message=env["SCAN_TEST_LOG"],
            )
        )
    if ERROR:
        send(
            protocol.ChildFailure(
                type_name=ERROR,
                message=env["SCAN_TEST_ERROR_MESSAGE"],
                next_step=env.get("SCAN_TEST_ERROR_NEXT") or None,
                stage="open",
                page=None,
                fatal=FATAL,
            )
        )
        if FATAL:
            os._exit(0)
        return
    if GARBAGE == "frame":
        write_all(protocol.LENGTH_PREFIX.pack(5) + b"nope!")
        hang("garbage")
    stage("configure")
    if GARBAGE == "oversize":
        write_all(protocol.LENGTH_PREFIX.pack(protocol.MAX_HEADER_BYTES + 1))
        hang("garbage")
    send(
        protocol.Configured(
            resolution=150,
            frame_format="gray",
            last_frame=True,
            pixels_per_line=WIDTH,
            lines=HEIGHT,
            depth=8,
            bytes_per_line=WIDTH,
            use_adf=True,
        )
    )
    for number in range(1, PAGES + 1):
        stage("start", number)
        current["page"] = number
        send(protocol.StageFrame(stage="read", page=number))
        here = number == AT_PAGE
        if CRASH == "read" and here:
            crash()
        if HANG == "cancel" and here:
            hang("read")
        pixels = bytes([number * 40]) * (WIDTH * HEIGHT)
        nbytes = len(pixels) + (1 if GARBAGE == "nbytes" else 0)
        header = protocol.PageHeader(
            number=number, mode="L", width=WIDTH, height=HEIGHT, dpi=150, nbytes=nbytes
        )
        if GARBAGE == "nbytes":
            send(header)
            hang("garbage")
        if HANG == "read" and here:
            send(header, pixels[: len(pixels) // 2])
            hang("read")
        send(header, pixels)
        command = commands.get()
        if command is None or command["op"] == "stop":
            os._exit(0)
    stage("close")
    send(protocol.PassDone(resolution=150, rejected=1, substituted_source="ADF", cap=(2, 3, False)))


threading.Thread(target=control, daemon=True).start()
if HANG == "startup":
    hang("startup")
send(protocol.Ready())
while True:
    command = commands.get()
    if command is None:
        os._exit(0)
    if command["op"] == "scan":
        run_pass()
    elif command["op"] == "restart":
        if HANG == "restart":
            hang("restart")
        send(protocol.Restarted())
    elif command["op"] == "exit":
        if HANG == "exit":
            hang("exit")
        send(protocol.Bye())
        os._exit(int(env.get("SCAN_TEST_EXIT_STATUS", "0")))
"""


class _StandIn:
    """The files a stand-in child writes, and the reading of them."""

    def __init__(self, tmp_path: Path) -> None:
        """Name the files inside ``tmp_path``."""
        self.pidfile = tmp_path / "child.pid"
        self.oplog = tmp_path / "ops.log"
        self.hung = tmp_path / "hung"
        self.argv = tmp_path / "argv"

    def pid(self) -> int:
        """Return the PID the latest stand-in wrote."""
        return int(self.pidfile.read_text(encoding="utf-8"))

    def ops(self) -> list[str]:
        """Return every op the stand-ins read, in order."""
        if not self.oplog.exists():
            return []
        lines = self.oplog.read_text(encoding="ascii").splitlines()
        return [json.loads(line)["op"] for line in lines]

    def hung_at(self, where: str) -> bool:
        """Tell whether the stand-in has blocked at ``where``."""
        try:
            return self.hung.read_text(encoding="utf-8") == where
        except FileNotFoundError:
            return False


def _stand_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **variables: str
) -> _StandIn:
    """
    Write the stand-in child, point the session at it and set its variables.

    Args:
        monkeypatch: Undoes the redirection and the variables after the test.
        tmp_path: Where the script and its files go.
        **variables: ``SCAN_TEST_*`` variables that script this stand-in.

    Returns:
        The stand-in's files.

    """
    child = tmp_path / "stand_in_scan_child.py"
    child.write_text(_STAND_IN, encoding="utf-8")
    monkeypatch.setattr(scan_child_mod, "_CHILD_FILE", child)
    files = _StandIn(tmp_path)
    monkeypatch.setenv("SCAN_TEST_PIDFILE", str(files.pidfile))
    monkeypatch.setenv("SCAN_TEST_OPLOG", str(files.oplog))
    monkeypatch.setenv("SCAN_TEST_HUNG", str(files.hung))
    monkeypatch.setenv("SCAN_TEST_ARGV", str(files.argv))
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    return files


def _shorten_deadlines(
    monkeypatch: pytest.MonkeyPatch, *, grace: float = _SHORT_SECONDS
) -> None:
    """Patch every stage deadline, the page budget and the grace to be short."""
    monkeypatch.setattr(scan_child_mod, "STAGE_DEADLINE_SECONDS", _SHORT_SECONDS)
    monkeypatch.setattr(scan_child_mod, "CANCEL_GRACE_SECONDS", grace)
    monkeypatch.setattr(page_budget, "_PAGE_TIMEOUT_FLOOR_SECONDS", _SHORT_SECONDS)
    monkeypatch.setattr(page_budget, "_PAGE_TIMEOUT_CEILING_SECONDS", _SHORT_SECONDS)


class _Starts:
    """A start factory that counts the children it started."""

    def __init__(self) -> None:
        """Start with no children."""
        self.children: list[ChildProcess] = []

    def __call__(self) -> ChildProcess:
        """Start one child through the real launcher."""
        child = scan_child_mod.start_scan_child("")
        self.children.append(child)
        return child


class _RecordingSink(PageSink):
    """A sink that keeps what it was given, or raises on the first page."""

    def __init__(self, tmp_path: Path, refusal: BaseException | None = None) -> None:
        """Keep pages, or raise ``refusal`` when one arrives."""
        self._directory = tmp_path
        self._refusal = refusal
        self.pages: list[tuple[tuple[int, int], str, int, bytes]] = []
        self.records: list[PageRecord] = []

    def add(self, image: Image.Image, *, dpi: int) -> PageRecord:
        """Record one page, or refuse it."""
        if self._refusal is not None:
            raise self._refusal
        self.pages.append((image.size, image.mode, dpi, image.tobytes()))
        record = PageRecord(
            sequence=len(self.pages),
            path=self._directory / f"page{len(self.pages)}.png",
            size=image.size,
            mode=image.mode,
            dpi=dpi,
            ink_coverage=0.0,
            paper_white=255,
        )
        self.records.append(record)
        return record


def _assert_reaped(pid: int) -> None:
    """
    Assert a child process is gone and already waited for.

    ``waitpid`` raising ``ChildProcessError`` means this process has no
    unreaped child with that PID: not a running one, and not a zombie.
    """
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    assert not Path(f"/proc/{pid}").exists()


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the session's WARNING messages."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _LOGGER and record.levelno == logging.WARNING
    ]


# The stages a stand-in can block at, each one case of the hang matrix.
_HANG_STAGES = (
    "startup",
    "open",
    "configure",
    "start",
    "read",
    "cancel",
    "close",
    "restart",
    "exit",
)

# How many pages reach the sink before the stand-in blocks there.
_PAGES_KEPT = {
    "startup": 0,
    "open": 0,
    "configure": 0,
    "start": 1,
    "read": 1,
    "cancel": 1,
    "close": 2,
    "restart": 2,
    "exit": 2,
}


def _expected_hang_error(stage: str) -> str:
    """
    Return the sentence a child stopped at ``stage`` is reported with.

    A page that overran its budget was cancelled first; the stand-in ignored
    the cancel, so it is the page-timeout sentence that says saneless stopped
    the scanner.  Any other stage names itself.
    """
    if stage in {"start", "read", "cancel"}:
        return page_timeout_error(
            page_budget._page_label(1),
            _SHORT_SECONDS,
            _PAGE_DESCRIPTION,
            returned=False,
        )
    return scan_child_stopped_error(ScanStage(stage), None)


def _drive(session: ScanChildSession, stage: str, sink: PageSink) -> None:
    """Run the session as far as the call that meets a stand-in blocked at ``stage``."""
    session.scan_pass(_DEVICE, _SETTINGS, sink)
    if stage == "restart":
        session.restart()
    elif stage == "exit":
        session.close()


@pytest.mark.parametrize("stage", _HANG_STAGES)
def test_a_child_that_hangs_is_killed_and_reaped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage: str
) -> None:
    """
    A stand-in blocked at any stage is killed and reaped, with a truthful error.

    The pages the sink already took stay in the sink, and the call ends a
    short way after the shortened deadline: the stage deadline, or the page
    budget plus the cancel grace.
    """
    _shorten_deadlines(monkeypatch)
    files = _stand_in(monkeypatch, tmp_path, SCAN_TEST_HANG_AT=stage)
    starts = _Starts()
    sink = _RecordingSink(tmp_path)
    session = ScanChildSession(starts)

    began = time.monotonic()
    with pytest.raises(ScanError) as failed:
        _drive(session, stage, sink)
    elapsed = time.monotonic() - began

    assert type(failed.value) is ScanError
    assert str(failed.value) == _expected_hang_error(stage)
    _assert_reaped(files.pid())
    assert session.children_killed == 1
    assert len(sink.pages) == _PAGES_KEPT[stage]
    assert elapsed < _ELAPSED_CEILING_SECONDS
    session.close()


def test_a_child_that_answers_the_cancel_is_not_killed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A page that overran is cancelled; a child that then exits was not stopped.

    The error is the page-timeout sentence for a read that came back.
    """
    _shorten_deadlines(monkeypatch)
    files = _stand_in(
        monkeypatch, tmp_path, SCAN_TEST_HANG_AT="read", SCAN_TEST_ON_CANCEL="exit"
    )
    starts = _Starts()
    session = ScanChildSession(starts)

    with pytest.raises(ScanError) as failed:
        session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))

    assert str(failed.value) == page_timeout_error(
        page_budget._page_label(1), _SHORT_SECONDS, _PAGE_DESCRIPTION, returned=True
    )
    assert starts.children[0].poll() == 0
    assert session.children_killed == 0
    assert "cancel" in files.ops()
    _assert_reaped(files.pid())


class _ReapInterruptedOnce:
    """A child whose first kill is interrupted between the kill and the reap."""

    def __init__(self, child: ChildProcess) -> None:
        """Wrap a started child."""
        self._child = child
        self._interrupted = False

    @property
    def pid(self) -> int:
        """The child's process id."""
        return self._child.pid

    @property
    def command_fd(self) -> int:
        """The write end of the child's command channel."""
        return self._child.command_fd

    @property
    def reply_fd(self) -> int:
        """The read end of the child's reply channel."""
        return self._child.reply_fd

    def poll(self) -> int | None:
        """Reap the child if it has exited."""
        return self._child.poll()

    def wait(self, timeout: float) -> int | None:
        """Wait for the child to exit."""
        return self._child.wait(timeout)

    def kill_and_reap(self) -> int:
        """
        Kill the child; the first time, raise a Ctrl-C before it is reaped.

        Raises:
            KeyboardInterrupt: The first time, once the child is killed.

        """
        if not self._interrupted:
            self._interrupted = True
            os.killpg(self._child.pid, signal.SIGKILL)
            raise KeyboardInterrupt
        return self._child.kill_and_reap()

    def close(self) -> None:
        """Close both channels."""
        self._child.close()


def test_a_child_whose_kill_was_interrupted_is_still_reaped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A Ctrl-C between the kill and the reap does not leave the child unreaped.

    The session keeps the child until it is reaped, so ending it again on
    the way out reaps it, and only then is the live Event cleared.
    """
    _shorten_deadlines(monkeypatch)
    files = _stand_in(monkeypatch, tmp_path, SCAN_TEST_HANG_AT="open")
    live = threading.Event()

    def start() -> ChildProcess:
        return _ReapInterruptedOnce(scan_child_mod.start_scan_child(""))

    session = ScanChildSession(start, live=live)

    with pytest.raises(KeyboardInterrupt):
        session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))

    _assert_reaped(files.pid())
    assert not live.is_set()


def test_a_crash_names_the_signal_stage_and_page(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A child that dies from a signal is reported by the signal's name only."""
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    _shorten_deadlines(monkeypatch)
    files = _stand_in(monkeypatch, tmp_path, SCAN_TEST_CRASH_AT="read")
    session = ScanChildSession(_Starts())

    with pytest.raises(ScanError) as failed:
        session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))

    assert str(failed.value) == scan_child_crashed_error(ScanStage.READ, 2, "SIGSEGV")
    assert session.children_killed == 0
    _assert_reaped(files.pid())
    assert any("SIGSEGV" in message for message in _warnings(caplog))


def test_a_child_that_exits_by_itself_is_told_as_such(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A child that exits part way, with a status, sent nothing unreadable.

    Nor did saneless stop it, so the error says the process ended, with its
    status, and not that a reply could not be read.
    """
    _shorten_deadlines(monkeypatch)
    files = _stand_in(monkeypatch, tmp_path, SCAN_TEST_EXIT_AT="open")
    session = ScanChildSession(_Starts())

    with pytest.raises(ScanError) as failed:
        session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))

    assert str(failed.value) == scan_child_ended_error(ScanStage.OPEN, None, 1)
    assert session.children_killed == 0
    _assert_reaped(files.pid())


def test_a_child_its_own_alarm_ended_is_told_it_ran_out_of_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``SIGALRM`` is the child's own time limit, not a crash."""
    _shorten_deadlines(monkeypatch)
    files = _stand_in(
        monkeypatch,
        tmp_path,
        SCAN_TEST_CRASH_AT="read",
        SCAN_TEST_CRASH_SIGNAL="SIGALRM",
    )
    session = ScanChildSession(_Starts())

    with pytest.raises(ScanError) as failed:
        session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))

    assert str(failed.value) == scan_child_out_of_time_error(ScanStage.READ, 2)
    assert session.children_killed == 0
    _assert_reaped(files.pid())


@pytest.mark.parametrize(
    ("garbage", "stage", "page"),
    [
        pytest.param("frame", ScanStage.OPEN, None, id="garbage-frame"),
        pytest.param("nbytes", ScanStage.READ, 1, id="mismatched-nbytes"),
        pytest.param("oversize", ScanStage.CONFIGURE, None, id="oversized-header"),
    ],
)
def test_an_unreadable_reply_is_no_answer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    garbage: str,
    stage: ScanStage,
    page: int | None,
) -> None:
    """A reply off the schema is no answer: the child is killed and reaped."""
    _shorten_deadlines(monkeypatch)
    files = _stand_in(monkeypatch, tmp_path, SCAN_TEST_GARBAGE=garbage)
    session = ScanChildSession(_Starts())

    with pytest.raises(ScanError) as failed:
        session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))

    assert str(failed.value) == scan_child_no_answer_error(stage, page)
    assert session.children_killed == 1
    _assert_reaped(files.pid())


@pytest.mark.parametrize(
    "error_class",
    [
        pytest.param(ScanError, id="ScanError"),
        pytest.param(FeederEmptyError, id="FeederEmptyError"),
        pytest.param(ConfigError, id="ConfigError"),
    ],
)
def test_an_allowlisted_error_is_rebuilt_as_its_class(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_class: type[SanelessError],
) -> None:
    """The child's error comes back as its own class, message and next step."""
    _shorten_deadlines(monkeypatch)
    files = _stand_in(
        monkeypatch,
        tmp_path,
        SCAN_TEST_ERROR_TYPE=error_class.__name__,
        SCAN_TEST_ERROR_MESSAGE="The feeder is empty",
        SCAN_TEST_ERROR_NEXT="Load the feeder and try again",
        SCAN_TEST_ERROR_FATAL="1",
    )
    session = ScanChildSession(_Starts())

    with pytest.raises(SanelessError) as failed:
        session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))

    assert type(failed.value) is error_class
    assert str(failed.value) == "The feeder is empty"
    assert failed.value.next_step == "Load the feeder and try again"
    assert session.children_killed == 0
    _assert_reaped(files.pid())


def test_an_unexpected_error_type_names_only_the_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any other type is a ScanError naming it; the child's text is only logged."""
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    _shorten_deadlines(monkeypatch)
    _stand_in(
        monkeypatch,
        tmp_path,
        SCAN_TEST_ERROR_TYPE="TypeError",
        SCAN_TEST_ERROR_MESSAGE="secret child text",
        SCAN_TEST_ERROR_FATAL="1",
    )
    session = ScanChildSession(_Starts())

    with pytest.raises(ScanError) as failed:
        session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))

    assert type(failed.value) is ScanError
    assert str(failed.value) == scan_child_unexpected_error(
        "TypeError", ScanStage.OPEN, None
    )
    assert "secret child text" not in str(failed.value)
    assert any("secret child text" in message for message in _warnings(caplog))


def test_a_non_fatal_error_keeps_the_child_for_the_next_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child that reported an error it survived is used again, not restarted."""
    _shorten_deadlines(monkeypatch)
    files = _stand_in(
        monkeypatch,
        tmp_path,
        SCAN_TEST_ERROR_TYPE="FeederEmptyError",
        SCAN_TEST_ERROR_MESSAGE="The feeder is empty",
    )
    starts = _Starts()
    session = ScanChildSession(starts)

    for _ in range(2):
        with pytest.raises(FeederEmptyError):
            session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))
    session.close()

    assert len(starts.children) == 1
    assert files.ops() == ["scan", "scan", "exit"]
    assert session.children_killed == 0


def test_the_next_pass_after_a_kill_starts_a_fresh_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """There is no wedged state: a killed child is simply replaced."""
    _shorten_deadlines(monkeypatch)
    files = _stand_in(monkeypatch, tmp_path, SCAN_TEST_HANG_AT="open")
    starts = _Starts()
    session = ScanChildSession(starts)
    with pytest.raises(ScanError):
        session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))
    first = files.pid()

    monkeypatch.delenv("SCAN_TEST_HANG_AT")
    sink = _RecordingSink(tmp_path)
    session.scan_pass(_DEVICE, _SETTINGS, sink)
    session.close()

    assert len(starts.children) == 2
    assert files.pid() != first
    assert len(sink.pages) == 2


def test_a_sink_refusal_stops_the_child_before_another_sheet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A page the sink refuses is answered with stop, never spooled.

    The child exits on the stop within the grace, so it is not killed, and
    the sink's own error is the one raised.
    """
    _shorten_deadlines(monkeypatch, grace=2.0)
    files = _stand_in(monkeypatch, tmp_path)
    refusal = DiskSpaceError("The spool is full")
    starts = _Starts()
    session = ScanChildSession(starts)

    with pytest.raises(DiskSpaceError) as raised:
        session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path, refusal))

    assert raised.value is refusal
    assert files.ops() == ["scan", "stop"]
    assert starts.children[0].poll() == 0
    assert session.children_killed == 0
    _assert_reaped(files.pid())


@pytest.mark.parametrize(
    ("on_cancel", "killed"),
    [
        pytest.param("exit", 0, id="honours-cancel"),
        pytest.param("ignore", 1, id="ignores-cancel"),
    ],
)
def test_an_abort_mid_read_returns_within_the_grace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, on_cancel: str, killed: int
) -> None:
    """
    An abort set on another thread ends the pass within the grace plus reaping.

    The session sends cancel first; a child that ignores it is killed once
    the grace has run out.  Either way the pass raises the server-stop
    interruption.
    """
    monkeypatch.setattr(scan_child_mod, "CANCEL_GRACE_SECONDS", _SHORT_SECONDS)
    files = _stand_in(
        monkeypatch, tmp_path, SCAN_TEST_HANG_AT="read", SCAN_TEST_ON_CANCEL=on_cancel
    )
    abort = threading.Event()
    set_at: list[float] = []

    def abort_once_reading() -> None:
        if poll_until(lambda: files.hung_at("read"), _POLL_BUDGET_SECONDS):
            set_at.append(time.monotonic())
            abort.set()

    session = ScanChildSession(_Starts(), abort=abort)
    helper = threading.Thread(target=abort_once_reading, daemon=True)
    helper.start()
    with pytest.raises(ScanInterrupted) as raised:
        session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))
    ended = time.monotonic()
    helper.join(_POLL_BUDGET_SECONDS)

    assert str(raised.value) == "The server is stopping"
    assert raised.value.signum is None
    _assert_reaped(files.pid())
    assert set_at
    assert ended - set_at[0] < 1.0
    assert "cancel" in files.ops()
    assert session.children_killed == killed


# Longer than the cancel grace by far, so a close that waits it out instead of
# the grace is plainly too slow; short enough that it still ends the test.
_LONG_STAGE_SECONDS = 10.0


def test_an_abort_during_close_cuts_the_wait_to_the_grace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A server stop that arrives while a child is asked to exit waits the grace.

    ``close`` had already given the child the stage deadline; the abort cuts
    it short, so the worker's stop is never held for a whole stage deadline.
    """
    monkeypatch.setattr(scan_child_mod, "STAGE_DEADLINE_SECONDS", _LONG_STAGE_SECONDS)
    monkeypatch.setattr(scan_child_mod, "CANCEL_GRACE_SECONDS", _SHORT_SECONDS)
    files = _stand_in(monkeypatch, tmp_path, SCAN_TEST_HANG_AT="exit")
    abort = threading.Event()
    session = ScanChildSession(_Starts(), abort=abort)
    session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))
    set_at: list[float] = []

    def abort_once_exiting() -> None:
        if poll_until(lambda: files.hung_at("exit"), _POLL_BUDGET_SECONDS):
            set_at.append(time.monotonic())
            abort.set()

    helper = threading.Thread(target=abort_once_exiting, daemon=True)
    helper.start()
    with pytest.raises(ScanError) as failed:
        session.close()
    ended = time.monotonic()
    helper.join(_POLL_BUDGET_SECONDS)

    assert str(failed.value) == scan_child_stopped_error(ScanStage.EXIT, None)
    assert set_at
    assert ended - set_at[0] < _LONG_STAGE_SECONDS / 2
    _assert_reaped(files.pid())


def test_a_child_that_exits_with_a_status_on_close_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A child that answers ``exit`` but exits with a status is not let pass."""
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    files = _stand_in(monkeypatch, tmp_path, SCAN_TEST_EXIT_STATUS="4")
    with ScanChildSession(_Starts()) as session:
        session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))

    assert _warnings(caplog) == [
        "The scanning process exited with status 4 when asked to exit"
    ]
    _assert_reaped(files.pid())


def _interrupt_main_thread_once(files: _StandIn) -> threading.Thread:
    """Send SIGINT to the main thread once the stand-in blocks mid-read."""
    main_thread = threading.main_thread().ident
    assert main_thread is not None

    def interrupt() -> None:
        if poll_until(lambda: files.hung_at("read"), _POLL_BUDGET_SECONDS):
            signal.pthread_kill(main_thread, signal.SIGINT)

    helper = threading.Thread(target=interrupt, daemon=True)
    helper.start()
    return helper


@pytest.mark.parametrize("how", [pytest.param("ctrl-c", id="ctrl-c"), "sink"])
def test_an_interrupt_mid_read_is_re_raised_after_the_child_is_reaped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str
) -> None:
    """
    A Ctrl-C or a stop raised from the sink comes back out once the child is gone.

    The session stops the child on its way out, so nothing is left running
    when the interruption reaches the caller.
    """
    monkeypatch.setattr(scan_child_mod, "CANCEL_GRACE_SECONDS", _SHORT_SECONDS)
    files = _stand_in(monkeypatch, tmp_path, SCAN_TEST_HANG_AT="read")
    live = threading.Event()
    session = ScanChildSession(_Starts(), live=live)
    if how == "ctrl-c":
        helper = _interrupt_main_thread_once(files)
        with pytest.raises(KeyboardInterrupt):
            session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))
        helper.join(_POLL_BUDGET_SECONDS)
        assert "cancel" in files.ops()
    else:
        interruption = ScanInterrupted("stopping", signum=None)
        with pytest.raises(ScanInterrupted) as raised:
            session.scan_pass(
                _DEVICE, _SETTINGS, _RecordingSink(tmp_path, interruption)
            )
        assert raised.value is interruption
        assert files.ops() == ["scan", "stop"]
    _assert_reaped(files.pid())
    assert not live.is_set()


def test_live_is_set_while_a_child_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The live Event is set from the spawn until the child is reaped."""
    _shorten_deadlines(monkeypatch)
    files = _stand_in(monkeypatch, tmp_path)
    live = threading.Event()
    session = ScanChildSession(_Starts(), live=live)

    assert not live.is_set()
    session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))
    assert live.is_set()
    session.close()

    assert not live.is_set()
    _assert_reaped(files.pid())


def test_the_device_id_travels_on_stdin_never_in_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Any local user can read argv, so the device id goes in the scan command."""
    _shorten_deadlines(monkeypatch)
    files = _stand_in(monkeypatch, tmp_path)
    with ScanChildSession(_Starts()) as session:
        session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))

    scan = json.loads(files.oplog.read_text(encoding="ascii").splitlines()[0])
    assert scan["op"] == "scan"
    assert scan["device"] == _DEVICE
    assert scan["settings"]["source"] == "ADF"
    assert _DEVICE.encode() not in files.argv.read_bytes()
    assert b"scanbox" not in files.argv.read_bytes()


def test_restart_without_a_child_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no child alive there is nothing to restart, and none is started."""
    files = _stand_in(monkeypatch, tmp_path)
    starts = _Starts()
    session = ScanChildSession(starts)

    session.restart()
    session.close()

    assert starts.children == []
    assert not files.pidfile.exists()


def test_restart_of_an_idle_child_returns_on_restarted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An idle child is asked to restart and answers; it is not replaced."""
    _shorten_deadlines(monkeypatch)
    files = _stand_in(monkeypatch, tmp_path)
    starts = _Starts()
    session = ScanChildSession(starts)
    session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))

    session.restart()
    session.close()

    assert files.ops() == ["scan", "spooled", "spooled", "restart", "exit"]
    assert len(starts.children) == 1
    assert session.children_killed == 0


def test_scan_stages_match_the_protocol() -> None:
    """Every stage the child can report maps to a stage saneless can name."""
    assert {stage.value for stage in ScanStage} == scan_protocol.STAGES


def test_a_two_page_pass_returns_the_batch_the_child_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    The batch is the sink's own records plus what the child said about the pass.

    Each page reaches the sink as the child sent it, and is acknowledged
    with spooled before the child reads the next sheet.
    """
    _shorten_deadlines(monkeypatch)
    files = _stand_in(monkeypatch, tmp_path)
    starts = _Starts()
    sink = _RecordingSink(tmp_path)
    session = ScanChildSession(starts)

    batch = session.scan_pass(_DEVICE, _SETTINGS, sink)
    session.close()

    assert batch == ScanBatch(
        pages=tuple(sink.records),
        actual_resolution=150,
        pages_rejected=1,
        substituted_source="ADF",
        cap_reached=PassCapReached(cap=2, sheet_not_kept=3, auto_source=False),
    )
    assert sink.pages == [
        ((4, 3), "L", 150, bytes([40]) * 12),
        ((4, 3), "L", 150, bytes([80]) * 12),
    ]
    assert files.ops() == ["scan", "spooled", "spooled", "exit"]
    assert starts.children[0].poll() == 0
    assert session.children_killed == 0
    _assert_reaped(files.pid())


def test_the_childs_log_lines_are_logged_under_their_own_logger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A log record the child sends is emitted here, at its level and name."""
    caplog.set_level(logging.DEBUG)
    _shorten_deadlines(monkeypatch)
    _stand_in(monkeypatch, tmp_path, SCAN_TEST_LOG="the feeder reported a jam")
    with ScanChildSession(_Starts()) as session:
        session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))

    forwarded = [
        (record.name, record.levelno)
        for record in caplog.records
        if record.getMessage() == "the feeder reported a jam"
    ]
    assert forwarded == [("saneless.scanner.scan_session", logging.WARNING)]


def test_a_childs_log_line_cannot_choose_a_logger_or_forge_a_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A logger the child's code does not use is not created here.

    The record goes under this module's logger instead, its control
    characters escaped and its later lines indented, so it cannot pass for a
    record of saneless's own.
    """
    caplog.set_level(logging.DEBUG)
    invented = "saneless.invented.by.the.child"

    scan_child_mod._emit(
        scan_protocol.LogLine(
            level=logging.WARNING,
            logger=invented,
            message="first\nERROR saneless: forged\x1b[31m",
        )
    )

    assert invented not in logging.Logger.manager.loggerDict
    assert [(record.name, record.getMessage()) for record in caplog.records] == [
        (_LOGGER, "first\n    ERROR saneless: forged\\x1b[31m")
    ]


def test_a_child_that_cannot_be_started_is_a_scan_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A refused fork is a ScanError, logged by its error number's name only."""
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    refusal = BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")

    def refuse(*_args: object, **_kwargs: object) -> NoReturn:
        raise refusal

    monkeypatch.setattr(subprocess, "Popen", refuse)
    live = threading.Event()
    session = ScanChildSession(lambda: scan_child_mod.start_scan_child(""), live=live)

    with pytest.raises(ScanError) as failed:
        session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(Path()))

    assert str(failed.value) == scan_child_not_started_error()
    assert failed.value.__cause__ is refusal
    assert _warnings(caplog) == ["The scanning process could not be started: EAGAIN"]
    assert not live.is_set()


def test_an_exit_failure_does_not_replace_the_error_in_flight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Leaving the session on an error logs a failed exit instead of raising it."""
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    _shorten_deadlines(monkeypatch)
    files = _stand_in(monkeypatch, tmp_path, SCAN_TEST_HANG_AT="exit")
    original = RuntimeError("the pipeline failed")

    def leave_on_an_error() -> None:
        with ScanChildSession(_Starts()) as session:
            session.scan_pass(_DEVICE, _SETTINGS, _RecordingSink(tmp_path))
            raise original

    with pytest.raises(RuntimeError) as raised:
        leave_on_an_error()

    assert raised.value is original
    assert scan_child_stopped_error(ScanStage.EXIT, None) in " ".join(_warnings(caplog))
    _assert_reaped(files.pid())


# The reply pipe capacity saneless asks for: a page then crosses in a few
# large reads instead of one per 64 KiB, the kernel's default.
_WIDE_PIPE_BYTES = 1 << 20


def test_a_started_childs_reply_pipe_is_widened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reply pipe holds 1 MiB or more on saneless's end once the child starts."""
    _stand_in(monkeypatch, tmp_path)
    child = scan_child_mod.start_scan_child("")
    try:
        capacity = fcntl.fcntl(child.reply_fd, fcntl.F_GETPIPE_SZ)
    finally:
        child.kill_and_reap()
        child.close()

    assert capacity >= _WIDE_PIPE_BYTES
    _assert_reaped(child.pid)


def test_a_refused_pipe_size_keeps_the_default_and_the_pass_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A kernel that refuses the larger pipe costs speed, never the scan.

    Unprivileged processes may not exceed ``/proc/sys/fs/pipe-max-size`` or
    their pipe quota; the pipe then keeps its default size.
    """
    real = fcntl.fcntl
    asked: list[int] = []

    def refuse(fd: int, cmd: int, arg: int = 0) -> int:
        if cmd == fcntl.F_SETPIPE_SZ:
            asked.append(arg)
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real(fd, cmd, arg)

    monkeypatch.setattr(fcntl, "fcntl", refuse)
    _shorten_deadlines(monkeypatch)
    files = _stand_in(monkeypatch, tmp_path)
    sink = _RecordingSink(tmp_path)

    with ScanChildSession(_Starts()) as session:
        batch = session.scan_pass(_DEVICE, _SETTINGS, sink)

    assert asked == [_WIDE_PIPE_BYTES]
    assert batch.pages == tuple(sink.records)
    assert len(sink.pages) == 2
    _assert_reaped(files.pid())
