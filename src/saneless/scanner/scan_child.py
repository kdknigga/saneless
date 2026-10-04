"""
Drive a scan session that runs in a child process saneless can always stop.

saneless makes no libsane call in its own process: every SANE call of a scan
session runs in one child, and that child is reaped before the session ends
and before any pass that had to stop it raises.  See
docs/explanation/decisions/0016-scan-sessions-in-a-child-process.md.

The child is started the way every SANE child is (``child_launch``), so the
device id travels in the ``scan`` command on its stdin, never in argv.  This
side owns every deadline, read at call time so tests can shorten them: the
start-up gets ``STARTUP_DEADLINE_SECONDS``, the open, configure, cancel,
close, restart and exit stages get ``STAGE_DEADLINE_SECONDS`` each, both
30 s, the start and read of one page share the page budget worked out from
the parameters the child reports, and no deadline runs while the session is
idle between passes.

A page that overruns its budget, an abort, a Ctrl-C and a sink that refuses a
page all stop the child the same way: saneless asks it to cancel (or stop),
waits up to ``CANCEL_GRACE_SECONDS`` for it to exit, and kills its process
group and reaps it if it has not.  Any other stage that overruns, and a reply
that is off the schema, kill the child at once.  Every outcome is reported
with today's error classes; only the thread that started the child signals
it, while other threads only set the abort Event.

This module imports no python-sane and none of the code the child runs.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import fcntl
import logging
import math
import os
import select
import signal
import subprocess
import time
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol, Self

from saneless.exceptions import (
    ConfigError,
    FeederEmptyError,
    SanelessError,
    ScanError,
    ScanInterrupted,
)
from saneless.scanner import child_launch, page_budget
from saneless.scanner.base import PassCapReached, ScanBatch
from saneless.scanner.scan_protocol import (
    CHILD_LOGGERS,
    REPLY_PIPE_BYTES,
    ChildFailure,
    ChildGoneError,
    Configured,
    ControlOp,
    LogLine,
    PageHeader,
    PassDone,
    ProtocolError,
    Ready,
    Restarted,
    ScanCommand,
    StageFrame,
    encode_command,
    max_page_pixels,
    read_frame,
    receive_page,
)
from saneless.text_safety import neutralise_controls
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

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Mapping
    from types import TracebackType

    from saneless.scanner.base import PageRecord, PageSink, ScanSettings
    from saneless.scanner.scan_protocol import Frame

__all__ = [
    "CANCEL_GRACE_SECONDS",
    "STAGE_DEADLINE_SECONDS",
    "STARTUP_DEADLINE_SECONDS",
    "ChildProcess",
    "ScanChildSession",
    "start_scan_child",
]

logger = logging.getLogger(__name__)

# How long the child may take over any stage that is not a page: starting up,
# opening the device, configuring it, closing it, restarting the scanner
# library and exiting.  The listing child's deadline, for the same kind of
# work.  Read at call time, so tests can shorten it.
STAGE_DEADLINE_SECONDS: Final = 30.0

# How long a new child may take to start its interpreter and the scanner
# library and say it is ready: the stage deadline's 30 s, kept apart so a test
# that shortens the other stages does not also race the interpreter's start.
# Read at call time.
STARTUP_DEADLINE_SECONDS: Final = 30.0

# How long a child asked to cancel or stop has to exit before it is killed.
# Some backends only give up a read on their own cancel after ten seconds.
# Read at call time.
CANCEL_GRACE_SECONDS: Final = 10.0

# The longest one wait on the child lasts before the abort Event and the
# deadline are looked at again.
_ABORT_POLL_SECONDS: Final = 0.1

# The script the child runs.  Read at call time, so tests can point the
# session at a stand-in.
_CHILD_FILE: Final = Path(__file__).with_name("_scan_child.py")

# How much of a reply nobody will read is drained at a time while a stopped
# child is waited for.
_DRAIN_BYTES: Final = 65_536

_SERVER_STOPPING: Final = "The server is stopping"
_OUT_OF_PLACE: Final = "The scan child sent a message out of place"
_GONE: Final = "The scan child closed its command channel"

# The only exception classes a child's error report is rebuilt as.  Any other
# type name becomes a ScanError naming it, so a child can never choose which
# class saneless raises.
_REBUILT: Final[Mapping[str, type[SanelessError]]] = MappingProxyType(
    {
        "ScanError": ScanError,
        "FeederEmptyError": FeederEmptyError,
        "ConfigError": ConfigError,
    }
)


class ChildProcess(Protocol):
    """A running scan child, as the session drives it."""

    @property
    def pid(self) -> int:
        """The child's process id."""
        ...

    @property
    def command_fd(self) -> int:
        """The write end of the child's command channel (its stdin)."""
        ...

    @property
    def reply_fd(self) -> int:
        """The read end of the child's reply channel."""
        ...

    def poll(self) -> int | None:
        """
        Reap the child if it has exited.

        Returns:
            Its exit status, negative for a signal, or ``None`` while it runs.

        """
        ...

    def wait(self, timeout: float) -> int | None:
        """
        Wait for the child to exit, and reap it.

        Args:
            timeout: The longest to wait, in seconds.

        Returns:
            Its exit status, or ``None`` if it was still running.

        """
        ...

    def kill_and_reap(self) -> int:
        """
        Kill the child's process group and reap the child.

        Returns:
            The child's exit status.

        """
        ...

    def close(self) -> None:
        """Close this side's ends of both channels; idempotent."""
        ...


