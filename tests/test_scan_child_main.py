"""
The scan child's own process, driven in this process over real pipes.

``_scan_child.main`` runs on a thread with an ``os.pipe()`` for its commands
and one for its replies, over ``FakeSaneModule`` patched into
``scan_session``.  The alarm and the process exit are recorders, because
pytest-timeout uses ``SIGALRM`` and a real ``os._exit`` would end the run.
Replies are read back with the same protocol functions saneless uses, each
read bounded so a child that stops answering fails the test instead of
hanging it.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import select
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Self

import pytest
from PIL import Image, ImageDraw

import saneless
import saneless.scanner._scan_child as scan_child_main
import saneless.scanner.scan_session as scan_session_mod
from saneless.exceptions import ScanError
from saneless.scanner import page_budget, scan_child
from saneless.scanner.base import PassCapReached, ScanSettings
from saneless.scanner.scan_protocol import (
    Bye,
    ChildFailure,
    Configured,
    ControlOp,
    LogLine,
    PageHeader,
    PassDone,
    Ready,
    Restarted,
    ScanCommand,
    StageFrame,
    encode_command,
    read_frame,
    receive_page,
)
from saneless.vocabulary import (
    PYTHON_SANE_INSTALL_NEXT_STEP,
    ScanStage,
    python_sane_missing_message,
)
from tests.fake_sane import FakeSaneDev, FakeSaneError, FakeSaneModule, ReadBlockMode

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType

    from saneless.scanner.scan_protocol import Frame
    from saneless.scanner.scan_session import PageOutlet

_CHILD_PATH = Path(saneless.__file__).parent / "scanner" / "_scan_child.py"
_DEVICE = "test:0"
_REPLY_TIMEOUT_SECONDS = 10.0
_JOIN_TIMEOUT_SECONDS = 10.0
_REST_SLICE_SECONDS = 0.05
_MAX_PIXELS = 1 << 30
_CONTROL_THREAD = "saneless-scan-control"
_STAGE = scan_child_main._STAGE_ALARM_SECONDS
_READ = scan_child_main._READ_ALARM_SECONDS


def _content_image(index: int) -> Image.Image:
    """
    Build a readable page that differs from the others.

    Returns:
        A 200x300 RGB page well above the byte floor.

    """
    image = Image.new("RGB", (200, 300), "white")
    ImageDraw.Draw(image).rectangle((10, 10 + index, 190, 290), fill="black")
    return image


def _feeder() -> ScanSettings:
    """
    Build settings that send the pass through the named feeder.

    Returns:
        Feeder settings.

    """
    return ScanSettings(
        source="Automatic Document Feeder", resolution=300, mode="Color"
    )


def _scan(settings: ScanSettings | None = None) -> ScanCommand:
    """
    Build the scan command saneless sends for one pass.

    Returns:
        The command, at INFO.

    """
    chosen = _feeder() if settings is None else settings
    return ScanCommand(
        device=_DEVICE, settings=dataclasses.asdict(chosen), log_level=logging.INFO
    )


class _ChildHarness:
    """
    ``_scan_child.main`` on a thread, over two real pipes.

    Attributes:
        alarms: Every value the child armed its alarm with, in order.
        exits: Every status the child asked its process to exit with.
        exited: Set once the child asked to exit.

    """

    def __init__(self, *, grace_seconds: float = 5.0) -> None:
        """
        Start the child.

        Args:
            grace_seconds: How long the child waits after its commands end
                before it asks to exit.

        """
        self.alarms: list[int] = []
        self.exits: list[int] = []
        self.exited = threading.Event()
        command_read, self._command_write = os.pipe()
        self._reply_read, reply_write = os.pipe()
        self._reply_write = reply_write
        self._commands = os.fdopen(command_read, "rb", buffering=0)
        self._status: list[int] = []
        runtime = scan_child_main.ChildRuntime(
            arm_alarm=self._record_alarm,
            exit_process=self._record_exit,
            grace_seconds=grace_seconds,
        )
        self._thread = threading.Thread(
            target=self._run, args=(runtime,), name="scan-child-main", daemon=True
        )
        self._thread.start()

    def _record_alarm(self, seconds: int) -> int:
        """Record an alarm instead of arming one."""
        self.alarms.append(seconds)
        return 0

    def _record_exit(self, status: int) -> None:
        """Record an exit instead of ending the process."""
        self.exits.append(status)
        self.exited.set()

    def _run(self, runtime: scan_child_main.ChildRuntime) -> None:
        """Run the child's main and keep its status."""
        self._status.append(
            scan_child_main.main(self._commands, self._reply_write, runtime)
        )

    def __enter__(self) -> Self:
        """
        Use the harness as a context.

        Returns:
            The harness.

        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """End the child's commands and close what can be closed."""
        self.close_commands()
        self._thread.join(_JOIN_TIMEOUT_SECONDS)
        for thread in threading.enumerate():
            if thread.name == _CONTROL_THREAD:
                thread.join(_JOIN_TIMEOUT_SECONDS)
        if not self._thread.is_alive():
            self._commands.close()
            os.close(self._reply_write)
            os.close(self._reply_read)

    def send(self, command: ScanCommand | ControlOp) -> None:
        """Send one command line."""
        self.send_raw(encode_command(command))

    def send_raw(self, line: bytes) -> None:
        """Send bytes as they are."""
        view = memoryview(line)
        while view:
            view = view[os.write(self._command_write, view) :]

    def close_commands(self) -> None:
        """Close the command pipe, as a parent that died would."""
        if self._command_write >= 0:
            os.close(self._command_write)
            self._command_write = -1

    def _wait(self) -> None:
        """
        Block until a reply is readable, failing the test after a while.

        Raises:
            AssertionError: Nothing arrived in time.

        """
        readable, _, _ = select.select(
            [self._reply_read], [], [], _REPLY_TIMEOUT_SECONDS
        )
        if not readable:
            msg = "the child sent nothing in time"
            raise AssertionError(msg)

    def frame(self) -> Frame:
        """
        Read the next reply, skipping log records.

        Returns:
            The reply.

        """
        while True:
            frame = read_frame(self._reply_read, self._wait, max_pixels=_MAX_PIXELS)
            if not isinstance(frame, LogLine):
                return frame

    def page(self, header: PageHeader) -> Image.Image:
        """
        Read the pixels that follow a page header.

        Returns:
            The page.

        """
        return receive_page(self._reply_read, header, self._wait)

    def frames_until(self, wanted: type) -> list[Frame]:
        """
        Read replies up to and including the first of type ``wanted``.

        Page pixels are read and dropped.

        Returns:
            The replies read.

        """
        frames: list[Frame] = []
        while True:
            frame = self.frame()
            frames.append(frame)
            if isinstance(frame, PageHeader):
                self.page(frame)
            if isinstance(frame, wanted):
                return frames

    def join(self) -> int:
        """
        Wait for ``main`` to return.

        Returns:
            Its status.

        """
        self._thread.join(_JOIN_TIMEOUT_SECONDS)
        assert not self._thread.is_alive(), "main did not return"
        return self._status[0]

    def rest(self) -> bytes:
        """
        Read every byte the child writes until ``main`` has returned.

        Returns:
            The bytes, however they split into frames.

        Raises:
            AssertionError: Nothing arrived for a while and ``main`` had not
                returned.

        """
        received = bytearray()
        idle = 0
        while True:
            done = not self._thread.is_alive()
            readable, _, _ = select.select(
                [self._reply_read], [], [], 0 if done else _REST_SLICE_SECONDS
            )
            if readable:
                received += os.read(self._reply_read, 1 << 20)
                idle = 0
            elif done:
                return bytes(received)
            else:
                idle += 1
                if idle * _REST_SLICE_SECONDS > _REPLY_TIMEOUT_SECONDS:
                    msg = "main did not return"
                    raise AssertionError(msg)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSaneModule:
    """
    Patch a fake python-sane module into ``scan_session``.

    Returns:
        The module, whose device holds three readable sheets.

    """
    dev = FakeSaneDev()
    dev.load_feeder([_content_image(index) for index in range(3)])
    module = FakeSaneModule(device=dev)
    monkeypatch.setattr(scan_session_mod, "sane", module)
    return module


