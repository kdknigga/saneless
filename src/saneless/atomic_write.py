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
    * An existing file's owner (when this process may chown) and permission
      bits are copied onto the temp file before any content is written, so
      the rewrite neither changes who can edit the file nor widens who can
      read it (D-06). A new file keeps mkstemp's 0600.
    * The temp file is fsynced **before** the rename; renaming unsynced data
      can leave a zero-length file after a crash.
    * A rename refused with EBUSY means the file is a single-file bind mount;
      that is reported as a ``ConfigError`` naming the fix, with no
      non-atomic fallback (D-08).
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
        ConfigError: The file is bind-mounted as a single file (EBUSY), so it
            cannot be replaced; the message tells the operator to mount its
            directory instead.
        PermissionError: The existing file is not writable by this process.
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
                # D-06: a host-owned config must not become root-owned after a
                # container rewrite, or the operator needs sudo to edit it.
                # When chown is not permitted (non-root on bare metal, where
                # the writer already owns the file) it is skipped silently.
                # chown comes BEFORE chmod because chown(2) may clear the
                # set-id bits the mode copy would otherwise restore
                # (Pitfall 5).
                with contextlib.suppress(PermissionError):
                    os.fchown(handle.fileno(), original.st_uid, original.st_gid)
                os.fchmod(handle.fileno(), stat.S_IMODE(original.st_mode))
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
                msg = (
                    f"Cannot replace {target}: it is bind-mounted as a single "
                    "file. Mount its directory instead (see "
                    "docs/how-to/deploy-docker-compose.md)."
                )
                raise ConfigError(msg) from exc
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
