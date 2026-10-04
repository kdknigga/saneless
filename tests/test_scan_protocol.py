"""
Tests for ``saneless.scanner.scan_protocol``, the scan child's wire format.

The child answers on a private pipe with length-prefixed JSON frames, and a
page frame is followed by exactly its pixel bytes; saneless commands the child
with one JSON object per line.  Every frame and command is checked against its
schema, and anything off it is refused before a page buffer is allocated.

The receive path is measured: a large page streamed through it into a sink
that keeps the image raises the traced peak by about one page, and the same
harness sees about two pages for a receiver that joins chunks or copies the
image with ``tobytes()``, so the bound is shown to catch a copy.

Every test runs over a real ``os.pipe()``; nothing here touches libsane.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import tracemalloc
from typing import TYPE_CHECKING

import pytest
from PIL import Image
from saneless.scanner.scan_protocol import (
    LENGTH_PREFIX,
    MAX_HEADER_BYTES,
    PAGE_BANDS,
    Bye,
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
    decode_command,
    encode_command,
    encode_frame,
    read_frame,
    receive_page,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from saneless.scanner.scan_protocol import Frame

# A4 at 300 dpi, the page the one-copy bound is measured on.
_A4_300_DPI = (2480, 3508)
# Generous enough for every page these tests send.
_LARGE_CAP = 2 * Image.MAX_IMAGE_PIXELS
# The cap the off-schema cases run under: a 10x10 page fits, 40x30 does not.
_SMALL_CAP = 1000
_ONE_COPY_BOUND = 1.2
_COPY_FLOOR = 1.8
_WRITER_JOIN_SECONDS = 10.0


def _no_wait() -> None:
    """Stand in for the caller's deadline hook: never stop the read."""


class _Stop(Exception):
    """Raised by a wait hook that stops the read."""


def _stop() -> None:
    """Stop the read before it starts."""
    raise _Stop


@contextlib.contextmanager
def _pipe_carrying(*chunks: bytes) -> Iterator[int]:
    """
    Yield the read end of a pipe a helper thread fills with ``chunks``.

    The writer sends memoryview slices, so a page larger than the pipe buffer
    neither deadlocks nor allocates a copy, then closes its end so a short
    send reads as end of file.
    """
    read_fd, write_fd = os.pipe()

    def write() -> None:
        try:
            for chunk in chunks:
                view = memoryview(chunk)
                sent = 0
                while sent < len(view):
                    sent += os.write(write_fd, view[sent:])
        except BrokenPipeError:
            # The reader refused the frame and closed its end first.
            pass
        finally:
            os.close(write_fd)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    try:
        yield read_fd
    finally:
        os.close(read_fd)
        writer.join(timeout=_WRITER_JOIN_SECONDS)
        assert not writer.is_alive()


def _framed(header: bytes) -> bytes:
    """Prefix a raw header with its length, as the child does."""
    return LENGTH_PREFIX.pack(len(header)) + header


def _json(payload: object) -> bytes:
    """Encode a payload the way the child would, for off-schema variants."""
    return json.dumps(payload).encode("ascii")


def _page(**changes: object) -> bytes:
    """Encode a valid 10x10 L page header, with ``changes`` applied."""
    payload: dict[str, object] = {
        "kind": "page",
        "number": 1,
        "mode": "L",
        "width": 10,
        "height": 10,
        "dpi": 300,
        "nbytes": 100,
    }
    payload.update(changes)
    return _json(payload)


def _without(payload: dict[str, object], key: str) -> dict[str, object]:
    """Return ``payload`` minus ``key``."""
    return {name: value for name, value in payload.items() if name != key}


_LOG = {"kind": "log", "level": 20, "logger": "saneless.scanner", "message": "hi"}
_STAGE = {"kind": "stage", "stage": "read", "page": 2}

