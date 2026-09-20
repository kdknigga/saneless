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
* An existing file's mode and owner survive the rewrite; ownership is copied
  only when the process is permitted to (D-06).
* A rename refused with EBUSY -- a config bind-mounted as a single file -- is
  a ``ConfigError`` that tells the operator to mount the directory, and there
  is no non-atomic fallback (D-08). A config inside a mounted *directory*, the
  documented ``./config:/etc/saneless`` layout, is replaced normally (CFG-09).
"""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from saneless.atomic_write import replace_file_atomically
from saneless.exceptions import ConfigError

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


def _deny_write_access(monkeypatch: pytest.MonkeyPatch, denied: Path) -> None:
    """
    Make ``os.access`` report ``denied`` unwritable; every other path is asked for real.

    The pre-check asks ``os.access``, so answering for the file is the whole
    failure.  Injecting it rather than taking the file's write bit away keeps
    the test meaningful as root, for whom ``os.access`` ignores modes.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        denied: The file to report as not writable.

    """
    real_access = os.access
    resolved = denied.resolve()

    def access(path: str | os.PathLike[str], mode: int) -> bool:
        if Path(path).resolve() == resolved and mode & os.W_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr("saneless.atomic_write.os.access", access)


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
        A config this process may not write is not silently replaced.

        ``rename(2)`` checks only the directory, so without a pre-check the
        documented "config cannot be written" case would quietly succeed.
        """
        target = tmp_path / "config.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        _deny_write_access(monkeypatch, target)
        directories = _record_mkstemp(monkeypatch)

        with pytest.raises(PermissionError):
            replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _ORIGINAL.encode("utf-8")
        assert directories == [], "a temp file was created before refusing"
        _leftovers(tmp_path)


