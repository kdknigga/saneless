"""
Logging setup with RotatingFileHandler.

Configures the root logger at the configured level with a rotating file
handler. Verbose mode also mirrors records to stderr and logs saneless's own
loggers at DEBUG, while the root logger and third-party libraries keep the
configured level.
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
    then attaches a RotatingFileHandler to the root logger. If the directory
    cannot be created or the file cannot be opened, a stderr handler is
    attached instead and a warning names the log file; this function does not
    raise for an unwritable log, so the caller keeps running.

    Args:
        log_file: Path to the log file.
        log_level: Logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        max_bytes: Maximum log file size before rotation.
        backup_count: Number of rotated log files to keep.
        verbose: If True, also mirror to stderr and log saneless's own
            loggers at DEBUG.

    """
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    root_logger = logging.getLogger()
    # The level-name mapping rather than an attribute lookup on the module,
    # which would also "resolve" non-level names such as BASIC_FORMAT (CFG-04).
    root_logger.setLevel(logging.getLevelNamesMapping()[log_level.upper()])

    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except OSError:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        root_logger.addHandler(stderr_handler)
        root_logger.warning("Cannot write to %s, logging to stderr only", log_file)

    if verbose:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        root_logger.addHandler(stderr_handler)

    # -v is saneless's own detail. The root logger keeps the configured level so
    # httpx, multipart and uvicorn do not flood the log -- httpx's DEBUG output
    # can include the Paperless Authorization header (T-27-23). NOTSET on the
    # non-verbose path makes repeated calls idempotent instead of leaking an
    # earlier call's DEBUG.
    logging.getLogger("saneless").setLevel(logging.DEBUG if verbose else logging.NOTSET)
