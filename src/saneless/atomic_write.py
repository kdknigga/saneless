"""
Durable, all-or-nothing replacement of a config file (M-10, CFG-08).

``replace_file_atomically`` is the one primitive saneless uses to rewrite a
file the operator owns. It writes the new text beside the real file, fsyncs
it, and renames it into place, so a crash, a full disk, or a killed process
leaves either the old file or the new one -- never a truncated config
(D-05). It keeps the original's mode and owner (D-06), follows symlinks to
the real file (D-07), and reports a single-file bind mount -- which cannot be
renamed over -- as a ``ConfigError`` naming the fix (D-08).

The module knows nothing about TOML: callers produce the text, this module
only makes the write durable.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Final

from .exceptions import ConfigError

__all__ = ["replace_file_atomically"]

logger = logging.getLogger(__name__)


def _fsync_directory(directory: Path) -> None:
    """
    Flush ``directory``'s entry table so a completed rename survives a crash.

    Best effort (Pitfall 7): some FUSE, network, and Docker Desktop shared
    folder mounts reject ``fsync`` on a directory descriptor. By the time this
    runs the rename has already happened and the new data is in place, so a
    refusal here must not turn a successful write into a reported failure.

    Args:
        directory: The directory that holds the file just renamed.

    """
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _single_file_mount_message(target: Path) -> str:
    """
    Word D-08's refusal for a config mounted as a single file.

    Args:
        target: The real file that cannot be replaced.

    Returns:
        The message naming the file and the fix.

    """
    return (
        f"Cannot replace {target}: it is bind-mounted as a single file. Mount "
        "its directory instead (see docs/how-to/deploy-docker-compose.md)."
    )


def _is_read_only_mount(path: Path) -> bool:
    """
    Report whether ``path`` lives on a filesystem mounted read-only.

    A failed ``statvfs`` answers False, so the ordinary checks that follow
    decide what to report.

    Args:
        path: An existing file or directory.

    Returns:
        True when the mount holding ``path`` carries ``ST_RDONLY``.

    """
    try:
        flags = os.statvfs(path).f_flag
    except OSError:
        return False
    return bool(flags & os.ST_RDONLY)


def _read_only_mount_error(target: Path) -> ConfigError | None:
    """
    Explain a read-only mount under an existing config file (WR-01).

    ``os.access`` answers False on a read-only filesystem even for root, so
    without this check a legacy ``:ro`` mount was reported as "Permission
    denied" and the operator went looking at file permissions. The read-only
    flag belongs to a mount, so a read-only file whose directory is writable
    is itself a mount point: the single-file bind mount D-08 describes.

    Args:
        target: The real, existing file about to be replaced.

    Returns:
        The error naming the fix, or None when the file's mount is writable.

    """
    if not _is_read_only_mount(target):
        return None
    if not _is_read_only_mount(target.parent):
        return ConfigError(_single_file_mount_message(target))
    return ConfigError(
        f"Cannot replace {target}: it is on a read-only mount. Mount its "
        "directory read-write instead (see docs/how-to/deploy-docker-compose.md)."
    )


# The errnos with which chown(2)/chmod(2) refuse rather than fail: not
# permitted (EPERM, a non-root writer), an id the user namespace does not map
# (EINVAL, the overflow uid of a rootless container), or a filesystem without
# Unix ownership or modes (EOPNOTSUPP/ENOTSUP, some FUSE, CIFS and vfat
# mounts). The file can still be replaced; anything else is a real failure.
_REFUSED: Final = frozenset(
    {errno.EPERM, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}
)


def _refused(exc: OSError) -> bool:
    """
    Report whether an ownership or mode change was refused, not broken.

    Args:
        exc: The error ``fchown`` or ``fchmod`` raised.

    Returns:
        True when the errno is one of ``_REFUSED``.

    """
    return exc.errno in _REFUSED


def _copy_owner_and_mode(fd: int, original: os.stat_result) -> None:
    """
    Give the temp file the original's owner, group and permission bits (D-06).

    A host-owned config must not become root-owned after a container rewrite,
    or the operator needs sudo to edit it. Each change is made only when the
    process is permitted and the filesystem supports it; a refusal is skipped
    silently (with a DEBUG line) so it never fails a write that would
    otherwise succeed (WR-02). When the owner cannot be set, the group alone
    is still tried: a service user rewriting a ``root:saneless`` 0664 config
    may keep the group it belongs to. chown comes BEFORE chmod because
    chown(2) may clear the set-id bits the mode copy would otherwise restore
    (Pitfall 5).

    Args:
        fd: The open temp file.
        original: The replaced file's status.

    Raises:
        OSError: A change failed for a reason other than a refusal.

    """
    try:
        os.fchown(fd, original.st_uid, original.st_gid)
    except OSError as exc:
        if not _refused(exc):
            raise
        logger.debug("Not copying the config file's owner: %s", exc.strerror)
        try:
            os.fchown(fd, -1, original.st_gid)
        except OSError as group_exc:
            if not _refused(group_exc):
                raise
            logger.debug("Not copying the config file's group: %s", group_exc.strerror)
    try:
        os.fchmod(fd, stat.S_IMODE(original.st_mode))
    except OSError as exc:
        # The temp file then keeps mkstemp's 0600: narrower, never wider.
        if not _refused(exc):
            raise
        logger.debug("Not copying the config file's mode: %s", exc.strerror)


def replace_file_atomically(path: Path, text: str) -> Path:
    """
    Replace ``path``'s contents with ``text`` durably, writing through symlinks.

    Load-bearing details:

    * ``path`` is resolved first and the **real** file is replaced, so a
      dotfiles-style symlink keeps pointing at it (D-07).
    * The temp file comes from ``tempfile.mkstemp`` in the real file's **own
      directory**: ``rename(2)`` is atomic only within one filesystem, and a
      mounted config directory is a separate one. mkstemp's random name and
      ``O_EXCL`` also mean no one can plant a symlink at the temp name.
    * The text is encoded as UTF-8 and written as bytes, so the result does
      not depend on the locale and CRLF line endings are not translated.
    * An existing file's owner, group and permission bits are copied onto
      the temp file before any content is written, each when this process is
      permitted to set it and the filesystem supports it, so the rewrite
      neither changes who can edit the file nor widens who can read it
      (D-06). A refused change is skipped, never a failed write (WR-02). A
      new file keeps mkstemp's 0600.
    * The temp file is fsynced **before** the rename; renaming unsynced data
      can leave a zero-length file after a crash.
    * A rename refused with EBUSY means the file is a single-file bind mount;
      that is reported as a ``ConfigError`` naming the fix, with no
      non-atomic fallback (D-08). A read-only mount -- the legacy ``:ro``
      single-file mount, or a read-only directory mount -- is reported the
      same way before anything is written, rather than as EACCES (WR-01).
    * The rename is ``Path.replace``, the atomic, unconditionally
      overwriting ``rename(2)``. The ``os`` module's function of the same
      name is not called directly only because ruff's PTH105 forbids it and
      this project does not permit per-line suppressions.
    * The temp file is removed on every failure path, including
      ``KeyboardInterrupt``.
    * The directory is then fsynced, best effort.

    Args:
        path: The file to replace; it need not exist yet.
        text: The complete new contents.

    Returns:
        The real file that was replaced, which differs from ``path`` when
        ``path`` is a symlink.

    Raises:
        ConfigError: The file is bind-mounted as a single file (EBUSY, or a
            read-only file mount), so it cannot be replaced, and the message
            tells the operator to mount its directory instead; or the file is
            on a read-only directory mount, and the message says to mount it
            read-write.
        PermissionError: The existing file, on a writable mount, is not
            writable by this process.
        OSError: Any other filesystem failure, re-raised after the temp file
            is removed.

    """
    # D-07: write through a symlink to the real file. The config path and its
    # directory are operator-controlled, and the plain in-place write this
    # helper replaces followed links too, so following one here is no
    # regression.
    target = path.resolve()
    try:
        original: os.stat_result | None = target.stat()
    except FileNotFoundError:
        original = None
    if original is not None and (mount_error := _read_only_mount_error(target)):
        raise mount_error
    if original is not None and not os.access(target, os.W_OK):
        # Pitfall 6: rename(2) needs only a writable directory, so without
        # this check a chmod 0444 config would be silently replaced. Refusing
        # keeps today's meaning of a read-only file ("cannot be written").
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(target))

    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    replaced = False
    try:
        with os.fdopen(fd, "wb") as handle:
            if original is not None:
                _copy_owner_and_mode(handle.fileno(), original)
            # A new file keeps mkstemp's 0600: it may hold the Paperless token.
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            tmp.replace(target)
        except OSError as exc:
            # D-08: the kernel refuses to rename over a bind-mount point, which
            # is what a config mounted as a single file is. There is no
            # non-atomic fallback by decision: it would bring back the
            # truncated-config risk this helper exists to remove.
            if exc.errno == errno.EBUSY:
                raise ConfigError(_single_file_mount_message(target)) from exc
            raise
        replaced = True
    finally:
        # A flag in ``finally`` rather than a blind ``except``: the temp file
        # is removed on any exception, KeyboardInterrupt included, and the
        # exception still propagates untouched.
        if not replaced:
            tmp.unlink(missing_ok=True)

    _fsync_directory(target.parent)
    return target
