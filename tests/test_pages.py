"""
Tests for page processing utilities: blank-page detection and thumbnails.

The blank-page rule is split in two, and so are its tests.  ``measure_ink``
and ``is_blank`` are judged on the synthetic pages in ``tests.blank_fixtures``,
by verdict only: no test here compares a coverage or a paper-white level with
a literal.  ``filter_blank_pages`` takes the records the spool made, so its
tests run real pages through a real ``SpooledPageSink`` and judge the
measurements that sink stored, rather than numbers typed in by a test.
"""

from __future__ import annotations

import base64
import inspect
import io
import logging
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
    BlankFilterResult,
    InkMeasurement,
    filter_blank_pages,
    generate_thumbnail,
    is_blank,
    measure_ink,
)
from saneless.pipeline import _SPOOL_LABEL_A, _SPOOL_LABEL_B, _interleave_duplex
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

# The ink-coverage threshold, in percent of the inset, that a profile carries
# when it sets none.  Read from the profile model -- the one place it is
# defined -- so every verdict below is asserted at the value that ships.
THRESHOLD = ProfileConfig().empty_page_coverage_threshold

# A verdict, as the per-page log line names it.
_KEEP = "KEEP"
_REMOVE = "REMOVE"

# The logger the blank-page filter writes its per-page lines to.
_PAGES_LOGGER = "saneless.pages"

# How every per-page line of the blank-page filter begins.
_CHECK_PREFIX = "Blank-page check: "

# A threshold at the top of its range: every page light enough to be paper is
# at or below it, so the filter removes every one.
_REMOVE_EVERYTHING = 100.0

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


def _spool(
    directory: Path, pages: Sequence[Image.Image], label: str = _SPOOL_LABEL_A
) -> list[PageRecord]:
    """
    Spool pages through a real sink and hand back the records it made.

    A real ``SpooledPageSink`` rather than hand-built records: the point of
    every test that calls this is that the measurements on a record were made
    from an actual page written to an actual file, not typed in by a test.

    Args:
        directory: Where the pages land. Must already exist.
        pages: The pages to spool, in order.
        label: The pass the pages belong to, which prefixes their file names.

    Returns:
        One record per page, in the order they were added.

    """
    sink = SpooledPageSink(directory, label, _TEST_RESERVE_MB)
    return [sink.add(page, dpi=300) for page in pages]


def _is_blank(
    coverage: float, paper_white: int, *, threshold: float = THRESHOLD
) -> bool:
    """
    Judge a stored measurement under the profile's default threshold.

    Args:
        coverage: The page's ink coverage, in percent of the inset.
        paper_white: The page's paper-white level.
        threshold: A coverage threshold overriding the profile default.

    Returns:
        What ``is_blank`` decides.

    """
    return is_blank(coverage, paper_white, coverage_threshold=threshold)


def _drop_blank(
    records: Sequence[PageRecord], *, threshold: float = THRESHOLD
) -> BlankFilterResult:
    """
    Filter records under the profile's default threshold.

    Args:
        records: The records to filter, in document order.
        threshold: A coverage threshold overriding the profile default.

    Returns:
        What ``filter_blank_pages`` keeps, and the positions it removed.

    """
    return filter_blank_pages(records, coverage_threshold=threshold)


def _check_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """
    Return the blank-page filter's per-page log records, in the order written.

    Args:
        caplog: pytest's log capture, already recording at INFO.

    Returns:
        Every record from the pages logger that begins as a per-page line.

    """
    return [
        entry
        for entry in caplog.records
        if entry.name == _PAGES_LOGGER and entry.getMessage().startswith(_CHECK_PREFIX)
    ]


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
        A 200x300 RGB image no threshold below the top of the range calls blank.

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
# are removed.  The bright-paper cases are the pages the old mean/stddev rule
# deleted; on the tinted paper the rest sit on, that rule kept every blank.
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


