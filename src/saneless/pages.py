"""
Page processing utilities: empty page detection and thumbnail generation.

Provides the pipeline's blank-page filter, which judges a page from the
greyscale statistics its ``PageRecord`` carries rather than from the page
itself, and generates base64-encoded JPEG thumbnails of scanned pages.

Only the thumbnail half still takes an image. The filter half takes records,
because the measurements it needs were made once already, at spool time, while
the page was in memory (D-06) -- reading them off the record is what stops the
same greyscale conversion being paid a second time per page.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import TYPE_CHECKING

import PIL.Image
from PIL import Image, ImageOps
from PIL.Image import Resampling

if TYPE_CHECKING:
    from collections.abc import Sequence

    from saneless.scanner.base import PageRecord

# Allow high-DPI scans (same as sane_backend.py and pdf.py).
PIL.Image.MAX_IMAGE_PIXELS = 200_000_000

__all__ = ["filter_empty_pages", "generate_thumbnail", "is_empty_page"]

logger = logging.getLogger(__name__)


def is_empty_page(
    mean: float,
    stddev: float,
    mean_threshold: float = 250.0,
    stddev_threshold: float = 5.0,
) -> bool:
    """
    Detect whether a scanned page is empty (blank) from its statistics.

    Checks if the mean luminance exceeds ``mean_threshold`` AND the
    standard deviation is below ``stddev_threshold``. Both conditions must
    be true for the page to be considered empty. The two thresholds, the
    AND between them and the strictness of both comparisons are exactly as
    they were; only where the two numbers come from has changed.

    The page itself no longer arrives here, and that is D-06. This used to
    convert the image to greyscale and run Pillow's image-statistics helper
    over the result -- measured at 6 ms for the conversion and 3 ms for the
    statistics, plus a
    full-size intermediate, for every page of every job. The spool already
    does that work once, while the page is in memory at write time, and
    carries the answers on the record. Doing it again here was the second of
    two reads of the same pixels, and it is the one that goes.

    Args:
        mean: Greyscale mean luminance, as measured when the page was spooled.
        stddev: Greyscale standard deviation, measured at the same moment.
        mean_threshold: Pages with mean luminance above this are
            candidates for empty detection. Default 250.0.
        stddev_threshold: Pages with stddev below this (combined
            with high mean) are considered empty. Default 5.0.

    Returns:
        True if the page is considered empty, False otherwise.

    """
    is_blank = mean > mean_threshold and stddev < stddev_threshold
    logger.debug(
        "Page stats: mean=%.1f, stddev=%.1f -> %s",
        mean,
        stddev,
        "DISCARD" if is_blank else "KEEP",
    )
    return is_blank


def filter_empty_pages(
    pages: Sequence[PageRecord],
    mean_threshold: float = 250.0,
    stddev_threshold: float = 5.0,
) -> list[PageRecord]:
    """
    Remove empty pages from a record sequence, by the dual-threshold rule.

    Records in, records out. The pages are files on the spool by the time this
    runs, so what gets filtered is the list, never the directory: a discarded
    page is simply not referenced by the result, and nothing here unlinks it.
    The surviving records keep their relative order, and their ``sequence``
    values keep the gaps the discards left -- the numbers name the sheet the
    device fed, not the position in this list.

    Args:
        pages: The page records to filter, in document order.
        mean_threshold: Mean luminance threshold for empty detection.
        stddev_threshold: Stddev threshold for empty detection.

    Returns:
        List of non-empty records (may be empty if all pages are blank).

    """
    kept: list[PageRecord] = []
    for record in pages:
        if is_empty_page(record.mean, record.stddev, mean_threshold, stddev_threshold):
            logger.info("Page %d: DISCARD (empty)", record.sequence)
        else:
            logger.info("Page %d: KEEP", record.sequence)
            kept.append(record)
    return kept


def generate_thumbnail(
    image: Image.Image,
    max_edge: int = 300,
    quality: int = 85,
) -> str:
    """
    Generate a base64-encoded JPEG thumbnail of a scanned page.

    Fits the image inside a ``max_edge`` square (preserving aspect
    ratio), then encodes as JPEG and returns the base64 string.

    The full-size duplicate this used to start with is gone, as one of M-08's
    cheap wins (D-06): it copied a 26 MB page so that a 300 px thumbnail could
    be made from the copy. Pillow's ``contain`` operation produces the same
    small result directly, in 29 ms measured, with no full-size intermediate
    and no mutation of the caller's image.

    Args:
        image: PIL Image of the scanned page.
        max_edge: Maximum pixel length of the longest edge. Default 300.
        quality: JPEG quality (1-100). Default 85.

    Returns:
        Base64-encoded JPEG string (ASCII).

    """
    thumb = ImageOps.contain(image, (max_edge, max_edge), Resampling.LANCZOS)
    # Strip EXIF to avoid img2pdf/viewer orientation issues (Pitfall #5)
    thumb.info.pop("exif", None)
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
