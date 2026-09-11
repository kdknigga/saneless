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


def assemble_pdf(images: list[Image.Image], output_dir: Path) -> Path:
    """
    Assemble PIL Images into a single PDF using img2pdf.

    Images are first saved as PNG files in a temporary directory
    within output_dir, then passed to img2pdf for lossless PDF
    assembly. Temp files are cleaned up automatically.

    Args:
        images: List of PIL Image objects to include in the PDF.
        output_dir: Directory where the output PDF will be written.

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

        pdf_path = output_dir / "output.pdf"
        pdf_bytes = img2pdf.convert(image_paths)
        if pdf_bytes is None:
            msg = "img2pdf.convert returned None"
            raise RuntimeError(msg)
        pdf_path.write_bytes(pdf_bytes)
        logger.info("Assembled %d page(s) into %s", len(images), pdf_path)

    return pdf_path
