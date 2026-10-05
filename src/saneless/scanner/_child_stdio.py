"""
Keep a SANE child's reply pipe private, so C code cannot write on it.

A child the parent starts gets the reply's pipe as its stdout.  C stdio on a
pipe holds its output until the process exits, so a single ``printf`` in any
backend left on that pipe would put stray text in the middle of the reply.
Before anything else runs, the child therefore moves the pipe to a descriptor
of its own and points fd 1 at stderr, where a backend's chatter does no harm.

A closed stderr is filled first, on ``/dev/null``: a free fd 2 would be the
lowest free descriptor, so the pipe's copy would land on it and fd 1 would
then point straight back at the reply's pipe.

The child scripts run in a bare interpreter, so this imports the standard
library only.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import sys
from typing import Final

__all__ = ["flush_standard_streams", "take_reply_fd"]

_STDOUT_FD: Final = 1
"""The descriptor C code and Python's ``sys.stdout`` write to."""

_STDERR_FD: Final = 2
"""The descriptor the child inherits as saneless's stderr."""


def take_reply_fd() -> int:
    """
    Take the parent's stdout pipe for the reply, and point fd 1 at stderr.

    A closed stderr is first opened on ``/dev/null``.  That fd 2 stays
    inheritable, so a helper program a backend starts does not take it with
    its first ``open``.

    Returns:
        A new descriptor for the parent's pipe, owned by the caller.

    """
    try:
        os.fstat(_STDERR_FD)
    except OSError:
        sink = os.open(os.devnull, os.O_WRONLY)
        if sink == _STDERR_FD:
            # os.open makes its descriptor close-on-exec, and only dup2 would
            # have cleared that.  FD_CLOEXEC is the one descriptor flag.
            fcntl.fcntl(_STDERR_FD, fcntl.F_SETFD, 0)
        else:
            os.dup2(sink, _STDERR_FD)
            os.close(sink)
    reply_fd = os.dup(_STDOUT_FD)
    os.dup2(_STDERR_FD, _STDOUT_FD)
    return reply_fd


def flush_standard_streams() -> None:
    """
    Flush Python's stdout and stderr, as far as they can be flushed.

    Either stream is None when the child started with its descriptor closed,
    and a failed flush must not cost the reply that is already written.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.flush()
