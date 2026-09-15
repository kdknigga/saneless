"""
Tests for the durable config-file replace (CFG-08, D-05..D-07).

``replace_file_atomically`` is the only way saneless rewrites a config file.
Its contract, which every test below pins down:

* The new bytes go to a ``tempfile.mkstemp`` file in the real target's own
  directory, are fsynced, and take the target's name in one ``rename(2)``
  (``Path.replace``). A crash therefore leaves the old file or the new file,
  never a truncated one (D-05).
* The text is written as UTF-8 bytes with no newline translation, so a CRLF
  file stays CRLF and a non-ASCII comment survives whatever the locale (D-05).
* The temp file is removed on every failure path (D-05).
* A symlinked config path is written through: the real file is replaced and
  the link keeps pointing at it (D-07).
* A target the process may not write is refused before any temp file exists,
  because a rename needs only a writable directory (Pitfall 6).
"""

from __future__ import annotations

import errno
import os
import stat
import tempfile
from pathlib import Path

import pytest
from saneless.atomic_write import replace_file_atomically

_ORIGINAL = "[profiles.default]\nsource = 'Flatbed'\n"
_NEW = "a = 1\r\n# café\n"


def _leftovers(directory: Path) -> list[str]:
    """Return the names of any temp files the helper left in ``directory``."""
    leftovers = sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.name.startswith(".") and entry.name.endswith(".tmp")
    )
    assert not leftovers, (
        f"temp files left behind in {directory}: {leftovers}. Every failure "
        f"path must remove the temp file; a survivor is a stray copy of the "
        f"config (possibly holding the Paperless token) that the operator "
        f"never asked for and that accumulates on every failed rewrite."
    )
    return leftovers


