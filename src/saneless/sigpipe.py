"""
Keep SIGPIPE blocked, so a write to a vanished peer raises instead of killing.

Python ignores SIGPIPE, but a C library can put it back to its default action
at the C level while ``signal.getsignal`` still reports ``SIG_IGN``, as libsane
does when it ends a scan read, whether the read succeeded or failed.  A write to
a peer that has gone, such as a paperless upload or a web client that hung up,
would then end the whole process silently.  A blocked SIGPIPE stays pending whatever the
disposition, and the write fails with EPIPE, raised as ``BrokenPipeError``.

The blocked mask belongs to a thread and is copied to every thread it starts,
so it is set once in the main thread, before any other thread exists.  It also
survives fork and exec, and ``subprocess`` resets only the disposition, not
the mask, so every child is launched inside ``sigpipe_unblocked`` and starts
with SIGPIPE unblocked.
"""

from __future__ import annotations

import contextlib
import signal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = ["block_sigpipe", "sigpipe_unblocked"]


def block_sigpipe() -> None:
    """
    Add SIGPIPE to the calling thread's blocked mask.

    Called from the main thread before any other thread starts, so every
    thread started afterwards inherits the mask.
    """
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGPIPE})


@contextlib.contextmanager
def sigpipe_unblocked() -> Generator[None]:
    """
    Unblock SIGPIPE in the calling thread for the body, then restore the mask.

    Meant for the launch of a child program, which inherits the mask of the
    thread that starts it and must not start with SIGPIPE blocked.

    A SIGPIPE already pending in this thread is discarded first: unblocked
    under the default action libsane may have put back, it would kill the
    process.

    Only the calling thread is affected.  The body should do no writes of its
    own to a pipe or socket whose peer can close; launching a process does
    none in the parent.

    Yields:
        None, while SIGPIPE is unblocked in the calling thread.

    """
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    try:
        if signal.SIGPIPE in previous:
            while signal.sigtimedwait({signal.SIGPIPE}, 0) is not None:
                pass
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGPIPE})
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
