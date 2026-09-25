"""
Logging setup for saneless's two operating shapes.

One-shot CLI commands configure the root logger at the configured level with a
rotating file handler. Verbose mode also mirrors records to stderr and logs
saneless's own loggers at DEBUG, while the root logger and third-party
libraries keep the configured level. The HTTP library loggers never go below
INFO, whatever that level is, because their DEBUG output carries headers.

``saneless serve`` passes no log file at all instead: a service streams to
stderr and writes nothing to disk, so the platform -- ``docker logs``,
journald -- owns retention.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, Final

from saneless.private_dirs import make_private_dir

if TYPE_CHECKING:
    from io import TextIOWrapper
    from pathlib import Path

__all__ = ["configure_logging"]

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

# The HTTP stack's loggers, held at INFO or above whatever the log level says.
# httpcore2's DEBUG trace writes headers; httpx2 is its client front end and
# hpack its HTTP/2 header codec (not installed today, and a name costs nothing
# when absent). python_multipart is the multipart parser's real logger name, and
# multipart the shim package that re-exports it. Children such as
# httpcore2.http11 inherit the level from their parent.
_LIBRARY_LOGGERS: Final = (
    "httpx2",
    "httpcore2",
    "hpack",
    "multipart",
    "python_multipart",
)


class _TracebackFreeFormatter(logging.Formatter):
    """
    A formatter that renders a record's message but never its traceback.

    Used for the stderr fallback when the log file cannot be opened and ``-v``
    was not given: stderr is then the user's terminal, and no user sees a
    traceback without asking for one. The record itself
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


# A log file this module creates is readable by its owner only: records carry
# document titles and preserved file names. An existing file keeps its mode.
_LOG_FILE_MODE: Final = 0o600


def _owner_only_opener(path: str, flags: int) -> int:
    """
    Open ``path`` as ``open`` would, creating a missing file 0600.

    The mode applies only when ``O_CREAT`` really creates the file, so a log
    an earlier release left with a wider mode is appended to unchanged.

    Args:
        path: The file to open.
        flags: The ``os.open`` flags ``open`` computed from its mode.

    Returns:
        The open file descriptor.

    """
    return os.open(path, flags, _LOG_FILE_MODE)


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """
    A rotating file handler whose every new log file is owner-only.

    ``RotatingFileHandler`` opens a new base file on construction and again
    after each rollover, both through ``_open`` and both with the umask's
    mode, 0644 under the usual 022. Overriding ``_open`` covers both. A
    rotated file is renamed, not copied, so it keeps the mode it was created
    with.
    """

    def _open(self) -> TextIOWrapper:
        """
        Open the base file for appending, creating it 0600 if it is missing.

        Returns:
            The stream the handler writes to.

        """
        return open(
            self.baseFilename,
            "a",
            encoding=self.encoding,
            errors=self.errors,
            opener=_owner_only_opener,
        )


_HANDLER_PREFIX = "saneless."


def _remove_own_handlers(root_logger: logging.Logger) -> None:
    """
    Detach and close the root handlers an earlier configure_logging installed.

    Only handlers whose name carries the ``saneless.`` prefix are touched. The
    root logger is shared, and a handler someone else put there -- pytest's
    log capture, an embedding application's own sink -- must keep working.

    Args:
        root_logger: The root logger to clean.

    """
    own = [
        handler
        for handler in root_logger.handlers
        if (handler.get_name() or "").startswith(_HANDLER_PREFIX)
    ]
    for handler in own:
        root_logger.removeHandler(handler)
        handler.close()