_FRAMES = [
    pytest.param(Ready(), id="ready"),
    pytest.param(Bye(), id="bye"),
    pytest.param(Restarted(), id="restarted"),
    pytest.param(StageFrame(stage="open", page=None), id="stage-without-page"),
    pytest.param(StageFrame(stage="read", page=3), id="stage-with-page"),
    pytest.param(
        Configured(
            resolution=300,
            frame_format="color",
            last_frame=True,
            pixels_per_line=2480,
            lines=-1,
            depth=8,
            bytes_per_line=7440,
            use_adf=True,
        ),
        id="configured",
    ),
    pytest.param(
        PageHeader(number=4, mode="RGB", width=20, height=30, dpi=150, nbytes=1800),
        id="page",
    ),
    pytest.param(
        PassDone(resolution=300, rejected=0, substituted_source=None, cap=None),
        id="pass-done",
    ),
    pytest.param(
        PassDone(
            resolution=200,
            rejected=2,
            substituted_source="ADF Front",
            cap=(50, 51, True),
        ),
        id="pass-done-capped",
    ),
    pytest.param(
        ChildFailure(
            type_name="FeederEmptyError",
            message="The feeder is empty",
            next_step="Load paper",
            stage="read",
            page=1,
            fatal=False,
        ),
        id="error",
    ),
    pytest.param(
        ChildFailure(
            type_name="ScanError",
            message="café",
            next_step=None,
            stage="startup",
            page=None,
            fatal=True,
        ),
        id="error-non-ascii-text",
    ),
    pytest.param(LogLine(level=10, logger="saneless", message="line\nbreak"), id="log"),
]


@pytest.mark.parametrize("frame", _FRAMES)
def test_every_frame_kind_round_trips(frame: Frame) -> None:
    """A frame encoded by the child reads back as an equal frame."""
    with _pipe_carrying(encode_frame(frame)) as fd:
        assert read_frame(fd, _no_wait, max_pixels=_LARGE_CAP) == frame


_COMMANDS = [
    pytest.param(
        ScanCommand(
            device="net:host:devé",
            settings={"mode": "Color", "resolution": 300},
            log_level=20,
        ),
        id="scan",
    ),
    pytest.param(ScanCommand(device="test:0", settings={}, log_level=10), id="bare"),
    *(pytest.param(op, id=op.value) for op in ControlOp),
]


@pytest.mark.parametrize("command", _COMMANDS)
def test_every_command_round_trips(command: ScanCommand | ControlOp) -> None:
    """A command saneless encodes decodes to an equal command."""
    assert decode_command(encode_command(command)) == command


@pytest.mark.parametrize("command", _COMMANDS)
def test_a_command_is_one_ascii_json_line(command: ScanCommand | ControlOp) -> None:
    """Each command is one ASCII line holding one JSON object keyed by ``op``."""
    line = encode_command(command)

    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    payload = json.loads(line.decode("ascii"))
    assert isinstance(payload, dict)
    if isinstance(command, ScanCommand):
        assert payload == {
            "op": "scan",
            "device": command.device,
            "settings": dict(command.settings),
            "log_level": command.log_level,
        }
    else:
        assert payload == {"op": command.value}


