"""
Tests for PDF assembly module.

``assemble_pdf`` takes ``PageRecord``s now, not images: the spool has already
written every page as a PNG, and that file is what img2pdf embeds, losslessly
and with no second encode (D-03). So the pages these tests assemble are spooled
through a real ``SpooledPageSink`` into a real directory, and the assertions
about what assembly left behind are scoped to the *output* directory -- which
is deliberately not the spool, because the spooled pages are supposed to
survive assembly untouched.
"""

import os
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import img2pdf
import pikepdf
import pytest
from PIL import Image

import saneless.pdf as pdf_mod
from saneless.exceptions import PdfError
from saneless.paper_sizes import crop_to_paper_size
from saneless.pdf import (
    assemble_pdf,
    build_pdf_filename,
    sanitise_title_for_filename,
)
from saneless.pipeline import _SPOOL_LABEL_A, _SPOOL_LABEL_B
from saneless.spool import SpooledPageSink

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from saneless.scanner.base import PageRecord

# Hostile titles.  Every one of these reaches the sanitiser from a web form
# body or a ``saneless scan --title`` argument, and its output is joined onto
# ``consume_dir`` and ``<data_dir>/failed/``.  Adding a case is one line.
HOSTILE_TITLES = [
    "../../etc/passwd",
    "/etc/passwd",
    "..\\..\\windows",
    "a\x00b",
    "con.pdf",
    "....//....//x",
    "..",
    ".",
    "~/.ssh/authorized_keys",
]

JOB_A = "aaaaaaaa-1111-2222-3333-444444444444"
JOB_B = "bbbbbbbb-1111-2222-3333-444444444444"

# The reserve the spool keeps free beyond each page, in megabytes.  One is the
# smallest honest value: the pages here are small, so the per-page check passes
# anywhere the suite can run, and it is still a real check rather than a
# disabled one.
_TEST_RESERVE_MB = 1

# A PNG file opens with an 8-byte signature; chunks follow it, each one a
# 4-byte length, a 4-byte type, that many payload bytes and a 4-byte CRC.
_PNG_SIGNATURE_LENGTH = 8
_PNG_CHUNK_OVERHEAD = 12

# Every error class img2pdf 0.6.3 defines.  Each is a direct ``Exception``
# subclass with no shared base, which is why the boundary cannot catch a tuple.
IMG2PDF_ERROR_CLASSES: list[type[Exception]] = [
    img2pdf.AlphaChannelError,
    img2pdf.ExifOrientationError,
    img2pdf.ImageOpenError,
    img2pdf.JpegColorspaceError,
    img2pdf.NegativeDimensionError,
    img2pdf.PdfTooLargeError,
    img2pdf.UnsupportedColorspaceError,
]


def _raising(exc: BaseException) -> Callable[..., NoReturn]:
    """
    Build a stand-in for ``img2pdf.convert`` that raises ``exc``.

    Args:
        exc: The exception instance the stand-in raises on every call.

    Returns:
        A callable accepting any arguments that always raises ``exc``.

    """

    def fake_convert(*_args: object, **_kwargs: object) -> NoReturn:
        raise exc

    return fake_convert


def _rounded_media_box(page: pikepdf.Page) -> list[int]:
    """
    Read a page's MediaBox as four whole points.

    Rounding is not incidental: the true A4-at-300-DPI box is 595.2 x 841.92,
    and 595.2 x 841.68 for what ``crop_to_paper_size`` actually produces, so an
    exact comparison against 595 x 842 would never hold.

    Args:
        page: The page whose MediaBox to read.

    Returns:
        ``[llx, lly, urx, ury]``, each rounded to the nearest point.

    """
    box = pikepdf.Rectangle(page.mediabox)
    return [round(box.llx), round(box.lly), round(box.urx), round(box.ury)]


def _embedded_streams(pdf_path: Path) -> list[bytes]:
    """
    Read each PDF page's single embedded image stream, in page order.

    Raw rather than decoded: img2pdf passes a suitable PNG's compressed pixel
    data straight through, so the raw stream is directly comparable with the
    spooled PNG's own IDAT payload.

    Args:
        pdf_path: The assembled PDF to read.

    Returns:
        One raw stream per page, in the order the pages appear.

    """
    streams: list[bytes] = []
    with pikepdf.open(pdf_path) as pdf:
        for page in pdf.pages:
            (image,) = pikepdf.Page(page).images.values()
            streams.append(image.read_raw_bytes())
    return streams


def _media_boxes(pdf_path: Path) -> list[tuple[float, float, float, float]]:
    """
    Read every page's MediaBox exactly, in page order.

    Exact rather than rounded, unlike :func:`_rounded_media_box`: this one
    compares two PDFs against each other rather than against a nominal paper
    size, so a hundredth of a point of drift between them is a real difference.

    Args:
        pdf_path: The PDF to read.

    Returns:
        One ``(llx, lly, urx, ury)`` tuple per page.

    """
    with pikepdf.open(pdf_path) as pdf:
        boxes = [pikepdf.Rectangle(page.mediabox) for page in pdf.pages]
    return [(box.llx, box.lly, box.urx, box.ury) for box in boxes]