class _PopenChild:
    """A ``ChildProcess`` over the ``Popen`` that ``child_launch`` started."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        """
        Wrap a child started with pipes on its stdin and stdout.

        Raises:
            ValueError: The child has no pipe on stdin or stdout.

        """
        if proc.stdin is None or proc.stdout is None:
            msg = "The scan child was started without its pipes"
            raise ValueError(msg)
        self._proc = proc
        self._stdin = proc.stdin
        self._stdout = proc.stdout
        self._command_fd = proc.stdin.fileno()
        self._reply_fd = proc.stdout.fileno()

    @property
    def pid(self) -> int:
        """The child's process id."""
        return self._proc.pid

    @property
    def command_fd(self) -> int:
        """The write end of the child's stdin."""
        return self._command_fd

    @property
    def reply_fd(self) -> int:
        """The read end of the child's stdout."""
        return self._reply_fd

    def poll(self) -> int | None:
        """Return the child's exit status, or ``None`` while it runs."""
        return self._proc.poll()

    def wait(self, timeout: float) -> int | None:
        """Wait up to ``timeout`` seconds for the child to exit."""
        try:
            return self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill_and_reap(self) -> int:
        """Kill the child's process group and reap the child."""
        child_launch.kill_and_reap(self._proc)
        return self._proc.wait()

    def close(self) -> None:
        """Close both pipes, ignoring a child that has already gone."""
        for stream in (self._stdin, self._stdout):
            try:
                stream.close()
            except OSError:
                # Flushing an empty buffer into a pipe nobody reads any more.
                continue


def start_scan_child(configured_host: str) -> ChildProcess:
    """
    Start one scan child running ``_CHILD_FILE``.

    Args:
        configured_host: The ``scanner.host`` setting, possibly empty.

    Returns:
        The running child.  The caller owns it, and must reap it.

    Raises:
        ScanError: The fork or the exec failed.  Only the error number's name
            is logged: the exception's text can name the interpreter's path.

    """
    try:
        proc = child_launch.start_child(_CHILD_FILE, configured_host)
    except OSError as exc:
        name = "unknown error"
        if exc.errno is not None:
            name = errno.errorcode.get(exc.errno, f"error {exc.errno}")
        logger.warning("The scanning process could not be started: %s", name)
        raise ScanError(scan_child_not_started_error()) from exc
    child = _PopenChild(proc)
    _widen_reply_pipe(child.reply_fd)
    return child


def _widen_reply_pipe(fd: int) -> None:
    """
    Ask for a reply pipe of ``REPLY_PIPE_BYTES``, keeping the default if refused.

    The kernel refuses a size above ``/proc/sys/fs/pipe-max-size``, or past
    the user's pipe quota; the default size still carries every page, only
    in more turns.

    Args:
        fd: saneless's end of the reply pipe.

    """
    with contextlib.suppress(OSError):
        fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, REPLY_PIPE_BYTES)


class _DeadlineError(Exception):
    """The child let the current deadline pass."""


class _AbortedError(Exception):
    """The abort Event was set while the child was waited on."""


