"""Tests for the page-record contract and the page spool (HARD-01, M-08)."""

from __future__ import annotations

import base64
import dataclasses
import logging
from pathlib import Path
from types import SimpleNamespace
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
        """PageRecord carries the seven agreed fields, in the agreed order."""
        names = [field.name for field in dataclasses.fields(PageRecord)]
        assert names == ["sequence", "path", "size", "mode", "dpi", "mean", "stddev"]

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
            dpi=300,
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
            dpi=300,
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

            def add(self, image: Image.Image, *, dpi: int) -> PageRecord:
                """Return a record describing ``image`` without writing it."""
                return PageRecord(
                    sequence=1,
                    path=Path("a-0001.png"),
                    size=image.size,
                    mode=image.mode,
                    dpi=dpi,
                    mean=255.0,
                    stddev=0.0,
                )

        sink = _RecordingSink()
        record = sink.add(Image.new("RGB", (200, 300), "white"), dpi=300)
        assert record.size == (200, 300)
        assert record.mode == "RGB"
        assert record.dpi == 300


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
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        A failed thumbnail is logged, and the page and the pass carry on (WR-01).

        The web worker's callback writes to the job store, which raises
        ``sqlite3.Error`` on a locked or closed database; the page it was
        describing is a sheet that really was fed.  The thumbnail is something
        to look at, not part of the scan, so its failure is logged at WARNING
        with the traceback and ``add`` returns the page's record as usual.

        This test used to pin the opposite: the callback went unguarded, and
        the only protection was that the record was appended before it fired.
        The callback is now best-effort, and the record still comes first.
        The assertion on the file, not only on the record, is the part that
        matters: what is being pinned is that the two agree.
        """
        thumbnails: list[str] = []

        def explode(thumbnail: str) -> None:
            thumbnails.append(thumbnail)
            msg = "the job store was closed"
            raise OSError(msg)

        sink = SpooledPageSink(tmp_path, "a", 10, explode)

        with caplog.at_level(logging.WARNING, logger="saneless.spool"):
            record = sink.add(_inked_page())
            sink.add(_inked_page())

        assert len(thumbnails) == 1
        assert len(sink.records) == 2
        assert record == sink.records[0]
        assert record.sequence == 1
        assert record.path == tmp_path / "a-0001.png"
        assert record.path.stat().st_size > 0
        warnings = [
            entry
            for entry in caplog.records
            if entry.name == "saneless.spool" and entry.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert warnings[0].exc_info is not None
        assert "the job store was closed" in str(warnings[0].exc_info[1])


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


# The modes a SANE snap produces, spooled exactly as they arrive.
_KEPT_MODES = ["1", "L", "RGB"]

# Modes the thumbnail or the PNG write mishandle, and what each becomes.
_CONVERTED_MODES = [
    ("LA", "L"),
    ("RGBA", "RGB"),
    ("RGBX", "RGB"),
    ("P", "RGB"),
    ("PA", "RGB"),
]

# The 16-bit greyscale layouts, scaled down to 8 bits rather than clipped.
_SIXTEEN_BIT_MODES = ["I;16", "I;16B", "I;16L"]

# Modes with no honest 8-bit page reading: refused, never guessed at.
_REFUSED_MODES = ["I", "F", "CMYK", "YCbCr", "LAB", "HSV"]

# Mid-grey in 16 bits: 0x8080, which is 128 once scaled by 1/256.  A clip to
# 8 bits would turn it into 255, which is white.
_SIXTEEN_BIT_MID_GREY = 32896


def _no_free_space(_path: object) -> SimpleNamespace:
    """
    Stand in for ``shutil.disk_usage`` on a disk with nothing free.

    Returns:
        An object with the one attribute the spool reads, ``free``, at zero.

    """
    return SimpleNamespace(total=0, used=0, free=0)


class TestSpooledPageSinkDpi:
    """Each page carries the resolution the device read back (N-33)."""

    @pytest.mark.parametrize("dpi", [150, 300, 600])
    def test_the_record_carries_the_dpi_it_was_given(
        self, tmp_path: Path, dpi: int
    ) -> None:
        """``add(image, dpi=N)`` returns a record whose ``dpi`` is N."""
        sink = SpooledPageSink(tmp_path, "a", 10)
        record = sink.add(_inked_page(), dpi=dpi)
        assert record.dpi == dpi

    @pytest.mark.parametrize("dpi", [150, 300, 600])
    def test_the_spooled_png_records_its_dpi(self, tmp_path: Path, dpi: int) -> None:
        """
        The PNG's pHYs chunk carries the dpi, so a sweep can read it back.

        Rounded: PNG stores pixels per metre as an integer, so 300 reads back
        as 299.9994.
        """
        sink = SpooledPageSink(tmp_path, "a", 10)
        record = sink.add(_inked_page(), dpi=dpi)
        with Image.open(record.path) as reopened:
            x_dpi, y_dpi = reopened.info["dpi"]
        assert round(x_dpi) == dpi
        assert round(y_dpi) == dpi


class TestSpooledPageSinkModes:
    """Every page mode is kept, converted or refused, never mishandled (N-33)."""

    @pytest.mark.parametrize("mode", _KEPT_MODES)
    def test_a_sane_mode_is_stored_unchanged(self, tmp_path: Path, mode: str) -> None:
        """``1``, ``L`` and ``RGB`` are spooled as they arrive."""
        sink = SpooledPageSink(tmp_path, "a", 10)
        record = sink.add(Image.new(mode, (40, 60)), dpi=300)
        assert record.mode == mode
        with Image.open(record.path) as reopened:
            assert reopened.mode == mode

    @pytest.mark.parametrize(("mode", "stored"), _CONVERTED_MODES)
    def test_a_convertible_mode_is_stored_converted(
        self, tmp_path: Path, mode: str, stored: str
    ) -> None:
        """Alpha, padding and palettes are converted to ``L`` or ``RGB``."""
        thumbnails: list[str] = []
        sink = SpooledPageSink(tmp_path, "a", 10, thumbnails.append)
        record = sink.add(Image.new(mode, (40, 60)), dpi=300)
        assert record.mode == stored
        assert record.size == (40, 60)
        with Image.open(record.path) as reopened:
            assert reopened.mode == stored
        # The thumbnail is made from the converted page, so it is made at all.
        assert len(thumbnails) == 1

    def test_an_rgba_page_keeps_its_colour(self, tmp_path: Path) -> None:
        """Dropping the alpha band keeps the colour bands as they were."""
        sink = SpooledPageSink(tmp_path, "a", 10)
        record = sink.add(Image.new("RGBA", (40, 60), (10, 20, 30, 40)), dpi=300)
        with Image.open(record.path) as reopened:
            assert reopened.getpixel((0, 0)) == (10, 20, 30)

    @pytest.mark.parametrize("mode", _SIXTEEN_BIT_MODES)
    def test_a_sixteen_bit_page_is_scaled_not_clipped(
        self, tmp_path: Path, mode: str
    ) -> None:
        """16-bit mid-grey is stored as 8-bit mid-grey, 128, and not as 255."""
        sink = SpooledPageSink(tmp_path, "a", 10)
        record = sink.add(Image.new(mode, (40, 60), _SIXTEEN_BIT_MID_GREY), dpi=300)
        assert record.mode == "L"
        with Image.open(record.path) as reopened:
            assert reopened.mode == "L"
            assert reopened.getpixel((0, 0)) == 128

    @pytest.mark.parametrize("mode", _REFUSED_MODES)
    def test_an_unsupported_mode_is_refused_by_name(
        self, tmp_path: Path, mode: str
    ) -> None:
        """The refusal is a ScanError naming the mode, and nothing is written."""
        sink = SpooledPageSink(tmp_path, "a", 10)
        with pytest.raises(ScanError) as excinfo:
            sink.add(Image.new(mode, (40, 60)), dpi=300)
        assert repr(mode) in str(excinfo.value)
        assert "page 1" in str(excinfo.value)
        assert list(tmp_path.iterdir()) == []
        assert sink.records == ()

    @pytest.mark.parametrize("mode", _REFUSED_MODES)
    def test_the_refusal_comes_before_the_room_check(
        self, tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On a full disk a refused mode is still reported as the mode."""
        monkeypatch.setattr("saneless.spool.shutil.disk_usage", _no_free_space)
        sink = SpooledPageSink(tmp_path, "a", 0)
        with pytest.raises(ScanError) as excinfo:
            sink.add(Image.new(mode, (40, 60)), dpi=300)
        assert repr(mode) in str(excinfo.value)
        assert "Insufficient disk space" not in str(excinfo.value)

    @pytest.mark.parametrize(
        ("mode", "size", "required_mb"),
        [
            # Four bands arrive and three are written: 3 MiB, not 4.
            ("RGBA", (1024, 1024), 3),
            # Two bytes a pixel arrive and one is written: 2 MiB, not 4.
            ("I;16", (2048, 1024), 2),
        ],
    )
    def test_the_room_estimate_is_of_the_normalised_page(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        size: tuple[int, int],
        required_mb: int,
    ) -> None:
        """The estimate counts what the page will be, not what it arrived as."""
        monkeypatch.setattr("saneless.spool.shutil.disk_usage", _no_free_space)
        sink = SpooledPageSink(tmp_path, "a", 0)
        with pytest.raises(ScanError) as excinfo:
            sink.add(Image.new(mode, size), dpi=300)
        assert f"{required_mb} MB required" in str(excinfo.value)


