"""
Load the C library's thread unwinder once, at startup, on a throwaway thread.

glibc does not load the unwinder (``libgcc_s``) that ending a thread needs
until the first time some thread ends through ``pthread_exit`` or is
cancelled.  That first time it ``dlopen``s the library, holding the dynamic
loader's lock and allocating memory as it goes.

Some SANE backends read the scanner on a thread of their own and stop it with
an *asynchronous* ``pthread_cancel``, which can kill a thread at any
instruction.  When the reader is the first thread in the process to end, a
cancel landing in that ``dlopen`` kills it with the loader's lock held.  Nothing
ever releases the lock, so the process hangs the next time anything loads a
library, or in its exit handlers.  Measured with libsane's ``test`` backend,
that was about one failed read in twenty.

Ending one thread of our own before any backend runs makes glibc load and keep
the unwinder there and then, so no backend thread ever has to.  The thread is
created and ended through the C library directly, because a Python thread
returns from its start routine instead of calling ``pthread_exit`` and would
load nothing.

It is a no-op anywhere but glibc on Linux: other C libraries load no unwinder
at thread exit, and saneless drives SANE only on Linux.
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

    Called from the main thread at startup, before SANE is initialised.
    Safe to call more than once: every call after the first loads nothing.
    A failure is logged at debug level and otherwise ignored, because the
    process works without it -- it only leaves the hazard described in the
    module docstring in place.
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