_OFF_SCHEMA_FRAMES = [
    pytest.param(b"not json", id="not-json"),
    pytest.param(b"[]", id="not-an-object"),
    pytest.param(b"[" * 30_000 + b"]" * 30_000, id="deep-nesting"),
    pytest.param(
        '{"kind": "log", "level": 20, "logger": "saneless", "message": "é"}'.encode(),
        id="non-ascii-header",
    ),
    pytest.param(_json({"level": 20}), id="no-kind"),
    pytest.param(_json({"kind": "teleport"}), id="unknown-kind"),
    pytest.param(_json({"kind": "ready", "extra": 1}), id="ready-extra-key"),
    pytest.param(_page(extra=1), id="page-extra-key"),
    pytest.param(
        _json(_without(json.loads(_page()), "dpi")),
        id="page-missing-key",
    ),
    pytest.param(_page(width=True), id="page-bool-for-int"),
    pytest.param(_page(dpi="300"), id="page-str-for-int"),
    pytest.param(_page(mode="CMYK", nbytes=400), id="page-mode-cmyk"),
    pytest.param(_page(nbytes=99), id="page-nbytes-short"),
    pytest.param(_page(mode="RGB"), id="page-nbytes-ignores-bands"),
    pytest.param(_page(width=0, nbytes=0), id="page-zero-width"),
    pytest.param(_page(height=-10, nbytes=-100), id="page-negative-height"),
    pytest.param(_page(width=40, height=30, nbytes=1200), id="page-over-pixel-cap"),
    pytest.param(_page(number=0), id="page-number-zero"),
    pytest.param(_page(dpi=0), id="page-dpi-zero"),
    pytest.param(_json({**_STAGE, "stage": "teleport"}), id="stage-unknown"),
    pytest.param(_json({**_STAGE, "page": True}), id="stage-bool-page"),
    pytest.param(_json({**_LOG, "logger": "requests"}), id="log-foreign-logger"),
    pytest.param(_json({**_LOG, "logger": "sanelessly"}), id="log-lookalike-logger"),
    pytest.param(_json({**_LOG, "level": 7}), id="log-unknown-level"),
    pytest.param(_json({**_LOG, "message": 5}), id="log-non-str-message"),
    pytest.param(
        _json(
            {
                "kind": "pass_done",
                "resolution": 300,
                "rejected": 0,
                "substituted_source": None,
                "cap": [50, 51],
            }
        ),
        id="pass-done-short-cap",
    ),
    pytest.param(
        _json(
            {
                "kind": "pass_done",
                "resolution": 300,
                "rejected": 0,
                "substituted_source": None,
                "cap": [50, 51, 1],
            }
        ),
        id="pass-done-int-for-bool",
    ),
    pytest.param(
        _json(
            {
                "kind": "error",
                "type_name": "ScanError",
                "message": "x",
                "next_step": None,
                "stage": "read",
                "page": None,
                "fatal": "yes",
            }
        ),
        id="error-str-for-bool",
    ),
    pytest.param(
        _json(
            {
                "kind": "configured",
                "resolution": 300,
                "frame_format": "gray",
                "last_frame": True,
                "pixels_per_line": 10,
                "lines": -1,
                "depth": 8,
                "bytes_per_line": 10.5,
                "use_adf": False,
            }
        ),
        id="configured-float-for-int",
    ),
]


@pytest.mark.parametrize("header", _OFF_SCHEMA_FRAMES)
def test_off_schema_frames_are_refused(header: bytes) -> None:
    """Every frame off the schema is a protocol error, never a key or type error."""
    with (
        _pipe_carrying(_framed(header)) as fd,
        pytest.raises(ProtocolError) as refused,
    ):
        read_frame(fd, _no_wait, max_pixels=_SMALL_CAP)

    assert type(refused.value) is ProtocolError


def test_a_page_at_the_pixel_cap_is_accepted() -> None:
    """The pixel cap is inclusive: a page of exactly the cap is read."""
    header = _page(width=50, height=20, nbytes=_SMALL_CAP)

    with _pipe_carrying(_framed(header)) as fd:
        frame = read_frame(fd, _no_wait, max_pixels=_SMALL_CAP)

    assert frame == PageHeader(
        number=1, mode="L", width=50, height=20, dpi=300, nbytes=_SMALL_CAP
    )


def test_an_oversized_header_is_refused_before_its_body() -> None:
    """
    A length over the cap is refused on the prefix alone.

    The pipe holds only the prefix and then closes, so a reader that went on
    to read the body would see end of file and raise ``ChildGoneError``.
    """
    with (
        _pipe_carrying(LENGTH_PREFIX.pack(MAX_HEADER_BYTES + 1)) as fd,
        pytest.raises(ProtocolError),
    ):
        read_frame(fd, _no_wait, max_pixels=_LARGE_CAP)


def test_a_header_of_the_cap_is_read() -> None:
    """A header of exactly the cap is read and then judged on its content."""
    payload = _json({**_LOG, "message": ""})
    padded = payload[:-1] + b" " * (MAX_HEADER_BYTES - len(payload)) + b"}"
    assert len(padded) == MAX_HEADER_BYTES

    with _pipe_carrying(_framed(padded)) as fd:
        frame = read_frame(fd, _no_wait, max_pixels=_LARGE_CAP)

    assert frame == LogLine(level=20, logger="saneless.scanner", message="")


