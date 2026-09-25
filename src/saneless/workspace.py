"""
Job workspaces: scratch directories named after their job and locked while alive.

A scan's pages live in a scratch directory under ``output.tmp_dir`` until the
PDF is assembled. When the process dies hard -- SIGKILL, the OOM killer, a
power cut -- that directory is all that is left of the scan. An anonymous
``tempfile.TemporaryDirectory`` leaves nothing to recover it by: its name says
nothing about the job, and nothing tells a crashed scan's directory apart from
a live one belonging to another process sharing the same ``tmp_dir``.

:class:`JobWorkspace` replaces it with two properties recovery needs:

* **A name.** The directory is ``job-<first 8 characters of the job id>-<random>``
  and holds a small metadata file with the job id, title and profile, so a
  survivor can be traced back to its job and its pages given a sensible name.
* **A liveness proof.** An exclusive ``flock`` on a lock file inside the
  directory is held for the workspace's whole life. The kernel drops it when
  the holder dies, however it dies, so a lock that can be taken proves the
  owner is gone -- unlike a PID file, whose PID the system may already have
  reused. The directory is created under a staging name, locked, and only then
  renamed to its ``job-*`` name, so a sweeper can never see a live workspace
  that is not yet locked.

There is deliberately **no** ``atexit`` or ``weakref`` finalizer.
``TemporaryDirectory`` registers one, and at interpreter exit it can delete a
workspace a daemon thread is still preserving pages from. Without one, a
process that exits early leaves an orphan for the next sweep instead.

:func:`find_orphans` is the discovery half: it returns the ``job-*``
directories whose lock is free, each still holding that lock so two sweepers
never recover the same workspace. :func:`sweep_orphans` is the recovery half,
run when ``serve`` starts and before each ``saneless scan``: it turns each
orphan's readable pages into a PDF in ``failed/`` (or moves the raw pages
there) and removes the workspace.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from PIL import Image

from saneless.pages import measure_ink
from saneless.pdf import sanitise_title_for_filename
from saneless.preservation import (
    BACKS_SUFFIX,
    FRONTS_SUFFIX,
    PARTIAL_SUFFIX,
    RunArtefacts,
    RunStage,
    preserve_most_finished,
)
from saneless.private_dirs import make_private_dir
from saneless.scanner.base import PageRecord

if TYPE_CHECKING:
    from typing import Self

__all__ = [
    "LOCK_FILE_NAME",
    "METADATA_FILE_NAME",
    "RECOVERED_TITLE",
    "SPOOL_DIR_NAME",
    "WORKSPACE_PREFIX",
    "JobWorkspace",
    "OrphanWorkspace",
    "RecoveredWorkspace",
    "find_orphans",
    "has_pages_left",
    "sweep_orphans",
]

logger = logging.getLogger(__name__)

SPOOL_DIR_NAME: Final = "spool"
"""The directory inside a workspace that holds the spooled page files."""

LOCK_FILE_NAME: Final = ".lock"
"""The file inside a workspace whose ``flock`` proves its owner is alive."""

METADATA_FILE_NAME: Final = "workspace.json"
"""The file inside a workspace naming its job id, title and profile."""

WORKSPACE_PREFIX: Final = "job-"
"""The name prefix of every visible workspace, and of nothing else."""

RECOVERED_TITLE: Final = "Recovered scan"
"""The title given to an orphan whose metadata cannot be read."""

_STAGING_PREFIX: Final = ".new-"

# The name prefix a finished workspace is given when it cannot be removed, so
# that no sweep mistakes what is left of it for a killed scan.
_LEFTOVER_PREFIX: Final = "leftover-"

# The name prefix of a workspace whose lock could not be taken.  Never
# ``job-``: nothing would prove its owner alive, so a sweeper whose own lock
# attempt succeeded -- a lock manager that recovered, another mount of the same
# share -- would take a live scan.
_UNLOCKED_PREFIX: Final = "unlocked-"

# The same truncation build_pdf_filename applies to the job id, so a
# workspace and the PDF it becomes share their job segment.
_JOB_SEGMENT_LENGTH: Final = 8

# Stands in for an empty job id, so every workspace name has three parts.
# A uuid4 cannot sanitise to it: "n", "o" and "j" are not hex digits.
_NO_JOB_SEGMENT: Final = "nojob"

# Generous for three short strings and a timestamp; anything larger is not
# metadata this module wrote, and is not read into memory.
_MAX_METADATA_BYTES: Final = 64 * 1024

_PRIVATE_FILE_MODE: Final = 0o600


def _job_segment(job_id: str) -> str:
    """
    Return the job-id segment of a workspace name.

    Args:
        job_id: The job's identifier; may be empty.

    Returns:
        The sanitised first eight characters of ``job_id``, or ``nojob``.

    """
    segment = sanitise_title_for_filename(job_id)[:_JOB_SEGMENT_LENGTH].strip("-")
    return segment or _NO_JOB_SEGMENT


def _job_id_from_name(name: str) -> str:
    """
    Recover as much of the job id as a workspace's name carries.

    The random suffix never contains ``-``, so everything between the prefix
    and the last ``-`` is the job segment.

    Args:
        name: A directory name starting with ``job-``.

    Returns:
        The job segment, or ``""`` for the ``nojob`` placeholder.

    """
    segment = name.removeprefix(WORKSPACE_PREFIX).rpartition("-")[0]
    return "" if segment == _NO_JOB_SEGMENT else segment


def _write_metadata(directory: Path, metadata: dict[str, str]) -> None:
    """
    Write the metadata file into ``directory`` atomically, mode 0600.

    ``mkstemp`` creates the temp file 0600 with ``O_EXCL``; it is fsynced
    before the rename, and removed on any failure, ``KeyboardInterrupt``
    included.

    Args:
        directory: The (staging) workspace directory.
        metadata: The fields to record.

    """
    target = directory / METADATA_FILE_NAME
    fd, tmp_name = tempfile.mkstemp(
        dir=directory, prefix=f".{METADATA_FILE_NAME}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    replaced = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(metadata).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        # Path.replace rather than the os function: ruff PTH105.
        tmp.replace(target)
        replaced = True
    finally:
        if not replaced:
            tmp.unlink(missing_ok=True)


def _remove_quietly(path: Path, what: str) -> bool:
    """
    Remove the directory tree ``path``, logging a failure instead of raising.

    Args:
        path: The directory to remove.
        what: How to describe it in the log line.

    Returns:
        Whether the tree is gone.

    """
    try:
        shutil.rmtree(path)
    except OSError:
        logger.warning("Could not remove %s %s", what, path, exc_info=True)
        return False
    return True


def _remove_or_retire(path: Path, what: str) -> None:
    """
    Remove a workspace nothing in which still needs keeping, or hide it.

    The run that owned it is over: its document was delivered, its pages were
    kept in ``failed/``, or it was cancelled.  If the removal fails, what is
    left must not look like a killed scan, or the next sweep would file the
    delivered document's pages in ``failed/`` as an interrupted scan.  So,
    while the caller still holds its lock, it is renamed out of the ``job-``
    names every sweep looks at; failing that, its lock file is removed, and
    a sweep skips a workspace with no lock file.

    Args:
        path: The workspace directory.
        what: How to describe it in the log line.

    """
    if _remove_quietly(path, what):
        return
    retired = path.with_name(f"{_LEFTOVER_PREFIX}{path.name}")
    try:
        path.rename(retired)
    except FileNotFoundError:
        return
    except OSError:
        try:
            (path / LOCK_FILE_NAME).unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "Could not stop the next sweep taking %s for an interrupted scan",
                path,
                exc_info=True,
            )
            return
        retired = path
    logger.warning(
        "%s holds nothing that still needs keeping, and no sweep will take it "
        "for an interrupted scan; delete it by hand",
        retired,
    )


class JobWorkspace:
    """
    A job's scratch directory, named after it and locked for as long as it lives.

    Use it as a context manager: entering creates the directory (with its
    ``spool`` subdirectory, lock and metadata) and returns its path; leaving
    removes it, however the block ends -- unless :meth:`keep` was called. A
    removal failure is logged at WARNING and never replaces the exception the
    block raised; what is left is renamed ``leftover-*``, out of every sweep's
    sight, because nothing in it still needs keeping.

    The caller is responsible for ``tmp_dir`` itself: it must exist and be
    private (``private_dirs.ensure_private_dir``).
    """

    def __init__(self, tmp_dir: Path, *, job_id: str, title: str, profile: str) -> None:
        """
        Describe a workspace; nothing is created until it is entered.

        Args:
            tmp_dir: The private scratch directory to create it in.
            job_id: The job's identifier; may be empty.
            title: The job's title as the operator typed it.
            profile: The name of the scan profile the job uses.

        """
        self._tmp_dir = tmp_dir
        self._job_id = job_id
        self._title = title
        self._profile = profile
        self._path: Path | None = None
        self._fd: int | None = None
        self._keep = False
        self._locked = False

    @property
    def path(self) -> Path:
        """
        The workspace directory.

        Raises:
            RuntimeError: If the workspace has not been entered.

        """
        if self._path is None:
            msg = "The job workspace has not been created yet"
            raise RuntimeError(msg)
        return self._path

    def __enter__(self) -> Path:
        """
        Create, lock and publish the workspace.

        The directory starts under a ``.new-*`` staging name (0700, from
        ``mkdtemp``), gets its lock, metadata and spool there, and is renamed
        to its ``job-*`` name only once the lock is held. The lock survives the
        rename because it belongs to the open lock file, not to a path.

        If ``flock`` is not supported (for example ENOLCK on some network
        filesystems) the workspace is used unlocked, with a WARNING, and is
        published as ``unlocked-*`` rather than ``job-*``. No sweep looks at
        that name, so a sweeper whose own lock attempt does succeed can never
        take the live scan inside; the cost is that such a workspace is never
        recovered.

        Returns:
            The workspace directory.

        Raises:
            RuntimeError: If this workspace is already in use.
            OSError: If the workspace cannot be created; nothing is left behind.

        """
        if self._path is not None:
            msg = "The job workspace is already in use"
            raise RuntimeError(msg)
        staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=self._tmp_dir))
        suffix = (
            f"{_job_segment(self._job_id)}-{staging.name.removeprefix(_STAGING_PREFIX)}"
        )
        final = self._tmp_dir / f"{WORKSPACE_PREFIX}{suffix}"
        published = False
        try:
            self._fd = os.open(
                staging / LOCK_FILE_NAME,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                _PRIVATE_FILE_MODE,
            )
            self._locked = self._lock(self._fd, staging)
            if not self._locked:
                final = self._tmp_dir / f"{_UNLOCKED_PREFIX}{suffix}"
            _write_metadata(
                staging,
                {
                    "job_id": self._job_id,
                    "title": self._title,
                    "profile": self._profile,
                    "created": datetime.now(tz=UTC).isoformat(),
                },
            )
            make_private_dir(staging / SPOOL_DIR_NAME)
            staging.rename(final)
            published = True
        finally:
            if not published:
                self._close_lock()
                _remove_quietly(staging, "the unfinished job workspace")
        self._path = final
        return final

    def keep(self) -> bool:
        """
        Leave the workspace in place when the block ends, for the next sweep.

        For a failed run whose pages could not all be kept in ``failed/``: the
        pages still in the spool are then the only copy, and removing them
        would lose them.  The lock is still released on the way out, so the
        next sweep -- when ``serve`` starts, or before a ``saneless scan`` --
        finds the workspace's owner gone and recovers it.  An ``unlocked-*``
        workspace is left in place too, but no sweep ever looks at it.

        Returns:
            Whether the next sweep will recover the workspace: False for one
            used unlocked.

        """
        self._keep = True
        return self._locked

    def __exit__(self, *exc_info: object) -> None:
        """
        Remove the workspace, then release its lock.

        The lock is held until the directory is gone, so no sweeper can claim
        a workspace while it is being removed. A workspace :meth:`keep` was
        called for is left in place, and only its lock is released. Returns
        None, so an exception raised in the block always propagates unchanged.

        Args:
            *exc_info: The exception type, value and traceback, if any.

        """
        path = self._path
        self._path = None
        try:
            if path is not None and self._keep:
                logger.warning(
                    "Leaving the job workspace %s in place: some of its pages "
                    "could not be kept, and %s",
                    path,
                    "the next sweep will recover them"
                    if self._locked
                    else "no sweep recovers an unlocked workspace",
                )
            elif path is not None:
                _remove_or_retire(path, "the job workspace")
        finally:
            self._close_lock()

    @staticmethod
    def _lock(fd: int, staging: Path) -> bool:
        """
        Take the workspace's exclusive lock, or warn that it cannot be had.

        Args:
            fd: The open lock file.
            staging: The staging directory, for the warning.

        Returns:
            Whether the lock is held.

        Raises:
            BlockingIOError: If someone already holds the brand-new lock.

        """
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise
        except OSError:
            logger.warning(
                "Could not lock the job workspace in %s; it is used unlocked, "
                "and will not be recovered if this process dies",
                staging.parent,
                exc_info=True,
            )
            return False
        return True

    def _close_lock(self) -> None:
        """Close the lock file, which releases the lock."""
        fd = self._fd
        self._fd = None
        if fd is not None:
            os.close(fd)


@dataclass(kw_only=True)
class OrphanWorkspace:
    """
    A workspace whose owner is gone, locked by whoever found it.

    The lock taken to prove the owner dead is kept until :meth:`close` (or the
    end of a ``with`` block), so a second sweep cannot claim the same
    workspace meanwhile. The fields come from the metadata file; when that
    cannot be read, ``job_id`` is what the directory name carries, ``title``
    is :data:`RECOVERED_TITLE` and ``profile`` is empty.

    ``title`` is untrusted text read from disk: log it with ``%r`` and turn it
    into a file name only through ``pdf.build_pdf_filename``.
    """

    path: Path
    job_id: str
    title: str
    profile: str
    spool: Path
    _lock_fd: int | None = field(repr=False, compare=False)

    def close(self) -> None:
        """Release the lock. Safe to call more than once."""
        fd = self._lock_fd
        self._lock_fd = None
        if fd is not None:
            os.close(fd)

    def __enter__(self) -> Self:
        """
        Return this orphan, still locked.

        Returns:
            This orphan.

        """
        return self

    def __exit__(self, *exc_info: object) -> None:
        """
        Release the lock.

        Args:
            *exc_info: The exception type, value and traceback, if any.

        """
        self.close()


def _open_lock(path: Path) -> int | None:
    """
    Open a candidate workspace's lock file without following a symlink.

    Args:
        path: The candidate workspace directory.

    Returns:
        The open lock file, or None if it is missing or not a regular file.

    """
    lock_path = path / LOCK_FILE_NAME
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        # Only JobWorkspace makes job-* names, and always with a lock, so
        # this is damage or a stranger; either way it cannot be proven dead.
        logger.warning(
            "Skipping %s: its lock file cannot be opened", path, exc_info=True
        )
        return None
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        regular = False
    if not regular:
        os.close(fd)
        logger.warning("Skipping %s: its lock file is not a regular file", path)
        return None
    return fd


def _try_lock(fd: int, path: Path) -> bool:
    """
    Take a candidate's lock if its owner is gone.

    Args:
        fd: The candidate's open lock file.
        path: The candidate directory, for the log.

    Returns:
        True if the lock was taken (the owner is dead), False otherwise.

    """
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.debug("Skipping %s: its job is still running", path)
        return False
    except OSError:
        logger.warning(
            "Skipping %s: its lock cannot be checked, so it is treated as live",
            path,
            exc_info=True,
        )
        return False
    return True


def _read_metadata(path: Path) -> tuple[str, str, str] | None:
    """
    Read a workspace's job id, title and profile, trusting nothing.

    Args:
        path: The workspace directory.

    Returns:
        ``(job_id, title, profile)``, or None if the file is missing, not a
        regular file, oversized, not UTF-8 JSON, or lacks any of the three as
        a string.

    """
    try:
        fd = os.open(
            path / METADATA_FILE_NAME,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        )
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            raw = handle.read(_MAX_METADATA_BYTES + 1)
        if len(raw) > _MAX_METADATA_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
    except OSError, ValueError, RecursionError:
        return None
    if not isinstance(data, dict):
        return None
    job_id, title, profile = data.get("job_id"), data.get("title"), data.get("profile")
    if not (
        isinstance(job_id, str) and isinstance(title, str) and isinstance(profile, str)
    ):
        return None
    return job_id, title, profile


def _is_own_directory(path: Path) -> bool:
    """
    Check, without following a symlink, that ``path`` is a directory of ours.

    Args:
        path: The candidate entry.

    Returns:
        True if it is a real directory owned by this process's user.

    """
    try:
        info = os.lstat(path)
    except OSError:
        logger.debug("Skipping %s: it vanished or cannot be inspected", path)
        return False
    if stat.S_ISLNK(info.st_mode):
        logger.debug("Skipping %s: it is a symbolic link", path)
        return False
    if not stat.S_ISDIR(info.st_mode):
        logger.debug("Skipping %s: it is not a directory", path)
        return False
    if info.st_uid != os.geteuid():
        logger.debug("Skipping %s: it is owned by uid %d", path, info.st_uid)
        return False
    return True


def _claim(path: Path) -> OrphanWorkspace | None:
    """
    Return ``path`` as a locked orphan if it is a dead job's workspace.

    Args:
        path: A ``job-*`` entry of ``tmp_dir``.

    Returns:
        The orphan, holding its lock; or None if it is live, not ours, or not
        a workspace.

    """
    if not _is_own_directory(path):
        return None
    fd = _open_lock(path)
    if fd is None:
        return None
    claimed = False
    try:
        if not _try_lock(fd, path):
            return None
        metadata = _read_metadata(path)
        if metadata is None:
            job_id, title, profile = _job_id_from_name(path.name), RECOVERED_TITLE, ""
            logger.warning(
                "The metadata of orphaned workspace %s cannot be read; "
                "recovering it as job %r",
                path,
                job_id,
            )
        else:
            job_id, title, profile = metadata
        logger.info(
            "Found orphaned workspace %s (job %r, title %r)", path, job_id, title
        )
        claimed = True
        return OrphanWorkspace(
            path=path,
            job_id=job_id,
            title=title,
            profile=profile,
            spool=path / SPOOL_DIR_NAME,
            _lock_fd=fd,
        )
    finally:
        if not claimed:
            os.close(fd)


def find_orphans(tmp_dir: Path) -> list[OrphanWorkspace]:
    """
    Find the workspaces in ``tmp_dir`` whose owning process is gone.

    Only direct ``job-*`` children are considered, in name order. Each must
    be a real directory (never a symlink) owned by this process's user, with
    a lock file that is not a symlink and whose lock can be taken without
    waiting. A lock someone holds -- this process included, through another
    open file description -- marks a live job, which is skipped; so is an
    entry whose lock cannot be checked at all. The staging ``.new-*``
    directories of workspaces still being created are never considered.

    Each returned orphan holds its lock: close it (or use it as a context
    manager) when done. Nothing is modified or removed here, and a bad entry
    is skipped rather than raised.

    Args:
        tmp_dir: The scratch directory workspaces are created in.

    Returns:
        The orphans, locked; an empty list if ``tmp_dir`` cannot be listed.

    """
    try:
        entries = sorted(
            entry
            for entry in tmp_dir.iterdir()
            if entry.name.startswith(WORKSPACE_PREFIX)
        )
    except OSError:
        logger.warning(
            "Could not look for orphaned workspaces in %s", tmp_dir, exc_info=True
        )
        return []
    orphans: list[OrphanWorkspace] = []
    try:
        for entry in entries:
            orphan = _claim(entry)
            if orphan is not None:
                orphans.append(orphan)
    except BaseException:
        # Interrupted mid-sweep: release what was claimed so far.
        for orphan in orphans:
            orphan.close()
        raise
    return orphans


@dataclass(frozen=True)
class RecoveredWorkspace:
    """
    What the sweep made of one orphaned workspace.

    Attributes:
        job_id: The job the workspace belonged to, as its metadata records it
            (or as much of it as the directory name carries).
        title: The job's title; untrusted text read from disk.
        pages: How many spooled pages could be read back.
        sentence: What was kept and where, every path directly after
            "preserved at "; None when the workspace held no page to keep.

    """

    job_id: str
    title: str
    pages: int
    sentence: str | None


# The spool file names of each acquisition pass, as the pipeline's sink writes
# them. Named for the side of the sheet rather than the pass, because ruff's
# S105 reads any variable whose name contains "pass" as a hardcoded password.
_FRONT_PAGES: Final = "a-*.png"
_BACK_PAGES: Final = "b-*.png"


def _read_page(path: Path, sequence: int) -> PageRecord:
    """
    Rebuild the record of one spooled page from the file alone.

    The file is checked with ``verify()`` first, then reopened to read its
    size, mode and the dpi in its pHYs chunk, and measured the way the spool
    measures a page it writes.

    Args:
        path: The spooled PNG.
        sequence: Its 1-based position in its pass.

    Returns:
        The page's record.

    Raises:
        ValueError: If the page carries no usable dpi, or its mode is one the
            spool never writes.
        OSError: If the file cannot be read or is not a whole image.

    """
    with Image.open(path) as probe:
        probe.verify()
    with Image.open(path) as image:
        image.load()
        dpi = image.info.get("dpi")
        if not isinstance(dpi, tuple) or not dpi or round(dpi[0]) <= 0:
            msg = f"{path.name} records no resolution"
            raise ValueError(msg)
        measurement = measure_ink(image)
        return PageRecord(
            sequence=sequence,
            path=path,
            size=image.size,
            mode=image.mode,
            dpi=round(dpi[0]),
            ink_coverage=measurement.coverage,
            paper_white=measurement.paper_white,
        )


def _page_files(spool: Path, pattern: str) -> list[Path]:
    """
    List one pass's spooled pages, in name order.

    Name order is the pass's acquisition order, because the sink numbers each
    page as it arrives. This is the one place order comes from names: a sweep
    has no records, and it never orders across the two passes. A ``.part``
    file is an interrupted write and never matches; a symbolic link is not a
    page the sink wrote and is left out.

    Args:
        spool: The workspace's spool directory.
        pattern: The pass's file-name pattern.

    Returns:
        The page files, possibly none.

    """
    return sorted(
        path for path in spool.glob(pattern) if not path.is_symlink() and path.is_file()
    )


def _rebuild_pass(paths: list[Path]) -> tuple[PageRecord, ...] | None:
    """
    Rebuild one pass's records, dropping an unreadable last page.

    A process killed while writing its last page may leave that page
    truncated, so an unreadable *last* page is dropped and the rest kept. An
    unreadable page anywhere else is not something a kill leaves behind, and
    skipping it would silently close a gap in the document, so the pass is
    then not assembled at all.

    Args:
        paths: The pass's page files, in acquisition order.

    Returns:
        The readable pages, or None if a page other than the last is
        unreadable.

    """
    records: list[PageRecord] = []
    for index, path in enumerate(paths):
        try:
            records.append(_read_page(path, index + 1))
        except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
            if index < len(paths) - 1:
                logger.warning(
                    "Page %s of an orphaned workspace cannot be read (%s) and is "
                    "not the last page, so the page files are kept instead of a PDF",
                    path,
                    exc,
                )
                return None
            logger.warning(
                "Skipping the unreadable last page %s of an orphaned workspace "
                "(%s): its process died while writing it",
                path,
                exc,
            )
    return tuple(records)


def has_pages_left(spool: Path) -> bool:
    """
    Say whether any spooled page is still in ``spool``.

    Args:
        spool: The workspace's spool directory.

    Returns:
        True if a page file of either pass is still there.

    """
    return bool(_page_files(spool, _FRONT_PAGES) or _page_files(spool, _BACK_PAGES))


def _recover_orphan(
    orphan: OrphanWorkspace, failed_dir: Path, reserve_mb: int
) -> RecoveredWorkspace:
    """
    Keep one orphan's pages in ``failed_dir``, then remove the workspace.

    Each pass becomes its own PDF through ``preservation``, just as a run
    that failed while acquiring keeps its passes: ``(partial)`` for a single
    pass, ``(fronts)`` and ``(backs)`` (in sheet order) for manual duplex,
    each under the twice-the-spool free-space rule. When a PDF cannot be
    built, or the rule refuses it, the raw page files are moved instead.

    The workspace is removed only once nothing in it is left un-kept: when
    keeping went wrong and pages are still in the spool, it stays, and the
    next sweep tries again.

    Args:
        orphan: The orphan, holding its lock.
        failed_dir: The durable directory preserved scans go in.
        reserve_mb: The ``min_free_space_mb`` reserve the free-space rule
            keeps.

    Returns:
        What was recovered.

    """
    spool = orphan.spool
    real_spool = _is_own_directory(spool)
    fronts = _page_files(spool, _FRONT_PAGES) if real_spool else []
    backs = _page_files(spool, _BACK_PAGES) if real_spool else []
    front_records = _rebuild_pass(fronts)
    back_records = _rebuild_pass(backs)
    readable = sum(len(records or ()) for records in (front_records, back_records))
    sentence: str | None = None
    kept_everything = True
    if fronts or backs:
        passes: list[tuple[str, tuple[PageRecord, ...]]] = []
        if front_records is not None and back_records is not None:
            passes = [
                (FRONTS_SUFFIX if backs else PARTIAL_SUFFIX, front_records),
                (BACKS_SUFFIX, back_records),
            ]
        report = preserve_most_finished(
            RunArtefacts(
                job_id=orphan.job_id,
                title=orphan.title,
                workspace=orphan.path,
                spool_dir=spool,
                failed_dir=failed_dir,
                reserve_mb=reserve_mb,
                stage=RunStage.ACQUIRING,
                passes=passes,
            )
        )
        sentence = report.sentence()
        kept_everything = not (report.problems and has_pages_left(spool))
    logger.warning(
        "Recovered an interrupted scan's workspace for job %s (%r): %s",
        orphan.job_id,
        orphan.title,
        sentence or "it held no page that could be kept",
    )
    if kept_everything:
        _remove_or_retire(orphan.path, "the recovered job workspace")
    else:
        logger.warning(
            "Leaving %s in place: some of its pages could not be kept, and the "
            "next sweep will try again",
            orphan.path,
        )
    return RecoveredWorkspace(
        job_id=orphan.job_id, title=orphan.title, pages=readable, sentence=sentence
    )


def sweep_orphans(
    tmp_dir: Path, failed_dir: Path, reserve_mb: int
) -> list[RecoveredWorkspace]:
    """
    Recover every workspace in ``tmp_dir`` whose process is gone.

    Each orphan :func:`find_orphans` returns -- a ``job-*`` directory of ours
    whose lock is free, so never a scan still running in this or another
    process -- has its readable pages kept in ``failed_dir``, gets one
    WARNING naming the job, its title and where the pages went, and is then
    removed. Its lock is held throughout and released afterwards.

    **This never raises an ``Exception``.** It runs at startup, and a sweep
    must never stop the service starting or a scan running: a failure
    recovering one orphan is logged with its traceback and the sweep moves on
    to the next, leaving that workspace for a later sweep.
    ``KeyboardInterrupt`` passes straight through.

    Scratch directories from releases before job-named workspaces (``tmp*``
    names) are never touched: nothing can prove their owner dead.

    Args:
        tmp_dir: The scratch directory workspaces are created in. A missing
            one has nothing in it to recover.
        failed_dir: The durable directory preserved scans go in.
        reserve_mb: The ``min_free_space_mb`` reserve the free-space rule
            keeps.

    Returns:
        One entry per orphan recovered, in name order.

    """
    if not tmp_dir.is_dir():
        return []
    orphans = find_orphans(tmp_dir)
    recovered: list[RecoveredWorkspace] = []
    try:
        for orphan in orphans:
            try:
                recovered.append(_recover_orphan(orphan, failed_dir, reserve_mb))
            except Exception:
                logger.warning(
                    "Could not recover the orphaned workspace %s (job %s, %r); "
                    "it is left for the next sweep",
                    orphan.path,
                    orphan.job_id,
                    orphan.title,
                    exc_info=True,
                )
            finally:
                orphan.close()
    finally:
        # Interrupted part way: release every lock still held. Closing twice
        # is safe.
        for orphan in orphans:
            orphan.close()
    return recovered
