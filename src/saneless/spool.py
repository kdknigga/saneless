"""
The one place a scanned page is materialised on disk.

``SpooledPageSink`` is the concrete ``PageSink`` the pipeline hands to the
backend.  It takes one acquired page at a time, brings it to a mode it can
store (or refuses it), checks there is room for it, writes it as a PNG under
the job's workspace, measures it, and returns a ``PageRecord``.  Nothing here
accumulates page images, so peak memory does not grow with page count.
See docs/explanation/decisions/0006-per-page-pdf-and-qpdf-merge.md.

The spooled PNG *is* the PDF's page content, embedded losslessly by
``assemble_pdf`` with no second encode.  It is written at Pillow's default
compression level by not passing the argument: measured on a noisy A4 300 DPI
colour page, level 1 saves about a second but is 13% larger in Paperless
storage for good.

Each page is written to ``<name>.png.part`` and renamed into place once
complete, so a killed process never leaves a truncated PNG under a page's
name.  Nothing is fsynced: a fed sheet cannot be re-fed either way, and a
per-page fsync would stall the feeder.  The PNG's pHYs chunk carries the
read-back dpi, so a sweep with no ``PageRecord`` can still lay the page out.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from .exceptions import (
    DiskSpaceError,
    ScanError,
    ScanInterrupted,
    SpoolError,
    describe,
    is_out_of_space,
)
from .pages import generate_thumbnail, measure_ink
from .scanner.base import PageRecord, PageSink

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from PIL import Image

__all__ = ["BYTES_PER_MB", "SpooledPageSink"]

logger = logging.getLogger(__name__)

# One megabyte, 10**6 bytes, as ``min_free_space_mb``, the docs and every
# message count it.
BYTES_PER_MB: Final[int] = 1_000_000

# The modes a page is spooled in exactly as it arrived.
_KEPT_MODES: Final[frozenset[str]] = frozenset({"1", "L", "RGB"})

# Modes that are converted, and to what: each breaks the thumbnail or the PNG
# write as it is.
_CONVERTED_MODES: Final[Mapping[str, str]] = MappingProxyType(
    {"LA": "L", "RGBA": "RGB", "RGBX": "RGB", "P": "RGB", "PA": "RGB"}
)

# 16-bit greyscale, in each byte order Pillow names it by.
_SIXTEEN_BIT_MODES: Final[frozenset[str]] = frozenset({"I;16", "I;16B", "I;16L"})

_SIXTEEN_TO_EIGHT_BITS: Final[float] = 1 / 256


def _normalise_mode(image: Image.Image, sequence: int) -> Image.Image:
    """
    Return the page in a mode the spool can store, or refuse it by name.

    16-bit greyscale is **scaled, not clipped**: ``convert("L")`` clips every
    value above 255, so a mid-grey page would come out white and be called
    blank.  The scale goes through mode ``I`` because Pillow applies a point
    transform to ``I;16`` but not to its byte-order spellings.  Anything else
    has no range to scale from or is not what a scanner sends, so it is refused.
    """
    mode = image.mode
    if mode in _KEPT_MODES:
        return image
    converted = _CONVERTED_MODES.get(mode)
    if converted is not None:
        return image.convert(converted)
    if mode in _SIXTEEN_BIT_MODES:
        return (
            image.convert("I")
            .point(lambda value: value * _SIXTEEN_TO_EIGHT_BITS)
            .convert("L")
        )
    msg = (
        f"Could not spool page {sequence}: the scanner delivered it in image "
        f"mode {mode!r}, which saneless does not store (it takes 1, L and RGB, "
        "and converts LA, RGBA, RGBX, P, PA and 16-bit greyscale)"
    )
    raise ScanError(msg)


class SpooledPageSink(PageSink):
    """
    Write each acquired page to a directory and report what it was.

    One sink serves one acquisition pass; a manual-duplex job builds ``"a"``
    for the fronts and ``"b"`` for the backs.  Document order comes from
    ``PageRecord.sequence`` and the order of ``records``, never from sorting
    the directory: after the duplex interleave the names do not sort into
    document order.
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
            directory: Where the pages land, inside the job's workspace, so
                anything to be preserved must be moved out before it closes.
            pass_label: The short prefix every file of this pass carries, e.g.
                ``"a"`` for a simplex job or a duplex job's fronts.
            min_free_space_mb: The reserve to keep free beyond the page being
                written, so PDF assembly still has room afterwards.
            thumbnail_callback: Called once, with the first page's base64 JPEG
                thumbnail, when that page is spooled.  None when nobody is
                watching.

        """
        self._directory = directory
        self._pass_label = pass_label
        self._min_free_space_mb = min_free_space_mb
        self._thumbnail_callback = thumbnail_callback
        self._records: list[PageRecord] = []

    @property
    def records(self) -> tuple[PageRecord, ...]:
        """
        Every page spooled so far, in acquisition order.

        A tuple, so a caller cannot append to the sink's own bookkeeping.

        Returns:
            The records, oldest first.

        """
        return tuple(self._records)

    def add(self, image: Image.Image, *, dpi: int) -> PageRecord:
        """
        Spool one acquired page and return the record describing it.

        The order is fixed: normalise, check room, measure, write, record.  A
        refused mode is refused before anything is written, and the record is
        appended the moment the page is on disk, since a page file the records
        do not name is a sheet the run's guard would not keep.

        A ``ScanInterrupted`` anywhere in that sequence does not lose the page:
        the sheet has left the feeder and this image is its only copy, so the
        sequence runs again, the page is recorded, and only then does the
        interruption go on.  The signal handler ignores both signals by then,
        so nothing interrupts the second attempt.

        The page is measured, and the first page's thumbnail made, here while
        it is already decoded, so nothing opens the file again.

        Args:
            image: The page the device produced, already cropped if the
                requested paper size required it.
            dpi: The resolution the device read back, stored on the record and
                in the PNG's pHYs chunk.

        Returns:
            A PageRecord with the next 1-based sequence, the spooled path, the
            page's size, its mode as spooled, its dpi, and its ink coverage and
            paper white.

        Raises:
            ScanError: If the page's mode is one the spool refuses: the
                scanner delivered a page saneless cannot store.
            DiskSpaceError: If the page plus the assembly reserve would not
                fit, or if writing it ran out of space or quota.  Not a
                ``ScanError``, so no scanner handler can claim a full disk.
            SpoolError: If free space could not be measured, or if writing it
                failed for any other reason.  No raw OSError escapes this
                method.

        """
        # No call sits between the page reaching disk and the append for a
        # signal to land in.  _spool changes nothing on the sink, and the
        # page's number is the count recorded, so a retry writes the same file.
        recorded = len(self._records)
        try:
            record, spooled = self._spool(image, dpi)
            self._records.append(record)
        except ScanInterrupted:
            if len(self._records) == recorded:
                record, spooled = self._spool(image, dpi)
                self._records.append(record)
            raise

        if record.sequence == 1 and self._thumbnail_callback is not None:
            # Best-effort: a failed thumbnail must not stop the feeder over a
            # missing picture.  The record is appended first, so anything that
            # escaped anyway still leaves the fed page recorded and kept.
            try:
                self._thumbnail_callback(generate_thumbnail(spooled))
            except Exception:
                logger.warning(
                    "Could not make or hand on the thumbnail of %s; the scan continues",
                    record.path,
                    exc_info=True,
                )

        logger.debug(
            "Spooled page %d to %s (%dx%d %s at %d dpi, ink %r%%, paper white %d)",
            record.sequence,
            record.path,
            record.size[0],
            record.size[1],
            record.mode,
            record.dpi,
            record.ink_coverage,
            record.paper_white,
        )
        return record

    def _spool(self, image: Image.Image, dpi: int) -> tuple[PageRecord, Image.Image]:
        """
        Normalise, check, measure and write the next page, recording nothing.

        Returns:
            The page's record, and the page as it was written.

        """
        sequence = len(self._records) + 1
        png_path = self._directory / f"{self._pass_label}-{sequence:04d}.png"

        page = _normalise_mode(image, sequence)
        self._check_room_for(page, sequence, png_path)

        measurement = measure_ink(page)
        record = PageRecord(
            sequence=sequence,
            path=png_path,
            size=page.size,
            mode=page.mode,
            dpi=dpi,
            ink_coverage=measurement.coverage,
            paper_white=measurement.paper_white,
        )
        self._write(page, sequence, png_path, dpi)
        return record, page

    def _check_room_for(
        self, image: Image.Image, sequence: int, png_path: Path
    ) -> None:
        """
        Refuse the page if it plus the assembly reserve would not fit.

        The decoded size estimates the PNG rather than bounding it: an
        incompressible page was measured slightly larger as a PNG than raw.

        Raises:
            DiskSpaceError: If free space is below the page plus the reserve.
            SpoolError: If free space could not be measured, so a vanished
                spool directory is never blamed on the scanner.

        """
        # From size and band count, never len(image.tobytes()), which copies
        # the whole page.  Exact for the normalised "L" and "RGB"; for "1" it
        # overstates eightfold, which errs on the safe side.
        decoded_bytes = image.size[0] * image.size[1] * len(image.getbands())
        page_mb = (decoded_bytes + BYTES_PER_MB - 1) // BYTES_PER_MB
        required_mb = page_mb + self._min_free_space_mb
        try:
            free_mb = shutil.disk_usage(self._directory).free // BYTES_PER_MB
        except OSError as exc:
            measure_msg = (
                f"Could not measure free space for page {sequence} in "
                f"{self._directory}: {describe(exc)}"
            )
            raise SpoolError(measure_msg) from exc
        if free_mb < required_mb:
            msg = (
                f"Insufficient disk space for page {sequence}: "
                f"{free_mb} MB free in {png_path}, {required_mb} MB required "
                "(configure min_free_space_mb to adjust)"
            )
            raise DiskSpaceError(msg)

    def _write(
        self, image: Image.Image, sequence: int, png_path: Path, dpi: int
    ) -> None:
        """
        Write the page as a PNG under ``.part``, then rename it into place.

        ``Path.replace`` is atomic within one directory, so ``png_path`` exists
        only once the whole page does.

        Raises:
            DiskSpaceError: If the write or the rename ran out of space or
                quota.
            SpoolError: If the write or the rename failed for any other reason.

        """
        part_path = png_path.with_name(f"{png_path.name}.part")
        try:
            # No compress_level argument, deliberately: the module docstring
            # says why.
            image.save(part_path, format="PNG", dpi=(dpi, dpi))
            part_path.replace(png_path)
        except OSError as exc:
            # Remove the partial file, so the spool never hands a truncated
            # page to assembly, preservation or a sweep.
            with contextlib.suppress(OSError):
                part_path.unlink(missing_ok=True)
            msg = f"Could not write page {sequence} to {png_path}: {describe(exc)}"
            if is_out_of_space(exc):
                raise DiskSpaceError(msg) from exc
            raise SpoolError(msg) from exc