_OFF_SCHEMA_COMMANDS = [
    pytest.param(b"not json\n", id="not-json"),
    pytest.param(b"[]\n", id="not-an-object"),
    pytest.param(b'{"op": "teleport"}\n', id="unknown-op"),
    pytest.param(b'{"op": "stop", "now": true}\n', id="control-extra-key"),
    pytest.param(b'{"settings": {}, "log_level": 20}\n', id="no-op"),
    pytest.param(b'{"op": "scan", "settings": {}, "log_level": 20}\n', id="no-device"),
    pytest.param(
        b'{"op": "scan", "device": "", "settings": {}, "log_level": 20}\n',
        id="empty-device",
    ),
    pytest.param(
        b'{"op": "scan", "device": 5, "settings": {}, "log_level": 20}\n',
        id="int-device",
    ),
    pytest.param(
        b'{"op": "scan", "device": "test:0", "settings": {"a": true}, '
        b'"log_level": 20}\n',
        id="bool-setting",
    ),
    pytest.param(
        b'{"op": "scan", "device": "test:0", "settings": {"a": 1.5}, '
        b'"log_level": 20}\n',
        id="float-setting",
    ),
    pytest.param(
        b'{"op": "scan", "device": "test:0", "settings": [], "log_level": 20}\n',
        id="settings-not-an-object",
    ),
    pytest.param(
        b'{"op": "scan", "device": "test:0", "settings": {}, "log_level": "20"}\n',
        id="str-log-level",
    ),
    pytest.param(
        b'{"op": "scan", "device": "test:0", "settings": {}, "log_level": true}\n',
        id="bool-log-level",
    ),
    pytest.param(
        b'{"op": "scan", "device": "test:0", "settings": {}, "log_level": 20, '
        b'"x": 1}\n',
        id="scan-extra-key",
    ),
    pytest.param(
        '{"op": "scan", "device": "é", "settings": {}, "log_level": 20}\n'.encode(),
        id="non-ascii-line",
    ),
]


@pytest.mark.parametrize("line", _OFF_SCHEMA_COMMANDS)
def test_off_schema_command_lines_decode_to_nothing(line: bytes) -> None:
    """The child treats a command line off the schema as no command at all."""
    assert decode_command(line) is None


_SHORT_SENDS = [
    pytest.param(b"", id="before-the-prefix"),
    pytest.param(b"\x00\x00", id="mid-prefix"),
    pytest.param(LENGTH_PREFIX.pack(20) + b'{"kind"', id="mid-header"),
]


@pytest.mark.parametrize("sent", _SHORT_SENDS)
def test_end_of_file_in_a_frame_means_the_child_is_gone(sent: bytes) -> None:
    """A reply channel that closes before a whole frame is a gone child."""
    with _pipe_carrying(sent) as fd, pytest.raises(ChildGoneError):
        read_frame(fd, _no_wait, max_pixels=_LARGE_CAP)


def test_end_of_file_mid_page_means_the_child_is_gone() -> None:
    """A reply channel that closes before the last pixel byte is a gone child."""
    header = PageHeader(number=1, mode="L", width=10, height=10, dpi=300, nbytes=100)

    with _pipe_carrying(bytes(50)) as fd, pytest.raises(ChildGoneError):
        receive_page(fd, header, _no_wait)


def _pixels(nbytes: int, seed: int = 0) -> bytes:
    """Build ``nbytes`` of varied, deterministic pixel data."""
    pattern = bytes((index * 7 + seed) % 256 for index in range(256))
    whole, part = divmod(nbytes, len(pattern))
    return pattern * whole + pattern[:part]


@pytest.mark.parametrize(
    "mode", [pytest.param("L", id="L"), pytest.param("RGB", id="RGB")]
)
def test_a_received_page_matches_the_bytes_sent(mode: str) -> None:
    """The image has the header's mode and size, and exactly the sent pixels."""
    width, height = 123, 45
    nbytes = width * height * PAGE_BANDS[mode]
    pixels = _pixels(nbytes)
    header = PageHeader(
        number=2, mode=mode, width=width, height=height, dpi=200, nbytes=nbytes
    )

    with _pipe_carrying(encode_frame(header), pixels) as fd:
        frame = read_frame(fd, _no_wait, max_pixels=_LARGE_CAP)
        assert frame == header
        image = receive_page(fd, header, _no_wait)

    assert image.mode == mode
    assert image.size == (width, height)
    assert image.tobytes() == pixels


