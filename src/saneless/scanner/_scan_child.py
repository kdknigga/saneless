"""
Run a scan job's SANE session in a process of its own.

Every SANE call of a scan job runs in this file, in a child process saneless
starts for the job, so a hang inside libsane costs only this process: saneless
owns every deadline and kills and reaps a child that overruns one.  The child
serves every pass of the job, holds no device between passes, and restarts
SANE when asked.  See
docs/explanation/decisions/0016-scan-sessions-in-a-child-process.md.

saneless sends commands on stdin, one ASCII JSON line each, and the child
answers with length-prefixed frames; ``scan_protocol`` holds both formats.
In brief: the child says ``ready`` once SANE is initialised, then serves
commands until it is told to go.  A ``scan`` runs one pass and reports each
stage it enters, the device's parameters (``configured``), every accepted page
(a header, then the page's ``tobytes()`` pixels, made and written in strips of
whole rows) and the pass's end
(``pass_done``).  After each page the child waits for ``spooled`` before it
reads another sheet, or for ``stop`` or ``cancel``, which end the pass and the
child.  ``restart`` restarts SANE (``restarted``); ``exit``, and ``stop`` or
``cancel`` while idle, end the child (``bye``).  A scan error or an empty
feeder is reported as a non-fatal ``error`` and the child stays ready; any
other failure is a fatal ``error`` and the child ends.

The reply channel is private: before the thread unwinder, python-sane or any
SANE call is loaded or run, the child moves the parent's stdout pipe to a
descriptor of its own and points fd 1 at stderr, so a backend's C stdio
cannot write into a frame.  The modules this file imports at load time
(saneless's own, and Pillow) run first, while fd 1 is still the pipe; none of
them writes to stdout or opens a descriptor when imported.  One lock serialises every
write to it.  A page whose pixels fail part way leaves the channel mid-page,
where any later frame would be read as pixels, so nothing more is written to
it.

The C library's thread unwinder is loaded next, after the channel is private
and before python-sane is imported, so no backend thread is ever the first to
load it.  Its loader is imported late, by name: it imports ``ctypes``, which
opens a descriptor that would take a closed fd 2 before the channel is made
private.  python-sane itself is reached only through ``scan_session``.

The main thread makes every SANE call.  A control thread owns stdin: a
``cancel``, or the end of stdin when saneless has gone, cancels the handle
being read, which python-sane allows from another thread, and the main thread
then ends the pass.  When stdin ends the control thread also gives the main
thread a grace period and then ends the process itself.  A SANE call that
holds the interpreter lock (``sane_init``, ``sane_close``, ``sane_exit`` and
option reads and writes) stops the control thread too, so an alarm is armed
before every stage that calls into SANE and cleared while the child is idle;
the default action for ``SIGALRM`` ends the process even inside a blocking C
call.  The alarms outlast saneless's own deadlines, so they only ever fire
for a child whose parent has died.  libsane sets SIGPIPE back to its
default action inside SANE calls, so the main thread ignores it again before
every frame it writes, and a write to a saneless that has gone fails instead
of killing the child before it cancels and closes the device.

Log records of saneless's own loggers travel to saneless as ``log`` frames,
at the level saneless asked for.  The child ends with ``os._exit`` once its
last frame is written, because a crash or hang in teardown would otherwise
turn a good session into a failed one.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import json
import logging
import os
import queue
import signal
import sys
import threading
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, BinaryIO, Final, get_args, get_type_hints

from saneless.exceptions import (
    ConfigError,
    SanelessError,
    ScanError,
    describe,
)
from saneless.pages import allow_large_scans
from saneless.scanner import scan_session
from saneless.scanner._child_stdio import flush_standard_streams, take_reply_fd
from saneless.scanner.base import ScanSettings
from saneless.scanner.scan_protocol import (
    LOG_LEVELS,
    PAGE_BANDS,
    PAGE_STRIP_BYTES,
    Bye,
    ChildFailure,
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
    decode_command,
    encode_frame,
    max_page_pixels,
)
from saneless.vocabulary import (
    PAGE_TOO_LARGE_NEXT_STEP,
    PYTHON_SANE_INSTALL_NEXT_STEP,
    PaperSize,
    ScanStage,
    page_too_large_error,
    python_sane_missing_message,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from PIL import Image

    from saneless.scanner.page_budget import _ScanParameters
    from saneless.scanner.scan_protocol import Frame
    from saneless.scanner.scan_session import SaneDevice

__all__ = ["ChildRuntime", "install_log_forwarding", "main", "prepare_process"]

_STAGE_ALARM_SECONDS: Final = 35
"""The alarm for a stage that is not a page: saneless's 30 s deadline, plus 5."""

