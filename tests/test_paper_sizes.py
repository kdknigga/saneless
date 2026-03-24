"""Tests for paper size dimension lookup, crop utility, and config integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

import pytest
from PIL import Image

if TYPE_CHECKING:
    pass


class TestPaperSizeLiteral:
    """PaperSize Literal type tests."""

    def test_paper_size_literal_includes_all_sizes(self) -> None:
        """PaperSize Literal includes full, a3, a4, a5, letter, legal."""
        from saneless.paper_sizes import PaperSize

        args = get_args(PaperSize)
        assert set(args) == {"full", "a3", "a4", "a5", "letter", "legal"}


class TestPaperSizesMM:
    """PAPER_SIZES_MM constant tests."""

    def test_a4_dimensions(self) -> None:
        """PAPER_SIZES_MM['a4'] is (210.0, 297.0)."""
        from saneless.paper_sizes import PAPER_SIZES_MM

        assert PAPER_SIZES_MM["a4"] == (210.0, 297.0)

    def test_letter_dimensions(self) -> None:
        """PAPER_SIZES_MM['letter'] is (215.9, 279.4)."""
        from saneless.paper_sizes import PAPER_SIZES_MM

        assert PAPER_SIZES_MM["letter"] == (215.9, 279.4)

    def test_legal_dimensions(self) -> None:
        """PAPER_SIZES_MM['legal'] is (215.9, 355.6)."""
        from saneless.paper_sizes import PAPER_SIZES_MM

        assert PAPER_SIZES_MM["legal"] == (215.9, 355.6)

    def test_a3_dimensions(self) -> None:
        """PAPER_SIZES_MM['a3'] is (297.0, 420.0)."""
        from saneless.paper_sizes import PAPER_SIZES_MM

        assert PAPER_SIZES_MM["a3"] == (297.0, 420.0)

    def test_a5_dimensions(self) -> None:
        """PAPER_SIZES_MM['a5'] is (148.0, 210.0)."""
        from saneless.paper_sizes import PAPER_SIZES_MM

        assert PAPER_SIZES_MM["a5"] == (148.0, 210.0)

    def test_full_not_in_paper_sizes(self) -> None:
        """'full' is NOT in PAPER_SIZES_MM (it means no constraint)."""
        from saneless.paper_sizes import PAPER_SIZES_MM

        assert "full" not in PAPER_SIZES_MM


class TestCropToPaperSize:
    """crop_to_paper_size function tests."""

    def test_full_returns_unchanged(self) -> None:
        """crop_to_paper_size returns image unchanged when paper_size is 'full'."""
        from saneless.paper_sizes import crop_to_paper_size

        img = Image.new("RGB", (3000, 4000), "red")
        result = crop_to_paper_size(img, "full", 300)
        assert result is img

    def test_unknown_size_returns_unchanged(self) -> None:
        """crop_to_paper_size returns image unchanged when paper_size not in PAPER_SIZES_MM."""
        from saneless.paper_sizes import crop_to_paper_size

        img = Image.new("RGB", (3000, 4000), "red")
        result = crop_to_paper_size(img, "unknown_size", 300)
        assert result is img

    def test_a4_at_300dpi_crops_correctly(self) -> None:
        """crop_to_paper_size with 'a4' at 300 DPI crops to (2480, 3507) pixels."""
        from saneless.paper_sizes import crop_to_paper_size

        # A4 at 300 DPI: 210mm * 300 / 25.4 = 2480.3 -> 2480
        # A4 at 300 DPI: 297mm * 300 / 25.4 = 3507.8 -> 3507
        img = Image.new("RGB", (5000, 6000), "red")
        result = crop_to_paper_size(img, "a4", 300)
        assert result.size == (2480, 3507)

    def test_clamps_to_image_dimensions(self) -> None:
        """crop_to_paper_size clamps to image dimensions when crop exceeds image size."""
        from saneless.paper_sizes import crop_to_paper_size

        # Small image: crop would be larger than image
        img = Image.new("RGB", (100, 100), "red")
        result = crop_to_paper_size(img, "a4", 300)
        assert result.size == (100, 100)


class TestProfileConfigPaperSize:
    """ProfileConfig paper_size field tests."""

    def test_a4_validates(self) -> None:
        """ProfileConfig(paper_size='a4') validates successfully."""
        from saneless.config import ProfileConfig

        p = ProfileConfig(paper_size="a4")
        assert p.paper_size == "a4"

    def test_full_validates(self) -> None:
        """ProfileConfig(paper_size='full') validates successfully."""
        from saneless.config import ProfileConfig

        p = ProfileConfig(paper_size="full")
        assert p.paper_size == "full"

    def test_default_is_full(self) -> None:
        """ProfileConfig() has paper_size='full' by default."""
        from saneless.config import ProfileConfig

        p = ProfileConfig()
        assert p.paper_size == "full"

    def test_invalid_raises_validation_error(self) -> None:
        """ProfileConfig(paper_size='invalid') raises ValidationError."""
        from pydantic import ValidationError

        from saneless.config import ProfileConfig

        with pytest.raises(ValidationError):
            ProfileConfig(paper_size="invalid")


class TestScanSettingsPaperSize:
    """ScanSettings paper_size field tests."""

    def test_default_is_full(self) -> None:
        """ScanSettings defaults paper_size to 'full'."""
        from saneless.scanner.base import ScanSettings

        s = ScanSettings(source="Flatbed", resolution=300, mode="color")
        assert s.paper_size == "full"

    def test_accepts_paper_size(self) -> None:
        """ScanSettings accepts paper_size parameter."""
        from saneless.scanner.base import ScanSettings

        s = ScanSettings(
            source="Flatbed", resolution=300, mode="color", paper_size="a4"
        )
        assert s.paper_size == "a4"
