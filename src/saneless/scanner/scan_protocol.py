"""
The wire format between saneless and the child process that scans.

saneless commands the child on its stdin with one ASCII JSON object per line,
keyed by ``op``: ``scan`` (the device id, the settings and the log level),
then ``spooled``, ``stop``, ``cancel``, ``restart`` and ``exit``.  The device
id travels only inside ``scan``, never in argv, which any local user can read.

The child answers on a private pipe.  Each message is a 4-byte big-endian
length followed by that many bytes of an ASCII JSON object keyed by ``kind``.
A ``page`` message is followed by exactly its ``nbytes`` of raw pixels, the
bytes the child's ``image.tobytes()`` produced.  JSON lines alone would need
the pixels in base64, a third larger and a second copy; the length prefix lets
them stay raw and be read in place with ``os.readv``.

Nothing the child writes can run in saneless: the messages are JSON, never
pickle, and every one is checked against its schema (exact keys, ints that
are not bools, allowlisted modes, stages and log levels, a pixel count and a
byte count that agree) before anything is allocated for a page.  A message
off the schema is a ``ProtocolError``, which the caller treats as no answer.

saneless receives each page into one buffer of exactly the announced size,
fresh for each page, and never joins chunks or copies it with ``tobytes()``.
A grey (``L``) image shares that buffer.  Pillow keeps colour pixels four
bytes apiece, so for an ``RGB`` page ``Image.frombuffer`` unpacks into memory
Pillow allocates itself; that copy is Pillow's, outside what saneless holds.

Both saneless and the child import this module, so it depends on the standard
library and Pillow only: never on python-sane, and never on the rest of
saneless.
"""

from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass, fields
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast

from PIL import Image

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = [
    "LENGTH_PREFIX",
    "LOG_LEVELS",
    "MAX_HEADER_BYTES",
    "PAGE_BANDS",
    "STAGES",
    "Bye",
    "ChildFailure",
    "ChildGoneError",
    "Configured",
    "ControlOp",
    "Frame",
    "LogLine",
    "PageHeader",
    "PassDone",
    "ProtocolError",
    "Ready",
    "Restarted",
    "ScanCommand",
    "StageFrame",
    "decode_command",
    "encode_command",
    "encode_frame",
    "read_frame",
    "receive_page",
]

# The length of each message header, ahead of its JSON.
LENGTH_PREFIX: Final = struct.Struct("!I")

# The longest header accepted.  It is refused on the prefix alone, before its
# body is read, so a child cannot make saneless buffer an unbounded message.
MAX_HEADER_BYTES: Final = 65_536

# The image modes a page may have, with their bytes per pixel.  python-sane
# only produces these: one-bit lineart is expanded to 8-bit grey, and
# three-pass frames are merged into one colour image.
PAGE_BANDS: Final[Mapping[str, int]] = MappingProxyType({"L": 1, "RGB": 3})

# Where in a scan session the child can be, as reported in ``stage`` and
# ``error`` messages.
STAGES: Final = frozenset(
    {
        "startup",
        "open",
        "configure",
        "start",
        "read",
        "cancel",
        "close",
        "restart",
        "exit",
    }
)

# The standard logging levels, DEBUG to CRITICAL: the only ones a child's log
# message may carry.
LOG_LEVELS: Final = frozenset({10, 20, 30, 40, 50})

_LOGGER_ROOT: Final = "saneless"
_OFF_SCHEMA: Final = "The scan child sent a message saneless does not understand"
_GONE: Final = "The scan child closed its reply channel"
_CAP_FIELDS: Final = 3


class ProtocolError(Exception):
    """A message or command off the schema: saneless treats it as no answer."""


class ChildGoneError(Exception):
    """The reply channel reached end of file: the child exited or was killed."""


@dataclass(frozen=True, slots=True)
class ScanCommand:
    """
    Start a scan pass: the one command that names a device.

    Attributes:
        device: The SANE device id to open.
        settings: The scan settings, by option name.
        log_level: saneless's effective level for its own loggers, so the
            child does not send records nobody would see.

    """

    device: str
    settings: Mapping[str, str | int]
    log_level: int