_READ_ALARM_SECONDS: Final = 3615
"""
The alarm for starting and reading a page.

The longest page budget saneless gives, 3600 s, plus its 10 s cancel grace,
plus 5.
"""

_PARENT_GONE_GRACE_SECONDS: Final = 10.0
"""How long the main thread has to end after stdin ends: saneless's grace."""

_PARENT_GONE_STATUS: Final = 3
"""The exit status of a child that ended because saneless had gone."""

_BAD_COMMAND_STATUS: Final = 2
"""The exit status after a command line off the schema."""

_FAILED_STATUS: Final = 1
"""The exit status after a fatal failure, once its error frame is sent."""

_MAX_COMMAND_BYTES: Final = 1 << 20
"""The longest command line read; a longer one is off the schema."""

_MAX_LOG_BYTES: Final = 32_000
"""
The longest a log message may escape to, as JSON, in bytes.

Measured escaped, not in characters, because a character outside the Basic
Multilingual Plane escapes to twelve bytes: the frame stays well under the
protocol's 64 KiB header limit whatever the text.
"""

_MAX_FAILURE_TEXT_BYTES: Final = 16_000
"""
The longest an error frame's message or next step may escape to, as JSON.

Two of them, with the type name and the rest of the frame, stay well under
the protocol's 64 KiB header limit, so a long or device-supplied text never
stops the error itself from being sent.
"""

_CONTROL_THREAD_NAME: Final = "saneless-scan-control"

_STDERR_FD: Final = 2
"""The descriptor the child inherits as saneless's stderr."""

_CLEANUP_STAGES: Final = frozenset({ScanStage.CANCEL, ScanStage.CLOSE})
"""The stages that end every pass; nothing in them raises."""

_PAGE_STAGES: Final = frozenset({ScanStage.START, ScanStage.READ})
"""
The stages saneless times against the page budget.

A page's start may wait as long as its read (a feeder pulling the sheet, a
lamp warming up), so both get the read alarm.
"""

# The scan settings' annotations, resolved: the Literal choices are read from
# ScanSettings itself, so the check below cannot drift from it.
_SETTING_TYPES: Final = get_type_hints(ScanSettings, localns={"PaperSize": PaperSize})
_SETTING_NAMES: Final = frozenset(
    field.name for field in dataclasses.fields(ScanSettings)
)


class _Signal(Enum):
    """What the control thread hands the main thread besides commands."""

    END = "end"
    """stdin ended: saneless has gone."""

    BAD = "bad"
    """A line that is not exactly one command."""


type _Item = ScanCommand | ControlOp | _Signal


class _OffSchemaError(Exception):
    """A scan command's settings are not exactly a ``ScanSettings``."""


class _ReplyClosedError(Exception):
    """
    The reply channel would not take a write.

    saneless has gone, or a page was cut short and the channel left mid-page.
    """