class _FailedError(Exception):
    """The child reported an exception it caught."""

    def __init__(self, failure: ChildFailure) -> None:
        """Keep the child's report."""
        super().__init__(failure.type_name)
        self.failure = failure


# What a wait on the child can end in, besides an exception from elsewhere.
_OUTCOMES: Final = (
    _DeadlineError,
    _AbortedError,
    _FailedError,
    ChildGoneError,
    ProtocolError,
)


# Each line after a record's first, such as a traceback's, is indented by
# this, so no text from the child can pass for a log record of its own.
_CONTINUATION: Final = "\n    "


def _emit(line: LogLine) -> None:
    """
    Log one of the child's records at its level, with its text made safe.

    A logger the child's code uses keeps its name; any other is logged under
    this module's, so a child never makes saneless create a logger.  Control
    characters are escaped, and each later line is indented.
    """
    name = line.logger if line.logger in CHILD_LOGGERS else __name__
    text = _CONTINUATION.join(
        neutralise_controls(part) for part in line.message.split("\n")
    )
    logging.getLogger(name).log(line.level, "%s", text)


class ScanChildSession:
    """
    One scan job's child: started at the first pass, reaped when the job ends.

    Every pass of the job runs in the same child.  A child that had to be
    killed is gone, and the next pass simply starts a fresh one.
    """

    def __init__(
        self,
        start: Callable[[], ChildProcess],
        *,
        abort: threading.Event | None = None,
        live: threading.Event | None = None,
    ) -> None:
        """
        Prepare a session; no child starts until the first pass.

        Args:
            start: Starts one child, such as ``start_scan_child`` bound to the
                configured host.
            abort: Set by another thread to stop the session part way, or
                ``None`` when only the deadlines end it.
            live: Set while a child exists, from its start until it is
                reaped, so a stopping server can wait for it.

        """
        self._start = start
        self._abort = abort
        self._live = live
        self._child: ChildProcess | None = None
        self._poller: select.poll | None = None
        self._killed = 0
        self._kill_sent = False
        self._stage = ScanStage.STARTUP
        self._page: int | None = None
        self._deadline = 0.0
        self._on_page_clock = False
        self._page_budget: tuple[float, str] | None = None
        self._in_sync = True
        self._watch_abort = True

    @property
    def children_killed(self) -> int:
        """How many of this session's children had to be killed."""
        return self._killed

    def __enter__(self) -> Self:
        """Return the session."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """
        End the session's child.

        A child that does not end cleanly raises only when nothing else is
        being raised; otherwise it is logged, so it never replaces the error
        that ended the session.
        """
        try:
            self.close()
        except ScanError as error:
            if exc is None:
                raise
            logger.warning("The scan session did not end cleanly: %s", error)

    def scan_pass(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Scan one pass in the session's child, starting the child if needed.

        Each page is handed to ``sink`` as it arrives, and acknowledged
        before the child reads another sheet.

        Args:
            device_id: The SANE device to open.
            settings: The pass's scan settings.
            sink: Where each page goes.

        Returns:
            The sink's records, with what the child reported about the pass.

        Raises:
            ScanError: The child stopped answering, died, sent something
                unreadable, could not be started, or reported a scan error.
            FeederEmptyError: The child reported an empty feeder.
            ConfigError: The child could not load the scanner library.
            ScanInterrupted: The abort Event was set.

        """
        command = ScanCommand(
            device=device_id,
            settings=dataclasses.asdict(settings),
            log_level=logging.getLogger("saneless").getEffectiveLevel(),
        )

        def run() -> ScanBatch:
            self._ensure_child()
            self._page_budget = None
            self._enter(ScanStage.OPEN)
            self._send(command)
            return self._receive_pass(sink)

        return self._guarded(run)

    def restart(self) -> None:
        """
        Restart the child's scanner library between passes.

        With no child alive there is nothing to restart: the next pass starts
        a fresh child, whose library starts fresh.

        Raises:
            ScanError: The child did not restart within the stage deadline,
                died, or answered with something else.
            ScanInterrupted: The abort Event was set.

        """
        if self._kill_sent:
            # saneless killed it already; an interrupt stopped the reap.
            self._end_child()
        if self._child is None:
            logger.debug("No scan child is running, so none was restarted")
            return

        def run() -> None:
            self._enter(ScanStage.RESTART)
            self._send(ControlOp.RESTART)
            frame = self._next_frame()
            if isinstance(frame, ChildFailure):
                raise _FailedError(frame)
            if not isinstance(frame, Restarted):
                raise ProtocolError(_OUT_OF_PLACE)

        self._guarded(run)

    def close(self) -> None:
        """
        Ask the child to exit, and reap it; kill it if it does not exit.

        It gets the stage deadline, or only the cancel grace once the abort
        Event is set, even part way through the wait.  The child is reaped
        whatever happens.

        Raises:
            ScanError: The child had to be killed, or died from a signal.

        """
        if self._child is None:
            return
        if self._kill_sent:
            # saneless killed it already; an interrupt stopped the reap.
            self._end_child()
            return
        self._enter(ScanStage.EXIT)
        status = self._stop_child(ControlOp.EXIT, STAGE_DEADLINE_SECONDS)
        if status is None:
            logger.warning(
                "The scanning process did not exit when asked, so it was stopped"
            )
            raise ScanError(scan_child_stopped_error(ScanStage.EXIT, None))
        if status < 0:
            raise self._crashed(-status, ScanStage.EXIT, None)
        if status > 0:
            logger.warning(
                "The scanning process exited with status %d when asked to exit",
                status,
            )

    def _guarded[T](self, operation: Callable[[], T]) -> T:
        """
        Run one exchange with the child, mapping every way it can end.

        A child that went quiet, died, failed or sent something unreadable is
        dealt with and reported as a saneless error.  Anything else raised
        here (a Ctrl-C, a stop signal, the sink's own error) stops the child
        gracefully first and is re-raised as it was.
        """
        try:
            return operation()
        except _OUTCOMES as outcome:
            try:
                error = self._settle(outcome)
            except BaseException:
                # A second interrupt while the child was being stopped.
                self._end_child()
                raise
            raise error from None
        except BaseException:
            self._stop_child(ControlOp.CANCEL, CANCEL_GRACE_SECONDS)
            raise

    def _settle(self, outcome: Exception) -> BaseException:
        """
        Deal with the child after ``outcome``, and build the error to raise.

        Args:
            outcome: How the wait on the child ended.

        Returns:
            The exception for the caller to raise.

        """
        stage, page = self._stage, self._page
        if isinstance(outcome, _AbortedError):
            logger.info("The scan was stopped because saneless is stopping")
            self._stop_child(ControlOp.CANCEL, CANCEL_GRACE_SECONDS)
            return ScanInterrupted(_SERVER_STOPPING)
        if isinstance(outcome, _DeadlineError):
            return self._overran(stage, page)
        if isinstance(outcome, _FailedError):
            return self._failed(outcome.failure)
        if isinstance(outcome, ChildGoneError):
            return self._gone(stage, page)
        self._end_child()
        logger.warning(
            "The scanning process sent a reply saneless could not read (%s), "
            "so it was stopped",
            stage.value,
        )
        return ScanError(scan_child_no_answer_error(stage, page))

    def _overran(self, stage: ScanStage, page: int | None) -> ScanError:
        """
        Stop a child that let a deadline pass, and build its error.

        A page that overran its budget is cancelled first, as an in-process
        read would be; any other stage is killed at once.
        """
        budget = self._page_budget
        if self._on_page_clock and budget is not None:
            seconds, description = budget
            label = page_budget._page_label((page or 1) - 1)
            status = self._stop_child(ControlOp.CANCEL, CANCEL_GRACE_SECONDS)
            returned = status is not None
            logger.warning(
                "%s did not arrive within %.0f s; the scanning process %s",
                label,
                seconds,
                "ended on the cancel" if returned else "was stopped",
            )
            return ScanError(
                page_timeout_error(label, seconds, description, returned=returned)
            )
        self._end_child()
        logger.warning(
            "The scanning process stopped answering (%s), so it was stopped",
            stage.value,
        )
        return ScanError(scan_child_stopped_error(stage, page))

    def _failed(self, failure: ChildFailure) -> SanelessError:
        """
        Rebuild the child's reported error; wait for the child if it is ending.

        Only the allowlisted classes are rebuilt.  Any other type is a
        ``ScanError`` naming the type; the child's text goes to the log only.
        """
        stage = ScanStage(failure.stage)
        error_class = _REBUILT.get(failure.type_name)
        error: SanelessError
        if error_class is not None:
            error = error_class(failure.message, next_step=failure.next_step)
        else:
            type_name = neutralise_controls(failure.type_name)
            logger.warning(
                "The scanning process failed unexpectedly (%s, %s): %s",
                type_name,
                stage.value,
                neutralise_controls(failure.message),
            )
            error = ScanError(
                scan_child_unexpected_error(type_name, stage, failure.page)
            )
        if failure.fatal:
            self._enter(ScanStage.EXIT)
            if self._finish_child(STAGE_DEADLINE_SECONDS) is None:
                logger.warning(
                    "The scanning process did not exit after its error, so it "
                    "was stopped"
                )
        return error

    def _gone(self, stage: ScanStage, page: int | None) -> ScanError:
        """
        Reap a child that closed its channels, and build the error for it.

        Each outcome is told as it was: a child that had to be killed, one a
        signal ended, and one that exited by itself with its status.
        """
        status = self._finish_child(CANCEL_GRACE_SECONDS)
        if status is None:
            logger.warning(
                "The scanning process closed its reply channel but did not exit, "
                "so it was stopped"
            )
            return ScanError(scan_child_stopped_error(stage, page))
        if status < 0:
            return self._crashed(-status, stage, page)
        logger.warning(
            "The scanning process exited with status %d before it finished",
            status,
        )
        return ScanError(scan_child_ended_error(stage, page, status))

    @staticmethod
    def _crashed(signum: int, stage: ScanStage, page: int | None) -> ScanError:
        """
        Log a child a signal killed, by the signal's name only.

        ``SIGALRM`` is the child's own time limit, not a crash, and is told so.
        """
        name = child_launch.signal_name(signum)
        if signum == signal.SIGALRM:
            logger.warning(
                "The scanning process stopped at its own time limit (%s)", stage.value
            )
            return ScanError(scan_child_out_of_time_error(stage, page))
        logger.warning("The scanning process died from %s (%s)", name, stage.value)
        return ScanError(scan_child_crashed_error(stage, page, name))

    def _ensure_child(self) -> None:
        """
        Start a child, and wait for it to be ready, unless one is running.

        A child saneless killed, whose reap an interrupt stopped, is not
        running: it is reaped first, and a new one started.
        """
        if self._kill_sent:
            self._end_child()
        if self._child is not None:
            return
        self._enter(ScanStage.STARTUP)
        self._deadline = time.monotonic() + STARTUP_DEADLINE_SECONDS
        if self._abort is not None and self._abort.is_set():
            raise _AbortedError
        if self._live is not None:
            self._live.set()
        try:
            child = self._start()
        except BaseException:
            if self._live is not None:
                self._live.clear()
            raise
        self._child = child
        poller = select.poll()
        poller.register(child.reply_fd, select.POLLIN)
        self._poller = poller
        self._in_sync = True
        frame = self._next_frame()
        if isinstance(frame, ChildFailure):
            # A scanner library that would not load or start is reported as
            # what it is, not as a reply out of place.
            raise _FailedError(frame)
        if not isinstance(frame, Ready):
            raise ProtocolError(_OUT_OF_PLACE)

    def _receive_pass(self, sink: PageSink) -> ScanBatch:
        """Receive a pass's frames, handing each page to the sink, until it ends."""
        records: list[PageRecord] = []
        while True:
            frame = self._next_frame()
            if isinstance(frame, Configured):
                parameters = page_budget._ScanParameters(
                    frame_format=frame.frame_format,
                    last_frame=frame.last_frame,
                    pixels_per_line=frame.pixels_per_line,
                    lines=frame.lines,
                    depth=frame.depth,
                    bytes_per_line=frame.bytes_per_line,
                )
                self._page_budget = (
                    page_budget._page_budget_seconds(parameters, frame.resolution),
                    page_budget._describe_page(parameters, frame.resolution),
                )
                self._restart_clock()
            elif isinstance(frame, PageHeader):
                records.append(self._take_page(frame, sink))
            elif isinstance(frame, PassDone):
                cap = None if frame.cap is None else PassCapReached(*frame.cap)
                return ScanBatch(
                    pages=tuple(records),
                    actual_resolution=frame.resolution,
                    pages_rejected=frame.rejected,
                    substituted_source=frame.substituted_source,
                    cap_reached=cap,
                )
            elif isinstance(frame, ChildFailure):
                raise _FailedError(frame)
            else:
                raise ProtocolError(_OUT_OF_PLACE)

    def _take_page(self, header: PageHeader, sink: PageSink) -> PageRecord:
        """
        Receive one page into a single buffer, spool it, and acknowledge it.

        The child waits for the acknowledgement before it reads another
        sheet: ``spooled``, or ``stop`` when the sink raised, in which case
        the child is stopped before the sink's error is re-raised.
        """
        child = self._require_child()
        image = receive_page(child.reply_fd, header, self._wait_readable)
        self._in_sync = True
        try:
            record = sink.add(image, dpi=header.dpi)
        except BaseException:
            self._stop_child(ControlOp.STOP, CANCEL_GRACE_SECONDS)
            raise
        del image
        self._send(ControlOp.SPOOLED)
        self._restart_clock()
        return record

    def _next_frame(self) -> Frame:
        """
        Read the next frame that needs an answer, under the current deadline.

        Log records are emitted as they arrive, and stage reports move the
        session's stage and deadline along.
        """
        child = self._require_child()
        while True:
            self._in_sync = False
            frame = read_frame(
                child.reply_fd, self._wait_readable, max_pixels=max_page_pixels()
            )
            self._in_sync = not isinstance(frame, PageHeader)
            if isinstance(frame, LogLine):
                _emit(frame)
            elif isinstance(frame, StageFrame):
                self._note_stage(frame)
                self._start_stage_clock()
            else:
                return frame

    def _note_stage(self, frame: StageFrame) -> None:
        """Record the stage and page the child reports it has entered."""
        self._stage = ScanStage(frame.stage)
        self._page = frame.page

    def _start_stage_clock(self) -> None:
        """
        Start the deadline for the stage just entered.

        The start and the read of one page share the page budget, as they do
        in one call in-process; every other stage gets the stage deadline.
        """
        budget = self._page_budget
        if self._stage is ScanStage.START and budget is not None:
            self._deadline = time.monotonic() + budget[0]
            self._on_page_clock = True
        elif self._stage is ScanStage.READ and budget is not None:
            if not self._on_page_clock:
                self._deadline = time.monotonic() + budget[0]
                self._on_page_clock = True
        else:
            self._restart_clock()

    def _enter(self, stage: ScanStage) -> None:
        """Enter a stage this side starts, with no page, under the stage deadline."""
        self._stage = stage
        self._page = None
        self._restart_clock()

    def _restart_clock(self) -> None:
        """Give the child the stage deadline from now."""
        self._deadline = time.monotonic() + STAGE_DEADLINE_SECONDS
        self._on_page_clock = False

    def _wait_readable(self) -> None:
        """
        Block until the reply channel is readable, in short slices.

        While a stopping child is waited for, the abort is not raised; it
        cuts the wait to the cancel grace instead, so a server stop never
        waits out a longer deadline.

        Raises:
            _AbortedError: The abort Event was set, and it is being watched.
            _DeadlineError: The current deadline passed.

        """
        poller = self._poller
        if poller is None:
            raise ChildGoneError(_GONE)
        while True:
            if self._abort is not None and self._abort.is_set():
                if self._watch_abort:
                    raise _AbortedError
                self._cut_for_abort()
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise _DeadlineError
            wait = min(_ABORT_POLL_SECONDS, remaining)
            if poller.poll(max(1, math.ceil(wait * 1000))):
                return

    def _send(self, command: ScanCommand | ControlOp) -> None:
        """
        Write one command to the child.

        Raises:
            ChildGoneError: The child closed its stdin, so it is gone.

        """
        child = self._require_child()
        view = memoryview(encode_command(command))
        try:
            while view:
                view = view[os.write(child.command_fd, view) :]
        except BrokenPipeError:
            raise ChildGoneError(_GONE) from None

    def _stop_child(self, op: ControlOp, seconds: float) -> int | None:
        """
        Ask the child to stop, give it ``seconds`` to exit, then kill it.

        The one path that a cancel, a stop, an exit, an abort, an interrupt
        and a page that overran all take.  Nothing happens without a child.

        Returns:
            The child's exit status if it exited by itself, or ``None`` when
            it had to be killed (or there was no child).

        """
        if self._child is None:
            return None
        # A child already gone cannot read it; the wait below reaps it.
        with contextlib.suppress(ChildGoneError):
            self._send(op)
        return self._finish_child(seconds)

    def _finish_child(self, seconds: float) -> int | None:
        """
        Wait up to ``seconds`` for the child to exit, then kill it, and reap it.

        What the child writes meanwhile is read, so a child blocked on a full
        reply pipe can still exit; its log records are kept, the rest is
        discarded.  An interrupt during the wait kills the child at once.

        Returns:
            The child's exit status if it exited by itself, else ``None``.

        """
        child = self._child
        if child is None:
            return None
        self._deadline = time.monotonic() + seconds
        self._watch_abort = False
        try:
            status = self._drain_until_exit(child)
        except BaseException:
            self._end_child()
            raise
        finally:
            self._watch_abort = True
        self._end_child()
        return status

    def _drain_until_exit(self, child: ChildProcess) -> int | None:
        """
        Read and drop the child's replies until it exits or the deadline passes.

        Returns:
            The child's exit status, or ``None`` if it is still running.

        """
        while True:
            status = child.poll()
            if status is not None:
                return status
            try:
                if self._in_sync:
                    self._drain_frame(child)
                else:
                    self._wait_readable()
                    if not os.read(child.reply_fd, _DRAIN_BYTES):
                        raise ChildGoneError(_GONE)
            except ProtocolError:
                self._in_sync = False
            except ChildGoneError:
                return self._wait_for_exit(child)
            except _DeadlineError:
                return child.poll()

    def _cut_for_abort(self) -> None:
        """Hold the wait on a stopping child to the cancel grace from now."""
        self._deadline = min(self._deadline, time.monotonic() + CANCEL_GRACE_SECONDS)

    def _wait_for_exit(self, child: ChildProcess) -> int | None:
        """
        Wait, in short slices, for a child whose reply channel has closed.

        An abort cuts the wait to the cancel grace, as it does while the
        child's replies are drained.

        Returns:
            The child's exit status, or ``None`` if it is still running.

        """
        while True:
            if self._abort is not None and self._abort.is_set():
                self._cut_for_abort()
            remaining = self._deadline - time.monotonic()
            status = child.wait(max(0.0, min(_ABORT_POLL_SECONDS, remaining)))
            if status is not None or remaining <= 0:
                return status

    def _drain_frame(self, child: ChildProcess) -> None:
        """Read one frame from a stopping child, keeping only its log records."""
        self._in_sync = False
        frame = read_frame(
            child.reply_fd, self._wait_readable, max_pixels=max_page_pixels()
        )
        if isinstance(frame, PageHeader):
            remaining = frame.nbytes
            while remaining:
                self._wait_readable()
                chunk = os.read(child.reply_fd, min(remaining, _DRAIN_BYTES))
                if not chunk:
                    raise ChildGoneError(_GONE)
                remaining -= len(chunk)
        elif isinstance(frame, LogLine):
            _emit(frame)
        elif isinstance(frame, StageFrame):
            self._note_stage(frame)
        self._in_sync = True

    def _end_child(self) -> None:
        """
        Kill the child if it still runs, reap it and close its channels.

        The child is forgotten only once it is reaped: an interrupt during the
        kill or the reap leaves it in place, so the session's next end of the
        child, its next pass or restart, or its close, kills and reaps it
        again.  The kill is counted as it is sent, so a child an interrupted
        kill left to be reaped later is still counted, once.
        """
        child = self._child
        if child is None:
            return
        if child.poll() is None:
            if not self._kill_sent:
                self._kill_sent = True
                self._killed += 1
            child.kill_and_reap()
        self._child = None
        self._kill_sent = False
        self._poller = None
        try:
            child.close()
        finally:
            if self._live is not None:
                self._live.clear()

    def _require_child(self) -> ChildProcess:
        """
        Return the running child.

        Raises:
            ChildGoneError: There is none, because it was already reaped.

        """
        if self._child is None:
            raise ChildGoneError(_GONE)
        return self._child