class TestReadOnlyMount:
    """
    A read-only mount is reported with the mount fix, not as EACCES (WR-01).

    ``statvfs`` is faked here so the tests run anywhere; ``TestRealBindMount``
    checks the kernel's own answer where user namespaces are available.
    """

    @staticmethod
    def _fake_read_only(monkeypatch: pytest.MonkeyPatch, read_only: set[Path]) -> None:
        """Report ``ST_RDONLY`` for exactly the paths in ``read_only``."""
        real_statvfs = os.statvfs

        def statvfs(path: str | Path) -> os.statvfs_result:
            """Return the real result, with the read-only flag faked."""
            result = real_statvfs(path)
            flags = result.f_flag & ~os.ST_RDONLY
            if Path(path) in read_only:
                flags |= os.ST_RDONLY
            fields = list(result)
            fields[8] = flags
            return os.statvfs_result(fields)

        monkeypatch.setattr(os, "statvfs", statvfs)

    def test_read_only_single_file_mount_is_the_d08_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read-only file in a writable directory is its own mount: D-08."""
        target = tmp_path / "config.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        self._fake_read_only(monkeypatch, {target.resolve()})
        directories = _record_mkstemp(monkeypatch)

        with pytest.raises(ConfigError) as excinfo:
            replace_file_atomically(target, _NEW)

        message = str(excinfo.value)
        assert str(target.resolve()) in message
        assert "bind-mounted as a single file" in message
        assert "Mount its directory instead" in message
        assert "docs/how-to/deploy-docker-compose.md" in message
        assert "Permission denied" not in message
        assert target.read_bytes() == _ORIGINAL.encode("utf-8")
        assert directories == [], "a temp file was created before refusing"

    def test_read_only_directory_mount_is_a_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file and its directory on one read-only mount name that mount."""
        target = tmp_path / "config.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        self._fake_read_only(monkeypatch, {target.resolve(), tmp_path.resolve()})

        with pytest.raises(ConfigError) as excinfo:
            replace_file_atomically(target, _NEW)

        message = str(excinfo.value)
        assert str(target.resolve()) in message
        assert "read-only mount" in message
        assert "read-write" in message
        assert "docs/how-to/deploy-docker-compose.md" in message
        assert target.read_bytes() == _ORIGINAL.encode("utf-8")

    def test_writable_mount_read_only_file_is_still_permission_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unwritable file on a writable mount is refused as read-only."""
        target = tmp_path / "config.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        _deny_write_access(monkeypatch, target)
        self._fake_read_only(monkeypatch, set())

        with pytest.raises(PermissionError):
            replace_file_atomically(target, _NEW)
        assert target.read_bytes() == _ORIGINAL.encode("utf-8")


class TestModeAndOwner:
    """The rewritten file keeps the original's mode and, when allowed, owner."""

    @staticmethod
    def _record_fchown_and_fchmod(
        monkeypatch: pytest.MonkeyPatch,
    ) -> list[tuple[str, int, int]]:
        """Record every fchown and fchmod call in order, then perform it."""
        calls: list[tuple[str, int, int]] = []
        real_fchown = os.fchown
        real_fchmod = os.fchmod

        def recording_fchown(fd: int, uid: int, gid: int) -> None:
            """Note the requested owner, then apply it."""
            calls.append(("fchown", uid, gid))
            real_fchown(fd, uid, gid)

        def recording_fchmod(fd: int, mode: int) -> None:
            """Note the requested mode, then apply it."""
            calls.append(("fchmod", mode, -1))
            real_fchmod(fd, mode)

        monkeypatch.setattr(os, "fchown", recording_fchown)
        monkeypatch.setattr(os, "fchmod", recording_fchmod)
        return calls

    def test_atomic_existing_mode_0640_is_preserved(self, tmp_path: Path) -> None:
        """
        A group-readable config stays group-readable.

        mkstemp creates 0600; without copying the mode, a rewrite would lock
        out a group (for example a backup user) that could read it before.
        """
        target = tmp_path / "config.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")
        target.chmod(0o640)

        replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _NEW.encode("utf-8")
        assert stat.S_IMODE(target.stat().st_mode) == 0o640

    def test_atomic_fchown_gets_the_original_owner_before_fchmod(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Ownership is copied first, then the mode.

        A host-owned config must not become root-owned after a container
        rewrite (D-06), and chown(2) may clear set-id bits, so the mode has
        to be applied after it (Pitfall 5).
        """
        target = tmp_path / "config.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")
        target.chmod(0o640)
        original = target.stat()
        calls = self._record_fchown_and_fchmod(monkeypatch)

        replace_file_atomically(target, _NEW)

        assert calls == [
            ("fchown", original.st_uid, original.st_gid),
            ("fchmod", 0o640, -1),
        ]

    def test_atomic_fchown_permission_error_is_skipped_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A process not allowed to chown still rewrites the file (D-06).

        On bare metal the non-root writer already owns the file, so the
        refused chown loses nothing and must not fail the write.
        """

        def refused_fchown(fd: int, uid: int, gid: int) -> None:
            """Refuse the way the kernel does for a non-root caller."""
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

        monkeypatch.setattr(os, "fchown", refused_fchown)
        target = tmp_path / "config.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")
        target.chmod(0o640)

        replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _NEW.encode("utf-8")
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
        _leftovers(tmp_path)

    @pytest.mark.parametrize(
        "code", [errno.EINVAL, errno.EOPNOTSUPP, errno.EPERM], ids=errno.errorcode.get
    )
    def test_atomic_fchown_unsupported_is_skipped_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        """
        A chown the filesystem cannot perform does not fail the write (WR-02).

        In a rootless container a host uid that is not mapped shows up as the
        overflow uid, and chown to it is EINVAL; FUSE, CIFS and vfat mounts
        answer EOPNOTSUPP. Replacing the file still works, so it must happen.
        """

        def unsupported_fchown(fd: int, uid: int, gid: int) -> None:
            """Refuse every ownership change with ``code``."""
            raise OSError(code, os.strerror(code))

        monkeypatch.setattr(os, "fchown", unsupported_fchown)
        target = tmp_path / "config.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")
        target.chmod(0o640)

        replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _NEW.encode("utf-8")
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
        _leftovers(tmp_path)

    def test_atomic_refused_owner_change_still_keeps_the_group(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        When the owner cannot be set, the group is still copied (WR-02).

        A ``root:saneless`` 0664 config rewritten by the ``saneless`` user
        cannot keep root as owner, but the writer may set the group it is a
        member of, and before this the group was lost too.
        """
        calls: list[tuple[int, int]] = []
        real_fchown = os.fchown

        def owner_refused(fd: int, uid: int, gid: int) -> None:
            """Refuse an owner change, allow a group-only change."""
            calls.append((uid, gid))
            if uid != -1:
                raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))
            real_fchown(fd, uid, gid)

        monkeypatch.setattr(os, "fchown", owner_refused)
        target = tmp_path / "config.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")
        original = target.stat()

        replace_file_atomically(target, _NEW)

        assert calls == [
            (original.st_uid, original.st_gid),
            (-1, original.st_gid),
        ]
        assert target.stat().st_gid == original.st_gid

    def test_atomic_fchmod_unsupported_does_not_fail_the_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A filesystem without Unix modes still gets the new contents (WR-02)."""

        def unsupported_fchmod(fd: int, mode: int) -> None:
            """Refuse the mode change the way such a filesystem does."""
            raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))

        monkeypatch.setattr(os, "fchmod", unsupported_fchmod)
        target = tmp_path / "config.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")

        replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _NEW.encode("utf-8")
        _leftovers(tmp_path)

    def test_atomic_unexpected_fchown_error_still_fails_and_cleans_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only "not permitted / not supported" is tolerated; EIO still fails."""

        def broken_fchown(fd: int, uid: int, gid: int) -> None:
            """Fail with an I/O error, which is not a refusal."""
            raise OSError(errno.EIO, os.strerror(errno.EIO))

        monkeypatch.setattr(os, "fchown", broken_fchown)
        target = tmp_path / "config.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")

        with pytest.raises(OSError, match=os.strerror(errno.EIO)):
            replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _ORIGINAL.encode("utf-8")
        _leftovers(tmp_path)

    def test_atomic_new_file_copies_no_owner_or_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With nothing to copy from, the new file keeps mkstemp's 0600."""
        calls = self._record_fchown_and_fchmod(monkeypatch)
        target = tmp_path / "config.toml"

        replace_file_atomically(target, _NEW)

        assert calls == []
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