@pytest.fixture
def child() -> Iterator[_ChildHarness]:
    """
    Start the child.

    Yields:
        The harness.

    """
    with _ChildHarness() as harness:
        yield harness


def _ready(child: _ChildHarness) -> None:
    """Read the child's ready message."""
    assert child.frame() == Ready()


def _spool_pass(child: _ChildHarness) -> tuple[list[Frame], list[Image.Image]]:
    """
    Read one pass, answering every page spooled.

    Returns:
        Every reply up to and including the pass's end, and the pages.

    """
    frames: list[Frame] = []
    pages: list[Image.Image] = []
    while True:
        frame = child.frame()
        frames.append(frame)
        if isinstance(frame, PageHeader):
            pages.append(child.page(frame))
            child.send(ControlOp.SPOOLED)
        elif isinstance(frame, PassDone | ChildFailure | Bye):
            return frames, pages


def _shape(frames: list[Frame]) -> list[str]:
    """
    Name each reply briefly, for comparing sequences.

    Returns:
        One word or two per reply.

    """
    shape: list[str] = []
    for frame in frames:
        match frame:
            case StageFrame(stage=stage, page=None):
                shape.append(f"stage {stage}")
            case StageFrame(stage=stage, page=page):
                shape.append(f"stage {stage} {page}")
            case PageHeader(number=number):
                shape.append(f"page {number}")
            case Configured():
                shape.append("configured")
            case PassDone():
                shape.append("pass_done")
            case ChildFailure(type_name=type_name):
                shape.append(f"error {type_name}")
            case Bye():
                shape.append("bye")
            case Restarted():
                shape.append("restarted")
            case Ready():
                shape.append("ready")
            case _:
                shape.append(type(frame).__name__)
    return shape