class ControlOp(StrEnum):
    """The commands that carry nothing but their name."""

    SPOOLED = "spooled"
    STOP = "stop"
    CANCEL = "cancel"
    RESTART = "restart"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class Ready:
    """The child started and its scanner library is initialised."""


@dataclass(frozen=True, slots=True)
class Bye:
    """The child closed everything and is exiting."""


@dataclass(frozen=True, slots=True)
class Restarted:
    """The child restarted its scanner library, as asked."""


@dataclass(frozen=True, slots=True)
class StageFrame:
    """
    The child is entering a stage of the session.

    Attributes:
        stage: One of ``STAGES``.
        page: The page the stage concerns, counting from 1, if any.

    """

    stage: str
    page: int | None


@dataclass(frozen=True, slots=True)
class Configured:
    """
    The device is configured, with the scan parameters it reported.

    Attributes:
        resolution: The resolution the device read back, in dpi.
        frame_format: SANE's frame format, as python-sane names it.
        last_frame: Whether this is the last frame of the image.
        pixels_per_line: The width of a line, in pixels.
        lines: The number of lines, or -1 when the length is unknown.
        depth: The bits per sample.
        bytes_per_line: The length of a line, in bytes.
        use_adf: Whether the pass reads from a document feeder.

    """

    resolution: int
    frame_format: str
    last_frame: bool
    pixels_per_line: int
    lines: int
    depth: int
    bytes_per_line: int
    use_adf: bool


@dataclass(frozen=True, slots=True)
class PageHeader:
    """
    An accepted page, whose ``nbytes`` raw pixel bytes follow this message.

    Attributes:
        number: The page's number in the pass, counting from 1.
        mode: The image mode, a key of ``PAGE_BANDS``.
        width: The width, in pixels.
        height: The height, in pixels.
        dpi: The resolution the page was read at.
        nbytes: The length of the pixel data: width x height x bands.

    """

    number: int
    mode: str
    width: int
    height: int
    dpi: int
    nbytes: int


@dataclass(frozen=True, slots=True)
class PassDone:
    """
    A pass ended normally.

    Attributes:
        resolution: The resolution the device read back, in dpi.
        rejected: How many fed sheets came back unreadable and were dropped.
        substituted_source: The source used in place of the one asked for,
            if the device had to substitute one.
        cap: ``(cap, sheet_not_kept, auto_source)`` when the pass stopped at
            its page cap, otherwise ``None``.

    """

    resolution: int
    rejected: int
    substituted_source: str | None
    cap: tuple[int, int, bool] | None


@dataclass(frozen=True, slots=True)
class ChildFailure:
    """
    An exception the child caught, as it reported it.

    Attributes:
        type_name: The exception's class name.
        message: Its text, exactly as the child sent it.
        next_step: Its suggested next step, if it carries one.
        stage: The stage it was raised in, one of ``STAGES``.
        page: The page being read, counting from 1, if any.
        fatal: Whether the child is exiting because of it.

    """

    type_name: str
    message: str
    next_step: str | None
    stage: str
    page: int | None
    fatal: bool


@dataclass(frozen=True, slots=True)
class LogLine:
    """
    One log record from the child, for saneless to emit under the same name.

    Attributes:
        level: One of ``LOG_LEVELS``.
        logger: The logger's name: ``saneless`` or one beneath it.
        message: The formatted message, with any traceback.

    """

    level: int
    logger: str
    message: str


type Frame = (
    Ready
    | Bye
    | Restarted
    | StageFrame
    | Configured
    | PageHeader
    | PassDone
    | ChildFailure
    | LogLine
)

_KINDS: Final[Mapping[type, str]] = MappingProxyType(
    {
        Ready: "ready",
        Bye: "bye",
        Restarted: "restarted",
        StageFrame: "stage",
        Configured: "configured",
        PageHeader: "page",
        PassDone: "pass_done",
        ChildFailure: "error",
        LogLine: "log",
    }
)


