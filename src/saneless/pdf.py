"""
PDF assembly via img2pdf with temporary file handling.

Scanned PIL Images are saved to temporary files on disk (seekable,
required by img2pdf) and assembled into a single PDF. Temporary
image files are cleaned up after assembly or on error via
TemporaryDirectory context manager.
"""

from __future__ import annotations

import logging
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import img2pdf
import PIL.Image
from PIL import Image

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
    images: list[Image.Image],
    output_dir: Path,
    filename: str,
    dpi: int,
) -> Path:
    """
    Assemble PIL Images into a single PDF using img2pdf.

    Images are first saved as PNG files in a temporary directory
    within output_dir, then passed to img2pdf for lossless PDF
    assembly. Temp files are cleaned up automatically.

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

    Args:
        images: List of PIL Image objects to include in the PDF.
        output_dir: Directory where the output PDF will be written.
        filename: File name for the PDF, including its ``.pdf`` extension.
            Must be a single path segment; :func:`build_pdf_filename`
            guarantees that.
        dpi: Resolution the pages were actually scanned at, as read back from
            the device. Determines the page size the PDF declares.

    Returns:
        Path to the generated PDF file.

    """
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=str(output_dir)) as tmp_dir:
        image_paths: list[str] = []
        for i, img in enumerate(images):
            img_path = Path(tmp_dir) / f"page_{i:04d}.png"
            img.save(str(img_path), format="PNG")
            image_paths.append(str(img_path))
            logger.debug("Saved page %d to %s", i, img_path)

        pdf_path = output_dir / filename
        # The argument is an (x_dpi, y_dpi) 2-tuple, not a scalar:
        # default_layout_fun unpacks it, and an int silently yields wrong
        # geometry.  Without it img2pdf lays pages out at its default_dpi of
        # 96, turning an A4 page at 300 DPI into a 1860 x 2631 pt monster.
        pdf_bytes = img2pdf.convert(
            image_paths,
            layout_fun=img2pdf.get_fixed_dpi_layout_fun((dpi, dpi)),
        )
        if pdf_bytes is None:
            msg = "img2pdf.convert returned None"
            raise RuntimeError(msg)
        pdf_path.write_bytes(pdf_bytes)
        logger.info("Assembled %d page(s) into %s", len(images), pdf_path)

    return pdf_path
