"""
PDF assembly via img2pdf, over the pages the spool already wrote.

Each page is already a PNG the spool wrote, and img2pdf embeds that file
losslessly with no second encode, so this module owns no page's lifetime.
Assembly converts one page at a time and merges the single-page PDFs with qpdf,
so peak memory does not grow with page count.
See docs/explanation/decisions/0006-per-page-pdf-and-qpdf-merge.md.
"""

from __future__ import annotations

import logging
import re
import tempfile
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING

import img2pdf
import pikepdf

from saneless.exceptions import DiskSpaceError, PdfError, describe, is_out_of_space

if TYPE_CHECKING:
    from collections.abc import Sequence

    from saneless.scanner.base import PageRecord

# Everything outside the allow-list becomes a separator.
_NON_SLUG_CHARACTERS = re.compile(r"[^a-z0-9]+")

# Cap on the title slug; the timestamp and job-id segments are fixed
# width, keeping the composed name far below NAME_MAX.
_MAX_SLUG_LENGTH = 60

# A uuid4 prefix long enough that a collision needs ~2^16 jobs in one second.
_JOB_ID_LENGTH = 8

# Cap on the part segment ("fronts", "backs", "partial"), which is saneless's
# own word and never truncated by the title's cap.
_MAX_PART_LENGTH = 16

__all__ = ["assemble_pdf", "build_pdf_filename", "sanitise_title_for_filename"]

logger = logging.getLogger(__name__)


def _producer() -> str:
    """
    Name saneless and its installed version, for every PDF's ``/Producer``.

    Resolved once at import, outside assembly's catch-all, so a source tree
    with no installed distribution cannot fail every scan as an assembly error.
    """
    try:
        return f"saneless {version('saneless')}"
    except PackageNotFoundError:
        return "saneless"


_PRODUCER = _producer()


def sanitise_title_for_filename(title: str) -> str:
    r"""
    Reduce a user-supplied title to a safe single path segment.

    The rule is an **allow-list**, deliberately: everything outside
    ``[a-z0-9]`` collapses to a single ``-``, so ``/``, ``\``, ``..``, ``~``
    and ``\x00`` are unrepresentable by construction.  The result is joined
    onto ``consume_dir`` and ``<data_dir>/failed/``.

    ``auto_profiles._slugify`` must not be reused: it has no length cap and
    turns an empty result into a placeholder name.

    The cap keeps the whole composed name under ``NAME_MAX`` and under
    eCryptfs's stricter 143-byte limit.  A title with nothing allow-listed in
    it returns the empty string, and the caller drops the segment rather than
    invent a name the operator never typed.

    Args:
        title: The title as typed by the operator. Wholly untrusted.

    Returns:
        A lowercase ``[a-z0-9-]`` slug of at most 60 characters, with no
        leading, trailing or doubled ``-``; or ``""`` if nothing survived.

    """
    slug = _NON_SLUG_CHARACTERS.sub("-", title.lower()).strip("-")
    # Cap first, then strip again: the cut can land mid-separator.
    return slug[:_MAX_SLUG_LENGTH].strip("-")


def build_pdf_filename(job_id: str, title: str, *, part: str = "") -> str:
    """
    Compose the unique file name for one job's assembled PDF.

    The shape is ``{YYYYmmdd-HHMMSS}-{job id}-{title slug}-{part}.pdf``, with
    any of the last three segments dropped entirely when it sanitises away, so
    the name never carries a dangling ``-`` before its extension.

    ``part`` is a segment of its own, after the title slug, because a suffix
    inside the slug would be cut off a long title, giving both halves of a
    duplex job one name.

    **Uniqueness comes from the job id, not from the timestamp**, which only
    makes a directory listing sort usefully.  A clash matters: preservation
    never replaces a file in ``failed/``, so a clashing PDF lands under a
    numbered name that no longer says which job it was.  The id prefix makes
    that a 1-in-4-billion coincidence per same-second same-title pair, not an
    impossibility.

    The job id is a path segment like any other, so it goes through the same
    sanitiser as the title.

    Args:
        job_id: The job's identifier, a uuid4.  When empty the segment is
            dropped, and with it the uniqueness this function promises.
        title: The title as typed by the operator. Wholly untrusted.
        part: Which of the job's PDFs this is, for example ``(fronts)``;
            sanitised like the title and never truncated by the title's cap.
            Empty for the job's one document.

    Returns:
        A single ``.pdf`` file name -- never a path, and never a name that
        escapes the directory it is joined onto.

    """
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    job_segment = sanitise_title_for_filename(job_id)[:_JOB_ID_LENGTH].strip("-")
    part_segment = sanitise_title_for_filename(part)[:_MAX_PART_LENGTH].strip("-")
    segments = [
        segment
        for segment in (
            timestamp,
            job_segment,
            sanitise_title_for_filename(title),
            part_segment,
        )
        if segment
    ]
    return "-".join(segments) + ".pdf"


