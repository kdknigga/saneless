"""
Job workspaces: named after their job, locked for their whole life.

A scan's pages live in a scratch directory until the PDF is assembled. When the
process dies hard -- SIGKILL, the OOM killer, a power cut -- that directory is
all that is left of the scan, so it has to be recognisable as a dead job's and
findable without ever touching a live scan that shares ``tmp_dir``. These tests
pin the naming, the privacy of what is inside, the lock that proves liveness,
the cleanup on every exit path, and the discovery of workspaces whose owner is
gone, including one left behind by a real SIGKILL in a child process, and the
sweep that turns such a workspace's pages into a PDF (or keeps the raw pages)
in ``failed/``.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import re
import stat
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pikepdf
import pytest
from PIL import Image

import saneless.preservation as preservation_module
import saneless.workspace as workspace_mod
from saneless.exceptions import PdfError
from saneless.spool import SpooledPageSink
from saneless.workspace import (
    LOCK_FILE_NAME,
    METADATA_FILE_NAME,
    SPOOL_DIR_NAME,
    JobWorkspace,
    OrphanWorkspace,
    RecoveredWorkspace,
    find_orphans,
    sweep_orphans,
)
from tests.conftest import leave_killed_workspace
from tests.golden_support import distinct_page, embedded_streams, png_idat

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from saneless.scanner.base import PageRecord

_LOGGER = "saneless.workspace"
_JOB_ID = "3f9c2a71-aaaa"
_TITLE = "Tax 2026"
_PROFILE = "default"
_PRIVATE_DIR = 0o700
_PRIVATE_FILE = 0o600


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """Return a private scratch directory, as ``output.tmp_dir`` would be."""
    path = tmp_path / "scratch"
    path.mkdir(mode=_PRIVATE_DIR)
    return path


def _mode(path: Path) -> int:
    """Return the permission bits of ``path``, not following a symlink."""
    return stat.S_IMODE(path.lstat().st_mode)


def _lock_is_free(workspace: Path) -> bool:
    """
    Try the workspace's lock through a new open file description.

    ``flock`` locks belong to the open file description, so a second
    ``os.open`` in this very process contends with a lock held by the first,
    just as another process would.

    Returns:
        True if the lock could be taken (it is released again before
        returning), False if someone holds it.

    """
    fd = os.open(workspace / LOCK_FILE_NAME, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def _plant_orphan(
    scratch: Path, name: str, metadata: bytes | None = None, *, lock: bool = True
) -> Path:
    """
    Build a dead job's workspace by hand: a directory with a free lock.

    Args:
        scratch: The shared scratch directory.
        name: The directory name.
        metadata: The raw bytes of the metadata file, or None for none.
        lock: Whether to create the lock file.

    Returns:
        The planted directory.

    """
    path = scratch / name
    path.mkdir(mode=_PRIVATE_DIR)
    (path / SPOOL_DIR_NAME).mkdir(mode=_PRIVATE_DIR)
    if lock:
        (path / LOCK_FILE_NAME).touch(mode=_PRIVATE_FILE)
    if metadata is not None:
        (path / METADATA_FILE_NAME).write_bytes(metadata)
    return path


def _snapshot(path: Path) -> dict[str, tuple[int, int, bytes | None]]:
    """Record every entry below ``path`` as (mode, mtime_ns, content)."""
    result: dict[str, tuple[int, int, bytes | None]] = {}
    for entry in sorted(path.rglob("*")):
        info = entry.lstat()
        content = entry.read_bytes() if stat.S_ISREG(info.st_mode) else None
        result[str(entry.relative_to(path))] = (info.st_mode, info.st_mtime_ns, content)
    return result


def _raise_inside(scratch: Path, error: Exception, seen: list[Path]) -> None:
    """
    Enter a workspace, note its path in ``seen``, and raise ``error`` inside it.

    Raises:
        Exception: ``error`` itself, from inside the ``with`` block.

    """
    with JobWorkspace(scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE) as path:
        seen.append(path)
        raise error


class TestJobWorkspaceLayout:
    """What a workspace looks like while its job runs."""

    def test_named_after_the_job_directly_under_tmp_dir(self, scratch: Path) -> None:
        """The name carries the job id's first eight characters and a suffix."""
        with JobWorkspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        ) as path:
            assert path.parent == scratch
            assert re.fullmatch(r"job-3f9c2a71-[a-z0-9_]+", path.name), path.name
            assert path.is_dir()
            assert (path / SPOOL_DIR_NAME).is_dir()

    def test_empty_job_id_gets_a_placeholder_segment(self, scratch: Path) -> None:
        """A request with no job id still yields a ``job-nojob-*`` name."""
        with JobWorkspace(scratch, job_id="", title=_TITLE, profile=_PROFILE) as path:
            assert re.fullmatch(r"job-nojob-[a-z0-9_]+", path.name), path.name

    def test_path_property_matches_the_yielded_path(self, scratch: Path) -> None:
        """``JobWorkspace.path`` is the directory the ``with`` block received."""
        workspace = JobWorkspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        )
        with workspace as path:
            assert workspace.path == path

    def test_directories_and_files_are_private(self, scratch: Path) -> None:
        """Workspace and spool are 0700; the lock and metadata files are 0600."""
        with JobWorkspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        ) as path:
            assert _mode(path) == _PRIVATE_DIR
            assert _mode(path / SPOOL_DIR_NAME) == _PRIVATE_DIR
            assert _mode(path / LOCK_FILE_NAME) == _PRIVATE_FILE
            assert _mode(path / METADATA_FILE_NAME) == _PRIVATE_FILE

    def test_metadata_records_the_job(self, scratch: Path) -> None:
        """The metadata names job id, title and profile, with a UTC timestamp."""
        with JobWorkspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        ) as path:
            metadata = json.loads(
                (path / METADATA_FILE_NAME).read_text(encoding="utf-8")
            )
        assert metadata["job_id"] == _JOB_ID
        assert metadata["title"] == _TITLE
        assert metadata["profile"] == _PROFILE
        created = datetime.fromisoformat(metadata["created"])
        assert created.utcoffset() == timedelta(0)

    def test_no_staging_directory_is_left_behind(self, scratch: Path) -> None:
        """Only the ``job-*`` name is visible, during the block and after it."""
        with JobWorkspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        ) as path:
            assert [entry.name for entry in scratch.iterdir()] == [path.name]
        assert list(scratch.iterdir()) == []


