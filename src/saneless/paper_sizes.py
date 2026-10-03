"""Paper size dimension lookup and crop utility for scan area control."""

from __future__ import annotations

from typing import TYPE_CHECKING

from saneless.vocabulary import PaperSize

if TYPE_CHECKING:
    from PIL import Image

__all__ = ["PAPER_SIZES_MM", "PaperSize", "crop_to_paper_size"]

PAPER_SIZES_MM: dict[str, tuple[float, float]] = {
    "a3": (297.0, 420.0),
    "a4": (210.0, 297.0),
    "a5": (148.0, 210.0),
    "letter": (215.9, 279.4),
    "legal": (215.9, 355.6),
}
"""Width x height in millimeters for standard paper sizes.

``"full"`` is intentionally absent -- it means no constraint (scan full bed).
"""


def crop_to_paper_size(
    image: Image.Image,
    paper_size: PaperSize,
    dpi: int,
) -> Image.Image:
    """
    Crop an image to the given paper size at the specified DPI.

    The crop is **top-left aligned**, which assumes the sheet's top-left corner
    is the image's: true on a flatbed, or in a feeder window the device has
    already centred on the paper.  A feeder that guides the sheet into the
    middle of a wider window would lose the right edge of every page to it.

    Args:
        image: Source PIL image to crop.
        paper_size: Paper size key (e.g. ``"a4"``, ``"letter"``).
        dpi: Scan resolution in dots per inch.

    Returns:
        Cropped image, or the original if no constraint applies.

    """
    if paper_size == "full":
        return image

    dims = PAPER_SIZES_MM.get(paper_size)
    if dims is None:
        return image

    width_mm, height_mm = dims
    crop_w = min(round(width_mm * dpi / 25.4), image.width)
    crop_h = min(round(height_mm * dpi / 25.4), image.height)
    return image.crop((0, 0, crop_w, crop_h))