def assemble_pdf(
    records: Sequence[PageRecord],
    output_dir: Path,
    filename: str,
    *,
    title: str,
) -> Path:
    """
    Assemble spooled pages into a single PDF using img2pdf.

    The order of ``records`` is the document order and the only source of it:
    the spool directory is never sorted or globbed, because after a
    manual-duplex interleave its file names do not sort into document order.

    ``filename`` is required, so each caller names its own file; build it with
    :func:`build_pdf_filename`.

    **Each page is laid out at its own record's ``dpi``**, the resolution the
    device read back, which ``crop_to_paper_size`` also used, so the crop and
    the MediaBox cannot disagree.  The PNG's pHYs chunk is deliberately not
    read: PNG stores pixels per metre as an integer, so it degrades 300 to
    299.9994.

    **Memory stays flat as page count grows** because each page gets its own
    ``img2pdf.convert(..., outputstream=...)`` call and qpdf merges the
    single-page PDFs lazily; ``outputstream=`` alone is not enough.
    See docs/explanation/decisions/0006-per-page-pdf-and-qpdf-merge.md.

    The accepted cost is the GIL. ``pikepdf.Job.run()`` holds it for roughly
    12 ms per page, so a 500-page job stalls every Python thread in the
    process for about 6 s during assembly: web requests and the health
    endpoint wait, not only the job's own thread.

    **The document describes itself.**  ``/Info`` carries ``/Title``,
    ``/Producer`` and ``/Creator``.  Only the first single-page PDF is
    converted with that metadata, and it is qpdf's primary input rather than
    ``--empty``, which was measured to leave ``/Info`` empty.

    **A recovered merge is not silent.**  When qpdf had to repair an input
    (``pikepdf.Job.has_warnings``), the PDF is still returned, but one
    WARNING names the page count and the PDF.

    The single-page PDFs are named by position in ``records``, not by
    ``PageRecord.sequence``, which repeats across the passes of a manual
    duplex.

    The catch is ``Exception``, **deliberately**, narrow in span and broad in
    type: img2pdf, Pillow and pikepdf raise many unrelated types with no
    shared base, so any tuple would leak one.  A failure whose chain holds an
    out-of-space ``OSError`` leaves as ``DiskSpaceError``; every other one as
    ``PdfError``, chained on ``__cause__``.

    Args:
        records: The spooled pages to include, in document order. Must not be
            empty.
        output_dir: Directory where the output PDF will be written.
        filename: File name for the PDF, including its ``.pdf`` extension.
            Must be a single path segment.
        title: The document's title, written to ``/Info`` as ``/Title``.

    Returns:
        Path to the generated PDF file.

    Raises:
        DiskSpaceError: When assembling or writing the PDF runs out of space
            or quota. The message names the page count, the target PDF path
            and the original failure's text.
        PdfError: When ``records`` is empty, or when anything else goes wrong
            while the PDF is being assembled or written. The message names
            the page count, the target PDF path and the original failure's
            text.

    """
    if not records:
        # Keeps img2pdf's "Unable to process empty list" ValueError unreachable
        # from any caller of this public function.
        msg = "Could not assemble a PDF: no pages were given"
        raise PdfError(msg)

    pdf_path = output_dir / filename
    try:
        output_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(dir=str(output_dir)) as tmp_dir:
            work_dir = Path(tmp_dir)
            # Only the first page carries the document metadata: that file is
            # the merge's primary input, and qpdf keeps the primary's /Info.
            metadata = {
                "title": title,
                "producer": _PRODUCER,
                "creator": "saneless",
            }
            singles: list[str] = []
            for position, record in enumerate(records, start=1):
                single = work_dir / f"{position:04d}.pdf"
                page_metadata = metadata if position == 1 else {}
                # An (x_dpi, y_dpi) 2-tuple, not a scalar: an int silently
                # yields wrong geometry.
                layout_fun = img2pdf.get_fixed_dpi_layout_fun((record.dpi, record.dpi))
                with single.open("wb") as stream:
                    img2pdf.convert(
                        [str(record.path)],
                        layout_fun=layout_fun,
                        outputstream=stream,
                        **page_metadata,
                    )
                singles.append(str(single))

            # An in-process qpdf API, not a shell: every argv element is a
            # literal or a file this call just wrote.  The first single is the
            # primary input, so its /Info is kept; the pages come from --pages
            # alone, so it is not merged twice.
            job = pikepdf.Job(
                ["qpdf", singles[0], "--pages", *singles, "--", str(pdf_path)]
            )
            job.run()
            if job.has_warnings:
                logger.warning(
                    "qpdf reported warnings while merging %d page(s) into %s; "
                    "the PDF may be damaged",
                    len(records),
                    pdf_path,
                )

        logger.info("Assembled %d page(s) into %s", len(records), pdf_path)
    except PdfError, DiskSpaceError:
        # Already a boundary type: wrapping it would repeat the message, or
        # report a full disk as a PDF fault.
        raise
    except Exception as exc:
        msg = (
            f"Could not assemble {len(records)} page(s) into {pdf_path}: "
            f"{describe(exc)}"
        )
        if is_out_of_space(exc):
            raise DiskSpaceError(msg) from exc
        raise PdfError(msg) from exc

    return pdf_path