@dataclass(frozen=True, slots=True)
class ChildRuntime:
    """
    The process-wide actions the child takes, so a test can record them.

    Attributes:
        arm_alarm: Arms the process alarm for so many seconds, or clears it
            with 0: ``signal.alarm`` in the real child.
        exit_process: Ends the process at once with a status: ``os._exit``.
        grace_seconds: How long the main thread has to end once stdin ends,
            before the process is ended.
        ignore_sigpipe: Sets SIGPIPE to be ignored again, called before
            every frame the thread running ``main`` writes:
            ``_ignore_sigpipe`` in the real child.

    """

    arm_alarm: Callable[[int], object]
    exit_process: Callable[[int], None]
    grace_seconds: float
    ignore_sigpipe: Callable[[], object]


def _ignore_sigpipe() -> None:
    """Ignore SIGPIPE again, as Python set it when the child started."""
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)


def _keep_sigpipe() -> None:
    """Leave SIGPIPE as it is: the default for a channel no test watches."""


class _ReplyChannel:
    """
    The private pipe to saneless, written under one lock.

    libsane sets SIGPIPE back to its default action inside SANE calls, after
    a read that fails and after one that succeeds, so a write to a saneless
    that has gone would kill the child before it could cancel and close the
    device.  The channel therefore ignores SIGPIPE again before every frame
    it writes from the thread that made it, the one that runs ``main`` and
    makes every SANE call; only that thread may set a signal's action.  No
    other thread writes: the control thread sends nothing, and the log
    forwarder drops a record logged on any other thread.

    Attributes:
        forward_logs: Whether saneless's log records are sent as frames.
            Only the real child process does this.

    """

    def __init__(
        self,
        fd: int,
        *,
        forward_logs: bool = False,
        ignore_sigpipe: Callable[[], object] = _keep_sigpipe,
    ) -> None:
        """
        Wrap the reply descriptor, for the calling thread to write.

        Args:
            fd: The descriptor; the caller keeps ownership.
            forward_logs: Whether log records are to be forwarded.
            ignore_sigpipe: Sets SIGPIPE to be ignored again; called before
                each frame the calling thread writes.

        """
        self._fd = fd
        self._lock = threading.Lock()
        self._torn = False
        self._owner = threading.get_ident()
        self._ignore_sigpipe = ignore_sigpipe
        self.forward_logs = forward_logs

    def on_owner_thread(self) -> bool:
        """
        Report whether the calling thread is the one that made the channel.

        Returns:
            True on the thread that runs ``main``.

        """
        return threading.get_ident() == self._owner

    def send(self, frame: Frame) -> None:
        """
        Write one frame.

        Args:
            frame: The frame.

        Raises:
            _ReplyClosedError: The pipe is closed, or a page was cut short.

        """
        header = encode_frame(frame)
        with self._lock:
            self._before_frame()
            self._write(header)

    def send_page(self, header: PageHeader, strips: Iterable[bytes]) -> None:
        """
        Write a page's header, then its pixels strip by strip, as one unit.

        Each strip is written as soon as it is made.  If making one fails, the
        pixels already written leave saneless mid-page, so the channel takes
        no more frames and the failure goes on.

        Args:
            header: The page's header.
            strips: The page's pixels, in order, adding up to ``nbytes``.

        Raises:
            _ReplyClosedError: The pipe is closed, or a page was cut short.

        """
        data = encode_frame(header)
        with self._lock:
            self._before_frame()
            self._write(data)
            try:
                for strip in strips:
                    self._write(strip)
            except _ReplyClosedError:
                raise
            except BaseException as exc:
                self._torn = True
                _note_cut_short(exc)
                raise

    def _before_frame(self) -> None:
        """Ignore SIGPIPE again, on the owner thread, before a frame."""
        if self.on_owner_thread():
            self._ignore_sigpipe()

    def _write(self, data: bytes) -> None:
        """
        Write all of ``data``.

        Raises:
            _ReplyClosedError: The pipe is closed, or a page was cut short.

        """
        if self._torn:
            raise _ReplyClosedError
        view = memoryview(data)
        try:
            while view:
                view = view[os.write(self._fd, view) :]
        except OSError as exc:
            raise _ReplyClosedError from exc
        finally:
            view.release()