class TestJobWorkspaceLock:
    """The lock is the proof that a workspace's owner is alive."""

    def test_lock_is_held_for_the_whole_block(self, scratch: Path) -> None:
        """Another open file description cannot take the lock while it runs."""
        with JobWorkspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        ) as path:
            assert not _lock_is_free(path)
            (path / SPOOL_DIR_NAME / "a-0001.png").write_bytes(b"page")
            assert not _lock_is_free(path)

    def test_live_workspace_is_not_an_orphan(self, scratch: Path) -> None:
        """``find_orphans`` skips a workspace whose owner still holds the lock."""
        with JobWorkspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        ) as path:
            orphans = find_orphans(scratch)
            assert [orphan.path for orphan in orphans] == []
            assert path.exists()

    def test_unsupported_flock_still_gives_a_workspace(
        self,
        scratch: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A filesystem without ``flock`` (ENOLCK) warns and runs unlocked."""

        def no_locks(fd: int, operation: int) -> None:
            raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

        caplog.set_level(logging.WARNING, logger=_LOGGER)
        monkeypatch.setattr(workspace_mod.fcntl, "flock", no_locks)
        with JobWorkspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        ) as path:
            assert (path / SPOOL_DIR_NAME).is_dir()
        assert not path.exists()
        assert [r.levelno for r in caplog.records] == [logging.WARNING]

    def test_an_unlocked_workspace_is_never_offered_to_a_sweep(
        self, scratch: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A sweeper whose own lock works must still not take an unlocked scan.

        The owner's flock failed, but locks may work again for a later sweep,
        or on another mount of the same share: the lock would then be free and
        prove nothing.  So the workspace is not published under ``job-``.
        """
        real_flock = workspace_mod.fcntl.flock

        def no_locks(fd: int, operation: int) -> None:
            raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

        monkeypatch.setattr(workspace_mod.fcntl, "flock", no_locks)
        workspace = JobWorkspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        )
        with workspace as path:
            monkeypatch.setattr(workspace_mod.fcntl, "flock", real_flock)
            assert path.name.startswith("unlocked-3f9c2a71-"), path.name
            assert find_orphans(scratch) == []
            assert sweep_orphans(scratch, scratch.parent / "failed", 0) == []
            assert workspace.keep() is False
        assert path.exists()


