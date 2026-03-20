"""
Logging setup with RotatingFileHandler.

Configures the root logger with a rotating file handler and optional
stderr handler for verbose output.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = ["configure_logging"]


def configure_logging(
    log_file: str,
    log_level: str = "INFO",
    max_bytes: int = 10_485_760,
    backup_count: int = 5,
    *,
    verbose: bool = False,
) -> None:
    """
    Configure application logging with rotating file handler.

    Creates parent directories for the log file if they don't exist,
    then attaches a RotatingFileHandler to the root logger.

    Args:
        log_file: Path to the log file.
        log_level: Logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        max_bytes: Maximum log file size before rotation.
        backup_count: Number of rotated log files to keep.
        verbose: If True, also add a StreamHandler writing to stderr.

    """
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))
    root_logger.addHandler(file_handler)

    if verbose:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        root_logger.addHandler(stderr_handler)