class TestBindMount:
    """A single-file bind mount fails clearly (D-08); a directory mount works."""

    def test_ebusy_single_file_bind_mount_is_a_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        EBUSY becomes a ConfigError naming the file and the fix.

        The kernel refuses to rename over a bind-mount point. A raw OSError
        would leave the operator guessing; a non-atomic fallback would bring
        back the truncated-config risk this helper exists to remove.
        """

        def busy(self: Path, target: object) -> Path:
            """Refuse the rename the way a single-file bind mount does."""
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))

        monkeypatch.setattr(Path, "replace", busy)
        target = tmp_path / "config.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))

        with pytest.raises(ConfigError) as excinfo:
            replace_file_atomically(target, _NEW)

        message = str(excinfo.value)
        assert str(target.resolve()) in message
        assert "bind-mounted as a single file" in message
        assert "Mount its directory instead" in message
        assert "docs/how-to/deploy-docker-compose.md" in message
        assert target.read_bytes() == _ORIGINAL.encode("utf-8")
        _leftovers(tmp_path)

    def test_directory_bind_mount_layout_replaces_in_place(
        self, tmp_path: Path
    ) -> None:
        """
        The documented ``./config:/etc/saneless`` layout is rewritten normally.

        The temp file is created inside the mounted directory, beside
        ``config.toml``, and nothing else is left there afterwards (CFG-09).
        """
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        target = config_dir / "config.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")

        result = replace_file_atomically(target, _NEW)

        assert result == target.resolve()
        assert target.read_bytes() == _NEW.encode("utf-8")
        assert sorted(entry.name for entry in config_dir.iterdir()) == ["config.toml"]


# The real-kernel variant runs the helper inside an unprivileged user and mount
# namespace, where a bind mount needs no root. Every argv element is a literal;
# the per-test paths travel in the environment. It is skipped wherever user
# namespaces are unavailable (for example a restricted CI runner); the
# monkeypatched EBUSY test above is the requirement's evidence either way.
_UNSHARE = Path("/usr/bin/unshare")
_NAMESPACE_SCRIPT = """\
import sys
from pathlib import Path

from saneless.atomic_write import replace_file_atomically
from saneless.exceptions import ConfigError

try:
    replace_file_atomically(Path(sys.argv[1]), "new = 1\\n")
except ConfigError as exc:
    print(exc)
    sys.exit(3)
