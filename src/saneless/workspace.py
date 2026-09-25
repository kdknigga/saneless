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
never recover the same workspace. What to do with them is the caller's
business.
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

from saneless.pdf import sanitise_title_for_filename
from saneless.private_dirs import make_private_dir

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
    "find_orphans",
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


def _remove_quietly(path: Path, what: str) -> None:
    """
    Remove the directory tree ``path``, logging a failure instead of raising.

    Args:
        path: The directory to remove.
        what: How to describe it in the log line.

    """
    try:
        shutil.rmtree(path)
    except OSError:
        logger.warning("Could not remove %s %s", what, path, exc_info=True)


class JobWorkspace:
    """
    A job's scratch directory, named after it and locked for as long as it lives.

    Use it as a context manager: entering creates the directory (with its
    ``spool`` subdirectory, lock and metadata) and returns its path; leaving
    removes it, however the block ends. A removal failure is logged at WARNING
    and never replaces the exception the block raised.

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
        filesystems) the workspace is used unlocked, with a WARNING. A sweeper
        that cannot take the lock treats the workspace as live, so nothing
        live is ever touched; the cost is that such a workspace is never
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
        final = self._tmp_dir / (
            f"{WORKSPACE_PREFIX}{_job_segment(self._job_id)}-"
            f"{staging.name.removeprefix(_STAGING_PREFIX)}"
        )
        published = False
        try:
            self._fd = os.open(
                staging / LOCK_FILE_NAME,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                _PRIVATE_FILE_MODE,
            )
            self._lock(self._fd, staging)
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

    def __exit__(self, *exc_info: object) -> None:
        """
        Remove the workspace, then release its lock.

        The lock is held until the directory is gone, so no sweeper can claim
        a workspace while it is being removed. Returns None, so an exception
        raised in the block always propagates unchanged.

        Args:
            *exc_info: The exception type, value and traceback, if any.

        """
        path = self._path
        self._path = None
        try:
            if path is not None:
                _remove_quietly(path, "the job workspace")
        finally:
            self._close_lock()

    @staticmethod
    def _lock(fd: int, staging: Path) -> None:
        """
        Take the workspace's exclusive lock, or warn that it cannot be had.

        Args:
            fd: The open lock file.
            staging: The staging directory, for the warning.

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