def _note_cut_short(exc: BaseException) -> None:
    """
    Tell stderr, by type only, why a page was cut short.

    Nothing more can reach saneless on the reply channel, so this line is
    the only trace of the cause.  The exception's text is left out: it can
    name a device.
    """
    line = f"saneless scan child: a page was cut short by {type(exc).__name__}\n"
    with contextlib.suppress(OSError):
        os.write(_STDERR_FD, line.encode("ascii", "replace"))


class _LogForwarder(logging.Handler):
    """Send each record to saneless as a ``log`` frame."""

    def __init__(self, reply: _ReplyChannel) -> None:
        """
        Forward to ``reply``.

        Args:
            reply: The reply channel.

        """
        super().__init__()
        self._reply = reply

    def emit(self, record: logging.LogRecord) -> None:
        """
        Send one record, at the nearest standard level at or below its own.

        A record logged on any thread but the one that runs ``main`` is
        dropped: that thread could not ignore SIGPIPE before the write.

        Args:
            record: The record.

        """
        if not self._reply.on_owner_thread():
            return
        try:
            message = _fit(self.format(record), _MAX_LOG_BYTES)
            self._reply.send(
                LogLine(
                    level=_frame_level(record.levelno),
                    logger=record.name,
                    message=message,
                )
            )
        except _ReplyClosedError, ProtocolError, ValueError, TypeError:
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:
        """
        Drop a record that could not be sent, saying nothing.

        The default prints the record's message and arguments, which can name
        a device, to stderr, outside saneless's logging; and once the channel
        is closed, it would do so for every record.

        Args:
            record: The record dropped.

        """
        del record


def _frame_level(levelno: int) -> int:
    """
    Map a record's level to one a ``log`` frame may carry.

    Returns:
        The highest standard level not above ``levelno``, or DEBUG.

    """
    below = [level for level in LOG_LEVELS if level <= levelno]
    return max(below) if below else min(LOG_LEVELS)


def install_log_forwarding(reply: _ReplyChannel, level: int) -> None:
    """
    Forward saneless's log records to saneless, from ``level`` up.

    Args:
        reply: The reply channel.
        level: saneless's own effective level; a level of 0 sends DEBUG up.

    """
    root = logging.getLogger("saneless")
    root.setLevel(max(level, logging.DEBUG))
    if not any(isinstance(handler, _LogForwarder) for handler in root.handlers):
        root.addHandler(_LogForwarder(reply))


class _Control:
    """
    The control thread, and what it shares with the main thread.

    Attributes:
        cancelled: Set by a ``cancel`` or the end of stdin.
        parent_gone: Set when stdin ends.
        main_done: Set by the main thread as ``main`` returns.

    """

    def __init__(self, commands: BinaryIO, runtime: ChildRuntime) -> None:
        """
        Prepare the control thread; ``start`` runs it.

        Args:
            commands: saneless's command stream.
            runtime: The process-wide actions.

        """
        self._commands = commands
        self._runtime = runtime
        self._items: queue.SimpleQueue[_Item] = queue.SimpleQueue()
        self._handle_lock = threading.Lock()
        self._handle: SaneDevice | None = None
        self.cancelled = threading.Event()
        self.parent_gone = threading.Event()
        self.main_done = threading.Event()

    def start(self) -> None:
        """Start reading commands."""
        threading.Thread(
            target=self._run, name=_CONTROL_THREAD_NAME, daemon=True
        ).start()

    def next(self) -> _Item:
        """
        Wait for the next command.

        Returns:
            The command, or what the control thread made of stdin.

        """
        return self._items.get()

    def reading(self, dev: SaneDevice | None) -> None:
        """
        Note the handle a start or read is in progress on, or None after it.

        Under the same lock the cancel takes, so a cancel never reaches a
        handle whose call has already returned.
        """
        with self._handle_lock:
            self._handle = dev

    def _cancel(self) -> None:
        """Ask the pass to stop, and cancel the handle being read, if any."""
        self.cancelled.set()
        with self._handle_lock:
            dev = self._handle
            if dev is not None:
                # python-sane raises _sane.error, RuntimeError or
                # AttributeError with no shared base; a failed cancel only
                # leaves the read to end on its own.
                with contextlib.suppress(Exception):
                    dev.cancel()

    def _run(self) -> None:
        """Read commands until stdin ends, then end the process if needed."""
        while True:
            try:
                line = self._commands.readline(_MAX_COMMAND_BYTES)
            except OSError, ValueError:
                line = b""
            if not line:
                break
            command = decode_command(line) if line.endswith(b"\n") else None
            if command is ControlOp.CANCEL:
                self._cancel()
            self._items.put(_Signal.BAD if command is None else command)
        self.parent_gone.set()
        self._cancel()
        self._items.put(_Signal.END)
        if not self.main_done.wait(self._runtime.grace_seconds):
            self._runtime.exit_process(_PARENT_GONE_STATUS)