def configure_logging(
    log_file: Path | None,
    log_level: str = "INFO",
    *,
    max_bytes: int,
    backup_count: int,
    verbose: bool = False,
) -> bool:
    """
    Configure application logging for a one-shot command or for a service.

    Calling it again replaces what an earlier call set up instead of adding
    to it: every handler it installs is named with a ``saneless.`` prefix,
    and each call first removes and closes the root handlers carrying that
    prefix. Handlers it did not install are left alone, so a second call never
    prints a record twice and never silences someone else's handler.

    Given a ``log_file`` -- the one-shot CLI shape -- this creates parent
    directories for it if they don't exist, the innermost one 0700, then
    attaches a RotatingFileHandler to the root logger. Each log file it
    creates, the first one and every one a rotation starts, is 0600; an
    existing directory or file keeps its mode. If the directory cannot be created
    or the file cannot be opened, one stderr handler is attached instead and a
    warning names the log file; this function does not raise for an unwritable
    log, so the caller keeps running. Without ``verbose`` that fallback handler
    renders each record's message but never its traceback, because stderr is
    the user's terminal and a traceback there is only shown on request. With
    ``verbose`` it renders the traceback too, and it is the only stderr
    handler, so each record is printed once.

    Given ``None`` -- the service shape ``saneless serve`` asks for -- a single
    stderr handler is attached and nothing is written to disk: no file, no
    directory, no rotation, and tracebacks always rendered. ``max_bytes`` and
    ``backup_count`` are then unused, because they describe a rotation that
    does not happen.

    ``None`` rather than a separate ``stream=True`` flag is deliberate twice
    over: a service genuinely has no log file, so the two modes cannot be
    asked for contradictorily; and ruff's ``PLR0913`` ceiling is five
    parameters, which this signature already sits on, and CLAUDE.md forbids
    both a suppression and raising the limit.

    Args:
        log_file: Path to the log file, or None to stream to stderr instead.
        log_level: Logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
            Applies in both modes.
        max_bytes: Maximum log file size before rotation. It has no default:
            the configuration's ``log_max_bytes`` is the one source of the
            value. Unused when ``log_file`` is None.
        backup_count: Number of rotated log files to keep. It has no default
            for the same reason, with ``log_backup_count`` as the source.
            Unused when ``log_file`` is None.
        verbose: If True, log saneless's own loggers at DEBUG; with a
            ``log_file`` this also puts records, tracebacks included, on
            stderr.

    Returns:
        Whether log records reach ``log_file``: True when the rotating file
        handler attached, False when logging fell back to stderr and False
        when there is no log file at all. The CLI uses it so "Full details in
        <log_file>" is printed only when it is true.

    """
    formatter = logging.Formatter(_FORMAT)

    root_logger = logging.getLogger()
    _remove_own_handlers(root_logger)
    # The level-name mapping rather than an attribute lookup on the module,
    # which would also "resolve" non-level names such as BASIC_FORMAT.
    level = logging.getLevelNamesMapping()[log_level.upper()]
    root_logger.setLevel(level)

    if log_file is None:
        # The reader who knows the fallback branch below will expect
        # _TracebackFreeFormatter here too. It is deliberately the plain
        # formatter instead. The fallback's traceback-free rule is justified by
        # "stderr is the user's terminal now" -- that is false for a service:
        # the stream *is* the log, and docker logs or journald is nobody's
        # terminal. There is also no file here to carry the traceback instead.
        # Swap in _TracebackFreeFormatter and every unexpected worker exception
        # leaves one message line in `docker logs` and nothing else, recoverable
        # only by restarting the service with -v.
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.set_name("saneless.stream")
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)
        attached = False
    else:
        try:
            # Created owner-only, like the log file itself: a directory that
            # already exists keeps whatever mode it has.
            make_private_dir(log_file.parent)
            file_handler = _PrivateRotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
            )
            file_handler.set_name("saneless.file")
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
            attached = True
        except OSError:
            # The one stderr handler on this path. stderr is the user's
            # terminal now, so it renders a traceback only when -v asked for
            # one. It is attached before the warning below, which would
            # otherwise reach no handler at all.
            stderr_handler = logging.StreamHandler(sys.stderr)
            stderr_handler.set_name("saneless.stderr")
            stderr_handler.setFormatter(
                formatter if verbose else _TracebackFreeFormatter(_FORMAT)
            )
            root_logger.addHandler(stderr_handler)
            root_logger.warning("Cannot write to %s, logging to stderr only", log_file)
            attached = False

    # The -v mirror only accompanies a file that really attached. In service
    # mode, and on the fallback path, the handler above already writes to
    # stderr, and a second one would print every record twice.
    if verbose and attached:
        mirror_handler = logging.StreamHandler(sys.stderr)
        mirror_handler.set_name("saneless.stderr")
        mirror_handler.setFormatter(formatter)
        root_logger.addHandler(mirror_handler)

    # -v is saneless's own detail. The root logger keeps the configured level so
    # httpx2, multipart and uvicorn do not flood the log. NOTSET on the
    # non-verbose path makes repeated calls idempotent instead of leaking an
    # earlier call's DEBUG.
    logging.getLogger("saneless").setLevel(logging.DEBUG if verbose else logging.NOTSET)

    # The root level alone does not keep library DEBUG output out: a configured
    # log_level of DEBUG is the root's level, and a library logger without one
    # of its own inherits it. That output is not saneless's to audit --
    # httpcore2 logs full response headers today, nothing stops it logging
    # request headers tomorrow, and the Paperless Authorization header must not
    # reach a log. So each HTTP library logger gets its own level, the
    # configured one but never below INFO. Not a flat INFO: a logger's own
    # level, not the root's, gates emission, so INFO under a WARNING root would
    # switch httpx2's per-request line on. Setting it on every call keeps
    # repeated calls idempotent, as above.
    library_level = max(level, logging.INFO)
    for name in _LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(library_level)
    return attached