def encode_command(command: ScanCommand | ControlOp) -> bytes:
    """
    Render a command as the one line the child reads from its stdin.

    Args:
        command: The command to send.

    Returns:
        One ASCII JSON object, newline-terminated.

    """
    payload: dict[str, object]
    if isinstance(command, ScanCommand):
        payload = {
            "op": "scan",
            "device": command.device,
            "settings": dict(command.settings),
            "log_level": command.log_level,
        }
    else:
        payload = {"op": command.value}
    return json.dumps(payload, ensure_ascii=True).encode("ascii") + b"\n"


def decode_command(line: bytes) -> ScanCommand | ControlOp | None:
    """
    Decode one command line, as the child reads it.

    Args:
        line: The line, with or without its newline.

    Returns:
        The command, or ``None`` when the line is not exactly one of the
        command schemas.

    """
    try:
        payload: object = json.loads(line.decode("ascii"))
    except ValueError, RecursionError:
        # UnicodeDecodeError and JSONDecodeError are both ValueErrors; a
        # deeply nested document exhausts the decoder's recursion.
        return None
    if not isinstance(payload, dict):
        return None
    op = payload.get("op")
    if op == "scan":
        return _scan_command(payload)
    if payload.keys() != {"op"} or not isinstance(op, str):
        return None
    try:
        return ControlOp(op)
    except ValueError:
        return None


def _scan_command(payload: dict[str, object]) -> ScanCommand | None:
    """Validate a ``scan`` command, or return ``None`` when it is off the schema."""
    if payload.keys() != {"op", "device", "settings", "log_level"}:
        return None
    device = payload["device"]
    settings = payload["settings"]
    log_level = payload["log_level"]
    if not isinstance(device, str) or not device:
        return None
    if not isinstance(settings, dict) or not all(
        isinstance(value, str | int) and not isinstance(value, bool)
        for value in settings.values()
    ):
        return None
    if not isinstance(log_level, int) or isinstance(log_level, bool) or log_level < 0:
        return None
    return ScanCommand(device=device, settings=settings, log_level=log_level)


def encode_frame(frame: Frame) -> bytes:
    """
    Render a message as the child writes it: length prefix, then JSON.

    A ``PageHeader`` covers the header only; the child writes the page's
    pixel bytes straight after it.

    Args:
        frame: The message to send.

    Returns:
        The length prefix and the ASCII JSON header.

    Raises:
        ProtocolError: The header would be longer than ``MAX_HEADER_BYTES``,
            so saneless would refuse it.

    """
    payload: dict[str, object] = {"kind": _KINDS[type(frame)]}
    payload.update({field.name: getattr(frame, field.name) for field in fields(frame)})
    header = json.dumps(payload, ensure_ascii=True).encode("ascii")
    if len(header) > MAX_HEADER_BYTES:
        raise ProtocolError(_OFF_SCHEMA)
    return LENGTH_PREFIX.pack(len(header)) + header


def read_frame(fd: int, wait: Callable[[], None], *, max_pixels: int) -> Frame:
    """
    Read and validate one message from the child's reply channel.

    Args:
        fd: The read end of the reply channel.
        wait: Called before every read; it blocks until the channel is
            readable and raises to stop the read (a deadline or an abort).
        max_pixels: The most pixels a page may announce.

    Returns:
        The message.  After a ``PageHeader``, the caller reads the pixels
        with ``receive_page``.

    Raises:
        ChildGoneError: The channel closed before a whole message arrived.
        ProtocolError: The header is longer than ``MAX_HEADER_BYTES``
            (refused before its body is read), not ASCII, not JSON, or not
            exactly one message schema.

    """
    (length,) = LENGTH_PREFIX.unpack(_read_exact(fd, LENGTH_PREFIX.size, wait))
    if length > MAX_HEADER_BYTES:
        raise ProtocolError(_OFF_SCHEMA)
    header = _read_exact(fd, length, wait)
    try:
        payload: object = json.loads(header.decode("ascii"))
    except ValueError, RecursionError:
        raise ProtocolError(_OFF_SCHEMA) from None
    if not isinstance(payload, dict):
        raise ProtocolError(_OFF_SCHEMA)
    kind = payload.pop("kind", None)
    decoder = _DECODERS.get(kind) if isinstance(kind, str) else None
    if decoder is None:
        raise ProtocolError(_OFF_SCHEMA)
    return decoder(payload, max_pixels)