class _ChildOutlet:
    """
    A pass's outlet onto the reply channel, in lockstep with saneless.

    Attributes:
        error_stage: The stage a failure of the pass comes from: the last
            stage entered before the device is cancelled and closed, since
            neither raises.
        error_page: The page that stage concerns, if any.
        bad_command: Whether a page was answered with something other than
            ``spooled``, ``stop`` or ``cancel``.

    """

    def __init__(
        self, reply: _ReplyChannel, control: _Control, runtime: ChildRuntime
    ) -> None:
        """
        Prepare the outlet for one pass.

        Args:
            reply: The reply channel.
            control: The control thread's shared state.
            runtime: The process-wide actions.

        """
        self._reply = reply
        self._control = control
        self._runtime = runtime
        self.error_stage = ScanStage.OPEN
        self.error_page: int | None = None
        self.bad_command = False

    def stage(self, stage: ScanStage, page: int | None) -> None:
        """
        Arm the alarm for the stage, and report it.

        Args:
            stage: The stage about to begin.
            page: The one-based sheet it concerns, or None.

        """
        seconds = _READ_ALARM_SECONDS if stage in _PAGE_STAGES else _STAGE_ALARM_SECONDS
        self._runtime.arm_alarm(seconds)
        if stage not in _CLEANUP_STAGES:
            self.error_stage = stage
            self.error_page = page
        self._reply.send(StageFrame(stage=stage.value, page=page))

    def configured(
        self, parameters: _ScanParameters, *, resolution: int, use_adf: bool
    ) -> None:
        """
        Report the device's parameters, from which saneless sets its budget.

        Args:
            parameters: The scan parameters the device reports.
            resolution: The resolution the device read back, in dpi.
            use_adf: Whether the pass reads from the document feeder.

        """
        self._reply.send(
            Configured(
                resolution=resolution,
                frame_format=parameters.frame_format,
                last_frame=parameters.last_frame,
                pixels_per_line=parameters.pixels_per_line,
                lines=parameters.lines,
                depth=parameters.depth,
                bytes_per_line=parameters.bytes_per_line,
                use_adf=use_adf,
            )
        )

    def page(self, image: Image.Image, *, number: int, dpi: int) -> bool:
        """
        Send one page, then wait, idle, for saneless's answer.

        Args:
            image: The accepted, cropped page.
            number: The one-based number of its sheet.
            dpi: The resolution to record it at.

        Returns:
            True once saneless spooled it; False to stop the pass.

        Raises:
            ScanError: The page has more pixels than saneless can keep, so
                it is not sent.

        """
        self._runtime.arm_alarm(0)
        limit = max_page_pixels()
        if image.width * image.height > limit:
            raise ScanError(
                page_too_large_error(number, image.width, image.height, limit),
                next_step=PAGE_TOO_LARGE_NEXT_STEP,
            )
        nbytes, strips = _pixels(image)
        header = PageHeader(
            number=number,
            mode=image.mode,
            width=image.width,
            height=image.height,
            dpi=dpi,
            nbytes=nbytes,
        )
        self._reply.send_page(header, strips)
        answer = self._control.next()
        if answer is ControlOp.SPOOLED:
            return True
        if answer not in {ControlOp.STOP, ControlOp.CANCEL, _Signal.END}:
            self.bad_command = True
        return False

    def reading(self, dev: SaneDevice | None) -> None:
        """
        Note the handle a start or read is in progress on, or None after it.

        Args:
            dev: The handle, or None once the call has returned.

        """
        self._control.reading(dev)

    def cancel_requested(self) -> bool:
        """
        Report whether a cancel arrived, or saneless has gone.

        Returns:
            True once the pass should stop.

        """
        return self._control.cancelled.is_set()


