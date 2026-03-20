"""Tests for PDF assembly module."""

from pathlib import Path

import pytest
from PIL import Image


class TestAssemblePdf:
    """PDF assembly tests."""

    def test_assemble_single_page(self, tmp_path) -> None:
        """Single image produces a valid PDF file."""
        from saneless.pdf import assemble_pdf

        img = Image.new("RGB", (100, 100), "white")
        pdf_path = assemble_pdf([img], tmp_path)

        assert pdf_path.exists()
        assert pdf_path.stat().st_size > 0
        content = pdf_path.read_bytes()
        assert content[:5] == b"%PDF-"

    def test_assemble_multiple_pages(self, tmp_path) -> None:
        """Multiple images produce a larger PDF than a single image."""
        from saneless.pdf import assemble_pdf

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

    def test_temp_files_cleaned_on_success(self, tmp_path) -> None:
        """Temporary PNG files are removed after successful assembly."""
        from saneless.pdf import assemble_pdf

        img = Image.new("RGB", (100, 100), "white")
        assemble_pdf([img], tmp_path)

        # After assembly, no .png temp files should remain
        # (they were in a TemporaryDirectory that was cleaned up)
        png_files = list(tmp_path.rglob("*.png"))
        assert len(png_files) == 0

    def test_temp_files_cleaned_on_error(self, tmp_path, monkeypatch) -> None:
        """Temporary PNG files are removed even when img2pdf raises."""
        import saneless.pdf as pdf_mod

        # Make img2pdf.convert raise an error
        def fake_convert(*_args, **_kwargs):
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

    def test_output_path(self, tmp_path) -> None:
        """Output PDF is written to the specified directory with .pdf extension."""
        from saneless.pdf import assemble_pdf

        img = Image.new("RGB", (100, 100), "white")
        pdf_path = assemble_pdf([img], tmp_path)

        assert isinstance(pdf_path, Path)
        assert pdf_path.parent == tmp_path
        assert pdf_path.suffix == ".pdf"