def _png_idat_payload(png_path: Path) -> bytes:
    """
    Concatenate a PNG's IDAT chunk payloads -- the compressed pixel data.

    This is the strongest available statement of D-03's "the spooled PNG is
    exactly what img2pdf embeds": for a non-interlaced, non-alpha PNG img2pdf
    copies the IDAT payload into the PDF stream untouched, so this byte string
    must come back out of the assembled PDF verbatim. Decoding either side
    would only prove the two images look alike, which a re-encode would also
    satisfy.

    Args:
        png_path: The spooled page to read.

    Returns:
        Every IDAT payload in the file, concatenated in file order.

    """
    raw = png_path.read_bytes()
    payload = bytearray()
    offset = _PNG_SIGNATURE_LENGTH
    while offset < len(raw):
        (length,) = struct.unpack(">I", raw[offset : offset + 4])
        if raw[offset + 4 : offset + 8] == b"IDAT":
            payload += raw[offset + 8 : offset + 8 + length]
        offset += length + _PNG_CHUNK_OVERHEAD
    return bytes(payload)


def _recording_convert(calls: list[dict[str, object]]) -> Callable[..., object]:
    """
    Wrap the real ``img2pdf.convert`` so every call is recorded and performed.

    A wrapper rather than a fake: the assertions are about *how many* times
    convert runs and *with what*, and a fake that skipped the real work would
    leave nothing for the merge to merge.

    Args:
        calls: The list each call appends a ``{"images", "kwargs"}`` record to.

    Returns:
        A stand-in for ``img2pdf.convert`` that delegates to the real one.

    """
    real_convert = img2pdf.convert

    def wrapper(*args: object, **kwargs: object) -> object:
        """Record this call, then delegate to the real ``img2pdf.convert``."""
        calls.append({"images": args[0] if args else None, "kwargs": dict(kwargs)})
        return real_convert(*args, **kwargs)

    return wrapper


def _recording_job(argvs: list[list[str]]) -> Callable[[list[str]], pikepdf.Job]:
    """
    Wrap ``pikepdf.Job`` so the argv it is built with is recorded.

    Args:
        argvs: The list each construction appends its argv to.

    Returns:
        A stand-in for the ``pikepdf.Job`` constructor that builds a real one.

    """
    real_job = pikepdf.Job

    def factory(argv: list[str]) -> pikepdf.Job:
        """Record ``argv``, then build the real ``pikepdf.Job``."""
        argvs.append(list(argv))
        return real_job(argv)

    return factory


class _FailingJob:
    """A ``pikepdf.Job`` stand-in whose ``run`` always fails."""

    def __init__(self, argv: list[str]) -> None:
        """Accept and keep the argv a real ``pikepdf.Job`` would be given."""
        self.argv = argv

    def run(self) -> NoReturn:
        """Fail the way qpdf fails, with a pikepdf exception type."""
        msg = "fake qpdf merge failure"
        raise pikepdf.PdfError(msg)


@pytest.fixture
def spool_dir(tmp_path: Path) -> Path:
    """
    Return the job's spool directory, created and empty.

    Its own subdirectory of ``tmp_path``, mirroring production, where the spool
    is ``<job workspace>/spool/`` and the assembled PDF is written beside it
    rather than among the pages.

    Returns:
        An existing, empty directory for the sink to write into.

    """
    directory = tmp_path / "spool"
    directory.mkdir()
    return directory


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    """
    Return where the assembled PDF goes -- deliberately not the spool.

    Keeping the two apart is what lets a test say "no PNG exists under the
    output directory" and mean it: with one shared directory that assertion
    would be about the spooled pages, which are supposed to be there.

    Returns:
        A path that does not exist yet; ``assemble_pdf`` creates it.

    """
    return tmp_path / "out"


@pytest.fixture
def spool_pages(spool_dir: Path) -> Callable[[Sequence[Image.Image]], list[PageRecord]]:
    """
    Hand back a factory that spools pages through a real ``SpooledPageSink``.

    A real sink rather than hand-built records, because the whole point of the
    record contract is that ``record.path`` names a file that exists and holds
    exactly the bytes the PDF will embed. A record pointing at a file nobody
    wrote would make every assertion here vacuous.

    Returns:
        A callable taking the pages to spool and returning their records, in
        order. It may be called more than once; the sequence numbers continue,
        as they would within one acquisition pass.

    """
    sink = SpooledPageSink(spool_dir, _SPOOL_LABEL_A, _TEST_RESERVE_MB)

    def _spool(images: Sequence[Image.Image]) -> list[PageRecord]:
        """Spool every image and return the records the sink produced."""
        return [sink.add(image) for image in images]

    return _spool


@pytest.fixture
def one_page(
    spool_pages: Callable[[Sequence[Image.Image]], list[PageRecord]],
) -> list[PageRecord]:
    """
    Return a single spooled page, for cases that only need "a page exists".

    Returns:
        A one-element record list.

    """
    return spool_pages([Image.new("RGB", (100, 100), "white")])


