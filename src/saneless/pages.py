"""
Page processing utilities: empty page detection and thumbnail generation.

Provides inline pipeline filters for detecting and discarding empty/blank
pages using a dual-threshold algorithm, and generating base64-encoded JPEG
thumbnails of scanned pages.
"""

from __future__ import annotations

import base64
import io
import logging

import PIL.Image
from PIL import Image
from PIL.Image import Resampling
from PIL.ImageStat import Stat

# Allow high-DPI scans (same as sane_backend.py and pdf.py).
PIL.Image.MAX_IMAGE_PIXELS = 200_000_000

__all__ = ["filter_empty_pages", "generate_thumbnail", "is_empty_page"]

logger = logging.getLogger(__name__)


def is_empty_page(
    image: Image.Image,
    mean_threshold: float = 250.0,
    stddev_threshold: float = 5.0,
) -> bool:
    """
    Detect whether a scanned page is empty (blank).

    Converts the image to grayscale and checks if the mean luminance
    exceeds ``mean_threshold`` AND the standard deviation is below
    ``stddev_threshold``. Both conditions must be true for the page
    to be considered empty.

    Args:
        image: PIL Image of the scanned page.
        mean_threshold: Pages with mean luminance above this are
            candidates for empty detection. Default 250.0.
        stddev_threshold: Pages with stddev below this (combined
            with high mean) are considered empty. Default 5.0.

    Returns:
        True if the page is considered empty, False otherwise.

    """
    gray = image.convert("L")
    stats = Stat(gray)
    mean_val = stats.mean[0]
    stddev_val = stats.stddev[0]
    is_blank = mean_val > mean_threshold and stddev_val < stddev_threshold
    logger.debug(
        "Page stats: mean=%.1f, stddev=%.1f -> %s",
        mean_val,
        stddev_val,
        "DISCARD" if is_blank else "KEEP",
    )
    return is_blank


def filter_empty_pages(
    pages: list[Image.Image],
    mean_threshold: float = 250.0,
    stddev_threshold: float = 5.0,
) -> list[Image.Image]:
    """
    Remove empty pages from a list using the dual-threshold algorithm.

    Args:
        pages: List of PIL Image objects to filter.
        mean_threshold: Mean luminance threshold for empty detection.
        stddev_threshold: Stddev threshold for empty detection.

    Returns:
        List of non-empty pages (may be empty if all pages are blank).

    """
    kept: list[Image.Image] = []
    for i, page in enumerate(pages):
        if is_empty_page(page, mean_threshold, stddev_threshold):
            logger.info("Page %d: DISCARD (empty)", i + 1)
        else:
            logger.info("Page %d: KEEP", i + 1)
            kept.append(page)
    return kept


def generate_thumbnail(
    image: Image.Image,
    max_edge: int = 300,
    quality: int = 85,
) -> str:
    """
    Generate a base64-encoded JPEG thumbnail of a scanned page.

    Creates a copy of the image, resizes so the longest edge is at
    most ``max_edge`` pixels (preserving aspect ratio), then encodes
    as JPEG and returns the base64 string.

    Args:
        image: PIL Image of the scanned page.
        max_edge: Maximum pixel length of the longest edge. Default 300.
        quality: JPEG quality (1-100). Default 85.

    Returns:
        Base64-encoded JPEG string (ASCII).

    """
    thumb = image.copy()
    # Strip EXIF to avoid img2pdf/viewer orientation issues (Pitfall #5)
    thumb.info.pop("exif", None)
    thumb.thumbnail((max_edge, max_edge), Resampling.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=quality)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    logger.debug(
        "Generated thumbnail: %dx%d, %d bytes base64",
        thumb.width,
        thumb.height,
        len(encoded),
    )
    return encoded
