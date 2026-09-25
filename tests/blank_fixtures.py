"""
Synthetic scanned pages for the blank-page detector's verdict tests.

The pages follow the cases the code review gave for blank-page detection: a
page carrying nothing but a footer page number, a lone small digit, faint
pencil, one typed line, a highlighter stroke, a dark "reverse text" cover,
and the blanks that must still go -- a tinted, noisy sheet, a sheet with a
few dust specks and a sheet framed by a dark border or backing.  Real paper
is a follow-up; these are the stand-ins until then.

Each builder returns a fresh A4 page at 300 dpi (2480 x 3508) unless its
``dpi`` keyword says otherwise.  Every page but the highlighter and the
reverse-text cover sits on the same paper: a light grey tint with Gaussian
scanner noise, because a scanner never returns pure white.  The noise comes
from seeded bytes -- the SHAKE-256 stream of a fixed seed -- rather than
Pillow's own noise effect, which draws from the C library's generator and so
differs from one build to the next; seeded bytes make every build of a page
identical.

The builders use Pillow primitives only -- no per-pixel Python loop -- so an
A4 page costs milliseconds, and nothing is cached between tests.

- ``tinted_blank``: paper and noise, nothing else.
- ``dusty_blank``: the tinted blank with a handful of small dark specks.
- ``framed_blank``: the tinted blank inside a dark frame, as a scanner lid or
  backing sheet leaves round a page smaller than the glass.
- ``footer_page_number``: ``- 7 -`` centred in a Word-style footer.
- ``lone_digit``: one small digit in the middle of the page.
- ``pencil_lines``: three short, faint, thin strokes.
- ``typed_line``: one sentence of 11pt type.
- ``highlighter_stroke``: an RGB page whose only mark is a yellow band.
- ``reverse_text_cover``: white type on a solid dark page.

Import it as ``from tests.blank_fixtures import ...``; the bare
``blank_fixtures`` form raises ``ModuleNotFoundError`` under pytest 9's
importlib mode, for the same reason ``tests.conftest`` is imported by its
package path.
"""

from __future__ import annotations

import hashlib
from statistics import NormalDist

from PIL import Image, ImageDraw, ImageFont

__all__ = [
    "A4_DPI",
    "dusty_blank",
    "footer_page_number",
    "framed_blank",
    "highlighter_stroke",
    "lone_digit",
    "pencil_lines",
    "reverse_text_cover",
    "tinted_blank",
    "typed_line",
]

# The resolution every page is built at unless a test asks for another.
A4_DPI = 300

# A4 in millimetres, and the millimetres in an inch.
_A4_MM = (210.0, 297.0)
_MM_PER_INCH = 25.4

# A typographic point is 1/72 inch.
_POINTS_PER_INCH = 72

# The paper tint and noise every inked page sits on.
_PAPER_TINT = 243
_PAPER_NOISE = 3.0

# One seed per use, so that no two fixtures share a noise field by accident.
_SEED_PAPER = b"paper"
_SEED_DUST = b"dust"
_SEED_FRAME = b"frame"

# How many seeded bytes make one draw from ``_seeded_draws``.
_DRAW_BYTES = 4

# The byte range of an 8-bit channel.
_LEVELS = 256
_WHITE = 255

# The sentence ``typed_line`` sets.
_SENTENCE = "Please find enclosed the signed copy of the agreement."


def _page_size(dpi: int) -> tuple[int, int]:
    """
    Return the pixel size of an A4 page at ``dpi``.

    Args:
        dpi: Scan resolution in dots per inch.

    Returns:
        ``(width, height)`` in pixels.

    """
    width_mm, height_mm = _A4_MM
    return round(width_mm * dpi / _MM_PER_INCH), round(height_mm * dpi / _MM_PER_INCH)


def _mm(millimetres: float, dpi: int) -> int:
    """
    Convert a length in millimetres to whole pixels at ``dpi``.

    Args:
        millimetres: The length on paper.
        dpi: Scan resolution in dots per inch.

    Returns:
        The length in pixels, rounded.

    """
    return round(millimetres * dpi / _MM_PER_INCH)