def _feeder_pass_shape(pages: int) -> list[str]:
    """
    Build the replies of a feeder pass that reads ``pages`` sheets.

    Returns:
        The shape ``_shape`` gives such a pass.

    """
    shape = ["stage open", "stage configure", "configured"]
    for number in range(1, pages + 1):
        shape += [f"stage start {number}", f"stage read {number}", f"page {number}"]
    return [*shape, f"stage start {pages + 1}", "stage close", "pass_done"]


def test_a_feeder_pass_streams_every_page_and_exits_on_request(
    fake: FakeSaneModule, child: _ChildHarness
) -> None:
    """Three sheets spooled one at a time, then ``exit`` ends the child cleanly."""
    loaded = [_content_image(index) for index in range(3)]
    fake.device.load_feeder(loaded)
    _ready(child)

    child.send(_scan())
    frames, pages = _spool_pass(child)
    child.send(ControlOp.EXIT)

    assert _shape(frames) == _feeder_pass_shape(3)
    framing = scan_session_mod._PageFraming("full", 300, geometry_set=False)
    assert [page.tobytes() for page in pages] == [
        framing.crop(image).tobytes() for image in loaded
    ]
    assert [page.size for page in pages] == [(200, 300)] * 3
    assert frames[-1] == PassDone(
        resolution=300, rejected=0, substituted_source=None, cap=None
    )
    assert child.frame() == Bye()
    assert child.join() == 0
    assert fake.device.close_calls == 1


def test_a_restart_between_scans_restarts_the_library(
    fake: FakeSaneModule, child: _ChildHarness
) -> None:
    """One child serves two passes, and restarts SANE between them when asked."""
    _ready(child)
    child.send(_scan())
    first, _ = _spool_pass(child)
    init_calls_after_first = fake.init_call_count

    child.send(ControlOp.RESTART)
    restarted = child.frame()
    counts_after_restart = (fake.exit_call_count, fake.init_call_count)
    fake.device.load_feeder([_content_image(index) for index in range(3)])
    child.send(_scan())
    second, _ = _spool_pass(child)
    child.send(ControlOp.EXIT)

    assert init_calls_after_first == 1
    assert restarted == Restarted()
    assert counts_after_restart == (1, 2)
    assert _shape(first) == _shape(second) == _feeder_pass_shape(3)
    assert child.frame() == Bye()
    assert child.join() == 0


def test_a_stop_answer_ends_the_pass_and_the_child(
    fake: FakeSaneModule, child: _ChildHarness
) -> None:
    """``stop`` for page 1 feeds no second sheet, closes the device and ends."""
    _ready(child)
    child.send(_scan())
    child.frames_until(PageHeader)

    child.send(ControlOp.STOP)

    assert _shape(child.frames_until(Bye)) == ["stage close", "bye"]
    assert child.join() == 0
    assert fake.device.calls.count("start") == 1
    assert fake.device.close_calls == 1


