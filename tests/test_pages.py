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

The ink-coverage rule that replaces it -- ``measure_ink`` and ``is_blank`` --
is judged on the synthetic pages in ``tests.blank_fixtures``, by verdict only:
no test here compares a coverage or a paper-white level with a literal.
"""

from __future__ import annotations

import base64
import inspect
import io
import os
import re
import subprocess
import sys
from functools import partial
from typing import TYPE_CHECKING

import pytest
from PIL import Image, ImageDraw

import saneless
import saneless.cli as cli_module
import saneless.pages as pages_module
from saneless.config import ProfileConfig
from saneless.pages import (
    EDGE_TRIM,
    INK_DELTA,
    PAPER_PERCENTILE,
    PAPER_WHITE_FLOOR,
    InkMeasurement,
    filter_empty_pages,
    generate_thumbnail,
    is_blank,
    is_empty_page,
    measure_ink,
)
from saneless.pipeline import _SPOOL_LABEL_A
from saneless.spool import SpooledPageSink
from tests.blank_fixtures import (
    dusty_blank,
    footer_page_number,
    framed_blank,
    highlighter_stroke,
    lone_digit,
    pencil_lines,
    reverse_text_cover,
    tinted_blank,
    typed_line,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from saneless.scanner.base import PageRecord

# The reserve the spool keeps free beyond each page. One megabyte is the
# smallest honest value: these pages are a few tens of kilobytes, so the check
# passes anywhere the test suite can run at all, and it is still a real check
# rather than a disabled one.
_TEST_RESERVE_MB = 1

# The blank-page thresholds a profile carries when it sets none, read from the
# profile model -- the one place they are defined -- rather than retyped here.
_PROFILE_DEFAULTS = ProfileConfig()
_MEAN_THRESHOLD = _PROFILE_DEFAULTS.empty_page_mean_threshold
_STDDEV_THRESHOLD = _PROFILE_DEFAULTS.empty_page_stddev_threshold

# The ink-coverage threshold, in percent of the inset, the verdict tests judge
# at: the value the profile model will ship as its default once the coverage
# rule replaces the mean/stddev one, where these verdicts are asserted again
# against the real default.
THRESHOLD = 0.001

# A verdict, as the per-page log line names it.
_KEEP = "KEEP"
_REMOVE = "REMOVE"

# The pixel size of an A4 page scanned at 300 dpi.
_A4_300DPI = (2480, 3508)

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


def _is_blank(
    mean: float, stddev: float, *, mean_threshold: float = _MEAN_THRESHOLD
) -> bool:
    """
    Judge a page's statistics under the profile's default thresholds.

    Args:
        mean: Greyscale mean luminance.
        stddev: Greyscale standard deviation.
        mean_threshold: A mean threshold overriding the profile default.

    Returns:
        What ``is_empty_page`` decides.

    """
    return is_empty_page(
        mean,
        stddev,
        mean_threshold=mean_threshold,
        stddev_threshold=_STDDEV_THRESHOLD,
    )


def _drop_blank(
    records: Sequence[PageRecord], *, mean_threshold: float = _MEAN_THRESHOLD
) -> list[PageRecord]:
    """
    Filter records under the profile's default thresholds.

    Args:
        records: The records to filter.
        mean_threshold: A mean threshold overriding the profile default.

    Returns:
        What ``filter_empty_pages`` keeps.

    """
    return filter_empty_pages(
        records, mean_threshold=mean_threshold, stddev_threshold=_STDDEV_THRESHOLD
    )


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


def _white_a4_page() -> Image.Image:
    """
    Return an A4 300 dpi page with no noise and no ink at all.

    Returns:
        A pure white ``L`` image the size of a real scan.

    """
    return Image.new("L", _A4_300DPI, 255)


def _verdict(image: Image.Image, *, threshold: float = THRESHOLD) -> str:
    """
    Measure a page's ink and judge it at a coverage threshold.

    Args:
        image: The page to judge.
        threshold: The coverage threshold, in percent of the inset.

    Returns:
        ``"REMOVE"`` when the rule calls the page blank, else ``"KEEP"``.

    """
    measurement = measure_ink(image)
    blank = is_blank(
        measurement.coverage,
        measurement.paper_white,
        coverage_threshold=threshold,
    )
    return _REMOVE if blank else _KEEP


# Every synthetic page and the verdict the review expects at ``THRESHOLD``:
# sparse or faint content is kept, and blanks -- tinted, dusty or framed --
# are removed.  The bright-paper cases are the pages today's mean/stddev rule
# deletes; on the tinted paper the rest sit on, that rule keeps every blank.
_FIXTURE_VERDICTS: list[tuple[str, Callable[[], Image.Image], str]] = [
    ("footer-10mm", partial(footer_page_number, 10), _KEEP),
    ("footer-12.5mm", partial(footer_page_number, 12.5), _KEEP),
    ("footer-15mm", partial(footer_page_number, 15), _KEEP),
    ("footer-21mm", partial(footer_page_number, 21), _KEEP),
    ("footer-bright-paper", partial(footer_page_number, tint=255), _KEEP),
    ("lone-digit", lone_digit, _KEEP),
    ("pencil-lines", pencil_lines, _KEEP),
    ("pencil-bright-paper", partial(pencil_lines, tint=255), _KEEP),
    ("typed-line", typed_line, _KEEP),
    ("highlighter", highlighter_stroke, _KEEP),
    ("reverse-text-cover", reverse_text_cover, _KEEP),
    ("tinted-noise-3", partial(tinted_blank, noise=3), _REMOVE),
    ("tinted-noise-6", partial(tinted_blank, noise=6), _REMOVE),
    ("dusty-5-specks", partial(dusty_blank, 5), _REMOVE),
    ("frame-2mm", partial(framed_blank, 2), _REMOVE),
    ("frame-3mm", partial(framed_blank, 3), _REMOVE),
    ("frame-4mm", partial(framed_blank, 4), _REMOVE),
]


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
        assert _is_blank(255.0, 0.0) is True

    def test_nearly_white_is_empty(self) -> None:
        """A (253,253,253) page measures mean 253, stddev 0, and is empty."""
        assert _is_blank(253.0, 0.0) is True

    def test_dark_content_is_not_empty(self) -> None:
        """A low mean fails the first condition however flat the page is."""
        assert _is_blank(120.0, 1.0) is False

    def test_text_like_content_not_empty(self) -> None:
        """Scattered ink raises the stddev, which fails the second condition."""
        assert _is_blank(252.0, 40.0) is False

    def test_mean_exactly_at_the_threshold_is_not_empty(self) -> None:
        """The mean comparison is strictly greater-than, and stays so."""
        assert _is_blank(250.0, 0.0) is False

    def test_stddev_exactly_at_the_threshold_is_not_empty(self) -> None:
        """The stddev comparison is strictly less-than, and stays so."""
        assert _is_blank(255.0, 5.0) is False

    def test_both_conditions_are_required(self) -> None:
        """Bright-but-noisy and flat-but-dark are both kept: the rule is an AND."""
        assert _is_blank(255.0, 6.0) is False
        assert _is_blank(249.0, 0.0) is False

    def test_custom_thresholds_stricter(self) -> None:
        """A stricter mean threshold rejects a page the default calls blank."""
        # mean 253, stddev 0 -- what a (253,253,253) page measures.
        assert _is_blank(253.0, 0.0) is True
        # Stricter mean threshold (254): now mean=253 is NOT above 254.
        assert _is_blank(253.0, 0.0, mean_threshold=254.0) is False

    def test_custom_thresholds_looser(self) -> None:
        """A looser mean threshold accepts a lightly-shaded page as blank."""
        # mean 200, stddev 0 -- what a (200,200,200) page measures.
        assert _is_blank(200.0, 0.0) is False
        assert _is_blank(200.0, 0.0, mean_threshold=190.0) is True


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
        assert _is_blank(record.mean, record.stddev) is True

    def test_an_inked_page_records_statistics_no_threshold_pair_calls_blank(
        self, tmp_path: Path
    ) -> None:
        """A spooled inked page records a low mean and a high stddev."""
        (record,) = _spool(tmp_path, [_inked_page()])

        assert record.mean < 250.0
        assert record.stddev > 5.0
        assert _is_blank(record.mean, record.stddev) is False

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

        result = _drop_blank(records)

        assert result == [records[0], records[2], records[4]]
        assert [record.sequence for record in result] == [1, 3, 5]

    def test_all_empty_returns_empty_list(self, tmp_path: Path) -> None:
        """An all-blank run returns an empty list rather than raising."""
        records = _spool(
            tmp_path,
            [_white_page(), Image.new("RGB", (200, 300), (254, 254, 254))],
        )

        assert _drop_blank(records) == []

    def test_no_empty_returns_all(self, tmp_path: Path) -> None:
        """A run with nothing blank in it returns every record untouched."""
        records = _spool(tmp_path, [_inked_page(), _inked_page(), _inked_page()])

        assert _drop_blank(records) == records

    def test_nothing_is_unlinked_from_the_spool(self, tmp_path: Path) -> None:
        """Every spooled file survives, including the pages that were dropped."""
        records = _spool(tmp_path, [_inked_page(), _white_page()])

        _drop_blank(records)

        assert sorted(path.name for path in tmp_path.iterdir()) == [
            "a-0001.png",
            "a-0002.png",
        ]

    def test_profile_thresholds_reach_the_rule(self, tmp_path: Path) -> None:
        """A looser mean threshold drops a page the default keeps."""
        records = _spool(tmp_path, [Image.new("RGB", (200, 300), (200, 200, 200))])

        assert _drop_blank(records) == records
        assert _drop_blank(records, mean_threshold=190.0) == []


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


class TestInkCoverage:
    """
    ``measure_ink`` and ``is_blank`` judge pages by the presence of ink.

    A page is blank when the share of its inset darker than the paper by more
    than a fixed margin is at or below the threshold, and its paper is light
    enough to be paper at all.  Only verdicts are asserted: the measurement's
    numbers are not pinned to literals.
    """

    @pytest.mark.parametrize(
        ("build", "expected"),
        [
            pytest.param(build, expected, id=name)
            for name, build, expected in _FIXTURE_VERDICTS
        ],
    )
    def test_fixture_verdicts_at_the_shipped_threshold(
        self, build: Callable[[], Image.Image], expected: str
    ) -> None:
        """Sparse and faint content is kept; tinted, dusty and framed blanks go."""
        assert _verdict(build()) == expected

    def test_the_existing_content_fixture_is_kept(
        self, content_page_image: Image.Image
    ) -> None:
        """The shared inked fixture stays content under the new rule."""
        assert _verdict(content_page_image) == _KEEP

    def test_the_existing_near_white_fixture_is_removed(
        self, empty_page_image: Image.Image
    ) -> None:
        """The shared near-white fixture stays blank under the new rule."""
        assert _verdict(empty_page_image) == _REMOVE

    def test_every_coloured_adf_page_is_kept(
        self, multi_page_images: list[Image.Image]
    ) -> None:
        """
        The solid red, blue, green and yellow pages are kept; the white one goes.

        Yellow is the case a luminance-only measurement gets wrong: its darkest
        channel is what shows it is not paper.
        """
        white, *coloured = multi_page_images

        assert _verdict(white) == _REMOVE
        assert [_verdict(page) for page in coloured] == [_KEEP] * len(coloured)

    def test_threshold_zero_removes_only_inkless_pages(self) -> None:
        """At a threshold of zero a noise-only blank goes and one digit stays."""
        assert _verdict(tinted_blank(noise=3), threshold=0.0) == _REMOVE
        assert _verdict(lone_digit(), threshold=0.0) == _KEEP

    @pytest.mark.parametrize(
        ("build", "expected"),
        [
            pytest.param(typed_line, _KEEP, id="typed-line"),
            pytest.param(_white_a4_page, _REMOVE, id="white-page"),
        ],
    )
    def test_measure_ink_accepts_one_bit_greyscale_and_rgb(
        self, build: Callable[[], Image.Image], expected: str
    ) -> None:
        """
        A page gets the same verdict in each mode the spool can hand over.

        The one-bit version is thresholded without dithering, because the
        default dither turns a tinted page into scattered black dots.
        """
        greyscale = build()
        pages = {
            "1": greyscale.convert("1", dither=Image.Dither.NONE),
            "L": greyscale,
            "RGB": greyscale.convert("RGB"),
        }

        verdicts = {mode: _verdict(page) for mode, page in pages.items()}

        assert verdicts == dict.fromkeys(pages, expected)

    @pytest.mark.parametrize("mode", ["RGBA", "CMYK", "I;16", "P"])
    def test_measure_ink_refuses_other_modes(self, mode: str) -> None:
        """Any mode but 1, L and RGB is refused by name rather than guessed at."""
        with pytest.raises(ValueError, match=re.escape(mode)):
            measure_ink(Image.new(mode, (200, 300)))

    def test_measure_ink_names_its_two_fields(self) -> None:
        """The result is an ``InkMeasurement`` of coverage and paper white."""
        measurement = measure_ink(typed_line())

        assert isinstance(measurement, InkMeasurement)
        assert measurement == (measurement.coverage, measurement.paper_white)

    def test_paper_white_floor_keeps_a_dark_page(self) -> None:
        """Paper darker than the floor is kept at any threshold."""
        assert is_blank(0.0, PAPER_WHITE_FLOOR - 1, coverage_threshold=100.0) is False

    def test_blank_means_at_or_below_the_threshold(self) -> None:
        """Coverage equal to the threshold is blank; any more is content."""
        assert is_blank(THRESHOLD, PAPER_WHITE_FLOOR, coverage_threshold=THRESHOLD)
        assert not is_blank(
            THRESHOLD * 2, PAPER_WHITE_FLOOR, coverage_threshold=THRESHOLD
        )

    def test_constants_are_module_constants(self) -> None:
        """The trim, percentile, delta and floor are design values, not knobs."""
        assert EDGE_TRIM == 0.03
        assert INK_DELTA == 40
        assert PAPER_PERCENTILE == 0.99
        assert PAPER_WHITE_FLOOR == 128

    def test_the_threshold_is_a_required_keyword(self) -> None:
        """``is_blank`` takes its threshold from the caller, never a default."""
        parameter = inspect.signature(is_blank).parameters["coverage_threshold"]

        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
