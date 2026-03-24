"""Paper size dimension lookup and crop utility for scan area control."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from PIL import Image

__all__ = ["PAPER_SIZES_MM", "PaperSize", "crop_to_paper_size"]

PaperSize = Literal["full", "a3", "a4", "a5", "letter", "legal"]
"""Valid paper size names for scan area constraint."""

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
    paper_size: str,
    dpi: int,
) -> Image.Image:
    """
    Crop an image to the given paper size at the specified DPI.

    Returns the image unchanged when *paper_size* is ``"full"`` or not
    recognised.  Otherwise crops top-left aligned to the calculated
    pixel dimensions, clamping to the actual image size.

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
    crop_w = min(int(width_mm * dpi / 25.4), image.width)
    crop_h = min(int(height_mm * dpi / 25.4), image.height)
    return image.crop((0, 0, crop_w, crop_h))