class TestSpooledPageSinkAtomicWrite:
    """A page is whole on disk or absent, never truncated (N-20)."""

    def test_the_png_does_not_exist_while_it_is_being_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The save writes a ``.part`` file; the page's own name appears only after.

        Observed from inside a wrapped ``Image.save``, so the claim is about
        the moment of writing and not only about what is left afterwards.
        """
        original_save = Image.Image.save
        final = tmp_path / "a-0001.png"
        targets: list[str] = []
        final_existed: list[bool] = []

        def watching_save(image: Image.Image, fp: Path, **params: object) -> None:
            targets.append(str(fp))
            final_existed.append(final.exists())
            original_save(image, fp, **params)

        monkeypatch.setattr(Image.Image, "save", watching_save)
        sink = SpooledPageSink(tmp_path, "a", 10)
        record = sink.add(_inked_page(), dpi=300)

        assert targets == [str(tmp_path / "a-0001.png.part")]
        assert final_existed == [False]
        assert record.path == final
        assert final.is_file()
        assert sorted(path.name for path in tmp_path.iterdir()) == ["a-0001.png"]

    def test_a_failed_write_leaves_neither_the_page_nor_its_part(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write that dies half way is cleaned up under both names."""

        def half_a_save(_image: Image.Image, fp: Path, **_params: object) -> None:
            Path(fp).write_bytes(b"\x89PNG half a page")
            msg = "No space left on device"
            raise OSError(msg)

        monkeypatch.setattr(Image.Image, "save", half_a_save)
        sink = SpooledPageSink(tmp_path, "a", 0)
        with pytest.raises(ScanError) as excinfo:
            sink.add(_inked_page(), dpi=300)

        assert "No space left on device" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, OSError)
        assert list(tmp_path.iterdir()) == []
        assert sink.records == ()
