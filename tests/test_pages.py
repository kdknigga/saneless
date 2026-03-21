"""Tests for page processing utilities: empty page detection and thumbnail generation."""

import base64
import io

from PIL import Image, ImageDraw

from saneless.pages import filter_empty_pages, generate_thumbnail, is_empty_page


class TestEmptyPageDetection:
    """Tests for is_empty_page dual-threshold detection."""

    def test_pure_white_is_empty(self) -> None:
        """Pure white image (mean=255, stddev=0) is detected as empty."""
        img = Image.new("RGB", (200, 300), "white")
        assert is_empty_page(img) is True

    def test_nearly_white_is_empty(self, empty_page_image: Image.Image) -> None:
        """Nearly-white image (253,253,253) is detected as empty."""
        assert is_empty_page(empty_page_image) is True

    def test_dark_content_is_not_empty(self, content_page_image: Image.Image) -> None:
        """Image with dark content (low mean) is not empty."""
        assert is_empty_page(content_page_image) is False

    def test_text_like_content_not_empty(self) -> None:
        """Image with text-like content (high stddev) is not empty."""
        img = Image.new("RGB", (200, 300), "white")
        draw = ImageDraw.Draw(img)
        # Draw scattered dark lines to create high stddev
        for y in range(0, 300, 10):
            draw.line([(0, y), (200, y)], fill="black", width=2)
        assert is_empty_page(img) is False

    def test_custom_thresholds_stricter(self) -> None:
        """Custom stricter thresholds reject a nearly-white image."""
        # This image has mean ~253, stddev ~0 in grayscale
        img = Image.new("RGB", (200, 300), (253, 253, 253))
        # Default thresholds: empty
        assert is_empty_page(img) is True
        # Stricter mean threshold (254): now mean=253 is NOT above 254
        assert is_empty_page(img, mean_threshold=254.0) is False

    def test_custom_thresholds_looser(self) -> None:
        """Custom looser thresholds accept a lightly-shaded image."""
        # Image with some light gray -- mean ~200
        img = Image.new("RGB", (200, 300), (200, 200, 200))
        # Default: not empty (mean 200 < 250)
        assert is_empty_page(img) is False
        # Looser: mean_threshold=190
        assert is_empty_page(img, mean_threshold=190.0) is True


class TestFilterEmptyPages:
    """Tests for filter_empty_pages list filtering."""

    def test_filters_empty_from_mixed(
        self, content_page_image: Image.Image, empty_page_image: Image.Image
    ) -> None:
        """Mixed list of content and empty pages returns only content pages."""
        pages = [
            content_page_image,
            empty_page_image,
            content_page_image.copy(),
            empty_page_image.copy(),
            content_page_image.copy(),
        ]
        result = filter_empty_pages(pages)
        assert len(result) == 3

    def test_all_empty_returns_empty_list(self) -> None:
        """All-empty page list returns empty list."""
        pages = [
            Image.new("RGB", (200, 300), "white"),
            Image.new("RGB", (200, 300), (254, 254, 254)),
        ]
        result = filter_empty_pages(pages)
        assert result == []

    def test_no_empty_returns_all(self, content_page_image: Image.Image) -> None:
        """No empty pages returns all pages."""
        pages = [
            content_page_image,
            content_page_image.copy(),
            content_page_image.copy(),
        ]
        result = filter_empty_pages(pages)
        assert len(result) == 3


class TestThumbnailGeneration:
    """Tests for generate_thumbnail."""

    def test_returns_nonempty_base64(self, content_page_image: Image.Image) -> None:
        """generate_thumbnail returns a non-empty base64 string."""
        result = generate_thumbnail(content_page_image)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_decodes_to_valid_jpeg(self, content_page_image: Image.Image) -> None:
        """Base64 output decodes to valid JPEG image data."""
        result = generate_thumbnail(content_page_image)
        raw = base64.b64decode(result)
        img = Image.open(io.BytesIO(raw))
        assert img.format == "JPEG"

    def test_landscape_max_edge(self) -> None:
        """Landscape image thumbnail has long edge <= 300px."""
        img = Image.new("RGB", (600, 400), "blue")
        result = generate_thumbnail(img)
        raw = base64.b64decode(result)
        thumb = Image.open(io.BytesIO(raw))
        assert max(thumb.width, thumb.height) <= 300

    def test_portrait_max_edge(self) -> None:
        """Portrait image thumbnail has long edge <= 300px."""
        img = Image.new("RGB", (400, 600), "green")
        result = generate_thumbnail(img)
        raw = base64.b64decode(result)
        thumb = Image.open(io.BytesIO(raw))
        assert max(thumb.width, thumb.height) <= 300

    def test_landscape_aspect_ratio(self) -> None:
        """Landscape 600x400 produces 300x200 thumbnail."""
        img = Image.new("RGB", (600, 400), "blue")
        result = generate_thumbnail(img)
        raw = base64.b64decode(result)
        thumb = Image.open(io.BytesIO(raw))
        assert thumb.width == 300
        assert thumb.height == 200

    def test_portrait_aspect_ratio(self) -> None:
        """Portrait 400x600 produces 200x300 thumbnail."""
        img = Image.new("RGB", (400, 600), "green")
        result = generate_thumbnail(img)
        raw = base64.b64decode(result)
        thumb = Image.open(io.BytesIO(raw))
        assert thumb.width == 200
        assert thumb.height == 300
