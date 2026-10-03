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
* Every extended attribute -- a POSIX ACL included -- is copied onto the temp
  file before its owner and mode, so an ACL survives exactly and the owning
  group is never handed the ACL mask. An attribute that cannot be copied
  refuses the rewrite; a filesystem with no extended attribute support has
  nothing to copy. A ``security.*`` label is copied only when the temp
  file's differs, and one the kernel refuses to set is a WARNING, not a
  refusal; IMA and EVM measurements are never copied.
* An owner or group that cannot be kept is logged at WARNING with its ids.
* Temp files a killed writer left beside the target -- regular, owned by this
  user, older than ten minutes -- are removed before the next rewrite; a
  fresh one, another user's, and a symlink are left alone.
* The mount errors cite the published deployment guide, and that URL maps to
  an existing page and heading under ``docs/``.
* A new file takes its directory's owner and group when this process may set
  them, and stays 0600 either way.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from saneless.atomic_write import (
    make_config_directory,
    refused_mode_change,
    replace_file_atomically,
)
from saneless.exceptions import ConfigError

_ORIGINAL = "[profiles.default]\nsource = 'Flatbed'\n"
_NEW = "a = 1\r\n# café\n"

# Where the mount errors send the operator: the published page, because a
# reader of the error has the site, not the repository.
_PUBLISHED_DOCS_URL = (
    "https://kdknigga.github.io/saneless/how-to/deploy-docker-compose/"
    "#moving-from-a-single-file-config-mount"
)
_REPO_ROOT = Path(__file__).resolve().parents[1]

# The Linux ``system.posix_acl_access`` value: a little-endian version word,
# then one (tag, permissions, id) entry per ACL line. Building it by hand lets
# the ACL tests run with no ACL tool installed, and on tmpfs, which accepts ACLs
# but refuses ``user.*`` attributes.
ACL_XATTR = "system.posix_acl_access"
ACL_VERSION = 2
ACL_USER_OBJ = 0x01
ACL_USER = 0x02
ACL_GROUP_OBJ = 0x04
ACL_MASK = 0x10
ACL_OTHER = 0x20
ACL_UNDEFINED_ID = 0xFFFFFFFF
_ACL_HEADER = struct.Struct("<I")
_ACL_ENTRY = struct.Struct("<HHI")


def acl_blob(entries: list[tuple[int, int, int]]) -> bytes:
    """
    Encode ``entries`` as a ``system.posix_acl_access`` attribute value.

    Args:
        entries: ``(tag, permissions, id)`` triples, in the kernel's order
            (owner, named users, owning group, mask, other); ``id`` is
            ``ACL_UNDEFINED_ID`` for every entry but a named user or group.

    Returns:
        The bytes ``os.setxattr`` takes to set that ACL.

    """
    return _ACL_HEADER.pack(ACL_VERSION) + b"".join(
        _ACL_ENTRY.pack(tag, perm, ident) for tag, perm, ident in entries
    )


def decode_acl(blob: bytes) -> list[tuple[int, int, int]]:
    """Decode a ``system.posix_acl_access`` value into its entries."""
    (version,) = _ACL_HEADER.unpack_from(blob)
    assert version == ACL_VERSION
    return list(_ACL_ENTRY.iter_unpack(blob[_ACL_HEADER.size :]))


# ``u::rw- u:65534:rw- g::r-- m::rw- o::---``: a named user may write, the
# owning group may only read. The mode's group bits show the mask (rw-), so a
# rewrite that copies the mode but drops the ACL hands the group write access.
_SHARED_ACL = acl_blob(
    [
        (ACL_USER_OBJ, 6, ACL_UNDEFINED_ID),
        (ACL_USER, 6, 65534),
        (ACL_GROUP_OBJ, 4, ACL_UNDEFINED_ID),
        (ACL_MASK, 6, ACL_UNDEFINED_ID),
        (ACL_OTHER, 0, ACL_UNDEFINED_ID),
    ]
)


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


def _fake_link_owner(monkeypatch: pytest.MonkeyPatch, link: Path, uid: int) -> None:
    """
    Make ``os.lstat`` report the symlink ``link`` as made by ``uid``.

    Every other path, and every other field, is answered for real, so the
    link still reads as a link. Nothing is chowned: a non-root test cannot
    give a link away.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        link: The symlink whose owner to fake; it is not resolved.
        uid: The owner to report.

    """
    real_lstat = os.lstat
    faked = os.fspath(link)

    def fake_lstat(
        path: str | os.PathLike[str], *, dir_fd: int | None = None
    ) -> os.stat_result:
        result = real_lstat(path, dir_fd=dir_fd)
        if os.fspath(path) != faked:
            return result
        fields = list(result)
        fields[stat.ST_UID] = uid
        return os.stat_result(fields)

    monkeypatch.setattr(os, "lstat", fake_lstat)


def _is_directory_fd(fd: int) -> bool:
    """Return True when ``fd`` refers to a directory."""
    return stat.S_ISDIR(os.fstat(fd).st_mode)


