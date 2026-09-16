"""Tests for pipeline orchestration."""

from __future__ import annotations

import base64
import errno
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pikepdf
import pytest
from PIL import Image, ImageDraw

import saneless.pipeline as pipeline_module
import saneless.scanner.sane_backend as sane_backend_mod
from saneless.config import ProfileConfig
from saneless.exceptions import (
    ConfigError,
    FeederEmptyError,
    PaperlessError,
    PaperlessTimeoutError,
    ScanCancelledError,
    ScanError,
)
from saneless.paperless import UploadResult
from saneless.pdf import assemble_pdf
from saneless.pipeline import (
    _SPOOL_LABEL_A,
    _SPOOL_LABEL_B,
    FAILED_DIR_WARN_THRESHOLD,
    FlipAnswerSlot,
    FlipCoordinator,
    PipelineEvent,
    PipelineRequest,
    ScanResult,
    _check_disk_space,
    _interleave_duplex,
    _preserving,
    run_pipeline,
)
from saneless.scanner.base import DeviceInfo, ScannerBackend
from saneless.scanner.sane_backend import SaneBackend
from saneless.spool import SpooledPageSink
from saneless.vocabulary import (
    ErrorCategory,
    FlipOutcome,
    JobState,
    ScanOutcome,
    classify_error,
)
from tests.conftest import (
    AlwaysContinueFlipCoordinator,
    spooling,
    spooling_in_turn,
)
from tests.fake_sane import FakeSaneDev, FakeSaneModule

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from saneless.config import Settings
    from saneless.scanner.base import PageRecord, PageSink, ScanBatch, ScanSettings


class TestRunPipeline:
    """Pipeline orchestration tests."""

    def test_run_pipeline_happy_path(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Full pipeline: scan -> assemble -> upload succeeds."""
        default_settings.output.tmp_dir = str(tmp_path)

        request = PipelineRequest(profile_name="default", title="Happy Path Doc")
        result = run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning is None
        mock_scanner.scan_pages.assert_called_once()
        mock_paperless.upload_document.assert_called_once()

    def test_run_pipeline_scan_error(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Scanner raises ScanError -> pipeline raises ScanError."""
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = ScanError("Device not found")

        request = PipelineRequest(profile_name="default", title="Scan Error Doc")
        with pytest.raises(ScanError, match="Device not found"):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

    def test_run_pipeline_upload_error(
        self,
        mock_scanner: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Paperless raises PaperlessError -> pipeline raises PaperlessError."""
        # data_dir is pointed at tmp_path too: a failing delivery now preserves
        # the assembled PDF into <data_dir>/failed/, and a test must not write
        # that into the shared default outside pytest's own temp directory.
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.output.data_dir = str(tmp_path / "state")

        paperless = MagicMock()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        request = PipelineRequest(profile_name="default", title="Upload Error Doc")
        with pytest.raises(PaperlessError, match="Upload failed"):
            run_pipeline(
                scanner=mock_scanner,
                paperless=paperless,
                settings=default_settings,
                request=request,
            )

    def test_run_pipeline_temp_cleanup(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """After successful run, tmp_dir has no leftover scan files."""
        default_settings.output.tmp_dir = str(tmp_path)

        request = PipelineRequest(profile_name="default", title="Cleanup Doc")
        run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        # The TemporaryDirectory should be cleaned up
        remaining = list(tmp_path.iterdir())
        # Only the output PDF may remain, no tmp subdirs
        for item in remaining:
            assert not item.is_dir(), f"Leftover directory: {item}"

    def test_run_pipeline_temp_cleanup_on_error(
        self, default_settings: Settings, tmp_path: Path
    ) -> None:
        """After failed run, tmp_dir has no leftover scan files."""
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = ScanError("Boom")
        paperless = MagicMock()

        request = PipelineRequest(profile_name="default", title="Error Cleanup Doc")
        with pytest.raises(ScanError, match="Boom"):
            run_pipeline(
                scanner=scanner,
                paperless=paperless,
                settings=default_settings,
                request=request,
            )

        remaining = list(tmp_path.iterdir())
        for item in remaining:
            assert not item.is_dir(), f"Leftover directory: {item}"

    def test_run_pipeline_calls_poll(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """After upload, pipeline calls poll_task with returned UUID."""
        default_settings.output.tmp_dir = str(tmp_path)
        mock_paperless.upload_document.return_value = UploadResult(
            delivered_to_api=True, task_uuid="task-uuid-123"
        )
        mock_paperless.poll_task.return_value = {"status": "SUCCESS"}

        request = PipelineRequest(profile_name="default", title="Poll Doc")
        run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        mock_paperless.poll_task.assert_called_once()
        call_args = mock_paperless.poll_task.call_args
        assert call_args[0][0] == "task-uuid-123"

    def test_run_pipeline_status_callback(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Pipeline calls status_callback with PipelineEvent enum values in order."""
        default_settings.output.tmp_dir = str(tmp_path)
        events: list[PipelineEvent] = []

        request = PipelineRequest(
            profile_name="default",
            title="Status Doc",
            status_callback=events.append,
        )
        run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert events[0] is PipelineEvent.SCANNING
        assert events[1] is PipelineEvent.ASSEMBLING
        assert events[2] is PipelineEvent.UPLOADING
        assert events[3] is PipelineEvent.DONE


def _make_content_image(color: str = "black") -> Image.Image:
    """Create an image with visible content (not empty)."""
    img = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 180, 280], fill=color)
    return img


def _make_empty_image() -> Image.Image:
    """Create a nearly-white image that should be detected as empty."""
    return Image.new("RGB", (200, 300), (254, 254, 254))


# The reserve the spool keeps free beyond each page, in megabytes.  One is the
# smallest honest value: these pages are tens of kilobytes, so the per-page
# check passes anywhere the suite can run and is still a real check.
_TEST_RESERVE_MB = 1


def _distinct_page(index: int) -> Image.Image:
    """
    Draw a page no other page of the same job can be mistaken for.

    The index is drawn as a run of marks along the top edge, so two pages
    differ in their **pixels** -- and therefore in their spooled PNG bytes and
    in the stream the PDF embeds -- rather than only in a file name.  A test
    that reads a PDF back to prove page order needs exactly that: content it
    can tell apart without trusting the thing under test.

    The body rectangle keeps every page far from the blank-page thresholds, so
    empty-page detection never removes one by accident.

    Args:
        index: The 0-based page number.  Up to 28 pages fit across the top
            edge, which is more than any test here scans.

    Returns:
        A 120x160 RGB page carrying that index.

    """
    page = Image.new("RGB", (120, 160), "white")
    draw = ImageDraw.Draw(page)
    draw.rectangle((10, 30, 110, 150), fill="black")
    for mark in range(index + 1):
        left = 2 + mark * 4
        draw.rectangle((left, 2, left + 2, 8), fill="black")
    return page


def _spool_pass(
    directory: Path, label: str, pages: Sequence[Image.Image]
) -> list[PageRecord]:
    """
    Spool one acquisition pass into ``directory`` and return its records.

    A real ``SpooledPageSink``, so the records name files that exist and carry
    statistics measured from real pixels.  The label is the pipeline's own
    constant, not a re-spelled ``"a"`` or ``"b"``, so the naming convention has
    one definition.

    Args:
        directory: The shared spool directory.  Must already exist.
        label: ``_SPOOL_LABEL_A`` for a simplex pass or a duplex job's fronts,
            ``_SPOOL_LABEL_B`` for its backs.
        pages: The pages this pass feeds, in acquisition order.

    Returns:
        One record per page, in acquisition order.

    """
    sink = SpooledPageSink(directory, label, _TEST_RESERVE_MB)
    return [sink.add(page) for page in pages]


def _duplex_spool(
    tmp_path: Path, fronts: int, backs: int
) -> tuple[Path, list[PageRecord], list[PageRecord]]:
    """
    Spool a manual-duplex job's two passes into one shared spool directory.

    Exactly as the pipeline does it: two sinks over the same directory, ``a-``
    for the fronts and ``b-`` for the backs, so the file names really do sort
    into all-fronts-then-all-backs while the document order is the interleave.

    Args:
        tmp_path: pytest's per-test directory; the spool is created under it.
        fronts: How many pages pass A feeds.
        backs: How many pages pass B feeds.

    Returns:
        ``(spool directory, front records, back records)``.  Every page carries
        distinct content, across both passes.

    """
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    front_records = _spool_pass(
        spool_dir, _SPOOL_LABEL_A, [_distinct_page(index) for index in range(fronts)]
    )
    back_records = _spool_pass(
        spool_dir,
        _SPOOL_LABEL_B,
        [_distinct_page(fronts + index) for index in range(backs)],
    )
    return spool_dir, front_records, back_records


def _keep_everything(
    pages: Sequence[PageRecord], **_thresholds: float
) -> list[PageRecord]:
    """
    Stand in for ``filter_empty_pages``, keeping every record it is given.

    A ``return_value`` cannot be used for this any more: the records the
    pipeline goes on to assemble have to be the ones its own sink produced, and
    a list built in the test would point at files nobody wrote.

    Args:
        pages: Whatever the pipeline passed.
        _thresholds: The profile thresholds, ignored here and asserted on
            through the mock's call args.

    Returns:
        The same records, in the same order.

    """
    return list(pages)


def _reading_the_pages(pdf_path: Path, seen: list[Image.Image]) -> Callable[..., Path]:
    """
    Build a stand-in for ``assemble_pdf`` that opens each page as it is called.

    The spooled pages live inside the job's workspace, a ``TemporaryDirectory``
    that is gone by the time ``run_pipeline`` returns, so an assertion about
    what is *in* a page has to take its copy while the job is still running.

    Args:
        pdf_path: The path the stand-in reports having written.  It is created,
            because a failing delivery's preservation guard moves it.
        seen: Filled with the pages, fully loaded, in record order.

    Returns:
        A callable with ``assemble_pdf``'s own shape, ready for ``side_effect``.

    """

    def _assemble(
        records: Sequence[PageRecord],
        output_dir: Path,
        *,
        filename: str,
        dpi: int,
    ) -> Path:
        """Open every spooled page, then report the PDF path."""
        for record in records:
            page = Image.open(record.path)
            # Forces the read now and lets Pillow close the file it opened; a
            # lazy ImageFile would trip the suite's ResourceWarning-as-error.
            page.load()
            seen.append(page)
        pdf_path.write_bytes(b"%PDF-fake")
        return pdf_path

    return _assemble


def _png_idat(png_bytes: bytes) -> bytes:
    """
    Concatenate a PNG's IDAT payloads: its compressed pixel data itself.

    This is what img2pdf embeds when it passes a suitable PNG through -- the
    zlib stream is copied into a ``/FlateDecode`` image object untouched -- so
    it is directly comparable with what pikepdf reads back out of the PDF.
    Comparing these bytes is a much stronger claim than comparing decoded
    pixels: it says the PDF's page *is* that spooled file, not merely a page
    that looks like it.

    Args:
        png_bytes: A whole PNG file, as the spool wrote it.

    Returns:
        Every IDAT chunk's payload, concatenated in file order.

    """
    payload = bytearray()
    # 8-byte signature, then length/type/data/CRC chunks to the end.
    position = 8
    while position < len(png_bytes):
        length = int.from_bytes(png_bytes[position : position + 4], "big")
        chunk_type = png_bytes[position + 4 : position + 8]
        if chunk_type == b"IDAT":
            payload += png_bytes[position + 8 : position + 8 + length]
        position += 12 + length
    return bytes(payload)


def _embedded_streams(pdf_path: Path) -> list[bytes]:
    """
    Read each PDF page's single embedded image stream, in page order.

    Raw, not decoded, so the result can be compared with ``_png_idat``.

    Args:
        pdf_path: The assembled PDF to read.

    Returns:
        One raw stream per page, in the order the pages appear in the PDF.

    """
    streams: list[bytes] = []
    with pikepdf.open(pdf_path) as pdf:
        for page in pdf.pages:
            (image,) = pikepdf.Page(page).images.values()
            streams.append(image.read_raw_bytes())
    return streams