class TestBlankVerdictOverStoredMeasurements:
    """
    ``is_blank`` over the two numbers a record carries, at the shipped threshold.

    The rule is ``paper_white >= PAPER_WHITE_FLOOR and coverage <=
    threshold``: an AND, with both comparisons inclusive.  A page is removed
    only when it is light enough to be paper and carries no more ink than the
    threshold allows.
    """

    def test_an_inkless_white_page_is_blank(self) -> None:
        """A white page with no ink pixel at all is removed."""
        assert _is_blank(0.0, 255) is True

    def test_an_inkless_tinted_page_is_blank(self) -> None:
        """Tinted paper with no ink is removed: the tint is the paper, not ink."""
        assert _is_blank(0.0, PAPER_WHITE_FLOOR + 1) is True

    def test_a_dark_sheet_is_kept_however_little_ink(self) -> None:
        """Paper darker than the floor is not blank-looking paper, so it stays."""
        assert _is_blank(0.0, PAPER_WHITE_FLOOR - 1) is False

    def test_ink_above_the_threshold_is_kept(self) -> None:
        """More ink than the threshold allows is content."""
        assert _is_blank(THRESHOLD * 2, 255) is False

    def test_coverage_exactly_at_the_threshold_is_blank(self) -> None:
        """The coverage comparison is at-or-below, so the threshold itself is blank."""
        assert _is_blank(THRESHOLD, 255) is True

    def test_paper_exactly_at_the_floor_is_paper(self) -> None:
        """The floor comparison is at-or-above, so the floor itself is paper."""
        assert _is_blank(0.0, PAPER_WHITE_FLOOR) is True

    def test_both_conditions_are_required(self) -> None:
        """Light-but-inked and dark-but-inkless are both kept: the rule is an AND."""
        assert _is_blank(THRESHOLD * 2, 255) is False
        assert _is_blank(0.0, PAPER_WHITE_FLOOR - 1) is False

    def test_a_stricter_threshold_keeps_more(self) -> None:
        """At zero only an inkless page is blank, so a trace of ink is kept."""
        trace = THRESHOLD / 2
        assert _is_blank(trace, 255) is True
        assert _is_blank(trace, 255, threshold=0.0) is False

    def test_a_looser_threshold_removes_more(self) -> None:
        """A higher threshold removes a page the default keeps."""
        assert _is_blank(0.5, 255) is False
        assert _is_blank(0.5, 255, threshold=1.0) is True


class TestMeasurementsComeFromASpooledPage:
    """
    The measurement a record carries is ``measure_ink``'s, made from the page.

    ``filter_blank_pages`` is only as honest as the two numbers on each
    record, and those numbers are produced in exactly one place:
    ``SpooledPageSink.add``, while the page is still decoded.  These cases
    keep that end of the contract under test, so the measurement and the
    judgement cannot drift apart unnoticed.
    """

    def test_a_blank_page_records_a_measurement_the_rule_calls_blank(
        self, tmp_path: Path
    ) -> None:
        """A spooled white page carries ``measure_ink``'s answer, which is blank."""
        page = _white_page()
        (record,) = _spool(tmp_path, [page])

        assert (record.ink_coverage, record.paper_white) == tuple(measure_ink(page))
        assert _is_blank(record.ink_coverage, record.paper_white) is True

    def test_an_inked_page_records_a_measurement_the_rule_keeps(
        self, tmp_path: Path
    ) -> None:
        """A spooled inked page carries ``measure_ink``'s answer, which is content."""
        page = _inked_page()
        (record,) = _spool(tmp_path, [page])

        assert (record.ink_coverage, record.paper_white) == tuple(measure_ink(page))
        assert _is_blank(record.ink_coverage, record.paper_white) is False

    def test_the_record_measures_the_file_that_was_written(
        self, tmp_path: Path
    ) -> None:
        """The page behind the numbers exists on disk and is the one judged."""
        (record,) = _spool(tmp_path, [_white_page()])

        assert record.path.exists()
        assert record.sequence == 1
        assert record.size == (200, 300)


