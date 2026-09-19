"""
Tests for page processing utilities: empty page detection and thumbnails.

``is_empty_page`` no longer takes a page. It takes the greyscale mean and
standard deviation the spool measured once, while the page was in memory at
write time (D-06), so most of these tests hand it the two numbers directly:
building an image purely to produce a mean and a stddev modelled nothing that
the production code does any more.

One class deliberately does keep a real page in the picture --
``TestStatisticsComeFromASpooledPage`` runs pages through a real
``SpooledPageSink`` -- so the numbers the rest of the file passes as literals
are still proven to be the numbers a real page produces.
"""

from __future__ import annotations

import base64
import inspect
import io
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from PIL import Image, ImageDraw

import saneless
import saneless.cli as cli_module
import saneless.pages as pages_module
from saneless.pages import filter_empty_pages, generate_thumbnail, is_empty_page
from saneless.pipeline import _SPOOL_LABEL_A
from saneless.spool import SpooledPageSink

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from saneless.scanner.base import PageRecord

# The reserve the spool keeps free beyond each page. One megabyte is the
# smallest honest value: these pages are a few tens of kilobytes, so the check
# passes anywhere the test suite can run at all, and it is still a real check
# rather than a disabled one.
_TEST_RESERVE_MB = 1

# The pixel ceiling the application relaxes Pillow's decompression-bomb check
# to at start-up: above a 1200 dpi A4 colour page, about 139M pixels.
_LARGE_SCAN_PIXEL_LIMIT = 200_000_000

# The EXIF orientation tag, set on a source image so a test can tell whether
# any EXIF survived into what saneless wrote.
_EXIF_ORIENTATION_TAG = 0x0112

# How long the fresh interpreter that imports the imaging modules may take.
_CHILD_IMPORT_SECONDS = 30.0

# Run in a fresh interpreter, so no earlier import in the test session can
# have changed Pillow's limit first.  Prints the limit before and after
# importing every module that used to change it as a side effect.
_IMPORT_CHECK = """
import PIL.Image

before = PIL.Image.MAX_IMAGE_PIXELS
import saneless.pages
import saneless.pdf
import saneless.scanner.sane_backend

print(before, PIL.Image.MAX_IMAGE_PIXELS)
"""


def _spool(directory: Path, pages: Sequence[Image.Image]) -> list[PageRecord]:
    """
    Spool pages through a real sink and hand back the records it made.

    A real ``SpooledPageSink`` rather than hand-built records: the point of
    every test that calls this is that the statistics on a record were measured
    from an actual page written to an actual file, not typed in by a test.

    Args:
        directory: Where the pages land. Must already exist.
        pages: The pages to spool, in order.

    Returns:
        One record per page, in the order they were added.

    """
    sink = SpooledPageSink(directory, _SPOOL_LABEL_A, _TEST_RESERVE_MB)
    return [sink.add(page) for page in pages]


def _white_page() -> Image.Image:
    """
    Return a page with nothing on it.

    Returns:
        A 200x300 pure white RGB image.

    """
    return Image.new("RGB", (200, 300), "white")


def _inked_page() -> Image.Image:
    """
    Return a page with a large black rectangle on it.

    Returns:
        A 200x300 RGB image no threshold pair calls blank.

    """
    page = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(page)
    draw.rectangle([20, 20, 180, 280], fill="black")
    return page