class TestJobWorkspaceCleanup:
    """Leaving the block removes the workspace, whatever the reason."""

    def test_normal_exit_removes_the_workspace(self, scratch: Path) -> None:
        """A block that ends normally leaves nothing in ``tmp_dir``."""
        with JobWorkspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        ) as path:
            (path / SPOOL_DIR_NAME / "a-0001.png").write_bytes(b"page")
        assert not path.exists()
        assert list(scratch.iterdir()) == []

    def test_exception_propagates_unchanged_and_workspace_is_removed(
        self, scratch: Path
    ) -> None:
        """The run's own exception object comes out, and the directory is gone."""
        error = RuntimeError("scan failed")
        seen: list[Path] = []
        with pytest.raises(RuntimeError) as caught:
            _raise_inside(scratch, error, seen)
        assert caught.value is error
        assert len(seen) == 1
        assert not seen[0].exists()

    def test_removal_failure_never_replaces_the_runs_exception(
        self,
        scratch: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failing ``rmtree`` is logged once at WARNING; the original escapes."""

        def failing_rmtree(path: object, *args: object, **kwargs: object) -> None:
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))

        caplog.set_level(logging.WARNING, logger=_LOGGER)
        monkeypatch.setattr(workspace_mod.shutil, "rmtree", failing_rmtree)
        error = RuntimeError("scan failed")
        seen: list[Path] = []
        with pytest.raises(RuntimeError) as caught:
            _raise_inside(scratch, error, seen)
        assert caught.value is error
        assert (scratch / f"leftover-{seen[0].name}").is_dir()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2

    def test_removal_failure_after_success_is_only_logged(
        self,
        scratch: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A normal block whose cleanup fails raises nothing and releases the lock."""

        def failing_rmtree(path: object, *args: object, **kwargs: object) -> None:
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))

        caplog.set_level(logging.WARNING, logger=_LOGGER)
        monkeypatch.setattr(workspace_mod.shutil, "rmtree", failing_rmtree)
        with JobWorkspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        ) as path:
            pass
        monkeypatch.undo()
        leftover = scratch / f"leftover-{path.name}"
        assert leftover.is_dir()
        assert _lock_is_free(leftover)
        assert [r.levelno for r in caplog.records] == [logging.WARNING] * 2

    def test_what_a_delivered_run_left_is_never_recovered(
        self, scratch: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A workspace that could not be removed is not a killed scan.

        Its document was delivered, so a sweep that took it for an
        interrupted scan would file the same pages in failed/ and invite the
        operator to upload them a second time.
        """

        def failing_rmtree(path: object, *args: object, **kwargs: object) -> None:
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))

        monkeypatch.setattr(workspace_mod.shutil, "rmtree", failing_rmtree)
        with JobWorkspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        ) as path:
            (path / SPOOL_DIR_NAME / "a-0001.png").write_bytes(b"page")
        monkeypatch.undo()
        failed_dir = scratch.parent / "failed"

        assert find_orphans(scratch) == []
        assert sweep_orphans(scratch, failed_dir, 0) == []
        assert not failed_dir.exists()

    def test_a_workspace_that_cannot_be_renamed_loses_its_lock_file(
        self, scratch: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failing the rename too, the lock file goes, and a sweep skips it."""

        def failing_rmtree(path: object, *args: object, **kwargs: object) -> None:
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))

        def failing_rename(self: Path, target: object) -> Path:
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))

        monkeypatch.setattr(workspace_mod.shutil, "rmtree", failing_rmtree)
        with JobWorkspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        ) as path:
            (path / SPOOL_DIR_NAME / "a-0001.png").write_bytes(b"page")
            # Only now: entering the workspace renames it into place.
            monkeypatch.setattr(workspace_mod.Path, "rename", failing_rename)
        monkeypatch.undo()

        assert path.is_dir()
        assert not (path / LOCK_FILE_NAME).exists()
        assert find_orphans(scratch) == []

    def test_a_kept_workspace_stays_with_its_lock_released(
        self, scratch: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``keep`` leaves the pages for the next sweep, which can claim them."""
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        workspace = JobWorkspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        )
        with workspace as path:
            (path / SPOOL_DIR_NAME / "a-0001.png").write_bytes(b"page")
            workspace.keep()
        assert (path / SPOOL_DIR_NAME / "a-0001.png").read_bytes() == b"page"
        assert _lock_is_free(path)
        assert [r.levelno for r in caplog.records] == [logging.WARNING]
        assert str(path) in caplog.records[0].getMessage()


class TestSigkillOrphan:
    """A workspace outlives a SIGKILL, and is found as a dead job's."""

    def test_sigkilled_workspace_is_found_with_pages_and_metadata(
        self, scratch: Path
    ) -> None:
        """No finalizer runs: the pages and the job's identity survive the kill."""
        workspace = leave_killed_workspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        )
        assert workspace.is_dir()

        orphans = find_orphans(scratch)
        try:
            assert len(orphans) == 1
            orphan = orphans[0]
            assert isinstance(orphan, OrphanWorkspace)
            assert orphan.path == workspace
            assert orphan.job_id == _JOB_ID
            assert orphan.title == _TITLE
            assert orphan.profile == _PROFILE
            assert orphan.spool == workspace / SPOOL_DIR_NAME
            pages = sorted(page.name for page in orphan.spool.iterdir())
            assert pages == ["a-0001.png", "a-0002.png"]
            with Image.open(orphan.spool / "a-0001.png") as page:
                assert round(page.info["dpi"][0]) == 300
            assert not _lock_is_free(workspace)
        finally:
            for orphan in orphans:
                orphan.close()
        assert _lock_is_free(workspace)