def test_a_cancel_mid_read_cancels_the_handle_from_the_control_thread(
    fake: FakeSaneModule,
    child: _ChildHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A cancel reaches the handle while the main thread is blocked reading.

    The cancelled read hands back a truncated page, which is never sent.
    """
    cancelled_on: list[str] = []
    real_cancel = FakeSaneDev.cancel

    def record_cancel(self: FakeSaneDev) -> None:
        cancelled_on.append(threading.current_thread().name)
        real_cancel(self)

    monkeypatch.setattr(FakeSaneDev, "cancel", record_cancel)
    fake.device.block_read(ReadBlockMode.PARTIAL)
    _ready(child)
    child.send(_scan())
    while child.frame() != StageFrame(stage="read", page=1):
        pass
    assert fake.device.read_started.wait(_REPLY_TIMEOUT_SECONDS)

    child.send(ControlOp.CANCEL)

    assert _shape(child.frames_until(Bye)) == ["stage close", "bye"]
    assert child.join() == 0
    assert cancelled_on[0] == _CONTROL_THREAD
    assert fake.device.close_calls == 1


def test_end_of_commands_while_idle_ends_main_without_an_exit(
    fake: FakeSaneModule, child: _ChildHarness
) -> None:
    """A parent that goes away between passes: main returns 0 and sends nothing."""
    _ready(child)
    assert fake.init_call_count == 1

    child.close_commands()

    assert child.join() == 0
    assert child.exits == []


def test_end_of_commands_mid_read_cancels_and_exits_after_the_grace(
    fake: FakeSaneModule,
) -> None:
    """
    A parent that dies mid-read: the handle is cancelled, then the process ends.

    The read here never comes back on a cancel, so only the exit after the
    grace can end the child.
    """
    fake.device.block_read(ReadBlockMode.NEVER)
    with _ChildHarness(grace_seconds=0.2) as child:
        _ready(child)
        child.send(_scan())
        while child.frame() != StageFrame(stage="read", page=1):
            pass
        assert fake.device.read_started.wait(_REPLY_TIMEOUT_SECONDS)

        child.close_commands()

        assert child.exited.wait(_REPLY_TIMEOUT_SECONDS)
        assert child.exits == [scan_child_main._PARENT_GONE_STATUS]
        assert fake.device.cancel_calls >= 1
        fake.device.release_read()
        assert child.join() == 0


def test_an_empty_feeder_is_a_non_fatal_error_and_the_child_stays_ready(
    fake: FakeSaneModule, child: _ChildHarness
) -> None:
    """An empty feeder is reported, and the next scan in the same child works."""
    fake.device.load_feeder([])
    _ready(child)

    child.send(_scan())
    frames, _ = _spool_pass(child)
    fake.device.load_feeder([_content_image(0)])
    child.send(_scan())
    second, _ = _spool_pass(child)
    child.send(ControlOp.EXIT)

    assert frames[-1] == ChildFailure(
        type_name="FeederEmptyError",
        message="No paper detected in feeder",
        next_step=None,
        stage="start",
        page=1,
        fatal=False,
    )
    assert _shape(second) == _feeder_pass_shape(1)
    assert child.frame() == Bye()
    assert child.join() == 0


def test_a_sane_that_will_not_start_is_a_fatal_scan_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``init`` is reported as the start-up's fatal error."""
    module = FakeSaneModule(init_error=FakeSaneError("Error during device I/O"))
    monkeypatch.setattr(scan_session_mod, "sane", module)

    with _ChildHarness() as child:
        failure = child.frame()
        status = child.join()

    assert isinstance(failure, ChildFailure)
    assert failure.type_name == "ScanError"
    assert failure.message.startswith("Could not initialise SANE: ")
    assert (failure.stage, failure.page, failure.fatal) == ("startup", None, True)
    assert status == 1


def test_a_missing_python_sane_is_a_fatal_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """python-sane that cannot be imported is reported with the install hint."""
    reason = "No module named 'sane'"

    def missing() -> object:
        raise ImportError(reason)

    monkeypatch.setattr(scan_session_mod, "_ensure_sane", missing)

    with _ChildHarness() as child:
        failure = child.frame()
        status = child.join()

    assert failure == ChildFailure(
        type_name="ConfigError",
        message=python_sane_missing_message(reason),
        next_step=PYTHON_SANE_INSTALL_NEXT_STEP,
        stage="startup",
        page=None,
        fatal=True,
    )
    assert status == 1


def test_an_unexpected_exception_is_fatal_and_named_by_type(
    fake: FakeSaneModule,
    child: _ChildHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception no pass should raise ends the child, naming its type."""
    assert fake.init_call_count <= 1

    def broken_pass(
        device_id: str, settings: ScanSettings, outlet: PageOutlet
    ) -> object:
        outlet.stage(ScanStage.CONFIGURE, None)
        raise ZeroDivisionError(device_id + settings.mode)

    monkeypatch.setattr(scan_session_mod, "run_pass", broken_pass)
    _ready(child)

    child.send(_scan())
    frames = child.frames_until(ChildFailure)

    failure = frames[-1]
    assert isinstance(failure, ChildFailure)
    assert failure.type_name == "ZeroDivisionError"
    assert (failure.stage, failure.fatal) == ("configure", True)
    assert child.join() == 1


def test_an_off_schema_command_ends_the_child_with_the_device_closed(
    fake: FakeSaneModule, child: _ChildHarness
) -> None:
    """A line that is not a command ends the child with its own status."""
    _ready(child)
    child.send(_scan())
    child.frames_until(PageHeader)

    child.send_raw(b'{"op": "spooled", "extra": 1}\n')

    assert child.join() == scan_child_main._BAD_COMMAND_STATUS
    assert fake.device.close_calls == 1
    assert fake.device.calls.count("start") == 1


def test_the_alarm_is_armed_before_every_libsane_stage(
    fake: FakeSaneModule, child: _ChildHarness
) -> None:
    """
    Every libsane stage runs under an alarm, and none runs while idle.

    Start-up, then a two-page pass, then a restart: the alarm is cleared
    after ready, while each page waits for its answer, after the pass and
    after the restart.
    """
    fake.device.load_feeder([_content_image(index) for index in range(2)])
    _ready(child)
    child.send(_scan())
    _spool_pass(child)
    child.send(ControlOp.RESTART)
    assert child.frame() == Restarted()
    child.send(ControlOp.EXIT)
    assert child.frame() == Bye()
    assert child.join() == 0

    assert child.alarms == [
        _STAGE,  # start-up
        0,  # ready
        _STAGE,  # open
        _STAGE,  # configure
        _READ,  # start 1
        _READ,  # read 1
        0,  # waiting for page 1's answer
        _READ,  # start 2
        _READ,  # read 2
        0,  # waiting for page 2's answer
        _READ,  # start 3: the end of the feed
        _STAGE,  # close
        0,  # pass done
        _STAGE,  # restart
        0,  # restarted
    ]


def test_a_log_record_becomes_a_log_line() -> None:
    """The forwarder sends a record as a log line, at its level and logger."""
    read_fd, write_fd = os.pipe()
    try:
        reply = scan_child_main._ReplyChannel(write_fd)
        handler = scan_child_main._LogForwarder(reply)
        record = logging.LogRecord(
            "saneless.scanner.scan_session",
            logging.WARNING,
            __file__,
            1,
            "Could not close scanner %s",
            ("test:0",),
            None,
        )

        handler.handle(record)
        frame = read_frame(read_fd, lambda: None, max_pixels=_MAX_PIXELS)
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert frame == LogLine(
        level=logging.WARNING,
        logger="saneless.scanner.scan_session",
        message="Could not close scanner test:0",
    )


def _run_isolated(script: str) -> str:
    """
    Run ``script`` in a fresh isolated interpreter.

    Returns:
        What it printed.

    """
    result = subprocess.run(
        [sys.executable, "-I", "-c", textwrap.dedent(script)],
        env={**os.environ, "SANELESS_TEST_MODULE": str(_CHILD_PATH)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_prepare_process_loads_the_unwinder_before_python_sane() -> None:
    """
    The thread unwinder is loaded with python-sane still unimported.

    A backend thread that is the first to end would otherwise load it, and an
    asynchronous cancel there can leave the dynamic loader's lock held.
    """
    output = _run_isolated(
        """
        import sys

        import saneless.thread_unwinder as unwinder

        seen = []
        unwinder.load_thread_unwinder = lambda: seen.append("sane" in sys.modules)

        from saneless.scanner import _scan_child

        _scan_child.prepare_process()
        print(seen)
        """
    )

    assert output == "[False]"


def test_loading_the_child_imports_no_sane_and_no_ctypes() -> None:
    """
    Loading the script pulls in neither python-sane nor ``ctypes``.

    python-sane loads only inside ``main``.  ``ctypes`` opens a descriptor
    of its own on import, which in a child started with stderr closed would
    take fd 2 before the reply channel is made private.
    """
    output = _run_isolated(
        """
        import importlib.util
        import os
        import sys

        spec = importlib.util.spec_from_file_location(
            "scan_child_probe", os.environ["SANELESS_TEST_MODULE"]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        print(sorted(name for name in ("sane", "_sane", "ctypes") if name in sys.modules))
        """
    )

    assert output == "[]"


def test_the_child_alarms_outlast_the_parents_deadlines() -> None:
    """
    The child's backstop fires only after saneless would have stopped it.

    The alarm is for a child whose parent died; while the parent lives, its
    own deadline and grace must always come first.
    """
    assert _STAGE >= scan_child.STAGE_DEADLINE_SECONDS + 5
    assert (
        _READ
        >= page_budget._PAGE_TIMEOUT_CEILING_SECONDS
        + scan_child.CANCEL_GRACE_SECONDS
        + 5
    )
    assert scan_child_main._PARENT_GONE_GRACE_SECONDS == (
        scan_child.CANCEL_GRACE_SECONDS
    )


def test_a_scan_error_keeps_its_next_step(
    fake: FakeSaneModule,
    child: _ChildHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass's own error is sent with its next step, and the child stays."""
    assert fake.init_call_count <= 1

    def failing_pass(
        device_id: str, settings: ScanSettings, outlet: PageOutlet
    ) -> object:
        outlet.stage(ScanStage.OPEN, None)
        message = f"{device_id} {settings.mode}"
        raise ScanError(message, next_step="Check the cable")

    monkeypatch.setattr(scan_session_mod, "run_pass", failing_pass)
    _ready(child)

    child.send(_scan())
    frames = child.frames_until(ChildFailure)
    child.send(ControlOp.EXIT)

    assert frames[-1] == ChildFailure(
        type_name="ScanError",
        message="test:0 Color",
        next_step="Check the cable",
        stage="open",
        page=None,
        fatal=False,
    )
    assert child.frame() == Bye()
    assert child.join() == 0


def test_a_cap_reached_travels_in_pass_done(
    fake: FakeSaneModule,
    child: _ChildHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass that stopped at its cap reports the cap, the sheet and the source."""
    real_pass = scan_session_mod.run_pass

    def capped_pass(
        device_id: str, settings: ScanSettings, outlet: PageOutlet
    ) -> scan_session_mod.PassOutcome:
        outcome = real_pass(device_id, settings, outlet)
        cap = PassCapReached(cap=2, sheet_not_kept=3, auto_source=True)
        return dataclasses.replace(outcome, cap_reached=cap, substituted_source="ADF")

    monkeypatch.setattr(scan_session_mod, "run_pass", capped_pass)
    assert fake.init_call_count <= 1
    _ready(child)

    child.send(_scan())
    frames, _ = _spool_pass(child)
    child.send(ControlOp.EXIT)

    assert frames[-1] == PassDone(
        resolution=300, rejected=0, substituted_source="ADF", cap=(2, 3, True)
    )
    assert child.frame() == Bye()
    assert child.join() == 0


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param({"source": "ADF"}, id="missing-keys"),
        pytest.param(
            {
                **dataclasses.asdict(
                    ScanSettings(source="ADF", resolution=300, mode="Color")
                ),
                "paper_size": "b5",
            },
            id="unknown-paper-size",
        ),
        pytest.param(
            {
                **dataclasses.asdict(
                    ScanSettings(source="ADF", resolution=300, mode="Color")
                ),
                "resolution": "x",
            },
            id="text-resolution",
        ),
    ],
)
def test_settings_off_the_schema_end_the_child_before_any_open(
    fake: FakeSaneModule,
    child: _ChildHarness,
    settings: dict[str, str | int],
) -> None:
    """A scan whose settings are not exactly a ScanSettings opens nothing."""
    _ready(child)

    child.send(ScanCommand(device=_DEVICE, settings=settings, log_level=20))

    assert child.join() == scan_child_main._BAD_COMMAND_STATUS
    assert fake.device.close_calls == 0
    assert fake.device.calls == []


# The most pixel bytes one write may carry: a page goes out in strips of whole
# rows of about this size, each written as soon as it is made.
_STRIP_CEILING = 1 << 18

# Longer than any frame header these tests receive, and shorter than a strip.
_FRAME_CEILING = 1 << 10

# The pages the strip tests send: taller than one strip, and of a height no
# strip divides, so the last strip is short.
_TALL_PAGES = {
    "L": Image.linear_gradient("L").resize((1000, 700)),
    "RGB": Image.merge(
        "RGB",
        [
            Image.linear_gradient("L").resize((1000, 301)),
            Image.linear_gradient("L").rotate(90).resize((1000, 301)),
            Image.new("L", (1000, 301), 77),
        ],
    ),
}


@pytest.mark.parametrize("mode", sorted(_TALL_PAGES))
def test_a_page_crosses_in_strips_that_rebuild_the_same_image(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeSaneModule,
    child: _ChildHarness,
    mode: str,
) -> None:
    """
    No write carries a whole page, and saneless still gets the page exactly.

    The child makes and writes the pixels a strip at a time, so it encodes the
    next strip while saneless reads the last; the strips add up to the bytes
    ``tobytes()`` gives for the whole page, in order.
    """
    writes: list[int] = []
    write = scan_child_main._ReplyChannel._write

    def recording(reply: scan_child_main._ReplyChannel, data: bytes) -> None:
        writes.append(len(data))
        write(reply, data)

    monkeypatch.setattr(scan_child_main._ReplyChannel, "_write", recording)
    source = _TALL_PAGES[mode]
    fake.device.load_feeder([source])
    _ready(child)

    child.send(_scan())
    frames, pages = _spool_pass(child)
    child.send(ControlOp.EXIT)

    assert _shape(frames) == _feeder_pass_shape(1)
    assert [(page.mode, page.size) for page in pages] == [(mode, source.size)]
    assert pages[0].tobytes() == source.tobytes()
    assert max(writes) <= _STRIP_CEILING
    pixel_writes = [size for size in writes if size > _FRAME_CEILING]
    assert sum(pixel_writes) == len(source.tobytes())
    assert len(pixel_writes) > 1
    assert child.join() == 0


def test_a_page_cut_short_ends_the_channel_after_its_last_strip(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    fake: FakeSaneModule,
    child: _ChildHarness,
) -> None:
    """
    A strip that cannot be made leaves the page short, and nothing follows it.

    The header and the strips already written cannot be taken back, so any
    later frame would be read as pixels: the child sends nothing more, not
    even the error, and ``main`` returns as for a channel that will not take
    a write.  saneless then sees the channel close mid-page.  The device is
    still cancelled and closed, and the cause is named, by type, on stderr.
    """
    source = _TALL_PAGES["L"]
    expected = source.tobytes()
    made: list[int] = []
    tobytes = Image.Image.tobytes

    def failing(image: Image.Image, *args: str) -> bytes:
        made.append(image.height)
        if len(made) > 1:
            raise MemoryError
        return tobytes(image, *args)

    monkeypatch.setattr(Image.Image, "tobytes", failing)
    fake.device.load_feeder([source])
    _ready(child)
    child.send(_scan())
    frames = [child.frame() for _ in range(6)]

    rest = child.rest()

    assert _shape(frames) == _feeder_pass_shape(1)[:6]
    assert len(made) == 2
    assert rest == expected[: made[0] * source.width]
    assert child.join() == scan_child_main._PARENT_GONE_STATUS
    assert fake.device.close_calls == 1
    assert "a page was cut short by MemoryError" in capfd.readouterr().err