def _record_mkstemp(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record the ``dir`` every ``tempfile.mkstemp`` call used, then create it."""
    real_mkstemp = tempfile.mkstemp
    directories: list[Path] = []

    def recording(**kwargs: str | Path) -> tuple[int, str]:
        """Note the directory, then create the temp file for real."""
        directory = Path(kwargs["dir"])
        directories.append(directory)
        return real_mkstemp(
            suffix=str(kwargs["suffix"]),
            prefix=str(kwargs["prefix"]),
            dir=directory,
        )

    monkeypatch.setattr(tempfile, "mkstemp", recording)
    return directories


def _is_directory_fd(fd: int) -> bool:
    """Return True when ``fd`` refers to a directory."""
    return stat.S_ISDIR(os.fstat(fd).st_mode)


class TestAtomicReplace:
    """A successful rewrite is byte-exact, durable, and tidy (D-05)."""

    def test_atomic_replace_writes_crlf_and_utf8_bytes_exactly(
        self, tmp_path: Path
    ) -> None:
        """CRLF endings and a non-ASCII comment reach disk unchanged, as UTF-8."""
        target = tmp_path / "config.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))

        result = replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _NEW.encode("utf-8")
        assert result == target.resolve()

    def test_atomic_new_file_is_created_with_mode_0600(self, tmp_path: Path) -> None:
        """
        A file that did not exist is created, private to its owner.

        A freshly written config may hold the Paperless token, so mkstemp's
        0600 is kept rather than widened.
        """
        target = tmp_path / "config.toml"

        replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _NEW.encode("utf-8")
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_atomic_replace_leaves_only_the_target(self, tmp_path: Path) -> None:
        """After success the directory holds the target and nothing else."""
        target = tmp_path / "config.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")

        replace_file_atomically(target, _NEW)

        _leftovers(tmp_path)
        assert sorted(entry.name for entry in tmp_path.iterdir()) == ["config.toml"]

    def test_atomic_temp_file_is_created_in_the_target_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The temp file shares the target's directory.

        ``rename(2)`` is atomic only within one filesystem; a temp file in
        /tmp would fail with EXDEV against a mounted config directory.
        """
        directories = _record_mkstemp(monkeypatch)
        target = tmp_path / "config.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")

        replace_file_atomically(target, _NEW)

        assert directories == [target.resolve().parent]

    def test_atomic_fsync_of_the_temp_file_precedes_the_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The temp file's data is on disk before it takes the target's name.

        Renaming unsynced data can leave a zero-length config after a crash.
        """
        calls: list[str] = []
        real_fsync = os.fsync
        real_replace = Path.replace

        def recording_fsync(fd: int) -> None:
            """Note whether a file or a directory was fsynced."""
            calls.append("fsync-dir" if _is_directory_fd(fd) else "fsync-file")
            real_fsync(fd)

        def recording_replace(self: Path, target: Path) -> Path:
            """Note the rename, then perform it."""
            calls.append("replace")
            return real_replace(self, target)

        monkeypatch.setattr(os, "fsync", recording_fsync)
        monkeypatch.setattr(Path, "replace", recording_replace)
        target = tmp_path / "config.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")

        replace_file_atomically(target, _NEW)

        assert "fsync-file" in calls
        assert "replace" in calls
        assert calls.index("fsync-file") < calls.index("replace")


class TestAtomicFailureCleanup:
    """Every failure leaves the original file intact and no temp file (D-05)."""

    def test_atomic_fsync_failure_keeps_original_and_removes_temp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An I/O error while syncing propagates; nothing on disk changes."""

        def failing_fsync(fd: int) -> None:
            """Fail the way a dying disk would."""
            raise OSError(errno.EIO, os.strerror(errno.EIO))

        monkeypatch.setattr(os, "fsync", failing_fsync)
        target = tmp_path / "config.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))

        with pytest.raises(OSError, match=os.strerror(errno.EIO)) as excinfo:
            replace_file_atomically(target, _NEW)

        assert excinfo.value.errno == errno.EIO
        assert target.read_bytes() == _ORIGINAL.encode("utf-8")
        _leftovers(tmp_path)

    def test_atomic_replace_failure_propagates_and_removes_temp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rename failure other than EBUSY is re-raised unchanged."""

        def cross_device(self: Path, target: object) -> Path:
            """Fail the way a rename across filesystems would."""
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))

        monkeypatch.setattr(Path, "replace", cross_device)
        target = tmp_path / "config.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))

        with pytest.raises(OSError, match=os.strerror(errno.EXDEV)) as excinfo:
            replace_file_atomically(target, _NEW)

        assert excinfo.value.errno == errno.EXDEV
        assert target.read_bytes() == _ORIGINAL.encode("utf-8")
        _leftovers(tmp_path)

    def test_atomic_directory_fsync_failure_does_not_fail_the_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A filesystem that rejects directory fsync still gets the new file.

        Some FUSE, network, and Docker Desktop mounts answer EINVAL; by then
        the rename has happened, so failing would report a write that worked.
        """
        real_fsync = os.fsync
        directory_attempts: list[int] = []

        def no_directory_fsync(fd: int) -> None:
            """Refuse directory fsync, allow file fsync."""
            if _is_directory_fd(fd):
                directory_attempts.append(fd)
                raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", no_directory_fsync)
        target = tmp_path / "config.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")

        replace_file_atomically(target, _NEW)

        assert directory_attempts, "the directory was never fsynced"
        assert target.read_bytes() == _NEW.encode("utf-8")
        _leftovers(tmp_path)


class TestSymlinkAndReadonly:
    """Symlinks are written through (D-07); read-only targets are refused."""

    def test_symlink_is_written_through_to_the_real_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A dotfiles-style link keeps working after a rewrite.

        Replacing the link itself would swap it for a regular file and
        silently detach the config from the operator's repository.
        """
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        real = real_dir / "config.toml"
        real.write_text(_ORIGINAL, encoding="utf-8")
        link = tmp_path / "link.toml"
        link.symlink_to(real)
        directories = _record_mkstemp(monkeypatch)

        result = replace_file_atomically(link, _NEW)

        assert real.read_bytes() == _NEW.encode("utf-8")
        assert link.is_symlink()
        assert link.resolve() == real.resolve()
        assert link.read_bytes() == _NEW.encode("utf-8")
        assert directories == [real.resolve().parent]
        assert result == real.resolve()
        _leftovers(real_dir)
        _leftovers(tmp_path)

    def test_readonly_target_is_refused_before_any_temp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A chmod 0444 config is not silently replaced.

        ``rename(2)`` checks only the directory, so without a pre-check the
        documented "config cannot be written" case would quietly succeed.
        """
        if os.geteuid() == 0:
            pytest.skip("root bypasses file permissions")
        target = tmp_path / "config.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        target.chmod(0o444)
        directories = _record_mkstemp(monkeypatch)

        try:
            with pytest.raises(PermissionError):
                replace_file_atomically(target, _NEW)

            assert target.read_bytes() == _ORIGINAL.encode("utf-8")
            assert directories == [], "a temp file was created before refusing"
            _leftovers(tmp_path)
        finally:
            target.chmod(0o600)