class TestEmptyPageDetection:
    """
    Tests for ``is_empty_page``'s dual-threshold rule over stored statistics.

    The rule is ``mean > mean_threshold and stddev < stddev_threshold``: an
    AND, with both comparisons strict. Neither the thresholds nor the
    strictness changed when the page stopped arriving here (D-06), so these
    cases assert exactly what they asserted when each one converted an image
    first.
    """

    def test_pure_white_is_empty(self) -> None:
        """A pure white page measures mean 255, stddev 0, and is empty."""
        assert is_empty_page(255.0, 0.0) is True

    def test_nearly_white_is_empty(self) -> None:
        """A (253,253,253) page measures mean 253, stddev 0, and is empty."""
        assert is_empty_page(253.0, 0.0) is True

    def test_dark_content_is_not_empty(self) -> None:
        """A low mean fails the first condition however flat the page is."""
        assert is_empty_page(120.0, 1.0) is False

    def test_text_like_content_not_empty(self) -> None:
        """Scattered ink raises the stddev, which fails the second condition."""
        assert is_empty_page(252.0, 40.0) is False

    def test_mean_exactly_at_the_threshold_is_not_empty(self) -> None:
        """The mean comparison is strictly greater-than, and stays so."""
        assert is_empty_page(250.0, 0.0) is False

    def test_stddev_exactly_at_the_threshold_is_not_empty(self) -> None:
        """The stddev comparison is strictly less-than, and stays so."""
        assert is_empty_page(255.0, 5.0) is False

    def test_both_conditions_are_required(self) -> None:
        """Bright-but-noisy and flat-but-dark are both kept: the rule is an AND."""
        assert is_empty_page(255.0, 6.0) is False
        assert is_empty_page(249.0, 0.0) is False

    def test_custom_thresholds_stricter(self) -> None:
        """A stricter mean threshold rejects a page the default calls blank."""
        # mean 253, stddev 0 -- what a (253,253,253) page measures.
        assert is_empty_page(253.0, 0.0) is True
        # Stricter mean threshold (254): now mean=253 is NOT above 254.
        assert is_empty_page(253.0, 0.0, mean_threshold=254.0) is False

    def test_custom_thresholds_looser(self) -> None:
        """A looser mean threshold accepts a lightly-shaded page as blank."""
        # mean 200, stddev 0 -- what a (200,200,200) page measures.
        assert is_empty_page(200.0, 0.0) is False
        assert is_empty_page(200.0, 0.0, mean_threshold=190.0) is True


class TestStatisticsComeFromASpooledPage:
    """
    The literals the rest of this file passes are what a real page measures.

    ``is_empty_page`` is only as honest as the two numbers handed to it, and
    those numbers are produced in exactly one place: ``SpooledPageSink.add``,
    while the page is still decoded (D-06). These cases keep that end of the
    contract under test, so the measurement and the judgement cannot drift
    apart unnoticed.
    """

    def test_a_blank_page_records_the_statistics_the_thresholds_expect(
        self, tmp_path: Path
    ) -> None:
        """A spooled white page really does record mean 255 and stddev 0."""
        (record,) = _spool(tmp_path, [_white_page()])

        assert record.mean == pytest.approx(255.0)
        assert record.stddev == pytest.approx(0.0)
        assert is_empty_page(record.mean, record.stddev) is True

    def test_an_inked_page_records_statistics_no_threshold_pair_calls_blank(
        self, tmp_path: Path
    ) -> None:
        """A spooled inked page records a low mean and a high stddev."""
        (record,) = _spool(tmp_path, [_inked_page()])

        assert record.mean < 250.0
        assert record.stddev > 5.0
        assert is_empty_page(record.mean, record.stddev) is False

    def test_the_record_measures_the_file_that_was_written(
        self, tmp_path: Path
    ) -> None:
        """The page behind the numbers exists on disk and is the one judged."""
        (record,) = _spool(tmp_path, [_white_page()])

        assert record.path.exists()
        assert record.sequence == 1
        assert record.size == (200, 300)


class TestFilterEmptyPages:
    """
    ``filter_empty_pages`` filters the record list, never the directory.

    A discarded page is simply not referenced by the result: nothing is
    unlinked, and the survivors keep both their relative order and the
    ``sequence`` numbers naming the sheets the device fed.
    """

    def test_filters_empty_from_mixed(self, tmp_path: Path) -> None:
        """A mixed run returns only the inked records, in document order."""
        records = _spool(
            tmp_path,
            [
                _inked_page(),
                _white_page(),
                _inked_page(),
                _white_page(),
                _inked_page(),
            ],
        )

        result = filter_empty_pages(records)

        assert result == [records[0], records[2], records[4]]
        assert [record.sequence for record in result] == [1, 3, 5]

    def test_all_empty_returns_empty_list(self, tmp_path: Path) -> None:
        """An all-blank run returns an empty list rather than raising."""
        records = _spool(
            tmp_path,
            [_white_page(), Image.new("RGB", (200, 300), (254, 254, 254))],
        )

        assert filter_empty_pages(records) == []

    def test_no_empty_returns_all(self, tmp_path: Path) -> None:
        """A run with nothing blank in it returns every record untouched."""
        records = _spool(tmp_path, [_inked_page(), _inked_page(), _inked_page()])

        assert filter_empty_pages(records) == records

    def test_nothing_is_unlinked_from_the_spool(self, tmp_path: Path) -> None:
        """Every spooled file survives, including the pages that were dropped."""
        records = _spool(tmp_path, [_inked_page(), _white_page()])

        filter_empty_pages(records)

        assert sorted(path.name for path in tmp_path.iterdir()) == [
            "a-0001.png",
            "a-0002.png",
        ]

    def test_profile_thresholds_reach_the_rule(self, tmp_path: Path) -> None:
        """A looser mean threshold drops a page the default keeps."""
        records = _spool(tmp_path, [Image.new("RGB", (200, 300), (200, 200, 200))])

        assert filter_empty_pages(records) == records
        assert filter_empty_pages(records, mean_threshold=190.0) == []


