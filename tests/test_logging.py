"""Tests for logging configuration."""

import logging
import logging.handlers
import re

from saneless.config import OutputConfig
from saneless.logging_config import configure_logging


class TestConfigureLogging:
    """Logging setup tests."""

    def _cleanup_handlers(self):
        """Remove all handlers from root logger to prevent leaks."""
        root = logging.getLogger()
        for handler in root.handlers[:]:
            handler.close()
            root.removeHandler(handler)
        root.setLevel(logging.WARNING)

    def test_configure_logging_creates_file_handler(self, tmp_path):
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

    def test_log_level_from_config(self, tmp_path):
        """Log level DEBUG is applied to the root logger."""
        log_file = tmp_path / "test.log"
        try:
            configure_logging(log_file=str(log_file), log_level="DEBUG")
            root = logging.getLogger()
            assert root.level == logging.DEBUG
        finally:
            self._cleanup_handlers()

    def test_log_level_info(self, tmp_path):
        """Log level INFO is applied to the root logger."""
        log_file = tmp_path / "test.log"
        try:
            configure_logging(log_file=str(log_file), log_level="INFO")
            root = logging.getLogger()
            assert root.level == logging.INFO
        finally:
            self._cleanup_handlers()

    def test_log_rotation_params(self, tmp_path):
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

    def test_log_format_includes_timestamp_and_module(self, tmp_path):
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

    def test_verbose_adds_stderr_handler(self, tmp_path):
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

    def test_unwritable_directory_falls_back_to_stderr(self, tmp_path):
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

    def test_default_log_file_is_xdg_compliant(self):
        """OutputConfig.log_file default uses XDG state dir, not /var/log."""
        config = OutputConfig()
        assert ".local/state/saneless" in config.log_file
        assert "/var/log" not in config.log_file