def test_each_page_gets_a_fresh_buffer() -> None:
    """A grey image shares its buffer, so the next page must not overwrite it."""
    header = PageHeader(number=1, mode="L", width=64, height=32, dpi=300, nbytes=2048)
    first, second = _pixels(2048, seed=1), _pixels(2048, seed=2)

    with _pipe_carrying(first, second) as fd:
        first_image = receive_page(fd, header, _no_wait)
        second_image = receive_page(fd, header, _no_wait)

    assert first_image.tobytes() == first
    assert second_image.tobytes() == second


def test_the_wait_hook_runs_before_reading_a_frame() -> None:
    """A wait hook that raises stops the frame read, even with data waiting."""
    with _pipe_carrying(encode_frame(Ready())) as fd, pytest.raises(_Stop):
        read_frame(fd, _stop, max_pixels=_LARGE_CAP)


def test_the_wait_hook_runs_before_reading_a_page() -> None:
    """A wait hook that raises stops the page read, even with pixels waiting."""
    header = PageHeader(number=1, mode="L", width=10, height=10, dpi=300, nbytes=100)

    with _pipe_carrying(bytes(100)) as fd, pytest.raises(_Stop):
        receive_page(fd, header, _stop)


def _receive_in_place(fd: int, header: PageHeader) -> Image.Image:
    """Receive a page the way saneless does."""
    return receive_page(fd, header, _no_wait)


def _receive_by_joining(fd: int, header: PageHeader) -> Image.Image:
    """Collect read chunks and join them: a second copy of the page."""
    chunks: list[bytes] = []
    got = 0
    while got < header.nbytes:
        chunk = os.read(fd, min(1 << 16, header.nbytes - got))
        if not chunk:
            raise ChildGoneError
        chunks.append(chunk)
        got += len(chunk)
    data = b"".join(chunks)
    size = (header.width, header.height)
    return Image.frombuffer(header.mode, size, data, "raw", header.mode, 0, 1)


def _receive_then_copy(fd: int, header: PageHeader) -> Image.Image:
    """Receive correctly, then rebuild the image from ``tobytes()``."""
    image = receive_page(fd, header, _no_wait)
    size = (header.width, header.height)
    return Image.frombytes(header.mode, size, image.tobytes())


class _RecordingSink:
    """Keep every page added, as a sink spooling the page would hold it."""

    def __init__(self) -> None:
        self.pages: list[Image.Image] = []

    def add(self, image: Image.Image) -> None:
        """Keep ``image``."""
        self.pages.append(image)


def _peak_pages(mode: str, receive: Callable[[int, PageHeader], Image.Image]) -> float:
    """
    Measure the traced peak of receiving one A4 300 dpi page, in pages.

    The pixels and the header are built before tracing starts and the writer
    sends slices of them, so the peak is the receiver's own allocations while
    the sink keeps the image.  Pillow's pixel memory is not traced.
    """
    width, height = _A4_300_DPI
    nbytes = width * height * PAGE_BANDS[mode]
    pixels = _pixels(nbytes)
    header = PageHeader(
        number=1, mode=mode, width=width, height=height, dpi=300, nbytes=nbytes
    )
    frame = encode_frame(header)
    sink = _RecordingSink()

    with _pipe_carrying(frame, pixels) as fd:
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            received = read_frame(fd, _no_wait, max_pixels=_LARGE_CAP)
            assert isinstance(received, PageHeader)
            sink.add(receive(fd, received))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    assert sink.pages[0].size == (width, height)
    return peak / nbytes


@pytest.mark.parametrize(
    "mode", [pytest.param("L", id="L"), pytest.param("RGB", id="RGB")]
)
def test_receiving_a_page_holds_one_copy(mode: str) -> None:
    """Receiving a page into a sink that keeps it costs about one page."""
    assert _peak_pages(mode, _receive_in_place) <= _ONE_COPY_BOUND


@pytest.mark.parametrize(
    "receive",
    [
        pytest.param(_receive_by_joining, id="bytes-join"),
        pytest.param(_receive_then_copy, id="tobytes"),
    ],
)
def test_the_harness_catches_a_copying_receiver(
    receive: Callable[[int, PageHeader], Image.Image],
) -> None:
    """The same harness measures two pages or more for a receiver that copies."""
    peaks = {mode: _peak_pages(mode, receive) for mode in ("L", "RGB")}

    assert all(peak >= _COPY_FLOOR for peak in peaks.values()), peaks
