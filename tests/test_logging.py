"""Tests for logging configuration."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from typing import TYPE_CHECKING

from saneless.config import OutputConfig
from saneless.logging_config import configure_logging

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _raise_runtime_error(message: str) -> None:
    """
    Raise a RuntimeError carrying ``message``, so a test can log a real traceback.

    Args:
        message: The exception's message.

    Raises:
        RuntimeError: Always.

    """
    raise RuntimeError(message)


class TestConfigureLogging:
    """Logging setup tests."""

    def _cleanup_handlers(self) -> None:
        """
        Remove all handlers from root logger to prevent leaks.

        The ``saneless`` logger is reset too: a verbose call sets it to DEBUG,
        and that must not leak into whichever test runs next.
        """
        root = logging.getLogger()
        for handler in root.handlers[:]:
            handler.close()
            root.removeHandler(handler)
        root.setLevel(logging.WARNING)
        logging.getLogger("saneless").setLevel(logging.NOTSET)

    def test_configure_logging_creates_file_handler(self, tmp_path: Path) -> None:
        """configure_logging adds a RotatingFileHandler to the root logger."""
        log_file = tmp_path / "logs" / "test.log"
        try:
            configure_logging(log_file=str(log_file))
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
            configure_logging(log_file=str(log_file), log_level="DEBUG")
            root = logging.getLogger()
            assert root.level == logging.DEBUG
        finally:
            self._cleanup_handlers()

    def test_log_level_info(self, tmp_path: Path) -> None:
        """Log level INFO is applied to the root logger."""
        log_file = tmp_path / "test.log"
        try:
            configure_logging(log_file=str(log_file), log_level="INFO")
            root = logging.getLogger()
            assert root.level == logging.INFO
        finally:
            self._cleanup_handlers()

    def test_log_level_warning_and_critical_by_name(self, tmp_path: Path) -> None:
        """WARNING and CRITICAL resolve to their numeric levels (CFG-04)."""
        log_file = tmp_path / "test.log"
        try:
            configure_logging(log_file=str(log_file), log_level="WARNING")
            assert logging.getLogger().level == 30
            configure_logging(log_file=str(log_file), log_level="CRITICAL")
            assert logging.getLogger().level == 50
        finally:
            self._cleanup_handlers()

    def test_verbose_sets_saneless_loggers_to_debug_only(self, tmp_path: Path) -> None:
        """
        -v is DEBUG for saneless's own loggers, not for the root or libraries.

        httpx logs request headers at DEBUG, and those can carry the Paperless
        Authorization header (T-27-23), so the root keeps the configured level.
        """
        log_file = tmp_path / "test.log"
        try:
            configure_logging(log_file=str(log_file), log_level="INFO", verbose=True)
            assert (
                logging.getLogger("saneless.pipeline").getEffectiveLevel()
                == logging.DEBUG
            )
            assert logging.getLogger("httpx").getEffectiveLevel() == logging.INFO
            assert logging.getLogger().level == logging.INFO
        finally:
            self._cleanup_handlers()

    def test_verbose_debug_record_reaches_log_file(self, tmp_path: Path) -> None:
        """A DEBUG record from a saneless logger is written under -v."""
        log_file = tmp_path / "test.log"
        try:
            configure_logging(log_file=str(log_file), log_level="INFO", verbose=True)
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
            configure_logging(log_file=str(log_file), log_level="INFO", verbose=True)
            self._remove_root_handlers()
            configure_logging(log_file=str(log_file), log_level="WARNING")
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
                log_file=str(log_file),
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
            configure_logging(log_file=str(log_file), log_level="INFO")
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
            configure_logging(log_file=str(log_file), verbose=True)
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

    def test_unwritable_directory_falls_back_to_stderr(self, tmp_path: Path) -> None:
        """configure_logging with unwritable dir does not raise, falls back to stderr."""
        unwritable = tmp_path / "noperm"
        unwritable.mkdir()
        unwritable.chmod(0o000)
        log_file = str(unwritable / "subdir" / "test.log")
        try:
            # Must not raise
            configure_logging(log_file=log_file)
            root = logging.getLogger()
            stream_handlers = [
                h
                for h in root.handlers
                if isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            assert len(stream_handlers) >= 1, "Expected stderr fallback handler"
        finally:
            unwritable.chmod(0o700)
            self._cleanup_handlers()

    def test_returns_true_when_the_file_handler_attached(self, tmp_path: Path) -> None:
        """
        A writable log file returns True and attaches a RotatingFileHandler.

        The CLI prints "Full details in <log_file>" only on True (D-06).
        """
        log_file = tmp_path / "logs" / "saneless.log"
        try:
            attached = configure_logging(str(log_file), "INFO", 1024, 1)
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
        log_file = str(blocker / "logs" / "saneless.log")
        try:
            with caplog.at_level(logging.WARNING):
                attached = configure_logging(log_file, "INFO", 1024, 1)
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
            configure_logging(str(blocker / "logs" / "saneless.log"))
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
            configure_logging(str(blocker / "logs" / "saneless.log"), verbose=True)
            try:
                _raise_runtime_error("kaboom-9e2a")
            except RuntimeError:
                logging.getLogger("saneless.test").exception("it failed")
            err = capsys.readouterr().err
            assert err.count("Traceback") == 1
        finally:
            self._cleanup_handlers()

    def test_default_log_file_is_xdg_compliant(self) -> None:
        """OutputConfig.log_file default uses XDG state dir, not /var/log."""
        config = OutputConfig()
        assert ".local/state/saneless" in config.log_file
        assert "/var/log" not in config.log_file


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
        and that must not leak into whichever test runs next.
        """
        root = logging.getLogger()
        for handler in root.handlers[:]:
            handler.close()
            root.removeHandler(handler)
        root.setLevel(logging.WARNING)
        logging.getLogger("saneless").setLevel(logging.NOTSET)

    def test_stream_mode_attaches_no_file_handler(self) -> None:
        """
        With no log file nothing on the root logger writes to disk (D-40).

        That the configured ``log_file`` path is left untouched -- no file, no
        parent directory -- is pinned end to end at the CLI seam, by
        ``tests/test_cli.py::TestServeLogging``.
        """
        try:
            configure_logging(None)
            root = logging.getLogger()
            assert not [h for h in root.handlers if isinstance(h, logging.FileHandler)]
        finally:
            self._cleanup_handlers()

    def test_stream_mode_attaches_one_stderr_handler_at_the_configured_level(
        self,
    ) -> None:
        """Exactly one stderr handler, root at the configured level (D-36)."""
        try:
            configure_logging(None, "DEBUG")
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
            assert configure_logging(None) is False
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
            configure_logging(None)
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
            configure_logging(None, verbose=True)
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
        self,
    ) -> None:
        """
        ``-v`` in stream mode raises only saneless's own loggers (D-38, T-27-23).

        httpx at DEBUG prints the Paperless ``Authorization`` header, so the
        root logger must keep the configured level in this mode too.
        """
        try:
            configure_logging(None, "INFO", verbose=True)
            assert logging.getLogger("saneless").level == logging.DEBUG
            assert logging.getLogger().level == logging.INFO
            assert len(_stderr_stream_handlers()) == 1
        finally:
            self._cleanup_handlers()
