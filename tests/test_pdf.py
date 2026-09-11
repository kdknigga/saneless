"""Tests for PDF assembly module."""

import re
from pathlib import Path

import pytest
from PIL import Image

import saneless.pdf as pdf_mod
from saneless.pdf import (
    assemble_pdf,
    build_pdf_filename,
    sanitise_title_for_filename,
)

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


class TestAssemblePdf:
    """PDF assembly tests."""

    def test_assemble_single_page(self, tmp_path: Path) -> None:
        """Single image produces a valid PDF file."""
        img = Image.new("RGB", (100, 100), "white")
        pdf_path = assemble_pdf([img], tmp_path)

        assert pdf_path.exists()
        assert pdf_path.stat().st_size > 0
        content = pdf_path.read_bytes()
        assert content[:5] == b"%PDF-"

    def test_assemble_multiple_pages(self, tmp_path: Path) -> None:
        """Multiple images produce a larger PDF than a single image."""
        images = [
            Image.new("RGB", (100, 100), "white"),
            Image.new("RGB", (200, 200), "red"),
            Image.new("RGB", (150, 150), "blue"),
        ]
        single_pdf = assemble_pdf([images[0]], tmp_path / "single")
        (tmp_path / "single").mkdir(exist_ok=True)
        single_pdf = assemble_pdf([images[0]], tmp_path / "single")
        single_size = single_pdf.stat().st_size

        multi_dir = tmp_path / "multi"
        multi_dir.mkdir()
        multi_pdf = assemble_pdf(images, multi_dir)
        multi_size = multi_pdf.stat().st_size

        assert multi_size > single_size

    def test_temp_files_cleaned_on_success(self, tmp_path: Path) -> None:
        """Temporary PNG files are removed after successful assembly."""
        img = Image.new("RGB", (100, 100), "white")
        assemble_pdf([img], tmp_path)

        # After assembly, no .png temp files should remain
        # (they were in a TemporaryDirectory that was cleaned up)
        png_files = list(tmp_path.rglob("*.png"))
        assert len(png_files) == 0

    def test_temp_files_cleaned_on_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Temporary PNG files are removed even when img2pdf raises."""

        # Make img2pdf.convert raise an error
        def fake_convert(*_args: object, **_kwargs: object) -> None:
            msg = "fake img2pdf error"
            raise RuntimeError(msg)

        monkeypatch.setattr(
            pdf_mod,
            "img2pdf",
            type("FakeImg2Pdf", (), {"convert": staticmethod(fake_convert)})(),
        )

        img = Image.new("RGB", (100, 100), "white")
        with pytest.raises(RuntimeError, match="fake img2pdf error"):
            pdf_mod.assemble_pdf([img], tmp_path)

        # Temp dir should still be cleaned up
        png_files = list(tmp_path.rglob("*.png"))
        assert len(png_files) == 0

    def test_output_path(self, tmp_path: Path) -> None:
        """Output PDF is written to the specified directory with .pdf extension."""
        img = Image.new("RGB", (100, 100), "white")
        pdf_path = assemble_pdf([img], tmp_path)

        assert isinstance(pdf_path, Path)
        assert pdf_path.parent == tmp_path
        assert pdf_path.suffix == ".pdf"


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
