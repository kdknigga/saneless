"""
Durable, all-or-nothing replacement of a config file (M-10, CFG-08).

``replace_file_atomically`` is the one primitive saneless uses to rewrite a
file the operator owns. It writes the new text beside the real file, fsyncs
it, and renames it into place, so a crash, a full disk, or a killed process
leaves either the old file or the new one -- never a truncated config
(D-05). It follows symlinks to the real file (D-07).

The module knows nothing about TOML: callers produce the text, this module
only makes the write durable.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import tempfile
from pathlib import Path

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
    * The temp file is fsynced **before** the rename; renaming unsynced data
      can leave a zero-length file after a crash.
    * The rename is ``Path.replace``, which delegates to ``os.replace``.
      ``os.replace`` is not called directly only because ruff's PTH105
      forbids it and this project does not permit per-line suppressions.
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
        PermissionError: The existing file is not writable by this process.
        OSError: Any other filesystem failure, re-raised after the temp file
            is removed.

    """
    # D-07: write through a symlink to the real file. The config path and its
    # directory are operator-controlled, and ``write_text`` followed links
    # before this helper existed, so following one here is no regression.
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
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(target)
        replaced = True
    finally:
        # A flag in ``finally`` rather than a blind ``except``: the temp file
        # is removed on any exception, KeyboardInterrupt included, and the
        # exception still propagates untouched.
        if not replaced:
            tmp.unlink(missing_ok=True)

    _fsync_directory(target.parent)
    return target
