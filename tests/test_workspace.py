"""
Job workspaces: named after their job, locked for their whole life.

A scan's pages live in a scratch directory until the PDF is assembled. When the
process dies hard -- SIGKILL, the OOM killer, a power cut -- that directory is
all that is left of the scan, so it has to be recognisable as a dead job's and
findable without ever touching a live scan that shares ``tmp_dir``. These tests
pin the naming, the privacy of what is inside, the lock that proves liveness,
the cleanup on every exit path, and the discovery of workspaces whose owner is
gone, including one left behind by a real SIGKILL in a child process.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import saneless.workspace as workspace_mod
from PIL import Image
from saneless.workspace import (
    LOCK_FILE_NAME,
    METADATA_FILE_NAME,
    SPOOL_DIR_NAME,
    JobWorkspace,
    OrphanWorkspace,
    find_orphans,
)

if TYPE_CHECKING:
    from pathlib import Path

_LOGGER = "saneless.workspace"
_JOB_ID = "3f9c2a71-aaaa"
_TITLE = "Tax 2026"
_PROFILE = "default"
_PRIVATE_DIR = 0o700
_PRIVATE_FILE = 0o600
_CHILD_TIMEOUT_SECONDS = 30
_SIGKILL_RETURNCODE = -9

# The child enters a real workspace, spools two pages the way the scanner
# does (PNG with a 300 dpi pHYs chunk), says where it is, and dies without
# running a single ``finally`` or ``__exit__``.
_SIGKILL_CHILD = """\
import os
import signal
from pathlib import Path

from PIL import Image

from saneless.workspace import SPOOL_DIR_NAME, JobWorkspace

tmp_dir = Path(os.environ["SANELESS_TEST_TMP_DIR"])
with JobWorkspace(
    tmp_dir,
    job_id=os.environ["SANELESS_TEST_JOB_ID"],
    title=os.environ["SANELESS_TEST_TITLE"],
    profile=os.environ["SANELESS_TEST_PROFILE"],
) as path:
    spool = path / SPOOL_DIR_NAME
    for name in ("a-0001.png", "a-0002.png"):
        Image.new("L", (64, 64), 255).save(spool / name, dpi=(300, 300))
    print(path, flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
"""


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


def _run_sigkill_child(script: Path, scratch: Path) -> subprocess.CompletedProcess[str]:
    """
    Run ``script`` in a child that enters a workspace and SIGKILLs itself.

    Every argv element is a literal and the per-run values travel in the
    environment, as ``test_pdf._measure_assembly`` does: ``sys.executable`` in
    the argv would take the call off ruff's S603 allow-list, and this project
    adds no suppressions.

    Args:
        script: The child source, already written to disk.
        scratch: The shared scratch directory the child creates its workspace in.

    Returns:
        The completed child, its output captured as text.

    """
    env = {
        **os.environ,
        "SANELESS_TEST_PYTHON": sys.executable,
        "SANELESS_TEST_SCRIPT": str(script),
        "SANELESS_TEST_TMP_DIR": str(scratch),
        "SANELESS_TEST_JOB_ID": _JOB_ID,
        "SANELESS_TEST_TITLE": _TITLE,
        "SANELESS_TEST_PROFILE": _PROFILE,
    }
    return subprocess.run(
        ["/bin/sh", "-c", 'exec "$SANELESS_TEST_PYTHON" "$SANELESS_TEST_SCRIPT"'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_CHILD_TIMEOUT_SECONDS,
    )


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
        assert seen[0].exists()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

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
        assert path.exists()
        assert _lock_is_free(path)
        assert [r.levelno for r in caplog.records] == [logging.WARNING]


class TestSigkillOrphan:
    """A workspace outlives a SIGKILL, and is found as a dead job's."""

    def test_sigkilled_workspace_is_found_with_pages_and_metadata(
        self, tmp_path: Path, scratch: Path
    ) -> None:
        """No finalizer runs: the pages and the job's identity survive the kill."""
        script = tmp_path / "sigkill_child.py"
        script.write_text(_SIGKILL_CHILD, encoding="utf-8")

        completed = _run_sigkill_child(script, scratch)

        assert completed.returncode == _SIGKILL_RETURNCODE, completed.stderr
        workspace = scratch / completed.stdout.strip().rsplit("/", 1)[-1]
        assert str(workspace) == completed.stdout.strip()
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
