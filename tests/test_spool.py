"""Tests for the page-record contract and the page spool (HARD-01, M-08)."""

from __future__ import annotations

import base64
import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from PIL import Image, ImageDraw
from PIL.ImageStat import Stat

from saneless.exceptions import ScanError
from saneless.scanner.base import PageRecord, PageSink
from saneless.spool import SpooledPageSink

if TYPE_CHECKING:
    from collections.abc import Callable

# Larger than any disk this suite will ever run on, so the per-page check is
# guaranteed to report a shortfall without monkeypatching shutil.
_IMPOSSIBLE_RESERVE_MB = 1_000_000_000


def _white_page(size: tuple[int, int] = (200, 300)) -> Image.Image:
    """Return a pure-white page: greyscale mean 255.0, stddev 0.0."""
    return Image.new("RGB", size, "white")


def _inked_page(size: tuple[int, int] = (200, 300)) -> Image.Image:
    """Return a page with dark content, so its stddev is far from zero."""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, size[0] - 10, size[1] - 10), fill="black")
    return image


class TestPageRecordContract:
    """The frozen record of facts a spooled page yields (D-02)."""

    def test_fields_are_exactly_the_contract(self) -> None:
        """PageRecord carries the six agreed fields, in the agreed order."""
        names = [field.name for field in dataclasses.fields(PageRecord)]
        assert names == ["sequence", "path", "size", "mode", "mean", "stddev"]

    def test_carries_no_verdict_field(self) -> None:
        """No is_blank/is_empty flag: verdicts belong to the pipeline (D-02)."""
        names = {field.name for field in dataclasses.fields(PageRecord)}
        assert "is_blank" not in names
        assert "is_empty" not in names

    def test_is_frozen(self) -> None:
        """A record of what already happened cannot be rewritten afterwards."""
        record = PageRecord(
            sequence=1,
            path=Path("a-0001.png"),
            size=(200, 300),
            mode="RGB",
            mean=255.0,
            stddev=0.0,
        )
        # Through setattr with the name in a variable, because a plain
        # ``record.sequence = 2`` is a static error both type checkers report,
        # and the point of the test is the runtime refusal.
        attribute = "sequence"
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(record, attribute, 2)

    def test_sequence_is_one_based(self) -> None:
        """The first page's sequence is 1, not 0 (D-02)."""
        record = PageRecord(
            sequence=1,
            path=Path("a-0001.png"),
            size=(200, 300),
            mode="RGB",
            mean=255.0,
            stddev=0.0,
        )
        assert record.sequence == 1


class TestPageSinkContract:
    """The seam the backend hands each acquired page to (D-01)."""

    def test_is_abstract(self) -> None:
        """PageSink cannot be instantiated: it declares a contract only."""
        # Bound as a zero-argument callable, because both type checkers
        # correctly refuse a direct call on an abstract class. Being refused is
        # exactly what this test asserts the interpreter does at runtime, so
        # the cast states the shape the call site claims and the raises block
        # below is the proof of what actually happens.
        sink_type = cast("Callable[[], object]", PageSink)
        with pytest.raises(TypeError):
            sink_type()

    def test_subclass_implementing_add_is_concrete(self) -> None:
        """A subclass that implements add() can be instantiated and used."""

        class _RecordingSink(PageSink):
            """A sink that records nothing to disk, for the contract test."""

            def add(self, image: Image.Image) -> PageRecord:
                """Return a record describing ``image`` without writing it."""
                return PageRecord(
                    sequence=1,
                    path=Path("a-0001.png"),
                    size=image.size,
                    mode=image.mode,
                    mean=255.0,
                    stddev=0.0,
                )

        sink = _RecordingSink()
        record = sink.add(Image.new("RGB", (200, 300), "white"))
        assert record.size == (200, 300)
        assert record.mode == "RGB"


