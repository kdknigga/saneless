"""
Private directories: created owner-only, and checked before they are trusted.

Scratch space defaults to a predictable name in a shared, world-writable temp
directory, so another local user can create it first -- as a symlink, or as a
directory they own or can write to. These tests pin that a missing directory
is created 0700 whatever the umask, and that an existing one is refused unless
it is a real directory this user owns with no group or world write.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

import pytest

import saneless.private_dirs as private_dirs_module
from saneless.exceptions import ConfigError
from saneless.private_dirs import (
    check_private_dir,
    ensure_private_dir,
    make_private_dir,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_KEY = "output.tmp_dir"
_PRIVATE = 0o700
_OPEN_READ = 0o755


def _mode(path: Path) -> int:
    """Return the permission bits of ``path``, not following a symlink."""
    return stat.S_IMODE(path.lstat().st_mode)


@pytest.fixture
def umask_002() -> Iterator[None]:
    """
    Run the test under umask 002, as a user-private-group login does.

    Under that umask a plain ``mkdir`` gives 0775, group-writable, which is
    exactly what a private directory must not end up as.
    """
    old = os.umask(0o002)
    try:
        yield
    finally:
        os.umask(old)


def _symlink_to_directory(target: Path) -> None:
    """Point ``target`` at a real directory beside it."""
    real = target.parent / "elsewhere"
    real.mkdir(mode=_PRIVATE)
    target.symlink_to(real)


def _dangling_symlink(target: Path) -> None:
    """Point ``target`` at a path that does not exist."""
    target.symlink_to(target.parent / "missing")


def _regular_file(target: Path) -> None:
    """Put a regular file where the directory should be."""
    target.write_text("")


def _group_writable(target: Path) -> None:
    """Create a directory its group may write to."""
    target.mkdir()
    target.chmod(0o770)


def _world_writable(target: Path) -> None:
    """Create a directory everyone may write to."""
    target.mkdir()
    target.chmod(0o777)


_REFUSED_SHAPES: dict[str, Callable[[Path], None]] = {
    "symlink to a directory": _symlink_to_directory,
    "dangling symlink": _dangling_symlink,
    "regular file": _regular_file,
    "group-writable directory": _group_writable,
    "world-writable directory": _world_writable,
}


def _assert_refusal(error: ConfigError, path: Path) -> None:
    """Check that a refusal names the setting, the path and the chmod fix."""
    message = str(error)
    assert _KEY in message
    assert str(path) in message
    assert "chmod 700" in message


class TestMakePrivateDir:
    """``make_private_dir`` creates the leaf owner-only and leaves others alone."""

    @pytest.mark.usefixtures("umask_002")
    def test_make_private_dir_creates_the_leaf_0700_under_umask_002(
        self, tmp_path: Path
    ) -> None:
        """A new directory is 0700 even though the umask would allow 0775."""
        target = tmp_path / "data"
        make_private_dir(target)
        assert target.is_dir()
        assert _mode(target) == _PRIVATE

    @pytest.mark.usefixtures("umask_002")
    def test_make_private_dir_only_the_leaf_is_private(self, tmp_path: Path) -> None:
        """
        Missing parents get the default permissions, not 0700.

        This is Python's documented behaviour for ``parents=True``, and the
        reason each private level needs its own call.
        """
        target = tmp_path / "parent" / "data"
        make_private_dir(target)
        assert _mode(target) == _PRIVATE
        assert _mode(target.parent) == 0o775

    def test_make_private_dir_keeps_an_existing_directory_mode(
        self, tmp_path: Path
    ) -> None:
        """An existing directory is not re-moded."""
        target = tmp_path / "data"
        target.mkdir()
        target.chmod(_OPEN_READ)
        make_private_dir(target)
        assert _mode(target) == _OPEN_READ


class TestEnsurePrivateDir:
    """``ensure_private_dir`` creates privately, then refuses anything unsafe."""

    @pytest.mark.usefixtures("umask_002")
    def test_ensure_private_dir_creates_a_missing_tmp_dir_0700(
        self, tmp_path: Path
    ) -> None:
        """A missing directory is created 0700 under umask 002."""
        target = tmp_path / f"saneless-{os.getuid()}"
        ensure_private_dir(target, key=_KEY)
        assert target.is_dir()
        assert _mode(target) == _PRIVATE

    def test_ensure_private_dir_accepts_an_owned_0755_directory(
        self, tmp_path: Path
    ) -> None:
        """Group and world *read* are fine: every workspace inside is 0700."""
        target = tmp_path / "scratch"
        target.mkdir()
        target.chmod(_OPEN_READ)
        ensure_private_dir(target, key=_KEY)
        assert _mode(target) == _OPEN_READ

    def test_ensure_private_dir_accepts_an_owned_0700_directory(
        self, tmp_path: Path
    ) -> None:
        """The directory a previous start created passes on the next one."""
        target = tmp_path / "scratch"
        target.mkdir(mode=_PRIVATE)
        ensure_private_dir(target, key=_KEY)
        assert _mode(target) == _PRIVATE

    @pytest.mark.parametrize("shape", sorted(_REFUSED_SHAPES))
    def test_ensure_private_dir_refuses_an_unsafe_tmp_dir(
        self, tmp_path: Path, shape: str
    ) -> None:
        """A symlink, a file or a group/world-writable directory is refused."""
        target = tmp_path / "scratch"
        _REFUSED_SHAPES[shape](target)
        before = target.lstat()
        with pytest.raises(ConfigError) as exc_info:
            ensure_private_dir(target, key=_KEY)
        _assert_refusal(exc_info.value, target)
        # Refused as found: nothing is re-moded or replaced.
        after = target.lstat()
        assert (after.st_mode, after.st_ino) == (before.st_mode, before.st_ino)

    def test_ensure_private_dir_refuses_a_symlink_without_touching_its_target(
        self, tmp_path: Path
    ) -> None:
        """The directory a squatter's link points at is neither used nor changed."""
        real = tmp_path / "victim"
        real.mkdir()
        real.chmod(0o777)
        target = tmp_path / "scratch"
        target.symlink_to(real)
        with pytest.raises(ConfigError, match="symbolic link"):
            ensure_private_dir(target, key=_KEY)
        assert _mode(real) == 0o777
        assert list(real.iterdir()) == []

    def test_ensure_private_dir_refuses_a_tmp_dir_owned_by_another_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A directory owned by another UID is refused, even at mode 0700."""
        target = tmp_path / "scratch"
        target.mkdir(mode=_PRIVATE)
        owner = target.stat().st_uid
        monkeypatch.setattr(private_dirs_module.os, "geteuid", lambda: owner + 1)
        with pytest.raises(ConfigError, match="owned by") as exc_info:
            ensure_private_dir(target, key=_KEY)
        _assert_refusal(exc_info.value, target)

    def test_ensure_private_dir_creation_failure_is_a_config_error(
        self, tmp_path: Path
    ) -> None:
        """A directory that cannot be created is a setup error naming the key."""
        blocker = tmp_path / "blocker"
        blocker.write_text("")
        target = blocker / "scratch"
        with pytest.raises(ConfigError) as exc_info:
            ensure_private_dir(target, key=_KEY)
        message = str(exc_info.value)
        assert _KEY in message
        assert str(target) in message
        assert isinstance(exc_info.value.__cause__, OSError)


class TestCheckPrivateDir:
    """``check_private_dir`` only inspects; it never creates."""

    def test_check_private_dir_does_not_create_a_missing_tmp_dir(
        self, tmp_path: Path
    ) -> None:
        """A missing directory is an error, not something to create."""
        target = tmp_path / "scratch"
        with pytest.raises(ConfigError) as exc_info:
            check_private_dir(target, key=_KEY)
        assert _KEY in str(exc_info.value)
        assert not os.path.lexists(target)

    @pytest.mark.parametrize("shape", sorted(_REFUSED_SHAPES))
    def test_check_private_dir_refuses_an_unsafe_tmp_dir(
        self, tmp_path: Path, shape: str
    ) -> None:
        """The check alone refuses every unsafe shape."""
        target = tmp_path / "scratch"
        _REFUSED_SHAPES[shape](target)
        with pytest.raises(ConfigError) as exc_info:
            check_private_dir(target, key=_KEY)
        _assert_refusal(exc_info.value, target)
