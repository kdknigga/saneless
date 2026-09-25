"""Tests for logging configuration."""

from __future__ import annotations

import errno
import inspect
import logging
import logging.handlers
import os
import re
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from saneless.config import OutputConfig
from saneless.logging_config import configure_logging

if TYPE_CHECKING:
    from collections.abc import Iterator


# A fabricated token-shaped literal, not a credential. Its only job is to be
# distinctive enough that a substring search for it cannot collide with
# ordinary log text, so "the sentinel is absent" means the record really was
# discarded rather than merely reformatted.
_LIBRARY_DEBUG_SENTINEL = "Token 4f2b9ac81de7350649fc2e0bd85a71c3"

# The HTTP-stack loggers whose DEBUG output must never reach saneless's log,
# listed here rather than imported so that dropping a name from the module's own
# list fails a test. ``httpcore2.http11`` is the child that writes the header
# trace; it has no level of its own and must inherit the cap from its parent.
# ``python_multipart`` is the real logger name, ``multipart`` its shim.
_LIBRARY_LOGGER_NAMES = (
    "httpx2",
    "httpcore2",
    "httpcore2.http11",
    "hpack",
    "multipart",
    "python_multipart",
)


def _reset_library_loggers() -> None:
    """Return every HTTP library logger to NOTSET, so no test's level leaks."""
    for name in _LIBRARY_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.NOTSET)


def _raise_runtime_error(message: str) -> None:
    """
    Raise a RuntimeError carrying ``message``, so a test can log a real traceback.

    Args:
        message: The exception's message.

    Raises:
        RuntimeError: Always.

    """
    raise RuntimeError(message)


