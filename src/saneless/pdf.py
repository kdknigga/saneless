"""
PDF assembly via img2pdf, over the pages the spool already wrote.

Each acquired page is on disk as a PNG before assembly starts, written once
by ``saneless.spool``, and that file is what img2pdf embeds -- losslessly,
with no second encode (D-03). This module therefore creates no temporary
image files and owns no page's lifetime: the spool lives in the job's
workspace, and assembly only reads from it.

Assembly runs **one page per ``img2pdf.convert`` call** and merges the
single-page PDFs with qpdf, because that is the only shape in which the
per-page memory bound is true at any page count. The measured figures, and the
one cost that buys, are in :func:`assemble_pdf`.
"""

from __future__ import annotations

import logging
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import img2pdf
import pikepdf
import PIL.Image

from saneless.exceptions import PdfError, describe

if TYPE_CHECKING:
    from collections.abc import Sequence

    from saneless.scanner.base import PageRecord

# Allow high-DPI scans without triggering Pillow's decompression bomb check.
# 600 DPI A4 color = ~34.8M pixels; 1200 DPI = ~139M pixels.
PIL.Image.MAX_IMAGE_PIXELS = 200_000_000

# Everything outside the allow-list becomes a separator.  See
# sanitise_title_for_filename for why this is an allow-list and not a
# deny-list, and why auto_profiles._slugify is not reused.
_NON_SLUG_CHARACTERS = re.compile(r"[^a-z0-9]+")

# Cap on the title slug only; the timestamp and job-id segments are fixed
# width, keeping the composed name far below NAME_MAX.
_MAX_SLUG_LENGTH = 60

# A uuid4 prefix long enough that a collision needs ~2^16 jobs in one second.
_JOB_ID_LENGTH = 8

__all__ = ["assemble_pdf", "build_pdf_filename", "sanitise_title_for_filename"]

logger = logging.getLogger(__name__)


def sanitise_title_for_filename(title: str) -> str:
    r"""
    Reduce a user-supplied title to a safe single path segment.

    The rule is an **allow-list**, deliberately: everything outside
    ``[a-z0-9]`` collapses to a single ``-``.  A deny-list can be defeated by
    a character nobody anticipated; an allow-list cannot.  That is what makes
    ``/``, ``\``, ``..``, ``~`` and ``\x00`` unrepresentable here by
    construction rather than by enumeration -- this title arrives from a web
    form body or a ``saneless scan --title`` argument and its result is joined
    onto ``consume_dir`` and ``<data_dir>/failed/``.

    ``auto_profiles._slugify`` is **not** reused for this, and must not be: it
    is two ``str.replace`` calls that pass ``/``, ``..`` and every control
    character straight through.  Its input is a trusted SANE source name, not
    operator input, so it is correct for its own job and unsafe for this one.

    The 60-character cap keeps the whole composed name (see
    :func:`build_pdf_filename`, whose other segments are fixed width) under
    ``NAME_MAX`` -- 255 bytes on ext4 and overlayfs -- and under eCryptfs's
    stricter 143-byte limit, so a long title cannot turn into
    ``OSError: [Errno 36] File name too long`` on a user-controlled path.

    A title with nothing allow-listed in it -- ``"..."``, or a wholly
    non-Latin one like ``"日本語"`` -- returns the empty string and the caller
    drops the segment.  Substituting a literal ``untitled`` is rejected: it
    would be a lie about what the operator typed.

    Args:
        title: The title as typed by the operator. Wholly untrusted.

    Returns:
        A lowercase ``[a-z0-9-]`` slug of at most 60 characters, with no
        leading, trailing or doubled ``-``; or ``""`` if nothing survived.

    """
    slug = _NON_SLUG_CHARACTERS.sub("-", title.lower()).strip("-")
    # Cap first, then strip again: the cut can land mid-separator.
    return slug[:_MAX_SLUG_LENGTH].strip("-")