def _font(points: float, dpi: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """
    Return Pillow's built-in font at a point size, as it prints at ``dpi``.

    Args:
        points: Type size in points.
        dpi: Scan resolution in dots per inch.

    Returns:
        The default font scaled to that size.

    """
    return ImageFont.load_default(size=points * dpi / _POINTS_PER_INCH)


def _seeded_bytes(seed: bytes, count: int) -> bytes:
    """
    Return ``count`` bytes that look random but are fixed by ``seed``.

    Args:
        seed: The seed; the same seed always gives the same bytes.
        count: How many bytes to return.

    Returns:
        The first ``count`` bytes of the seed's SHAKE-256 stream.

    """
    return hashlib.shake_256(seed).digest(count)


def _seeded_draws(seed: bytes, count: int) -> list[int]:
    """
    Return ``count`` non-negative integers fixed by ``seed``.

    Args:
        seed: The seed; the same seed always gives the same draws.
        count: How many draws to return.

    Returns:
        Integers spread evenly enough for placing a few marks on a page.

    """
    stream = _seeded_bytes(seed, count * _DRAW_BYTES)
    return [
        int.from_bytes(stream[index : index + _DRAW_BYTES])
        for index in range(0, len(stream), _DRAW_BYTES)
    ]


def _uniform_noise(size: tuple[int, int], seed: bytes) -> Image.Image:
    """
    Return a greyscale image of seeded, uniformly distributed bytes.

    Args:
        size: ``(width, height)`` in pixels.
        seed: The seed, so the same call gives the same image.

    Returns:
        An ``L`` image whose pixels are independent uniform draws over 0-255.

    """
    width, height = size
    return Image.frombytes("L", size, _seeded_bytes(seed, width * height))


def tinted_blank(
    tint: int = _PAPER_TINT, noise: float = _PAPER_NOISE, *, dpi: int = A4_DPI
) -> Image.Image:
    """
    Build a blank sheet of tinted paper with Gaussian scanner noise.

    The noise is made by mapping seeded uniform bytes through the inverse of
    the normal distribution, one 256-entry lookup table applied at C speed.
    Levels above white clip at white, as a scanner's do.

    Args:
        tint: The paper's grey level, 0-255.
        noise: Standard deviation of the noise, in grey levels.
        dpi: Scan resolution in dots per inch.

    Returns:
        An ``L`` page with nothing on it.

    """
    normal = NormalDist(mu=tint, sigma=noise) if noise > 0 else None
    table = [
        tint
        if normal is None
        else min(max(round(normal.inv_cdf((level + 0.5) / _LEVELS)), 0), _WHITE)
        for level in range(_LEVELS)
    ]
    return _uniform_noise(_page_size(dpi), _SEED_PAPER).point(table)


def dusty_blank(specks: int = 5, *, dpi: int = A4_DPI) -> Image.Image:
    """
    Build a tinted blank carrying a few specks of dust.

    Each speck is a 1-4 px square of grey 60-160 at a seeded position well
    inside the page, where no edge trim can hide it.

    Args:
        specks: How many specks to scatter.
        dpi: Scan resolution in dots per inch.

    Returns:
        An ``L`` page with only dust on it.

    """
    page = tinted_blank(dpi=dpi)
    width, height = page.size
    margin_x, margin_y = width // 10, height // 10
    draws = iter(_seeded_draws(_SEED_DUST, specks * 4))
    draw = ImageDraw.Draw(page)
    for _ in range(specks):
        side = 1 + next(draws) % 4
        left = margin_x + next(draws) % (width - 2 * margin_x)
        top = margin_y + next(draws) % (height - 2 * margin_y)
        grey = 60 + next(draws) % 101
        draw.rectangle((left, top, left + side - 1, top + side - 1), fill=grey)
    return page


def framed_blank(frame_mm: float = 3.0, *, dpi: int = A4_DPI) -> Image.Image:
    """
    Build a tinted blank inside a dark frame on every edge.

    The frame is textured grey 0-30, the way a black scanner lid or backing
    sheet shows round a page, and ``frame_mm`` wide on each edge.

    Args:
        frame_mm: Width of the frame, in millimetres.
        dpi: Scan resolution in dots per inch.

    Returns:
        An ``L`` page with nothing on it but the frame.

    """
    page = tinted_blank(dpi=dpi)
    width, height = page.size
    frame = _uniform_noise(page.size, _SEED_FRAME).point(
        [level * 30 // _WHITE for level in range(_LEVELS)]
    )
    inner = _mm(frame_mm, dpi)
    mask = Image.new("L", page.size, _WHITE)
    ImageDraw.Draw(mask).rectangle(
        (inner, inner, width - inner - 1, height - inner - 1), fill=0
    )
    return Image.composite(frame, page, mask)


def footer_page_number(
    offset_mm: float = 12.5,
    text: str = "- 7 -",
    points: float = 11,
    *,
    tint: int = _PAPER_TINT,
    dpi: int = A4_DPI,
) -> Image.Image:
    """
    Build a page whose only content is a centred footer page number.

    The text's baseline sits ``offset_mm`` above the bottom edge, the place a
    word processor's footer puts it.

    Args:
        offset_mm: Distance from the bottom edge to the baseline.
        text: The page number as printed.
        points: Type size in points.
        tint: The paper's grey level.
        dpi: Scan resolution in dots per inch.

    Returns:
        An ``L`` page carrying only the page number.

    """
    page = tinted_blank(tint, dpi=dpi)
    width, height = page.size
    ImageDraw.Draw(page).text(
        (width / 2, height - _mm(offset_mm, dpi)),
        text,
        fill=0,
        font=_font(points, dpi),
        anchor="ms",
    )
    return page


def lone_digit(points: float = 9, *, dpi: int = A4_DPI) -> Image.Image:
    """
    Build a page whose only content is one small digit in its middle.

    Args:
        points: Type size in points.
        dpi: Scan resolution in dots per inch.

    Returns:
        An ``L`` page carrying a single ``7``.

    """
    page = tinted_blank(dpi=dpi)
    width, height = page.size
    ImageDraw.Draw(page).text(
        (width / 2, height / 2), "7", fill=0, font=_font(points, dpi), anchor="mm"
    )
    return page


def pencil_lines(
    grey: int = 175, lines: int = 3, *, tint: int = _PAPER_TINT, dpi: int = A4_DPI
) -> Image.Image:
    """
    Build a page whose only content is a few faint pencil strokes.

    Each stroke is 2 px thick and a third of the page wide, one above the
    other in the upper half of the page.

    Args:
        grey: The pencil's grey level.
        lines: How many strokes to draw.
        tint: The paper's grey level.
        dpi: Scan resolution in dots per inch.

    Returns:
        An ``L`` page carrying only the strokes.

    """
    page = tinted_blank(tint, dpi=dpi)
    width, height = page.size
    draw = ImageDraw.Draw(page)
    left = width // 3
    for index in range(lines):
        top = height // 4 + index * _mm(8, dpi)
        draw.rectangle((left, top, left + width // 3 - 1, top + 1), fill=grey)
    return page


def typed_line(*, dpi: int = A4_DPI) -> Image.Image:
    """
    Build a page carrying one typed sentence near the top.

    Args:
        dpi: Scan resolution in dots per inch.

    Returns:
        An ``L`` page with one line of 11pt type.

    """
    page = tinted_blank(dpi=dpi)
    ImageDraw.Draw(page).text(
        (_mm(25, dpi), _mm(40, dpi)),
        _SENTENCE,
        fill=0,
        font=_font(11, dpi),
        anchor="ls",
    )
    return page


def highlighter_stroke(*, dpi: int = A4_DPI) -> Image.Image:
    """
    Build a colour page whose only mark is a yellow highlighter band.

    There is no dark ink anywhere: in greyscale the yellow is nearly as light
    as the paper, and only its blue channel shows it.

    Args:
        dpi: Scan resolution in dots per inch.

    Returns:
        An ``RGB`` page carrying one pure-yellow band.

    """
    page = tinted_blank(dpi=dpi).convert("RGB")
    width, height = page.size
    top = height // 3
    ImageDraw.Draw(page).rectangle(
        (width // 4, top, width * 3 // 4, top + _mm(5, dpi)), fill=(255, 240, 60)
    )
    return page


def reverse_text_cover(*, dpi: int = A4_DPI) -> Image.Image:
    """
    Build a solid dark cover carrying white type, as a report cover might.

    Args:
        dpi: Scan resolution in dots per inch.

    Returns:
        An ``L`` page filled grey 30 with one white title.

    """
    page = Image.new("L", _page_size(dpi), 30)
    width, height = page.size
    ImageDraw.Draw(page).text(
        (width / 2, height / 3),
        "ANNUAL REPORT",
        fill=_WHITE,
        font=_font(36, dpi),
        anchor="mm",
    )
    return page
