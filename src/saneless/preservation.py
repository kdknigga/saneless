"""
Keep a failed run's pages: the one place that knows how.

A scan that fails after paper has gone through the feeder must not lose the
pages it already has. This module moves whatever a failed run produced into
``<data_dir>/failed/``, privately, and says in one sentence what survived. The
pipeline's guard calls it when a run fails, and the startup sweep calls it for
a workspace a killed process left behind.

What is kept is the *most finished* artefact that exists for the stage the run
reached (see ``preserve_most_finished``): PDFs already assembled, else the
unfiltered document as one PDF, else each spooled pass as its own PDF, else the
raw page files. Every PDF built here first passes ``ensure_room_to_assemble``,
so preserving a scan cannot itself fill the disk.

Nothing here ever deletes, prunes or rotates anything in ``failed/``: every file
in it is a document that reached paper and never reached paperless-ngx.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from saneless.atomic_write import refused_mode_change
from saneless.exceptions import DiskSpaceError, PdfError, describe
from saneless.pdf import assemble_pdf, build_pdf_filename
from saneless.private_dirs import make_private_dir
from saneless.spool import BYTES_PER_MB

# Re-exported for every caller that names a pass by its title suffix.
from saneless.vocabulary import (
    BACKS_SUFFIX,
    FRONTS_SUFFIX,
    PARTIAL_SUFFIX,
    half_title,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from saneless.scanner.base import PageRecord

__all__ = [
    "BACKS_SUFFIX",
    "FAILED_DIR_WARN_THRESHOLD",
    "FRONTS_SUFFIX",
    "PARTIAL_SUFFIX",
    "PRESERVED_DIR_NAME",
    "KeptGroup",
    "KeptKind",
    "PreservationReport",
    "RunArtefacts",
    "RunStage",
    "best_effort_chmod",
    "build_pass_pdf",
    "ensure_room_to_assemble",
    "make_failed_dir",
    "move_page_files",
    "move_private",
    "preserve_most_finished",
    "warn_if_failed_dir_growing",
]

logger = logging.getLogger(__name__)

# The workspace subdirectory a preserved PDF is assembled in, kept apart so a
# preservation can never collide with the document the run was delivering.
PRESERVED_DIR_NAME: Final = "preserved"

FAILED_DIR_WARN_THRESHOLD = 20
"""
How many preserved scans make ``<data_dir>/failed/`` worth mentioning in the log.

