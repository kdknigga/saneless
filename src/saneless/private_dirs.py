"""
Directories only this user can use: created 0700, and checked before use.

Scratch space defaults to a predictable name in the shared temp directory,
where any local user can create that name first -- as a symlink pointing
somewhere else, or as a directory they own or can write to. Scans written
there could then be read or replaced. A missing directory is therefore
created owner-only, and an existing one is trusted only after ``lstat`` shows
a real directory this process owns that nobody else can write to.

Only the directory itself is checked, not its parent. That is enough when
the parent is not writable by other users, or is sticky as ``/tmp`` is: then
nobody else can rename the checked directory away and put a symlink in its
place. A directory inside a parent that others can write to and that is not
sticky can be swapped that way after the check, so ``tmp_dir`` must not be
placed in one.

The default scratch directory is named with ``os.getuid()``, the real user,
while ownership is compared with ``os.geteuid()``, the identity that creates
files. The two are equal in every supported deployment.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

from saneless.exceptions import ConfigError, describe

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["check_private_dir", "ensure_private_dir", "make_private_dir"]

_PRIVATE_MODE = 0o700
_OTHERS_WRITE = stat.S_IWGRP | stat.S_IWOTH


def make_private_dir(path: Path) -> None:
    """
    Create ``path`` with mode 0700 unless it already exists.

    Only the leaf gets 0700: Python creates any missing parents with the
    default permissions, without taking the mode into account. A caller that
    needs several private levels creates each with its own call. The mode is
    only ever narrowed by the umask, so 0700 holds under 022, 002 and 077.

    An existing directory keeps the mode it has; nothing is re-moded.

    Args:
        path: The directory to create.

    """
    path.mkdir(mode=_PRIVATE_MODE, parents=True, exist_ok=True)


def _refusal(path: Path, key: str, problem: str) -> ConfigError:
    """
    Build the error for an existing directory that is not safe to use.

    Args:
        path: The configured directory.
        key: The setting that names it, e.g. ``output.tmp_dir``.
        problem: What is wrong with it, as a phrase following the path.

    Returns:
        The error, naming the setting, the path, the problem and the fix.

    """
    msg = (
        f"{key} {path} {problem}, so another local user could read or replace "
        f"the scans kept there. Use a directory you own that nobody else can "
        f"write to: run `chmod 700 {path}` if it is yours, remove it so saneless "
        f"creates it privately, or set {key} to another directory."
    )
    return ConfigError(msg)


def check_private_dir(path: Path, *, key: str) -> None:
    """
    Refuse ``path`` unless it is a real directory owned by this process's user.

    The check uses ``lstat``, so a symlink is refused rather than followed.
    Also refused: anything that is not a directory, a directory owned by
    another user, and one with group- or world-write. Group- or world-*read*
    is accepted, because every workspace created inside is itself 0700.
    Nothing is created or changed.

    The parent is not checked; the module docstring says why a parent that
    others can write to, and that is not sticky, must not hold ``path``.

    Args:
        path: The directory to check.
        key: The setting that names it, for the message.

    Raises:
        ConfigError: If the path cannot be inspected or is not safe to use.

    """
    try:
        info = os.lstat(path)
    except OSError as exc:
        msg = f"{key} {path} could not be checked: {describe(exc)}"
        raise ConfigError(msg) from exc
    if stat.S_ISLNK(info.st_mode):
        raise _refusal(path, key, "is a symbolic link")
    if not stat.S_ISDIR(info.st_mode):
        raise _refusal(path, key, "is not a directory")
    owner = os.geteuid()
    if info.st_uid != owner:
        raise _refusal(
            path, key, f"is owned by uid {info.st_uid}, not by this user (uid {owner})"
        )
    if info.st_mode & _OTHERS_WRITE:
        mode = stat.S_IMODE(info.st_mode)
        raise _refusal(
            path, key, f"can be written by its group or by everyone (mode {mode:o})"
        )


def ensure_private_dir(path: Path, *, key: str) -> None:
    """
    Create ``path`` 0700 if it is missing, then check it is safe to use.

    ``mkdir`` succeeds silently on a symlink to a directory and raises
    ``FileExistsError`` on a dangling symlink or a file; either way the
    ``lstat`` check that follows refuses what is really there.

    Args:
        path: The directory to create or verify.
        key: The setting that names it, for the message.

    Raises:
        ConfigError: If the directory cannot be created, or exists and is not
            safe to use; the message names ``key`` and the path.

    """
    try:
        make_private_dir(path)
    except FileExistsError:
        # Something that is not a directory holds the name; the check below
        # says what it is.
        pass
    except OSError as exc:
        msg = (
            f"{key} {path} could not be created: {describe(exc)}. Create it "
            f"yourself with `mkdir -m 700 {path}`, or set {key} to another "
            f"directory."
        )
        raise ConfigError(msg) from exc
    check_private_dir(path, key=key)