class TestAtomicReplace:
    """A successful rewrite is byte-exact, durable, and tidy (D-05)."""

    def test_atomic_replace_writes_crlf_and_utf8_bytes_exactly(
        self, tmp_path: Path
    ) -> None:
        """CRLF endings and a non-ASCII comment reach disk unchanged, as UTF-8."""
        target = tmp_path / "saneless.toml"
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
        target = tmp_path / "saneless.toml"

        replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _NEW.encode("utf-8")
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_atomic_replace_leaves_only_the_target(self, tmp_path: Path) -> None:
        """After success the directory holds the target and nothing else."""
        target = tmp_path / "saneless.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")

        replace_file_atomically(target, _NEW)

        _leftovers(tmp_path)
        assert sorted(entry.name for entry in tmp_path.iterdir()) == ["saneless.toml"]

    def test_atomic_temp_file_is_created_in_the_target_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The temp file shares the target's directory.

        ``rename(2)`` is atomic only within one filesystem; a temp file in
        /tmp would fail with EXDEV against a mounted config directory.
        """
        directories = _record_mkstemp(monkeypatch)
        target = tmp_path / "saneless.toml"
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
        target = tmp_path / "saneless.toml"
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
        target = tmp_path / "saneless.toml"
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
        target = tmp_path / "saneless.toml"
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
        target = tmp_path / "saneless.toml"
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
        real = real_dir / "saneless.toml"
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
        target = tmp_path / "saneless.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        _deny_write_access(monkeypatch, target)
        directories = _record_mkstemp(monkeypatch)

        with pytest.raises(PermissionError):
            replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _ORIGINAL.encode("utf-8")
        assert directories == [], "a temp file was created before refusing"
        _leftovers(tmp_path)

    def test_a_dangling_symlink_is_refused_rather_than_followed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A new file is never created at the far end of a link.

        Writing through a link is for a file the operator already has.  A link
        to nothing names a file no load ever read, and whoever can write the
        link's directory could point it anywhere -- a root writer would then
        create that file and hand it to the far directory's owner.
        """
        conf = tmp_path / "conf"
        conf.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        planted = elsewhere / "planted.conf"
        link = conf / "saneless.toml"
        link.symlink_to(planted)
        directories = _record_mkstemp(monkeypatch)

        with pytest.raises(ConfigError) as excinfo:
            replace_file_atomically(link, _NEW)

        message = str(excinfo.value)
        assert str(link) in message
        assert str(planted) in message
        assert "symlink" in message
        assert not planted.exists()
        assert link.is_symlink()
        assert directories == [], "a temp file was created before refusing"
        _leftovers(elsewhere)

    def test_root_does_not_rewrite_a_file_the_links_maker_does_not_own(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A planted link to an existing file cannot aim a root rewrite at it.

        Whoever made the link chose where it points. Followed as root, it
        would replace any file that parses as TOML -- an empty one does -- so
        root writes through a link only to a file the link's maker owns, and
        could therefore write anyway. Here the link's directory is the
        maker's too, and the file is someone else's.
        """
        conf = tmp_path / "conf"
        conf.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        victim = elsewhere / "victim.conf"
        victim.write_bytes(b"")
        link = conf / "saneless.toml"
        link.symlink_to(victim)
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        _fake_directory_owner(monkeypatch, conf, os.getuid() + 1, os.getgid())
        _fake_link_owner(monkeypatch, link, os.getuid() + 1)
        directories = _record_mkstemp(monkeypatch)

        with pytest.raises(ConfigError) as excinfo:
            replace_file_atomically(link, _NEW)

        message = str(excinfo.value)
        assert str(link) in message
        assert str(victim) in message
        assert victim.read_bytes() == b""
        assert link.is_symlink()
        assert directories == [], "a temp file was created before refusing"

    def test_root_does_not_follow_a_link_a_co_writer_planted_in_its_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Owning the link's directory is not the same as making the link.

        A root-owned config directory the service may also write, through its
        group or an ACL, lets the service plant a link to a file the
        directory's owner owns. The directory and the file then share an
        owner, but the link's maker owns neither, so root refuses it.
        """
        conf = tmp_path / "conf"
        conf.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        victim = elsewhere / "victim.conf"
        victim.write_bytes(b"")
        link = conf / "saneless.toml"
        link.symlink_to(victim)
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        _fake_directory_owner(monkeypatch, conf, os.getuid(), os.getgid())
        _fake_link_owner(monkeypatch, link, os.getuid() + 1)
        directories = _record_mkstemp(monkeypatch)

        with pytest.raises(ConfigError) as excinfo:
            replace_file_atomically(link, _NEW)

        message = str(excinfo.value)
        assert str(link) in message
        assert str(victim) in message
        assert victim.read_bytes() == b""
        assert link.is_symlink()
        assert directories == [], "a temp file was created before refusing"

    def test_root_writes_through_a_link_made_by_its_files_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A dotfiles link, or the compose ``./config`` one, still works as root.

        The link and the file it points to share an owner, whoever owns the
        link's directory, so root can reach only what that owner could write.
        """
        conf = tmp_path / "conf"
        conf.mkdir()
        real = tmp_path / "dotfiles" / "saneless.toml"
        real.parent.mkdir()
        real.write_text(_ORIGINAL, encoding="utf-8")
        link = conf / "saneless.toml"
        link.symlink_to(real)
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        _fake_directory_owner(monkeypatch, conf, 0, 0)

        result = replace_file_atomically(link, _NEW)

        assert result == real.resolve()
        assert real.read_bytes() == _NEW.encode("utf-8")
        assert link.is_symlink()

    def test_root_writes_through_its_own_link_to_another_users_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A link root made itself is followed wherever it points.

        Root in its own ``/etc/saneless`` may link the config to a file a
        user keeps; the choice of target was root's, so nothing is gained by
        refusing it.
        """
        conf = tmp_path / "conf"
        conf.mkdir()
        real = tmp_path / "home" / "saneless.toml"
        real.parent.mkdir()
        real.write_text(_ORIGINAL, encoding="utf-8")
        link = conf / "saneless.toml"
        link.symlink_to(real)
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        _fake_directory_owner(monkeypatch, conf, 0, 0)
        _fake_link_owner(monkeypatch, link, 0)

        result = replace_file_atomically(link, _NEW)

        assert result == real.resolve()
        assert real.read_bytes() == _NEW.encode("utf-8")
        assert link.is_symlink()


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

    def test_read_only_single_file_mount_tells_the_operator_to_mount_the_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read-only file in a writable directory is its own mount: D-08."""
        target = tmp_path / "saneless.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        self._fake_read_only(monkeypatch, {target.resolve()})
        directories = _record_mkstemp(monkeypatch)

        with pytest.raises(ConfigError) as excinfo:
            replace_file_atomically(target, _NEW)

        message = str(excinfo.value)
        assert str(target.resolve()) in message
        assert "bind-mounted as a single file" in message
        assert "Mount its directory instead" in message
        assert _PUBLISHED_DOCS_URL in message
        assert "docs/how-to/" not in message
        assert "Permission denied" not in message
        assert target.read_bytes() == _ORIGINAL.encode("utf-8")
        assert directories == [], "a temp file was created before refusing"

    def test_read_only_directory_mount_is_a_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file and its directory on one read-only mount name that mount."""
        target = tmp_path / "saneless.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        self._fake_read_only(monkeypatch, {target.resolve(), tmp_path.resolve()})

        with pytest.raises(ConfigError) as excinfo:
            replace_file_atomically(target, _NEW)

        message = str(excinfo.value)
        assert str(target.resolve()) in message
        assert "read-only mount" in message
        assert "read-write" in message
        assert _PUBLISHED_DOCS_URL in message
        assert "docs/how-to/" not in message
        assert target.read_bytes() == _ORIGINAL.encode("utf-8")

    def test_writable_mount_read_only_file_is_still_permission_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unwritable file on a writable mount is refused as read-only."""
        target = tmp_path / "saneless.toml"
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
        target = tmp_path / "saneless.toml"
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
        target = tmp_path / "saneless.toml"
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
        target = tmp_path / "saneless.toml"
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
        target = tmp_path / "saneless.toml"
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
        target = tmp_path / "saneless.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")
        original = target.stat()

        replace_file_atomically(target, _NEW)

        assert calls == [
            (original.st_uid, original.st_gid),
            (-1, original.st_gid),
        ]
        assert target.stat().st_gid == original.st_gid

    def test_refused_owner_copy_is_a_warning_naming_the_ids(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A rewrite that changes who owns the file says so at WARNING.

        The write still happens, but the file now belongs to the writer; an
        operator who later cannot edit it without sudo needs to see why.
        """

        def refused_fchown(fd: int, uid: int, gid: int) -> None:
            """Refuse every ownership change, as for a non-root caller."""
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

        monkeypatch.setattr(os, "fchown", refused_fchown)
        target = tmp_path / "saneless.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")
        original = target.stat()
        caplog.set_level(logging.DEBUG, logger="saneless.atomic_write")

        replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _NEW.encode("utf-8")
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 2, warnings
        owner_warning, group_warning = warnings
        assert str(original.st_uid) in owner_warning
        assert str(original.st_gid) in owner_warning
        assert str(original.st_gid) in group_warning

    def test_refused_owner_with_kept_group_is_one_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A lost owner warns once; the group that was kept adds nothing."""
        real_fchown = os.fchown

        def owner_refused(fd: int, uid: int, gid: int) -> None:
            """Refuse an owner change; allow a group-only change."""
            if uid != -1:
                raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))
            real_fchown(fd, uid, gid)

        monkeypatch.setattr(os, "fchown", owner_refused)
        target = tmp_path / "saneless.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")
        original = target.stat()
        caplog.set_level(logging.DEBUG, logger="saneless.atomic_write")

        replace_file_atomically(target, _NEW)

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1, warnings
        assert str(original.st_uid) in warnings[0]

    def test_atomic_fchmod_unsupported_does_not_fail_the_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A filesystem without Unix modes still gets the new contents (WR-02)."""

        def unsupported_fchmod(fd: int, mode: int) -> None:
            """Refuse the mode change the way such a filesystem does."""
            raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))

        monkeypatch.setattr(os, "fchmod", unsupported_fchmod)
        target = tmp_path / "saneless.toml"
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
        target = tmp_path / "saneless.toml"
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
        target = tmp_path / "saneless.toml"

        replace_file_atomically(target, _NEW)

        assert calls == []
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