An attention threshold, not a retention policy: reaching it only logs a
WARNING, because a preserved file is the only remaining copy of a document.
"""

# A preserved scan is a whole document, so only its owner may read it.
_PRIVATE_FILE_MODE: Final = 0o600

# Why a hard link can fail where a copy still works: the two paths are on
# different filesystems, or the filesystem has no hard links at all (vfat, some
# FUSE and network mounts refuse with EPERM or ENOTSUP), or the file already
# has as many links as it may.
_COPY_INSTEAD_OF_LINK: Final = frozenset(
    {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK}
)

# How many numbered names a PDF may try in failed/ before giving up; the bound
# only stops a directory that refuses every name from looping forever.
_MAX_NAME_ATTEMPTS: Final = 100


def make_failed_dir(failed_dir: Path) -> None:
    """
    Create ``failed_dir`` and the ``data_dir`` it sits in, each owner-only.

    Each level gets its own call because ``mkdir`` with ``parents=True``
    creates a missing parent with the default permissions.  A directory that
    already exists keeps its mode.

    Args:
        failed_dir: The durable directory preserved scans go in, directly
            inside ``data_dir``.

    Raises:
        OSError: If either directory cannot be created.

    """
    make_private_dir(failed_dir.parent)
    make_private_dir(failed_dir)


def best_effort_chmod(path: Path, mode: int) -> None:
    """
    Set ``path``'s permission bits, tolerating a filesystem without them.

    A refused change (for example on a vfat or CIFS ``tmp_dir``) is logged at
    DEBUG and skipped: failing here would lose the very scan being preserved.

    Args:
        path: The file to change.
        mode: The permission bits to give it.

    Raises:
        OSError: If the change failed for a reason other than a refusal.

    """
    try:
        path.chmod(mode)
    except OSError as exc:
        if not refused_mode_change(exc):
            raise
        logger.debug("Not setting the mode of %s: %s", path, exc.strerror)


def move_private(source: Path, destination: Path) -> None:
    """
    Move one file to ``destination``, privately, and never over another file.

    A file already at ``destination`` is somebody's document too, so it is
    never replaced.

    On one filesystem the move is a hard link, which refuses an existing
    destination and keeps the source's mode, then removing the source.
    Otherwise the copy is made into a file created 0600 with ``O_EXCL``,
    never through ``shutil.move``, whose copy is readable under the umask's
    mode until it finishes.  A failed copy is removed; a source that cannot be
    removed afterwards is logged and left, which loses nothing.

    Args:
        source: The file to move, inside the job workspace.
        destination: Its full path in the durable directory, never the bare
            directory.

    Raises:
        FileExistsError: If something is already at ``destination``; nothing
            was moved.
        OSError: If the file could be neither linked nor copied.  A
            ``shutil.Error`` from the copy is an ``OSError`` too.

    """
    try:
        os.link(source, destination)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in _COPY_INSTEAD_OF_LINK:
            raise
    else:
        _remove_moved_source(source, destination)
        return
    descriptor = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _PRIVATE_FILE_MODE
    )
    try:
        with os.fdopen(descriptor, "wb") as copy, source.open("rb") as original:
            shutil.copyfileobj(original, copy)
            copy.flush()
            os.fsync(copy.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    _remove_moved_source(source, destination)


def _remove_moved_source(source: Path, destination: Path) -> None:
    """
    Remove a moved file's source, once the file is whole at its destination.

    A failure is logged, not raised, so the report never says a file already
    kept could not be kept.
    """
    try:
        source.unlink()
    except OSError:
        logger.warning(
            "%s is kept at %s, but the original could not be removed; it is "
            "a spare copy",
            source,
            destination,
            exc_info=True,
        )


def warn_if_failed_dir_growing(failed_dir: Path) -> None:
    """
    Log one WARNING when preserved scans have piled up in ``failed_dir``.

    Warn only: every file in that directory is a document that never reached
    paperless-ngx, so nothing in saneless deletes from it.

    **This function must never raise.** It runs while a run's failure is in
    flight, and a raise would replace the real failure; any filesystem trouble
    ends the check silently.

    A page-file directory counts as one preserved scan, with its whole
    recursive size, beside the PDFs.

    Args:
        failed_dir: The directory preserved scans are moved into.

    """
    try:
        preserved = list(failed_dir.glob("*.pdf"))
        # Inside the try: a page directory removed underneath the walk raises
        # OSError and must leave through the same silent return.
        page_dirs = [entry for entry in failed_dir.iterdir() if entry.is_dir()]
        if len(preserved) + len(page_dirs) < FAILED_DIR_WARN_THRESHOLD:
            return
        total_bytes = sum(pdf.stat().st_size for pdf in preserved)
        total_bytes += sum(
            page.stat().st_size
            for page_dir in page_dirs
            for page in page_dir.rglob("*")
            if page.is_file()
        )
    except OSError:
        return
    logger.warning(
        "%d preserved scans (%.1f MB) have accumulated in %s -- saneless "
        "never deletes these files itself, so draining the directory is yours "
        "to do once those documents are safely in paperless-ngx",
        len(preserved) + len(page_dirs),
        total_bytes / BYTES_PER_MB,
        failed_dir,
    )


def move_page_files(spool_dir: Path, destination: Path, moved: list[Path]) -> None:
    """
    Move every spooled page file into ``destination``.

    The last resort of every preservation, when there is no PDF.  Each page
    moves through ``move_private``, since ``tmp_dir`` and ``data_dir`` may be
    on different filesystems.

    Args:
        spool_dir: The job's spool, inside the workspace about to be unwound.
        destination: The job-keyed directory to move the pages into, created
            only if there is at least one page.
        moved: Appended to as each page lands, in name order.  An
            out-parameter so that, when this raises part way, the caller can
            still say which pages it kept.

    Raises:
        OSError: If the directory cannot be created or a move fails. Whatever
            had already moved stays moved, and stays named in ``moved``.

    """
    page_files = sorted(entry for entry in spool_dir.iterdir() if entry.is_file())
    if not page_files:
        return
    make_failed_dir(destination.parent)
    make_private_dir(destination)
    for page_file in page_files:
        target = destination / page_file.name
        # Owner-only before the move, not after: the source sits in the 0700
        # workspace, and a hard link keeps the mode it has.
        best_effort_chmod(page_file, _PRIVATE_FILE_MODE)
        move_private(page_file, target)
        moved.append(target)
    warn_if_failed_dir_growing(destination.parent)


def _free_bytes(directory: Path) -> int:
    """
    Return the bytes free on the filesystem holding ``directory``.

    The one place the disk rule measures, so a test can give it any number.
    """
    return shutil.disk_usage(directory).free


def _mb_rounded_up(size: int) -> int:
    """
    Return ``size`` bytes in megabytes, rounded up.

    Args:
        size: A number of bytes.

    Returns:
        The smallest whole number of megabytes that holds it.

    """
    return (size + BYTES_PER_MB - 1) // BYTES_PER_MB


def ensure_room_to_assemble(
    records: Sequence[PageRecord], directory: Path, reserve_mb: int
) -> None:
    """
    Refuse, before it starts, an assembly the disk has no room for.

    For a moment the spool, the single-page PDFs and the merged output
    coexist; measured, that peak is twice the spooled pages' size on top of
    the spool, so the rule asks for twice the spool free plus ``reserve_mb``.
    The MB needed is rounded up and the MB free down, so the message never
    shows a need smaller than what is free.

    Args:
        records: The pages about to be assembled.
        directory: The directory the PDF will be written in, which must exist.
        reserve_mb: The ``min_free_space_mb`` reserve to keep free as well.

    Raises:
        DiskSpaceError: If the free space is under twice the spooled pages'
            bytes plus the reserve, naming the MB needed, the MB free and the
            page count.  A full disk, not a PDF the writer refused.
        PdfError: If a page or the directory cannot be measured.

    """
    try:
        spooled = sum(record.path.stat().st_size for record in records)
        free = _free_bytes(directory)
    except OSError as exc:
        msg = (
            f"Could not measure the room to assemble {len(records)} page(s) "
            f"in {directory}: {describe(exc)}"
        )
        raise PdfError(msg) from exc
    need = 2 * spooled + reserve_mb * BYTES_PER_MB
    if free >= need:
        return
    msg = (
        f"Not enough free disk space to assemble {len(records)} page(s) in "
        f"{directory}: {_mb_rounded_up(need)} MB needed (twice the "
        f"{_mb_rounded_up(spooled)} MB of spooled pages plus the {reserve_mb} MB "
        f"min_free_space_mb reserve), {free // BYTES_PER_MB} MB free"
    )
    raise DiskSpaceError(msg)


class RunStage(StrEnum):
    """How far a scan run got, which decides what is worth keeping."""

    ACQUIRING = "ACQUIRING"
    FILTERING = "FILTERING"
    ASSEMBLING = "ASSEMBLING"
    DELIVERING = "DELIVERING"
    DELIVERED = "DELIVERED"


@dataclass
class RunArtefacts:
    """
    What a scan run has produced so far, for ``preserve_most_finished``.

    Mutable on purpose: the run updates ``stage``, ``passes``, ``document``
    and ``pdfs`` as it goes, so whatever fails finds the latest state.

    Attributes:
        job_id: The job's id; every preserved name is keyed on it.
        title: The operator's title, which preserved names and PDF titles
            carry.
        workspace: The job's workspace. Preserved PDFs are built in its
            ``PRESERVED_DIR_NAME`` subdirectory and must leave it before the
            workspace is removed.
        spool_dir: The directory holding the spooled page files.
        failed_dir: The durable directory everything kept goes in.
        reserve_mb: The ``min_free_space_mb`` reserve the disk rule keeps.
        stage: How far the run got.
        passes: One ``(title suffix, records)`` pair per acquisition pass, in
            pass order, with pass B's records in the order it scanned them.
        document: Every page accepted into the document, in document order,
            unfiltered; None while there is none.  A run that accepts passes
            into it one at a time sets it during acquisition, and ``passes``
            then holds only the pass in flight.
        pdfs: Assembled PDFs not yet delivered, still in the workspace.
        accepted: Of those PDFs, each one paperless-ngx took or may have
            taken, with the words that say which.  It is still kept, but the
            sentence stops anyone uploading it again unchecked.
        unreadable_sheets: How many sheets the finished passes reported they
            could not read.  Zero means none is known, not that none was
            skipped.

    """

    job_id: str
    title: str
    workspace: Path
    spool_dir: Path
    failed_dir: Path
    reserve_mb: int
    stage: RunStage = RunStage.ACQUIRING
    passes: list[tuple[str, tuple[PageRecord, ...]]] = field(default_factory=list)
    document: tuple[PageRecord, ...] | None = None
    pdfs: list[Path] = field(default_factory=list)
    accepted: dict[Path, str] = field(default_factory=dict)
    unreadable_sheets: int = 0


class KeptKind(StrEnum):
    """What kind of artefact a preservation kept, most finished first."""

    ASSEMBLED = "ASSEMBLED"
    DOCUMENT = "DOCUMENT"
    PASSES = "PASSES"
    PAGE_FILES = "PAGE_FILES"
    NOTHING = "NOTHING"


@dataclass(frozen=True)
class KeptGroup:
    """
    One kind of artefact that reached ``failed/``.

    Attributes:
        kind: What was kept.
        paths: Where each kept file or directory now is, in the order kept.
            For a complete set of page files it is the one directory holding
            them; otherwise each file.
        count: How many pages (or, for assembled PDFs, how many PDFs) this
            group holds.
        out_of: How many there were to keep, when not all of them were kept;
            None when the group is complete.

    """

    kind: KeptKind
    paths: tuple[Path, ...]
    count: int
    out_of: int | None = None

    def sentence(self) -> str:
        """
        Say what this group kept, with every path directly after "preserved at ".

        Returns:
            The sentence, without a closing full stop.

        Raises:
            ValueError: If the group's kind is NOTHING, which no group holds.

        """
        where = ", ".join(str(path) for path in self.paths)
        part = f"{self.count} of the {self.out_of}" if self.out_of else None
        match self.kind:
            case KeptKind.ASSEMBLED:
                if part:
                    return f"{part} assembled PDF(s) were preserved at {where}"
                return f"The scan was preserved at {where}"
            case KeptKind.DOCUMENT:
                return f"The {self.count} scanned page(s) were preserved at {where}"
            case KeptKind.PASSES:
                pages = part or f"The {self.count}"
                return (
                    f"{pages} page(s) scanned before the error were preserved "
                    f"at {where}"
                )
            case KeptKind.PAGE_FILES:
                pages = part or f"The {self.count}"
                return f"{pages} spooled page file(s) were preserved at {where}"
            case KeptKind.NOTHING:
                # The report never builds a group of this kind.
                msg = "A kept group cannot be of kind NOTHING"
                raise ValueError(msg)


@dataclass
class PreservationReport:
    """
    What a preservation kept and what went wrong on the way.

    Built as the preservation goes, so it names exactly what reached
    ``failed/`` even when a later step fails.

    Attributes:
        failed_dir: The durable directory everything kept went in.
        groups: The artefacts kept, most finished first.
        problems: What failed, one short description each, in order.
        cautions: What the operator must check before using a kept file, one
            sentence each: a kept PDF paperless-ngx had already taken.

    """

    failed_dir: Path
    groups: list[KeptGroup] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)

    @property
    def kind(self) -> KeptKind:
        """
        The most finished kind of artefact kept.

        Returns:
            The first group's kind, or NOTHING when nothing was kept.

        """
        return self.groups[0].kind if self.groups else KeptKind.NOTHING

    @property
    def kept_paths(self) -> list[Path]:
        """
        Every kept file or directory, in the order it was kept.

        Returns:
            The paths of every group, flattened.

        """
        return [path for group in self.groups for path in group.paths]

    def sentence(self) -> str | None:
        """
        Say what survived, truthfully, in one line.

        Every kept path follows the words "preserved at ", which is also what
        lets the web UI recognise the path as a preserved file. "could NOT be
        preserved" is said only when nothing at all was kept; anything that
        went wrong on the way follows in parentheses.

        Returns:
            The sentence, without a closing full stop, or None when there was
            no page to keep.

        """
        problems = "; ".join(self.problems)
        if not self.groups:
            if not problems:
                return None
            return f"The scan could NOT be preserved to {self.failed_dir}: {problems}"
        kept = ". ".join(group.sentence() for group in self.groups)
        if problems:
            kept = f"{kept} ({problems})"
        return ". ".join([kept, *self.cautions])


def _build_pdf(
    artefacts: RunArtefacts, records: Sequence[PageRecord], suffix: str = ""
) -> Path:
    """
    Assemble ``records`` into one PDF in the workspace, if there is room.

    ``suffix``, such as ``FRONTS_SUFFIX``, is appended to the title and is a
    segment of its own in the file name, where a long title cannot cut it off.
    """
    ensure_room_to_assemble(records, artefacts.workspace, artefacts.reserve_mb)
    title = half_title(artefacts.title, suffix) if suffix else artefacts.title
    return assemble_pdf(
        records,
        artefacts.workspace / PRESERVED_DIR_NAME,
        filename=build_pdf_filename(artefacts.job_id, artefacts.title, part=suffix),
        title=title,
    )


def build_pass_pdf(
    artefacts: RunArtefacts, suffix: str, records: Sequence[PageRecord]
) -> Path:
    """
    Assemble one acquisition pass into its own PDF, unfiltered.

    Pass B runs over the flipped stack, so it scans the last sheet's back
    first; a ``(backs)`` pass is reversed here so the PDF runs in sheet order.
    Nothing is blank-filtered: an anomaly is kept whole for a person to look
    at.

    Args:
        artefacts: The run, for its workspace, title, job id and reserve.
        suffix: The pass's title suffix, ``PARTIAL_SUFFIX``, ``FRONTS_SUFFIX``
            or ``BACKS_SUFFIX``.
        records: The pass's pages, in the order it scanned them.

    Returns:
        The assembled PDF, still inside the workspace.

    Raises:
        DiskSpaceError: If the disk rule refuses, or the assembly runs out
            of space.
        PdfError: If the assembly fails for any other reason.

    """
    ordered = list(reversed(records)) if suffix == BACKS_SUFFIX else list(records)
    return _build_pdf(artefacts, ordered, suffix)


def _keep_file(pdf: Path, failed_dir: Path) -> Path:
    """
    Move one PDF into ``failed_dir``, owner-only, beside whatever is there.

    A file already holding the PDF's name is never replaced: the PDF takes
    the next free numbered name instead (``<name>-2.pdf``, ``-3`` and so on).

    Raises:
        FileExistsError: If every numbered name is taken.

    """
    # Owner-only before the move, for the reason ``move_page_files`` gives.
    best_effort_chmod(pdf, _PRIVATE_FILE_MODE)
    for attempt in range(1, _MAX_NAME_ATTEMPTS + 1):
        name = pdf.name if attempt == 1 else f"{pdf.stem}-{attempt}{pdf.suffix}"
        destination = failed_dir / name
        try:
            move_private(pdf, destination)
        except FileExistsError:
            continue
        return destination
    msg = f"No free name for {pdf.name} in {failed_dir}"
    raise FileExistsError(errno.EEXIST, msg)


def _discard_unkept(pdf: Path | None) -> None:
    """
    Delete a PDF built for ``failed/`` that could not be moved there.

    It is only a copy of pages still in the spool, and left behind it would
    gain another whole copy on every sweep that failed the same way.  Never
    called for an assembled PDF from a delivery, which can be the only copy.
    """
    if pdf is None:
        return
    try:
        pdf.unlink(missing_ok=True)
    except OSError:
        logger.warning(
            "Could not delete %s, a PDF that could not be kept", pdf, exc_info=True
        )


def _record_problem(report: PreservationReport, what: str, exc: Exception) -> None:
    """
    Note a failure in the report and log it with its traceback.

    Args:
        report: The report being built.
        what: What was being attempted, to start the description with.
        exc: What went wrong.

    """
    logger.warning("While preserving a failed scan, %s failed", what, exc_info=exc)
    report.problems.append(f"{what} failed: {describe(exc)}")


def _keep_assembled(artefacts: RunArtefacts, report: PreservationReport) -> bool:
    """
    Move the assembled, undelivered PDFs into ``failed/``.

    Args:
        artefacts: The run, for its PDFs and ``failed/``.
        report: Where the kept PDFs and any failure are recorded.

    Returns:
        True when every PDF was kept.

    """
    pdfs = [pdf for pdf in artefacts.pdfs if pdf.exists()]
    kept: list[Path] = []
    try:
        make_failed_dir(artefacts.failed_dir)
        for pdf in pdfs:
            # Recorded as each lands, so a later failure still names it.
            destination = _keep_file(pdf, artefacts.failed_dir)
            kept.append(destination)
            how = artefacts.accepted.get(pdf)
            if how is not None:
                report.cautions.append(
                    f"{destination.name} {how}, so check paperless-ngx before "
                    f"uploading it again"
                )
    except Exception as exc:
        _record_problem(report, "moving the assembled PDF(s)", exc)
    # A taken PDF that could not be kept here is kept as page files, and
    # uploading those would make the same duplicate, so the caution goes too.
    for pdf in pdfs[len(kept) :]:
        how = artefacts.accepted.get(pdf)
        if how is not None:
            report.cautions.append(
                f"{pdf.name} {how}, so check paperless-ngx before uploading its "
                f"pages again"
            )
    if kept:
        out_of = len(pdfs) if len(kept) < len(pdfs) else None
        report.groups.append(
            KeptGroup(KeptKind.ASSEMBLED, tuple(kept), len(kept), out_of)
        )
    return len(kept) == len(pdfs)


def _keep_document(artefacts: RunArtefacts, report: PreservationReport) -> bool:
    """
    Keep the unfiltered document as one PDF in ``failed/``.

    Args:
        artefacts: The run, for its document and ``failed/``.
        report: Where the kept PDF and any failure are recorded.

    Returns:
        True when the PDF was kept.

    """
    document = artefacts.document or ()
    pdf: Path | None = None
    try:
        pdf = _build_pdf(artefacts, document)
        make_failed_dir(artefacts.failed_dir)
        kept = _keep_file(pdf, artefacts.failed_dir)
    except Exception as exc:
        _record_problem(report, "keeping the unfiltered document as a PDF", exc)
        _discard_unkept(pdf)
        return False
    report.groups.append(KeptGroup(KeptKind.DOCUMENT, (kept,), len(document)))
    return True


def _keep_document_and_passes_in_flight(
    artefacts: RunArtefacts, report: PreservationReport
) -> bool:
    """
    Keep the document accepted so far, and beside it every pass still in flight.

    Both are attempted whatever happens to the other.  False sends the caller
    to the page files, which hold both.
    """
    document_kept = _keep_document(artefacts, report)
    if not any(records for _, records in artefacts.passes):
        return document_kept
    passes_kept = _keep_passes(artefacts, report)
    return document_kept and passes_kept


def _keep_passes(artefacts: RunArtefacts, report: PreservationReport) -> bool:
    """
    Keep each non-empty acquisition pass as its own PDF in ``failed/``.

    The passes are one document between them, so the first failure stops the
    rest: the caller then keeps the page files, which hold every pass.

    Args:
        artefacts: The run, for its passes and ``failed/``.
        report: Where the kept PDFs and any failure are recorded.

    Returns:
        True when every non-empty pass was kept.

    """
    passes = [(suffix, records) for suffix, records in artefacts.passes if records]
    total = sum(len(records) for _, records in passes)
    kept: list[Path] = []
    pages = 0
    complete = True
    pdf: Path | None = None
    try:
        make_failed_dir(artefacts.failed_dir)
        for suffix, records in passes:
            pdf = build_pass_pdf(artefacts, suffix, records)
            kept.append(_keep_file(pdf, artefacts.failed_dir))
            pdf = None
            pages += len(records)
    except Exception as exc:
        _record_problem(report, "keeping the scanned passes as PDFs", exc)
        _discard_unkept(pdf)
        complete = False
    fronts = next(
        (records for suffix, records in passes if suffix == FRONTS_SUFFIX), ()
    )
    backs = next((records for suffix, records in passes if suffix == BACKS_SUFFIX), ())
    pairing = _pairing_caution(len(fronts), len(backs), artefacts.unreadable_sheets)
    if complete and pairing is not None:
        report.cautions.append(pairing)
    if kept:
        out_of = total if pages < total else None
        report.groups.append(KeptGroup(KeptKind.PASSES, tuple(kept), pages, out_of))
    return complete


def _pairing_caution(fronts: int, backs: int, unreadable: int) -> str | None:
    """
    Say how the kept ``(backs)`` pages pair with the ``(fronts)``, if needed.

    Pairing by page number holds only while every sheet was fed exactly once.
    An unreadable sheet breaks it for certain, so no page is named then; the
    fewer backs of a stopped pass B are named only on condition, since no
    pass reports a sheet it missed or fed twice.
    """
    if not backs:
        return None
    if unreadable:
        return (
            f"{unreadable} sheet(s) could not be read, so the {BACKS_SUFFIX} "
            f"pages cannot be paired with the {FRONTS_SUFFIX} pages by page "
            f"number: match them by what is on the pages"
        )
    if backs >= fronts:
        return None
    return (
        f"Pass B feeds the stack from its last sheet, so if every sheet was fed "
        f"exactly once, the {backs} {BACKS_SUFFIX} page(s) are the backs of the "
        f"last {backs} of the {fronts} sheets and {BACKS_SUFFIX} page 1 goes "
        f"with {FRONTS_SUFFIX} page {fronts - backs + 1}; a sheet skipped, "
        f"missed or fed twice shifts that, so check by what is on the pages"
    )


def _keep_page_files(artefacts: RunArtefacts, report: PreservationReport) -> None:
    """
    Keep the spooled page files in a job-keyed directory under ``failed/``.

    The directory's name is ``build_pdf_filename``'s, without the extension,
    so it inherits that function's uniqueness: two failures cannot bury their
    pages in one directory.

    Args:
        artefacts: The run, for its spool, job id, title and ``failed/``.
        report: Where the kept pages and any failure are recorded.

    """
    destination = (
        artefacts.failed_dir
        / Path(build_pdf_filename(artefacts.job_id, artefacts.title)).stem
    )
    moved: list[Path] = []
    total = 0
    try:
        if not artefacts.spool_dir.is_dir():
            return
        total = sum(1 for entry in artefacts.spool_dir.iterdir() if entry.is_file())
        move_page_files(artefacts.spool_dir, destination, moved)
    except Exception as exc:
        _record_problem(report, f"moving the page files into {destination}", exc)
    if not moved:
        return
    if len(moved) < total:
        report.groups.append(
            KeptGroup(KeptKind.PAGE_FILES, tuple(moved), len(moved), total)
        )
    else:
        report.groups.append(KeptGroup(KeptKind.PAGE_FILES, (destination,), total))


def preserve_most_finished(artefacts: RunArtefacts) -> PreservationReport:
    """
    Keep the most finished artefact a failed run produced, and report it.

    The order, keyed on ``artefacts.stage``:

    1. ``DELIVERED``: nothing; the document reached paperless-ngx.
    2. ``DELIVERING`` with assembled PDFs: move them, owner-only.
    3. ``ASSEMBLING``: straight to the page files, since building another PDF
       would fail the same way.
    4. ``ACQUIRING`` with ``document`` set: the accepted document as one PDF,
       and then each non-empty pass in flight as its own ``(partial)`` PDF,
       because those pages are not part of the document yet.
    5. Otherwise, once acquisition has finished (``document`` is set): the
       unfiltered document, in document order, as one PDF.
    6. Otherwise each non-empty pass as its own PDF, the ``(backs)`` pass in
       sheet order.

    When a PDF cannot be built or moved, or the disk rule refuses, the raw
    page files are kept as well, even if that keeps a pass twice: a redundant
    copy is cheap, and guessing which half of a broken job to drop is not.

    **This never raises an ``Exception``.** It runs while the run's own
    failure is in flight, so every failure here is logged with its traceback
    and folded into the report.  ``KeyboardInterrupt`` and ``ScanInterrupted``
    are not ``Exception``s and pass straight through.

    Args:
        artefacts: What the run produced and how far it got.

    Returns:
        What was kept and what went wrong; its ``sentence()`` is None when the
        run had no page to keep.

    """
    report = PreservationReport(failed_dir=artefacts.failed_dir)
    if artefacts.stage is RunStage.DELIVERED:
        return report
    kept_as_pdf = False
    if artefacts.stage is RunStage.DELIVERING and any(
        pdf.exists() for pdf in artefacts.pdfs
    ):
        kept_as_pdf = _keep_assembled(artefacts, report)
    elif artefacts.stage is RunStage.ASSEMBLING:
        pass
    elif artefacts.stage is RunStage.ACQUIRING and artefacts.document:
        kept_as_pdf = _keep_document_and_passes_in_flight(artefacts, report)
    elif artefacts.document:
        kept_as_pdf = _keep_document(artefacts, report)
    elif any(records for _, records in artefacts.passes):
        kept_as_pdf = _keep_passes(artefacts, report)
    if not kept_as_pdf:
        _keep_page_files(artefacts, report)
    if report.groups:
        warn_if_failed_dir_growing(artefacts.failed_dir)
    return report