def receive_page(fd: int, header: PageHeader, wait: Callable[[], None]) -> Image.Image:
    """
    Read a page's pixels into one buffer and wrap it as an image.

    The buffer is allocated once, at exactly ``header.nbytes``, and filled in
    place; it is fresh for every page because a grey image shares it.

    Args:
        fd: The read end of the reply channel.
        header: The validated header the pixels follow.
        wait: Called before every read, as for ``read_frame``.

    Returns:
        The page, of the header's mode and size.

    Raises:
        ChildGoneError: The channel closed before the last pixel byte.

    """
    buffer = bytearray(header.nbytes)
    view = memoryview(buffer)
    received = 0
    try:
        while received < header.nbytes:
            wait()
            count = os.readv(fd, [view[received:]])
            if count == 0:
                raise ChildGoneError(_GONE)
            received += count
    finally:
        view.release()
    size = (header.width, header.height)
    # Pillow documents ``data`` as "a bytes or other buffer object" but
    # annotates only ``bytes``; a bytearray is read in place, never copied.
    data = cast("bytes", buffer)
    return Image.frombuffer(header.mode, size, data, "raw", header.mode, 0, 1)


def _read_exact(fd: int, size: int, wait: Callable[[], None]) -> bytes:
    """
    Read exactly ``size`` bytes of a message header.

    Raises:
        ChildGoneError: The channel closed first.

    """
    data = bytearray()
    while len(data) < size:
        wait()
        chunk = os.read(fd, size - len(data))
        if not chunk:
            raise ChildGoneError(_GONE)
        data += chunk
    return bytes(data)


def _fields(payload: dict[str, object], keys: frozenset[str]) -> dict[str, object]:
    """
    Check a message has exactly ``keys``.

    Raises:
        ProtocolError: A key is missing or unknown.

    """
    if payload.keys() != keys:
        raise ProtocolError(_OFF_SCHEMA)
    return payload


def _int(value: object, minimum: int) -> int:
    """
    Validate an int, not a bool, of at least ``minimum``.

    Raises:
        ProtocolError: ``value`` is anything else.

    """
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ProtocolError(_OFF_SCHEMA)
    return value


def _optional_page(value: object) -> int | None:
    """Validate a page number, counting from 1, or ``None``."""
    return None if value is None else _int(value, 1)


def _str(value: object) -> str:
    """
    Validate a string.

    Raises:
        ProtocolError: ``value`` is anything else.

    """
    if not isinstance(value, str):
        raise ProtocolError(_OFF_SCHEMA)
    return value


def _optional_str(value: object) -> str | None:
    """Validate a string or ``None``."""
    return None if value is None else _str(value)


def _bool(value: object) -> bool:
    """
    Validate a bool.

    Raises:
        ProtocolError: ``value`` is anything else, including 0 or 1.

    """
    if not isinstance(value, bool):
        raise ProtocolError(_OFF_SCHEMA)
    return value


def _stage(value: object) -> str:
    """
    Validate a stage name.

    Raises:
        ProtocolError: ``value`` is not one of ``STAGES``.

    """
    if not isinstance(value, str) or value not in STAGES:
        raise ProtocolError(_OFF_SCHEMA)
    return value


def _decode_ready(payload: dict[str, object], _max_pixels: int) -> Ready:
    """Validate a ``ready`` message."""
    _fields(payload, frozenset())
    return Ready()


def _decode_bye(payload: dict[str, object], _max_pixels: int) -> Bye:
    """Validate a ``bye`` message."""
    _fields(payload, frozenset())
    return Bye()


def _decode_restarted(payload: dict[str, object], _max_pixels: int) -> Restarted:
    """Validate a ``restarted`` message."""
    _fields(payload, frozenset())
    return Restarted()


def _decode_stage(payload: dict[str, object], _max_pixels: int) -> StageFrame:
    """Validate a ``stage`` message."""
    _fields(payload, frozenset({"stage", "page"}))
    return StageFrame(
        stage=_stage(payload["stage"]), page=_optional_page(payload["page"])
    )