class _XattrRecorder:
    """
    Stand-ins for ``os.listxattr``/``getxattr``/``setxattr`` that log each call.

    tmpfs, where ``tmp_path`` lives on many hosts, refuses ``user.*``
    attributes, so the attribute tests fake the three calls and read the
    log. ``setxattr`` records without setting anything.
    """

    def __init__(
        self,
        attributes: dict[str, bytes],
        refused: frozenset[str] = frozenset(),
        *,
        on_temp: dict[str, bytes] | None = None,
        refusal: int = errno.EPERM,
    ) -> None:
        """
        Serve ``attributes`` from the original and ``on_temp`` from the temp.

        The temp file is the one read through a descriptor. Setting a name in
        ``refused`` fails with ``refusal``.
        """
        self.attributes = attributes
        self.refused = refused
        self.on_temp = on_temp or {}
        self.refusal = refusal
        self.calls: list[tuple[str, ...]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace the three ``os`` functions with this recorder's."""
        monkeypatch.setattr(os, "listxattr", self.listxattr)
        monkeypatch.setattr(os, "getxattr", self.getxattr)
        monkeypatch.setattr(os, "setxattr", self.setxattr)

    def listxattr(
        self,
        path: int | str | os.PathLike[str] | None = None,
        *,
        follow_symlinks: bool = True,
    ) -> list[str]:
        """List the served attribute names."""
        self.calls.append(("listxattr",))
        return list(self.attributes)

    def getxattr(
        self,
        path: int | str | os.PathLike[str],
        attribute: str,
        *,
        follow_symlinks: bool = True,
    ) -> bytes:
        """
        Return a served attribute's value, the temp file's when given a descriptor.

        Raises:
            OSError: ENODATA, for an attribute the temp file does not carry.

        """
        if isinstance(path, int):
            self.calls.append(("getxattr", attribute, "fd"))
            if attribute not in self.on_temp:
                raise OSError(errno.ENODATA, os.strerror(errno.ENODATA))
            return self.on_temp[attribute]
        self.calls.append(("getxattr", attribute))
        return self.attributes[attribute]

    def setxattr(
        self,
        path: int | str | os.PathLike[str],
        attribute: str,
        value: bytes,
        flags: int = 0,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        """Log the call, noting whether it targets a descriptor; maybe refuse."""
        on_fd = "fd" if isinstance(path, int) else "path"
        self.calls.append(("setxattr", attribute, on_fd))
        if attribute in self.refused:
            raise OSError(self.refusal, os.strerror(self.refusal))


class TestExtendedAttributes:
    """
    ACLs and other extended attributes survive the rewrite, or it is refused.

    ``chmod`` on a file with an ACL sets the ACL *mask* from the group bits,
    and an ACL'd file's group bits already show the mask. Copying the mode
    without the ACL therefore hands the owning group whatever the mask
    allowed a named user.
    """

    def test_posix_acl_survives_the_rewrite_exactly(self, tmp_path: Path) -> None:
        """The ACL comes back byte-identical, and the owning group stays r--."""
        target = tmp_path / "saneless.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        target.chmod(0o600)
        try:
            os.setxattr(target, ACL_XATTR, _SHARED_ACL)
        except OSError as exc:
            if exc.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise
            pytest.skip(f"{tmp_path} does not support POSIX ACLs")
        original_acl = os.getxattr(target, ACL_XATTR)

        replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _NEW.encode("utf-8")
        rewritten_acl = os.getxattr(target, ACL_XATTR)
        assert rewritten_acl == original_acl
        group_perms = [
            perm for tag, perm, _ in decode_acl(rewritten_acl) if tag == ACL_GROUP_OBJ
        ]
        assert group_perms == [4], (
            "the owning group's ACL entry changed; it must stay r--, never "
            "take the mask's rw-"
        )
        _leftovers(tmp_path)

    def test_user_xattr_is_copied_and_a_matching_label_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``user.comment`` is copied onto the temp file; an equal label is not set.

        The new file takes its ``security.*`` label from the directory's
        policy, which is usually the original's already, and setting one needs
        a relabel permission a confined container lacks -- so it is only asked
        for when the labels differ.
        """
        label = b"system_u:object_r:etc_t:s0\x00"
        recorder = _XattrRecorder(
            {"user.comment": b"scanned by the office", "security.selinux": label},
            on_temp={"security.selinux": label},
        )
        target = tmp_path / "saneless.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        recorder.install(monkeypatch)

        replace_file_atomically(target, _NEW)

        assert ("getxattr", "user.comment") in recorder.calls
        assert ("setxattr", "user.comment", "fd") in recorder.calls
        assert ("setxattr", "security.selinux", "fd") not in recorder.calls
        assert target.read_bytes() == _NEW.encode("utf-8")

    def test_a_differing_label_is_copied_onto_the_temp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A relabelled original keeps its label through the rewrite.

        A file an admin gave a confined service's type must not fall back to
        the directory's default type, which can change who may read it.
        """
        recorder = _XattrRecorder(
            {"security.selinux": b"system_u:object_r:saneless_conf_t:s0\x00"},
            on_temp={"security.selinux": b"system_u:object_r:etc_t:s0\x00"},
        )
        target = tmp_path / "saneless.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        recorder.install(monkeypatch)

        replace_file_atomically(target, _NEW)

        assert ("setxattr", "security.selinux", "fd") in recorder.calls
        assert target.read_bytes() == _NEW.encode("utf-8")

    def test_a_label_the_temp_file_lacks_is_copied_onto_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Smack labels a new file with the writer's label, or none at all."""
        recorder = _XattrRecorder({"security.SMACK64": b"saneless\x00"})
        target = tmp_path / "saneless.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        recorder.install(monkeypatch)

        replace_file_atomically(target, _NEW)

        assert ("setxattr", "security.SMACK64", "fd") in recorder.calls

    @pytest.mark.parametrize(
        "refusal",
        [errno.EACCES, errno.EPERM, errno.EINVAL],
        ids=["EACCES", "EPERM", "EINVAL"],
    )
    def test_a_label_the_kernel_refuses_is_warned_about_not_fatal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        refusal: int,
    ) -> None:
        """
        A confined writer that may not relabel still writes, and says so.

        A container rewriting a config the host user created is refused the
        host user's label although the two differ only in the SELinux user, so
        refusing the rewrite would break the common case. The WARNING names
        the file and the attribute, never the label itself.
        """
        original = b"unconfined_u:object_r:container_file_t:s0\x00"
        recorder = _XattrRecorder(
            {"security.selinux": original},
            refused=frozenset({"security.selinux"}),
            on_temp={"security.selinux": b"system_u:object_r:container_file_t:s0\x00"},
            refusal=refusal,
        )
        target = tmp_path / "saneless.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        recorder.install(monkeypatch)
        caplog.set_level(logging.WARNING, logger="saneless.atomic_write")

        replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _NEW.encode("utf-8")
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.name == "saneless.atomic_write"
            and record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert str(target.resolve()) in warnings[0]
        assert "'security.selinux'" in warnings[0]
        assert "container_file_t" not in warnings[0]
        _leftovers(tmp_path)

    def test_a_label_that_fails_for_another_reason_refuses_the_rewrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a refusal is survivable; an I/O error keeps the original."""
        recorder = _XattrRecorder(
            {"security.selinux": b"system_u:object_r:etc_t:s0\x00"},
            refused=frozenset({"security.selinux"}),
            refusal=errno.EIO,
        )
        target = tmp_path / "saneless.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        recorder.install(monkeypatch)

        with pytest.raises(ConfigError, match=r"'security\.selinux'"):
            replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _ORIGINAL.encode("utf-8")
        _leftovers(tmp_path)

    @pytest.mark.parametrize("name", ["security.ima", "security.evm"])
    def test_content_measurements_are_never_copied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        """
        IMA's hash and EVM's HMAC describe the old bytes, not the new ones.

        Copying one would stamp the rewritten file with a measurement it
        fails, and the kernel computes the new file's own.
        """
        recorder = _XattrRecorder({name: b"\x04\x01measurement"})
        target = tmp_path / "saneless.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        recorder.install(monkeypatch)

        replace_file_atomically(target, _NEW)

        assert [call for call in recorder.calls if name in call] == []
        assert target.read_bytes() == _NEW.encode("utf-8")

    def test_a_real_selinux_label_survives_the_rewrite(self, tmp_path: Path) -> None:
        """
        On an SELinux host, a relabelled file keeps its type through the rewrite.

        Skipped where the kernel has no SELinux labels, where the policy has
        no MLS field to carry over (a ``user:role:type`` context), or where
        this user may not relabel a file it owns.
        """
        target = tmp_path / "saneless.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        name = "security.selinux"
        try:
            current = os.getxattr(target, name)
        except OSError as exc:
            pytest.skip(f"no SELinux label on {tmp_path}: {exc.strerror}")
        fields = current.split(b":", 3)
        if len(fields) != 4:
            pytest.skip(f"the SELinux context on {tmp_path} has no MLS field")
        user, _role, type_, rest = fields
        replacement = b"user_home_t" if type_ != b"user_home_t" else b"user_tmp_t"
        relabelled = b":".join((user, b"object_r", replacement, rest))
        try:
            os.setxattr(target, name, relabelled)
        except OSError as exc:
            pytest.skip(f"this user may not relabel a file: {exc.strerror}")

        replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _NEW.encode("utf-8")
        assert os.getxattr(target, name) == relabelled
        _leftovers(tmp_path)

    def test_attribute_that_cannot_be_copied_refuses_the_rewrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused attribute stops the write: the original and nothing else remain."""
        recorder = _XattrRecorder(
            {"user.comment": b"scanned by the office"},
            refused=frozenset({"user.comment"}),
        )
        target = tmp_path / "saneless.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        recorder.install(monkeypatch)

        with pytest.raises(ConfigError) as excinfo:
            replace_file_atomically(target, _NEW)

        message = str(excinfo.value)
        assert str(target.resolve()) in message
        assert "'user.comment'" in message
        assert "rewrite it as its owner or remove the attribute" in message
        assert target.read_bytes() == _ORIGINAL.encode("utf-8")
        _leftovers(tmp_path)

    def test_filesystem_without_xattr_support_still_rewrites(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``listxattr`` answering ENOTSUP means there is nothing to copy."""

        def unsupported(
            path: int | str | os.PathLike[str] | None = None,
            *,
            follow_symlinks: bool = True,
        ) -> list[str]:
            """Refuse the way a filesystem without xattrs does."""
            raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))

        monkeypatch.setattr(os, "listxattr", unsupported)
        target = tmp_path / "saneless.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))

        replace_file_atomically(target, _NEW)

        assert target.read_bytes() == _NEW.encode("utf-8")
        _leftovers(tmp_path)

    def test_attributes_are_set_before_owner_and_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Every attribute is copied, then the owner, then the mode.

        Setting ``user.*`` needs write permission on the file, which the
        writer may no longer have once the original's owner and mode are on
        it; and ``fchmod`` after the ACL keeps the group bits on the mask.
        """
        recorder = _XattrRecorder(
            {
                "user.comment": b"scanned by the office",
                "user.origin": b"host",
            }
        )
        real_fchown = os.fchown
        real_fchmod = os.fchmod

        def recording_fchown(fd: int, uid: int, gid: int) -> None:
            """Log the owner change, then make it."""
            recorder.calls.append(("fchown",))
            real_fchown(fd, uid, gid)

        def recording_fchmod(fd: int, mode: int) -> None:
            """Log the mode change, then make it."""
            recorder.calls.append(("fchmod",))
            real_fchmod(fd, mode)

        target = tmp_path / "saneless.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))
        recorder.install(monkeypatch)
        monkeypatch.setattr(os, "fchown", recording_fchown)
        monkeypatch.setattr(os, "fchmod", recording_fchmod)

        replace_file_atomically(target, _NEW)

        on_temp = [
            call[0]
            for call in recorder.calls
            if call[0] in {"setxattr", "fchown", "fchmod"}
        ]
        assert on_temp == ["setxattr", "setxattr", "fchown", "fchmod"]

    def test_new_file_copies_no_attributes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no original there is nothing to read attributes from."""
        recorder = _XattrRecorder({"user.comment": b"scanned by the office"})
        recorder.install(monkeypatch)
        target = tmp_path / "saneless.toml"

        replace_file_atomically(target, _NEW)

        assert recorder.calls == []
        assert target.read_bytes() == _NEW.encode("utf-8")


_STALE_AGE_SECONDS = 11 * 60


def _age(path: Path, seconds: float) -> None:
    """Set ``path``'s own times ``seconds`` into the past, not a link target's."""
    then = time.time() - seconds
    os.utime(path, (then, then), follow_symlinks=False)


def _info_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the INFO messages ``saneless.atomic_write`` logged."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "saneless.atomic_write" and record.levelno == logging.INFO
    ]


class TestStaleTempSweep:
    """
    A temp file a killed writer left behind is removed by the next rewrite.

    It is a stray copy of the config, possibly holding the Paperless token.
    Only this helper's own leftovers go: a matching name, a regular file this
    user owns, and old enough that no rewrite can still be writing it -- the
    worker's start-up generation and a CLI run may overlap.
    """

    def test_old_own_temp_is_removed_and_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An eleven-minute-old ``.saneless.toml.XXXXXXXX.tmp`` is swept."""
        target = tmp_path / "saneless.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")
        stale = tmp_path / ".saneless.toml.abcd1234.tmp"
        stale.write_text("token = 'left by a killed writer'\n", encoding="utf-8")
        _age(stale, _STALE_AGE_SECONDS)
        caplog.set_level(logging.INFO, logger="saneless.atomic_write")

        replace_file_atomically(target, _NEW)

        assert not stale.exists()
        assert any(stale.name in message for message in _info_messages(caplog))
        assert sorted(entry.name for entry in tmp_path.iterdir()) == ["saneless.toml"]

    def test_fresh_temp_is_kept(self, tmp_path: Path) -> None:
        """A temp written just now may belong to a rewrite still in progress."""
        target = tmp_path / "saneless.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")
        fresh = tmp_path / ".saneless.toml.abcd1234.tmp"
        fresh.write_text("in flight\n", encoding="utf-8")

        replace_file_atomically(target, _NEW)

        assert fresh.read_text(encoding="utf-8") == "in flight\n"

    @pytest.mark.parametrize(
        "name",
        [
            ".other.toml.abcd1234.tmp",
            ".saneless.toml.abc1234.tmp",
            ".saneless.toml.abcd1234.tmp.bak",
            "saneless.toml.abcd1234.tmp",
        ],
    )
    def test_names_outside_the_temp_pattern_are_kept(
        self, tmp_path: Path, name: str
    ) -> None:
        """Only the exact temp name of *this* target is ever swept."""
        target = tmp_path / "saneless.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")
        bystander = tmp_path / name
        bystander.write_text("not ours\n", encoding="utf-8")
        _age(bystander, _STALE_AGE_SECONDS)

        replace_file_atomically(target, _NEW)

        assert bystander.read_text(encoding="utf-8") == "not ours\n"

    def test_another_users_temp_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A temp this user does not own is some other writer's business."""
        target = tmp_path / "saneless.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")
        foreign = tmp_path / ".saneless.toml.abcd1234.tmp"
        foreign.write_text("theirs\n", encoding="utf-8")
        _age(foreign, _STALE_AGE_SECONDS)
        # The file is ours, so the writer is made to look like someone else.
        other_uid = os.geteuid() + 1
        monkeypatch.setattr(os, "geteuid", lambda: other_uid)

        replace_file_atomically(target, _NEW)

        assert foreign.read_text(encoding="utf-8") == "theirs\n"

    def test_symlink_with_the_temp_name_is_kept_and_its_target_untouched(
        self, tmp_path: Path
    ) -> None:
        """A planted symlink is neither removed nor followed."""
        target = tmp_path / "saneless.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")
        victim = tmp_path / "victim.txt"
        victim.write_text("keep me\n", encoding="utf-8")
        _age(victim, _STALE_AGE_SECONDS)
        link = tmp_path / ".saneless.toml.zzzz9999.tmp"
        link.symlink_to(victim)
        _age(link, _STALE_AGE_SECONDS)

        replace_file_atomically(target, _NEW)

        assert link.is_symlink()
        assert victim.read_text(encoding="utf-8") == "keep me\n"