class _AssemblingSomewhereDurable:
    """
    Stand in for ``assemble_pdf`` by calling the real one, somewhere durable.

    The pipeline assembles inside its per-job workspace, a
    ``TemporaryDirectory``: by the time ``run_pipeline`` returns, the PDF and
    every page it embedded are gone.  A read-back therefore has to take its
    copies while the job is still running.

    The real ``assemble_pdf`` still does the work, so what is read back
    afterwards is the production artefact rather than a test's imitation of
    one.  Only the destination changes.
    """

    def __init__(self, output_dir: Path) -> None:
        """
        Remember where the PDF should be written instead.

        Args:
            output_dir: A directory outside the job workspace.  It need not
                exist; ``assemble_pdf`` creates it.

        """
        self._output_dir = output_dir
        self.records: list[PageRecord] = []
        self.page_bytes: list[bytes] = []
        self.pdf_path: Path | None = None

    def __call__(
        self,
        records: Sequence[PageRecord],
        output_dir: Path,
        *,
        filename: str,
        dpi: int,
    ) -> Path:
        """
        Copy the spooled pages out, then assemble them for real.

        Args:
            records: The pages the pipeline chose to assemble, in document
                order.
            output_dir: The workspace directory the pipeline asked for, which
                is exactly what this stand-in exists to override.
            filename: The PDF's file name, used unchanged.
            dpi: The resolution the device reported, used unchanged.

        Returns:
            The assembled PDF's path, outside the workspace.

        """
        self.records = list(records)
        self.page_bytes = [record.path.read_bytes() for record in records]
        self.pdf_path = assemble_pdf(
            records, self._output_dir, filename=filename, dpi=dpi
        )
        return self.pdf_path


