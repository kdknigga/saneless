"""
How long the parent lets one page take, from the parameters the device reports.

This is the parent's page deadline.  The constants are read at call time, so
tests can shorten them.  It imports no python-sane.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from saneless.paper_sizes import PAPER_SIZES_MM
from saneless.vocabulary import scan_page_description

# The per-page timeout scales with the page the device agreed to send: twice
# the reference page's minute (A4, 600 dpi, 8-bit colour), pro rata by bytes.
# The floor keeps every page at two minutes or more.  The ceiling exists
# because the parameters are device-reported numbers, over the LAN for a `net`
# scanner, and a frame claiming 2**31-1 bytes a line would otherwise earn a
# budget too large for `threading.Event.wait` to accept.
#
# A device that does not know the length in advance, as a feeder may not, is
# budgeted for a legal sheet, the longest paper size a profile can name.
_REFERENCE_PAGE_BYTES: Final = 4961 * 7016 * 3
_REFERENCE_PAGE_SECONDS: Final = 60.0
_PAGE_TIMEOUT_FLOOR_SECONDS: Final = 120.0
_PAGE_TIMEOUT_CEILING_SECONDS: Final = 3600.0
_UNKNOWN_LENGTH_MM: Final = PAPER_SIZES_MM["legal"][1]

# The frame formats of a three-pass colour scan, which sends a page as three
# frames, one per channel, each described by the same parameters.
_THREE_PASS_FORMATS: Final = frozenset({"red", "green", "blue"})

# Every frame format of a colour page, sent as one frame or as three.
_COLOUR_FORMATS: Final = _THREE_PASS_FORMATS | {"color"}

# The same factor crop_to_paper_size() uses.
_MM_PER_INCH = 25.4


@dataclass(frozen=True)
class _ScanParameters:
    """
    The frame the device reports it is set up to scan, before any page starts.

    What python-sane's ``get_parameters()`` returns, with names, read after
    every option and the scan area are set.

    Attributes:
        frame_format: SANE's frame format, such as ``"gray"`` or ``"color"``.
        last_frame: Whether this is the last frame of the image.
        pixels_per_line: The width of the frame in pixels.
        lines: The height of the frame in lines, or -1 when the device does
            not know it in advance, as a feeder may not.
        depth: The bits per sample.
        bytes_per_line: The length of one line of image data in bytes.

    """

    frame_format: str
    last_frame: bool
    pixels_per_line: int
    lines: int
    depth: int
    bytes_per_line: int


def _page_budget_seconds(parameters: _ScanParameters, resolution: int) -> float:
    """
    Return how long one page may take, from the page the device will send.

    See the constants for the formula.  A three-pass colour scan counts three
    frames, and a negative line length counts as no data.
    """
    lines = parameters.lines
    if lines <= 0:
        # SANE reports -1 when the length is not known in advance.
        lines = math.ceil(resolution * _UNKNOWN_LENGTH_MM / _MM_PER_INCH)
    frames = 3 if parameters.frame_format in _THREE_PASS_FORMATS else 1
    page_bytes = max(parameters.bytes_per_line, 0) * lines * frames
    estimate = _REFERENCE_PAGE_SECONDS * page_bytes / _REFERENCE_PAGE_BYTES
    return min(
        _PAGE_TIMEOUT_CEILING_SECONDS,
        max(_PAGE_TIMEOUT_FLOOR_SECONDS, 2.0 * estimate),
    )


def _describe_page(parameters: _ScanParameters, resolution: int) -> str:
    """
    Describe the page a budget was worked out for, as a timeout message names it.

    Args:
        parameters: What the configured device reports.
        resolution: The resolution the device settled on.

    Returns:
        The page's description.

    """
    return scan_page_description(
        parameters.pixels_per_line,
        parameters.lines,
        colour=parameters.frame_format in _COLOUR_FORMATS,
        dpi=resolution,
    )


def _page_label(page_num: int) -> str:
    """
    Name one page, for its timeout message and its reader thread.

    Args:
        page_num: Zero-based index of the page being acquired.

    Returns:
        The one-based human label, e.g. ``"Page 3"``.

    """
    return f"Page {page_num + 1}"