def build_pdf_filename(job_id: str, title: str) -> str:
    """
    Compose the unique file name for one job's assembled PDF.

    The shape is ``{YYYYmmdd-HHMMSS}-{job id}-{title slug}.pdf``, with either
    of the last two segments dropped entirely when it sanitises away, so the
    name never carries a dangling ``-`` before its extension.

    **Uniqueness comes from the job id, not from the timestamp.** The job id is
    a uuid4; the timestamp is only there to make a directory listing sort
    usefully. Two jobs submitted in the same second with the same title would
    collide on the timestamp alone, and a collision is not cosmetic here:
    ``shutil.move`` onto an explicit destination path overwrites silently, so
    two same-named PDFs preserved into ``failed/`` would destroy one scan.

    The job id is put through the same sanitiser as the title. In production it
    is a uuid4, every character of which already survives the allow-list
    untouched, so this costs nothing there -- but the id is a path segment like
    any other, and a path segment that is merely *expected* to be safe is not a
    control this function owns.

    Args:
        job_id: The job's identifier, normally a uuid4. Empty only in tests,
            where the worker that supplies ``job.id`` is not in the picture;
            the segment is then dropped.
        title: The title as typed by the operator. Wholly untrusted.

    Returns:
        A single ``.pdf`` file name -- never a path, and never a name that
        escapes the directory it is joined onto.

    """
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    job_segment = sanitise_title_for_filename(job_id)[:_JOB_ID_LENGTH].strip("-")
    segments = [
        part
        for part in (timestamp, job_segment, sanitise_title_for_filename(title))
        if part
    ]
    return "-".join(segments) + ".pdf"