class TestSpooledPageSinkNaming:
    """Sequence assignment and pass-distinguishable file names (D-02)."""

    def test_first_page_is_sequence_one(self, tmp_path: Path) -> None:
        """The first add() assigns sequence 1 and writes a-0001.png."""
        sink = SpooledPageSink(tmp_path, "a", 10)
        record = sink.add(_white_page())
        assert record.sequence == 1
        assert record.path == tmp_path / "a-0001.png"
        assert record.path.is_file()

    def test_second_page_increments(self, tmp_path: Path) -> None:
        """The second add() assigns sequence 2 and writes a-0002.png."""
        sink = SpooledPageSink(tmp_path, "a", 10)
        sink.add(_white_page())
        second = sink.add(_inked_page())
        assert second.sequence == 2
        assert second.path == tmp_path / "a-0002.png"
        assert second.path.is_file()

    def test_records_are_in_acquisition_order(self, tmp_path: Path) -> None:
        """The records property returns every record so far, in order."""
        sink = SpooledPageSink(tmp_path, "a", 10)
        assert sink.records == ()
        first = sink.add(_white_page())
        second = sink.add(_inked_page())
        assert sink.records == (first, second)
        assert isinstance(sink.records, tuple)

    def test_pass_label_distinguishes_the_two_duplex_passes(
        self, tmp_path: Path
    ) -> None:
        """A pass-B sink writes b-0001.png beside pass A's a-0001.png."""
        pass_a = SpooledPageSink(tmp_path, "a", 10)
        pass_b = SpooledPageSink(tmp_path, "b", 10)
        pass_a.add(_white_page())
        record = pass_b.add(_inked_page())
        assert record.path == tmp_path / "b-0001.png"
        assert sorted(path.name for path in tmp_path.iterdir()) == [
            "a-0001.png",
            "b-0001.png",
        ]


class TestSpooledPageSinkWrite:
    """What lands on disk, and what the record says about it."""

    def test_written_file_is_a_png_that_round_trips(self, tmp_path: Path) -> None:
        """The spooled file reopens as a PNG at the same size and mode."""
        sink = SpooledPageSink(tmp_path, "a", 10)
        image = _inked_page((150, 220))
        record = sink.add(image)
        with Image.open(record.path) as reopened:
            assert reopened.format == "PNG"
            assert reopened.size == image.size
            assert reopened.mode == image.mode

    def test_record_size_and_mode_match_the_image(self, tmp_path: Path) -> None:
        """The record's size and mode are the image's own, not the file's."""
        sink = SpooledPageSink(tmp_path, "a", 10)
        image = _inked_page((150, 220))
        record = sink.add(image)
        assert record.size == (150, 220)
        assert record.mode == "RGB"


class TestSpooledPageSinkStatistics:
    """Greyscale statistics, measured once at spool time (D-02, D-06)."""

    def test_white_page_statistics(self, tmp_path: Path) -> None:
        """A pure-white page has mean 255.0 and stddev 0.0."""
        sink = SpooledPageSink(tmp_path, "a", 10)
        record = sink.add(_white_page())
        assert record.mean == 255.0
        assert record.stddev == 0.0

    def test_inked_page_statistics_match_pillow(self, tmp_path: Path) -> None:
        """mean/stddev equal Stat(image.convert("L")) for an inked page."""
        image = _inked_page()
        expected = Stat(image.convert("L"))
        sink = SpooledPageSink(tmp_path, "a", 10)
        record = sink.add(image)
        assert record.mean == pytest.approx(expected.mean[0])
        assert record.stddev == pytest.approx(expected.stddev[0])
        assert record.stddev > 0.0