def _decode_configured(payload: dict[str, object], _max_pixels: int) -> Configured:
    """Validate a ``configured`` message; ``lines`` may be -1, for unknown."""
    _fields(payload, frozenset(field.name for field in fields(Configured)))
    return Configured(
        resolution=_int(payload["resolution"], 1),
        frame_format=_str(payload["frame_format"]),
        last_frame=_bool(payload["last_frame"]),
        pixels_per_line=_int(payload["pixels_per_line"], 0),
        lines=_int(payload["lines"], -1),
        depth=_int(payload["depth"], 1),
        bytes_per_line=_int(payload["bytes_per_line"], 0),
        use_adf=_bool(payload["use_adf"]),
    )


def _decode_page(payload: dict[str, object], max_pixels: int) -> PageHeader:
    """
    Validate a ``page`` message, before any buffer is allocated for it.

    Raises:
        ProtocolError: Off the schema, an unknown mode, more pixels than
            ``max_pixels``, or a byte count other than width x height x bands.

    """
    _fields(payload, frozenset(field.name for field in fields(PageHeader)))
    mode = _str(payload["mode"])
    bands = PAGE_BANDS.get(mode)
    width = _int(payload["width"], 1)
    height = _int(payload["height"], 1)
    nbytes = _int(payload["nbytes"], 1)
    if bands is None or width * height > max_pixels:
        raise ProtocolError(_OFF_SCHEMA)
    if nbytes != width * height * bands:
        raise ProtocolError(_OFF_SCHEMA)
    return PageHeader(
        number=_int(payload["number"], 1),
        mode=mode,
        width=width,
        height=height,
        dpi=_int(payload["dpi"], 1),
        nbytes=nbytes,
    )


def _cap(value: object) -> tuple[int, int, bool] | None:
    """
    Validate a pass's cap state: ``[cap, sheet_not_kept, auto_source]``.

    Raises:
        ProtocolError: ``value`` is neither ``None`` nor that list.

    """
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != _CAP_FIELDS:
        raise ProtocolError(_OFF_SCHEMA)
    return (_int(value[0], 0), _int(value[1], 0), _bool(value[2]))


def _decode_pass_done(payload: dict[str, object], _max_pixels: int) -> PassDone:
    """Validate a ``pass_done`` message."""
    _fields(payload, frozenset(field.name for field in fields(PassDone)))
    return PassDone(
        resolution=_int(payload["resolution"], 1),
        rejected=_int(payload["rejected"], 0),
        substituted_source=_optional_str(payload["substituted_source"]),
        cap=_cap(payload["cap"]),
    )


def _decode_failure(payload: dict[str, object], _max_pixels: int) -> ChildFailure:
    """Validate an ``error`` message."""
    _fields(payload, frozenset(field.name for field in fields(ChildFailure)))
    return ChildFailure(
        type_name=_str(payload["type_name"]),
        message=_str(payload["message"]),
        next_step=_optional_str(payload["next_step"]),
        stage=_stage(payload["stage"]),
        page=_optional_page(payload["page"]),
        fatal=_bool(payload["fatal"]),
    )


def _decode_log(payload: dict[str, object], _max_pixels: int) -> LogLine:
    """
    Validate a ``log`` message.

    Raises:
        ProtocolError: Off the schema, a level outside ``LOG_LEVELS``, or a
            logger outside saneless's own, so a child cannot write into
            another library's log.

    """
    _fields(payload, frozenset(field.name for field in fields(LogLine)))
    level = _int(payload["level"], 0)
    logger = _str(payload["logger"])
    if level not in LOG_LEVELS:
        raise ProtocolError(_OFF_SCHEMA)
    if logger != _LOGGER_ROOT and not logger.startswith(f"{_LOGGER_ROOT}."):
        raise ProtocolError(_OFF_SCHEMA)
    return LogLine(level=level, logger=logger, message=_str(payload["message"]))


_DECODERS: Final[Mapping[str, Callable[[dict[str, object], int], Frame]]] = (
    MappingProxyType(
        {
            "ready": _decode_ready,
            "bye": _decode_bye,
            "restarted": _decode_restarted,
            "stage": _decode_stage,
            "configured": _decode_configured,
            "page": _decode_page,
            "pass_done": _decode_pass_done,
            "error": _decode_failure,
            "log": _decode_log,
        }
    )
)
