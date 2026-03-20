"""
PDF assembly via img2pdf with temporary file handling.

Scanned PIL Images are saved to temporary files on disk (seekable,
required by img2pdf) and assembled into a single PDF. Temporary
image files are cleaned up after assembly or on error via
TemporaryDirectory context manager.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import img2pdf
import PIL.Image
from PIL import Image

# Allow high-DPI scans without triggering Pillow's decompression bomb check.
# 600 DPI A4 color = ~34.8M pixels; 1200 DPI = ~139M pixels.
PIL.Image.MAX_IMAGE_PIXELS = 200_000_000

__all__ = ["assemble_pdf"]

logger = logging.getLogger(__name__)


def assemble_pdf(images: list[Image.Image], output_dir: Path) -> Path:
    """
    Assemble PIL Images into a single PDF using img2pdf.

    Images are first saved as PNG files in a temporary directory
    within output_dir, then passed to img2pdf for lossless PDF
    assembly. Temp files are cleaned up automatically.

    Args:
        images: List of PIL Image objects to include in the PDF.
        output_dir: Directory where the output PDF will be written.

    Returns:
        Path to the generated PDF file.

    """
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=str(output_dir)) as tmp_dir:
        image_paths: list[str] = []
        for i, img in enumerate(images):
            img_path = Path(tmp_dir) / f"page_{i:04d}.png"
            img.save(str(img_path), format="PNG")
            image_paths.append(str(img_path))
            logger.debug("Saved page %d to %s", i, img_path)

        pdf_path = output_dir / "output.pdf"
        pdf_bytes = img2pdf.convert(image_paths)
        if pdf_bytes is None:
            msg = "img2pdf.convert returned None"
            raise RuntimeError(msg)
        pdf_path.write_bytes(pdf_bytes)
        logger.info("Assembled %d page(s) into %s", len(images), pdf_path)

    return pdf_path
