"""
The one place a scanned page is materialised on disk.

``SpooledPageSink`` is the concrete ``PageSink`` the pipeline hands to the
backend.  It takes one acquired page at a time, brings it to a mode it can
store (or refuses it), checks there is room for it, writes it as a PNG under
the job's workspace, measures it, and returns a ``PageRecord``.  Nothing accumulates a list of page images anywhere, which is
the whole of the memory bound: peak memory is a property of who holds a page,
not of how many pages a job has.  The old shape was measured at 1395 MB for a
48-page job; with the spool the ceiling is a small constant number of decoded
pages, independent of page count.

The spooled PNG is not scratch: it *is* the PDF's page content, embedded
losslessly by ``assemble_pdf`` with no second encode.  It is therefore
written at Pillow's default compression level, 6, which this module gets by
**not** passing the argument at all.  Measured on a noisy A4 300 DPI colour
page: level 6 gives 13.7 MB in 1.57 s, level 1 gives 15.8 MB in 0.62 s.
The 13% is saved in the PDF, on the Paperless upload and in Paperless storage
forever, while the extra second is paid once against a 10-15 s per-page scan.
Level 1 is the documented throughput fallback if ADF speed ever matters more
than output size -- recorded here, deliberately, rather than added as a config
key.

Each page is written to ``<name>.png.part`` and renamed to ``<name>.png``
only once the write is complete, so a process killed part way through a page
leaves a whole page or a ``.part`` file that every reader ignores -- never a
truncated PNG under a page's name.  The PNG also carries a pHYs chunk with
the resolution the device read back, so a sweep that finds the spool with no
``PageRecord`` to go on can still read each page's dpi from the file.  The
rename protects against the process dying, not against power loss: nothing
here is fsynced, because a sheet already fed cannot be re-fed either way and
a per-page fsync would stall a feeder that is otherwise disk-bound.

The spool knows nothing about SANE: the backend hands it one image at a time,
and this module only decides where that image lands and measures it.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from .exceptions import ScanError, describe
from .pages import generate_thumbnail, measure_ink
from .scanner.base import PageRecord, PageSink

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from PIL import Image

__all__ = ["SpooledPageSink"]

logger = logging.getLogger(__name__)

# One megabyte, as the free-space arithmetic counts them.  Matches
# ``pipeline._check_disk_space``, so the per-page shortfall and the up-front
# one are reported in the same units.
_BYTES_PER_MB: Final[int] = 1024 * 1024

# The modes a page is spooled in exactly as it arrived.  "L" and "RGB" are
# what a SANE snap produces; "1" is lineart, which the thumbnail, the PNG
# write and img2pdf all handle as it is.
_KEPT_MODES: Final[frozenset[str]] = frozenset({"1", "L", "RGB"})

# Modes that are converted, and to what.  Each breaks the thumbnail or the PNG
# write as it is, and each has one obvious 8-bit reading: the alpha band or
# the padding byte is dropped, and a palette is expanded.
_CONVERTED_MODES: Final[Mapping[str, str]] = MappingProxyType(
    {"LA": "L", "RGBA": "RGB", "RGBX": "RGB", "P": "RGB", "PA": "RGB"}
)

# 16-bit greyscale, in each byte order Pillow names it by.  Scaled into 8 bits
# rather than converted directly, which clips.
_SIXTEEN_BIT_MODES: Final[frozenset[str]] = frozenset({"I;16", "I;16B", "I;16L"})

# One 16-bit step is 1/256 of an 8-bit one.
_SIXTEEN_TO_EIGHT_BITS: Final[float] = 1 / 256


def _normalise_mode(image: Image.Image, sequence: int) -> Image.Image:
    """
    Return the page in a mode the spool can store, or refuse it by name.

    The table, in full:

    ==============================  ===========================================
    Arriving mode                   Spooled as
    ==============================  ===========================================
    ``1``, ``L``, ``RGB``           itself, unchanged
    ``LA``                          ``L`` (the alpha band is dropped)
    ``RGBA``, ``RGBX``              ``RGB`` (the fourth band is dropped)
    ``P``, ``PA``                   ``RGB`` (the palette is expanded)
    ``I;16``, ``I;16B``, ``I;16L``  ``L``, each value scaled by 1/256
    anything else                   refused with a ``ScanError``
    ==============================  ===========================================

    The converted modes are not guesses: each broke something downstream as it
    was.  ``LA``, ``RGBA`` and ``P`` made the thumbnail raise, and ``PA`` and
    ``RGBX`` made the PNG write raise.

    16-bit greyscale is **scaled, not clipped**.  ``convert("L")`` clips
    every 16-bit value above 255 to 255, so a mid-grey 32896 page came out
    white -- which empty-page detection would then call blank.  Dividing by
    256 keeps mid-grey mid-grey (32896 becomes 128).  The scale goes through
    mode ``I`` because Pillow applies a point transform to ``I;16`` itself
    but not to its big- and little-endian spellings.

    Everything else is refused rather than converted.  ``I`` and ``F`` have
    no fixed range to scale from, and ``CMYK``, ``YCbCr``, ``LAB`` and
    ``HSV`` are not what a scanner hands a SANE frontend; a conversion would
    be a guess nobody could check.  Writing ``I`` as a PNG also raises a
    ``DeprecationWarning`` in current Pillow.

    Args:
        image: The page as the device produced it.
        sequence: Its 1-based page number, for the message.

    Returns:
        The page itself when its mode is kept, otherwise a converted copy.

    Raises:
        ScanError: If the mode is not in the table.  Raised before anything
            is measured or written.

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

    One sink serves one acquisition pass.  A manual-duplex job therefore builds
    two, ``"a"`` for the fronts and ``"b"`` for the backs, so the spooled names
    stay distinguishable while the two passes share a directory.  That is for
    debuggability only: document order comes from ``PageRecord.sequence`` and
    the order of ``records``, never from sorting or globbing the directory.
    After the duplex interleave the names do not sort into document order at
    all, so a sort would silently scramble the document.

    The constructor takes four arguments beside ``self``, which is ruff's
    ``PLR0913`` ceiling.  Any further knob has to be a method, not a fifth
    parameter.
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
                workspace owns it, so the spool's lifetime is the
                job's and anything to be preserved must be moved out before
                that workspace closes.
            pass_label: The short prefix every file of this pass carries, e.g.
                ``"a"`` for a simplex job or a duplex job's fronts.
            min_free_space_mb: The reserve to keep free beyond the page being
                written, so PDF assembly still has room afterwards.  It is the
                operator's ``min_free_space_mb``, the same value the up-front
                check uses.
            thumbnail_callback: Called once, with the first page's base64 JPEG
                thumbnail, at the moment that page is spooled.  None
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

    def add(self, image: Image.Image, *, dpi: int) -> PageRecord:
        """
        Spool one acquired page and return the record describing it.

        The order is fixed.  The mode is normalised first (``_normalise_mode``
        has the table), so a mode the spool refuses is refused before anything
        is measured or written, and the room check that follows estimates the
        page as it will be written rather than as it arrived.  Then the page
        is written, and then measured.

        The page is measured exactly once, here, while it is already decoded,
        by ``pages.measure_ink``: its ink coverage and paper white go on the
        record, and the blank-page filter judges those two numbers later
        without opening the file again.  No threshold is applied here, because
        the threshold is the profile's.  The measurement runs on the
        normalised page, which is the page as written.  The first page's
        thumbnail is generated here for the same reason -- reopening a 26 MB
        page afterwards would be a second decode of something that is in
        memory right now.

        Args:
            image: The page the device produced, already cropped if the
                requested paper size required it.
            dpi: The resolution the device read back.  It is stored on the
                record, which every PDF lays the page out from, and in the
                PNG's pHYs chunk.

        Returns:
            A PageRecord with the next 1-based sequence, the spooled path, the
            page's size, its mode as spooled, its dpi, and its ink coverage and
            paper white.

        Raises:
            ScanError: If the page's mode is one the spool refuses, if the page
                plus the assembly reserve would not fit, or if writing it
                failed.  No raw OSError escapes this method.

        """
        self._sequence += 1
        sequence = self._sequence
        png_path = self._directory / f"{self._pass_label}-{sequence:04d}.png"

        image = _normalise_mode(image, sequence)
        self._check_room_for(image, sequence, png_path)
        self._write(image, sequence, png_path, dpi)

        measurement = measure_ink(image)
        record = PageRecord(
            sequence=sequence,
            path=png_path,
            size=image.size,
            mode=image.mode,
            dpi=dpi,
            ink_coverage=measurement.coverage,
            paper_white=measurement.paper_white,
        )

        self._records.append(record)

        if sequence == 1 and self._thumbnail_callback is not None:
            # Best-effort: the thumbnail is something to look at while the
            # scan runs, not part of the scan.  It can fail for reasons that
            # have nothing to do with the page -- the web worker's callback is
            # a job-store write, which raises sqlite3.Error on a locked or
            # closed database, and generate_thumbnail itself raises OSError
            # for a mode JPEG cannot encode -- and a failure that ended the
            # pass would stop the feeder over a missing picture.  So it is
            # logged with its traceback and the page stands.
            #
            # The record is still appended *first*, and that ordering stays
            # load-bearing: if anything here escaped anyway (a
            # KeyboardInterrupt, say), the page on disk is already recorded,
            # so the run's guard keeps it rather than let the workspace delete
            # a sheet that had really been fed.
            try:
                self._thumbnail_callback(generate_thumbnail(image))
            except Exception:
                logger.warning(
                    "Could not make or hand on the thumbnail of %s; the scan continues",
                    png_path,
                    exc_info=True,
                )

        logger.debug(
            "Spooled page %d to %s (%dx%d %s at %d dpi, ink %r%%, paper white %d)",
            sequence,
            png_path,
            record.size[0],
            record.size[1],
            record.mode,
            record.dpi,
            record.ink_coverage,
            record.paper_white,
        )
        return record

    def _check_room_for(
        self, image: Image.Image, sequence: int, png_path: Path
    ) -> None:
        """
        Refuse the page if it plus the assembly reserve would not fit.

        The page's decoded size is computed from its dimensions and band
        count, which is an upper bound on the PNG because the PNG is
        compressed.

        Args:
            image: The page about to be written, already in the mode it will
                be written in.
            sequence: Its 1-based page number, for the message.
            png_path: Where it would have been written, for the message.

        Raises:
            ScanError: If free space is below the page plus the reserve, or if
                it could not be measured at all.  ``add``'s "no raw OSError
                escapes this method" promise covers the measurement as
                well as the write: a spool directory that has been removed, or
                whose mount went away, raises ``FileNotFoundError`` here, and
                untranslated it escaped past ``_acquire_pages``' ``except
                ScanError`` ladder into its generic handler -- where it came
                out as "Scanner error on page N", blaming the scanner for a
                disk fault -- and past the flatbed path's handler entirely,
                because ``_snap_flatbed``'s ``sink.add`` call sits outside its
                ``try``.

        """
        # From size and band count, never len(image.tobytes()): that copied
        # the whole page -- 26 MB at A4 300 dpi colour -- just to measure its
        # length.  Exact for "L" and "RGB", which is why it runs on the
        # normalised page: an RGBA page arriving with four bands is written
        # with three.  For "1" it overstates eightfold, which errs on the
        # safe side.
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

    def _write(
        self, image: Image.Image, sequence: int, png_path: Path, dpi: int
    ) -> None:
        """
        Write the page as a PNG under ``.part``, then rename it into place.

        ``png_path`` exists only once the whole page does: the save goes to
        ``<name>.png.part`` and ``Path.replace`` renames it, which is atomic
        within one directory.  A process killed mid-write therefore leaves a
        ``.part`` file, which no reader looks for, and never a truncated page
        under the page's own name.

        Args:
            image: The page to write.
            sequence: Its 1-based page number, for the message.
            png_path: Where the finished page lands.
            dpi: The resolution written into the PNG's pHYs chunk.

        Raises:
            ScanError: If the write or the rename failed, chained from the
                original OSError.

        """
        part_path = png_path.with_name(f"{png_path.name}.part")
        try:
            # No compress_level argument, deliberately: Pillow's default 6 is
            # what the module docstring's measurement chose.  dpi= is an
            # (x, y) pair, as Pillow's PNG writer requires.
            image.save(part_path, format="PNG", dpi=(dpi, dpi))
            part_path.replace(png_path)
        except OSError as exc:
            # Remove whatever the failed write left behind, so the spool never
            # hands a truncated page to assembly, the preservation path or a
            # sweep.  The page's own name was never created: only the rename
            # creates it, and the rename is the last thing that can fail.
            with contextlib.suppress(OSError):
                part_path.unlink(missing_ok=True)
            msg = f"Could not write page {sequence} to {png_path}: {describe(exc)}"
            raise ScanError(msg) from exc