def _deny_mkdir(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    """
    Make creating ``directory`` fail with EACCES; every other mkdir still works.

    Injecting the failure rather than taking a directory's permissions away
    keeps the test meaningful as root, whom file modes do not stop.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        directory: The directory whose creation must be refused.

    """
    real_mkdir = Path.mkdir

    def denied(
        self: Path, mode: int = 0o777, *, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if self == directory:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(self))
        real_mkdir(self, mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", denied)


def _mode(path: Path) -> int:
    """Return the permission bits of ``path``, not following a symlink."""
    return stat.S_IMODE(path.lstat().st_mode)


@pytest.fixture
def umask_022() -> Iterator[None]:
    """
    Run the test under umask 022, the common default.

    Under it a plain create gives a file 0644 and a directory 0755, both
    readable by every local user; the umask is restored afterwards.
    """
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


class TestConfigureLogging:
    """Logging setup tests."""

    def _cleanup_handlers(self) -> None:
        """
        Remove all handlers from root logger to prevent leaks.

        The ``saneless`` logger is reset too: a verbose call sets it to DEBUG,
        and that must not leak into whichever test runs next. So are the HTTP
        library loggers, whose level every call sets.
        """
        root = logging.getLogger()
        for handler in root.handlers[:]:
            handler.close()
            root.removeHandler(handler)
        root.setLevel(logging.WARNING)
        logging.getLogger("saneless").setLevel(logging.NOTSET)
        _reset_library_loggers()

    def test_configure_logging_creates_file_handler(self, tmp_path: Path) -> None:
        """configure_logging adds a RotatingFileHandler to the root logger."""
        log_file = tmp_path / "logs" / "test.log"
        try:
            configure_logging(log_file=log_file, max_bytes=1024, backup_count=1)
            root = logging.getLogger()
            file_handlers = [
                h
                for h in root.handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            assert len(file_handlers) >= 1
            assert file_handlers[0].baseFilename == str(log_file)
        finally:
            self._cleanup_handlers()

    def test_log_level_from_config(self, tmp_path: Path) -> None:
        """Log level DEBUG is applied to the root logger."""
        log_file = tmp_path / "test.log"
        try:
            configure_logging(
                log_file=log_file, log_level="DEBUG", max_bytes=1024, backup_count=1
            )
            root = logging.getLogger()
            assert root.level == logging.DEBUG
        finally:
            self._cleanup_handlers()

    def test_log_level_info(self, tmp_path: Path) -> None:
        """Log level INFO is applied to the root logger."""
        log_file = tmp_path / "test.log"
        try:
            configure_logging(
                log_file=log_file, log_level="INFO", max_bytes=1024, backup_count=1
            )
            root = logging.getLogger()
            assert root.level == logging.INFO
        finally:
            self._cleanup_handlers()

    def test_log_level_warning_and_critical_by_name(self, tmp_path: Path) -> None:
        """WARNING and CRITICAL resolve to their numeric levels (CFG-04)."""
        log_file = tmp_path / "test.log"
        try:
            configure_logging(
                log_file=log_file, log_level="WARNING", max_bytes=1024, backup_count=1
            )
            assert logging.getLogger().level == 30
            configure_logging(
                log_file=log_file, log_level="CRITICAL", max_bytes=1024, backup_count=1
            )
            assert logging.getLogger().level == 50
        finally:
            self._cleanup_handlers()

    def test_verbose_sets_saneless_loggers_to_debug_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """
        -v is DEBUG for saneless's own loggers, not for the root or libraries.

        The root logger keeps the configured level, and a DEBUG record emitted
        on a library's own logger is discarded before it reaches any handler.
        Two things now hold that record back: the root level, and the HTTP
        library loggers' own capped level (``TestLibraryLoggerCap``). The cap
        alone would still hide the sentinel if -v raised the root to DEBUG, so
        the root-level assertion at the end is the one that fails then.

        Both surfaces are asserted on: under -v the mirror handler sits on the
        root logger and writes to stderr as well as the file, so a file-only
        assertion would let a leak through the mirror pass unnoticed.
        """
        log_file = tmp_path / "test.log"
        try:
            configure_logging(
                log_file=log_file,
                log_level="INFO",
                max_bytes=1024,
                backup_count=1,
                verbose=True,
            )
            logging.getLogger("httpx2").debug(_LIBRARY_DEBUG_SENTINEL)
            for handler in logging.getLogger().handlers:
                handler.flush()
            assert _LIBRARY_DEBUG_SENTINEL not in log_file.read_text()
            assert _LIBRARY_DEBUG_SENTINEL not in capsys.readouterr().err
            assert (
                logging.getLogger("saneless.pipeline").getEffectiveLevel()
                == logging.DEBUG
            )
            assert logging.getLogger().level == logging.INFO
        finally:
            self._cleanup_handlers()

    def test_verbose_debug_record_reaches_log_file(self, tmp_path: Path) -> None:
        """A DEBUG record from a saneless logger is written under -v."""
        log_file = tmp_path / "test.log"
        try:
            configure_logging(
                log_file=log_file,
                log_level="INFO",
                max_bytes=1024,
                backup_count=1,
                verbose=True,
            )
            logging.getLogger("saneless.pipeline").debug("verbose-detail-7f3e")
            for handler in logging.getLogger().handlers:
                handler.flush()
            assert "verbose-detail-7f3e" in log_file.read_text()
        finally:
            self._cleanup_handlers()

    def test_non_verbose_call_resets_verbose_debug(self, tmp_path: Path) -> None:
        """A later non-verbose configure_logging does not inherit -v's DEBUG."""
        log_file = tmp_path / "test.log"
        try:
            configure_logging(
                log_file=log_file,
                log_level="INFO",
                max_bytes=1024,
                backup_count=1,
                verbose=True,
            )
            self._remove_root_handlers()
            configure_logging(
                log_file=log_file, log_level="WARNING", max_bytes=1024, backup_count=1
            )
            assert logging.getLogger("saneless").level == logging.NOTSET
            assert (
                logging.getLogger("saneless.pipeline").getEffectiveLevel()
                == logging.WARNING
            )
        finally:
            self._cleanup_handlers()

    @staticmethod
    def _remove_root_handlers() -> None:
        """Close and detach the root handlers without touching any level."""
        root = logging.getLogger()
        for handler in root.handlers[:]:
            handler.close()
            root.removeHandler(handler)

    def test_log_rotation_params(self, tmp_path: Path) -> None:
        """Custom max_bytes and backup_count are passed to RotatingFileHandler."""
        log_file = tmp_path / "test.log"
        try:
            configure_logging(
                log_file=log_file,
                max_bytes=1000,
                backup_count=3,
            )
            root = logging.getLogger()
            file_handlers = [
                h
                for h in root.handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            assert len(file_handlers) >= 1
            assert file_handlers[0].maxBytes == 1000
            assert file_handlers[0].backupCount == 3
        finally:
            self._cleanup_handlers()

    def test_log_format_includes_timestamp_and_module(self, tmp_path: Path) -> None:
        """Log messages include timestamp, module name, and level."""
        log_file = tmp_path / "test.log"
        try:
            configure_logging(
                log_file=log_file, log_level="INFO", max_bytes=1024, backup_count=1
            )
            logger = logging.getLogger("test_format")
            logger.info("test message")
            content = log_file.read_text()
            assert re.search(r"\d{4}-\d{2}-\d{2}.*\w+.*INFO", content)
        finally:
            self._cleanup_handlers()

    def test_verbose_adds_stderr_handler(self, tmp_path: Path) -> None:
        """verbose=True adds a StreamHandler alongside the file handler."""
        log_file = tmp_path / "test.log"
        try:
            configure_logging(
                log_file=log_file, max_bytes=1024, backup_count=1, verbose=True
            )
            root = logging.getLogger()
            stream_handlers = [
                h
                for h in root.handlers
                if isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            assert len(stream_handlers) >= 1
        finally:
            self._cleanup_handlers()

    def test_unwritable_directory_falls_back_to_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A log directory that cannot be created falls back to stderr, no raise.

        The PermissionError is injected at the directory creation, so the test
        means the same thing when it runs as root, where a mode-0 directory
        would not stop anything.
        """
        log_file = tmp_path / "noperm" / "subdir" / "test.log"
        _deny_mkdir(monkeypatch, log_file.parent)
        try:
            attached = configure_logging(
                log_file=log_file, max_bytes=1024, backup_count=1
            )
            assert attached is False
            assert not log_file.parent.exists()
            assert len(_stderr_stream_handlers()) == 1, "Expected stderr fallback"
        finally:
            self._cleanup_handlers()

    def test_returns_true_when_the_file_handler_attached(self, tmp_path: Path) -> None:
        """
        A writable log file returns True and attaches a RotatingFileHandler.

        The CLI prints "Full details in <log_file>" only on True (D-06).
        """
        log_file = tmp_path / "logs" / "saneless.log"
        try:
            attached = configure_logging(
                log_file, "INFO", max_bytes=1024, backup_count=1
            )
            assert attached is True
            file_handlers = [
                h
                for h in logging.getLogger().handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            assert [h.baseFilename for h in file_handlers] == [str(log_file)]
        finally:
            self._cleanup_handlers()

    def test_returns_false_when_the_file_handler_is_not_attached(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A log path under a regular file returns False and falls back to stderr.

        mkdir fails with an OSError whoever runs the test (root included), so
        no permission trick is needed.
        """
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")
        log_file = blocker / "logs" / "saneless.log"
        try:
            with caplog.at_level(logging.WARNING):
                attached = configure_logging(
                    log_file, "INFO", max_bytes=1024, backup_count=1
                )
            assert attached is False
            root = logging.getLogger()
            assert not [
                h
                for h in root.handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            assert [
                h
                for h in root.handlers
                if isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.FileHandler)
                and h.stream is sys.stderr
            ]
            assert f"Cannot write to {log_file}, logging to stderr only" in (
                caplog.messages
            )
        finally:
            self._cleanup_handlers()

    def test_stderr_fallback_renders_no_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """
        The stderr fallback prints a failure's message, never its traceback.

        stderr is the user's terminal once the log file cannot be opened, and
        a traceback reaches it only with -v (CR-01, D-06).
        """
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")
        try:
            configure_logging(
                blocker / "logs" / "saneless.log", max_bytes=1024, backup_count=1
            )
            try:
                _raise_runtime_error("kaboom-4c1d")
            except RuntimeError:
                logging.getLogger("saneless.test").exception("it failed")
            err = capsys.readouterr().err
            assert "it failed" in err
            assert "Traceback" not in err
            assert "kaboom-4c1d" not in err
        finally:
            self._cleanup_handlers()

    def test_stderr_fallback_with_verbose_renders_the_traceback_once(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With -v the mirror handler renders the traceback, and only it does."""
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")
        try:
            configure_logging(
                blocker / "logs" / "saneless.log",
                max_bytes=1024,
                backup_count=1,
                verbose=True,
            )
            try:
                _raise_runtime_error("kaboom-9e2a")
            except RuntimeError:
                logging.getLogger("saneless.test").exception("it failed")
            err = capsys.readouterr().err
            assert err.count("Traceback") == 1
        finally:
            self._cleanup_handlers()

    @pytest.mark.usefixtures("umask_022")
    def test_new_log_file_and_its_directory_are_owner_only(
        self, tmp_path: Path
    ) -> None:
        """
        A log file and directory created here are 0600 and 0700 under umask 022.

        The log carries document titles, so another local user must not be
        able to read it. Under 022 a plain create would give 0644 and 0755.
        """
        log_file = tmp_path / "logs" / "saneless.log"
        try:
            assert configure_logging(log_file, max_bytes=1024, backup_count=1)
            assert _mode(log_file.parent) == 0o700
            assert _mode(log_file) == 0o600
        finally:
            self._cleanup_handlers()

    @pytest.mark.usefixtures("umask_022")
    def test_log_file_started_by_rotation_is_owner_only(self, tmp_path: Path) -> None:
        """The fresh file rotation opens is 0600, and the rotated one keeps 0600."""
        log_file = tmp_path / "saneless.log"
        try:
            configure_logging(log_file, max_bytes=200, backup_count=1)
            logger = logging.getLogger("saneless.test_rotation")
            for number in range(10):
                logger.warning("record %d fills the log toward rotation", number)
            rotated = tmp_path / "saneless.log.1"
            assert rotated.exists(), "the log never rotated; the test proves nothing"
            assert _mode(rotated) == 0o600
            assert _mode(log_file) == 0o600
        finally:
            self._cleanup_handlers()

    @pytest.mark.usefixtures("umask_022")
    def test_existing_log_file_and_directory_keep_their_modes(
        self, tmp_path: Path
    ) -> None:
        """A log and directory an earlier release left 0644 and 0755 are not changed."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_dir.chmod(0o755)
        log_file = log_dir / "saneless.log"
        log_file.write_text("")
        log_file.chmod(0o644)
        try:
            assert configure_logging(log_file, max_bytes=1024, backup_count=1)
            logging.getLogger("saneless.test_modes").warning("appended")
            assert _mode(log_dir) == 0o755
            assert _mode(log_file) == 0o644
            assert "appended" in log_file.read_text()
        finally:
            self._cleanup_handlers()

    def test_default_log_file_is_xdg_compliant(self) -> None:
        """OutputConfig.log_file default uses XDG state dir, not /var/log."""
        config = OutputConfig()
        assert config.log_file.parts[-4:] == (
            ".local",
            "state",
            "saneless",
            "saneless.log",
        )
        assert not config.log_file.is_relative_to("/var/log")


def _stderr_stream_handlers() -> list[logging.Handler]:
    """
    Return the root logger's handlers that write to the real ``sys.stderr``.

    pytest's own capture handlers are ``StreamHandler`` subclasses over a
    ``StringIO``, so filtering on the stream identity counts only the handlers
    ``configure_logging`` attached.

    Returns:
        The matching handlers, in root-logger order.

    """
    root = logging.getLogger()
    return [
        h
        for h in root.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        and h.stream is sys.stderr
    ]


class TestConfigureLoggingStreamMode:
    """No log file: the 12-factor service shape ``serve`` uses (D-35, DLVR-04)."""

    def _cleanup_handlers(self) -> None:
        """
        Remove all handlers from root logger to prevent leaks.

        The ``saneless`` logger is reset too: a verbose call sets it to DEBUG,
        and that must not leak into whichever test runs next. So are the HTTP
        library loggers, whose level every call sets.
        """
        root = logging.getLogger()
        for handler in root.handlers[:]:
            handler.close()
            root.removeHandler(handler)
        root.setLevel(logging.WARNING)
        logging.getLogger("saneless").setLevel(logging.NOTSET)
        _reset_library_loggers()

    def test_stream_mode_attaches_no_file_handler(self) -> None:
        """
        With no log file nothing on the root logger writes to disk (D-40).

        That the configured ``log_file`` path is left untouched -- no file, no
        parent directory -- is pinned end to end at the CLI seam, by
        ``tests/test_cli.py::TestServeLogging``.
        """
        try:
            configure_logging(None, max_bytes=1024, backup_count=1)
            root = logging.getLogger()
            assert not [h for h in root.handlers if isinstance(h, logging.FileHandler)]
        finally:
            self._cleanup_handlers()

    def test_stream_mode_attaches_one_stderr_handler_at_the_configured_level(
        self,
    ) -> None:
        """Exactly one stderr handler, root at the configured level (D-36)."""
        try:
            configure_logging(None, "DEBUG", max_bytes=1024, backup_count=1)
            assert len(_stderr_stream_handlers()) == 1
            assert logging.getLogger().level == logging.DEBUG
        finally:
            self._cleanup_handlers()

    def test_stream_mode_returns_false(self) -> None:
        """
        Stream mode returns False, so the caller records no ``log_file``.

        ``ctx.obj["log_file"] = settings.output.log_file if attached else None``
        needs no edit: nothing can print "Full details in <log_file>" for a
        service that writes no file.
        """
        try:
            assert configure_logging(None, max_bytes=1024, backup_count=1) is False
        finally:
            self._cleanup_handlers()

    def test_stream_mode_renders_the_traceback_without_verbose(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """
        The serve stream renders a traceback without ``-v`` (D-36 amended).

        The inverse of ``test_stderr_fallback_renders_no_traceback``, and
        deliberately so: the stream *is* the log here, and no file carries the
        traceback instead.
        """
        try:
            configure_logging(None, max_bytes=1024, backup_count=1)
            try:
                _raise_runtime_error("kaboom-71bd")
            except RuntimeError:
                logging.getLogger("saneless.test").exception("it failed")
            err = capsys.readouterr().err
            assert "it failed" in err
            assert "Traceback" in err
            assert "kaboom-71bd" in err
        finally:
            self._cleanup_handlers()

    def test_stream_mode_renders_the_traceback_with_verbose(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With ``-v`` the serve stream renders the traceback exactly once."""
        try:
            configure_logging(None, max_bytes=1024, backup_count=1, verbose=True)
            try:
                _raise_runtime_error("kaboom-3f0e")
            except RuntimeError:
                logging.getLogger("saneless.test").exception("it failed")
            err = capsys.readouterr().err
            assert err.count("Traceback") == 1
            assert "kaboom-3f0e" in err
        finally:
            self._cleanup_handlers()

    def test_stream_mode_leaves_non_saneless_loggers_at_the_configured_level(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """
        ``-v`` in stream mode raises only saneless's own loggers.

        The root logger keeps the configured level in this mode too, so a DEBUG
        record emitted on a library's own logger is discarded before it reaches
        the one stderr handler. There is no file here, so that stream is the
        only surface a leak could reach, and the Paperless ``Authorization``
        header must not reach it. Raising the root to DEBUG puts the sentinel
        below on stderr, which is what makes this check falsifiable.
        """
        try:
            configure_logging(
                None, "INFO", max_bytes=1024, backup_count=1, verbose=True
            )
            logging.getLogger("httpx2").debug(_LIBRARY_DEBUG_SENTINEL)
            for handler in logging.getLogger().handlers:
                handler.flush()
            assert _LIBRARY_DEBUG_SENTINEL not in capsys.readouterr().err
            assert logging.getLogger("saneless").level == logging.DEBUG
            assert logging.getLogger().level == logging.INFO
            assert len(_stderr_stream_handlers()) == 1
        finally:
            self._cleanup_handlers()


def _handlers_named(name: str) -> list[logging.Handler]:
    """
    Return the root logger's handlers carrying ``name``.

    Args:
        name: The handler name to match exactly.

    Returns:
        The matching handlers, in root-logger order.

    """
    return [h for h in logging.getLogger().handlers if h.get_name() == name]


class TestConfigureLoggingIsIdempotent:
    """Repeated calls replace saneless's own handlers and leave the rest alone."""

    @pytest.fixture(autouse=True)
    def _restore_root(self) -> Iterator[None]:
        """
        Remove every handler the test added and reset the levels it changed.

        Handlers that were on the root before the test (pytest's capture
        handlers) are left in place.

        Yields:
            Nothing; the restore runs after the test.

        """
        root = logging.getLogger()
        before = root.handlers[:]
        level = root.level
        yield
        for handler in root.handlers[:]:
            if handler not in before:
                handler.close()
                root.removeHandler(handler)
        root.setLevel(level)
        logging.getLogger("saneless").setLevel(logging.NOTSET)
        _reset_library_loggers()

    def test_two_file_mode_calls_leave_one_file_handler(self, tmp_path: Path) -> None:
        """A second call replaces the first call's file handler, not adds to it."""
        log_file = tmp_path / "logs" / "saneless.log"
        configure_logging(log_file, max_bytes=1024, backup_count=1)
        configure_logging(log_file, max_bytes=1024, backup_count=1)

        file_handlers = _handlers_named("saneless.file")
        assert len(file_handlers) == 1
        assert isinstance(file_handlers[0], logging.handlers.RotatingFileHandler)
        assert _stderr_stream_handlers() == []

    def test_a_handler_saneless_did_not_install_survives(self, tmp_path: Path) -> None:
        """Only handlers configure_logging installed are removed by a later call."""
        foreign = logging.Handler()
        logging.getLogger().addHandler(foreign)
        log_file = tmp_path / "saneless.log"

        configure_logging(log_file, max_bytes=1024, backup_count=1)
        configure_logging(log_file, max_bytes=1024, backup_count=1)

        assert foreign in logging.getLogger().handlers

    def test_two_service_mode_calls_leave_one_stream_handler(self) -> None:
        """Two service-mode calls leave one stderr stream, so no record doubles."""
        configure_logging(None, max_bytes=1024, backup_count=1)
        configure_logging(None, max_bytes=1024, backup_count=1)

        assert len(_handlers_named("saneless.stream")) == 1
        assert len(_stderr_stream_handlers()) == 1

    def test_unwritable_log_with_verbose_prints_each_record_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """
        With no log file and -v, one stderr handler prints message and traceback.

        Two stderr handlers would print every record twice to the same stream.
        """
        log_file = tmp_path / "logs" / "saneless.log"
        _deny_mkdir(monkeypatch, log_file.parent)

        configure_logging(log_file, max_bytes=1024, backup_count=1, verbose=True)
        try:
            _raise_runtime_error("kaboom-5a17")
        except RuntimeError:
            logging.getLogger("saneless.test").exception("it failed 5a17")

        assert len(_stderr_stream_handlers()) == 1
        err = capsys.readouterr().err
        assert err.count("it failed 5a17") == 1
        assert err.count("Traceback") == 1
        assert err.count("Cannot write to") == 1

    def test_unwritable_log_without_verbose_prints_no_traceback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Without -v the one stderr handler still keeps tracebacks off the terminal."""
        log_file = tmp_path / "logs" / "saneless.log"
        _deny_mkdir(monkeypatch, log_file.parent)

        configure_logging(log_file, max_bytes=1024, backup_count=1)
        try:
            _raise_runtime_error("kaboom-2b64")
        except RuntimeError:
            logging.getLogger("saneless.test").exception("it failed 2b64")

        assert len(_stderr_stream_handlers()) == 1
        err = capsys.readouterr().err
        assert err.count("it failed 2b64") == 1
        assert "Traceback" not in err


class TestLibraryLoggerCap:
    """
    The HTTP library loggers stay at INFO or above whatever the log level says.

    ``output.log_level = "DEBUG"`` sets the root logger, and without a level of
    their own the library loggers would inherit it. httpcore2's DEBUG trace
    carries response headers today, and a request header -- the Paperless
    Authorization header among them -- is one library release away. The cap
    keeps that out of saneless's log on saneless's side, not the library's.
    """

    @pytest.fixture(autouse=True)
    def _restore_root(self) -> Iterator[None]:
        """
        Remove every handler the test added and reset the levels it changed.

        Yields:
            Nothing; the restore runs after the test.

        """
        root = logging.getLogger()
        before = root.handlers[:]
        level = root.level
        yield
        for handler in root.handlers[:]:
            if handler not in before:
                handler.close()
                root.removeHandler(handler)
        root.setLevel(level)
        logging.getLogger("saneless").setLevel(logging.NOTSET)
        _reset_library_loggers()

    @pytest.mark.parametrize("name", _LIBRARY_LOGGER_NAMES)
    def test_debug_level_leaves_library_logger_off_debug(
        self, tmp_path: Path, name: str
    ) -> None:
        """Under a DEBUG root no HTTP library logger is enabled for DEBUG."""
        configure_logging(
            tmp_path / "test.log", "DEBUG", max_bytes=1024, backup_count=1
        )

        library_logger = logging.getLogger(name)
        assert not library_logger.isEnabledFor(logging.DEBUG)
        assert library_logger.getEffectiveLevel() >= logging.INFO

    def test_debug_level_keeps_library_header_trace_out_of_the_log(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """
        A DEBUG record on httpcore2's header-trace logger reaches no handler.

        ``-v`` is on so the stderr mirror is attached too, and both surfaces are
        read. A saneless DEBUG record written alongside it is the control: it
        proves the file really takes DEBUG records, so the sentinel's absence
        means the library record was discarded, not that nothing was written.
        """
        log_file = tmp_path / "test.log"
        configure_logging(
            log_file, "DEBUG", max_bytes=1024 * 1024, backup_count=1, verbose=True
        )

        logging.getLogger("httpcore2.http11").debug(_LIBRARY_DEBUG_SENTINEL)
        logging.getLogger("saneless.test").debug("control-7c1e")
        for handler in logging.getLogger().handlers:
            handler.flush()

        written = log_file.read_text()
        err = capsys.readouterr().err
        assert "control-7c1e" in written
        assert "control-7c1e" in err
        assert _LIBRARY_DEBUG_SENTINEL not in written
        assert _LIBRARY_DEBUG_SENTINEL not in err

    def test_warning_level_keeps_library_info_off(self, tmp_path: Path) -> None:
        """
        The cap never lowers a library logger below the configured level.

        A flat INFO would enable httpx2's per-request INFO line under a WARNING
        root, because a logger's own level, not the root's, gates emission.
        """
        configure_logging(
            tmp_path / "test.log", "WARNING", max_bytes=1024, backup_count=1
        )

        assert not logging.getLogger("httpx2").isEnabledFor(logging.INFO)

    def test_verbose_raises_saneless_but_not_the_library_loggers(
        self, tmp_path: Path
    ) -> None:
        """-v enables DEBUG for saneless's loggers and leaves httpcore2 alone."""
        configure_logging(
            tmp_path / "test.log", "INFO", max_bytes=1024, backup_count=1, verbose=True
        )

        assert logging.getLogger("saneless.pipeline").isEnabledFor(logging.DEBUG)
        assert not logging.getLogger("httpcore2").isEnabledFor(logging.DEBUG)

    def test_library_logger_level_follows_the_latest_call(self, tmp_path: Path) -> None:
        """A DEBUG call followed by a WARNING call leaves httpx2 at WARNING."""
        log_file = tmp_path / "test.log"
        configure_logging(log_file, "DEBUG", max_bytes=1024, backup_count=1)
        configure_logging(log_file, "WARNING", max_bytes=1024, backup_count=1)

        assert logging.getLogger("httpx2").level == logging.WARNING


def test_rotation_values_have_no_default() -> None:
    """
    max_bytes and backup_count come from the configuration, never a default.

    OutputConfig's log_max_bytes and log_backup_count are the one place those
    numbers are defined; a second default here could drift from them.
    """
    parameters = inspect.signature(configure_logging).parameters

    assert parameters["max_bytes"].default is inspect.Parameter.empty
    assert parameters["backup_count"].default is inspect.Parameter.empty
    assert len(parameters) == 5