class TestFindOrphans:
    """Discovery takes only what is provably dead and provably ours."""

    def test_returned_orphan_holds_the_lock(self, scratch: Path) -> None:
        """A second sweep cannot claim an orphan the first one still holds."""
        path = _plant_orphan(
            scratch,
            "job-deadbeef-abc123",
            json.dumps({"job_id": "deadbeef-1", "title": "T", "profile": "p"}).encode(),
        )
        first = find_orphans(scratch)
        try:
            assert [orphan.path for orphan in first] == [path]
            assert find_orphans(scratch) == []
        finally:
            for orphan in first:
                orphan.close()
        second = find_orphans(scratch)
        assert [orphan.path for orphan in second] == [path]
        for orphan in second:
            orphan.close()

    def test_context_manager_releases_the_lock(self, scratch: Path) -> None:
        """Leaving an ``OrphanWorkspace``'s ``with`` block frees the lock."""
        path = _plant_orphan(scratch, "job-deadbeef-abc123", b"{}")
        (orphan,) = find_orphans(scratch)
        with orphan as held:
            assert held is orphan
            assert not _lock_is_free(path)
        assert _lock_is_free(path)
        orphan.close()

    def test_orphans_come_back_in_name_order(self, scratch: Path) -> None:
        """Several dead workspaces are returned sorted by name."""
        names = ["job-cccccccc-3", "job-aaaaaaaa-1", "job-bbbbbbbb-2"]
        for name in names:
            _plant_orphan(scratch, name, b"{}")
        orphans = find_orphans(scratch)
        try:
            assert [orphan.path.name for orphan in orphans] == sorted(names)
        finally:
            for orphan in orphans:
                orphan.close()

    def test_refuses_everything_that_is_not_a_dead_job_workspace(
        self, tmp_path: Path, scratch: Path
    ) -> None:
        """Symlinks, files, lockless and staging directories are never returned."""
        elsewhere = _plant_orphan(tmp_path, "elsewhere", b"{}")
        (elsewhere / SPOOL_DIR_NAME / "a-0001.png").write_bytes(b"page")
        (scratch / "job-link").symlink_to(elsewhere, target_is_directory=True)
        (scratch / "job-file").write_bytes(b"not a directory")
        _plant_orphan(scratch, "job-nolock", b"{}", lock=False)
        _plant_orphan(scratch, ".new-xyz", b"{}")
        _plant_orphan(scratch, "tmpabcdef", b"{}")
        before_elsewhere = _snapshot(elsewhere)
        before_scratch = _snapshot(scratch)

        orphans = find_orphans(scratch)
        for orphan in orphans:
            orphan.close()

        assert orphans == []
        assert _snapshot(elsewhere) == before_elsewhere
        assert _snapshot(scratch) == before_scratch

    def test_symlinked_lock_file_is_not_followed(
        self, tmp_path: Path, scratch: Path
    ) -> None:
        """A ``.lock`` that is a symlink is refused rather than opened."""
        target = tmp_path / "target-lock"
        target.write_bytes(b"")
        path = _plant_orphan(scratch, "job-deadbeef-abc123", b"{}", lock=False)
        (path / LOCK_FILE_NAME).symlink_to(target)

        orphans = find_orphans(scratch)
        for orphan in orphans:
            orphan.close()

        assert orphans == []

    def test_directory_of_another_owner_is_skipped(
        self, scratch: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An entry whose owner is not this process's user is never claimed."""
        _plant_orphan(scratch, "job-deadbeef-abc123", b"{}")
        real_uid = os.geteuid()
        monkeypatch.setattr(workspace_mod.os, "geteuid", lambda: real_uid + 1)

        orphans = find_orphans(scratch)
        for orphan in orphans:
            orphan.close()

        assert orphans == []

    @pytest.mark.parametrize(
        "metadata",
        [
            pytest.param(b"{not json", id="invalid-json"),
            pytest.param(b'["a", "list"]', id="not-an-object"),
            pytest.param(b'{"job_id": 5, "title": "T", "profile": "p"}', id="bad-type"),
            pytest.param(b'{"job_id": "x", "title": "T"}', id="missing-key"),
            pytest.param(b"[" * 50_000, id="too-deep"),
            pytest.param(None, id="missing"),
            pytest.param(b"\xff\xfe{", id="not-utf8"),
        ],
    )
    def test_unreadable_metadata_falls_back_to_the_name(
        self, scratch: Path, metadata: bytes | None
    ) -> None:
        """The job id comes from the name and the title is the neutral fallback."""
        path = _plant_orphan(scratch, "job-deadbeef-abc123", metadata)

        orphans = find_orphans(scratch)
        try:
            assert len(orphans) == 1
            assert orphans[0].path == path
            assert orphans[0].job_id == "deadbeef"
            assert orphans[0].title == "Recovered scan"
            assert orphans[0].profile == ""
        finally:
            for orphan in orphans:
                orphan.close()

    def test_placeholder_segment_falls_back_to_no_job_id(self, scratch: Path) -> None:
        """A ``job-nojob-*`` orphan with no metadata has an empty job id."""
        _plant_orphan(scratch, "job-nojob-abc123", b"{not json")
        orphans = find_orphans(scratch)
        try:
            assert [orphan.job_id for orphan in orphans] == [""]
        finally:
            for orphan in orphans:
                orphan.close()

    def test_unsupported_flock_treats_the_entry_as_live(
        self, scratch: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a working ``flock`` nothing can be proven dead, so nothing is."""

        def no_locks(fd: int, operation: int) -> None:
            raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

        _plant_orphan(scratch, "job-deadbeef-abc123", b"{}")
        monkeypatch.setattr(workspace_mod.fcntl, "flock", no_locks)

        assert find_orphans(scratch) == []

    def test_unlistable_tmp_dir_yields_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing ``tmp_dir`` is logged, not raised."""
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        assert find_orphans(tmp_path / "missing") == []
        assert [r.levelno for r in caplog.records] == [logging.WARNING]


# --- The sweep: an orphan's pages end up in failed/ --------------------------

# How many bytes of a whole PNG a "truncated" page keeps: its signature and
# the start of its header, and none of its pixel data.
_TRUNCATED_BYTES = 100


@pytest.fixture
def failed_dir(tmp_path: Path) -> Path:
    """Return where preserved scans go; it does not exist until one is kept."""
    return tmp_path / "data" / "failed"


def _plant_dead_workspace(
    scratch: Path, *, job_id: str = _JOB_ID, title: str = _TITLE, suffix: str = "x1"
) -> Path:
    """
    Build the workspace a dead job leaves behind: free lock, metadata, spool.

    Args:
        scratch: The shared scratch directory.
        job_id: The job id the metadata records.
        title: The title the metadata records.
        suffix: The random part of the directory name.

    Returns:
        The planted workspace.

    """
    metadata = json.dumps({"job_id": job_id, "title": title, "profile": _PROFILE})
    return _plant_orphan(
        scratch, f"job-{job_id[:8]}-{suffix}", metadata.encode("utf-8")
    )


def _spool_pages(
    spool: Path, label: str, count: int, *, first: int = 0
) -> tuple[PageRecord, ...]:
    """
    Spool ``count`` distinct pages into ``spool`` through the real sink.

    The sink writes each page as the scanner's pages are written: through a
    ``.part`` file, with the dpi in the PNG's pHYs chunk.

    Args:
        spool: The spool directory, which must exist.
        label: The pass label, ``a`` or ``b``.
        count: How many pages to spool.
        first: The ``distinct_page`` index of the first page.

    Returns:
        The records the sink made, in acquisition order.

    """
    sink = SpooledPageSink(spool, label, 0)
    for index in range(count):
        sink.add(distinct_page(first + index), dpi=300)
    return sink.records


def _streams(records: Sequence[PageRecord]) -> list[bytes]:
    """Return the image data each record's file would embed, in order."""
    return [png_idat(record.path.read_bytes()) for record in records]


def _pdfs(failed_dir: Path) -> list[Path]:
    """Return the PDFs in ``failed_dir``, by name; none if it does not exist."""
    return sorted(failed_dir.glob("*.pdf")) if failed_dir.exists() else []


def _page_dirs(failed_dir: Path) -> list[Path]:
    """Return the page-file directories in ``failed_dir``, by name."""
    if not failed_dir.exists():
        return []
    return sorted(entry for entry in failed_dir.iterdir() if entry.is_dir())


def _sweep_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the workspace module's WARNING records."""
    return [
        record
        for record in caplog.records
        if record.name == _LOGGER and record.levelno == logging.WARNING
    ]


class TestSweepOrphans:
    """An orphan's pages become a PDF in ``failed/``, or stay as page files."""

    def test_a_sigkilled_scan_becomes_a_partial_pdf(
        self,
        scratch: Path,
        failed_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A real SIGKILL: two spooled pages, one two-page (partial) PDF."""
        caplog.set_level(logging.INFO, logger=_LOGGER)
        workspace = leave_killed_workspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        )

        recovered = sweep_orphans(scratch, failed_dir, reserve_mb=0)

        assert len(recovered) == 1
        only = recovered[0]
        assert isinstance(only, RecoveredWorkspace)
        assert (only.job_id, only.title, only.pages) == (_JOB_ID, _TITLE, 2)
        pdfs = _pdfs(failed_dir)
        assert len(pdfs) == 1
        assert pdfs[0].name.endswith("-partial.pdf")
        with pikepdf.open(pdfs[0]) as pdf:
            assert len(pdf.pages) == 2
        assert only.sentence is not None
        assert f"preserved at {pdfs[0]}" in only.sentence
        assert not workspace.exists()
        assert _page_dirs(failed_dir) == []
        named = [
            record.getMessage()
            for record in _sweep_warnings(caplog)
            if _JOB_ID in record.getMessage()
        ]
        assert len(named) == 1
        assert repr(_TITLE) in named[0]
        assert str(pdfs[0]) in named[0]

    def test_a_live_workspace_is_left_alone(
        self, scratch: Path, failed_dir: Path
    ) -> None:
        """A workspace whose lock is held belongs to a running scan."""
        with JobWorkspace(
            scratch, job_id=_JOB_ID, title=_TITLE, profile=_PROFILE
        ) as live:
            _spool_pages(live / SPOOL_DIR_NAME, "a", 2)
            before = _snapshot(live)

            recovered = sweep_orphans(scratch, failed_dir, reserve_mb=0)

            assert recovered == []
            assert _snapshot(live) == before
            assert not _lock_is_free(live)
        assert not failed_dir.exists()

    def test_part_files_and_a_truncated_last_page_are_skipped(
        self, scratch: Path, failed_dir: Path
    ) -> None:
        """Only the readable pages reach the PDF; the rest go with the workspace."""
        workspace = _plant_dead_workspace(scratch)
        spool = workspace / SPOOL_DIR_NAME
        records = _spool_pages(spool, "a", 2)
        whole = records[0].path.read_bytes()
        (spool / "a-0003.png").write_bytes(whole[:_TRUNCATED_BYTES])
        (spool / "a-0004.png.part").write_bytes(whole[: 2 * _TRUNCATED_BYTES])
        expected = _streams(records)

        recovered = sweep_orphans(scratch, failed_dir, reserve_mb=0)

        assert [entry.pages for entry in recovered] == [2]
        pdfs = _pdfs(failed_dir)
        assert len(pdfs) == 1
        assert embedded_streams(pdfs[0]) == expected
        assert _page_dirs(failed_dir) == []
        assert not workspace.exists()

    def test_an_unreadable_page_before_the_last_keeps_the_raw_pages(
        self, scratch: Path, failed_dir: Path
    ) -> None:
        """A damaged page mid-pass is not skipped over: every page file is kept."""
        workspace = _plant_dead_workspace(scratch)
        spool = workspace / SPOOL_DIR_NAME
        records = _spool_pages(spool, "a", 3)
        records[1].path.write_bytes(records[1].path.read_bytes()[:_TRUNCATED_BYTES])

        recovered = sweep_orphans(scratch, failed_dir, reserve_mb=0)

        assert len(recovered) == 1
        assert _pdfs(failed_dir) == []
        (kept,) = _page_dirs(failed_dir)
        assert sorted(page.name for page in kept.iterdir()) == [
            "a-0001.png",
            "a-0002.png",
            "a-0003.png",
        ]
        assert not workspace.exists()

    def test_an_assembly_failure_moves_the_raw_pages(
        self, scratch: Path, failed_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No PDF can be built, so the page files themselves are kept."""

        def failing_assembly(*_args: object, **_kwargs: object) -> Path:
            msg = "img2pdf refused the page"
            raise PdfError(msg)

        monkeypatch.setattr(preservation_module, "assemble_pdf", failing_assembly)
        workspace = _plant_dead_workspace(scratch)
        records = _spool_pages(workspace / SPOOL_DIR_NAME, "a", 2)
        expected = {record.path.name: record.path.read_bytes() for record in records}

        recovered = sweep_orphans(scratch, failed_dir, reserve_mb=0)

        assert _pdfs(failed_dir) == []
        (kept,) = _page_dirs(failed_dir)
        assert {page.name: page.read_bytes() for page in kept.iterdir()} == expected
        (only,) = recovered
        assert only.sentence is not None
        assert f"spooled page file(s) were preserved at {kept}" in only.sentence
        assert not workspace.exists()

    def test_two_passes_become_fronts_and_backs_in_sheet_order(
        self, scratch: Path, failed_dir: Path
    ) -> None:
        """Pass B scanned the flipped stack, so the (backs) PDF is reversed."""
        workspace = _plant_dead_workspace(scratch)
        spool = workspace / SPOOL_DIR_NAME
        fronts = _spool_pages(spool, "a", 3)
        backs = _spool_pages(spool, "b", 3, first=3)
        expected_fronts = _streams(fronts)
        expected_backs = _streams(list(reversed(backs)))

        (only,) = sweep_orphans(scratch, failed_dir, reserve_mb=0)

        assert only.pages == 6
        pdfs = _pdfs(failed_dir)
        assert len(pdfs) == 2
        (fronts_pdf,) = [pdf for pdf in pdfs if pdf.name.endswith("-fronts.pdf")]
        (backs_pdf,) = [pdf for pdf in pdfs if pdf.name.endswith("-backs.pdf")]
        assert embedded_streams(fronts_pdf) == expected_fronts
        assert embedded_streams(backs_pdf) == expected_backs
        assert not workspace.exists()

    def test_the_free_space_rule_keeps_the_raw_pages_instead(
        self, scratch: Path, failed_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under twice the spool free, no PDF is built: the pages are moved."""
        monkeypatch.setattr(preservation_module, "_free_bytes", lambda _path: 0)
        workspace = _plant_dead_workspace(scratch)
        records = _spool_pages(workspace / SPOOL_DIR_NAME, "a", 2)

        (only,) = sweep_orphans(scratch, failed_dir, reserve_mb=0)

        assert _pdfs(failed_dir) == []
        (kept,) = _page_dirs(failed_dir)
        assert sorted(page.name for page in kept.iterdir()) == [
            record.path.name for record in records
        ]
        assert only.sentence is not None
        assert "Not enough free disk space" in only.sentence
        assert not workspace.exists()

    def test_a_symlinked_workspace_is_never_followed(
        self, tmp_path: Path, scratch: Path, failed_dir: Path
    ) -> None:
        """A ``job-*`` link to a directory full of pages moves nothing."""
        metadata = json.dumps({"job_id": _JOB_ID, "title": _TITLE, "profile": ""})
        target = _plant_orphan(tmp_path, "elsewhere", metadata.encode("utf-8"))
        _spool_pages(target / SPOOL_DIR_NAME, "a", 2)
        link = scratch / "job-3f9c2a71-link"
        link.symlink_to(target)
        before = _snapshot(target)

        assert sweep_orphans(scratch, failed_dir, reserve_mb=0) == []

        assert _snapshot(target) == before
        assert link.is_symlink()
        assert not failed_dir.exists()

    def test_one_failed_recovery_does_not_stop_the_next(
        self,
        scratch: Path,
        failed_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A bug recovering one orphan is logged; the next is still recovered."""
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        first = _plant_dead_workspace(scratch, job_id="11111111-first", suffix="a")
        second = _plant_dead_workspace(scratch, job_id="22222222-second", suffix="b")
        _spool_pages(first / SPOOL_DIR_NAME, "a", 1)
        _spool_pages(second / SPOOL_DIR_NAME, "a", 1)
        original = workspace_mod._recover_orphan
        seen: list[Path] = []

        def failing_first(
            orphan: OrphanWorkspace, failed: Path, reserve_mb: int
        ) -> RecoveredWorkspace:
            seen.append(orphan.path)
            if len(seen) == 1:
                msg = "a bug in the recovery"
                raise RuntimeError(msg)
            return original(orphan, failed, reserve_mb)

        monkeypatch.setattr(workspace_mod, "_recover_orphan", failing_first)

        recovered = sweep_orphans(scratch, failed_dir, reserve_mb=0)

        assert seen == [first, second]
        assert [entry.job_id for entry in recovered] == ["22222222-second"]
        assert not second.exists()
        assert first.is_dir()
        assert _lock_is_free(first)
        assert any(
            record.exc_info is not None and str(first) in record.getMessage()
            for record in _sweep_warnings(caplog)
        )

    def test_an_orphan_with_no_pages_is_removed_and_reported(
        self,
        scratch: Path,
        failed_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Nothing to keep: the workspace goes, and the WARNING says so."""
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        workspace = _plant_dead_workspace(scratch)

        (only,) = sweep_orphans(scratch, failed_dir, reserve_mb=0)

        assert (only.pages, only.sentence) == (0, None)
        assert not workspace.exists()
        assert not failed_dir.exists()
        assert any(_JOB_ID in r.getMessage() for r in _sweep_warnings(caplog))

    def test_a_missing_tmp_dir_is_nothing_to_sweep(
        self, tmp_path: Path, failed_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A first run has no scratch directory yet; that is not a problem."""
        caplog.set_level(logging.WARNING, logger=_LOGGER)

        assert sweep_orphans(tmp_path / "missing", failed_dir, reserve_mb=0) == []

        assert _sweep_warnings(caplog) == []
