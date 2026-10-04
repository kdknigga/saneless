"""
Load the C library's thread unwinder in a scanner-library child, on a throwaway thread.

glibc ``dlopen``s the unwinder (``libgcc_s``) the first time a thread ends
through ``pthread_exit`` or is cancelled, holding the dynamic loader's lock.
Some SANE backends stop their reader thread with an *asynchronous*
``pthread_cancel``; if that reader is the first thread to end, the cancel can
kill it inside the ``dlopen`` with the lock held, and the process hangs the
next time anything loads a library, or at exit.

Each child process that loads python-sane calls this at its start, before
python-sane is imported.  Ending one thread of our own before any backend runs
makes glibc load and keep the unwinder there and then.  The thread is made through the C library
directly, because a Python thread returns from its start routine instead of
calling ``pthread_exit`` and would load nothing.  Other C libraries load no
unwinder at thread exit, so this is a no-op anywhere but glibc on Linux.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys

__all__ = ["load_thread_unwinder"]

logger = logging.getLogger(__name__)


def _is_glibc() -> bool:
    """
    Report whether this process runs on glibc.

    Returns:
        True on glibc; False on any other C library or platform.

    """
    if sys.platform != "linux":
        return False
    try:
        return bool(os.confstr("CS_GNU_LIBC_VERSION"))
    except ValueError, OSError:
        # Unknown name on this platform, or a C library that does not answer.
        return False


def load_thread_unwinder() -> None:
    """
    End one native thread through ``pthread_exit``, so glibc loads its unwinder.

    Called from a scanner-library child's main thread, before python-sane loads.
    Safe to call more than once. A failure is logged at debug level and
    otherwise ignored: the process works without it, with the hang left
    possible.
    """
    if not _is_glibc():
        return
    try:
        libc = ctypes.CDLL(None)
        pthread_create = libc.pthread_create
        pthread_create.argtypes = [
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        pthread_create.restype = ctypes.c_int
        pthread_join = libc.pthread_join
        pthread_join.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
        pthread_join.restype = ctypes.c_int
        # pthread_exit(NULL) as the start routine: the thread ends at once,
        # through the very call that makes glibc load the unwinder.
        start_routine = ctypes.cast(libc.pthread_exit, ctypes.c_void_p)
        thread = ctypes.c_ulong()
        created = pthread_create(ctypes.byref(thread), None, start_routine, None)
        if created != 0:
            logger.debug("Could not start the unwinder-loading thread: %d", created)
            return
        pthread_join(thread, None)
    except OSError, AttributeError:
        logger.debug("Could not load the thread unwinder up front", exc_info=True)