print("replaced")
"""


def _run_in_mount_namespace(
    source: Path, mount_point: Path, target: Path, *, read_only: bool = False
) -> subprocess.CompletedProcess[str]:
    """
    Bind-mount ``source`` on ``mount_point`` in a private namespace, then write.

    With ``read_only`` the bind mount is remounted read-only first, the
    legacy ``:ro`` compose mount.
    """
    env = {
        **os.environ,
        "SANELESS_TEST_SOURCE": str(source),
        "SANELESS_TEST_MOUNT_POINT": str(mount_point),
        "SANELESS_TEST_TARGET": str(target),
        "SANELESS_TEST_PYTHON": sys.executable,
        "SANELESS_TEST_SCRIPT": _NAMESPACE_SCRIPT,
        "SANELESS_TEST_READ_ONLY": "1" if read_only else "",
    }
    return subprocess.run(
        [
            "/usr/bin/unshare",
            "--user",
            "--map-root-user",
            "--mount",
            "/bin/sh",
            "-c",
            'mount --bind "$SANELESS_TEST_SOURCE" "$SANELESS_TEST_MOUNT_POINT" '
            '&& { [ -z "$SANELESS_TEST_READ_ONLY" ] '
            '|| mount -o remount,bind,ro "$SANELESS_TEST_MOUNT_POINT"; } '
            '&& exec "$SANELESS_TEST_PYTHON" -c "$SANELESS_TEST_SCRIPT" '
            '"$SANELESS_TEST_TARGET"',
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.fixture
def mount_namespace() -> None:
    """Skip unless this host lets an unprivileged user create a mount namespace."""
    if not _UNSHARE.is_file():
        pytest.skip("unshare is not installed")
    probe = subprocess.run(
        ["/usr/bin/unshare", "--user", "--map-root-user", "--mount", "/bin/true"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if probe.returncode != 0:
        pytest.skip("unprivileged user and mount namespaces are not available")


@pytest.mark.usefixtures("mount_namespace")
class TestRealBindMount:
    """The kernel's own answer for both mount shapes (D-08, CFG-09)."""

    def test_real_single_file_bind_mount_raises_config_error(
        self, tmp_path: Path
    ) -> None:
        """Renaming over a real single-file bind mount is EBUSY -> ConfigError."""
        host_file = tmp_path / "host-config.toml"
        host_file.write_text(_ORIGINAL, encoding="utf-8")
        container_dir = tmp_path / "etc-saneless"
        container_dir.mkdir()
        mounted = container_dir / "config.toml"
        mounted.write_text("", encoding="utf-8")

        completed = _run_in_mount_namespace(host_file, mounted, mounted)

        assert completed.returncode == 3, completed.stderr
        assert "Mount its directory instead" in completed.stdout
        assert host_file.read_text(encoding="utf-8") == _ORIGINAL
        _leftovers(container_dir)

    def test_real_read_only_single_file_mount_names_the_mount_fix(
        self, tmp_path: Path
    ) -> None:
        """
        A legacy ``:ro`` single-file mount gets D-08's fix, not EACCES (WR-01).

        ``os.access`` answers False on a read-only filesystem even for root, so
        without a mount check the operator was told "Permission denied" and
        went looking at file permissions.
        """
        host_file = tmp_path / "host-config.toml"
        host_file.write_text(_ORIGINAL, encoding="utf-8")
        container_dir = tmp_path / "etc-saneless"
        container_dir.mkdir()
        mounted = container_dir / "config.toml"
        mounted.write_text("", encoding="utf-8")

        completed = _run_in_mount_namespace(host_file, mounted, mounted, read_only=True)

        assert completed.returncode == 3, completed.stderr
        assert "bind-mounted as a single file" in completed.stdout
        assert "Mount its directory instead" in completed.stdout
        assert host_file.read_text(encoding="utf-8") == _ORIGINAL
        _leftovers(container_dir)

    def test_real_read_only_directory_mount_names_the_mount_fix(
        self, tmp_path: Path
    ) -> None:
        """A config in a ``:ro`` directory mount is reported as a read-only mount."""
        host_dir = tmp_path / "config"
        host_dir.mkdir()
        host_file = host_dir / "config.toml"
        host_file.write_text(_ORIGINAL, encoding="utf-8")
        container_dir = tmp_path / "etc-saneless"
        container_dir.mkdir()

        completed = _run_in_mount_namespace(
            host_dir, container_dir, container_dir / "config.toml", read_only=True
        )

        assert completed.returncode == 3, completed.stderr
        assert "read-only mount" in completed.stdout
        assert "read-write" in completed.stdout
        assert host_file.read_text(encoding="utf-8") == _ORIGINAL

    def test_real_directory_bind_mount_replaces_the_host_file(
        self, tmp_path: Path
    ) -> None:
        """Through a real directory bind mount, the host's config.toml changes."""
        host_dir = tmp_path / "config"
        host_dir.mkdir()
        host_file = host_dir / "config.toml"
        host_file.write_text(_ORIGINAL, encoding="utf-8")
        container_dir = tmp_path / "etc-saneless"
        container_dir.mkdir()

        completed = _run_in_mount_namespace(
            host_dir, container_dir, container_dir / "config.toml"
        )

        assert completed.returncode == 0, completed.stderr
        assert "replaced" in completed.stdout
        assert host_file.read_text(encoding="utf-8") == "new = 1\n"
        assert sorted(entry.name for entry in host_dir.iterdir()) == ["config.toml"]