class TestFilterBlankPages:
    """
    ``filter_blank_pages`` filters the record list, never the directory.

    A removed page is simply not referenced by the result: nothing is
    unlinked, and the survivors keep both their relative order and the
    ``sequence`` numbers naming the sheets the device fed.  What it removed
    is reported as 1-based positions in the document it was given, which is
    the numbering the operator reads on every surface.
    """

    def test_filters_blank_from_mixed(self, tmp_path: Path) -> None:
        """A mixed run keeps the inked records and names the blanks' positions."""
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

        assert result.kept == [records[0], records[2], records[4]]
        assert [record.sequence for record in result.kept] == [1, 3, 5]
        assert result.removed_positions == (2, 4)

    def test_all_blank_keeps_nothing_and_names_every_position(
        self, tmp_path: Path
    ) -> None:
        """An all-blank run returns no records rather than raising."""
        records = _spool(
            tmp_path,
            [_white_page(), Image.new("RGB", (200, 300), (254, 254, 254))],
        )

        result = _drop_blank(records)

        assert result.kept == []
        assert result.removed_positions == (1, 2)

    def test_no_blank_returns_all(self, tmp_path: Path) -> None:
        """A run with nothing blank in it returns every record and no positions."""
        records = _spool(tmp_path, [_inked_page(), _inked_page(), _inked_page()])

        result = _drop_blank(records)

        assert result.kept == records
        assert result.removed_positions == ()

    def test_nothing_is_unlinked_from_the_spool(self, tmp_path: Path) -> None:
        """Every spooled file survives, including the pages that were removed."""
        records = _spool(tmp_path, [_inked_page(), _white_page()])

        _drop_blank(records)

        assert sorted(path.name for path in tmp_path.iterdir()) == [
            "a-0001.png",
            "a-0002.png",
        ]

    def test_the_profile_threshold_reaches_the_rule(self, tmp_path: Path) -> None:
        """A threshold at the top of the range removes a page the default keeps."""
        records = _spool(tmp_path, [_inked_page()])

        assert _drop_blank(records).kept == records
        assert _drop_blank(records, threshold=_REMOVE_EVERYTHING).kept == []

    def test_positions_are_places_in_the_document_not_sequences(
        self, tmp_path: Path
    ) -> None:
        """
        A duplex blank is named by its interleaved position, not its sheet number.

        Pass B runs over the flipped stack, so its first page is the last
        sheet's back.  Here that page is blank: its own ``sequence`` is 1, but
        it is the back of sheet 2, which is page 4 of the document -- and page
        4 is the number the operator has to rescan by.
        """
        fronts = _spool(tmp_path, [_inked_page(), _inked_page()])
        backs = _spool(tmp_path, [_white_page(), _inked_page()], _SPOOL_LABEL_B)
        document = _interleave_duplex(fronts, backs)

        result = _drop_blank(document)

        assert document[3].sequence == 1
        assert result.removed_positions == (4,)
        assert result.kept == [document[0], document[1], document[2]]

    def test_the_result_is_a_named_pair(self, tmp_path: Path) -> None:
        """The result unpacks as ``(kept, removed_positions)``."""
        records = _spool(tmp_path, [_inked_page(), _white_page()])

        result = _drop_blank(records)

        assert isinstance(result, BlankFilterResult)
        kept, removed_positions = result
        assert kept == result.kept
        assert removed_positions == result.removed_positions

    def test_one_log_line_per_page_names_its_position_and_measurement(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Every page gets one INFO line: where it sits, what it measured, the verdict.

        The coverage is written at full precision, so a page removed at
        0.0009 % cannot read as 0.0 % in the log; the threshold is written
        beside it, so the line alone says why.
        """
        records = _spool(
            tmp_path, [_inked_page(), _white_page(), _inked_page(), _white_page()]
        )
        caplog.set_level(logging.INFO, logger=_PAGES_LOGGER)

        _drop_blank(records)

        lines = _check_lines(caplog)
        assert len(lines) == len(records)
        for position, (record, line) in enumerate(
            zip(records, lines, strict=True), start=1
        ):
            message = line.getMessage()
            assert line.levelno == logging.INFO
            assert f"page {position} of {len(records)}" in message
            assert record.path.name in message
            assert repr(record.ink_coverage) in message
            assert f"paper white {record.paper_white}" in message
            assert f"threshold {THRESHOLD!r}%" in message
            expected = _REMOVE if position % 2 == 0 else _KEEP
            assert message.endswith(f"-> {expected}")


class TestTheShippedDefault:
    """
    The review's pages, spooled for real and judged at ``ProfileConfig()``.

    The page number and the pencil note are the pages the old rule deleted;
    the typed line is the control; the framed blank is the blank a
    keep-when-unsure default must still remove.
    """

    def test_fixtures_at_the_profile_default(self, tmp_path: Path) -> None:
        """Footer number, pencil and typed line are kept; the framed blank goes."""
        pages = [
            footer_page_number(12.5),
            framed_blank(3),
            pencil_lines(),
            typed_line(),
        ]
        records = _spool(tmp_path, pages)

        result = filter_blank_pages(
            records, coverage_threshold=ProfileConfig().empty_page_coverage_threshold
        )

        assert result.kept == [records[0], records[2], records[3]]
        assert result.removed_positions == (2,)


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
    """The blank-page threshold comes from the profile, never from a default."""

    @pytest.mark.parametrize("function", [is_blank, filter_blank_pages])
    def test_the_threshold_is_a_required_keyword(
        self, function: Callable[..., object]
    ) -> None:
        """The coverage threshold is keyword-only and carries no default."""
        parameter = inspect.signature(function).parameters["coverage_threshold"]

        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty

    def test_the_old_rule_is_gone(self) -> None:
        """Nothing is left of the mean/stddev rule for a caller to reach by mistake."""
        assert not hasattr(pages_module, "is_empty_page")
        assert not hasattr(pages_module, "filter_empty_pages")


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