class TestSpooledPageSinkThumbnail:
    """The first page's thumbnail fires at spool time, once (D-05)."""

    def test_fires_once_on_the_first_page_only(self, tmp_path: Path) -> None:
        """Three pages produce exactly one thumbnail, from page 1."""
        thumbnails: list[str] = []
        sink = SpooledPageSink(tmp_path, "a", 10, thumbnails.append)
        sink.add(_inked_page())
        assert len(thumbnails) == 1
        sink.add(_inked_page())
        sink.add(_inked_page())
        assert len(thumbnails) == 1

    def test_thumbnail_is_non_empty_ascii_base64(self, tmp_path: Path) -> None:
        """The callback receives a decodable base64 ASCII JPEG string."""
        thumbnails: list[str] = []
        sink = SpooledPageSink(tmp_path, "a", 10, thumbnails.append)
        sink.add(_inked_page())
        encoded = thumbnails[0]
        assert encoded
        assert encoded.isascii()
        assert base64.b64decode(encoded)

    def test_no_callback_never_raises(self, tmp_path: Path) -> None:
        """A sink built without a callback spools pages normally."""
        sink = SpooledPageSink(tmp_path, "a", 10)
        sink.add(_inked_page())
        sink.add(_inked_page())
        assert len(sink.records) == 2

    def test_a_raising_thumbnail_callback_still_records_the_page(
        self, tmp_path: Path
    ) -> None:
        """
        A failed thumbnail cannot unsee a page that is already on disk (WR-01).

        The callback is deliberately not wrapped -- a blank thumbnail strip is
        a silence this project does not want -- but the record has to be
        appended before it fires.  The web worker's callback writes to the job
        store, which raises ``sqlite3.Error`` on a locked or closed database,
        and the page it was describing is a sheet that really was fed.  With
        the record appended afterwards, ``page_count()`` answered 0 and the
        partial-scan guard took its "nothing reached the spool" branch, so the
        workspace deleted the sheet on its way out.

        The assertion on the file, not only on the record, is the part that
        matters: what is being pinned is that the two agree.
        """
        thumbnails: list[str] = []

        def explode(thumbnail: str) -> None:
            thumbnails.append(thumbnail)
            msg = "the job store was closed"
            raise OSError(msg)

        sink = SpooledPageSink(tmp_path, "a", 10, explode)

        with pytest.raises(OSError, match="the job store was closed"):
            sink.add(_inked_page())

        assert len(thumbnails) == 1
        assert len(sink.records) == 1
        assert sink.records[0].sequence == 1
        assert sink.records[0].path == tmp_path / "a-0001.png"
        assert sink.records[0].path.stat().st_size > 0


class TestSpooledPageSinkFailures:
    """No raw OSError escapes, and a shortfall names the page (D-07)."""

    def test_disk_shortfall_raises_scan_error_naming_the_page(
        self, tmp_path: Path
    ) -> None:
        """A per-page shortfall names the page, the path and the config key."""
        sink = SpooledPageSink(tmp_path, "a", _IMPOSSIBLE_RESERVE_MB)
        with pytest.raises(ScanError) as excinfo:
            sink.add(_white_page())
        message = str(excinfo.value)
        assert "page 1" in message
        assert str(tmp_path / "a-0001.png") in message
        assert "configure min_free_space_mb to adjust" in message

    def test_disk_shortfall_leaves_no_partial_file(self, tmp_path: Path) -> None:
        """Nothing is written when the page could not have fitted."""
        sink = SpooledPageSink(tmp_path, "a", _IMPOSSIBLE_RESERVE_MB)
        with pytest.raises(ScanError):
            sink.add(_white_page())
        assert list(tmp_path.iterdir()) == []
        assert sink.records == ()

    def test_write_oserror_becomes_a_chained_scan_error(self, tmp_path: Path) -> None:
        """An OSError from the write surfaces as ScanError, chained with from."""
        # A regular file where the spool directory should be: the free-space
        # check still succeeds on it, and the save raises NotADirectoryError.
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory")
        sink = SpooledPageSink(blocked, "a", 0)
        with pytest.raises(ScanError) as excinfo:
            sink.add(_white_page())
        message = str(excinfo.value)
        assert "page 1" in message
        assert str(blocked / "a-0001.png") in message
        assert isinstance(excinfo.value.__cause__, OSError)
        assert sink.records == ()

    def test_an_unmeasurable_directory_becomes_a_chained_scan_error(
        self, tmp_path: Path
    ) -> None:
        """
        A spool directory that has gone is a disk fault, not a scanner one.

        ``add``'s docstring promises "no raw OSError escapes this method"
        (D-07), and ``_write`` honoured it while ``_check_room_for`` did not:
        ``shutil.disk_usage`` on a directory whose mount went away raises
        ``FileNotFoundError``.  Untranslated it escaped past
        ``_acquire_pages``' ``except ScanError`` ladder into its generic
        handler, where it came back as "Scanner error on page N" -- blaming
        the scanner for a disk fault -- and on the flatbed path it escaped
        untranslated altogether (WR-11).

        The directory is removed after the sink is built, so the failure is
        the one production sees: a spool that existed when the scan started
        and did not when the page arrived.
        """
        spool = tmp_path / "spool"
        spool.mkdir()
        sink = SpooledPageSink(spool, "a", 0)
        spool.rmdir()

        with pytest.raises(ScanError) as excinfo:
            sink.add(_white_page())

        message = str(excinfo.value)
        assert "Could not measure free space for page 1" in message
        assert str(spool) in message
        assert isinstance(excinfo.value.__cause__, OSError)
        assert sink.records == ()
