"""
What the parent may hold while a page crosses from the scan child.

A page arrives from the scan child over a pipe, and the parent reads it into
one buffer of exactly the page's size, which the image then wraps.  A
receive that collected chunks and joined them, or rebuilt the image from
its bytes, would hold the page twice at its peak; on an A4 colour page at
300 dpi that is tens of megabytes more per page for no gain.

These tests run a whole scan pass through ``ScanChildSession.scan_pass``,
against a stand-in child process started by the real launcher, and measure
the parent's peak with ``tracemalloc``.  Only allocations ``tracemalloc``
can see are counted: Pillow unpacks an RGB page into memory of its own,
which ``tracemalloc`` does not trace, so the bound is on the receive
buffer and on everything the session allocates around it.
"""

from __future__ import annotations

import tracemalloc
from typing import TYPE_CHECKING, Final

import pytest

import saneless.scanner.scan_child as scan_child_mod
from saneless.scanner.base import PageRecord, PageSink, ScanSettings
from saneless.scanner.scan_child import ScanChildSession
from saneless.scanner.scan_protocol import PAGE_BANDS

if TYPE_CHECKING:
    from pathlib import Path

    from PIL import Image

# An A4 page at 300 dpi, in pixels.
_A4_300_DPI: Final = (2480, 3508)

# The most the parent's traced peak may be, in pages: one page and a margin
# for the session's own small allocations.
_ONE_COPY_BOUND: Final = 1.2

_SETTINGS: Final = ScanSettings(source="Flatbed", resolution=300, mode="Gray")

# The stand-in child.  It uses the standard library and the protocol module
# only, reads commands as lines on fd 0 and writes its replies to fd 1, as
# the real child does.  Its first scan sends one 1 x 1 page and every later
# scan one A4 300 dpi page in SCAN_TEST_MODE, whose bytes are built before
# the page's header is written.  Its variables carry no SANELESS_ prefix, so
# the child environment's strip keeps them.
_STAND_IN: Final = """\
import json
import os
import sys

from saneless.scanner import scan_protocol as protocol

MODE = os.environ["SCAN_TEST_MODE"]
BANDS = protocol.PAGE_BANDS[MODE]
LARGE = (2480, 3508)


def write_all(data):
    view = memoryview(data)
    while view:
        view = view[os.write(1, view):]


def send(frame, payload=b""):
    write_all(protocol.encode_frame(frame))
    if payload:
        write_all(payload)


def next_op():
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            os._exit(0)
        op = json.loads(line)["op"]
        if op != "cancel":
            return op


def run_pass(width, height):
    pixels = bytes(range(256)) * (width * height * BANDS // 256 + 1)
    pixels = pixels[: width * height * BANDS]
    send(protocol.StageFrame(stage="open", page=None))
    send(protocol.StageFrame(stage="configure", page=None))
    send(
        protocol.Configured(
            resolution=300,
            frame_format="rgb" if BANDS == 3 else "gray",
            last_frame=True,
            pixels_per_line=width,
            lines=height,
            depth=8,
            bytes_per_line=width * BANDS,
            use_adf=False,
        )
    )
    send(protocol.StageFrame(stage="start", page=1))
    send(protocol.StageFrame(stage="read", page=1))
    header = protocol.PageHeader(
        number=1, mode=MODE, width=width, height=height, dpi=300, nbytes=len(pixels)
    )
    send(header, pixels)
    del pixels
    if next_op() != "spooled":
        os._exit(0)
    send(protocol.StageFrame(stage="close", page=None))
    send(protocol.PassDone(resolution=300, rejected=0, substituted_source=None, cap=None))


send(protocol.Ready())
passes = 0
while True:
    op = next_op()
    if op == "scan":
        passes += 1
        run_pass(*((1, 1) if passes == 1 else LARGE))
    elif op == "exit":
        send(protocol.Bye())
        os._exit(0)
"""


class _KeepingSink(PageSink):
    """A sink that keeps every image it is given, as a spooling sink holds one."""

    def __init__(self, tmp_path: Path) -> None:
        """Keep pages, naming their records inside ``tmp_path``."""
        self._directory = tmp_path
        self.images: list[Image.Image] = []

    def add(self, image: Image.Image, *, dpi: int) -> PageRecord:
        """Keep ``image`` and return a record for it."""
        self.images.append(image)
        return PageRecord(
            sequence=len(self.images),
            path=self._directory / f"page{len(self.images)}.png",
            size=image.size,
            mode=image.mode,
            dpi=dpi,
            ink_coverage=0.0,
            paper_white=255,
        )


@pytest.mark.parametrize(
    "mode", [pytest.param("L", id="L"), pytest.param("RGB", id="RGB")]
)
def test_a_scan_pass_holds_one_copy_of_a_page(
    mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A whole pass of one A4 300 dpi page costs the parent about one page.

    The first pass, of a 1 x 1 page, starts the child and sees it ready, so
    neither the child's start nor anything this interpreter loads on first
    use is counted.  Tracing covers only the second pass.

    Args:
        mode: The page's image mode.
        monkeypatch: Points the session at the stand-in and sets its mode.
        tmp_path: Holds the stand-in script.

    """
    child = tmp_path / "stand_in_scan_child.py"
    child.write_text(_STAND_IN, encoding="utf-8")
    monkeypatch.setattr(scan_child_mod, "_CHILD_FILE", child)
    monkeypatch.setenv("SCAN_TEST_MODE", mode)
    width, height = _A4_300_DPI
    nbytes = width * height * PAGE_BANDS[mode]
    sink = _KeepingSink(tmp_path)

    with ScanChildSession(lambda: scan_child_mod.start_scan_child("")) as session:
        session.scan_pass("test:0", _SETTINGS, sink)
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            batch = session.scan_pass("test:0", _SETTINGS, sink)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    assert [record.sequence for record in batch.pages] == [2]
    assert [(image.mode, image.size) for image in sink.images[1:]] == [
        (mode, _A4_300_DPI)
    ]
    assert peak / nbytes <= _ONE_COPY_BOUND, f"peak {peak / nbytes:.2f} pages"
