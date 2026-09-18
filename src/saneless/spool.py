"""
The one place a scanned page is materialised on disk (M-08, HARD-01, D-01).

``SpooledPageSink`` is the concrete ``PageSink`` the pipeline hands to the
backend.  It takes one acquired page at a time, checks there is room for it,
writes it as a PNG under the job's workspace, measures it, and returns a
``PageRecord``.  Nothing accumulates a list of page images anywhere, which is
the whole of HARD-01's bound: peak memory is a property of who holds a page,
not of how many pages a job has.  M-08 measured the old shape at 1395 MB for a
48-page job; with the spool the ceiling is a small constant number of decoded
pages, independent of page count.

The spooled PNG is not scratch: it *is* the PDF's page content, embedded
losslessly by ``assemble_pdf`` with no second encode (D-03).  It is therefore
written at Pillow's default compression level, 6, which this module gets by
**not** passing the argument at all.  Measured on a noisy A4 300 DPI colour
page: level 6 gives 13.7 MB in 1.57 s, level 1 gives 15.8 MB in 0.62 s.
The 13% is saved in the PDF, on the Paperless upload and in Paperless storage
forever, while the extra second is paid once against a 10-15 s per-page scan.
Level 1 is the documented throughput fallback if ADF speed ever matters more
than output size -- recorded here, deliberately, rather than added as a config
key (Phase 24 D-04).

The spool knows nothing about SANE: the backend hands it one image at a time,
and this module only decides where that image lands and measures it.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from typing import TYPE_CHECKING, Final

from PIL.ImageStat import Stat

from .exceptions import ScanError, describe
from .pages import generate_thumbnail
from .scanner.base import PageRecord, PageSink

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from PIL import Image

__all__ = ["SpooledPageSink"]

logger = logging.getLogger(__name__)

# One megabyte, as the free-space arithmetic counts them.  Matches
# ``pipeline._check_disk_space``, so the per-page shortfall and the up-front
# one are reported in the same units.
_BYTES_PER_MB: Final[int] = 1024 * 1024


class SpooledPageSink(PageSink):
    """
    Write each acquired page to a directory and report what it was (D-01).

    One sink serves one acquisition pass.  A manual-duplex job therefore builds
    two, ``"a"`` for the fronts and ``"b"`` for the backs, so the spooled names
    stay distinguishable while the two passes share a directory.  That is for
    debuggability only: document order comes from ``PageRecord.sequence`` and
    the order of ``records``, never from sorting or globbing the directory
    (D-02).  After the duplex interleave the names do not sort into document
    order at all, which is exactly the invariant HARD-01's test attacks.

    The constructor takes four arguments beside ``self``, which is ruff's
    ``PLR0913`` ceiling.  Any further knob has to be a method, not a fifth
    parameter -- the rule ``FakeSaneDev.set_page_delay`` already follows.
    """

    def __init__(
        self,
        directory: Path,
        pass_label: str,
        min_free_space_mb: int,
        thumbnail_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Prepare a sink that spools this pass's pages into ``directory``.

        Args:
            directory: Where the pages land.  The pipeline's per-job
                ``TemporaryDirectory`` owns it, so the spool's lifetime is the
                job's and anything to be preserved must be moved out before
                that workspace closes.
            pass_label: The short prefix every file of this pass carries, e.g.
                ``"a"`` for a simplex job or a duplex job's fronts.
            min_free_space_mb: The reserve to keep free beyond the page being
                written, so PDF assembly still has room afterwards.  It is the
                operator's ``min_free_space_mb``, the same value the up-front
                check uses.
            thumbnail_callback: Called once, with the first page's base64 JPEG
                thumbnail, at the moment that page is spooled (D-05).  None
                when nobody is watching.

        """
        self._directory = directory
        self._pass_label = pass_label
        self._min_free_space_mb = min_free_space_mb
        self._thumbnail_callback = thumbnail_callback
        self._sequence = 0
        self._records: list[PageRecord] = []

    @property
    def records(self) -> tuple[PageRecord, ...]:
        """
        Every page spooled so far, in acquisition order.

        A tuple rather than the internal list, so a caller holding the result
        of a failed pass cannot append to the sink's own bookkeeping while the
        preservation path is reading it.

        Returns:
            The records, oldest first.

        """
        return tuple(self._records)

    def add(self, image: Image.Image) -> PageRecord:
        """
        Spool one acquired page and return the record describing it.

        The page is measured exactly once, here, while it is already decoded:
        the greyscale conversion the blank-page thresholds need used to happen
        again later in ``is_empty_page``, and this is the conversion D-06
        removes from there.  The first page's thumbnail is generated here for
        the same reason -- reopening a 26 MB page afterwards would be a second
        decode of something that is in memory right now (D-05).

        Args:
            image: The page the device produced, already cropped if the
                requested paper size required it.

        Returns:
            A PageRecord with the next 1-based sequence, the spooled path, the
            page's size and mode, and its greyscale mean and stddev.

        Raises:
            ScanError: If the page plus the assembly reserve would not fit, or
                if writing it failed.  No raw OSError escapes this method
                (D-07).

        """
        self._sequence += 1
        sequence = self._sequence
        png_path = self._directory / f"{self._pass_label}-{sequence:04d}.png"

        self._check_room_for(image, sequence, png_path)
        self._write(image, sequence, png_path)

        grey = image.convert("L")
        stats = Stat(grey)
        record = PageRecord(
            sequence=sequence,
            path=png_path,
            size=image.size,
            mode=image.mode,
            mean=stats.mean[0],
            stddev=stats.stddev[0],
        )

        self._records.append(record)

        if sequence == 1 and self._thumbnail_callback is not None:
            # Not wrapped in a suppression: a thumbnail is a visible part of
            # what the operator sees, so a failure here should surface rather
            # than leave the strip silently blank.
            #
            # The record is appended *first*, and that ordering is load-bearing
            # rather than tidy.  The callback can raise for reasons that have
            # nothing to do with the page -- the web worker's is a job-store
            # write, which raises sqlite3.Error on a locked or closed database,
            # and generate_thumbnail itself raises OSError for a mode JPEG
            # cannot encode.  Appending afterwards meant a raise here left
            # a-0001.png on disk with no record of it: page_count() answered 0,
            # so _preserving_partial_scan took its "nothing reached the spool"
            # branch and let the workspace delete a sheet that had really been
            # fed (WR-01).
            self._thumbnail_callback(generate_thumbnail(image))

        logger.debug(
            "Spooled page %d to %s (%dx%d %s, mean %.1f, stddev %.1f)",
            sequence,
            png_path,
            record.size[0],
            record.size[1],
            record.mode,
            record.mean,
            record.stddev,
        )
        return record

    def _check_room_for(
        self, image: Image.Image, sequence: int, png_path: Path
    ) -> None:
        """
        Refuse the page if it plus the assembly reserve would not fit (D-07).

        The page's decoded size is computed from its dimensions and band
        count, which is an upper bound on the PNG because the PNG is
        compressed.

        Args:
            image: The page about to be written.
            sequence: Its 1-based page number, for the message.
            png_path: Where it would have been written, for the message.

        Raises:
            ScanError: If free space is below the page plus the reserve, or if
                it could not be measured at all.  ``add``'s "no raw OSError
                escapes this method" promise (D-07) covers the measurement as
                well as the write: a spool directory that has been removed, or
                whose mount went away, raises ``FileNotFoundError`` here, and
                untranslated it escaped past ``_acquire_pages``' ``except
                ScanError`` ladder into its generic handler -- where it came
                out as "Scanner error on page N", blaming the scanner for a
                disk fault -- and past the flatbed path's handler entirely,
                because ``_snap_flatbed``'s ``sink.add`` call sits outside its
                ``try`` (WR-11).

        """
        # From size and band count, never len(image.tobytes()): that copied
        # the whole page just to measure its length, and removing it is one of
        # M-08's cheap wins (D-06).  Exact for the "L" and "RGB" modes a SANE
        # snap produces.
        decoded_bytes = image.size[0] * image.size[1] * len(image.getbands())
        page_mb = (decoded_bytes + _BYTES_PER_MB - 1) // _BYTES_PER_MB
        required_mb = page_mb + self._min_free_space_mb
        try:
            free_mb = shutil.disk_usage(self._directory).free // _BYTES_PER_MB
        except OSError as exc:
            measure_msg = (
                f"Could not measure free space for page {sequence} in "
                f"{self._directory}: {describe(exc)}"
            )
            raise ScanError(measure_msg) from exc
        if free_mb < required_mb:
            msg = (
                f"Insufficient disk space for page {sequence}: "
                f"{free_mb} MB free in {png_path}, {required_mb} MB required "
                "(configure min_free_space_mb to adjust)"
            )
            raise ScanError(msg)

    def _write(self, image: Image.Image, sequence: int, png_path: Path) -> None:
        """
        Write the page as a PNG, translating any OSError (D-07).

        Args:
            image: The page to write.
            sequence: Its 1-based page number, for the message.
            png_path: Where to write it.

        Raises:
            ScanError: If the write failed, chained from the original OSError.

        """
        try:
            # No compress_level argument, deliberately: Pillow's default 6 is
            # what the module docstring's measurement chose.
            image.save(png_path, format="PNG")
        except OSError as exc:
            # Remove whatever the failed write left behind, so the spool never
            # hands a truncated page to assembly or to the preservation path.
            with contextlib.suppress(OSError):
                png_path.unlink(missing_ok=True)
            msg = f"Could not write page {sequence} to {png_path}: {describe(exc)}"
            raise ScanError(msg) from exc