class TestAssemblePdf:
    """PDF assembly tests, over spooled page records."""

    def test_assemble_single_page(
        self, one_page: list[PageRecord], output_dir: Path
    ) -> None:
        """A single spooled page produces a valid PDF file."""
        pdf_path = assemble_pdf(one_page, output_dir, filename="single.pdf", dpi=300)

        assert pdf_path.exists()
        assert pdf_path.stat().st_size > 0
        content = pdf_path.read_bytes()
        assert content[:5] == b"%PDF-"

    def test_assemble_multiple_pages(
        self,
        spool_pages: Callable[[Sequence[Image.Image]], list[PageRecord]],
        output_dir: Path,
    ) -> None:
        """Multiple pages produce a larger PDF than a single page."""
        records = spool_pages(
            [
                Image.new("RGB", (100, 100), "white"),
                Image.new("RGB", (200, 200), "red"),
                Image.new("RGB", (150, 150), "blue"),
            ]
        )

        single_pdf = assemble_pdf(records[:1], output_dir, filename="one.pdf", dpi=300)
        multi_pdf = assemble_pdf(records, output_dir, filename="many.pdf", dpi=300)

        assert multi_pdf.stat().st_size > single_pdf.stat().st_size

    def test_no_second_encode_of_any_page(
        self,
        spool_pages: Callable[[Sequence[Image.Image]], list[PageRecord]],
        spool_dir: Path,
        output_dir: Path,
    ) -> None:
        """
        D-03: assembly re-saves nothing -- it embeds the spooled PNG itself.

        The deleted code wrote a ``page_NNNN.png`` copy of every page into a
        ``TemporaryDirectory`` under ``output_dir`` and handed img2pdf those.
        Three facts together say it is gone: no PNG of any name survives
        anywhere under the output directory, the output directory holds nothing
        but the PDF, and the spool still holds exactly the pages that were
        spooled, byte for byte.
        """
        images = [Image.new("RGB", (100, 100), colour) for colour in ("white", "red")]
        records = spool_pages(images)
        before = {record.path.name: record.path.read_bytes() for record in records}

        assemble_pdf(records, output_dir, filename="embedded.pdf", dpi=300)

        assert list(output_dir.rglob("*.png")) == []
        assert sorted(path.name for path in output_dir.rglob("*")) == ["embedded.pdf"]
        after = {path.name: path.read_bytes() for path in spool_dir.iterdir()}
        assert after == before

    def test_no_second_encode_survives_a_failed_assembly(
        self,
        one_page: list[PageRecord],
        spool_dir: Path,
        output_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failure leaves no scratch PNG behind either, and keeps the spool."""
        monkeypatch.setattr(
            pdf_mod.img2pdf, "convert", _raising(RuntimeError("fake img2pdf error"))
        )
        before = sorted(path.name for path in spool_dir.iterdir())

        with pytest.raises(PdfError, match="fake img2pdf error"):
            assemble_pdf(one_page, output_dir, filename="boom.pdf", dpi=300)

        assert list(output_dir.rglob("*.png")) == []
        assert sorted(path.name for path in spool_dir.iterdir()) == before

    def test_output_path(self, one_page: list[PageRecord], output_dir: Path) -> None:
        """Output PDF is written to the specified directory with .pdf extension."""
        pdf_path = assemble_pdf(one_page, output_dir, filename="named.pdf", dpi=300)

        assert isinstance(pdf_path, Path)
        assert pdf_path.parent == output_dir
        assert pdf_path.name == "named.pdf"
        assert pdf_path.suffix == ".pdf"

    def test_page_order_is_the_record_order(
        self,
        spool_pages: Callable[[Sequence[Image.Image]], list[PageRecord]],
        output_dir: Path,
    ) -> None:
        """
        Reversing the records reverses the PDF, with no sort and no glob (D-02).

        The spooled names are unchanged between the two assemblies, so a
        directory listing cannot tell the two PDFs apart -- only the record
        order can.
        """
        records = spool_pages(
            [
                Image.new("RGB", (100, 100), "white"),
                Image.new("RGB", (100, 100), "black"),
            ]
        )

        forward = assemble_pdf(records, output_dir, filename="fwd.pdf", dpi=300)
        backward = assemble_pdf(
            list(reversed(records)), output_dir, filename="rev.pdf", dpi=300
        )

        assert _embedded_streams(forward) == list(reversed(_embedded_streams(backward)))


class TestBoundedAssembly:
    """
    D-03 as amended: one convert per page, then a qpdf merge.

    ``img2pdf.convert`` reads every input fully into memory and finalises the
    whole document before ``outputstream`` is written, so a single convert is
    linear in page count whether it streams or not (measured: 1395 MB and
    787 MB respectively at 48 pages). Converting one page at a time and merging
    the single-page PDFs with qpdf measured flat at 131 MB for both 12 and 48
    pages. These tests hold that shape in place and prove the merged document
    is not a different document.
    """

    def test_merge_produces_one_page_per_record_in_record_order(
        self,
        spool_pages: Callable[[Sequence[Image.Image]], list[PageRecord]],
        output_dir: Path,
    ) -> None:
        """Three records become three pages, each carrying its own spooled PNG."""
        records = spool_pages(
            [
                Image.new("RGB", (120, 160), "white"),
                Image.new("RGB", (120, 160), "red"),
                Image.new("RGB", (120, 160), "blue"),
            ]
        )

        pdf_path = assemble_pdf(records, output_dir, filename="merged.pdf", dpi=300)

        with pikepdf.open(pdf_path) as pdf:
            assert len(pdf.pages) == 3
        assert _embedded_streams(pdf_path) == [
            _png_idat_payload(record.path) for record in records
        ]

    def test_convert_runs_once_per_page_with_an_outputstream(
        self,
        spool_pages: Callable[[Sequence[Image.Image]], list[PageRecord]],
        output_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Assembly never hands img2pdf more than one page at a time."""
        records = spool_pages(
            [
                Image.new("RGB", (120, 160), colour)
                for colour in ("white", "red", "blue")
            ]
        )
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(pdf_mod.img2pdf, "convert", _recording_convert(calls))

        assemble_pdf(records, output_dir, filename="perpage.pdf", dpi=300)

        assert len(calls) == len(records)
        assert [call["images"] for call in calls] == [
            [str(record.path)] for record in records
        ]
        for call in calls:
            kwargs = call["kwargs"]
            assert isinstance(kwargs, dict)
            assert "outputstream" in kwargs

    def test_merge_runs_exactly_one_qpdf_job(
        self,
        spool_pages: Callable[[Sequence[Image.Image]], list[PageRecord]],
        output_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One qpdf job, over an argv built only from files assembly just wrote."""
        records = spool_pages(
            [Image.new("RGB", (120, 160), colour) for colour in ("white", "red")]
        )
        argvs: list[list[str]] = []
        monkeypatch.setattr(pdf_mod.pikepdf, "Job", _recording_job(argvs))

        pdf_path = assemble_pdf(records, output_dir, filename="job.pdf", dpi=300)

        assert len(argvs) == 1
        (argv,) = argvs
        assert argv[0] == "qpdf"
        assert "--empty" in argv
        assert "--pages" in argv
        assert argv[-1] == str(pdf_path)
        # T-29-29: every input named in the argv is a file this call created
        # inside its own scratch directory under the output directory.
        singles = argv[argv.index("--pages") + 1 : argv.index("--")]
        assert len(singles) == len(records)
        assert all(Path(single).is_relative_to(output_dir) for single in singles)

    def test_merge_matches_a_single_convert_pdf(
        self,
        spool_pages: Callable[[Sequence[Image.Image]], list[PageRecord]],
        output_dir: Path,
        tmp_path: Path,
    ) -> None:
        """
        The merged document is the single-convert document, page for page.

        Same page count, the same exact MediaBox on every page, and the same
        embedded image stream bytes -- which is what makes the amendment a
        change of memory profile rather than a change of output.
        """
        records = spool_pages(
            [
                Image.new("RGB", (2480, 3508), "white"),
                Image.new("RGB", (1240, 1754), "red"),
                Image.new("RGB", (2480, 3508), "blue"),
            ]
        )

        merged = assemble_pdf(records, output_dir, filename="merged.pdf", dpi=300)

        # No outputstream here, so convert returns the bytes -- the very
        # contract whose other half (None when streaming) retired the old
        # ``pdf_bytes is None`` guard.
        reference_bytes = img2pdf.convert(
            [str(record.path) for record in records],
            layout_fun=img2pdf.get_fixed_dpi_layout_fun((300, 300)),
        )
        assert reference_bytes is not None
        reference = tmp_path / "single-convert.pdf"
        reference.write_bytes(reference_bytes)
        assert _media_boxes(merged) == _media_boxes(reference)
        assert _embedded_streams(merged) == _embedded_streams(reference)

    def test_merge_leaves_no_single_page_pdf_behind(
        self,
        spool_pages: Callable[[Sequence[Image.Image]], list[PageRecord]],
        output_dir: Path,
    ) -> None:
        """The per-page scratch PDFs are temporary; only the merged one survives."""
        records = spool_pages(
            [Image.new("RGB", (120, 160), colour) for colour in ("white", "red")]
        )

        assemble_pdf(records, output_dir, filename="only.pdf", dpi=300)

        assert sorted(path.name for path in output_dir.rglob("*")) == ["only.pdf"]

    def test_merge_survives_duplex_records_sharing_a_sequence_number(
        self, spool_dir: Path, output_dir: Path
    ) -> None:
        """
        A duplex interleave hands assembly two records both numbered 1.

        ``PageRecord.sequence`` is assigned per acquisition pass, so pass A's
        first front and pass B's first back are both sequence 1 -- their
        *file names* differ, their numbers do not. Naming the single-page
        scratch PDFs from the sequence would therefore write one over the
        other and merge the survivor twice; naming them from the position in
        ``records`` cannot. Four distinct pages in, four distinct pages out.
        """
        fronts = SpooledPageSink(spool_dir, _SPOOL_LABEL_A, _TEST_RESERVE_MB)
        backs = SpooledPageSink(spool_dir, _SPOOL_LABEL_B, _TEST_RESERVE_MB)
        records = [
            fronts.add(Image.new("RGB", (120, 160), "white")),
            backs.add(Image.new("RGB", (120, 160), "red")),
            fronts.add(Image.new("RGB", (120, 160), "blue")),
            backs.add(Image.new("RGB", (120, 160), "green")),
        ]
        assert [record.sequence for record in records] == [1, 1, 2, 2]

        pdf_path = assemble_pdf(records, output_dir, filename="duplex.pdf", dpi=300)

        assert _embedded_streams(pdf_path) == [
            _png_idat_payload(record.path) for record in records
        ]

    def test_a_failing_merge_becomes_pdf_error(
        self,
        spool_pages: Callable[[Sequence[Image.Image]], list[PageRecord]],
        output_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        EXC-01 is unaffected: the existing broad boundary already covers pikepdf.

        ``Job.run()`` raises pikepdf exception types, which are ordinary
        ``Exception`` subclasses, so no new ``except`` clause was needed and the
        message keeps its shape -- the page count and the target path.
        """
        records = spool_pages(
            [Image.new("RGB", (120, 160), colour) for colour in ("white", "red")]
        )
        monkeypatch.setattr(pdf_mod.pikepdf, "Job", _FailingJob)

        with pytest.raises(PdfError) as excinfo:
            assemble_pdf(records, output_dir, "boom.pdf", dpi=300)

        message = str(excinfo.value)
        assert message.startswith(
            f"Could not assemble 2 page(s) into {output_dir / 'boom.pdf'}: "
        )
        assert message.endswith("fake qpdf merge failure")
        assert isinstance(excinfo.value.__cause__, pikepdf.PdfError)


# The flat-memory proof, run in a child process because the thing being
# measured is resident memory and only a fresh process has an honest peak.
#
# The pages are **random** pixels, deliberately: deflate cannot compress noise,
# so the PNG the spool writes lands within a few percent of the decoded size,
# and "the decoded size of the extra pages" below is then a faithful stand-in
# for what a linear assembly would have to be holding.
_MEMORY_PAGE_WIDTH = 1200
_MEMORY_PAGE_HEIGHT = 1200
_MEMORY_SMALL_PAGES = 4
_MEMORY_LARGE_PAGES = 16
_RGB_BANDS = 3
# ru_maxrss is reported in kilobytes on Linux (getrusage(2)); every other
# figure in this test is in bytes, so the conversion happens exactly once.
_RU_MAXRSS_UNIT_BYTES = 1024
# A bounded wait, not a sleep (TEST-02).  Measured at 0.9 s and 3.0 s for the
# two runs, so 20 s is slack rather than a limit, and two of them still leave
# the test far inside pytest-timeout's 60 s.
_MEMORY_CHILD_TIMEOUT_SECONDS = 20

_MEMORY_CHILD_SOURCE = """\
import os
import resource
from pathlib import Path

import pikepdf
from PIL import Image

from saneless.pdf import assemble_pdf
from saneless.spool import SpooledPageSink

page_count = int(os.environ["SANELESS_TEST_PAGES"])
width = int(os.environ["SANELESS_TEST_WIDTH"])
height = int(os.environ["SANELESS_TEST_HEIGHT"])
workspace = Path(os.environ["SANELESS_TEST_WORKSPACE"])

spool = workspace / "spool"
spool.mkdir(parents=True)
sink = SpooledPageSink(spool, "a", 1)

records = []
for _ in range(page_count):
    # One page decoded at a time: the parent is measuring assembly, so the
    # spooling phase must not be what sets the high-water mark.
    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    records.append(sink.add(image))
    del image

pdf_path = assemble_pdf(records, workspace / "out", "memory.pdf", 300)
with pikepdf.open(pdf_path) as pdf:
    assembled_pages = len(pdf.pages)

peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(f"pages={assembled_pages} peak_kib={peak}")
"""


def _measure_assembly(script: Path, workspace: Path, page_count: int) -> dict[str, int]:
    """
    Spool and assemble ``page_count`` pages in a child, and read its peak RSS.

    Every argv element is a literal and the per-run values travel in the
    environment, exactly as ``test_atomic_write._run_in_mount_namespace`` does.
    That is not decoration: ``sys.executable`` sitting in the argv is the one
    thing that takes the call off ruff's S603 allow-list, and this project adds
    no suppressions. ``shell=False`` throughout -- the shell is an explicit
    program running an explicit literal, with nothing interpolated into it.

    ``saneless`` is installed editable, so the child's imports resolve from
    ``sys.executable`` alone with no ``PYTHONPATH`` fiddling.

    Args:
        script: The child source, already written to disk.
        workspace: A directory the child creates its spool and output under.
        page_count: How many pages the child spools and assembles.

    Returns:
        ``{"pages": ..., "peak_kib": ...}`` as the child reported them.

    """
    env = {
        **os.environ,
        "SANELESS_TEST_PYTHON": sys.executable,
        "SANELESS_TEST_SCRIPT": str(script),
        "SANELESS_TEST_PAGES": str(page_count),
        "SANELESS_TEST_WIDTH": str(_MEMORY_PAGE_WIDTH),
        "SANELESS_TEST_HEIGHT": str(_MEMORY_PAGE_HEIGHT),
        "SANELESS_TEST_WORKSPACE": str(workspace),
    }
    completed = subprocess.run(
        ["/bin/sh", "-c", 'exec "$SANELESS_TEST_PYTHON" "$SANELESS_TEST_SCRIPT"'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_MEMORY_CHILD_TIMEOUT_SECONDS,
    )

    assert completed.returncode == 0, completed.stderr
    fields = dict(item.split("=", 1) for item in completed.stdout.split())
    return {"pages": int(fields["pages"]), "peak_kib": int(fields["peak_kib"])}


class TestAssemblyMemory:
    """HARD-01's memory sentence, measured rather than asserted."""

    def test_peak_memory_is_flat_in_page_count(self, tmp_path: Path) -> None:
        """
        Assembling four times as many pages does not cost four times the RAM.

        Measured the way RESEARCH.md Finding 3 measured it: peak
        ``ru_maxrss`` in a child process. ``tracemalloc`` is unusable for this
        -- a 26 MB Pillow image adds 460 bytes to its traced total, because the
        pixels are malloc'd in C -- and in-process measurement has no honest
        peak anyway once an earlier test has already grown the heap.

        The same measurement against the single-convert implementation grew
        from 454 MB at 12 pages to 1395 MB at 48, and to 787 MB with
        ``outputstream=`` alone; the per-page-plus-qpdf path measured flat at
        131 MB for both. A regression to either of the old shapes fails here.
        """
        script = tmp_path / "measure_assembly_memory.py"
        script.write_text(_MEMORY_CHILD_SOURCE, encoding="utf-8")

        small = _measure_assembly(script, tmp_path / "small", _MEMORY_SMALL_PAGES)
        large = _measure_assembly(script, tmp_path / "large", _MEMORY_LARGE_PAGES)

        assert small["pages"] == _MEMORY_SMALL_PAGES
        assert large["pages"] == _MEMORY_LARGE_PAGES

        # The budget is derived, not chosen: it is exactly what the extra
        # pages weigh decoded, so exceeding it means assembly was holding
        # them.  A linear assembly holds each page's compressed bytes *and*
        # the whole finished document, so it overshoots this by about 2x.
        decoded_page_bytes = _MEMORY_PAGE_WIDTH * _MEMORY_PAGE_HEIGHT * _RGB_BANDS
        extra_pages = _MEMORY_LARGE_PAGES - _MEMORY_SMALL_PAGES
        budget_bytes = decoded_page_bytes * extra_pages
        growth_bytes = (large["peak_kib"] - small["peak_kib"]) * _RU_MAXRSS_UNIT_BYTES

        assert growth_bytes < budget_bytes, (
            f"peak grew {growth_bytes} bytes over {extra_pages} extra pages, "
            f"which is not less than the {budget_bytes} bytes they decode to"
        )


class TestPdfBoundary:
    """
    ``assemble_pdf`` is a module boundary that raises only ``PdfError``.

    M-17 and D-04: img2pdf raises seven unrelated error classes plus bare
    ``Exception``, ``TypeError`` and ``ValueError``, and Pillow raises
    ``OSError`` and ``SystemError`` while a page is read.  Each must surface as
    ``PdfError`` carrying the original text, with the original chained on
    ``__cause__`` (Phase 28 success criterion 1).
    """

    @pytest.mark.parametrize(
        "error_cls",
        IMG2PDF_ERROR_CLASSES,
        ids=[cls.__name__ for cls in IMG2PDF_ERROR_CLASSES],
    )
    def test_img2pdf_error_class_becomes_pdf_error(
        self,
        one_page: list[PageRecord],
        output_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        error_cls: type[Exception],
    ) -> None:
        """Each img2pdf error class is translated, with the original chained."""
        original = error_cls("boom from img2pdf")
        monkeypatch.setattr(pdf_mod.img2pdf, "convert", _raising(original))

        with pytest.raises(PdfError, match="boom from img2pdf") as excinfo:
            assemble_pdf(one_page, output_dir, "x.pdf", dpi=300)

        assert excinfo.value.__cause__ is original
        assert not isinstance(excinfo.value, error_cls)

    @pytest.mark.parametrize(
        "original",
        [Exception("bare"), TypeError("typed"), ValueError("valued")],
        ids=["Exception", "TypeError", "ValueError"],
    )
    def test_untyped_img2pdf_raise_becomes_pdf_error(
        self,
        one_page: list[PageRecord],
        output_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        original: Exception,
    ) -> None:
        """img2pdf's untyped raises are translated too."""
        monkeypatch.setattr(pdf_mod.img2pdf, "convert", _raising(original))

        with pytest.raises(PdfError, match=str(original)) as excinfo:
            assemble_pdf(one_page, output_dir, "x.pdf", dpi=300)

        assert excinfo.value.__cause__ is original

    def test_an_unreadable_spooled_page_becomes_pdf_error(
        self, one_page: list[PageRecord], output_dir: Path
    ) -> None:
        """
        A real failure from the imaging stack, on the read side, is translated.

        Nothing is monkeypatched here: the page is genuinely corrupt, so
        img2pdf really does fail opening it and really does raise one of the
        seven classes the boundary cannot name in a tuple.
        """
        one_page[0].path.write_bytes(b"not a PNG at all")

        with pytest.raises(PdfError, match="cannot read input image") as excinfo:
            assemble_pdf(one_page, output_dir, "x.pdf", dpi=300)

        assert isinstance(excinfo.value.__cause__, img2pdf.ImageOpenError)

    def test_an_unwritable_output_directory_becomes_pdf_error(
        self, one_page: list[PageRecord], tmp_path: Path
    ) -> None:
        """A real OSError creating the output directory is translated too."""
        blocker = tmp_path / "blocker"
        blocker.write_text("a regular file where the directory should be")

        with pytest.raises(PdfError) as excinfo:
            assemble_pdf(one_page, blocker / "out", "x.pdf", dpi=300)

        assert isinstance(excinfo.value.__cause__, OSError)
        assert str(blocker / "out" / "x.pdf") in str(excinfo.value)

    def test_message_names_page_count_and_target_path(
        self,
        one_page: list[PageRecord],
        output_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D-08: the one-line message names the page count and the PDF path."""
        monkeypatch.setattr(pdf_mod.img2pdf, "convert", _raising(ValueError("valued")))

        with pytest.raises(PdfError) as excinfo:
            assemble_pdf(one_page, output_dir, "x.pdf", dpi=300)

        message = str(excinfo.value)
        assert message.startswith(
            f"Could not assemble 1 page(s) into {output_dir / 'x.pdf'}: "
        )
        assert message.endswith("valued")

    def test_empty_page_list_is_refused_before_img2pdf(
        self, output_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """EXC-03: an empty list never reaches img2pdf's empty-list ValueError."""
        calls: list[object] = []

        def fake_convert(*args: object, **_kwargs: object) -> bytes:
            calls.append(args)
            return b""

        monkeypatch.setattr(pdf_mod.img2pdf, "convert", fake_convert)

        with pytest.raises(PdfError, match="no pages"):
            assemble_pdf([], output_dir, "x.pdf", dpi=300)

        assert calls == []

    def test_keyboard_interrupt_is_not_translated(
        self,
        one_page: list[PageRecord],
        output_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cancel is a BaseException and passes through the boundary untouched."""
        monkeypatch.setattr(pdf_mod.img2pdf, "convert", _raising(KeyboardInterrupt()))

        with pytest.raises(KeyboardInterrupt):
            assemble_pdf(one_page, output_dir, "x.pdf", dpi=300)


class TestSanitiseTitleForFilename:
    """Allow-list sanitisation of a user-supplied title into a path segment."""

    def test_ordinary_title(self) -> None:
        """A plain title becomes a lowercase, hyphen-separated slug."""
        assert sanitise_title_for_filename("Invoice 2026") == "invoice-2026"

    @pytest.mark.parametrize("hostile", HOSTILE_TITLES)
    def test_cannot_escape_base_directory(self, tmp_path: Path, hostile: str) -> None:
        """No hostile title escapes the directory its slug is joined onto."""
        base = tmp_path.resolve()
        resolved = (base / sanitise_title_for_filename(hostile)).resolve()

        assert resolved.is_relative_to(base)

    @pytest.mark.parametrize("hostile", HOSTILE_TITLES)
    def test_contains_no_dangerous_character(self, hostile: str) -> None:
        """Separators, dots, NUL and tildes are gone by construction."""
        result = sanitise_title_for_filename(hostile)

        assert re.fullmatch(r"[a-z0-9-]*", result), result

    def test_absolute_path_yields_relative_segment(self) -> None:
        """A leading slash cannot survive into an absolute path segment."""
        result = sanitise_title_for_filename("/etc/passwd")

        assert not Path(result).is_absolute()
        assert result == "etc-passwd"

    def test_nul_byte_is_stripped(self) -> None:
        """An embedded NUL, which truncates a path at the C layer, is removed."""
        assert "\x00" not in sanitise_title_for_filename("a\x00b")

    @pytest.mark.parametrize("title", ["...", "日本語", "", "   ", "!!!"])
    def test_fully_stripped_title_is_empty(self, title: str) -> None:
        """A title with nothing allow-listed in it sanitises to the empty string."""
        assert sanitise_title_for_filename(title) == ""

    def test_length_is_capped(self) -> None:
        """An overlong title is capped well below NAME_MAX."""
        result = sanitise_title_for_filename("x" * 500)

        assert len(result) <= 60

    def test_cap_does_not_leave_a_trailing_separator(self) -> None:
        """Truncating mid-word never leaves a dangling hyphen."""
        result = sanitise_title_for_filename(("a" * 59) + " " + ("b" * 20))

        assert not result.endswith("-")

    def test_never_starts_or_ends_with_a_separator(self) -> None:
        """Leading and trailing punctuation does not become a leading hyphen."""
        result = sanitise_title_for_filename("  ...Quarterly Report!!!  ")

        assert result == "quarterly-report"

    def test_runs_of_separators_collapse(self) -> None:
        """Adjacent non-allow-listed characters collapse to a single hyphen."""
        result = sanitise_title_for_filename("a   ///   b")

        assert "--" not in result
        assert result == "a-b"


class TestBuildPdfFilename:
    """Composition of the unique, safe PDF file name."""

    def test_ends_with_pdf_and_carries_the_job_id(self) -> None:
        """The name is a .pdf and contains the first 8 characters of the job id."""
        name = build_pdf_filename(JOB_A, "Tax Return")

        assert name.endswith(".pdf")
        assert "aaaaaaaa" in name
        assert "tax-return" in name

    def test_same_title_different_job_ids_differ(self) -> None:
        """Uniqueness survives two jobs sharing a title."""
        assert build_pdf_filename(JOB_A, "Tax Return") != build_pdf_filename(
            JOB_B, "Tax Return"
        )

    def test_same_job_id_and_title_is_stable(self) -> None:
        """Uniqueness comes from the job id, not from the clock."""
        for _ in range(3):
            # Retried so a second boundary falling between the two calls
            # cannot make this flaky; three straddles in a row is impossible.
            if build_pdf_filename(JOB_A, "Tax Return") == build_pdf_filename(
                JOB_A, "Tax Return"
            ):
                return
        pytest.fail("build_pdf_filename is not stable for one job id and title")

    def test_empty_title_leaves_no_dangling_separator(self) -> None:
        """A title that sanitises away drops its segment rather than emptying it."""
        name = build_pdf_filename(JOB_A, "")

        assert name.endswith(".pdf")
        assert "-.pdf" not in name
        assert "aaaaaaaa" in name

    def test_untranslatable_title_drops_its_segment(self) -> None:
        """A title of only non-allow-listed characters behaves like an empty one."""
        name = build_pdf_filename(JOB_A, "日本語")

        assert "-.pdf" not in name
        assert name.endswith(".pdf")

    def test_starts_with_a_utc_timestamp(self) -> None:
        """The name is sortable: a YYYYmmdd-HHMMSS prefix leads it."""
        name = build_pdf_filename(JOB_A, "Tax Return")

        assert re.match(r"^\d{8}-\d{6}-", name), name

    @pytest.mark.parametrize("hostile", HOSTILE_TITLES)
    def test_whole_name_stays_inside_its_directory(
        self, tmp_path: Path, hostile: str
    ) -> None:
        """The composed name is a single safe segment, not a path."""
        base = tmp_path.resolve()
        resolved = (base / build_pdf_filename(JOB_A, hostile)).resolve()

        assert resolved.is_relative_to(base)
        assert resolved.parent == base

    @pytest.mark.parametrize("hostile", HOSTILE_TITLES)
    def test_a_hostile_job_id_also_cannot_escape(
        self, tmp_path: Path, hostile: str
    ) -> None:
        """The job id is a path segment too, so it is sanitised as well."""
        base = tmp_path.resolve()
        resolved = (base / build_pdf_filename(hostile, "Tax Return")).resolve()

        assert resolved.is_relative_to(base)
        assert resolved.parent == base

    def test_name_fits_within_restrictive_filesystem_limits(self) -> None:
        """The composed name stays under eCryptfs's 143-byte name limit."""
        name = build_pdf_filename(JOB_A, "x" * 500)

        assert len(name.encode()) <= 143


class TestMediaBox:
    """Page geometry: a page scanned at N DPI must declare N DPI (OUTC-06)."""

    def test_a4_at_300_dpi_is_an_a4_page(
        self,
        spool_pages: Callable[[Sequence[Image.Image]], list[PageRecord]],
        output_dir: Path,
    ) -> None:
        """An exact A4 raster at 300 DPI yields a 595 x 842 pt MediaBox."""
        # Rounded, never compared exactly: the true value is 595.2 x 841.92.
        records = spool_pages([Image.new("RGB", (2480, 3508), "white")])
        pdf_path = assemble_pdf(records, output_dir, filename="a4.pdf", dpi=300)

        with pikepdf.open(pdf_path) as pdf:
            box = _rounded_media_box(pdf.pages[0])

        assert box == [0, 0, 595, 842]

    def test_a4_page_is_not_the_unlayouted_default(
        self,
        spool_pages: Callable[[Sequence[Image.Image]], list[PageRecord]],
        output_dir: Path,
    ) -> None:
        """The layout function is provably in play, not passing by accident."""
        # img2pdf.default_dpi is 96, so an unlayouted 2480 x 3508 raster
        # becomes 1860 x 2631 pt.  Seeing that means no layout_fun was passed.
        records = spool_pages([Image.new("RGB", (2480, 3508), "white")])
        pdf_path = assemble_pdf(records, output_dir, filename="a4.pdf", dpi=300)

        with pikepdf.open(pdf_path) as pdf:
            box = _rounded_media_box(pdf.pages[0])

        assert box != [0, 0, 1860, 2631]

    def test_cropped_a4_also_rounds_to_a4(
        self,
        spool_pages: Callable[[Sequence[Image.Image]], list[PageRecord]],
        output_dir: Path,
    ) -> None:
        """What crop_to_paper_size really produces is 2480 x 3507, and still A4."""
        # int(297 * 300 / 25.4) == 3507, so the exact box is 595.2 x 841.68.
        cropped = crop_to_paper_size(Image.new("RGB", (2600, 3700), "white"), "a4", 300)
        assert cropped.size == (2480, 3507)

        records = spool_pages([cropped])
        pdf_path = assemble_pdf(records, output_dir, filename="a4.pdf", dpi=300)

        with pikepdf.open(pdf_path) as pdf:
            box = _rounded_media_box(pdf.pages[0])

        assert box == [0, 0, 595, 842]

    def test_every_page_gets_the_same_fixed_dpi(
        self,
        spool_pages: Callable[[Sequence[Image.Image]], list[PageRecord]],
        output_dir: Path,
    ) -> None:
        """A fixed-DPI layout applies unconditionally, page by page."""
        records = spool_pages(
            [
                Image.new("RGB", (2480, 3508), "white"),
                Image.new("RGB", (2480, 3508), "white"),
                Image.new("RGB", (1240, 1754), "white"),
            ]
        )
        pdf_path = assemble_pdf(records, output_dir, filename="mixed.pdf", dpi=300)

        with pikepdf.open(pdf_path) as pdf:
            boxes = [_rounded_media_box(page) for page in pdf.pages]

        assert boxes[0] == [0, 0, 595, 842]
        assert boxes[1] == [0, 0, 595, 842]
        # Half the pixels at the same DPI is half the page, not a rescaled A4.
        assert boxes[2] == [0, 0, 298, 421]
