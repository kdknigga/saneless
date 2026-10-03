"""Paper sizes have fixed dimensions, crop images to fit, and validate in config."""

from __future__ import annotations

from typing import cast, get_args

import pytest
from PIL import Image
from pydantic import ValidationError

import saneless.vocabulary as vocabulary_module
from saneless.config import ProfileConfig
from saneless.paper_sizes import PAPER_SIZES_MM, PaperSize, crop_to_paper_size
from saneless.scanner.base import ScanSettings


class TestPaperSizeLiteral:
    """The set of paper-size names."""

    def test_paper_size_literal_includes_all_sizes(self) -> None:
        """The paper sizes are exactly full, a3, a4, a5, letter and legal."""
        args = get_args(PaperSize)
        assert set(args) == {"full", "a3", "a4", "a5", "letter", "legal"}


class TestPaperSizeHasOneDefinition:
    """
    The paper-size names are defined once and the lookup table cannot drift.

    ``PaperSize`` lives in the vocabulary module, and every other module uses
    that one object.  ``PAPER_SIZES_MM`` has an entry for every name except
    ``"full"``, which means the whole bed and so has no dimensions.
    """

    def test_the_table_covers_every_size_but_full(self) -> None:
        """Every non-full paper size has dimensions, and nothing else does."""
        assert set(PAPER_SIZES_MM) == set(get_args(vocabulary_module.PaperSize)) - {
            "full"
        }

    def test_the_other_modules_use_the_vocabulary_definition(self) -> None:
        """The paper-size module and the profile field share the one Literal."""
        assert PaperSize is vocabulary_module.PaperSize
        field = ProfileConfig.model_fields["paper_size"]
        assert field.annotation is vocabulary_module.PaperSize


class TestPaperSizesMM:
    """Each paper size's width and height in millimetres."""

    def test_a4_dimensions(self) -> None:
        """A4 is 210 by 297 mm."""
        assert PAPER_SIZES_MM["a4"] == (210.0, 297.0)

    def test_letter_dimensions(self) -> None:
        """Letter is 215.9 by 279.4 mm."""
        assert PAPER_SIZES_MM["letter"] == (215.9, 279.4)

    def test_legal_dimensions(self) -> None:
        """Legal is 215.9 by 355.6 mm."""
        assert PAPER_SIZES_MM["legal"] == (215.9, 355.6)

    def test_a3_dimensions(self) -> None:
        """A3 is 297 by 420 mm."""
        assert PAPER_SIZES_MM["a3"] == (297.0, 420.0)

    def test_a5_dimensions(self) -> None:
        """A5 is 148 by 210 mm."""
        assert PAPER_SIZES_MM["a5"] == (148.0, 210.0)

    def test_full_not_in_paper_sizes(self) -> None:
        """'full' has no dimensions, because it means the whole bed."""
        assert "full" not in PAPER_SIZES_MM


class TestCropToPaperSize:
    """Cropping a scanned image to the chosen paper size."""

    def test_full_returns_unchanged(self) -> None:
        """'full' returns the very image it was given."""
        img = Image.new("RGB", (3000, 4000), "red")
        result = crop_to_paper_size(img, "full", 300)
        assert result is img

    def test_unknown_size_returns_unchanged(self) -> None:
        """A size with no dimensions returns the very image it was given."""
        img = Image.new("RGB", (3000, 4000), "red")
        # Past the type, as an unvalidated caller would: the guard is for them.
        unknown = cast("PaperSize", "unknown_size")
        result = crop_to_paper_size(img, unknown, 300)
        assert result is img

    def test_a4_at_300dpi_crops_correctly(self) -> None:
        """A4 at 300 DPI crops to 2480 by 3508 pixels."""
        # 210 * 300 / 25.4 = 2480.31, rounds to 2480
        # 297 * 300 / 25.4 = 3507.87, rounds to 3508
        img = Image.new("RGB", (5000, 6000), "red")
        result = crop_to_paper_size(img, "a4", 300)
        assert result.size == (2480, 3508)

    def test_clamps_to_image_dimensions(self) -> None:
        """An image smaller than the paper size is left at its own size."""
        # The A4 crop is far larger than this image.
        img = Image.new("RGB", (100, 100), "red")
        result = crop_to_paper_size(img, "a4", 300)
        assert result.size == (100, 100)


class TestProfileConfigPaperSize:
    """A profile's paper_size accepts only known sizes."""

    def test_a4_validates(self) -> None:
        """A profile with paper_size 'a4' validates."""
        p = ProfileConfig(paper_size="a4")
        assert p.paper_size == "a4"

    def test_full_validates(self) -> None:
        """A profile with paper_size 'full' validates."""
        p = ProfileConfig(paper_size="full")
        assert p.paper_size == "full"

    def test_invalid_raises_validation_error(self) -> None:
        """An unknown paper_size is refused with a ValidationError."""
        with pytest.raises(ValidationError):
            ProfileConfig.model_validate({"paper_size": "invalid"})


class TestScanSettingsPaperSize:
    """The paper size a scan's settings carry."""

    def test_default_is_full(self) -> None:
        """Scan settings default to the full bed."""
        s = ScanSettings(source="Flatbed", resolution=300, mode="color")
        assert s.paper_size == "full"

    def test_accepts_paper_size(self) -> None:
        """Scan settings keep the paper size they are given."""
        s = ScanSettings(
            source="Flatbed", resolution=300, mode="color", paper_size="a4"
        )
        assert s.paper_size == "a4"
