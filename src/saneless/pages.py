"""
Page processing utilities: blank-page detection and thumbnail generation.

Measures how much of a page is ink -- the share of the page, inside a thin
trimmed margin, that is clearly darker than the paper it is printed on -- and
judges a page blank from that measurement and a per-profile threshold.  Also
provides the pipeline's blank-page filter, which judges each page from the
measurement its ``PageRecord`` carries rather than from the page itself, and
generates base64-encoded JPEG thumbnails of scanned pages.

``measure_ink`` and the thumbnail take an image; the filter takes records,
because the measurement it needs was made once already, at spool time, while
the page was in memory -- reading it off the record is what stops the page
being decoded a second time.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import TYPE_CHECKING, Final, NamedTuple

from PIL import Image, ImageChops, ImageOps
from PIL.Image import Resampling

if TYPE_CHECKING:
    from collections.abc import Sequence

    from saneless.scanner.base import PageRecord

__all__ = [
    "EDGE_TRIM",
    "INK_DELTA",
    "PAPER_PERCENTILE",
    "PAPER_WHITE_FLOOR",
    "BlankFilterResult",
    "InkMeasurement",
    "allow_large_scans",
    "filter_blank_pages",
    "generate_thumbnail",
    "is_blank",
    "measure_ink",
]

logger = logging.getLogger(__name__)

# The fraction of the page's width and height ignored on each edge, so a dark
# frame, a scanner lid or a backing sheet round a smaller page is not counted
# as ink.  Three per cent -- about 6 mm across and 9 mm down an A4 page --
# still trims a 4 mm frame, while keeping a word-processor footer 10-12.5 mm
# from the edge that five per cent would cut off.
EDGE_TRIM: Final = 0.03

# Paper white is the grey level at or below which this share of the trimmed
# page falls.  A page is mostly paper, so a high percentile finds the paper
# whatever its tint, while the brightest noise pixels cannot drag it upward;
# the 95th and 99th percentiles agreed within two levels on every page tried.
PAPER_PERCENTILE: Final = 0.99

# A pixel is ink when it is darker than paper white by more than this many
# grey levels.  Scanner noise stays well inside it, while faint marks do not:
# on paper measuring 247, anything up to about 207 -- light pencil included --
# still counts as ink.
INK_DELTA: Final = 40

# A page whose paper white is darker than this is not blank-looking paper at
# all -- a solid dark cover with reverse type, a photograph, a coloured sheet
# -- so it is kept whatever its ink coverage.  Keeping a page when unsure is
# the cheaper mistake: a kept blank costs a page, a dropped page may be the
# only copy.
PAPER_WHITE_FLOOR: Final = 128

# The image modes ``measure_ink`` reads; anything else is normalised first.
_MEASURABLE_MODES = frozenset({"1", "L", "RGB"})


class InkMeasurement(NamedTuple):
    """
    How much of a page is ink, and what its paper measures.

    A measurement, never a verdict: whether a page is blank depends on the
    coverage threshold, which is per profile, so it is decided by ``is_blank``
    from these two numbers.

    Attributes:
        coverage: The ink's share of the trimmed page, in percent (0-100).
        paper_white: The paper's grey level, 0-255, in the page's darkest
            channel.

    """

    coverage: float
    paper_white: int


class BlankFilterResult(NamedTuple):
    """
    What the blank-page filter kept, and where the pages it removed were.

    Attributes:
        kept: The records to assemble, in document order.
        removed_positions: The 1-based places in the scanned document of the
            pages removed as blank, in document order.  Empty when nothing
            was removed.

    """

    kept: list[PageRecord]
    removed_positions: tuple[int, ...]


def _darkest_channel(image: Image.Image) -> Image.Image:
    """
    Return one greyscale image holding each pixel's darkest channel.

    For a colour page that is the minimum of red, green and blue, which sees a
    yellow highlighter or a light-blue pen that a luminance conversion would
    render nearly as light as the paper.  A greyscale page is already its own
    darkest channel, and a one-bit page is widened to greyscale.

    Args:
        image: The page, in mode ``1``, ``L`` or ``RGB``.

    Returns:
        An ``L`` image the size of the page.

    """
    if image.mode == "RGB":
        red, green, blue = image.split()
        return ImageChops.darker(ImageChops.darker(red, green), blue)
    if image.mode == "L":
        return image
    return image.convert("L")


def _level_at(histogram: Sequence[int], count: float) -> int:
    """
    Return the lowest grey level whose cumulative pixel count reaches ``count``.

    Args:
        histogram: Pixel counts per grey level, darkest first.
        count: The cumulative count to reach.

    Returns:
        The grey level, 0-255.

    """
    running = 0
    for level, pixels in enumerate(histogram):
        running += pixels
        if running >= count:
            return level
    return len(histogram) - 1


def measure_ink(image: Image.Image) -> InkMeasurement:
    """
    Measure how much of a page is ink.

    The page is read in its darkest channel and trimmed by ``EDGE_TRIM`` on
    every edge.  Paper white is the level at the ``PAPER_PERCENTILE`` of what
    remains, and every pixel darker than paper white by more than
    ``INK_DELTA`` is ink.  The coverage is the ink's share of the trimmed
    page, so it measures whether marks are present rather than how much area
    they fill: a lone page number counts, and a tinted or noisy sheet does
    not.

    No threshold is applied here, because the threshold belongs to the
    profile; ``is_blank`` turns the measurement into a verdict.  The work is
    one channel reduction, one crop and one histogram, all at C speed, with no
    per-pixel Python loop.

    Args:
        image: The page, in mode ``1``, ``L`` or ``RGB``.

    Returns:
        The page's ink coverage and paper white.

    Raises:
        ValueError: If the page is in any other mode; the caller normalises
            the mode before measuring.

    """
    if image.mode not in _MEASURABLE_MODES:
        msg = (
            f"cannot measure ink on a page in image mode {image.mode!r}; "
            f"expected one of {sorted(_MEASURABLE_MODES)}"
        )
        raise ValueError(msg)
    darkest = _darkest_channel(image)
    width, height = darkest.size
    trim_x, trim_y = round(width * EDGE_TRIM), round(height * EDGE_TRIM)
    if width > 2 * trim_x and height > 2 * trim_y:
        darkest = darkest.crop((trim_x, trim_y, width - trim_x, height - trim_y))
    histogram = darkest.histogram()
    total = sum(histogram)
    paper_white = _level_at(histogram, total * PAPER_PERCENTILE)
    ink = sum(histogram[: max(paper_white - INK_DELTA, 0)])
    coverage = 100.0 * ink / total if total else 0.0
    return InkMeasurement(coverage=coverage, paper_white=paper_white)


def is_blank(coverage: float, paper_white: int, *, coverage_threshold: float) -> bool:
    """
    Decide whether a measured page is blank.

    A page is blank when its paper is light enough to be paper at all -- at
    or above ``PAPER_WHITE_FLOOR`` -- and its ink coverage is at or below the
    threshold.  At a threshold of zero only a page with no ink pixel at all is
    blank.  Anything the rule is unsure of is kept: a kept blank costs a page,
    a dropped page may be the only copy.

    Args:
        coverage: The page's ink coverage, in percent, from ``measure_ink``.
        paper_white: The page's paper-white level, from ``measure_ink``.
        coverage_threshold: The most ink, in percent of the trimmed page, a
            blank page may carry.  The profile supplies it.

    Returns:
        True if the page is blank, False if it is kept.

    """
    return paper_white >= PAPER_WHITE_FLOOR and coverage <= coverage_threshold


def allow_large_scans() -> None:
    """
    Raise Pillow's decompression-bomb limit so high-dpi pages can be opened.

    A 600 dpi A4 colour page is about 34.8M pixels and a 1200 dpi one about
    139M, over Pillow's default limit of about 89.5M.  Pages arrive from
    python-sane through ``Image.frombuffer``, which does not check the limit,
    but img2pdf re-opens every spooled page with ``Image.open``, which does,
    so without this a high-dpi scan would fail at PDF assembly.

    The limit is process-wide, so it is set here, once, by the entry point
    at start-up, rather than as a side effect of importing a module.
    """
    Image.MAX_IMAGE_PIXELS = 200_000_000


def filter_blank_pages(
    pages: Sequence[PageRecord], *, coverage_threshold: float
) -> BlankFilterResult:
    """
    Remove blank pages from a record sequence, and say where they were.

    Each page is judged by ``is_blank`` from the ink coverage and paper white
    its record carries, measured once when it was spooled; nothing here opens
    a page file.

    Records in, records out.  The pages are files on the spool by the time
    this runs, so what gets filtered is the list, never the directory: a
    removed page is simply not referenced by the result, and nothing here
    unlinks it or copies it anywhere.  It goes when the job's workspace does.
    The surviving records keep their relative order, and their ``sequence``
    values keep the gaps the removals left -- those numbers name the sheet
    the device fed within its pass.

    What was removed is reported by **position** instead: the page's 1-based
    place in ``pages``, which is the scanned document in order -- for manual
    duplex, the interleaved one.  That is the number the operator reads on
    every surface and rescans by, and it is the number each log line gives.

    One INFO line is logged per page, whether kept or removed, naming its
    position, its spooled file, its coverage at full precision, its paper
    white, the threshold and the verdict, so a page that went missing can be
    traced from the log alone.

    Args:
        pages: The page records to filter, in document order.
        coverage_threshold: The most ink, in percent of the trimmed page, a
            blank page may carry.  The profile supplies it.

    Returns:
        The records kept, and the 1-based positions of those removed, in
        document order.  Both may be empty; ``kept`` is empty when every page
        was blank.

    """
    total = len(pages)
    kept: list[PageRecord] = []
    removed: list[int] = []
    for position, record in enumerate(pages, start=1):
        blank = is_blank(
            record.ink_coverage,
            record.paper_white,
            coverage_threshold=coverage_threshold,
        )
        logger.info(
            "Blank-page check: page %d of %d (%s): ink %r%% of the inset, "
            "paper white %d, threshold %r%% -> %s",
            position,
            total,
            record.path.name,
            record.ink_coverage,
            record.paper_white,
            coverage_threshold,
            "REMOVE" if blank else "KEEP",
        )
        if blank:
            removed.append(position)
        else:
            kept.append(record)
    return BlankFilterResult(kept=kept, removed_positions=tuple(removed))


def generate_thumbnail(
    image: Image.Image,
    max_edge: int = 300,
    quality: int = 85,
) -> str:
    """
    Generate a base64-encoded JPEG thumbnail of a scanned page.

    Fits the image inside a ``max_edge`` square (preserving aspect
    ratio), then encodes as JPEG and returns the base64 string.

    The full-size duplicate this used to start with is gone, because it
    copied a 26 MB page so that a 300 px thumbnail could be made from the
    copy. Pillow's ``contain`` operation produces the same small result
    directly, in 29 ms measured, with no full-size intermediate and no
    mutation of the caller's image.

    Args:
        image: PIL Image of the scanned page.
        max_edge: Maximum pixel length of the longest edge. Default 300.
        quality: JPEG quality (1-100). Default 85.

    Returns:
        Base64-encoded JPEG string (ASCII).

    """
    thumb = ImageOps.contain(image, (max_edge, max_edge), Resampling.LANCZOS)
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
