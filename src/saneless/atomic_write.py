"""
Durable, all-or-nothing replacement of a config file.

``replace_file_atomically`` is the one primitive saneless uses to rewrite a
file the operator owns. It writes the new text beside the real file, fsyncs
it, and renames it into place, so a crash, a full disk, or a killed process
leaves either the old file or the new one -- never a truncated config.
It keeps the original's mode and owner (warning when the owner cannot be
kept), and its extended attributes and POSIX ACL -- refusing the rewrite when
one of those cannot be kept -- follows symlinks to the real file, but never to
a file that does not exist yet, and reports a single-file bind mount -- which
cannot be renamed over -- as a ``ConfigError`` naming the fix. A file it
creates takes its directory's owner when permitted and stays 0600. Temp files
left by a killed rewrite are swept on the next one.

The module knows nothing about TOML: callers produce the text, this module
only makes the write durable.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Final

from .exceptions import ConfigError

__all__ = ["refused_mode_change", "replace_file_atomically"]

logger = logging.getLogger(__name__)

# Where the mount errors send the operator: the published page, because an
# operator reading the error has the site, not the repository.
_DEPLOY_DOCS_URL: Final = (
    "https://kdknigga.github.io/saneless/how-to/deploy-docker-compose/"
    "#moving-from-a-single-file-config-mount"
)


def _fsync_directory(directory: Path) -> None:
    """
    Flush ``directory``'s entry table so a completed rename survives a crash.

    Best effort: some FUSE, network, and Docker Desktop shared
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
    Word the refusal for a config mounted as a single file.

    Args:
        target: The real file that cannot be replaced.

    Returns:
        The message naming the file and the fix.

    """
    return (
        f"Cannot replace {target}: it is bind-mounted as a single file. Mount "
        f"its directory instead (see {_DEPLOY_DOCS_URL})."
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
    Explain a read-only mount under an existing config file.

    ``os.access`` answers False on a read-only filesystem even for root, so
    without this check a legacy ``:ro`` mount was reported as "Permission
    denied" and the operator went looking at file permissions. The read-only
    flag belongs to a mount, so a read-only file whose directory is writable
    is itself a mount point: a single-file bind mount, which cannot be
    renamed over either.

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
        f"directory read-write instead (see {_DEPLOY_DOCS_URL})."
    )


# The errnos with which chown(2)/chmod(2) refuse rather than fail: not
# permitted (EPERM, a non-root writer), an id the user namespace does not map
# (EINVAL, the overflow uid of a rootless container), or a filesystem without
# Unix ownership or modes (EOPNOTSUPP/ENOTSUP, some FUSE, CIFS and vfat
# mounts). The file can still be replaced; anything else is a real failure.
_REFUSED: Final = frozenset(
    {errno.EPERM, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}
)


def refused_mode_change(exc: OSError) -> bool:
    """
    Report whether an ownership or mode change was refused, not broken.

    Serves every ``chown`` or ``chmod`` whose refusal must not fail an
    otherwise-good write: the config rewrite here, and the consume-directory
    copy, whose mode a filesystem without Unix modes cannot take.

    Args:
        exc: The error ``fchown``, ``fchmod`` or ``chmod`` raised.

    Returns:
        True when the errno is one of ``_REFUSED``.

    """
    return exc.errno in _REFUSED


# Extended attributes in this namespace are LSM labels (SELinux, Smack) and
# measurements. A label is copied only when the new file's differs, because
# the directory's policy usually gives it the original's already and setting
# one needs a relabel permission a confined container lacks.
_LABEL_PREFIX: Final = "security."

# Measurements of the file's bytes and metadata (IMA's hash, EVM's HMAC). The
# old ones describe the old contents, so copying one would stamp the new file
# with a measurement it fails; the kernel computes the new file's own.
_MEASUREMENTS: Final = frozenset({"security.ima", "security.evm"})

# The errnos with which the kernel refuses a label rather than fails to set
# it: not permitted to relabel (EACCES from the LSM, EPERM without the
# capability Smack wants), a label this policy does not know (EINVAL), or a
# filesystem that stores none (ENOTSUP/EOPNOTSUPP).
_LABEL_REFUSED: Final = frozenset(
    {errno.EACCES, errno.EPERM, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}
)

# ``listxattr`` fails with these when the filesystem has no extended
# attributes at all, so there is nothing to copy.
_NO_XATTR_SUPPORT: Final = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP})


def _copy_xattrs(fd: int, target: Path) -> None:
    """
    Copy ``target``'s extended attributes, POSIX ACLs included, onto ``fd``.

    On Linux a POSIX ACL is the ``system.posix_acl_access`` attribute, so
    copying attributes carries it. Unlike the owner and mode, an attribute
    that cannot be copied is not skipped: dropping an ACL can widen who reads
    the file, so the rewrite is refused instead.

    ``security.*`` labels are the exception, handled by ``_keep_label``: kept
    when the kernel allows it, warned about when it refuses, because a
    confined writer is refused a label that differs only in a way that grants
    nothing. The IMA and EVM measurements are never copied.

    Args:
        fd: The open temp file, still owned by this process with mode 0600.
        target: The existing file being replaced.

    Raises:
        ConfigError: An attribute could not be read or set on the temp file.
        OSError: Listing the attributes failed for a reason other than the
            filesystem lacking extended attribute support.

    """
    try:
        names = os.listxattr(target)
    except OSError as exc:
        if exc.errno in _NO_XATTR_SUPPORT:
            return
        raise
    for name in names:
        if name in _MEASUREMENTS:
            continue
        try:
            value = os.getxattr(target, name)
        except OSError as exc:
            if exc.errno == errno.ENODATA:
                # Removed since it was listed: there is nothing to drop.
                continue
            raise _xattr_refusal(target, name, exc) from exc
        if name.startswith(_LABEL_PREFIX):
            _keep_label(fd, target, name, value)
            continue
        try:
            os.setxattr(fd, name, value)
        except OSError as exc:
            raise _xattr_refusal(target, name, exc) from exc


def _keep_label(fd: int, target: Path, name: str, value: bytes) -> None:
    """
    Give the temp file ``target``'s ``security.*`` label, when it differs.

    A label the new file already carries is left alone. A differing one --
    an SELinux type an admin set with ``chcon``, a Smack label -- is copied,
    because falling back to the directory's default can change who may read
    the file. When the kernel refuses the copy the rewrite still goes ahead,
    with a WARNING naming the file and the attribute but never the label: a
    container rewriting a config its host user created is refused that
    user's label although the two differ only in the SELinux user, which
    grants no access, and refusing the rewrite would break that common case.

    Args:
        fd: The open temp file.
        target: The file being replaced.
        name: The ``security.*`` attribute.
        value: Its value on ``target``.

    Raises:
        ConfigError: Setting the label failed for a reason other than a
            refusal.

    """
    try:
        current: bytes | None = os.getxattr(fd, name)
    except OSError:
        current = None
    if current == value:
        return
    try:
        os.setxattr(fd, name, value)
    except OSError as exc:
        if exc.errno not in _LABEL_REFUSED:
            raise _xattr_refusal(target, name, exc) from exc
        logger.warning(
            "Not keeping the security label %r of %s (%s); the rewritten file "
            "carries the label its directory gives a new file, which may "
            "change who can read it",
            name,
            target,
            exc.strerror,
        )


def _xattr_refusal(target: Path, name: str, exc: OSError) -> ConfigError:
    """
    Word the refusal for an extended attribute that cannot be kept.

    Args:
        target: The file being replaced.
        name: The attribute that could not be copied.
        exc: The error reading or setting it; its strerror names no content.

    Returns:
        The error naming the file, the attribute and the fix.

    """
    return ConfigError(
        f"Cannot rewrite {target} without dropping its access control list or "
        f"extended attribute {name!r} ({exc.strerror}); rewrite it as its owner "
        "or remove the attribute, then run again"
    )


def _copy_owner_and_mode(fd: int, original: os.stat_result) -> None:
    """
    Give the temp file the original's owner, group and permission bits.

    A host-owned config must not become root-owned after a container rewrite,
    or the operator needs sudo to edit it. Each change is made only when the
    process is permitted and the filesystem supports it; a refusal is skipped
    so it never fails a write that would otherwise succeed, but it is logged
    at WARNING with the ids that could not be kept, because the file then
    changes hands. When the owner cannot be set, the group alone
    is still tried: a service user rewriting a ``root:saneless`` 0664 config
    may keep the group it belongs to. chown comes BEFORE chmod because
    chown(2) may clear the set-id bits the mode copy would otherwise
    restore.

    Args:
        fd: The open temp file.
        original: The replaced file's status.

    Raises:
        OSError: A change failed for a reason other than a refusal.

    """
    try:
        os.fchown(fd, original.st_uid, original.st_gid)
    except OSError as exc:
        if not refused_mode_change(exc):
            raise
        logger.warning(
            "Not keeping the config file's owner (uid %d, gid %d); it is now "
            "owned by this process: %s",
            original.st_uid,
            original.st_gid,
            exc.strerror,
        )
        try:
            os.fchown(fd, -1, original.st_gid)
        except OSError as group_exc:
            if not refused_mode_change(group_exc):
                raise
            logger.warning(
                "Not keeping the config file's group (gid %d) either: %s",
                original.st_gid,
                group_exc.strerror,
            )
    try:
        os.fchmod(fd, stat.S_IMODE(original.st_mode))
    except OSError as exc:
        # The temp file then keeps mkstemp's 0600: narrower, never wider.
        if not refused_mode_change(exc):
            raise
        logger.debug("Not copying the config file's mode: %s", exc.strerror)


# A leftover temp file is swept only once it is this old. That is longer than
# any rewrite takes, so a concurrent writer's in-flight temp is never removed:
# the worker's start-up profile generation and a CLI run can overlap.
_STALE_TEMP_AGE_SECONDS: Final = 600


def _sweep_stale_temps(target: Path) -> None:
    """
    Remove temp files a killed rewrite of ``target`` left beside it.

    A SIGKILL or power loss between ``mkstemp`` and the rename skips the
    ``finally`` that removes the temp, leaving a stray copy of the config --
    possibly holding the Paperless token -- that nothing else cleans up.

    Only this helper's own leftovers are removed: names that match exactly
    what ``mkstemp`` produces for ``target`` (the ``.{name}.`` prefix, eight
    characters from its alphabet, the ``.tmp`` suffix), that ``lstat`` shows
    as a regular file -- never a symlink, which is neither removed nor
    followed -- owned by this process's user, and older than
    ``_STALE_TEMP_AGE_SECONDS``. Best effort: a failure to list or remove is
    logged at DEBUG and never fails the rewrite.

    Args:
        target: The real file about to be replaced.

    """
    pattern = re.compile(rf"^\.{re.escape(target.name)}\.[a-z0-9_]{{8}}\.tmp$")
    directory = target.parent
    try:
        candidates = [
            entry for entry in directory.iterdir() if pattern.match(entry.name)
        ]
    except OSError as exc:
        logger.debug("Not sweeping stale temp files in %s: %s", directory, exc.strerror)
        return
    cutoff = time.time() - _STALE_TEMP_AGE_SECONDS
    euid = os.geteuid()
    for candidate in candidates:
        try:
            status = candidate.lstat()
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != euid
                or status.st_mtime >= cutoff
            ):
                continue
            candidate.unlink()
        except OSError as exc:
            logger.debug("Not removing stale temp file %s: %s", candidate, exc.strerror)
            continue
        logger.info(
            "Removed a stale temp file left by an interrupted rewrite: %s", candidate
        )


def _adopt_directory_owner(fd: int, directory: Path) -> None:
    """
    Give a newly created file its directory's owner and group, when permitted.

    A root ``docker compose exec`` writing a new config into the service's
    config directory must leave a file the service can read. The mode is
    never changed: the file keeps mkstemp's 0600, whoever owns it, because a
    new config may hold the Paperless token. A refused change keeps this
    process as the owner and is logged at DEBUG; the write goes ahead.

    Args:
        fd: The open temp file.
        directory: The directory the file is being created in.

    Raises:
        OSError: The ownership change failed for a reason other than a
            refusal.

    """
    status = directory.stat()
    if (status.st_uid, status.st_gid) == (os.geteuid(), os.getegid()):
        return
    try:
        os.fchown(fd, status.st_uid, status.st_gid)
    except OSError as exc:
        if not refused_mode_change(exc):
            raise
        logger.debug(
            "Not giving the new config file its directory's owner (uid %d, gid %d): %s",
            status.st_uid,
            status.st_gid,
            exc.strerror,
        )


def replace_file_atomically(path: Path, text: str) -> Path:
    """
    Replace ``path``'s contents with ``text`` durably, writing through symlinks.

    Load-bearing details:

    * ``path`` is resolved first and the **real** file is replaced, so a
      dotfiles-style symlink keeps pointing at it. A symlink to nothing is
      refused rather than followed: it would create a file wherever the link
      points.
    * The temp file comes from ``tempfile.mkstemp`` in the real file's **own
      directory**: ``rename(2)`` is atomic only within one filesystem, and a
      mounted config directory is a separate one. mkstemp's random name and
      ``O_EXCL`` also mean no one can plant a symlink at the temp name.
    * The text is encoded as UTF-8 and written as bytes, so the result does
      not depend on the locale and CRLF line endings are not translated.
    * An existing file's owner, group and permission bits are copied onto
      the temp file before any content is written, each when this process is
      permitted to set it and the filesystem supports it, so the rewrite
      neither changes who can edit the file nor widens who can read it. A
      refused change is skipped, never a failed write; a lost owner or group
      is logged at WARNING, because the file then changes hands.
    * A **new** file takes its directory's owner and group when this process
      may set them, so a config root creates in the service's config
      directory (a ``docker compose exec``) stays readable by the service. A
      refused change keeps this process as the owner. Either way the file
      keeps mkstemp's 0600, because a new config may hold the Paperless
      token.
    * An existing file's extended attributes -- its POSIX ACL included -- are
      copied onto the temp file **first**, before the owner and mode.
      Setting a ``user.*`` attribute needs write permission the writer may
      lose once the original's owner and mode are applied; and ``fchmod`` on
      a file with an ACL sets the ACL mask from the group bits, which on the
      original already are its mask, so the owning group's own entry is kept
      and is never handed the mask. Unlike a refused owner or mode, an
      attribute that cannot be copied refuses the rewrite, because dropping
      an ACL can widen who reads the file. A ``security.*`` LSM label is
      copied when the new file's differs; a label the kernel refuses to set
      is logged at WARNING and the rewrite goes ahead. IMA and EVM
      measurements are never copied.
    * Before the temp file is made, leftovers of a killed earlier rewrite --
      the exact mkstemp name for this file, a regular file this user owns,
      older than ten minutes -- are removed; a younger one may belong to a
      concurrent writer and is left alone.
    * The temp file is fsynced **before** the rename; renaming unsynced data
      can leave a zero-length file after a crash.
    * A rename refused with EBUSY means the file is a single-file bind mount;
      that is reported as a ``ConfigError`` naming the fix and citing the
      published deployment guide, with no
      non-atomic fallback. A read-only mount -- the legacy ``:ro``
      single-file mount, or a read-only directory mount -- is reported the
      same way before anything is written, rather than as EACCES.
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
            read-write; or an extended attribute or ACL of the existing file
            could not be copied, and the file is left as it was; or ``path``
            is a symlink to a file that does not exist.
        PermissionError: The existing file, on a writable mount, is not
            writable by this process.
        OSError: Any other filesystem failure, re-raised after the temp file
            is removed.

    """
    # Write through a symlink to the real file. The config path and its
    # directory are operator-controlled, and the plain in-place write this
    # helper replaces followed links too, so following one here is no
    # regression.
    target = path.resolve()
    try:
        original: os.stat_result | None = target.stat()
    except FileNotFoundError:
        original = None
    if original is None and path.is_symlink():
        # That argument covers a file the operator already has. A link to
        # nothing names a file no load ever read, and whoever can write the
        # link's directory chose where it points: following it would create
        # that file -- as root, anywhere -- and hand it to the far
        # directory's owner.
        msg = (
            f"Cannot create {path}: it is a symlink to {target}, which does not "
            "exist; remove the link or create the file it points to first"
        )
        raise ConfigError(msg)
    if original is not None and (mount_error := _read_only_mount_error(target)):
        raise mount_error
    if original is not None and not os.access(target, os.W_OK):
        # rename(2) needs only a writable directory, so without
        # this check a chmod 0444 config would be silently replaced. Refusing
        # keeps today's meaning of a read-only file ("cannot be written").
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(target))

    _sweep_stale_temps(target)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    replaced = False
    try:
        with os.fdopen(fd, "wb") as handle:
            if original is not None:
                _copy_xattrs(handle.fileno(), target)
                _copy_owner_and_mode(handle.fileno(), original)
            else:
                # A new file takes its directory's owner, so one created by
                # root in the service's config directory stays readable by
                # the service; it keeps mkstemp's 0600 because it may hold
                # the Paperless token.
                _adopt_directory_owner(handle.fileno(), target.parent)
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            tmp.replace(target)
        except OSError as exc:
            # The kernel refuses to rename over a bind-mount point, which
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