def _spooling_at_each_resolution(
    *passes: tuple[Sequence[Image.Image], int],
) -> Callable[[str, ScanSettings, PageSink], ScanBatch]:
    """
    Build one ``side_effect`` reporting a different resolution per call.

    ``spooling_in_turn`` carries one resolution for the whole job, which is
    right for every manual-duplex case except the one that exists precisely
    because the device changed its mind between the two passes.

    ONE callable that counts its own calls, never a list of callables:
    ``unittest.mock`` consumes a list ``side_effect`` as an iterable of
    *results* and hands each element back uncalled, so a list of functions
    would make ``scan_pages`` return a function object.

    Args:
        passes: One ``(pages, resolution)`` pair per expected call, in order.

    Returns:
        A callable with ``scan_pages``' own shape, for ``MagicMock.side_effect``.

    """
    calls = 0

    def _spool_next(
        device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """Spool this call's pages at this call's reported resolution."""
        nonlocal calls
        index = calls
        calls += 1
        if index >= len(passes):
            msg = (
                f"scan_pages was called {calls} time(s), but only "
                f"{len(passes)} pass(es) were prepared"
            )
            raise AssertionError(msg)
        pages, resolution = passes[index]
        return spooling(pages, resolution=resolution)(device_id, settings, sink)

    return _spool_next


class TestInterleave:
    """Unit tests for _interleave_duplex, over spooled page records."""

    def test_interleave_basic(self, tmp_path: Path) -> None:
        """Interleave 3 fronts + 3 backs correctly reverses backs."""
        _, fronts, backs = _duplex_spool(tmp_path, 3, 3)
        result = _interleave_duplex(fronts, backs)
        assert len(result) == 6
        # Backs are reversed: the last sheet fed in pass B is page 2.
        # Result: front1, back3, front2, back2, front3, back1
        assert result[0] is fronts[0]
        assert result[1] is backs[2]  # reversed
        assert result[2] is fronts[1]
        assert result[3] is backs[1]  # reversed
        assert result[4] is fronts[2]
        assert result[5] is backs[0]  # reversed

    def test_interleave_single_page(self, tmp_path: Path) -> None:
        """Single front + single back works."""
        _, fronts, backs = _duplex_spool(tmp_path, 1, 1)
        result = _interleave_duplex(fronts, backs)
        assert len(result) == 2
        assert result[0] is fronts[0]
        assert result[1] is backs[0]

    def test_interleave_count_mismatch_raises(self, tmp_path: Path) -> None:
        """Mismatched front/back counts raise ScanError."""
        _, fronts, backs = _duplex_spool(tmp_path, 3, 2)
        with pytest.raises(ScanError, match="Page count mismatch: 3 fronts, 2 backs"):
            _interleave_duplex(fronts, backs)


class TestPageOrderComesFromTheRecordsNeverTheFilesystem:
    """
    D-02 and D-04: document order is the record list's order, and nothing else.

    The invariant both tests attack is one sentence: **nothing ever sorts or
    globs the spool directory to recover page order.**  Order is carried by the
    record list, and ``PageRecord.sequence`` is the proof of what the device
    fed.

    They attack it from opposite ends.  The twelve-page test follows a whole
    simplex job out the far side, reading the assembled PDF back and matching
    each of its pages against one specific spooled file.  The interleave test
    builds a duplex job whose filename order is deliberately *not* its document
    order, and asserts that difference before asserting the order itself -- so
    an implementation that ever reached for ``sorted()`` or ``glob()`` would
    fail it rather than pass it by luck.
    """

    def test_twelve_page_order_survives_into_the_assembled_pdf(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        A twelve-page scan is pages 1..12 of the PDF, each carrying its own page.

        The equivalence being asserted, stated so this test cannot quietly
        weaken into "there are twelve pages": for every position *i*, the raw
        image stream pikepdf reads out of PDF page *i* is **byte-identical** to
        the concatenated IDAT payload of the PNG that record *i* names.  That
        is an equality between a PDF page and one specific spooled file.  The
        twelve spooled files are asserted to be twelve *distinct* byte strings
        first, so a PDF of twelve identical pages cannot satisfy it, and the
        records are asserted to carry ``sequence`` 1..12, so the order being
        matched is the order the device fed.

        The real ``assemble_pdf`` runs: the stand-in only redirects the output
        somewhere that outlives the job's workspace.
        """
        default_settings.output.tmp_dir = str(tmp_path / "scratch")
        pages = [_distinct_page(index) for index in range(12)]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling(pages)

        assembly = _AssemblingSomewhereDurable(tmp_path / "out")
        with patch("saneless.pipeline.assemble_pdf", assembly):
            result = run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=PipelineRequest(profile_name="default", title="Twelve Pages"),
            )

        assert result.outcome is ScanOutcome.SUCCESS
        assert result.pages_scanned == 12
        assert result.pages_removed == 0
        assert [record.sequence for record in assembly.records] == list(range(1, 13))
        # Distinct content, so the read-back below cannot be satisfied by
        # twelve copies of one page.
        assert len(set(assembly.page_bytes)) == 12

        assert assembly.pdf_path is not None
        streams = _embedded_streams(assembly.pdf_path)
        assert len(streams) == 12
        assert streams == [_png_idat(page) for page in assembly.page_bytes]

    def test_interleave_records_never_recovers_order_from_the_filesystem(
        self, tmp_path: Path
    ) -> None:
        """
        A duplex job whose filename order is deliberately not its document order.

        Pass A spools ``a-0001`` to ``a-0003`` and pass B ``b-0001`` to
        ``b-0003`` into one directory, so sorting that directory by name yields
        all three fronts and then all three backs.  The document order is
        front1, back3, front2, back2, front3, back1: a different order.

        The difference is asserted **first**, on purpose.  Without that
        assertion this test would still pass against an implementation that
        rebuilt the page list from a sorted glob, which is exactly the mistake
        D-02 exists to forbid.

        Nothing on disk moves: the interleave reorders records only, and every
        record's ``path`` is the same before and after.
        """
        spool_dir, fronts, backs = _duplex_spool(tmp_path, 3, 3)
        paths_before = [record.path for record in [*fronts, *backs]]
        on_disk = [path.name for path in sorted(spool_dir.iterdir())]

        interleaved = _interleave_duplex(fronts, backs)
        document_order = [record.path.name for record in interleaved]

        assert on_disk != document_order
        assert on_disk == [
            "a-0001.png",
            "a-0002.png",
            "a-0003.png",
            "b-0001.png",
            "b-0002.png",
            "b-0003.png",
        ]
        assert document_order == [
            "a-0001.png",
            "b-0003.png",
            "a-0002.png",
            "b-0002.png",
            "a-0003.png",
            "b-0001.png",
        ]
        assert interleaved == [
            fronts[0],
            backs[2],
            fronts[1],
            backs[1],
            fronts[2],
            backs[0],
        ]
        assert [path.name for path in sorted(spool_dir.iterdir())] == on_disk
        assert [record.path for record in [*fronts, *backs]] == paths_before


class TestPipelineThumbnail:
    """Thumbnail generation in pipeline."""

    def test_thumbnail_callback_called_flatbed(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Flatbed scan calls thumbnail_callback with non-empty base64 string."""
        default_settings.output.tmp_dir = str(tmp_path)
        # Use a content image so it doesn't get filtered as empty
        mock_scanner.scan_pages.side_effect = spooling([_make_content_image()])

        thumb_results: list[str] = []
        request = PipelineRequest(
            profile_name="default",
            title="Thumb Test",
            thumbnail_callback=thumb_results.append,
        )
        run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert len(thumb_results) == 1
        assert len(thumb_results[0]) > 0
        # Should be valid base64
        base64.b64decode(thumb_results[0])

    def test_no_thumbnail_callback_ok(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Pipeline works without thumbnail_callback."""
        default_settings.output.tmp_dir = str(tmp_path)
        mock_scanner.scan_pages.side_effect = spooling([_make_content_image()])

        request = PipelineRequest(
            profile_name="default",
            title="No Thumb Test",
        )
        # Should not raise
        run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )


class TestPipelineEmptyPageFilter:
    """Empty page filtering in pipeline."""

    def test_empty_pages_filtered(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Pipeline with 5 pages (3 content + 2 empty) assembles PDF with only 3."""
        default_settings.output.tmp_dir = str(tmp_path)

        content_pages = [_make_content_image(c) for c in ["black", "red", "blue"]]
        empty_pages = [_make_empty_image(), _make_empty_image()]
        all_pages = [
            content_pages[0],
            empty_pages[0],
            content_pages[1],
            empty_pages[1],
            content_pages[2],
        ]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling(all_pages)

        request = PipelineRequest(profile_name="default", title="Filter Test")

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

            # assemble_pdf should receive only the 3 content page records
            called_records = mock_assemble.call_args[0][0]
            assert len(called_records) == 3

    def test_custom_thresholds_from_profile(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Custom thresholds from ProfileConfig are passed to filter_empty_pages."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].empty_page_mean_threshold = 200.0
        default_settings.profiles["default"].empty_page_stddev_threshold = 10.0

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling([_make_content_image()])

        request = PipelineRequest(profile_name="default", title="Threshold Test")

        with patch("saneless.pipeline.filter_empty_pages", wraps=None) as mock_filter:
            # A side_effect, not a return_value: what comes back has to be the
            # records the pipeline's own sink produced, because assembly reads
            # their files.
            mock_filter.side_effect = _keep_everything
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )
            mock_filter.assert_called_once()
            _, kwargs = mock_filter.call_args
            assert kwargs["mean_threshold"] == 200.0
            assert kwargs["stddev_threshold"] == 10.0

    def test_all_pages_empty_raises(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        A non-empty batch that detection empties says "All pages were blank".

        EXC-03: the blank message is reserved for detection really removing
        every page, so it names what happened rather than an empty scan.
        """
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling(
            [_make_empty_image(), _make_empty_image()]
        )

        request = PipelineRequest(profile_name="default", title="All Empty Test")
        with pytest.raises(ScanError, match=r"^All pages were blank$"):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )


class TestFlipAnswerSlot:
    """
    The one claim-once answer both flip coordinators compose (IN-02).

    Every wait here is bounded, and none sleeps: a wait on an answered slot
    returns because its event is already set, and a wait on an unanswered one
    is given ``0``.
    """

    def test_a_new_slot_is_unanswered(self) -> None:
        """Nothing has claimed a freshly built slot."""
        assert FlipAnswerSlot().answer is None

    def test_the_first_offer_claims_and_a_later_one_is_dropped(self) -> None:
        """D-16: the first offer is the answer, and a later offer changes nothing."""
        slot = FlipAnswerSlot()
        assert slot.offer(FlipOutcome.CONTINUED) is True
        assert slot.answer is FlipOutcome.CONTINUED
        assert slot.offer(FlipOutcome.ABORTED) is False
        assert slot.answer is FlipOutcome.CONTINUED

    def test_settle_claims_an_unanswered_slot(self) -> None:
        """Settling an unanswered slot makes the offered outcome the answer."""
        slot = FlipAnswerSlot()
        assert slot.settle(FlipOutcome.TIMED_OUT) is FlipOutcome.TIMED_OUT
        assert slot.answer is FlipOutcome.TIMED_OUT

    def test_settle_returns_the_answer_already_in_effect(self) -> None:
        """A settle that loses the race hands back the answer that beat it."""
        slot = FlipAnswerSlot()
        slot.offer(FlipOutcome.CONTINUED)
        assert slot.settle(FlipOutcome.TIMED_OUT) is FlipOutcome.CONTINUED
        assert slot.answer is FlipOutcome.CONTINUED

    def test_settle_wakes_a_waiter(self) -> None:
        """A settled slot's wait returns at once: settle sets the event too."""
        slot = FlipAnswerSlot()
        slot.settle(FlipOutcome.ABORTED)
        started = time.monotonic()
        slot.wait(5)
        assert time.monotonic() - started < 1

    def test_a_zero_wait_on_an_unanswered_slot_returns_promptly(self) -> None:
        """``wait(0)`` is a valid bound and returns without an answer."""
        slot = FlipAnswerSlot()
        started = time.monotonic()
        slot.wait(0)
        assert time.monotonic() - started < 1
        assert slot.answer is None

    def test_an_offer_wakes_a_waiter(self) -> None:
        """After a claiming offer, a long wait returns at once."""
        slot = FlipAnswerSlot()
        slot.offer(FlipOutcome.CONTINUED)
        started = time.monotonic()
        slot.wait(5)
        assert time.monotonic() - started < 1

    def test_racing_offers_claim_exactly_once(self) -> None:
        """
        Twenty threads offering at once yield exactly one claim.

        A barrier releases every thread together, so the offers genuinely
        contend for the lock; the answer left behind is the winner's outcome.
        """
        slot = FlipAnswerSlot()
        contenders = 20
        barrier = threading.Barrier(contenders, timeout=5)
        results: list[tuple[FlipOutcome, bool]] = []
        results_lock = threading.Lock()

        def _offer(outcome: FlipOutcome) -> None:
            """Wait for the others, then offer ``outcome`` and record the result."""
            barrier.wait()
            claimed = slot.offer(outcome)
            with results_lock:
                results.append((outcome, claimed))

        threads = [
            threading.Thread(
                target=_offer,
                args=(FlipOutcome.CONTINUED if i % 2 else FlipOutcome.ABORTED,),
            )
            for i in range(contenders)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert len(results) == contenders
        winners = [outcome for outcome, claimed in results if claimed]
        assert len(winners) == 1
        assert slot.answer is winners[0]

    def test_the_slot_is_public_api(self) -> None:
        """``FlipAnswerSlot`` is exported next to the coordinator contract."""
        assert "FlipAnswerSlot" in pipeline_module.__all__


class _FixedFlipCoordinator(FlipCoordinator):
    """
    A flip coordinator that resolves to one chosen outcome, and records the ask.

    Subclasses the ABC for the same reason as ``AlwaysContinueFlipCoordinator``:
    a contract change must reach this stub through the type checkers.
    """

    def __init__(
        self,
        outcome: FlipOutcome,
        events: list[PipelineEvent] | None = None,
    ) -> None:
        """
        Remember the outcome to answer with and the event log to snapshot.

        Args:
            outcome: What every wait resolves to.
            events: The status-callback log, snapshotted at each wait so a test
                can see which events preceded it.

        """
        self._outcome = outcome
        self._events = events if events is not None else []
        self.timeouts: list[float] = []
        self.events_at_wait: list[PipelineEvent] = []

    def wait_for_flip(self, timeout: float) -> FlipOutcome:
        """
        Record the timeout and the events so far, then answer at once.

        Args:
            timeout: The bound the pipeline asked for.

        Returns:
            The outcome this stub was built with.

        """
        self.timeouts.append(timeout)
        self.events_at_wait = list(self._events)
        return self._outcome


class _BrokenPromptFlipCoordinator(FlipCoordinator):
    """A flip coordinator whose prompt broke: ``ABORTED``, with the cause kept."""

    def __init__(self, cause: Exception) -> None:
        """
        Remember the exception the broken prompt raised.

        Args:
            cause: What the prompt failed with.

        """
        self._cause = cause

    def wait_for_flip(self, timeout: float) -> FlipOutcome:
        """
        Answer at once, as a prompt that failed does.

        Args:
            timeout: Ignored; the answer is immediate.

        Returns:
            Always ``FlipOutcome.ABORTED``.

        """
        return FlipOutcome.ABORTED

    @property
    def abort_cause(self) -> Exception | None:
        """The exception the broken prompt raised."""
        return self._cause


class TestFlipCoordinatorContract:
    """The parts of the flip contract a coordinator gets without writing them."""

    def test_abort_cause_defaults_to_none(self) -> None:
        """
        A coordinator implementing only ``wait_for_flip`` reports no abort cause.

        ``abort_cause`` is concrete on the ABC, so an ``ABORTED`` answer from a
        coordinator that never overrides it is always an operator's abort -- a
        cancel -- and existing coordinators need no change (D-02).
        """
        coordinator = _FixedFlipCoordinator(FlipOutcome.ABORTED)

        assert coordinator.wait_for_flip(0) is FlipOutcome.ABORTED
        assert coordinator.abort_cause is None

    def test_abort_cause_adds_no_fourth_flip_outcome(self) -> None:
        """The cause travels beside the outcome, never as a new member (D-09)."""
        assert set(FlipOutcome) == {
            FlipOutcome.CONTINUED,
            FlipOutcome.ABORTED,
            FlipOutcome.TIMED_OUT,
        }


class TestZeroPages:
    """
    An empty batch is reported truthfully at the pipeline boundary (EXC-03, N-06).

    Before this check an empty batch was misreported as all-blank with
    detection on, and leaked img2pdf's bare ``ValueError`` with it off or on an
    empty duplex half. The SANE backend never returns an empty batch --
    an empty feeder raises ``FeederEmptyError`` (Phase 24 D-03) -- so these
    tests drive the pipeline's contract check with a stubbed backend.
    """

    @pytest.mark.parametrize("detection", [True, False])
    def test_an_empty_simplex_batch_says_no_pages_were_scanned(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        *,
        detection: bool,
    ) -> None:
        """
        An empty simplex batch raises "No pages were scanned", detection on or off.

        Assembly and upload are never reached, so img2pdf never sees an empty
        list (N-06).
        """
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].enable_empty_page_detection = detection

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling([])

        request = PipelineRequest(profile_name="default", title="Zero Pages")
        with (
            patch("saneless.pipeline.assemble_pdf") as mock_assemble,
            pytest.raises(ScanError, match=r"^No pages were scanned$"),
        ):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        mock_assemble.assert_not_called()
        mock_paperless.upload_document.assert_not_called()

    def test_an_empty_pass_a_fails_before_anyone_is_asked_to_flip(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """An empty manual-duplex pass A raises before the flip prompt (EXC-03)."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling([])

        coordinator = _FixedFlipCoordinator(FlipOutcome.CONTINUED)
        request = PipelineRequest(
            profile_name="default",
            title="Empty Pass A",
            flip_coordinator=coordinator,
        )
        with pytest.raises(ScanError, match=r"^No pages were scanned$"):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert coordinator.timeouts == []
        assert scanner.scan_pages.call_count == 1
        mock_paperless.upload_document.assert_not_called()

    def test_an_empty_pass_b_fails_before_any_half_is_assembled(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        An empty manual-duplex pass B raises before the count comparison (EXC-03).

        Without the check the counts differ and the mismatch recovery would try
        to assemble the empty back half.  The message names the pass and what
        pass A scanned: "No pages were scanned" would be false once the fronts
        were fed (IN-01).
        """
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling_in_turn([_make_content_image()], [])

        request = PipelineRequest(
            profile_name="default",
            title="Empty Pass B",
            flip_coordinator=_FixedFlipCoordinator(FlipOutcome.CONTINUED),
        )
        with (
            patch("saneless.pipeline.assemble_pdf") as mock_assemble,
            pytest.raises(
                ScanError,
                match=(
                    r"^No back pages were scanned in pass B "
                    r"\(pass A scanned 1 front page\(s\)\)$"
                ),
            ),
        ):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert scanner.scan_pages.call_count == 2
        mock_assemble.assert_not_called()
        mock_paperless.upload_document.assert_not_called()

    def test_all_blank_pages_say_all_pages_were_blank(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Detection removing every page of a non-empty batch is "blank" (EXC-03)."""
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling([_make_empty_image()])

        request = PipelineRequest(profile_name="default", title="All Blank")
        with (
            patch("saneless.pipeline.assemble_pdf") as mock_assemble,
            pytest.raises(ScanError, match=r"^All pages were blank$"),
        ):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        mock_assemble.assert_not_called()

    def test_an_empty_feeder_keeps_its_own_message(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        ``FeederEmptyError`` propagates unchanged (Phase 24 D-03).

        The zero-page check runs on a returned batch, so it can never replace
        the backend's more specific feeder message.
        """
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = FeederEmptyError("No paper detected in feeder")

        request = PipelineRequest(profile_name="default", title="Feeder Empty")
        with pytest.raises(FeederEmptyError, match=r"^No paper detected in feeder$"):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        mock_paperless.upload_document.assert_not_called()


class TestManualDuplex:
    """Manual duplex pipeline tests."""

    def test_manual_duplex_happy_path(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Manual duplex: 3 fronts + 3 backs -> 6 interleaved pages."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        fronts = [_make_content_image(c) for c in ["red", "green", "blue"]]
        backs = [_make_content_image(c) for c in ["cyan", "magenta", "yellow"]]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling_in_turn(fronts, backs)

        request = PipelineRequest(
            profile_name="default",
            title="Duplex Test",
            flip_coordinator=AlwaysContinueFlipCoordinator(),
        )

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

            called_records = mock_assemble.call_args[0][0]
            assert len(called_records) == 6

    def test_duplex_mismatch_saves_partial_pdfs(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Pass A yields 3 pages, pass B yields 2 -> saves both as separate PDFs."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        fronts = [_make_content_image() for _ in range(3)]
        backs = [_make_content_image() for _ in range(2)]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling_in_turn(fronts, backs)

        request = PipelineRequest(
            profile_name="default",
            title="Mismatch Test",
            flip_coordinator=AlwaysContinueFlipCoordinator(),
        )

        result = run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        # Both partial PDFs uploaded
        assert mock_paperless.upload_document.call_count == 2
        first_call = mock_paperless.upload_document.call_args_list[0]
        second_call = mock_paperless.upload_document.call_args_list[1]
        assert "(fronts)" in first_call[0][1]
        assert "(backs)" in second_call[0][1]

        # Returns SUCCESS with a warning, not an error
        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning is not None
        assert "Page count mismatch: 3 fronts, 2 backs" in result.warning
        # Both partial PDFs count as uploaded pages
        assert result.pages_scanned == 5
        assert result.pages_removed == 0
        assert result.pages_uploaded == 5

    def test_duplex_mismatch_reports_fallback_when_upload_falls_back(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        A mismatch whose partial PDFs only reached the consume dir is FALLBACK.

        The mismatch path uploads twice. If either upload fell back, the run did
        not reach paperless-ngx and must not claim SUCCESS -- that is precisely
        the lie ScanOutcome.FALLBACK exists to prevent (CTR-02).
        """
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        mock_paperless.upload_document.return_value = UploadResult(
            delivered_to_api=False,
            consume_dir_path=tmp_path / "consume" / "doc.pdf",
        )

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling_in_turn(
            [_make_content_image() for _ in range(3)],
            [_make_content_image() for _ in range(2)],
        )

        result = run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default",
                title="Mismatch Fallback",
                flip_coordinator=AlwaysContinueFlipCoordinator(),
            ),
        )

        assert result.outcome is ScanOutcome.FALLBACK
        assert result.warning is not None
        assert "Page count mismatch" in result.warning
        mock_paperless.poll_task.assert_not_called()

    def test_duplex_mismatch_is_fallback_when_only_one_upload_falls_back(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A partially-delivered mismatch is still FALLBACK, not SUCCESS (CTR-02)."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        mock_paperless.upload_document.side_effect = [
            UploadResult(delivered_to_api=True, task_uuid="fronts-task"),
            UploadResult(
                delivered_to_api=False,
                consume_dir_path=tmp_path / "consume" / "backs.pdf",
            ),
        ]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling_in_turn(
            [_make_content_image() for _ in range(3)],
            [_make_content_image() for _ in range(2)],
        )

        result = run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default",
                title="Half Delivered",
                flip_coordinator=AlwaysContinueFlipCoordinator(),
            ),
        )

        assert result.outcome is ScanOutcome.FALLBACK

    def test_duplex_match_still_interleaves_normally(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Matching front/back counts still interleave and upload single PDF."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        fronts = [_make_content_image("red"), _make_content_image("blue")]
        backs = [_make_content_image("green"), _make_content_image("yellow")]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling_in_turn(fronts, backs)

        request = PipelineRequest(
            profile_name="default",
            title="Normal Duplex",
            flip_coordinator=AlwaysContinueFlipCoordinator(),
        )

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            result = run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

            # Normal path: single PDF uploaded
            mock_paperless.upload_document.assert_called_once()
            assert result.warning is None

    def test_manual_duplex_empty_page_after_interleave(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Empty page detection runs on interleaved result, not individual passes."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        # 2 fronts: 1 content + 1 content, 2 backs: 1 empty + 1 content
        fronts = [_make_content_image("red"), _make_content_image("blue")]
        backs = [_make_empty_image(), _make_content_image("green")]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling_in_turn(fronts, backs)

        request = PipelineRequest(
            profile_name="default",
            title="Duplex Filter Test",
            flip_coordinator=AlwaysContinueFlipCoordinator(),
        )

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

            # 4 interleaved - 1 empty = 3 pages
            called_records = mock_assemble.call_args[0][0]
            assert len(called_records) == 3

    def test_manual_duplex_thumbnail_from_first_page(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Thumbnail generated from first front page in manual duplex."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        fronts = [_make_content_image()]
        backs = [_make_content_image()]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling_in_turn(fronts, backs)

        thumb_results: list[str] = []
        request = PipelineRequest(
            profile_name="default",
            title="Duplex Thumb Test",
            thumbnail_callback=thumb_results.append,
            flip_coordinator=AlwaysContinueFlipCoordinator(),
        )

        run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert len(thumb_results) == 1
        assert len(thumb_results[0]) > 0

    def test_manual_duplex_waits_on_the_coordinator_with_the_configured_timeout(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        The flip wait is one bounded call to the coordinator (DPLX-04, DPLX-05).

        ``AWAITING_FLIP`` is announced before the wait and ``SCANNING_REVERSE``
        only after it, and the timeout handed over is the configured one.
        """
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.output.flip_timeout_seconds = 42
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling_in_turn(
            [_make_content_image()], [_make_content_image()]
        )

        events: list[PipelineEvent] = []
        coordinator = _FixedFlipCoordinator(FlipOutcome.CONTINUED, events)
        request = PipelineRequest(
            profile_name="default",
            title="Coordinator Wait Test",
            status_callback=events.append,
            flip_coordinator=coordinator,
        )

        run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert coordinator.timeouts == [42]
        assert coordinator.events_at_wait[-1] is PipelineEvent.AWAITING_FLIP
        assert PipelineEvent.SCANNING_REVERSE not in coordinator.events_at_wait
        assert PipelineEvent.SCANNING_REVERSE in events

    def test_manual_duplex_abort_at_the_flip_prompt_is_a_cancel(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        An operator's ABORTED cancels the run before pass B (EXC-04, N-08).

        Web Abort, n, Ctrl-D and Ctrl-C all reach the pipeline as ``ABORTED``
        with no ``abort_cause``: someone chose to stop, so the run raises
        ``ScanCancelledError`` -- never a ``ScanError`` -- and the worker and
        the CLI record a cancel rather than a scanner failure (D-01, D-02).
        """
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling([_make_content_image()])

        request = PipelineRequest(
            profile_name="default",
            title="Abort Test",
            flip_coordinator=_FixedFlipCoordinator(FlipOutcome.ABORTED),
        )

        with pytest.raises(
            ScanCancelledError,
            match=r"^Manual duplex scan cancelled at the flip prompt$",
        ) as excinfo:
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert not isinstance(excinfo.value, ScanError)
        assert scanner.scan_pages.call_count == 1
        mock_paperless.upload_document.assert_not_called()

    def test_manual_duplex_broken_prompt_abort_is_a_scan_failure_not_a_cancel(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        An ``ABORTED`` with an ``abort_cause`` fails the run, chained to the cause.

        A broken terminal prompt is not anyone's choice to stop (D-02, WR-08),
        so it raises ``ScanError`` -- exit 1 at the CLI, ERROR on the web --
        naming what broke, with the original exception as ``__cause__``.
        """
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling([_make_content_image()])
        cause = OSError(5, "Input/output error")

        request = PipelineRequest(
            profile_name="default",
            title="Broken Prompt Test",
            flip_coordinator=_BrokenPromptFlipCoordinator(cause),
        )

        with pytest.raises(
            ScanError, match=r"^Flip prompt failed: \[Errno 5\] Input/output error$"
        ) as excinfo:
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert not isinstance(excinfo.value, ScanCancelledError)
        assert excinfo.value.__cause__ is cause
        assert scanner.scan_pages.call_count == 1
        mock_paperless.upload_document.assert_not_called()

    def test_manual_duplex_flip_wait_timeout_raises(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """TIMED_OUT fails the run naming the flip wait and its timeout (DPLX-05)."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.output.flip_timeout_seconds = 17
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling([_make_content_image()])

        request = PipelineRequest(
            profile_name="default",
            title="Timeout Test",
            flip_coordinator=_FixedFlipCoordinator(FlipOutcome.TIMED_OUT),
        )

        with pytest.raises(ScanError, match="flip wait") as excinfo:
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert "17" in str(excinfo.value)
        assert scanner.scan_pages.call_count == 1
        mock_paperless.upload_document.assert_not_called()


def _two_pass_scanner() -> MagicMock:
    """
    Build a scanner mock whose ``scan_pages`` calls can be counted.

    It has two page lists queued, so a simplex run makes one call and a manual
    duplex run makes two. ``get_devices`` reports one device, so an
    auto-detecting run can proceed if nothing stops it first.
    """
    scanner = MagicMock(spec=ScannerBackend)
    scanner.get_devices.return_value = [
        DeviceInfo(name="test:auto:001", vendor="V", model="M", device_type="t"),
    ]
    scanner.scan_pages.side_effect = spooling_in_turn(
        [_make_content_image("red")], [_make_content_image("blue")]
    )
    return scanner


class TestDuplexStrategy:
    """
    ``profile.duplex`` alone chooses the scanning strategy (DPLX-01, DPLX-03).

    ``source`` is a pure SANE value: a source that merely reads like manual
    duplex must not start the two-pass flow, and a manual-duplex profile must
    not need a special source name.
    """

    def test_manual_duplex_without_a_coordinator_is_refused_before_the_scanner(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        No coordinator means no run, and the device is never contacted.

        The device is left empty so that ``_resolve_device`` would call
        ``get_devices``. The refusal has to come before that call, and so before
        the first ``scan_pages`` call too.
        """
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.scanner.device = ""
        default_settings.profiles["default"] = ProfileConfig(
            source="ADF Front", duplex="manual"
        )
        scanner = _two_pass_scanner()

        with pytest.raises(ConfigError) as excinfo:
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=PipelineRequest(profile_name="default", title="No Coordinator"),
            )

        message = str(excinfo.value)
        assert "'default'" in message
        assert "flip coordinator" in message
        scanner.get_devices.assert_not_called()
        scanner.scan_pages.assert_not_called()
        mock_paperless.upload_document.assert_not_called()

    def test_a_manual_duplex_looking_source_on_a_simplex_profile_runs_simplex(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """An explicit ``duplex = "none"`` wins over a legacy-looking source."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"] = ProfileConfig(
            source="Manual Duplex", duplex="none"
        )
        scanner = _two_pass_scanner()

        result = run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=PipelineRequest(profile_name="default", title="Looks Duplex"),
        )

        assert scanner.scan_pages.call_count == 1
        assert result.pages_scanned == 1

    def test_manual_duplex_needs_no_special_source_name(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A ``duplex = "manual"`` profile on a plain feeder source scans twice."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"] = ProfileConfig(
            source="ADF Front", duplex="manual"
        )
        scanner = _two_pass_scanner()

        result = run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default",
                title="Plain Feeder Duplex",
                flip_coordinator=AlwaysContinueFlipCoordinator(),
            ),
        )

        assert scanner.scan_pages.call_count == 2
        assert result.pages_scanned == 2


class TestManualDuplexOverTheSharedFake:
    """
    A manual duplex run driven through a real SaneBackend (SCNR-07, M-32).

    Every other pipeline test here hands ``run_pipeline`` a
    ``MagicMock(spec=ScannerBackend)``, which can only ever return what the test
    already told it to return.  A SANE-level defect on a duplex path -- a source
    never assigned to the device, a feeder never rewound, a second pass handing
    back the first pass's sheets -- cannot surface through a mock like that,
    which is M-32's actual complaint.  This test drives the pipeline through the
    real backend over the one shared fake, so such a defect can.

    The two passes share one device handle.  ``scan_pages`` opens and closes the
    device per pass and drains the feeder to its end, so the stack has to be
    reloaded between them -- which is precisely the physical act manual duplex
    asks the operator to perform.  The pipeline announces that moment with
    ``AWAITING_FLIP`` *before* it asks the flip coordinator, so the status
    callback is the honest place to do the reload: no second device, no thread,
    and the reload happens exactly when the operator's would.
    """

    def test_both_passes_run_through_the_backend_and_interleave(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Three fronts and three backs become six interleaved pages.

        The device is named the way consumer feeders really are: it reports
        ``"Automatic Document Feeder"``, with no plain ``"ADF"`` and no
        manual-duplex pseudo-source, while the profile says ``source = "ADF"``
        as the how-to teaches. Only resolving the feeder from the device's own
        list can make this pass (C-01, D-02).
        """
        default_settings.output.tmp_dir = str(tmp_path)
        profile = default_settings.profiles["default"]
        profile.source = "ADF"
        profile.duplex = "manual"
        # The fake carries the real device's list constraints, which reject an
        # unlisted value -- so the mode is the device's own spelling.
        profile.mode = "Color"

        dev = FakeSaneDev()
        dev.report_sources(["Flatbed", "Automatic Document Feeder"])
        fronts = [_make_content_image(c) for c in ["red", "green", "blue"]]
        backs = [_make_content_image(c) for c in ["cyan", "magenta", "yellow"]]
        dev.load_feeder(fronts)
        monkeypatch.setattr(sane_backend_mod, "sane", FakeSaneModule(device=dev))

        def _reload_the_stack(event: PipelineEvent) -> None:
            """Put the flipped stack back when the pipeline asks for it."""
            if event is PipelineEvent.AWAITING_FLIP:
                dev.load_feeder(backs)

        request = PipelineRequest(
            profile_name="default",
            title="Duplex over the shared fake",
            status_callback=_reload_the_stack,
            flip_coordinator=AlwaysContinueFlipCoordinator(),
        )

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            result = run_pipeline(
                scanner=SaneBackend(),
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

            assembled = mock_assemble.call_args[0][0]

        assert len(assembled) == 6
        assert result.outcome is ScanOutcome.SUCCESS
        # The backend really drove the device on both passes: the source was
        # assigned each time, and twelve feeder calls is six start/snap pairs.
        assert dev.assignments.count("source") == 2
        assert dev.calls.count("snap") == 6
        assert dev.source == "Automatic Document Feeder"

    def test_a_device_with_no_feeder_refuses_before_pass_a(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A flatbed-plus-Auto device fails loudly instead of snapshotting twice.

        This is C-01's silent path: ``Auto`` on offer, ``auto_source_mode`` at
        its ``"flatbed"`` default, and manual duplex used to take one platen
        snapshot per pass and report a green Complete. Now no page is taken,
        the operator is never asked to flip, and nothing is uploaded.
        """
        default_settings.output.tmp_dir = str(tmp_path)
        profile = default_settings.profiles["default"]
        profile.source = "ADF"
        profile.duplex = "manual"
        profile.mode = "Color"

        dev = FakeSaneDev()
        dev.report_sources(["Flatbed", "Auto"])
        monkeypatch.setattr(sane_backend_mod, "sane", FakeSaneModule(device=dev))

        events: list[PipelineEvent] = []
        request = PipelineRequest(
            profile_name="default",
            title="No feeder",
            status_callback=events.append,
            flip_coordinator=AlwaysContinueFlipCoordinator(),
        )

        with pytest.raises(ScanError, match="feeder") as excinfo:
            run_pipeline(
                scanner=SaneBackend(),
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert "'Auto'" in str(excinfo.value)
        assert dev.calls == []
        assert PipelineEvent.AWAITING_FLIP not in events
        mock_paperless.upload_document.assert_not_called()


class TestExifStripped:
    """No EXIF reaches the PDF, now that the page reaches it as a file."""

    def test_exif_stripped_before_pdf(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        A page arriving with EXIF is spooled without it (Pitfall #5).

        The pipeline no longer strips EXIF page by page, and restoring that
        loop would have nothing to act on: what reaches ``assemble_pdf`` is a
        record naming a PNG, and Pillow's PNG encoder emits an EXIF chunk only
        for one handed to it through ``encoderinfo``, which the spool never
        does.  The property the deleted loop protected is therefore still true,
        and this asserts it where it is now decided -- on the spooled file the
        PDF embeds, read while that file still exists.
        """
        default_settings.output.tmp_dir = str(tmp_path)

        img = _make_content_image()
        img.info["exif"] = b"fake-exif-data"

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling([img])

        spooled: list[Image.Image] = []
        request = PipelineRequest(profile_name="default", title="EXIF Test")

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.side_effect = _reading_the_pages(
                tmp_path / "output.pdf", spooled
            )

            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert len(spooled) == 1
        for page in spooled:
            assert "exif" not in page.info


class TestEmptyPageDetectionToggle:
    """Empty page detection toggle gating in pipeline."""

    def test_empty_page_filter_skipped_when_disabled(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """When enable_empty_page_detection=False, every page is kept."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].enable_empty_page_detection = False

        # Use pages that would normally be filtered as empty
        empty_pages = [_make_empty_image(), _make_empty_image()]
        content_page = _make_content_image()
        all_pages = [content_page, *empty_pages]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling(all_pages)

        request = PipelineRequest(profile_name="default", title="Toggle Test")

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

            # All 3 pages should be kept since detection is disabled
            called_records = mock_assemble.call_args[0][0]
            assert len(called_records) == 3


class TestFlatbedStillWorks:
    """Flatbed regression tests."""

    def test_flatbed_single_page_with_thumbnail(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Single-page flatbed scan produces correct PDF with thumbnail."""
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling([_make_content_image()])

        thumb_results: list[str] = []
        request = PipelineRequest(
            profile_name="default",
            title="Flatbed Test",
            thumbnail_callback=thumb_results.append,
        )

        run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert len(thumb_results) == 1
        mock_paperless.upload_document.assert_called_once()


class TestDiskSpaceCheck:
    """Disk space pre-flight check tests."""

    def test_disk_space_check_passes_when_sufficient(self, tmp_path: Path) -> None:
        """No exception when free space exceeds minimum."""
        _check_disk_space(str(tmp_path), 1)

    def test_disk_space_check_fails_when_insufficient(self, tmp_path: Path) -> None:
        """Raises ScanError when free space below threshold."""
        with pytest.raises(ScanError, match="Insufficient disk space"):
            _check_disk_space(str(tmp_path), 999_999_999)

    @pytest.mark.parametrize(
        "failing_call", ["mkdir", "disk_usage", "TemporaryDirectory"]
    )
    def test_workspace_filesystem_failure_is_a_config_error(
        self,
        failing_call: str,
        tmp_path: Path,
        default_settings: Settings,
        mock_paperless: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        IN-07: a tmp_dir that cannot be used is a setup error, not a bug.

        Each of the three start-up calls can raise a raw OSError -- a full
        disk, or tmp_dir removed since start-up.  Untranslated, it classified
        UNKNOWN and the CLI called it a saneless bug (exit 5), while
        ``validate_settings_dirs`` reports the same condition as a ConfigError
        at start-up.  Nothing is scanned.
        """
        tmp_dir = tmp_path / "work"
        default_settings.output.tmp_dir = str(tmp_dir)
        failure = OSError(errno.ENOSPC, "No space left on device")
        if failing_call == "mkdir":
            (tmp_path / "blocker").write_text("")
            tmp_dir = tmp_path / "blocker" / "work"
            default_settings.output.tmp_dir = str(tmp_dir)
        elif failing_call == "disk_usage":

            def failing_disk_usage(*_args: object) -> object:
                raise failure

            monkeypatch.setattr(
                "saneless.pipeline.shutil.disk_usage", failing_disk_usage
            )
        else:

            def failing_temporary_directory(
                *_args: object, **_kwargs: object
            ) -> object:
                raise failure

            monkeypatch.setattr(
                "saneless.pipeline.tempfile.TemporaryDirectory",
                failing_temporary_directory,
            )
        scanner = MagicMock(spec=ScannerBackend)

        with pytest.raises(ConfigError) as exc_info:
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=PipelineRequest(profile_name="default", title="No room"),
            )

        message = str(exc_info.value)
        assert message.startswith(
            f"Could not prepare the working directory {tmp_dir}: "
        )
        assert isinstance(exc_info.value.__cause__, OSError)
        scanner.scan_pages.assert_not_called()


class TestPipelineEventEnum:
    """PipelineEvent StrEnum tests."""

    def test_pipeline_event_enum_members(self) -> None:
        """All 6 PipelineEvent members exist with correct string values."""
        assert PipelineEvent.SCANNING == "SCANNING"
        assert PipelineEvent.AWAITING_FLIP == "AWAITING_FLIP"
        assert PipelineEvent.SCANNING_REVERSE == "SCANNING_REVERSE"
        assert PipelineEvent.ASSEMBLING == "ASSEMBLING"
        assert PipelineEvent.UPLOADING == "UPLOADING"
        assert PipelineEvent.DONE == "DONE"
        assert len(PipelineEvent) == 6

    def test_pipeline_emits_enum_events(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """All status_callback values are PipelineEvent instances, not strings."""
        default_settings.output.tmp_dir = str(tmp_path)
        events: list[object] = []

        request = PipelineRequest(
            profile_name="default",
            title="Enum Check",
            status_callback=events.append,
        )
        run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert len(events) > 0
        for event in events:
            assert isinstance(event, PipelineEvent)

    @pytest.mark.parametrize("event", list(PipelineEvent))
    def test_job_state_projection_is_total(self, event: PipelineEvent) -> None:
        """Every PipelineEvent projects to a JobState, with no None escape (DPLX-06)."""
        assert isinstance(event.job_state, JobState)

    def test_job_state_projection_mapping(self) -> None:
        """Each state-changing event names the state the worker persists (CTR-01)."""
        assert PipelineEvent.SCANNING.job_state is JobState.SCANNING
        assert PipelineEvent.AWAITING_FLIP.job_state is JobState.AWAITING_FLIP
        assert PipelineEvent.SCANNING_REVERSE.job_state is JobState.SCANNING_REVERSE
        assert PipelineEvent.ASSEMBLING.job_state is JobState.ASSEMBLING
        assert PipelineEvent.UPLOADING.job_state is JobState.UPLOADING
        assert PipelineEvent.DONE.job_state is JobState.DONE

    def test_scanning_reverse_projects_to_its_own_job_state(self) -> None:
        """
        Pass B persists SCANNING_REVERSE, so the job leaves AWAITING_FLIP (DPLX-06).

        While this projected to None the job stayed AWAITING_FLIP for the whole
        of pass B, leaving the flip prompt and its dead Abort on screen as pages fed.
        """
        assert PipelineEvent.SCANNING_REVERSE.job_state is JobState.SCANNING_REVERSE


class TestScanResultContract:
    """run_pipeline's typed ScanResult return (CTR-03)."""

    def test_success_outcome_and_page_counts(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Three pages scanned, one blank dropped, two uploaded."""
        default_settings.output.tmp_dir = str(tmp_path)

        all_pages = [
            _make_content_image("black"),
            _make_empty_image(),
            _make_content_image("red"),
        ]
        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling(all_pages)

        request = PipelineRequest(profile_name="default", title="Counts Test")

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            result = run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert isinstance(result, ScanResult)
        assert result.outcome is ScanOutcome.SUCCESS
        assert result.pages_scanned == 3
        assert result.pages_removed == 1
        assert result.pages_uploaded == 2
        assert result.warning is None

    def test_pages_removed_zero_when_detection_disabled(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """With empty-page detection off nothing is removed, blanks included."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].enable_empty_page_detection = False

        all_pages = [_make_content_image(), _make_empty_image(), _make_empty_image()]
        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling(all_pages)

        request = PipelineRequest(profile_name="default", title="No Filter Test")

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            result = run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert result.pages_scanned == 3
        assert result.pages_removed == 0
        assert result.pages_uploaded == 3

    def test_fallback_outcome_when_not_delivered_to_api(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """An upload that only reached the consume dir reports FALLBACK."""
        default_settings.output.tmp_dir = str(tmp_path)
        mock_paperless.upload_document.return_value = UploadResult(
            delivered_to_api=False,
            consume_dir_path=tmp_path / "consume" / "doc.pdf",
        )

        request = PipelineRequest(profile_name="default", title="Fallback Doc")
        result = run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert result.outcome is ScanOutcome.FALLBACK
        # OUTC-02: the FALLBACK state says the document took the other route;
        # the warning says what that route did not do.  A state on its own
        # leaves the user to guess why their title and tags never appeared.
        assert result.warning is not None
        assert "consume directory" in result.warning
        assert str(tmp_path / "consume" / "doc.pdf") in result.warning
        assert "title, tags and correspondent" in result.warning
        mock_paperless.poll_task.assert_not_called()


def _isolate_dirs(settings: Settings, tmp_path: Path) -> Path:
    """
    Put ``tmp_dir`` and ``data_dir`` on separate subtrees, return the failed dir.

    They must not share a root. The temp-cleanup assertions walk ``tmp_dir``
    demanding that no directory survives a run, while ``failed/`` is a
    directory that is *meant* to survive -- so a ``data_dir`` nested under
    ``tmp_dir`` would make a correct preservation look like a leaked temporary
    directory. Separate subtrees also mirror the real deployment, where scratch
    space and durable state are different volumes.

    Args:
        settings: The settings object to repoint, mutated in place.
        tmp_path: pytest's per-test temporary directory.

    Returns:
        The ``failed/`` directory the preservation guard will move into. It
        does not exist yet; the guard is responsible for creating it.

    """
    settings.output.tmp_dir = str(tmp_path / "scratch")
    settings.output.data_dir = str(tmp_path / "state")
    return settings.output.failed_dir


def _one_page_scanner() -> MagicMock:
    """Return a scanner backend spooling a single page with content."""
    scanner = MagicMock(spec=ScannerBackend)
    scanner.scan_pages.side_effect = spooling([_make_content_image()])
    return scanner


def _delivering_to_api(task_uuid: str = "task-uuid-1") -> MagicMock:
    """Return a paperless client whose upload reaches the API and polls clean."""
    paperless = MagicMock()
    paperless.upload_document.return_value = UploadResult(
        delivered_to_api=True, task_uuid=task_uuid
    )
    paperless.poll_task.return_value = None
    return paperless


class TestPreservation:
    """The preservation guard: a failed delivery must never destroy the scan."""

    def test_preserves_the_pdf_when_upload_raises(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """An exhausted upload with no consume dir leaves the PDF in failed/."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = MagicMock()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Upload Raises", job_id="job-a"
                ),
            )

        preserved = list(failed_dir.glob("*.pdf"))
        assert len(preserved) == 1
        assert preserved[0].stat().st_size > 0

    def test_preserves_the_pdf_when_poll_reports_a_paperless_failure(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        A Paperless FAILURE after a successful upload still preserves the PDF.

        This is the case the whole phase is named after: a guard wrapped around
        only the upload would let this raise unwind the temporary directory and
        delete the finished scan.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = _delivering_to_api()
        paperless.poll_task.side_effect = PaperlessError("Paperless reported FAILURE")

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Poll Fails", job_id="job-b"
                ),
            )

        paperless.poll_task.assert_called_once()
        assert len(list(failed_dir.glob("*.pdf"))) == 1

    def test_preserves_the_pdf_when_poll_times_out(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """D-10: a timeout is ambiguous, so the local copy is kept too."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = _delivering_to_api()
        paperless.poll_task.side_effect = PaperlessTimeoutError(
            "Timed out after 300s waiting for task task-uuid-1"
        )

        with pytest.raises(PaperlessTimeoutError) as excinfo:
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Poll Times Out", job_id="job-c"
                ),
            )

        assert len(list(failed_dir.glob("*.pdf"))) == 1
        # The subclass survives the re-raise, so a caller that narrows to a
        # timeout deliberately (D-11) still can.
        assert type(excinfo.value) is PaperlessTimeoutError

    def test_preserved_destination_is_named_in_the_raised_message(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """OUTC-04: the user must be told where the scan went."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = MagicMock()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        with pytest.raises(PaperlessError) as excinfo:
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Named Path", job_id="job-d"
                ),
            )

        preserved = next(iter(failed_dir.glob("*.pdf")))
        message = str(excinfo.value)
        assert str(preserved) in message
        assert "Upload failed" in message

    def test_preserving_chains_the_original_exception(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """The original type and traceback survive on __cause__."""
        _isolate_dirs(default_settings, tmp_path)
        original = PaperlessError("Upload failed")
        paperless = MagicMock()
        paperless.upload_document.side_effect = original

        with pytest.raises(PaperlessError) as excinfo:
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Chained", job_id="job-e"
                ),
            )

        assert excinfo.value.__cause__ is original

    def test_preserved_paperless_failure_still_classifies_as_upload(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """The escaping exception needs no new classify_error arm."""
        _isolate_dirs(default_settings, tmp_path)
        paperless = _delivering_to_api()
        paperless.poll_task.side_effect = PaperlessError("Paperless reported FAILURE")

        with pytest.raises(PaperlessError) as excinfo:
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Classified", job_id="job-f"
                ),
            )

        assert classify_error(excinfo.value) is ErrorCategory.UPLOAD

    def test_preserving_maps_a_foreign_exception_to_a_paperless_error(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        A non-saneless exception in the delivery window is a delivery failure.

        The guard spans nothing but the upload and the poll, so from the job's
        point of view an arbitrary exception there is a delivery failure -- and
        rebuilding an arbitrary third-party exception from a single string is
        not safe, so the re-raise uses PaperlessError instead.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = MagicMock()
        paperless.upload_document.side_effect = RuntimeError("something odd")

        with pytest.raises(PaperlessError) as excinfo:
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Foreign", job_id="job-g"
                ),
            )

        assert len(list(failed_dir.glob("*.pdf"))) == 1
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert "something odd" in str(excinfo.value)

    def test_preservation_creates_a_missing_failed_dir(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """data_dir may have vanished since startup validation."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        assert not failed_dir.exists()

        paperless = MagicMock()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Makes Dir", job_id="job-h"
                ),
            )

        assert failed_dir.is_dir()

    def test_a_failed_preservation_reports_both_failures(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        Being told only "upload failed" while the scan was destroyed is a lie.

        ``failed_dir`` is made a regular file, so the guard's own mkdir raises
        and the move can never run.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        failed_dir.parent.mkdir(parents=True, exist_ok=True)
        failed_dir.write_text("a regular file where the directory should be")

        original = PaperlessError("Upload failed")
        paperless = MagicMock()
        paperless.upload_document.side_effect = original

        with pytest.raises(PaperlessError) as excinfo:
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Both Fail", job_id="job-i"
                ),
            )

        message = str(excinfo.value)
        assert "Upload failed" in message
        assert str(failed_dir) in message
        assert "NOT" in message
        assert excinfo.value.__cause__ is original

    def test_nothing_is_preserved_when_delivery_succeeds(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A clean run must not create failed/ at all."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)

        result = run_pipeline(
            scanner=_one_page_scanner(),
            paperless=_delivering_to_api(),
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default", title="Clean Run", job_id="job-j"
            ),
        )

        assert result.outcome is ScanOutcome.SUCCESS
        assert not failed_dir.exists()

    def test_two_preserved_scans_sharing_a_title_are_two_files(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        Two jobs with the same title must not overwrite each other in failed/.

        ``shutil.move`` onto an explicit destination path overwrites silently,
        so this only holds because the job id is part of the file name.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = MagicMock()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        for job_id in ("job-first", "job-second"):
            with pytest.raises(PaperlessError):
                run_pipeline(
                    scanner=_one_page_scanner(),
                    paperless=paperless,
                    settings=default_settings,
                    request=PipelineRequest(
                        profile_name="default",
                        title="Same Title Twice",
                        job_id=job_id,
                    ),
                )

        preserved = sorted(path.name for path in failed_dir.glob("*.pdf"))
        assert len(preserved) == 2
        assert preserved[0] != preserved[1]

    def test_only_the_pdf_is_preserved_no_page_images(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        Only the PDF is preserved: the spooled pages die with the workspace.

        The spool lives inside the job's ``TemporaryDirectory``, and the
        preservation guard moves the assembled PDF and nothing else, so
        ``failed/`` holds exactly one kind of file.  Plan 29-09 is what adds a
        page directory beside it; until then this is the whole contract.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = MagicMock()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Pdf Only", job_id="job-k"
                ),
            )

        assert sorted(path.suffix for path in failed_dir.iterdir()) == [".pdf"]

    def test_preservation_leaves_no_leftover_temp_directories(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """The TemporaryDirectory still unwinds; only the PDF escaped it."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = MagicMock()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="No Leftovers", job_id="job-l"
                ),
            )

        scratch = tmp_path / "scratch"
        assert [item for item in scratch.iterdir() if item.is_dir()] == []
        assert len(list(failed_dir.glob("*.pdf"))) == 1


def _spooling_then_failing(
    pages: Sequence[Image.Image],
    failure: Exception,
) -> Callable[[str, ScanSettings, PageSink], ScanBatch]:
    """
    Build a ``scan_pages`` stand-in that spools some pages and then fails.

    This is the mid-batch failure HARD-02 is about: a jam on sheet N+1 of a
    stack whose first N sheets already went through the feeder.  The pages go
    into the pipeline's own sink, so the records the preservation path finds --
    and the files behind them -- are the ones production would have.

    Args:
        pages: The sheets that made it through, in acquisition order.
        failure: What the device raises once they have.

    Returns:
        A callable with ``scan_pages``' own shape, for ``MagicMock.side_effect``.

    """

    def _spool_then_fail(
        device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """Spool every page the device managed, then raise as it would."""
        for page in pages:
            sink.add(page)
        raise failure

    return _spool_then_fail


def _jamming_scanner(pages: int, failure: Exception) -> MagicMock:
    """
    Return a scanner that spools ``pages`` distinct sheets and then raises.

    Args:
        pages: How many sheets reach the spool before the fault.
        failure: The exception the device raises on the next sheet.

    Returns:
        A ScannerBackend mock ready for ``run_pipeline``.

    """
    scanner = MagicMock(spec=ScannerBackend)
    scanner.scan_pages.side_effect = _spooling_then_failing(
        [_distinct_page(index) for index in range(pages)], failure
    )
    return scanner


class TestPartialScanPreservation:
    """
    HARD-02 / D-09: a mid-batch failure keeps the pages already fed.

    A jam on page 40 of a 50-sheet stack used to discard the 39 sheets the
    operator had already put through the feeder.  The spool holds them, so the
    guard assembles them unfiltered into a ``(partial)`` PDF under ``failed/``
    and names the count and the path in the exception it re-raises -- without
    changing that exception's type, which is what keeps the job's error
    category and the CLI's exit code correct (Phase 28 D-07).
    """

    def test_partial_scan_preserved_when_the_scanner_fails_mid_batch(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        Three sheets fed, a jam on the fourth: the three are kept.

        The page count is read back out of the preserved PDF rather than
        inferred from its name, because the name is under test here too.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = MagicMock()

        with pytest.raises(ScanError) as excinfo:
            run_pipeline(
                scanner=_jamming_scanner(
                    3,
                    ScanError("Scanner error on page 4: Document feeder jammed"),
                ),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Jammed Stack", job_id="job-part-1"
                ),
            )

        preserved = list(failed_dir.glob("*.pdf"))
        assert len(preserved) == 1
        # ``partial``, not ``(partial)``: build_pdf_filename runs the title
        # through sanitise_title_for_filename, whose allow-list drops the
        # brackets.  The same is already true of the duplex-mismatch halves,
        # whose own test asserts on ``fronts`` for exactly this reason.
        assert "partial" in preserved[0].name
        with pikepdf.open(preserved[0]) as pdf:
            assert len(pdf.pages) == 3
        message = str(excinfo.value)
        assert "Document feeder jammed" in message
        assert "3 page(s)" in message
        assert str(preserved[0]) in message

    def test_partial_scan_keeps_the_original_exception_type(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        A ScanError stays a ScanError, so exit 1 and the category hold.

        ``type(...) is``, not ``isinstance``: a silent widening to
        ``SanelessError`` would satisfy an isinstance check while changing the
        exit code the CLI chooses (Phase 28's table).
        """
        _isolate_dirs(default_settings, tmp_path)
        original = ScanError("Scanner error on page 3: Paper jam")

        with pytest.raises(ScanError) as excinfo:
            run_pipeline(
                scanner=_jamming_scanner(2, original),
                paperless=MagicMock(),
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Type Survives", job_id="job-part-2"
                ),
            )

        assert type(excinfo.value) is ScanError
        assert excinfo.value.__cause__ is original
        assert classify_error(excinfo.value) is ErrorCategory.SCANNER

    def test_partial_scan_is_not_blank_filtered(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        An anomaly is delivered whole for review, following the mismatch path.

        Every spooled sheet here is blank enough that ``_drop_empty_pages``
        would have removed it -- and removing them all would raise "All pages
        were blank" and destroy the very evidence the operator needs.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        assert default_settings.profiles["default"].enable_empty_page_detection

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = _spooling_then_failing(
            [_make_empty_image() for _ in range(4)],
            ScanError("Scanner error on page 5: Document feeder jammed"),
        )

        with pytest.raises(ScanError):
            run_pipeline(
                scanner=scanner,
                paperless=MagicMock(),
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="All Faint", job_id="job-part-3"
                ),
            )

        preserved = list(failed_dir.glob("*.pdf"))
        assert len(preserved) == 1
        with pikepdf.open(preserved[0]) as pdf:
            assert len(pdf.pages) == 4

    def test_a_partial_scan_is_never_uploaded(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """N-02's rejected alternative: no incomplete document reaches paperless."""
        _isolate_dirs(default_settings, tmp_path)
        paperless = MagicMock()

        with pytest.raises(ScanError):
            run_pipeline(
                scanner=_jamming_scanner(3, ScanError("Feeder jammed")),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Never Sent", job_id="job-part-4"
                ),
            )

        paperless.upload_document.assert_not_called()
        paperless.poll_task.assert_not_called()

    def test_cancel_preserves_nothing(
        self,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        D-10: the operator chose to stop, so nothing is kept.

        ``ScanCancelledError`` is an ordinary ``Exception``, so a guard that
        caught only broadly would file a cancelled scan into a directory
        saneless never prunes.  The message must come through untouched, with
        no preserved-at text appended and no ERROR logged.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        original = ScanCancelledError("Scan cancelled by the operator")

        with (
            caplog.at_level(logging.DEBUG, logger="saneless.pipeline"),
            pytest.raises(
                ScanCancelledError, match=r"^Scan cancelled by the operator$"
            ) as excinfo,
        ):
            run_pipeline(
                scanner=_jamming_scanner(3, original),
                paperless=MagicMock(),
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Cancelled", job_id="job-part-5"
                ),
            )

        assert excinfo.value is original
        assert not failed_dir.exists()
        assert [
            record for record in caplog.records if record.levelno >= logging.ERROR
        ] == []

    def test_zero_pages_spooled_keeps_todays_message_and_preserves_nothing(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Nothing reached the spool, so the feeder message stands unchanged."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = FeederEmptyError("No paper detected in feeder")

        with pytest.raises(
            FeederEmptyError, match=r"^No paper detected in feeder$"
        ) as excinfo:
            run_pipeline(
                scanner=scanner,
                paperless=MagicMock(),
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Empty Feeder", job_id="job-part-6"
                ),
            )

        assert type(excinfo.value) is FeederEmptyError
        assert not failed_dir.exists()

    def test_an_empty_batch_still_says_no_pages_were_scanned(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """``_require_pages`` runs with an empty sink, so its message stands."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling([])

        with pytest.raises(ScanError, match=r"^No pages were scanned$"):
            run_pipeline(
                scanner=scanner,
                paperless=MagicMock(),
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="No Pages", job_id="job-part-7"
                ),
            )

        assert not failed_dir.exists()

    def test_a_failed_partial_preservation_reports_both_failures(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        Being told only "the feeder jammed" while the pages were destroyed is a lie.

        ``failed_dir`` is made a regular file, so the guard's own mkdir raises
        and the move can never run -- the shape ``_preserving`` already uses.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        failed_dir.parent.mkdir(parents=True, exist_ok=True)
        failed_dir.write_text("a regular file where the directory should be")
        original = ScanError("Scanner error on page 4: Document feeder jammed")

        with pytest.raises(ScanError) as excinfo:
            run_pipeline(
                scanner=_jamming_scanner(3, original),
                paperless=MagicMock(),
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Both Fail", job_id="job-part-8"
                ),
            )

        message = str(excinfo.value)
        assert "Document feeder jammed" in message
        assert str(failed_dir) in message
        assert "NOT" in message
        assert excinfo.value.__cause__ is original


def _failing_in_pass_b(
    fronts: int,
    backs: int,
    failure: Exception,
) -> Callable[[str, ScanSettings, PageSink], ScanBatch]:
    """
    Build a manual-duplex ``side_effect`` whose second pass fails part-way.

    Pass A completes normally; pass B spools ``backs`` sheets and then raises.
    ONE callable that counts its own calls, never a list of callables, for the
    reason ``spooling_in_turn`` records at length.

    Args:
        fronts: How many sheets pass A feeds.
        backs: How many sheets pass B gets through before the fault.
        failure: What the device raises on the next sheet of pass B.

    Returns:
        A callable with ``scan_pages``' own shape, for ``MagicMock.side_effect``.

    """
    calls = 0

    def _spool_next(
        device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """Run pass A to completion, then fail part-way through pass B."""
        nonlocal calls
        index = calls
        calls += 1
        if index == 0:
            pages = [_distinct_page(number) for number in range(fronts)]
            return spooling(pages)(device_id, settings, sink)
        for number in range(backs):
            sink.add(_distinct_page(fronts + number))
        raise failure

    return _spool_next


def _duplex_settings(settings: Settings, tmp_path: Path) -> Path:
    """
    Point the settings at isolated directories and make the profile duplex.

    Args:
        settings: The settings object to repoint, mutated in place.
        tmp_path: pytest's per-test temporary directory.

    Returns:
        The ``failed/`` directory the preservation guards move into.

    """
    failed_dir = _isolate_dirs(settings, tmp_path)
    settings.profiles["default"].source = "ADF"
    settings.profiles["default"].duplex = "manual"
    return failed_dir


def _preserved_page_counts(failed_dir: Path) -> dict[str, int]:
    """
    Read every preserved PDF back and report its page count by half.

    Args:
        failed_dir: The directory the partial PDFs were moved into.

    Returns:
        A mapping of ``"fronts"`` / ``"backs"`` / ``"partial"`` -- whichever
        marker the sanitised file name carries -- to that PDF's page count.

    """
    counts: dict[str, int] = {}
    for preserved in failed_dir.glob("*.pdf"):
        with pikepdf.open(preserved) as pdf:
            pages = len(pdf.pages)
        for marker in ("fronts", "backs", "partial"):
            if marker in preserved.name:
                counts[marker] = pages
    return counts


class TestPassBAndFlipFailuresKeepTheFronts:
    """
    D-10: every way pass A's fronts can be lost now keeps them.

    The gap this closes was written into ``_scan_manual_duplex`` itself -- "The
    fronts are still lost here; keeping them needs Phase 29's spooling" -- and
    the halves are named exactly as the duplex-mismatch recovery already names
    them, so an operator finds the same two artefacts either way.
    """

    def test_pass_b_preserves_fronts_when_the_scanner_fails(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A jam during pass B keeps the fronts and the backs fed so far."""
        failed_dir = _duplex_settings(default_settings, tmp_path)
        original = ScanError("Scanner error on page 2 of pass B: Paper jam")
        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = _failing_in_pass_b(3, 1, original)
        paperless = MagicMock()

        with pytest.raises(ScanError) as excinfo:
            run_pipeline(
                scanner=scanner,
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Pass B Jam",
                    job_id="job-passb-1",
                    flip_coordinator=AlwaysContinueFlipCoordinator(),
                ),
            )

        assert _preserved_page_counts(failed_dir) == {"fronts": 3, "backs": 1}
        assert type(excinfo.value) is ScanError
        assert excinfo.value.__cause__ is original
        assert "4 page(s)" in str(excinfo.value)
        paperless.upload_document.assert_not_called()

    def test_pass_b_preserves_fronts_when_it_is_empty(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        A pass B that fed nothing still keeps the fronts.

        The existing message is unchanged; the preservation text is appended to
        it, exactly as ``_preserving`` appends to a delivery failure's.
        """
        failed_dir = _duplex_settings(default_settings, tmp_path)
        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling_in_turn(
            [_distinct_page(index) for index in range(2)], []
        )

        with pytest.raises(ScanError) as excinfo:
            run_pipeline(
                scanner=scanner,
                paperless=MagicMock(),
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Empty Pass B Kept",
                    job_id="job-passb-2",
                    flip_coordinator=AlwaysContinueFlipCoordinator(),
                ),
            )

        assert _preserved_page_counts(failed_dir) == {"fronts": 2}
        message = str(excinfo.value)
        assert message.startswith(
            "No back pages were scanned in pass B (pass A scanned 2 front page(s))"
        )
        assert "2 page(s)" in message

    def test_pass_b_preserves_fronts_after_a_flip_wait_timeout(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Nobody chose to stop, so the fronts are kept (D-10, Phase 28 D-02)."""
        failed_dir = _duplex_settings(default_settings, tmp_path)
        default_settings.output.flip_timeout_seconds = 17
        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling(
            [_distinct_page(index) for index in range(3)]
        )

        with pytest.raises(ScanError) as excinfo:
            run_pipeline(
                scanner=scanner,
                paperless=MagicMock(),
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Forgotten Flip",
                    job_id="job-passb-3",
                    flip_coordinator=_FixedFlipCoordinator(FlipOutcome.TIMED_OUT),
                ),
            )

        assert _preserved_page_counts(failed_dir) == {"fronts": 3}
        message = str(excinfo.value)
        assert "flip wait timed out" in message
        assert "3 page(s)" in message
        assert type(excinfo.value) is ScanError
        assert scanner.scan_pages.call_count == 1

    def test_pass_b_preserves_fronts_after_a_broken_flip_prompt(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A prompt that broke is not anyone's decision to stop, so keep them."""
        failed_dir = _duplex_settings(default_settings, tmp_path)
        cause = OSError(5, "Input/output error")
        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling(
            [_distinct_page(index) for index in range(2)]
        )

        with pytest.raises(ScanError) as excinfo:
            run_pipeline(
                scanner=scanner,
                paperless=MagicMock(),
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Broken Prompt Kept",
                    job_id="job-passb-4",
                    flip_coordinator=_BrokenPromptFlipCoordinator(cause),
                ),
            )

        assert _preserved_page_counts(failed_dir) == {"fronts": 2}
        assert "Flip prompt failed" in str(excinfo.value)
        assert not isinstance(excinfo.value, ScanCancelledError)

    def test_a_flip_abort_preserves_nothing(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        The asymmetry is the policy: an abort is a decision, a timeout is not.

        A cancelled run stays a cancel -- exit 130, not shown red -- and leaves
        ``failed/`` untouched, because ``failed/`` is never pruned and nobody
        asked for those pages to be filed there.
        """
        failed_dir = _duplex_settings(default_settings, tmp_path)
        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling(
            [_distinct_page(index) for index in range(3)]
        )

        with pytest.raises(
            ScanCancelledError,
            match=r"^Manual duplex scan cancelled at the flip prompt$",
        ) as excinfo:
            run_pipeline(
                scanner=scanner,
                paperless=MagicMock(),
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Aborted",
                    job_id="job-passb-5",
                    flip_coordinator=_FixedFlipCoordinator(FlipOutcome.ABORTED),
                ),
            )

        assert not failed_dir.exists()
        # Still a cancel, not a scan failure: the worker reads the type to
        # decide between its cancelled and error endings, and the CLI reads it
        # for exit 130 rather than exit 1.
        assert type(excinfo.value) is ScanCancelledError
        assert not isinstance(excinfo.value, ScanError)

    def test_pass_a_failing_mid_batch_keeps_its_fronts_too(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        D-10 applies to each manual-duplex pass, not only to pass B.

        Nobody is asked to flip, so only one half exists and it is named as
        the fronts it is.
        """
        failed_dir = _duplex_settings(default_settings, tmp_path)
        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = _spooling_then_failing(
            [_distinct_page(index) for index in range(2)],
            ScanError("Scanner error on page 3: Paper jam"),
        )

        with pytest.raises(ScanError):
            run_pipeline(
                scanner=scanner,
                paperless=MagicMock(),
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Pass A Jam",
                    job_id="job-passb-6",
                    flip_coordinator=_FixedFlipCoordinator(FlipOutcome.CONTINUED),
                ),
            )

        assert _preserved_page_counts(failed_dir) == {"fronts": 2}


def _fill_failed_dir(failed_dir: Path, count: int) -> list[str]:
    """
    Pre-populate ``failed_dir`` with ``count`` preserved-looking PDFs.

    Args:
        failed_dir: The directory to fill; created if absent.
        count: How many files to write.

    Returns:
        The file names written, so a test can assert every one of them
        survived a later preservation.

    """
    failed_dir.mkdir(parents=True, exist_ok=True)
    names = [f"20260101-000000-old{index:02d}-doc.pdf" for index in range(count)]
    for name in names:
        (failed_dir / name).write_bytes(b"%PDF-old" * 64)
    return names


def _preserve_one_scan(settings: Settings, job_id: str) -> PaperlessError:
    """
    Drive one failing delivery through the pipeline and return what escaped.

    Args:
        settings: Settings already pointed at isolated directories.
        job_id: The job id, which is what makes the preserved name unique.

    Returns:
        The PaperlessError that escaped run_pipeline.

    """
    paperless = MagicMock()
    paperless.upload_document.side_effect = PaperlessError("Upload failed")
    with pytest.raises(PaperlessError) as excinfo:
        run_pipeline(
            scanner=_one_page_scanner(),
            paperless=paperless,
            settings=settings,
            request=PipelineRequest(
                profile_name="default", title="Growth Check", job_id=job_id
            ),
        )
    return excinfo.value


class TestFailedDirWarningFiresOncePerGuard:
    """
    One guard preserving two PDFs warns once, not once per file (WR-09).

    The duplex-mismatch recovery passes both halves under a single
    ``_preserving`` guard, because they are one document between them (D-08).
    Running the threshold check inside the per-file loop therefore emitted the
    same "N preserved scans have accumulated" WARNING twice, with different
    counts -- log noise on the one path already flagged as an anomaly, and a
    contradiction of the helper's own "one WARNING" docstring.
    """

    def _two_scans(self, tmp_path: Path) -> list[Path]:
        """Write two assembled-looking PDFs for one guard to preserve."""
        pdfs = [tmp_path / "fronts.pdf", tmp_path / "backs.pdf"]
        for pdf in pdfs:
            pdf.write_bytes(b"%PDF-new" * 64)
        return pdfs

    def _preserve_both(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> Path:
        """Fail one delivery holding two PDFs, and return the failed dir."""
        failed_dir = tmp_path / "failed"
        # One short of the threshold, so the per-file call crossed it on the
        # first move and again on the second -- two warnings for one failure.
        _fill_failed_dir(failed_dir, FAILED_DIR_WARN_THRESHOLD - 1)
        pdfs = self._two_scans(tmp_path)
        failure = PaperlessError("Upload failed")

        with (
            caplog.at_level(logging.WARNING, logger="saneless.pipeline"),
            pytest.raises(PaperlessError),
            _preserving(pdfs, failed_dir),
        ):
            raise failure

        return failed_dir

    def test_two_preserved_scans_emit_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One failure is one anomaly, however many files it preserved."""
        failed_dir = self._preserve_both(tmp_path, caplog)

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and str(failed_dir) in record.getMessage()
        ]
        assert len(warnings) == 1

    def test_the_single_warning_counts_both_preserved_scans(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The count reflects the finished state, not a mid-loop snapshot."""
        failed_dir = self._preserve_both(tmp_path, caplog)

        message = next(
            record.getMessage()
            for record in caplog.records
            if str(failed_dir) in record.getMessage()
        )
        assert str(FAILED_DIR_WARN_THRESHOLD + 1) in message


class TestFailedDirWarning:
    """Warn -- never prune -- when preserved scans accumulate (T-23-29)."""

    def test_failed_dir_warns_when_the_threshold_is_reached(
        self,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Preserving into a crowded directory logs one WARNING."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        _fill_failed_dir(failed_dir, FAILED_DIR_WARN_THRESHOLD - 1)

        with caplog.at_level(logging.WARNING, logger="saneless.pipeline"):
            _preserve_one_scan(default_settings, "job-growth-1")

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and str(failed_dir) in record.getMessage()
        ]
        assert len(warnings) == 1

    def test_failed_dir_warning_names_the_count_size_and_path(
        self,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The operator needs all three facts to act on the warning."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        _fill_failed_dir(failed_dir, FAILED_DIR_WARN_THRESHOLD - 1)

        with caplog.at_level(logging.WARNING, logger="saneless.pipeline"):
            _preserve_one_scan(default_settings, "job-growth-2")

        message = next(
            record.getMessage()
            for record in caplog.records
            if str(failed_dir) in record.getMessage()
        )
        assert str(FAILED_DIR_WARN_THRESHOLD) in message
        assert "MiB" in message
        assert str(failed_dir) in message

    def test_failed_dir_warning_is_silent_below_the_threshold(
        self,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One preserved scan is not a problem and must not be announced."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)

        with caplog.at_level(logging.WARNING, logger="saneless.pipeline"):
            _preserve_one_scan(default_settings, "job-growth-3")

        assert len(list(failed_dir.glob("*.pdf"))) == 1
        assert [
            record.getMessage()
            for record in caplog.records
            if "MiB" in record.getMessage()
        ] == []

    def test_failed_dir_warning_deletes_nothing(
        self,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        The warning is the whole control: no prune, no sweep, no rotation.

        Deleting a preserved scan would be exactly the data loss the guard
        exists to prevent, so every pre-existing file must still be there.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        existing = _fill_failed_dir(failed_dir, FAILED_DIR_WARN_THRESHOLD - 1)

        with caplog.at_level(logging.WARNING, logger="saneless.pipeline"):
            _preserve_one_scan(default_settings, "job-growth-4")

        survivors = {path.name for path in failed_dir.glob("*.pdf")}
        assert set(existing) <= survivors
        assert len(survivors) == FAILED_DIR_WARN_THRESHOLD

    def test_failed_dir_warning_failure_cannot_mask_the_delivery_error(
        self,
        default_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        An unreadable failed/ must not replace the real failure.

        The growth check runs inside the preservation guard's own exception
        handler, so a raise there would swap the delivery failure -- the
        message OUTC-04 requires the job to carry -- for a bookkeeping error.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        real_glob = Path.glob

        def exploding_glob(self: Path, pattern: str) -> Iterator[Path]:
            if self == failed_dir:
                msg = "failed/ became unreadable"
                raise OSError(msg)
            return real_glob(self, pattern)

        monkeypatch.setattr(Path, "glob", exploding_glob)

        escaped = _preserve_one_scan(default_settings, "job-growth-5")

        assert "Upload failed" in str(escaped)
        assert "preserved at" in str(escaped)
        # iterdir, not glob: the patch is still in force.
        assert len([path for path in failed_dir.iterdir() if path.is_file()]) == 1


def _mismatched_duplex_scanner(fronts: int = 3, backs: int = 2) -> MagicMock:
    """
    Return a scanner whose two manual-duplex passes disagree on page count.

    Args:
        fronts: Pages produced by pass A.
        backs: Pages produced by pass B.

    Returns:
        A ScannerBackend mock whose two scan_pages calls spool those pages into
        the sink the pipeline hands them.

    """
    scanner = MagicMock(spec=ScannerBackend)
    scanner.scan_pages.side_effect = spooling_in_turn(
        [_make_content_image() for _ in range(fronts)],
        [_make_content_image() for _ in range(backs)],
    )
    return scanner


def _both_halves_delivered() -> MagicMock:
    """Return a paperless client that accepts both partial uploads."""
    paperless = MagicMock()
    paperless.upload_document.side_effect = [
        UploadResult(delivered_to_api=True, task_uuid="fronts-task"),
        UploadResult(delivered_to_api=True, task_uuid="backs-task"),
    ]
    paperless.poll_task.return_value = None
    return paperless


class TestDuplexMismatchDelivery:
    """D-08: the duplex-mismatch path gets the same honesty as the simplex one."""

    def test_duplex_mismatch_polls_both_halves(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Leaving either task unpolled is C-03 surviving in a corner."""
        _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        paperless = _both_halves_delivered()

        result = run_pipeline(
            scanner=_mismatched_duplex_scanner(),
            paperless=paperless,
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default",
                title="Polled Twice",
                job_id="job-dx-1",
                flip_coordinator=AlwaysContinueFlipCoordinator(),
            ),
        )

        polled = [call.args[0] for call in paperless.poll_task.call_args_list]
        assert polled == ["fronts-task", "backs-task"]
        assert result.outcome is ScanOutcome.SUCCESS

    def test_duplex_mismatch_uses_the_configured_task_timeout(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Both halves honour output.paperless_task_timeout."""
        _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        default_settings.output.paperless_task_timeout = 17
        paperless = _both_halves_delivered()

        run_pipeline(
            scanner=_mismatched_duplex_scanner(),
            paperless=paperless,
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default",
                title="Timeout Wired",
                job_id="job-dx-2",
                flip_coordinator=AlwaysContinueFlipCoordinator(),
            ),
        )

        timeouts = [
            call.kwargs["timeout"] for call in paperless.poll_task.call_args_list
        ]
        assert timeouts == [17, 17]

    def test_duplex_mismatch_preserves_both_halves_when_the_fronts_fail(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        A failure on one half must not cost the user the other half.

        The two partial PDFs are one document between them, so both go into
        failed/ even though only the fronts task reported FAILURE.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        paperless = _both_halves_delivered()
        paperless.poll_task.side_effect = PaperlessError("Paperless reported FAILURE")

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_mismatched_duplex_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Fronts Fail",
                    job_id="job-dx-3",
                    flip_coordinator=AlwaysContinueFlipCoordinator(),
                ),
            )

        assert len(list(failed_dir.glob("*.pdf"))) == 2

    def test_duplex_mismatch_preserves_both_halves_when_the_backs_fail(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A clean fronts poll followed by a failing backs poll still raises."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        paperless = _both_halves_delivered()
        paperless.poll_task.side_effect = [
            None,
            PaperlessError("Paperless reported FAILURE"),
        ]

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_mismatched_duplex_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Backs Fail",
                    job_id="job-dx-4",
                    flip_coordinator=AlwaysContinueFlipCoordinator(),
                ),
            )

        assert paperless.poll_task.call_count == 2
        assert len(list(failed_dir.glob("*.pdf"))) == 2

    def test_duplex_mismatch_preserved_halves_have_distinct_names(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Two same-named halves would destroy the one this path exists to save."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        paperless = _both_halves_delivered()
        paperless.poll_task.side_effect = PaperlessError("Paperless reported FAILURE")

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_mismatched_duplex_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Distinct Halves",
                    job_id="job-dx-5",
                    flip_coordinator=AlwaysContinueFlipCoordinator(),
                ),
            )

        names = sorted(path.name for path in failed_dir.glob("*.pdf"))
        assert len(names) == 2
        assert any("fronts" in name for name in names)
        assert any("backs" in name for name in names)

    def test_duplex_mismatch_preservation_failure_is_reported_too(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """An unusable failed/ must not silently swallow the duplex failure."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        failed_dir.parent.mkdir(parents=True, exist_ok=True)
        failed_dir.write_text("a regular file where the directory should be")
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        paperless = _both_halves_delivered()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        with pytest.raises(PaperlessError) as excinfo:
            run_pipeline(
                scanner=_mismatched_duplex_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Duplex Both Fail",
                    job_id="job-dx-6",
                    flip_coordinator=AlwaysContinueFlipCoordinator(),
                ),
            )

        message = str(excinfo.value)
        assert "Upload failed" in message
        assert str(failed_dir) in message

    def test_duplex_mismatch_result_carries_outcome_warning_and_counts(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """OUTC-03: the worker needs an outcome, a warning and three counts."""
        _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        result = run_pipeline(
            scanner=_mismatched_duplex_scanner(fronts=4, backs=3),
            paperless=_both_halves_delivered(),
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default",
                title="Counts And Warning",
                job_id="job-dx-7",
                flip_coordinator=AlwaysContinueFlipCoordinator(),
            ),
        )

        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning is not None
        assert "Page count mismatch: 4 fronts, 3 backs" in result.warning
        assert result.pages_scanned == 7
        assert result.pages_removed == 0
        assert result.pages_uploaded == 7

    def test_duplex_mismatch_is_fallback_not_success_when_a_half_falls_back(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A consume-dir half is not a success, and carries no task to poll."""
        _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        paperless = MagicMock()
        paperless.upload_document.side_effect = [
            UploadResult(delivered_to_api=True, task_uuid="fronts-task"),
            UploadResult(
                delivered_to_api=False,
                consume_dir_path=tmp_path / "consume" / "backs.pdf",
            ),
        ]
        paperless.poll_task.return_value = None

        result = run_pipeline(
            scanner=_mismatched_duplex_scanner(),
            paperless=paperless,
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default",
                title="Half Fallback",
                job_id="job-dx-8",
                flip_coordinator=AlwaysContinueFlipCoordinator(),
            ),
        )

        assert result.outcome is ScanOutcome.FALLBACK
        assert paperless.poll_task.call_count == 1


class TestTheDpiTheDeviceActuallyChose:
    """
    The PDF declares the resolution the scanner used, not the one asked for.

    Phase 23 made the profile's requested resolution authoritative for
    ``img2pdf.get_fixed_dpi_layout_fun``. SANE substitutes silently -- measured,
    5000 comes back as 1200 -- so a device that substitutes produced both a
    mis-cropped page and a MediaBox disagreeing with its own content, which
    re-opened part of OUTC-06 (T-24-22).
    """

    def test_the_pdf_is_assembled_at_the_resolution_the_device_chose(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A profile asking 600 on a device that gives 300 assembles at 300."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].resolution = 600

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling(
            [_make_content_image()], resolution=300
        )

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=PipelineRequest(profile_name="default", title="Clamped"),
            )

        assert mock_assemble.call_args.kwargs["dpi"] == 300

    def test_the_duplex_mismatch_recovery_also_uses_the_actual_dpi(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """The recovery path builds its two partial PDFs at the device's dpi."""
        _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        default_settings.profiles["default"].resolution = 600

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling_in_turn(
            [_make_content_image() for _ in range(3)],
            [_make_content_image() for _ in range(2)],
            resolution=150,
        )

        fronts_pdf = tmp_path / "fronts.pdf"
        backs_pdf = tmp_path / "backs.pdf"
        fronts_pdf.write_bytes(b"%PDF-fake")
        backs_pdf.write_bytes(b"%PDF-fake")

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.side_effect = [fronts_pdf, backs_pdf]

            run_pipeline(
                scanner=scanner,
                paperless=_both_halves_delivered(),
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Mismatch Dpi",
                    job_id="job-dpi-1",
                    flip_coordinator=AlwaysContinueFlipCoordinator(),
                ),
            )

        dpis = [call.kwargs["dpi"] for call in mock_assemble.call_args_list]
        assert dpis == [150, 150]

    def test_two_passes_disagreeing_on_resolution_say_so(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        Manual duplex must not silently pick one of two different resolutions.

        The two passes use identical settings on one device, so a disagreement
        means the device changed its mind mid-job. Pass A's value is used and
        the difference is logged rather than swallowed.
        """
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = _spooling_at_each_resolution(
            ([_make_content_image() for _ in range(2)], 300),
            ([_make_content_image() for _ in range(2)], 150),
        )

        with (
            patch("saneless.pipeline.assemble_pdf") as mock_assemble,
            caplog.at_level(logging.WARNING, logger="saneless.pipeline"),
        ):
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Two Dpis",
                    flip_coordinator=AlwaysContinueFlipCoordinator(),
                ),
            )

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert [m for m in messages if "300" in m and "150" in m]
        assert mock_assemble.call_args.kwargs["dpi"] == 300


class TestRejectedPagesAreNotBlankPages:
    """
    D-07: a sheet the scanner could not read is counted, and counted apart.

    ``pages_scanned`` is ``len(records)``, which already excludes a skipped
    sheet, so a ten-sheet stack with one unreadable page reported nine and
    nobody learned a page was lost (T-24-23). The count must not be folded into
    the blank-page total, which Phase 30 renders as pages removed for being
    blank (T-24-24).
    """

    def test_rejected_pages_are_reported_without_touching_the_blank_count(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Two integrity rejections and no blank pages: 0 removed, 2 reported."""
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling(
            [_make_content_image() for _ in range(3)], rejected=2
        )

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            result = run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=PipelineRequest(profile_name="default", title="Two Rejected"),
            )

        assert result.pages_removed == 0
        assert result.warning is not None
        assert "2" in result.warning

    def test_a_clean_scan_reports_zero_for_both_counts(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """No rejections and no blank removals leaves nothing to warn about."""
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = spooling(
            [_make_content_image() for _ in range(3)]
        )

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            result = run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=PipelineRequest(profile_name="default", title="All Clean"),
            )

        assert result.pages_removed == 0
        assert result.warning is None