class TestThumbnailGeneration:
    """Tests for generate_thumbnail, which still takes a page image."""

    def test_the_thumbnail_carries_no_exif(self) -> None:
        """
        A source page with EXIF still yields a thumbnail JPEG with none.

        Asserted on the decoded JPEG, because that is what a browser shows: an
        orientation tag surviving into it would rotate the preview.
        """
        page = _inked_page()
        exif = Image.Exif()
        exif[_EXIF_ORIENTATION_TAG] = 6
        page.info["exif"] = exif.tobytes()

        raw = base64.b64decode(generate_thumbnail(page))

        with Image.open(io.BytesIO(raw)) as thumb:
            assert "exif" not in thumb.info
            assert dict(thumb.getexif()) == {}

    def test_returns_nonempty_base64(self) -> None:
        """generate_thumbnail returns a non-empty base64 string."""
        result = generate_thumbnail(_inked_page())
        assert isinstance(result, str)
        assert len(result) > 0

    def test_decodes_to_valid_jpeg(self) -> None:
        """Base64 output decodes to valid JPEG image data."""
        result = generate_thumbnail(_inked_page())
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


class TestLargeScanPixelLimit:
    """
    Pillow's pixel limit is relaxed once, at start-up, and nowhere else.

    A 1200 dpi A4 colour page is about 139M pixels, over Pillow's default of
    about 89.5M, and img2pdf re-opens every spooled page with ``Image.open``,
    where the decompression-bomb check applies.  Importing a module must not
    change a process-wide setting, so the relaxation is one call the entry
    point makes.
    """

    def test_allow_large_scans_raises_the_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The call sets the limit a high-dpi page fits under."""
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS)

        pages_module.allow_large_scans()

        assert Image.MAX_IMAGE_PIXELS == _LARGE_SCAN_PIXEL_LIMIT

    def test_importing_the_imaging_modules_leaves_the_limit_alone(self) -> None:
        """A fresh interpreter keeps Pillow's default after every import."""
        result = subprocess.run(
            ["/bin/sh", "-c", 'exec "$SANELESS_TEST_PYTHON" -c "$SANELESS_TEST_CODE"'],
            env={
                **os.environ,
                "SANELESS_TEST_PYTHON": sys.executable,
                "SANELESS_TEST_CODE": _IMPORT_CHECK,
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=_CHILD_IMPORT_SECONDS,
        )

        assert result.returncode == 0, result.stderr
        before, after = result.stdout.split()
        assert after == before
        assert int(after) != _LARGE_SCAN_PIXEL_LIMIT

    def test_main_relaxes_the_limit_before_running_the_cli(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The console entry point makes the call, and makes it first."""
        calls: list[str] = []
        monkeypatch.setattr(
            pages_module, "allow_large_scans", lambda: calls.append("allow")
        )
        monkeypatch.setattr(cli_module, "cli", lambda: calls.append("cli"))

        saneless.main()

        assert calls == ["allow", "cli"]


class TestThresholdsHaveOneSource:
    """The blank-page thresholds come from the profile, never from a default."""

    @pytest.mark.parametrize("function", [is_empty_page, filter_empty_pages])
    def test_thresholds_are_required_keywords(
        self, function: Callable[..., object]
    ) -> None:
        """Both thresholds are keyword-only and carry no default of their own."""
        parameters = inspect.signature(function).parameters

        for name in ("mean_threshold", "stddev_threshold"):
            assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
            assert parameters[name].default is inspect.Parameter.empty
