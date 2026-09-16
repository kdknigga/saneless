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

import re
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
from saneless.pipeline import _SPOOL_LABEL_A
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

    def test_convert_returning_none_becomes_pdf_error(
        self,
        one_page: list[PageRecord],
        output_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The defensive None guard raises PdfError, not RuntimeError."""

        def fake_convert(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(pdf_mod.img2pdf, "convert", fake_convert)

        with pytest.raises(PdfError, match=r"img2pdf\.convert returned None"):
            assemble_pdf(one_page, output_dir, "x.pdf", dpi=300)

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