def assemble_pdf(
    records: Sequence[PageRecord],
    output_dir: Path,
    filename: str,
    dpi: int,
) -> Path:
    """
    Assemble spooled pages into a single PDF using img2pdf.

    The pages are already files: the spool wrote each one as it was acquired,
    and img2pdf embeds those very files. There is no longer a temporary
    directory of re-saved copies here, because that second encode produced a
    PNG that was byte-for-byte pointless -- the spooled one already *is* the
    PDF's page content (D-03).

    The order of ``records`` is the document order and the only source of it.
    The spool directory is never sorted and never globbed: after a
    manual-duplex interleave the file names do not sort into document order,
    and a file sitting in that directory without a record does not belong in
    this PDF (D-02).

    ``filename`` is **required**, with no default. Giving it one -- the single
    hardcoded name this function used to write every PDF to -- would have kept
    every existing caller compiling while silently preserving the collision
    this argument exists to remove: every document reaching paperless carried
    the same original filename, and two preserved scans overwrote each other.
    So the type level forces each caller to name its own file.
    Build the name with :func:`build_pdf_filename` rather than composing one.

    ``dpi`` is likewise supplied by the caller -- as the resolution the scanner
    reported actually using, read back from the device rather than the one the
    profile requested -- and is deliberately **not** read from the images. On
    this path PIL carries no DPI at all: images arrive from ``dev.snap()`` and
    go through ``crop_to_paper_size``, whose ``Image.crop()`` returns a fresh
    image whose ``.info`` is measured as ``{}``. A "prefer the image's own DPI"
    branch would therefore be unreachable dead code -- and a PNG round-trip
    degrades 300 to 299.9994 anyway, because PNG stores pixels per metre as an
    integer. That same read-back value is what ``crop_to_paper_size`` uses for
    its crop arithmetic, so the crop shape and the MediaBox cannot disagree.

    A fixed-DPI layout function applies that DPI to **every** page
    unconditionally, so a page's size in points is determined entirely by its
    pixel count. That is correct here because one pipeline run scans every page
    at one resolution: a half-size raster becomes a half-size page rather than
    being rescaled to match its neighbours.

    **Memory is bounded by converting one page at a time.** Each page gets its
    own ``img2pdf.convert(..., outputstream=...)`` call into its own single-page
    PDF in a scratch directory, and qpdf merges those through
    :class:`pikepdf.Job`, copying each page's streams lazily. Measured, not
    assumed, on 48 synthetic A4 300 DPI colour pages: one ``convert`` whose
    bytes were then written out peaked at 1395 MB, and the same ``convert``
    with ``outputstream=`` at 787 MB -- both still linear in page count,
    because ``convert`` reads every input fully into memory and finalises the
    whole document before ``outputstream`` is written a byte. Per-page convert
    plus the qpdf merge measured flat at **131 MB for both 12 and 48 pages**,
    with byte-identical ``/FlateDecode`` image streams, the same
    ``/MediaBox [0 0 595.2 841.92]`` and the same total wall clock. That is a
    settled answer: the shape is not a tuning knob, it is what makes the memory
    sentence true end to end, and ``outputstream=`` on its own does not.

    The accepted cost is the GIL. ``pikepdf.Job.run()`` holds it for roughly
    12 ms per page, so a 500-page job stalls its thread for about 6 s during
    assembly. That thread is the worker's, already blocked for the whole scan,
    so the visible effect is one briefly frozen status poll -- and the
    alternative is gigabytes of resident memory.

    The single-page PDFs are named from each record's **position in
    ``records``**, not from ``PageRecord.sequence``: sequence numbers are
    assigned per acquisition pass, so after a manual-duplex interleave two
    records legitimately share the number 1. Naming by position keeps the merge
    argv unique and in document order by construction, with nothing sorted and
    nothing globbed (D-02).

    This function is a module boundary that raises only ``PdfError``, and the
    caught type is ``Exception``, **deliberately**. img2pdf raises seven
    unrelated error classes -- each a direct ``Exception`` subclass with no
    shared base -- plus bare ``Exception``, ``TypeError`` and ``ValueError``;
    Pillow raises ``OSError`` and ``SystemError`` while a page is read; and
    ``pikepdf.Job.run()`` raises pikepdf's own exception types, such as
    ``pikepdf.PdfError``, when qpdf refuses a file. So any tuple of types would
    leak whichever one was left off it. The catch is narrow in *span* --
    directory creation, the per-page converts and the merge, nothing else --
    and broad in *type*. pikepdf needed **no new** ``except`` clause for that
    reason: its errors are ordinary ``Exception`` subclasses and this boundary
    already covered them, so EXC-01 is unaffected.
    Nothing is masked: the original is always chained on ``__cause__`` and its
    text kept in the message. ``KeyboardInterrupt`` and ``SystemExit`` derive
    from ``BaseException`` and pass through untouched.

    Args:
        records: The spooled pages to include, in document order. Must not be
            empty. Each record's PNG is embedded exactly as the spool wrote
            it, losslessly and with no re-encode.
        output_dir: Directory where the output PDF will be written.
        filename: File name for the PDF, including its ``.pdf`` extension.
            Must be a single path segment; :func:`build_pdf_filename`
            guarantees that.
        dpi: Resolution the pages were actually scanned at, as read back from
            the device. Determines the page size the PDF declares.

    Returns:
        Path to the generated PDF file.

    Raises:
        PdfError: When ``records`` is empty, or when anything goes wrong while
            the PDF is being assembled or written. The message names the page
            count, the target PDF path and the original failure's text.

    """
    if not records:
        # The pipeline's _require_pages already refuses an empty batch; this
        # keeps img2pdf's "Unable to process empty list" ValueError unreachable
        # from any caller of this public function (N-06).
        msg = "Could not assemble a PDF: no pages were given"
        raise PdfError(msg)

    pdf_path = output_dir / filename
    try:
        output_dir.mkdir(parents=True, exist_ok=True)

        # The argument is an (x_dpi, y_dpi) 2-tuple, not a scalar:
        # default_layout_fun unpacks it, and an int silently yields wrong
        # geometry.  Without it img2pdf lays pages out at its default_dpi
        # of 96, turning an A4 page at 300 DPI into a 1860 x 2631 pt monster.
        layout_fun = img2pdf.get_fixed_dpi_layout_fun((dpi, dpi))

        with tempfile.TemporaryDirectory(dir=str(output_dir)) as tmp_dir:
            work_dir = Path(tmp_dir)
            singles: list[str] = []
            for position, record in enumerate(records, start=1):
                single = work_dir / f"{position:04d}.pdf"
                # convert returns None once outputstream= is supplied -- that
                # is its documented contract, not a failure, so there is
                # nothing here to guard against.
                with single.open("wb") as stream:
                    img2pdf.convert(
                        [str(record.path)],
                        layout_fun=layout_fun,
                        outputstream=stream,
                    )
                singles.append(str(single))

            # An in-process qpdf API, not a shell invocation: every element of
            # this argv is a literal or a file this call just wrote inside its
            # own scratch directory, so no operator string reaches it.
            pikepdf.Job(
                ["qpdf", "--empty", "--pages", *singles, "--", str(pdf_path)]
            ).run()

        logger.info("Assembled %d page(s) into %s", len(records), pdf_path)
    except PdfError:
        # Already the boundary's own type: wrapping it again would only
        # repeat the message.
        raise
    except Exception as exc:
        msg = (
            f"Could not assemble {len(records)} page(s) into {pdf_path}: "
            f"{describe(exc)}"
        )
        raise PdfError(msg) from exc

    return pdf_path