def _pixels(image: Image.Image) -> tuple[int, Iterator[bytes]]:
    """
    Size a page's raw pixels, and make them a strip at a time.

    Each strip is ``tobytes()`` of a band of whole rows, about
    ``PAGE_STRIP_BYTES`` long, top to bottom: together they are exactly the
    bytes ``image.tobytes()`` gives, with no page-sized copy joined from
    them.  A mode saneless does not take goes in one piece, and saneless
    refuses its header.

    Returns:
        The byte count, and the strips, made only as they are taken.

    """
    bands = PAGE_BANDS.get(image.mode)
    if bands is None:
        pixels = image.tobytes()
        return len(pixels), iter((pixels,))
    width, height = image.size
    rows = max(1, PAGE_STRIP_BYTES // max(1, width * bands))
    strips = (
        image.crop((0, top, width, min(height, top + rows))).tobytes()
        for top in range(0, height, rows)
    )
    return width * height * bands, strips


def _choice[T](raw: Mapping[str, str | int], name: str, choices: tuple[T, ...]) -> T:
    """
    Return the setting ``name`` if it is one of ``choices``.

    Raises:
        _OffSchemaError: It is not.

    """
    value = raw[name]
    for choice in choices:
        if value == choice:
            return choice
    raise _OffSchemaError


def _text(raw: Mapping[str, str | int], name: str) -> str:
    """
    Return the setting ``name`` if it is a string.

    Raises:
        _OffSchemaError: It is not.

    """
    value = raw[name]
    if not isinstance(value, str):
        raise _OffSchemaError
    return value


def _settings(raw: Mapping[str, str | int]) -> ScanSettings:
    """
    Rebuild the pass's settings from a scan command.

    Raises:
        _OffSchemaError: The keys are not exactly ScanSettings' fields, or a
            value is of the wrong type or not one of its field's choices.

    """
    if raw.keys() != _SETTING_NAMES:
        raise _OffSchemaError
    resolution = raw["resolution"]
    if not isinstance(resolution, int) or isinstance(resolution, bool):
        raise _OffSchemaError
    return ScanSettings(
        source=_text(raw, "source"),
        resolution=resolution,
        mode=_text(raw, "mode"),
        auto_source_mode=_choice(
            raw, "auto_source_mode", get_args(_SETTING_TYPES["auto_source_mode"])
        ),
        duplex=_choice(raw, "duplex", get_args(_SETTING_TYPES["duplex"])),
        paper_size=_choice(raw, "paper_size", get_args(PaperSize)),
    )


def _fit(text: str, limit: int) -> str:
    """
    Cut ``text`` short until it escapes, as JSON, to at most ``limit`` bytes.

    Returns:
        ``text`` itself when it fits, otherwise its longest start that does.

    """
    size = len(json.dumps(text, ensure_ascii=True))
    while size > limit:
        # Each pass keeps a share of the characters in proportion to the
        # bytes allowed, and always at least one fewer, so it soon fits.
        keep = min(len(text) - 1, len(text) * limit // size)
        text = text[:keep]
        size = len(json.dumps(text, ensure_ascii=True))
    return text


def _failure(
    exc: Exception, stage: ScanStage, page: int | None, *, fatal: bool
) -> ChildFailure:
    """
    Describe an exception as the error frame saneless reads.

    The message and the next step are cut short to fit the frame, so a long
    text, such as a device's list of its sources, cannot stop it being sent.

    Returns:
        The frame: saneless's own errors with their text and next step,
        anything else by its type and a one-line description.

    """
    if isinstance(exc, SanelessError):
        message, next_step = str(exc), exc.next_step
    else:
        message, next_step = describe(exc), None
    return ChildFailure(
        type_name=type(exc).__name__,
        message=_fit(message, _MAX_FAILURE_TEXT_BYTES),
        next_step=None
        if next_step is None
        else _fit(next_step, _MAX_FAILURE_TEXT_BYTES),
        stage=stage.value,
        page=page,
        fatal=fatal,
    )


def _start_library() -> Exception | None:
    """
    Load python-sane and initialise SANE.

    Returns:
        None once SANE is running, or the error to report: a ``ConfigError``
        with the install hint when python-sane cannot be imported, or what
        the start raised.

    """
    try:
        scan_session._ensure_sane()
    except ImportError as exc:
        return ConfigError(
            python_sane_missing_message(describe(exc)),
            next_step=PYTHON_SANE_INSTALL_NEXT_STEP,
        )
    try:
        scan_session.start_library()
    except Exception as exc:
        return exc
    return None


def _scan(
    command: ScanCommand,
    reply: _ReplyChannel,
    control: _Control,
    runtime: ChildRuntime,
) -> int | None:
    """
    Run one pass and report how it ended.

    Returns:
        None when the child stays ready, or the status ``main`` returns.

    """
    try:
        settings = _settings(command.settings)
    except _OffSchemaError:
        return _BAD_COMMAND_STATUS
    if reply.forward_logs:
        install_log_forwarding(reply, command.log_level)
    outlet = _ChildOutlet(reply, control, runtime)
    try:
        outcome = scan_session.run_pass(command.device, settings, outlet)
    except scan_session.PassStopped:
        runtime.arm_alarm(0)
        if outlet.bad_command:
            return _BAD_COMMAND_STATUS
        if not control.parent_gone.is_set():
            reply.send(Bye())
        return 0
    except _ReplyClosedError:
        raise
    except ScanError as exc:
        # FeederEmptyError is a ScanError: the pass failed, the child did not.
        runtime.arm_alarm(0)
        reply.send(_failure(exc, outlet.error_stage, outlet.error_page, fatal=False))
        return None
    except Exception as exc:
        runtime.arm_alarm(0)
        reply.send(_failure(exc, outlet.error_stage, outlet.error_page, fatal=True))
        return _FAILED_STATUS
    runtime.arm_alarm(0)
    cap = outcome.cap_reached
    reply.send(
        PassDone(
            resolution=outcome.resolution,
            rejected=outcome.rejected,
            substituted_source=outcome.substituted_source,
            cap=None if cap is None else (cap.cap, cap.sheet_not_kept, cap.auto_source),
        )
    )
    return None


def _restart(reply: _ReplyChannel, runtime: ChildRuntime) -> int | None:
    """
    Restart SANE between passes.

    Returns:
        None when the child stays ready, or ``_FAILED_STATUS``.

    """
    runtime.arm_alarm(_STAGE_ALARM_SECONDS)
    try:
        scan_session.restart_library()
    except Exception as exc:
        reply.send(_failure(exc, ScanStage.RESTART, None, fatal=True))
        return _FAILED_STATUS
    reply.send(Restarted())
    runtime.arm_alarm(0)
    return None


def _serve(reply: _ReplyChannel, control: _Control, runtime: ChildRuntime) -> int:
    """
    Start SANE, say ready, and serve commands until told to go.

    Returns:
        The status ``main`` returns.

    """
    runtime.arm_alarm(_STAGE_ALARM_SECONDS)
    failure = _start_library()
    if failure is not None:
        reply.send(_failure(failure, ScanStage.STARTUP, None, fatal=True))
        return _FAILED_STATUS
    reply.send(Ready())
    runtime.arm_alarm(0)
    control.start()
    while True:
        item = control.next()
        status: int | None
        if isinstance(item, ScanCommand):
            status = _scan(item, reply, control, runtime)
        elif item is ControlOp.RESTART:
            status = _restart(reply, runtime)
        elif item in {ControlOp.EXIT, ControlOp.STOP, ControlOp.CANCEL}:
            reply.send(Bye())
            status = 0
        elif item is _Signal.END:
            status = 0
        else:
            # A line off the schema, or a page answer with no page waiting.
            status = _BAD_COMMAND_STATUS
        if status is not None:
            return status


def prepare_process() -> None:
    """
    Make the process ready for python-sane, without importing it.

    The C library's thread unwinder is loaded first, so no backend thread
    is ever the first to load it, and Pillow's image-size limit is raised to
    saneless's own.  The loader is imported here, not at the top of the file,
    because it imports ``ctypes``; call this once the reply channel is
    private.
    """
    importlib.import_module("saneless.thread_unwinder").load_thread_unwinder()
    allow_large_scans()


def main(
    commands: BinaryIO,
    reply_fd: int,
    runtime: ChildRuntime,
    *,
    forward_logs: bool = False,
) -> int:
    """
    Run the child: start SANE, say ready, and serve saneless's commands.

    Args:
        commands: saneless's command stream; a control thread reads it.
        reply_fd: The private reply descriptor; the caller keeps ownership.
        runtime: The alarm, the process exit and the grace.
        forward_logs: Whether saneless's log records are sent as frames;
            only the real child process sends them.

    Returns:
        0 after ``bye``, or when saneless has gone; 1 after a fatal error
        frame, or an exception no error frame could carry; 2 after a command
        off the schema; 3 when a reply could not be written because saneless
        had gone or a page was cut short.

    """
    reply = _ReplyChannel(
        reply_fd, forward_logs=forward_logs, ignore_sigpipe=runtime.ignore_sigpipe
    )
    control = _Control(commands, runtime)
    try:
        return _serve(reply, control, runtime)
    except _ReplyClosedError:
        return _PARENT_GONE_STATUS
    except Exception as exc:
        # Raised outside any pass, or while its error frame was sent: saneless
        # sees the child end with this status, and stderr names the type.
        _note_unexpected(exc)
        return _FAILED_STATUS
    finally:
        control.main_done.set()


def _note_unexpected(exc: BaseException) -> None:
    """Tell stderr, by type only, what ended the child unexpectedly."""
    line = f"saneless scan child: ended by {type(exc).__name__}\n"
    with contextlib.suppress(OSError):
        os.write(_STDERR_FD, line.encode("ascii", "replace"))


if __name__ == "__main__":
    exit_status = _FAILED_STATUS
    try:
        reply_channel_fd = take_reply_fd()
        prepare_process()
        exit_status = main(
            sys.stdin.buffer,
            reply_channel_fd,
            ChildRuntime(
                arm_alarm=signal.alarm,
                exit_process=os._exit,
                grace_seconds=_PARENT_GONE_GRACE_SECONDS,
                ignore_sigpipe=_ignore_sigpipe,
            ),
            forward_logs=True,
        )
    except BaseException as exc:
        _note_unexpected(exc)
    finally:
        # Every frame is written, so skip teardown, whose crash or hang would
        # turn a good session into a failed one.  The kernel closes the
        # sockets and USB handles.
        flush_standard_streams()
        os._exit(exit_status)
