"""
Logging setup for saneless's two operating shapes.

One-shot CLI commands configure the root logger at the configured level with a
rotating file handler. Verbose mode also mirrors records to stderr and logs
saneless's own loggers at DEBUG, while the root logger and third-party
libraries keep the configured level.

``saneless serve`` passes no log file at all instead: a service streams to
stderr and writes nothing to disk, so the platform -- ``docker logs``,
journald -- owns retention (D-35, DLVR-04).
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = ["configure_logging"]

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"


class _TracebackFreeFormatter(logging.Formatter):
    """
    A formatter that renders a record's message but never its traceback.

    Used for the stderr fallback when the log file cannot be opened and ``-v``
    was not given: stderr is then the user's terminal, and no user sees a
    traceback without asking for one (EXC-02, D-06, CR-01). The record itself
    keeps its ``exc_info``, so any other handler still renders it in full.
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        Format ``record`` without its exception or stack information.

        Args:
            record: The record to render.

        Returns:
            The formatted message line.

        """
        if record.exc_info is None and not record.exc_text and not record.stack_info:
            return super().format(record)
        stripped = logging.makeLogRecord(record.__dict__)
        stripped.exc_info = None
        stripped.exc_text = None
        stripped.stack_info = None
        return super().format(stripped)


def configure_logging(
    log_file: str | None,
    log_level: str = "INFO",
    max_bytes: int = 10_485_760,
    backup_count: int = 5,
    *,
    verbose: bool = False,
) -> bool:
    """
    Configure application logging for a one-shot command or for a service.

    Given a ``log_file`` -- the one-shot CLI shape -- this creates parent
    directories for it if they don't exist, then attaches a
    RotatingFileHandler to the root logger. If the directory cannot be created
    or the file cannot be opened, a stderr handler is attached instead and a
    warning names the log file; this function does not raise for an unwritable
    log, so the caller keeps running. That fallback handler renders each
    record's message but never its traceback: stderr is the user's terminal,
    and only ``-v`` puts a traceback there (D-06).

    Given ``None`` -- the service shape ``saneless serve`` asks for -- a single
    stderr handler is attached and nothing is written to disk: no file, no
    directory, no rotation, and tracebacks always rendered. ``max_bytes`` and
    ``backup_count`` are then unused, because they describe a rotation that
    does not happen (D-35, D-39, D-40, DLVR-04).

    ``None`` rather than a separate ``stream=True`` flag is deliberate twice
    over: a service genuinely has no log file, so the two modes cannot be
    asked for contradictorily; and ruff's ``PLR0913`` ceiling is five
    parameters, which this signature already sits on, and CLAUDE.md forbids
    both a suppression and raising the limit.

    Args:
        log_file: Path to the log file, or None to stream to stderr instead.
        log_level: Logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
            Applies in both modes.
        max_bytes: Maximum log file size before rotation. Unused when
            ``log_file`` is None.
        backup_count: Number of rotated log files to keep. Unused when
            ``log_file`` is None.
        verbose: If True, log saneless's own loggers at DEBUG; with a
            ``log_file`` this also mirrors records to stderr.

    Returns:
        Whether log records reach ``log_file``: True when the rotating file
        handler attached, False when logging fell back to stderr and False
        when there is no log file at all. The CLI uses it so "Full details in
        <log_file>" is printed only when it is true (D-06).

    """
    formatter = logging.Formatter(_FORMAT)

    root_logger = logging.getLogger()
    # The level-name mapping rather than an attribute lookup on the module,
    # which would also "resolve" non-level names such as BASIC_FORMAT (CFG-04).
    root_logger.setLevel(logging.getLevelNamesMapping()[log_level.upper()])

    if log_file is None:
        # The reader who knows the fallback branch below will expect
        # _TracebackFreeFormatter here too. It is deliberately the plain
        # formatter instead. D-06's traceback-free rule was justified by
        # "stderr is the user's terminal now" -- that is false for a service:
        # the stream *is* the log, and docker logs or journald is nobody's
        # terminal. There is also no file here to carry the traceback instead.
        # Swap in _TracebackFreeFormatter and every unexpected worker exception
        # leaves one message line in `docker logs` and nothing else, recoverable
        # only by restarting the service with -v (D-36 amended, DLVR-04).
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)
        attached = False
    else:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
            )
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
            attached = True
        except OSError:
            stderr_handler = logging.StreamHandler(sys.stderr)
            # stderr is the user's terminal now, so this handler never renders
            # a traceback (CR-01). With -v the mirror handler below renders it,
            # once, as D-06 promises.
            stderr_handler.setFormatter(_TracebackFreeFormatter(_FORMAT))
            root_logger.addHandler(stderr_handler)
            root_logger.warning("Cannot write to %s, logging to stderr only", log_file)
            attached = False

    # Only with a log file: without one the handler attached above already is a
    # plain-format stderr sink, so a mirror would print every record to the
    # same stream twice.
    if verbose and log_file is not None:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        root_logger.addHandler(stderr_handler)

    # -v is saneless's own detail. The root logger keeps the configured level so
    # httpx, multipart and uvicorn do not flood the log -- httpx's DEBUG output
    # can include the Paperless Authorization header (T-27-23). NOTSET on the
    # non-verbose path makes repeated calls idempotent instead of leaking an
    # earlier call's DEBUG.
    logging.getLogger("saneless").setLevel(logging.DEBUG if verbose else logging.NOTSET)
    return attached