def _mkdocs_slug(heading: str) -> str:
    """Slug a heading the way Python-Markdown's default ``toc`` does."""
    text = re.sub(r"[^\w\s-]", "", heading).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


class TestPublishedDocsUrl:
    """The URL the mount errors cite is a real page and heading in ``docs/``."""

    def test_published_docs_url_names_an_existing_page_and_heading(self) -> None:
        """
        The URL's path is a page under ``docs/``; its fragment is a heading.

        Checked offline against the sources the site is built from, so moving
        the page or renaming the heading fails here rather than in front of
        an operator.
        """
        mkdocs = (_REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
        site_url = re.search(r"^site_url:\s*(\S+)\s*$", mkdocs, re.MULTILINE)
        assert site_url is not None
        base = site_url.group(1)
        assert _PUBLISHED_DOCS_URL.startswith(base)
        page_path, _, fragment = _PUBLISHED_DOCS_URL.removeprefix(base).partition("#")

        page = _REPO_ROOT / "docs" / f"{page_path.rstrip('/')}.md"

        assert page == _REPO_ROOT / "docs" / "how-to" / "deploy-docker-compose.md"
        assert page.is_file()
        prose = re.sub(
            r"^```.*?^```",
            "",
            page.read_text(encoding="utf-8"),
            flags=re.MULTILINE | re.DOTALL,
        )
        slugs = {
            _mkdocs_slug(match.group(1))
            for match in re.finditer(r"^#{1,6}\s+(.+?)\s*$", prose, re.MULTILINE)
        }
        assert fragment in slugs, sorted(slugs)


def _fake_directory_owner(
    monkeypatch: pytest.MonkeyPatch, directory: Path, uid: int, gid: int
) -> None:
    """
    Make ``os.stat`` report ``directory`` as owned by ``uid``:``gid``.

    Every other path, and every other field, is answered for real. Nothing is
    chowned: a non-root test cannot give a directory away.
    """
    real_stat = os.stat
    faked = os.fspath(directory.resolve())

    def fake_stat(
        path: int | str | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        """Return the real status, with the directory's owner replaced."""
        result = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if isinstance(path, int) or os.fspath(path) != faked:
            return result
        fields = list(result)
        fields[stat.ST_UID] = uid
        fields[stat.ST_GID] = gid
        return os.stat_result(fields)

    monkeypatch.setattr(os, "stat", fake_stat)


def _record_fchown(
    monkeypatch: pytest.MonkeyPatch, *, refuse: bool
) -> list[tuple[int, int]]:
    """
    Record every ``fchown`` without performing it; optionally refuse it.

    Not performing it stands in for root, who may give the file away; the
    refusal stands in for everyone else.
    """
    calls: list[tuple[int, int]] = []

    def fchown(fd: int, uid: int, gid: int) -> None:
        """Note the requested owner; refuse it when asked to."""
        calls.append((uid, gid))
        if refuse:
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(os, "fchown", fchown)
    return calls


class TestNewFileOwner:
    """
    A config this process creates belongs to its directory's owner.

    A root ``docker compose exec`` writing into the service's config
    directory must leave a file the service can read. The mode stays 0600
    whoever ends up owning it, because a new config may hold the token.
    """

    def test_new_file_takes_the_directory_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The temp file is given the directory's uid and gid before the rename."""
        dir_uid, dir_gid = os.geteuid() + 1, os.getegid() + 1
        _fake_directory_owner(monkeypatch, tmp_path, dir_uid, dir_gid)
        calls = _record_fchown(monkeypatch, refuse=False)
        target = tmp_path / "saneless.toml"

        replace_file_atomically(target, _NEW)

        assert calls == [(dir_uid, dir_gid)]
        assert target.read_bytes() == _NEW.encode("utf-8")
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_refused_directory_owner_keeps_the_writer_and_still_writes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A writer that may not give the file away still writes it, as itself."""
        dir_uid, dir_gid = os.geteuid() + 1, os.getegid() + 1
        _fake_directory_owner(monkeypatch, tmp_path, dir_uid, dir_gid)
        calls = _record_fchown(monkeypatch, refuse=True)
        target = tmp_path / "saneless.toml"
        caplog.set_level(logging.DEBUG, logger="saneless.atomic_write")

        replace_file_atomically(target, _NEW)

        assert calls == [(dir_uid, dir_gid)]
        assert target.read_bytes() == _NEW.encode("utf-8")
        written = target.stat()
        assert written.st_uid == os.geteuid()
        assert stat.S_IMODE(written.st_mode) == 0o600
        records = [r for r in caplog.records if r.name == "saneless.atomic_write"]
        assert [r.levelno for r in records] == [logging.DEBUG]
        assert str(dir_uid) in records[0].getMessage()
        _leftovers(tmp_path)

    def test_directory_owned_by_the_writer_needs_no_chown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The file already belongs to the directory's owner: nothing to change."""
        _fake_directory_owner(monkeypatch, tmp_path, os.geteuid(), os.getegid())
        calls = _record_fchown(monkeypatch, refuse=False)
        target = tmp_path / "saneless.toml"

        replace_file_atomically(target, _NEW)

        assert calls == []
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_existing_file_keeps_its_own_owner_not_the_directorys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rewrite copies the original's owner; the directory's is not asked."""
        target = tmp_path / "saneless.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")
        original = target.stat()
        _fake_directory_owner(monkeypatch, tmp_path, os.geteuid() + 1, os.getegid() + 1)
        calls = _record_fchown(monkeypatch, refuse=False)

        replace_file_atomically(target, _NEW)

        assert calls == [(original.st_uid, original.st_gid)]


def _record_directory_fchown(
    monkeypatch: pytest.MonkeyPatch, *, refuse: bool
) -> list[tuple[int, int, int]]:
    """
    Record every ``fchown`` by inode, without performing it; optionally refuse.

    The inode says which directory each call was for.
    """
    calls: list[tuple[int, int, int]] = []

    def fchown(fd: int, uid: int, gid: int) -> None:
        """Note the directory and the requested owner; refuse it when asked to."""
        calls.append((os.fstat(fd).st_ino, uid, gid))
        if refuse:
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(os, "fchown", fchown)
    return calls


class TestNewDirectoryOwner:
    """
    A config directory this process creates belongs to its parent's owner.

    The rule a new config file follows for its directory, one level up: root
    running with an ordinary user's HOME must not leave a root-only directory
    in that user's home, where it would hide the file from the user and
    refuse the user's own later writes.
    """

    def test_each_new_directory_takes_its_parents_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing parent and the directory itself are each given away."""
        owner, group = os.geteuid(), os.getegid()
        monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
        calls = _record_directory_fchown(monkeypatch, refuse=False)
        directory = tmp_path / "config" / "saneless"

        make_config_directory(directory)

        assert calls == [
            (directory.parent.stat().st_ino, owner, group),
            (directory.stat().st_ino, owner, group),
        ]
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_a_directory_owned_like_the_writer_needs_no_chown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The usual case: the writer owns the home it is writing into."""
        calls = _record_directory_fchown(monkeypatch, refuse=False)
        directory = tmp_path / "config" / "saneless"

        make_config_directory(directory)

        assert calls == []
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_a_refused_owner_change_still_creates_the_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A writer that may not give a directory away keeps it, as the file rule does."""
        _fake_directory_owner(monkeypatch, tmp_path, os.geteuid() + 1, os.getegid())
        calls = _record_directory_fchown(monkeypatch, refuse=True)
        directory = tmp_path / "saneless"
        caplog.set_level(logging.DEBUG, logger="saneless.atomic_write")

        make_config_directory(directory)

        assert len(calls) == 1
        assert directory.is_dir()
        records = [r for r in caplog.records if r.name == "saneless.atomic_write"]
        assert [r.levelno for r in records] == [logging.DEBUG]
        assert str(directory) in records[0].getMessage()

    def test_an_existing_directory_is_left_as_it_is(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a directory this call created is given away."""
        monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
        calls = _record_directory_fchown(monkeypatch, refuse=False)
        directory = tmp_path / "saneless"
        directory.mkdir(mode=0o755)

        make_config_directory(directory)

        assert calls == []
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755


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
        target = tmp_path / "saneless.toml"
        target.write_bytes(_ORIGINAL.encode("utf-8"))

        with pytest.raises(ConfigError) as excinfo:
            replace_file_atomically(target, _NEW)

        message = str(excinfo.value)
        assert str(target.resolve()) in message
        assert "bind-mounted as a single file" in message
        assert "Mount its directory instead" in message
        assert _PUBLISHED_DOCS_URL in message
        assert "docs/how-to/" not in message
        assert target.read_bytes() == _ORIGINAL.encode("utf-8")
        _leftovers(tmp_path)

    def test_directory_bind_mount_layout_replaces_in_place(
        self, tmp_path: Path
    ) -> None:
        """
        The documented ``./config:/etc/saneless`` layout is rewritten normally.

        The temp file is created inside the mounted directory, beside
        ``saneless.toml``, and nothing else is left there afterwards (CFG-09).
        """
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        target = config_dir / "saneless.toml"
        target.write_text(_ORIGINAL, encoding="utf-8")

        result = replace_file_atomically(target, _NEW)

        assert result == target.resolve()
        assert target.read_bytes() == _NEW.encode("utf-8")
        assert sorted(entry.name for entry in config_dir.iterdir()) == ["saneless.toml"]


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
        host_file = tmp_path / "host-saneless.toml"
        host_file.write_text(_ORIGINAL, encoding="utf-8")
        container_dir = tmp_path / "etc-saneless"
        container_dir.mkdir()
        mounted = container_dir / "saneless.toml"
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
        host_file = tmp_path / "host-saneless.toml"
        host_file.write_text(_ORIGINAL, encoding="utf-8")
        container_dir = tmp_path / "etc-saneless"
        container_dir.mkdir()
        mounted = container_dir / "saneless.toml"
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
        host_file = host_dir / "saneless.toml"
        host_file.write_text(_ORIGINAL, encoding="utf-8")
        container_dir = tmp_path / "etc-saneless"
        container_dir.mkdir()

        completed = _run_in_mount_namespace(
            host_dir, container_dir, container_dir / "saneless.toml", read_only=True
        )

        assert completed.returncode == 3, completed.stderr
        assert "read-only mount" in completed.stdout
        assert "read-write" in completed.stdout
        assert host_file.read_text(encoding="utf-8") == _ORIGINAL

    def test_real_directory_bind_mount_replaces_the_host_file(
        self, tmp_path: Path
    ) -> None:
        """Through a real directory bind mount, the host's saneless.toml changes."""
        host_dir = tmp_path / "config"
        host_dir.mkdir()
        host_file = host_dir / "saneless.toml"
        host_file.write_text(_ORIGINAL, encoding="utf-8")
        container_dir = tmp_path / "etc-saneless"
        container_dir.mkdir()

        completed = _run_in_mount_namespace(
            host_dir, container_dir, container_dir / "saneless.toml"
        )

        assert completed.returncode == 0, completed.stderr
        assert "replaced" in completed.stdout
        assert host_file.read_text(encoding="utf-8") == "new = 1\n"
        assert sorted(entry.name for entry in host_dir.iterdir()) == ["saneless.toml"]


class TestRefusedModeChange:
    """
    The public refusal predicate other modules share.

    The consume-directory handoff sets its copy's mode and must not fail a
    delivery on a filesystem that has no Unix modes, so it asks the same
    question the config rewrite does.
    """

    @pytest.mark.parametrize(
        "code",
        [errno.EPERM, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP],
        ids=["EPERM", "EINVAL", "EOPNOTSUPP", "ENOTSUP"],
    )
    def test_refused_mode_change_is_true_for_a_refusal(self, code: int) -> None:
        """A permission or filesystem refusal is a refusal, not a failure."""
        assert refused_mode_change(OSError(code, os.strerror(code)))

    def test_refused_mode_change_is_false_for_an_io_error(self) -> None:
        """A real I/O failure is not mistaken for a refusal."""
        assert not refused_mode_change(OSError(errno.EIO, os.strerror(errno.EIO)))
